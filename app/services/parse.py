"""Douyin URL / share-link parse — best-effort, no cookies required."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx

from app.config import get_settings
from app.schemas.modes import ParseErrorKind

logger = logging.getLogger(__name__)

# Common patterns: v.douyin.com short links, www.douyin.com/video/{id},
# share text that embeds a URL, modal_id / aweme_id query params.
_AWEME_ID_RE = re.compile(r"(?:video|note|share/video)/(\d{6,})")
_MODAL_ID_RE = re.compile(r"[?&](?:modal_id|aweme_id|item_ids?)=(\d{6,})")
_BARE_ID_RE = re.compile(r"(?<!\d)(\d{15,21})(?!\d)")  # typical aweme id length
_URL_IN_TEXT_RE = re.compile(
    r"https?://[^\s<>\"']+|v\.douyin\.com/[A-Za-z0-9_\-]+/?",
    re.IGNORECASE,
)


@dataclass
class ParseResult:
    ok: bool
    aweme_id: Optional[str] = None
    canonical_url: Optional[str] = None
    resolved_url: Optional[str] = None
    caption_hint: Optional[str] = None
    cover_url_hint: Optional[str] = None
    error_kind: Optional[ParseErrorKind] = None
    error_message: Optional[str] = None
    degradation_flags: list[str] = field(default_factory=list)


def extract_urls(text: str) -> list[str]:
    return _URL_IN_TEXT_RE.findall(text or "")


def extract_aweme_id(url_or_text: str) -> Optional[str]:
    if not url_or_text:
        return None
    for pattern in (_AWEME_ID_RE, _MODAL_ID_RE):
        m = pattern.search(url_or_text)
        if m:
            return m.group(1)
    parsed = urlparse(url_or_text if "://" in url_or_text else f"https://{url_or_text}")
    qs = parse_qs(parsed.query)
    for key in ("modal_id", "aweme_id", "item_id", "item_ids"):
        if key in qs and qs[key]:
            val = qs[key][0]
            if val.isdigit() and len(val) >= 6:
                return val
    # Last resort: long numeric token in path
    m = _BARE_ID_RE.search(url_or_text)
    if m:
        return m.group(1)
    return None


async def _follow_short_link(url: str, timeout: float) -> tuple[Optional[str], Optional[ParseErrorKind], Optional[str]]:
    """HEAD/GET short link without cookies; capture final URL or classify failure."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; DouyinDiagnoseBot/0.1; +https://localhost)"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            # Prefer GET — some CDNs dislike bare HEAD
            resp = await client.get(url)
            final = str(resp.url)
            if resp.status_code in (401, 403, 451):
                return final, ParseErrorKind.FETCH_BLOCKED, f"HTTP {resp.status_code} fetching share link"
            if resp.status_code >= 400:
                return final, ParseErrorKind.PARSE_ERROR, f"HTTP {resp.status_code} fetching share link"
            return final, None, None
    except httpx.TimeoutException:
        return None, ParseErrorKind.TIMEOUT, "Timeout while resolving Douyin short link"
    except httpx.HTTPError as exc:
        logger.info("fetch failed: %s", exc)
        return None, ParseErrorKind.FETCH_BLOCKED, f"Network/fetch error: {exc.__class__.__name__}"


async def parse_aweme_url(aweme_url: str) -> ParseResult:
    """Best-effort extract video id from share short links / common patterns.

    Does NOT require cookies. On fetch fail → structured error_kind for
    degradation / link_fail decisions upstream.
    """
    raw = (aweme_url or "").strip()
    if not raw:
        return ParseResult(
            ok=False,
            error_kind=ParseErrorKind.PARSE_ERROR,
            error_message="Empty aweme_url",
        )

    # Pull first URL out of pasted share text if needed
    urls = extract_urls(raw)
    candidate = urls[0] if urls else raw
    if candidate.startswith("v.douyin.com"):
        candidate = "https://" + candidate

    settings = get_settings()
    resolved = candidate
    fetch_kind: Optional[ParseErrorKind] = None
    fetch_msg: Optional[str] = None
    flags: list[str] = []

    host = urlparse(candidate).netloc.lower()
    if "v.douyin.com" in host or "v.douyin.com" in candidate:
        resolved_url, fetch_kind, fetch_msg = await _follow_short_link(
            candidate if "://" in candidate else f"https://{candidate}",
            settings.diagnose_fetch_timeout_sec,
        )
        if resolved_url:
            resolved = resolved_url
        if fetch_kind:
            flags.append(f"url_fetch:{fetch_kind.value}")

    aweme_id = extract_aweme_id(resolved) or extract_aweme_id(candidate) or extract_aweme_id(raw)

    if aweme_id:
        canonical = f"https://www.douyin.com/video/{aweme_id}"
        # MVP: we do not scrape caption/cover from Douyin HTML (often blocked).
        # Parsed id alone upgrades metadata; media still comes from client inputs.
        if fetch_kind and not (urls or "douyin.com" in candidate):
            # Had id somehow but fetch failed — still ok for id extraction
            pass
        return ParseResult(
            ok=True,
            aweme_id=aweme_id,
            canonical_url=canonical,
            resolved_url=resolved,
            degradation_flags=flags,
            error_kind=fetch_kind,  # advisory if set
            error_message=fetch_msg,
        )

    # No id — classify
    kind = fetch_kind or ParseErrorKind.PARSE_ERROR
    msg = fetch_msg or (
        "Could not extract aweme/video id from URL. "
        "Paste a full douyin.com/video/{id} link, or provide caption/cover directly."
    )
    return ParseResult(
        ok=False,
        resolved_url=resolved,
        error_kind=kind,
        error_message=msg,
        degradation_flags=flags or [f"url_parse:{kind.value}"],
    )
