"""Load diagnose system prompts from prompts/*.md at the repo root.

Missing or unreadable files fall back to the caller's inline system string.
Loaded templates always include a `Role marker:` line so MockLLMClient can route.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# app/services/prompts.py → parents[2] is the repository root (/app in the image).
REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"

# Pipeline role marker → markdown filename.
PROMPT_FILES: dict[str, str] = {
    "content_expert": "content.md",
    "visual_expert": "visual.md",
    "growth_expert": "growth.md",
    "cross_critique": "debate.md",
    "judge": "judge.md",
}


def role_marker_line(marker: str) -> str:
    return f"Role marker: {marker}"


def ensure_role_marker(text: str, marker: str) -> str:
    """Append `Role marker: {marker}` when that exact line is absent."""
    line = role_marker_line(marker)
    for raw in text.splitlines():
        if raw.strip() == line:
            return text if text.endswith("\n") else text + "\n"
    return text.rstrip() + "\n\n" + line + "\n"


def load_system_prompt(marker: str, fallback: str) -> str:
    """Return the markdown template for `marker`, or `fallback` if it cannot be read.

    `fallback` is returned unchanged so a missing file keeps the historical inline prompt.
    """
    filename = PROMPT_FILES.get(marker)
    if not filename:
        return fallback
    path = PROMPTS_DIR / filename
    try:
        if path.is_file():
            loaded = path.read_text(encoding="utf-8").strip()
            if loaded:
                logger.debug("Loaded prompt %s from %s", marker, filename)
                return ensure_role_marker(loaded, marker)
    except Exception:
        logger.warning(
            "Prompt file unreadable (%s); using inline fallback",
            filename,
            exc_info=True,
        )
        return fallback
    logger.warning("Prompt file missing for %s; using inline fallback", marker)
    return fallback
