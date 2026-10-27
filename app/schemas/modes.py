"""Degradation modes and parse-error taxonomy (PRD v0.2 / D1)."""

from enum import Enum


class DegradationMode(str, Enum):
    """MVP modes — prefer video_3s when frames_0_3s exist.

    video_3s       — main path: first-3s frames + caption
    cover_caption  — frame extract failed; cover + caption (yellow-flag)
    caption_only   — caption only
    cover_only     — cover only
    link_fail      — URL-only and parse failed
    full           — legacy alias for metadata-only (cover+caption+url meta);
                     demoted; prefer video_3s when frames exist
    """

    VIDEO_3S = "video_3s"
    COVER_CAPTION = "cover_caption"
    CAPTION_ONLY = "caption_only"
    COVER_ONLY = "cover_only"
    LINK_FAIL = "link_fail"
    FULL = "full"  # legacy / demoted — metadata-only, not full-frame analysis


class VideoSource(str, Enum):
    """Locked D1 field: how the video signal arrived."""

    URL = "url"
    UPLOAD = "upload"


class ParseErrorKind(str, Enum):
    PARSE_ERROR = "parse_error"
    FETCH_BLOCKED = "fetch_blocked"
    TIMEOUT = "timeout"


# Mode selection table (MVP — no full-video frame analysis; that is P1):
#
# | frames_0_3s | cover | caption | mode           | experts                    |
# |-------------|-------|---------|----------------|----------------------------|
# | Y           | *     | Y       | video_3s       | Content+Visual+Growth      |
# | Y           | *     | N       | video_3s*      | Visual+Content light       |
# | N           | Y     | Y       | cover_caption  | Content+Visual+Growth (⚠️) |
# | N           | N     | Y       | caption_only   | Content+Growth             |
# | N           | Y     | N       | cover_only     | Visual (+Content light)    |
# | only url fail | —   | —       | link_fail      | none — business 4xx        |
#
# * caption empty with frames still uses video_3s but Growth may skip.
# 「审核」= hook/retention diagnosis, NOT compliance / 限流 audit.
