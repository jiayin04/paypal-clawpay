"""Request body for ``POST /api/v1/assistant/messages`` (watch / voice clients)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints


class AssistantMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # Text from Siri/dictation or the separate authenticated transcription endpoint.
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    # Drop the remembered conversation before handling this message.
    reset: bool = False
    # Who signs orders made with this token: the claw's button, or the watch's own key
    # (a watch registered as its own device). Only changes what the reply tells the user.
    approver: Literal["device", "watch"] = "device"


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
