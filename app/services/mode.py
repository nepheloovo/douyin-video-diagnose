"""Degradation mode selection from available signals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.schemas.modes import DegradationMode, ParseErrorKind
from app.services.parse import ParseResult


@dataclass
class ModeDecision:
    mode: DegradationMode
    has_cover: bool
    has_caption: bool
    has_url_meta: bool
    has_frames: bool
    run_content: bool
    run_visual: bool
    run_growth: bool
    degradation_flags: list[str] = field(default_factory=list)
    # Link-fail path
    link_fail: bool = False
    error_kind: Optional[ParseErrorKind] = None
    error_message: Optional[str] = None
    actionable: Optional[str] = None


def decide_mode(
    *,
    has_cover: bool,
    has_caption: bool,
    aweme_url: Optional[str],
    parse: Optional[ParseResult],
    has_frames: bool = False,
) -> ModeDecision:
    """Pick degradation mode per PRD v0.2 table.

    Prefer video_3s when frames_0_3s exist.
    LinkFail: only-url (no cover, no caption, no frames) AND parse failed → do NOT debate.
    """
    url_provided = bool(aweme_url and aweme_url.strip())
    has_url_meta = bool(parse and parse.ok and parse.aweme_id)
    flags: list[str] = []
    if parse:
        flags.extend(parse.degradation_flags)

    # Pure link failure — no other diagnosable signal
    if (
        url_provided
        and not has_cover
        and not has_caption
        and not has_frames
        and not has_url_meta
    ):
        kind = (parse.error_kind if parse else None) or ParseErrorKind.PARSE_ERROR
        msg = (parse.error_message if parse else None) or "Failed to parse Douyin link"
        actionable = _actionable_for(kind)
        flags.append("link_fail")
        flags.append(f"parse:{kind.value}")
        return ModeDecision(
            mode=DegradationMode.LINK_FAIL,
            has_cover=False,
            has_caption=False,
            has_url_meta=False,
            has_frames=False,
            run_content=False,
            run_visual=False,
            run_growth=False,
            degradation_flags=flags,
            link_fail=True,
            error_kind=kind,
            error_message=msg,
            actionable=actionable,
        )

    # Main path: first-3s frames → video_3s (even if caption empty)
    if has_frames:
        mode = DegradationMode.VIDEO_3S
        if not has_caption:
            flags.append("caption_empty_with_frames")
        run_visual = True
        run_content = True  # Content runs on frames (+ caption when present)
        run_growth = has_caption  # Growth needs caption/ASR for retention cues
        if not run_growth:
            flags.append("skip:growth")
        return ModeDecision(
            mode=mode,
            has_cover=has_cover,
            has_caption=has_caption,
            has_url_meta=has_url_meta,
            has_frames=True,
            run_content=run_content,
            run_visual=run_visual,
            run_growth=run_growth,
            degradation_flags=flags,
        )

    # No frames — degrade
    if has_cover and has_caption:
        mode = DegradationMode.COVER_CAPTION
        flags.append("frames_extract_unavailable")
    elif has_caption and not has_cover:
        mode = DegradationMode.CAPTION_ONLY
        flags.append("visual_skipped_no_cover")
        flags.append("frames_extract_unavailable")
    elif has_cover and not has_caption:
        mode = DegradationMode.COVER_ONLY
        flags.append("growth_skipped_no_caption")
        flags.append("frames_extract_unavailable")
    elif has_cover and has_caption and has_url_meta:
        # unreachable after cover+caption branch; kept for clarity / legacy
        mode = DegradationMode.FULL
        flags.append("mvp_metadata_only")
    else:
        # URL meta alone without cover/caption/frames — not enough for experts
        kind = ParseErrorKind.PARSE_ERROR
        return ModeDecision(
            mode=DegradationMode.LINK_FAIL,
            has_cover=False,
            has_caption=False,
            has_url_meta=has_url_meta,
            has_frames=False,
            run_content=False,
            run_visual=False,
            run_growth=False,
            degradation_flags=flags + ["insufficient_signals", "link_fail"],
            link_fail=True,
            error_kind=kind,
            error_message=(
                "Parsed video id but no cover/caption/frames available. "
                "Cannot diagnose from id alone (no cookies / no full scrape)."
            ),
            actionable=(
                "Re-submit with video upload (preferred), or caption + cover "
                "(upload or cover_url), optionally keeping the aweme_url."
            ),
        )

    run_visual = has_cover
    run_growth = has_caption
    run_content = has_caption or has_cover

    if not run_visual:
        flags.append("skip:visual")
    if not run_growth:
        flags.append("skip:growth")
    if mode == DegradationMode.COVER_ONLY:
        flags.append("content_light_no_caption")

    return ModeDecision(
        mode=mode,
        has_cover=has_cover,
        has_caption=has_caption,
        has_url_meta=has_url_meta,
        has_frames=False,
        run_content=run_content,
        run_visual=run_visual,
        run_growth=run_growth,
        degradation_flags=flags,
    )


def _actionable_for(kind: ParseErrorKind) -> str:
    if kind == ParseErrorKind.TIMEOUT:
        return (
            "Link resolve timed out. Retry, or paste caption + cover directly "
            "and skip the URL."
        )
    if kind == ParseErrorKind.FETCH_BLOCKED:
        return (
            "Douyin short-link fetch was blocked (no cookies by design). "
            "Open the share link in a browser, copy the final "
            "douyin.com/video/{id} URL, or upload cover + paste caption."
        )
    return (
        "Could not parse a video id from this link. Use a standard "
        "douyin.com/video/{id} URL, or provide caption and/or cover without URL."
    )
