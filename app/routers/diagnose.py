"""POST /diagnose → job_id; GET /diagnose/{id} → SSE stream."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from sse_starlette.sse import EventSourceResponse

from app.schemas.events import EventPhase
from app.schemas.modes import VideoSource
from app.schemas.request import DiagnoseAccepted, DiagnoseRequest
from app.services.pipeline import UPLOAD_ROOT, JobInputs, run_diagnose_job
from app.services.store import JobStatus, job_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["diagnose"])


@router.post("/diagnose", response_model=DiagnoseAccepted)
async def start_diagnose(
    aweme_url: Optional[str] = Form(default=None),
    caption: Optional[str] = Form(default=None),
    extra_notes: Optional[str] = Form(default=None),
    cover_url: Optional[str] = Form(default=None),
    video_file: Optional[UploadFile] = File(default=None),
    cover_file: Optional[UploadFile] = File(default=None),
) -> DiagnoseAccepted:
    """Accept multipart or form-only diagnose request; return job_id immediately."""
    video_present = video_file is not None and bool(video_file.filename)
    cover_present = cover_file is not None and bool(cover_file.filename)

    try:
        req = DiagnoseRequest(
            aweme_url=aweme_url,
            caption=caption,
            extra_notes=extra_notes,
            cover_url=cover_url,
            cover_present=cover_present,
            video_present=video_present,
            video_source=(
                VideoSource.UPLOAD
                if video_present
                else (VideoSource.URL if (aweme_url and aweme_url.strip()) else None)
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    job = job_store.create()
    work_dir = UPLOAD_ROOT / job.id
    work_dir.mkdir(parents=True, exist_ok=True)

    video_path: Optional[Path] = None
    cover_path: Optional[Path] = None

    if video_present and video_file is not None:
        suffix = Path(video_file.filename or "video.mp4").suffix or ".mp4"
        video_path = work_dir / f"input{suffix}"
        with video_path.open("wb") as f:
            shutil.copyfileobj(video_file.file, f)

    if cover_present and cover_file is not None:
        suffix = Path(cover_file.filename or "cover.jpg").suffix or ".jpg"
        cover_path = work_dir / f"cover{suffix}"
        with cover_path.open("wb") as f:
            shutil.copyfileobj(cover_file.file, f)

    inputs = JobInputs(
        request=req,
        video_path=video_path,
        cover_path=cover_path,
        work_dir=work_dir,
    )
    asyncio.create_task(run_diagnose_job(job, inputs))
    return DiagnoseAccepted(job_id=job.id)


@router.get("/diagnose/{job_id}")
async def stream_diagnose(job_id: str) -> EventSourceResponse:
    """SSE stream of diagnose stages: parsing|experts|debate|judge|done|error."""
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    async def event_generator():
        queue = job.subscribe()
        try:
            # If already terminal and no events somehow, still close cleanly
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # heartbeat comment-style ping via empty data keep-alive
                    if job.status in (JobStatus.DONE, JobStatus.ERROR):
                        break
                    yield {"event": "ping", "data": "{}"}
                    continue

                payload = event.model_dump_json()
                yield {"event": event.phase.value, "data": payload}

                if event.phase in (EventPhase.DONE, EventPhase.ERROR):
                    break
        finally:
            job.unsubscribe(queue)

    return EventSourceResponse(event_generator())


@router.get("/diagnose/{job_id}/snapshot")
async def snapshot_diagnose(job_id: str) -> dict:
    """Non-SSE poll helper for tests / clients that prefer JSON snapshot."""
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "job_id": job.id,
        "status": job.status.value,
        "events": [e.model_dump(mode="json") for e in job.events],
        "report": job.report.model_dump(mode="json") if job.report else None,
        "error": job.error,
    }
