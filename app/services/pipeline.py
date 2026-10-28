"""Diagnose pipeline: parse → mode → experts → debate → judge → done.

「审核」= hook/retention diagnosis, NOT compliance / 限流 audit.
No cookies. Frames only from first 3s (or cover fallback).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.config import get_settings
from app.schemas.events import DiagnoseEvent, EventPhase
from app.schemas.modes import DIAGNOSE_SCOPE, DegradationMode, VideoSource
from app.schemas.report import (
    DiagnoseReport,
    DimensionResult,
    Disagreement,
    Finding,
    FrameMeta,
    TopFix,
)
from app.schemas.request import DiagnoseRequest
from app.services.frames import extract_frames_0_3s, save_cover_as_fallback
from app.services.llm import LLMClient, get_llm_clients
from app.services.mode import ModeDecision, decide_mode
from app.services.parse import ParseResult, parse_aweme_url
from app.services.prompts import load_system_prompt
from app.services.store import Job, JobStatus

logger = logging.getLogger(__name__)

UPLOAD_ROOT = Path(__file__).resolve().parents[2] / "uploads"


@dataclass
class JobInputs:
    request: DiagnoseRequest
    video_path: Optional[Path] = None
    cover_path: Optional[Path] = None
    work_dir: Optional[Path] = None


def _emit(job: Job, phase: EventPhase, message: str, progress: float, **kwargs: Any) -> None:
    event = DiagnoseEvent(
        phase=phase,
        message=message,
        progress=progress,
        data=kwargs.get("data"),
        report=kwargs.get("report"),
        error_code=kwargs.get("error_code"),
    )
    job.emit(event)


def _findings_from(raw: Any) -> list[Finding]:
    out: list[Finding] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(Finding.model_validate(item))
        except Exception:
            continue
    return out


async def _run_expert(
    llm: LLMClient,
    *,
    marker: str,
    name: str,
    caption: str,
    frames_summary: str,
    cover_note: str,
    extra: str,
    mode: DegradationMode,
) -> DimensionResult:
    fallback = (
        f"You are the {name} for Douyin short-video diagnosis. "
        f"Role marker: {marker}. "
        "「审核」means hook/retention quality, NEVER platform compliance or ban risk. "
        "Return JSON: {\"score\": number 0-100, \"findings\": "
        "[{\"claim\",\"evidence\",\"severity\": \"info|warn|critical\"}]}."
    )
    system = load_system_prompt(marker, fallback)
    user = json.dumps(
        {
            "mode": mode.value,
            "caption": caption,
            "frames_0_3s": frames_summary,
            "cover": cover_note,
            "extra_notes": extra,
            "scope": DIAGNOSE_SCOPE,
        },
        ensure_ascii=False,
    )
    data = await llm.complete_json(system=system, user=user, temperature=0.3)
    score = data.get("score")
    try:
        score_f = float(score) if score is not None else None
    except (TypeError, ValueError):
        score_f = None
    return DimensionResult(
        name=name,
        score=score_f,
        skipped=False,
        findings=_findings_from(data.get("findings")),
    )


async def _run_debate(
    llm: LLMClient,
    dimensions: list[DimensionResult],
    *,
    mode: DegradationMode,
) -> tuple[list[dict[str, Any]], list[Disagreement]]:
    fallback = (
        "You facilitate a written debate among three Douyin experts "
        "(content / visual / growth). Role marker: cross_critique. "
        "Return JSON: {\"critiques\":[{\"from_expert\",\"to_expert\",\"point\"}], "
        "\"disagreements\":[{\"topic\",\"sides\":{expert:stance}}]}."
    )
    system = load_system_prompt("cross_critique", fallback)
    user = json.dumps(
        {
            "mode": mode.value,
            "dimensions": [d.model_dump() for d in dimensions],
        },
        ensure_ascii=False,
    )
    data = await llm.complete_json(system=system, user=user, temperature=0.4)
    critiques = data.get("critiques") if isinstance(data.get("critiques"), list) else []
    disagreements: list[Disagreement] = []
    for item in data.get("disagreements") or []:
        if not isinstance(item, dict):
            continue
        try:
            disagreements.append(Disagreement.model_validate(item))
        except Exception:
            continue
    return critiques, disagreements


async def _run_judge(
    llm: LLMClient,
    *,
    mode: DegradationMode,
    dimensions: list[DimensionResult],
    disagreements: list[Disagreement],
    critiques: list[dict[str, Any]],
    caption: str,
    frames_summary: str,
) -> dict[str, Any]:
    fallback = (
        "You are the Judge for Douyin video diagnose. Role marker: judge. "
        "Judge is REQUIRED. Aggregate expert findings into a final report. "
        "「审核」= hook/retention, not compliance. "
        "Return JSON with overall_score, one_liner, verdict, final_scores, "
        "top3_fixes (max 3: title/why/how/expected_effect), do_not, "
        "and optional disagreement resolutions."
    )
    system = load_system_prompt("judge", fallback)
    user = json.dumps(
        {
            "mode": mode.value,
            "caption": caption,
            "frames_0_3s": frames_summary,
            "dimensions": [d.model_dump() for d in dimensions],
            "disagreements": [d.model_dump() for d in disagreements],
            "critiques": critiques,
        },
        ensure_ascii=False,
    )
    return await llm.complete_json(system=system, user=user, temperature=0.2)


def _link_fail_report(decision: ModeDecision, parse: Optional[ParseResult]) -> DiagnoseReport:
    return DiagnoseReport(
        overall_score=None,
        mode=DegradationMode.LINK_FAIL,
        video_source=VideoSource.URL.value if decision else None,
        degradation_flags=decision.degradation_flags,
        one_liner=decision.error_message or "Link parse failed",
        dimensions=[],
        disagreements=[],
        final_scores={},
        verdict="无法诊断：链接解析失败且无可用信号（封面/文案/前3秒帧）。",
        top3_fixes=[],
        do_not=["不要在无 cookies 场景强行抓取私有页"],
        meta={
            "error_kind": decision.error_kind.value if decision.error_kind else None,
            "actionable": decision.actionable,
            "aweme_id": parse.aweme_id if parse else None,
            "frames_0_3s": [],
        },
    )


def _merge_caption(req: DiagnoseRequest, parse: Optional[ParseResult]) -> str:
    parts: list[str] = []
    if req.caption and req.caption.strip():
        parts.append(req.caption.strip())
    if parse and parse.caption_hint and parse.caption_hint.strip():
        hint = parse.caption_hint.strip()
        if hint not in parts:
            parts.append(hint)
    return "\n".join(parts).strip()


def _frames_summary(frames: list[FrameMeta]) -> str:
    if not frames:
        return ""
    return "; ".join(f"{f.source}@{f.t_sec:.1f}s:{Path(f.path).name}" for f in frames)


async def run_diagnose_job(job: Job, inputs: JobInputs) -> None:
    """Full pipeline for one job. Emits SSE phases along the way."""
    job.status = JobStatus.RUNNING
    llm, judge_llm = get_llm_clients(get_settings())
    req = inputs.request
    work_dir = inputs.work_dir or (UPLOAD_ROOT / job.id)
    work_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = work_dir / "frames_0_3s"

    try:
        _emit(job, EventPhase.PARSING, "Parsing inputs & selecting mode", 0.05)

        parse: Optional[ParseResult] = None
        if req.aweme_url and req.aweme_url.strip():
            parse = await parse_aweme_url(req.aweme_url.strip())

        caption = _merge_caption(req, parse)
        has_caption = bool(caption)
        has_cover = bool(
            (inputs.cover_path and inputs.cover_path.exists())
            or (req.cover_url and req.cover_url.strip())
            or req.cover_present
        )

        frames: list[FrameMeta] = []
        frame_error: Optional[str] = None

        if inputs.video_path and inputs.video_path.exists():
            extract = await extract_frames_0_3s(inputs.video_path, frames_dir)
            if extract.ok:
                frames = extract.frames
            else:
                frame_error = extract.error
                logger.info("frame extract failed: %s", frame_error)

        # Cover as visual signal only (not counted as frames_0_3s for video_3s)
        cover_note = ""
        if inputs.cover_path and inputs.cover_path.exists():
            cover_note = str(inputs.cover_path)
            has_cover = True
        elif req.cover_url:
            cover_note = req.cover_url
            has_cover = True

        if not frames and has_cover and inputs.cover_path and inputs.cover_path.exists():
            # Soft fallback asset for visual expert; mode stays cover_caption
            await save_cover_as_fallback(inputs.cover_path, frames_dir / "cover_fallback")

        video_source: Optional[str] = None
        if req.video_source:
            video_source = req.video_source.value
        elif inputs.video_path:
            video_source = VideoSource.UPLOAD.value
        elif req.aweme_url:
            video_source = VideoSource.URL.value

        decision = decide_mode(
            has_cover=has_cover,
            has_caption=has_caption,
            aweme_url=req.aweme_url,
            parse=parse,
            has_frames=bool(frames),
            video_attempted=bool(inputs.video_path and inputs.video_path.exists()),
        )
        if frame_error and not frames:
            decision.degradation_flags.append(f"frames_fail:{frame_error[:80]}")

        _emit(
            job,
            EventPhase.PARSING,
            f"Mode={decision.mode.value}",
            0.15,
            data={
                "mode": decision.mode.value,
                "frames_0_3s_count": len(frames),
                "has_caption": has_caption,
                "has_cover": has_cover,
                "video_source": video_source,
                "link_fail": decision.link_fail,
            },
        )

        # Link fail — no experts / debate / judge
        if decision.link_fail:
            report = _link_fail_report(decision, parse)
            report.video_source = video_source  # type: ignore[assignment]
            job.report = report
            job.status = JobStatus.DONE
            _emit(
                job,
                EventPhase.DONE,
                "Link fail — no debate",
                1.0,
                report=report,
                error_code=decision.error_kind.value if decision.error_kind else "link_fail",
                data={"actionable": decision.actionable},
            )
            return

        # Experts
        _emit(job, EventPhase.EXPERTS, "Running content / visual / growth experts", 0.3)
        frames_sum = _frames_summary(frames)
        dimensions: list[DimensionResult] = []

        if decision.run_content:
            dimensions.append(
                await _run_expert(
                    llm,
                    marker="content_expert",
                    name="content",
                    caption=caption,
                    frames_summary=frames_sum,
                    cover_note=cover_note,
                    extra=req.extra_notes or "",
                    mode=decision.mode,
                )
            )
        else:
            dimensions.append(
                DimensionResult(
                    name="content",
                    score=None,
                    skipped=True,
                    skip_reason="no caption/cover/frames signal",
                )
            )

        if decision.run_visual:
            dimensions.append(
                await _run_expert(
                    llm,
                    marker="visual_expert",
                    name="visual",
                    caption=caption,
                    frames_summary=frames_sum or cover_note,
                    cover_note=cover_note,
                    extra=req.extra_notes or "",
                    mode=decision.mode,
                )
            )
        else:
            dimensions.append(
                DimensionResult(
                    name="visual",
                    score=None,
                    skipped=True,
                    skip_reason="no cover/frames",
                )
            )

        if decision.run_growth:
            dimensions.append(
                await _run_expert(
                    llm,
                    marker="growth_expert",
                    name="growth",
                    caption=caption,
                    frames_summary=frames_sum,
                    cover_note=cover_note,
                    extra=req.extra_notes or "",
                    mode=decision.mode,
                )
            )
        else:
            dimensions.append(
                DimensionResult(
                    name="growth",
                    score=None,
                    skipped=True,
                    skip_reason="no caption for retention cues",
                )
            )

        _emit(
            job,
            EventPhase.EXPERTS,
            "Experts finished",
            0.55,
            data={"dimensions": [d.name for d in dimensions if not d.skipped]},
        )

        # Debate
        _emit(job, EventPhase.DEBATE, "Written cross-expert debate", 0.65)
        critiques, disagreements = await _run_debate(llm, dimensions, mode=decision.mode)
        _emit(
            job,
            EventPhase.DEBATE,
            f"Debate done ({len(disagreements)} disagreements)",
            0.75,
            data={"critiques": critiques, "disagreements": [d.model_dump() for d in disagreements]},
        )

        # Judge (required)
        _emit(job, EventPhase.JUDGE, "Judge aggregating final report", 0.85)
        judge_raw = await _run_judge(
            judge_llm,
            mode=decision.mode,
            dimensions=dimensions,
            disagreements=disagreements,
            critiques=critiques,
            caption=caption,
            frames_summary=frames_sum,
        )

        final_scores: dict[str, Optional[float]] = {}
        for d in dimensions:
            final_scores[d.name] = d.score
        # Prefer judge overrides when present
        js = judge_raw.get("final_scores") or {}
        if isinstance(js, dict):
            for k, v in js.items():
                if v is not None:
                    try:
                        final_scores[str(k)] = float(v)
                    except (TypeError, ValueError):
                        pass

        top3: list[TopFix] = []
        for item in (judge_raw.get("top3_fixes") or [])[:3]:
            if isinstance(item, dict):
                try:
                    top3.append(TopFix.model_validate(item))
                except Exception:
                    continue

        # Attach judge resolutions onto disagreements when provided
        for i, d in enumerate(disagreements):
            if not d.judge_resolution and isinstance(judge_raw.get("disagreements"), list):
                for jd in judge_raw["disagreements"]:
                    if isinstance(jd, dict) and jd.get("topic") == d.topic:
                        d.judge_resolution = str(jd.get("judge_resolution") or jd.get("resolution") or "")
                        break

        overall = judge_raw.get("overall_score")
        try:
            overall_f = float(overall) if overall is not None else None
        except (TypeError, ValueError):
            overall_f = None
        if overall_f is None:
            scored = [s for s in final_scores.values() if s is not None]
            overall_f = sum(scored) / len(scored) if scored else 60.0

        do_not = judge_raw.get("do_not") if isinstance(judge_raw.get("do_not"), list) else []
        do_not_s = [str(x) for x in do_not]

        report = DiagnoseReport(
            overall_score=overall_f,
            mode=decision.mode,
            video_source=video_source,  # type: ignore[arg-type]
            degradation_flags=decision.degradation_flags,
            one_liner=str(judge_raw.get("one_liner") or ""),
            dimensions=dimensions,
            disagreements=disagreements,
            final_scores=final_scores,
            verdict=str(judge_raw.get("verdict") or ""),
            top3_fixes=top3,
            do_not=do_not_s,
            meta={
                "aweme_id": parse.aweme_id if parse else None,
                "canonical_url": parse.canonical_url if parse else None,
                "frames_0_3s": [f.model_dump() for f in frames],
                "caption_merged": caption[:500],
                "critiques": critiques,
            },
        )

        job.report = report
        job.status = JobStatus.DONE
        _emit(job, EventPhase.JUDGE, "Judge finished", 0.95, report=report)
        _emit(job, EventPhase.DONE, "Diagnosis complete", 1.0, report=report)

    except Exception as exc:
        logger.exception("diagnose job failed: %s", job.id)
        job.status = JobStatus.ERROR
        job.error = {"message": str(exc), "type": exc.__class__.__name__}
        _emit(
            job,
            EventPhase.ERROR,
            f"Pipeline error: {exc}",
            1.0,
            error_code="pipeline_error",
            data=job.error,
        )
