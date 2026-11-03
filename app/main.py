from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.models.schemas import HealthResponse
from app.api.meetings import router as meetings_router
from app.api.websocket import router as websocket_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Mongolian AI Minutes API",
    description="Backend for Mongolian meeting transcription and AI minutes",
    version="0.1.0",
)

origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(meetings_router)
app.include_router(websocket_router)


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version="0.1.0",
        asr_provider=settings.asr_provider,
    )
