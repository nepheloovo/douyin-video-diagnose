"""Douyin video diagnose API — uvicorn app.main:app"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import diagnose

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(
    title="Douyin Video Diagnose",
    version="0.2.0",
    description=(
        "First-3s frames + caption diagnose. "
        "「审核」= hook/retention, not compliance. No cookies."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(diagnose.router)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "douyin-video-diagnose", "version": "0.2.0"}
