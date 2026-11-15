from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app import db
from app.config import settings
from app.models.schemas import HealthResponse
from app.security import RateLimitMiddleware, SecurityHeadersMiddleware
from app.api.ask import router as ask_router
from app.api.auth import router as auth_router
from app.api.contacts import router as contacts_router
from app.api.meetings import router as meetings_router
from app.api.templates import router as templates_router
from app.api.vocabulary import router as vocabulary_router
from app.api.websocket import router as websocket_router
from app.api.workspaces import router as workspaces_router
from app.services import live_transcriber, meeting_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.is_production and not settings.jwt_secret:
        # Нууцгүй бол сервер дахин эхлэх бүрт бүх хэрэглэгч гарч, олон instance-д токен таарахгүй
        raise RuntimeError("JWT_SECRET заавал тохируулна (production)")
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
    docs_url=None if settings.is_production else "/api/docs",
    redoc_url=None if settings.is_production else "/api/redoc",
    openapi_url=None if settings.is_production else "/api/openapi.json",
)

# Security middleware
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)
_hosts = [h.strip() for h in settings.allowed_hosts.split(",") if h.strip()] or ["*"]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_hosts)

# CORS
origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["Content-Disposition"],
)

app.include_router(auth_router)
app.include_router(meetings_router)
app.include_router(websocket_router)
app.include_router(ask_router)
app.include_router(contacts_router)
app.include_router(vocabulary_router)
app.include_router(templates_router)
app.include_router(workspaces_router)


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Нийтэд нээлттэй, хамгийн бага мэдээлэлтэй. Provider, түлхүүр, хостын мэдээлэл гаргахгүй."""
    return HealthResponse(
        status="ok",
        version="0.2.0",
        database=db.dialect(),
        live_transcription=live_transcriber.enabled(),
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    # Дэлгэрэнгүйг зөвхөн сервер дээр бүртгэнэ; клиентэд код л очно
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "INTERNAL_ERROR: Серверт алдаа гарлаа"}, status_code=500)
