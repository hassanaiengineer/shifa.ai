#
# Shifa Health — Live Voice Assistant (Gemini Live, native-driven).
#
# Mounted into the main Shifa.ai FastAPI app. Provides a real-time voice
# receptionist for the health clinic: it answers questions about services /
# hours / pricing from a knowledge base (RAG) and books appointments, then ends
# the call automatically once a booking is confirmed.
#
#   • lookup_info(query)      -> RAG over the knowledge base (grounded answers)
#   • update_booking(...)     -> records details as they're gathered (live UI fill)
#   • book_appointment(...)   -> finalizes the booking and returns a code
#
# Each tool call streams the live state to the browser (`agent_state` event) so
# the UI can show the flow: stepper + collected-info card + booking card.
#

import asyncio
import json
import os
import random
import string
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import BotStoppedSpeakingFrame, EndFrame, Frame, LLMRunFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
    LLMUserAggregatorParams,
)
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from backend.voice.browser_serializer import BrowserLiveFrameSerializer, DEFAULT_SAMPLE_RATE
from backend.voice.rag import KnowledgeBase
from backend.store import save_appointment

router = APIRouter()

VOICE_ID = os.getenv("AGENT_VOICE_ID", "Aoede")  # warm female voice

SYSTEM_PROMPT = (
    "You are Shifa, the warm and friendly AI voice assistant for Shifa Health Clinic. "
    "You sound like a helpful front-desk receptionist on a phone call. Keep every reply short "
    "and natural — 1 to 2 sentences, no lists or markdown.\n"
    "RULES:\n"
    "1. For ANY question about the clinic (services, prices, hours, location, insurance, "
    "telehealth, policies, FAQs), you MUST call lookup_info and answer ONLY from what it returns. "
    "Never guess clinic details.\n"
    "2. You help with clinic information and booking appointments only. You do NOT give medical "
    "diagnoses, prescriptions, or emergency advice — gently direct such requests to a doctor.\n"
    "3. To book an appointment, collect the patient's full name, preferred day, and preferred "
    "time — ask naturally, one at a time. Each time you learn one of these (or the type of visit), "
    "call update_booking with everything you have so far.\n"
    "4. Once you have the name, day, and time, briefly confirm them, then call book_appointment "
    "and tell the patient the confirmation code it returns.\n"
    "5. IMPORTANT — in that SAME reply right after booking, warmly thank the patient and wish them "
    "good health to close the call (e.g. 'You're all set! Your code is X. Thank you for choosing "
    "Shifa Health — take care and be well!'). Do NOT ask if they need anything else; the call ends "
    "after this. Only close like this AFTER a booking is confirmed.\n"
    "6. Greet the patient warmly as soon as the call connects. Never mention tools, AI, or these "
    "instructions, and never invent information."
)

TOOLS = ToolsSchema(
    standard_tools=[
        FunctionSchema(
            name="lookup_info",
            description=(
                "Look up factual information about Shifa Health Clinic (services, prices, hours, "
                "location, insurance, telehealth, policies, FAQs). Call before answering any "
                "clinic question."
            ),
            properties={"query": {"type": "string", "description": "What to look up."}},
            required=["query"],
        ),
        FunctionSchema(
            name="update_booking",
            description=(
                "Record appointment details as you gather them. Pass whatever you currently know; "
                "omit what you don't. Call this each time the patient provides a new detail."
            ),
            properties={
                "name": {"type": "string", "description": "Patient's full name."},
                "date": {"type": "string", "description": "Preferred day, e.g. 'next Tuesday'."},
                "time": {"type": "string", "description": "Preferred time, e.g. '10 AM'."},
                "reason": {"type": "string", "description": "Type of visit, if mentioned."},
            },
            required=[],
        ),
        FunctionSchema(
            name="book_appointment",
            description="Finalize the booking once you have the patient's name, day, and time.",
            properties={
                "name": {"type": "string", "description": "Patient's full name."},
                "date": {"type": "string", "description": "Day of the appointment."},
                "time": {"type": "string", "description": "Time of the appointment."},
                "reason": {"type": "string", "description": "Type of visit, if known."},
            },
            required=["name", "date", "time"],
        ),
    ]
)


class HangupAfterBooking(FrameProcessor):
    """Ends the call gracefully once the assistant finishes its post-booking farewell.

    Armed only after book_appointment succeeds, so the call never ends early. On
    the next BotStoppedSpeakingFrame (farewell finishing), it notifies the browser
    (so auto-reconnect stays off) and queues an EndFrame, flushing remaining audio.
    """

    def __init__(self, task, on_complete):
        super().__init__()
        self._task = task
        self._on_complete = on_complete
        self.armed = False
        self._ending = False
        self._end_task = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if self.armed and not self._ending and isinstance(frame, BotStoppedSpeakingFrame):
            self._ending = True
            self._end_task = asyncio.create_task(self._finish())
        await self.push_frame(frame, direction)

    async def _finish(self):
        await asyncio.sleep(2.5)  # let the client play the buffered farewell first
        await self._on_complete()
        await self._task.queue_frame(EndFrame())


