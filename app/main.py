from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.config import settings
from app.models.schemas import HealthResponse
from app.api.auth import router as auth_router
from app.api.meetings import router as meetings_router
from app.api.websocket import router as websocket_router
from app.services import live_transcriber, meeting_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure()
    interrupted = meeting_store.fail_interrupted_processing()
    if interrupted:
        logger.warning("%d meeting(s) were interrupted by a restart and marked as failed", interrupted)
    yield
    await live_transcriber.stop_all()


app = FastAPI(
    title="Mongolian AI Minutes API",
    description="Backend for Mongolian meeting transcription and AI minutes",
    version="0.2.0",
    lifespan=lifespan,
)

origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(meetings_router)
app.include_router(websocket_router)


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version="0.2.0",
        asr_provider=settings.asr_provider,
        llm_provider=settings.llm_provider,
        database=db.dialect(),
        live_transcription=live_transcriber.enabled(),
    )
