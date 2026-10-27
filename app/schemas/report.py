"""Report schema — PRD §4 / 课代表 v0.2 field lock.

「审核」= content hook/retention diagnosis, NOT compliance/ban audit.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.modes import DegradationMode


class Finding(BaseModel):
    claim: str
    evidence: str
    severity: Literal["info", "warn", "critical"] = "info"


class DimensionResult(BaseModel):
    name: str
    score: Optional[float] = Field(
        default=None,
        description="0–100; null when expert skipped for this mode",
    )
    skipped: bool = False
    skip_reason: Optional[str] = None
    findings: list[Finding] = Field(default_factory=list)


class Disagreement(BaseModel):
    topic: str
    sides: dict[str, str] = Field(
        default_factory=dict,
        description="expert_name → stance summary",
    )
    judge_resolution: str = ""


class TopFix(BaseModel):
    title: str
    why: str
    how: str
    expected_effect: str


class FrameMeta(BaseModel):
    """One extracted frame from t=0–3s (or cover fallback)."""

    path: str
    t_sec: float = Field(description="Timestamp in seconds within the clip")
    source: Literal["video_0_3s", "cover", "keyframe"] = "video_0_3s"


class DiagnoseReport(BaseModel):
    overall_score: Optional[float] = Field(
        default=None,
        description="Judge-mandatory aggregate 0–100; absent only on error paths",
    )
    mode: DegradationMode
    video_source: Optional[Literal["url", "upload"]] = Field(
        default=None,
        description="Where the video bytes came from; null when no video path",
    )
    degradation_flags: list[str] = Field(default_factory=list)
    one_liner: str = ""
    dimensions: list[DimensionResult] = Field(default_factory=list)
    disagreements: list[Disagreement] = Field(default_factory=list)
    final_scores: dict[str, Optional[float]] = Field(
        default_factory=dict,
        description="content / visual / growth → score or null if skipped",
    )
    verdict: str = ""
    top3_fixes: list[TopFix] = Field(default_factory=list, max_length=3)
    do_not: list[str] = Field(default_factory=list)
    # Extra diagnostics for UI (non-breaking). frames_0_3s listed for clients.
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description="Includes frames_0_3s summary, aweme_id, etc. Never cookies.",
    )