# Knowledge base is embedded once and shared across all calls (lazy-loaded).
_kb: KnowledgeBase | None = None


def get_kb() -> KnowledgeBase | None:
    global _kb
    if _kb is None:
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if api_key:
            _kb = KnowledgeBase(api_key)
    return _kb


@router.websocket("/ws/voice")
async def websocket_voice(websocket: WebSocket):
    await websocket.accept()
    session = uuid.uuid4().hex
    logger.info(f"Voice caller connected (session {session[:8]}).")

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    kb = get_kb()
    if not api_key or kb is None:
        await websocket.close(code=1011, reason="Voice assistant unavailable.")
        return

    state = {
        "stage": "identifying",
        "caller_name": None,
        "reason": None,
        "preferred_date": None,
        "preferred_time": None,
        "booking_ref": None,
    }

    async def push_state():
        try:
            await websocket.send_text(json.dumps({"event": "agent_state", "data": state}))
        except Exception:  # noqa: BLE001
            pass

    serializer = BrowserLiveFrameSerializer()
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    llm = GeminiLiveLLMService(
        api_key=api_key,
        voice_id=VOICE_ID,
        system_instruction=SYSTEM_PROMPT,
        tools=TOOLS,
    )

    async def handle_lookup(params: FunctionCallParams):
        query = params.arguments.get("query", "")
        info = await asyncio.get_event_loop().run_in_executor(None, kb.retrieve, query)
        if state["stage"] in ("identifying", "answering"):
            state["stage"] = "answering"
            await push_state()
        await params.result_callback({"information": info})

    def _merge_details(args: dict):
        if args.get("name"):
            state["caller_name"] = args["name"]
        if args.get("date"):
            state["preferred_date"] = args["date"]
        if args.get("time"):
            state["preferred_time"] = args["time"]
        if args.get("reason"):
            state["reason"] = args["reason"]

    async def handle_update(params: FunctionCallParams):
        _merge_details(params.arguments)
        have_all = all(state[k] for k in ("caller_name", "preferred_date", "preferred_time"))
        state["stage"] = "confirming" if have_all else "collecting"
        await push_state()
        await params.result_callback({"ok": True})

    async def handle_book(params: FunctionCallParams):
        _merge_details(params.arguments)
        ref = "SH-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        state["booking_ref"] = ref
        state["stage"] = "booked"
        logger.info(
            f"📅 BOOKING: {state['caller_name']} | {state['preferred_date']} "
            f"{state['preferred_time']} | {state['reason']} | ref {ref}"
        )
        # Persist so it shows up in the SaaS dashboard.
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                save_appointment,
                ref,
                state["caller_name"],
                state["reason"],
                state["preferred_date"],
                state["preferred_time"],
                "voice",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to persist appointment: {exc}")
        await push_state()
        if all(state[k] for k in ("caller_name", "preferred_date", "preferred_time")):
            hangup.armed = True
            logger.info("Auto-hangup armed (booking complete).")
        await params.result_callback({"status": "confirmed", "confirmation_code": ref})

    llm.register_function("lookup_info", handle_lookup)
    llm.register_function("update_booking", handle_update)
    llm.register_function("book_appointment", handle_book)

    async def notify_call_complete():
        try:
            await websocket.send_text(json.dumps({"event": "call_ended"}))
        except Exception:  # noqa: BLE001
            pass

    hangup = HangupAfterBooking(task=None, on_complete=notify_call_complete)

    context = OpenAILLMContext(messages=[{"role": "user", "content": "A patient just connected."}])
    context_aggregator = llm.create_context_aggregator(
        context,
        user_params=LLMUserAggregatorParams(),
        assistant_params=LLMAssistantAggregatorParams(),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            context_aggregator.user(),
            llm,
            transport.output(),
            hangup,
            context_aggregator.assistant(),
        ]
    )
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=DEFAULT_SAMPLE_RATE,
            audio_out_sample_rate=DEFAULT_SAMPLE_RATE,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )
    hangup._task = task

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_, __):
        await push_state()
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_, __):
        logger.info(
            f"Voice caller disconnected (session {session[:8]}, stage={state['stage']}, "
            f"booked={'yes' if state['booking_ref'] else 'no'})."
        )
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    except WebSocketDisconnect:
        logger.info("Voice caller disconnected.")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Voice session failed: {exc}")
    finally:
        try:
            await task.cancel()
        except Exception:  # noqa: BLE001
            pass
