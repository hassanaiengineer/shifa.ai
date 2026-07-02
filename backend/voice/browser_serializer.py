#
# WebSocket audio serializer for the browser voice client.
#
# Converts between Pipecat audio frames and a simple JSON protocol the browser
# understands:
#   • Bot audio      -> {"event": "media", "media": {"payload": <base64 pcm>}}
#   • Interruptions  -> {"event": "clear"}
#   • Mic audio in   -> {"event": "media", "media": {"payload": <base64 pcm>}}
#
# Audio is raw 16-bit mono PCM at LIVE_SAMPLE_RATE (default 16 kHz).
#

import base64
import json
import os
from typing import Optional

from pydantic import BaseModel

from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    InputAudioRawFrame,
    StartInterruptionFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

DEFAULT_SAMPLE_RATE = int(os.getenv("LIVE_SAMPLE_RATE", "16000"))


class BrowserLiveFrameSerializer(FrameSerializer):
    """Bridges Pipecat frames and the browser's JSON audio protocol."""

    class InputParams(BaseModel):
        sample_rate: int = DEFAULT_SAMPLE_RATE

    def __init__(self, params: Optional[InputParams] = None):
        self._params = params or self.InputParams()

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, AudioRawFrame):
            payload = base64.b64encode(frame.audio).decode("utf-8")
            return json.dumps({"event": "media", "media": {"payload": payload}})

        if isinstance(frame, StartInterruptionFrame):
            return json.dumps({"event": "clear"})

        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            return None

        event = message.get("event")
        if event == "media":
            payload_b64 = message.get("media", {}).get("payload")
            if not payload_b64:
                return None
            audio = base64.b64decode(payload_b64)
            return InputAudioRawFrame(
                audio=audio,
                num_channels=1,
                sample_rate=self._params.sample_rate,
            )

        # Ignore session markers from the browser.
        return None
