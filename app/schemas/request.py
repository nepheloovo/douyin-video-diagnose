"""Inbound diagnose request models (D1 v0.2 field names)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.modes import VideoSource


class DiagnoseRequest(BaseModel):
    """JSON / form fields for POST /diagnose.

    Input: aweme_url OR uploaded video file (二选一即可) + caption/cover 兜底.
    video_source is set by the router from which signal was provided.
    """

    aweme_url: Optional[str] = Field(
        default=None,
        description="Douyin share / aweme URL",
    )
    caption: Optional[str] = Field(
        default=None,
        description="Video caption / 文案 (merged with ASR/subtitle when available)",
    )
    extra_notes: Optional[str] = Field(
        default=None,
        description="Optional context for experts; not a required signal",
    )
    cover_url: Optional[str] = Field(
        default=None,
        description="Public cover image URL if not uploading multipart file",
    )
    cover_present: bool = Field(
        default=False,
        description="Set by router when multipart cover file was provided",
    )
    video_present: bool = Field(
        default=False,
        description="Set by router when multipart video_file was provided",
    )
    video_source: Optional[VideoSource] = Field(
        default=None,
        description="Locked D1: url | upload — how the video signal arrived",
    )

    @model_validator(mode="after")
    def at_least_one_signal(self) -> "DiagnoseRequest":
        has_url = bool(self.aweme_url and self.aweme_url.strip())
        has_caption = bool(self.caption and self.caption.strip())
        has_cover = self.cover_present or bool(self.cover_url and self.cover_url.strip())
        has_video = self.video_present
        if not (has_url or has_caption or has_cover or has_video):
            raise ValueError(
                "Provide at least one of: aweme_url, video_file, caption, "
                "cover (upload or cover_url)"
            )
        return self


class DiagnoseAccepted(BaseModel):
    job_id: str
