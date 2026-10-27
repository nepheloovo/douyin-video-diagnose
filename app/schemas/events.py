"""SSE / poll event payloads."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.schemas.report import DiagnoseReport


class EventPhase(str, Enum):
    PARSING = "parsing"
    EXPERTS = "experts"
    DEBATE = "debate"
    JUDGE = "judge"
    DONE = "done"
    ERROR = "error"


class DiagnoseEvent(BaseModel):
    phase: EventPhase
    message: str = ""
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    data: Optional[dict[str, Any]] = None
    report: Optional[DiagnoseReport] = None
    error_code: Optional[str] = None
