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
    video_attempted: bool = False,
) -> ModeDecision:
    """Pick a locked mode: video_3s | cover_caption | link_fail.

    Prefer video_3s when frames_0_3s exist.
    After that fails, any cover_url / cover_file / caption signal is
    cover_caption (yellow bar) and still runs experts, debate, and judge.
    With none of those signals, link_fail returns guidance only.
    """
    url_provided = bool(aweme_url and aweme_url.strip())
    has_url_meta = bool(parse and parse.ok and parse.aweme_id)
    flags: list[str] = []
    if parse:
        flags.extend(parse.degradation_flags)

    # Main path: first-3s frames → video_3s (even if caption empty)
    if has_frames:
        if not has_caption:
            flags.append("caption_empty_with_frames")
        run_growth = has_caption  # Growth needs caption/ASR for retention cues
        if not run_growth:
            flags.append("skip:growth")
        return ModeDecision(
            mode=DegradationMode.VIDEO_3S,
            has_cover=has_cover,
            has_caption=has_caption,
            has_url_meta=has_url_meta,
            has_frames=True,
            run_content=True,
            run_visual=True,
            run_growth=run_growth,
            degradation_flags=flags,
        )

    # No frames. Cover or caption is enough for the yellow-bar report.
    if has_cover or has_caption:
        flags.append("frames_extract_unavailable")
        if not has_cover:
            flags.append("visual_skipped_no_cover")
        if not has_caption:
            flags.append("growth_skipped_no_caption")
            flags.append("content_light_no_caption")
        run_visual = has_cover
        run_growth = has_caption
        if not run_visual:
            flags.append("skip:visual")
        if not run_growth:
            flags.append("skip:growth")
        return ModeDecision(
            mode=DegradationMode.COVER_CAPTION,
            has_cover=has_cover,
            has_caption=has_caption,
            has_url_meta=has_url_meta,
            has_frames=False,
            run_content=True,
            run_visual=run_visual,
            run_growth=run_growth,
            degradation_flags=flags,
        )

    return _link_fail_decision(
        flags=flags,
        url_provided=url_provided,
        has_url_meta=has_url_meta,
        parse=parse,
        video_attempted=video_attempted,
    )


def _link_fail_decision(
    *,
    flags: list[str],
    url_provided: bool,
    has_url_meta: bool,
    parse: Optional[ParseResult],
    video_attempted: bool,
) -> ModeDecision:
    """No frames and no cover/caption. Do not run experts, debate, or judge."""
    flags.append("link_fail")
    kind = ParseErrorKind.PARSE_ERROR
    if video_attempted:
        flags.append("frames_extract_unavailable")
        flags.append("insufficient_signals")
        message = (
            "Could not extract first-3s frames from the uploaded video, "
            "and no cover or caption was provided."
        )
        actionable = (
            "Re-upload a readable video file, or provide a cover "
            "(cover_file or cover_url) and a caption."
        )
    elif url_provided and not has_url_meta:
        kind = (parse.error_kind if parse else None) or ParseErrorKind.PARSE_ERROR
        message = (parse.error_message if parse else None) or "Failed to parse Douyin link"
        actionable = _actionable_for(kind)
        flags.append(f"parse:{kind.value}")
    elif has_url_meta:
        flags.append("insufficient_signals")
        message = (
            "Parsed video id but no cover/caption/frames available. "
            "Cannot diagnose from id alone (no cookies / no full scrape)."
        )
        actionable = (
            "Re-submit with video upload (preferred), or caption + cover "
            "(upload or cover_url), optionally keeping the aweme_url."
        )
    else:
        flags.append("insufficient_signals")
        message = "No cover, caption, or first-3s frames available to diagnose."
        actionable = (
            "Provide a video file, or a cover (cover_file or cover_url) and a caption."
        )

    return ModeDecision(
        mode=DegradationMode.LINK_FAIL,
        has_cover=False,
        has_caption=False,
        has_url_meta=has_url_meta,
        has_frames=False,
        run_content=False,
        run_visual=False,
        run_growth=False,
        degradation_flags=flags,
        link_fail=True,
        error_kind=kind,
        error_message=message,
        actionable=actionable,
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
