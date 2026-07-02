#
# LangGraph receptionist agent — the stateful "brain" behind the voice.
#
# A real StateGraph with a typed state and a MemorySaver checkpointer drives a
# clean receptionist flow:
#
#   greet -> understand -> (answer | collect | confirm | book | farewell)
#
# The voice layer (Gemini Live) just relays: it sends each caller utterance to
# turn() and speaks back the returned `reply`. All conversation logic, slot
# filling, RAG, and booking live here — fully inspectable, which is what we
# stream to the UI so the client can SEE the agent work.
#

import json
import os
import random
import string
from typing import Optional, TypedDict

from google import genai
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from loguru import logger

from backend.voice.rag import KnowledgeBase

TEXT_MODEL = os.getenv("AGENT_TEXT_MODEL", "gemini-2.5-flash")
NLU_MODEL = os.getenv("AGENT_NLU_MODEL", "gemini-2.5-flash-lite")  # faster for extraction

# Ordered slots required to make a booking. Reason is captured if mentioned but
# never blocks the flow — we don't interrogate callers for a demo.
BOOKING_SLOTS = ["caller_name", "preferred_date", "preferred_time"]


class ReceptionistState(TypedDict, total=False):
    stage: str
    caller_name: Optional[str]
    patient_type: Optional[str]
    reason: Optional[str]
    preferred_date: Optional[str]
    preferred_time: Optional[str]
    booking_ref: Optional[str]
    last_user: str
    reply: str
    # transient, per-turn routing signals
    _intent: str
    _confirmation: str
    _question: str


