"""In-memory job store for MVP (single-process)."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from app.schemas.events import DiagnoseEvent, EventPhase
from app.schemas.report import DiagnoseReport


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class Job:
    id: str
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    events: list[DiagnoseEvent] = field(default_factory=list)
    report: Optional[DiagnoseReport] = None
    error: Optional[dict[str, Any]] = None
    _subscribers: list[asyncio.Queue] = field(default_factory=list)

    def emit(self, event: DiagnoseEvent) -> None:
        self.events.append(event)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        for ev in self.events:
            q.put_nowait(ev)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def create(self) -> Job:
        job_id = uuid.uuid4().hex
        job = Job(id=job_id)
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)


job_store = JobStore()
