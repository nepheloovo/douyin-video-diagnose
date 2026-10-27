"""Extract frames from the first 3 seconds of a video (ffmpeg). No full-video analysis."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.schemas.report import FrameMeta

logger = logging.getLogger(__name__)

# Module-level override for tests (force failure / inject frames).
_FORCE_FAIL: bool = False
_INJECT_FRAMES: Optional[list[FrameMeta]] = None


def set_frame_extract_override(
    *,
    force_fail: bool = False,
    inject: Optional[list[FrameMeta]] = None,
) -> None:
    """Test helper — reset with set_frame_extract_override() (no args)."""
    global _FORCE_FAIL, _INJECT_FRAMES
    _FORCE_FAIL = force_fail
    _INJECT_FRAMES = inject


@dataclass
class FrameExtractResult:
    ok: bool
    frames: list[FrameMeta] = field(default_factory=list)
    error: Optional[str] = None


async def extract_frames_0_3s(
    video_path: Path,
    out_dir: Path,
    *,
    fps: float = 1.0,
) -> FrameExtractResult:
    """Run ffmpeg on [0, 3)s only. Returns FrameMeta list or a soft failure."""
    if _INJECT_FRAMES is not None:
        return FrameExtractResult(ok=bool(_INJECT_FRAMES), frames=list(_INJECT_FRAMES))
    if _FORCE_FAIL:
        return FrameExtractResult(ok=False, error="forced_extract_fail")

    if not video_path.exists():
        return FrameExtractResult(ok=False, error=f"video not found: {video_path}")

    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin is None:
        try:
            import imageio_ffmpeg

            ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg_bin = None
    if not ffmpeg_bin:
        return FrameExtractResult(ok=False, error="ffmpeg not installed")

    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / "frame_%03d.jpg"

    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "0",
        "-t",
        "3",
        "-i",
        str(video_path),
        "-vf",
        f"fps={fps}",
        "-q:v",
        "3",
        str(pattern),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        return FrameExtractResult(ok=False, error="ffmpeg timeout")
    except OSError as exc:
        return FrameExtractResult(ok=False, error=f"ffmpeg spawn failed: {exc}")

    if proc.returncode != 0:
        msg = (stderr or b"").decode("utf-8", errors="replace")[:400]
        logger.info("ffmpeg failed rc=%s: %s", proc.returncode, msg)
        return FrameExtractResult(ok=False, error=f"ffmpeg rc={proc.returncode}: {msg}")

    frames: list[FrameMeta] = []
    for i, path in enumerate(sorted(out_dir.glob("frame_*.jpg"))):
        frames.append(
            FrameMeta(
                path=str(path),
                t_sec=float(i) / fps if fps > 0 else float(i),
                source="video_0_3s",
            )
        )

    if not frames:
        return FrameExtractResult(ok=False, error="ffmpeg produced no frames")

    return FrameExtractResult(ok=True, frames=frames)


async def save_cover_as_fallback(
    cover_path: Path,
    out_dir: Path,
) -> list[FrameMeta]:
    """When video frames fail, surface cover as a single FrameMeta (source=cover)."""
    if not cover_path.exists():
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "cover.jpg"
    try:
        shutil.copy2(cover_path, dest)
    except OSError:
        return []
    return [FrameMeta(path=str(dest), t_sec=0.0, source="cover")]