class ReceptionistAgent:
    """Compiled LangGraph receptionist with per-session memory."""

    def __init__(self, api_key: str):
        self._client = genai.Client(api_key=api_key)
        self._kb = KnowledgeBase(api_key)
        self._graph = self._build()
        logger.info("LangGraph receptionist agent compiled.")

    # ---------- LLM helpers ----------
    def _understand_llm(self, state: ReceptionistState) -> dict:
        """Classify intent + extract any slots from the latest utterance."""
        known = {k: state.get(k) for k in ["caller_name", "reason", "preferred_date", "preferred_time", "stage"]}
        pending = state.get("reply") or ""
        prompt = (
            "You are the NLU unit of a dental clinic receptionist. Interpret the caller's reply "
            "IN THE CONTEXT of the question you just asked them, then return STRICT JSON (no "
            "markdown) with keys:\n"
            '{"intent": one of ["greeting","question","booking","provide_info","goodbye","other"],\n'
            ' "question": "the caller\'s question if intent is question, else empty",\n'
            ' "caller_name": "full name if they gave their name, else empty",\n'
            ' "reason": "reason for visit if mentioned (e.g. cleaning, checkup, whitening), else empty",\n'
            ' "preferred_date": "date if they gave one (e.g. next Tuesday), else empty",\n'
            ' "preferred_time": "time if they gave one (e.g. 10 AM), else empty",\n'
            ' "confirmation": "yes|no if they are confirming/declining, else empty"}\n\n'
            "IMPORTANT: If you just asked for their name and they reply with a name, fill "
            "caller_name. If you asked for a day and they say 'Tuesday', fill preferred_date. If "
            "you asked for a time and they say '10am', fill preferred_time. A short reply like "
            "'cleaning' or 'John Smith' is intent 'provide_info'.\n\n"
            f"You just asked the caller: \"{pending}\"\n"
            f"Known state so far: {json.dumps(known)}\n"
            f"Caller's reply: \"{state.get('last_user','')}\""
        )
        try:
            resp = self._client.models.generate_content(
                model=NLU_MODEL,
                contents=prompt,
                config={"response_mime_type": "application/json", "temperature": 0},
            )
            return json.loads(resp.text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"understand_llm failed, defaulting to 'other': {exc}")
            return {"intent": "other"}

    def _compose_answer(self, question: str) -> str:
        context = self._kb.retrieve(question)
        prompt = (
            "You are Bella, a warm dental clinic receptionist on a phone call. Using ONLY the "
            "facts below, answer the caller's question in 1-2 short, natural spoken sentences. "
            "No markdown or lists. If the facts don't cover it, say you'll have a team member follow up.\n\n"
            f"FACTS:\n{context}\n\nQUESTION: {question}"
        )
        resp = self._client.models.generate_content(
            model=TEXT_MODEL, contents=prompt, config={"temperature": 0.3}
        )
        return (resp.text or "").strip()

    # ---------- Graph nodes ----------
    def _understand(self, state: ReceptionistState) -> dict:
        user = (state.get("last_user") or "").strip()
        # Call-start sentinel: greet without an LLM call.
        if user == "__start__":
            return {"_intent": "greeting"}

        data = self._understand_llm(state)
        intent = data.get("intent", "other")
        updates: dict = {"_intent": intent, "_confirmation": data.get("confirmation", "")}
        if data.get("question"):
            updates["_question"] = data["question"]

        # Only fill booking slots once a booking is actually underway — otherwise a
        # question like "how much is whitening?" would wrongly set reason=whitening.
        booking_context = intent in ("booking", "provide_info") or state.get("stage") in (
            "collecting",
            "confirming",
        )
        slot_keys = ["caller_name", "patient_type"]
        if booking_context:
            slot_keys += ["reason", "preferred_date", "preferred_time"]
        for key in slot_keys:
            val = (data.get(key) or "").strip()
            if val:
                updates[key] = val
        return updates

    def _greet(self, state: ReceptionistState) -> dict:
        return {
            "reply": (
                "Hi there, thanks for calling BrightSmile Dental! I'm Bella, your virtual "
                "receptionist. How can I help you today?"
            ),
            "stage": "identifying",
        }

    def _answer(self, state: ReceptionistState) -> dict:
        question = state.get("_question") or state.get("last_user", "")
        reply = self._compose_answer(question)
        # Don't disturb an in-progress booking sub-flow.
        stage = state.get("stage")
        new_stage = stage if stage in ("collecting", "confirming", "booked") else "answering"
        return {"reply": reply, "stage": new_stage}

    def _collect(self, state: ReceptionistState) -> dict:
        name = state.get("caller_name")
        date = state.get("preferred_date")
        time = state.get("preferred_time")
        if not name:
            reply = "I'd be glad to book that for you. May I have your full name, please?"
        elif not date:
            reply = f"Thanks, {name}! What day works best for your visit?"
        elif not time:
            reply = f"Great. And what time on {date} would you prefer?"
        else:
            reply = "Let me just confirm those details."
        return {"reply": reply, "stage": "collecting"}

    def _confirm(self, state: ReceptionistState) -> dict:
        reason = state.get("reason")
        appt = f"a {reason} appointment" if reason else "an appointment"
        reply = (
            f"Just to confirm, {state.get('caller_name')} — {appt} on "
            f"{state.get('preferred_date')} at {state.get('preferred_time')}. Shall I book it?"
        )
        return {"reply": reply, "stage": "confirming"}

    def _book(self, state: ReceptionistState) -> dict:
        ref = "BS-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        reason = state.get("reason")
        appt = f"your {reason}" if reason else "your appointment"
        logger.info(
            f"📅 BOOKING CONFIRMED: {state.get('caller_name')} | {state.get('preferred_date')} "
            f"{state.get('preferred_time')} | {reason} | ref {ref}"
        )
        reply = (
            f"You're all set, {state.get('caller_name')}! I've booked {appt} for "
            f"{state.get('preferred_date')} at {state.get('preferred_time')}. Your confirmation "
            f"code is {ref}. Is there anything else I can help you with?"
        )
        return {"reply": reply, "stage": "booked", "booking_ref": ref}

    def _farewell(self, state: ReceptionistState) -> dict:
        return {
            "reply": "Thanks for calling BrightSmile Dental. Have a wonderful day, and take care of that smile!",
            "stage": state.get("stage", "ended"),
        }

    # ---------- Routing ----------
    def _route(self, state: ReceptionistState) -> str:
        intent = state.get("_intent", "other")
        stage = state.get("stage")
        confirmation = state.get("_confirmation", "")

        if intent == "goodbye":
            return "farewell"

        if stage == "confirming":
            if confirmation == "yes":
                return "book"
            if confirmation == "no":
                return "collect"
            if intent == "question":
                return "answer"
            return "confirm"

        if intent == "question":
            return "answer"

        booking_active = stage in ("collecting", "confirming")
        if intent in ("booking", "provide_info") or booking_active:
            if all(state.get(s) for s in BOOKING_SLOTS):
                return "confirm"
            return "collect"

        if intent == "greeting":
            return "greet"
        return "greet"

    def _build(self):
        g = StateGraph(ReceptionistState)
        g.add_node("understand", self._understand)
        g.add_node("greet", self._greet)
        g.add_node("answer", self._answer)
        g.add_node("collect", self._collect)
        g.add_node("confirm", self._confirm)
        g.add_node("book", self._book)
        g.add_node("farewell", self._farewell)

        g.add_edge(START, "understand")
        g.add_conditional_edges(
            "understand",
            self._route,
            {
                "greet": "greet",
                "answer": "answer",
                "collect": "collect",
                "confirm": "confirm",
                "book": "book",
                "farewell": "farewell",
            },
        )
        for node in ["greet", "answer", "collect", "confirm", "book", "farewell"]:
            g.add_edge(node, END)
        return g.compile(checkpointer=MemorySaver())

    # ---------- Public API ----------
    def turn(self, session_id: str, user_message: str) -> dict:
        """Run one conversation turn; returns the reply + a snapshot of state."""
        config = {"configurable": {"thread_id": session_id}}
        result = self._graph.invoke({"last_user": user_message}, config)
        return {
            "reply": result.get("reply", ""),
            "stage": result.get("stage", "greeting"),
            "caller_name": result.get("caller_name"),
            "patient_type": result.get("patient_type"),
            "reason": result.get("reason"),
            "preferred_date": result.get("preferred_date"),
            "preferred_time": result.get("preferred_time"),
            "booking_ref": result.get("booking_ref"),
        }
