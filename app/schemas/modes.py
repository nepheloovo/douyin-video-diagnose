"""Degradation modes and parse-error taxonomy (PRD v0.2 / D1)."""

from enum import Enum


class DegradationMode(str, Enum):
    """Locked product modes — no fourth mode.

    video_3s       — main path: first-3s frames exist
    cover_caption  — no frames; cover and/or caption still usable (yellow bar)
    link_fail      — URL/video failed and no cover or caption; error only
    """

    VIDEO_3S = "video_3s"
    COVER_CAPTION = "cover_caption"
    LINK_FAIL = "link_fail"


class VideoSource(str, Enum):
    """Locked D1 field: how the video signal arrived."""

    URL = "url"
    UPLOAD = "upload"


class ParseErrorKind(str, Enum):
    PARSE_ERROR = "parse_error"
    FETCH_BLOCKED = "fetch_blocked"
    TIMEOUT = "timeout"


# Mode selection (MVP — no full-video frame analysis; that is P1):
#
# | frames_0_3s | cover or caption                         | mode          | pipeline                         |
# |-------------|------------------------------------------|---------------|----------------------------------|
# | Y           | *                                        | video_3s      | Content+Visual+Growth            |
# | N           | any of cover_url / cover_file / caption | cover_caption | experts + debate + judge (⚠️)    |
# | N           | none                                     | link_fail     | none — error / guidance only     |
#
# video_3s with an empty caption still runs, but Growth may skip.
# cover_caption with only one of cover/caption still uses that mode; the
# missing side is skipped and recorded on degradation_flags.
# 「审核」= hook/retention diagnosis, NOT compliance / 限流 audit.

# Scope token sent to experts. Must not collide with a mode name.
DIAGNOSE_SCOPE = "first_3_seconds_and_caption"
