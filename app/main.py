from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app import db
from app.config import settings
from app.models.schemas import HealthResponse, UserResponse
from app.services import access
from app.services.auth import current_user
from app.security import RateLimitMiddleware, RequestLogMiddleware, SecurityHeadersMiddleware
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
    from app.services import audio_store

    audio_store.validate_config()  # AUDIO_STORAGE_BACKEND буруу бол эхлэхгүй
    db.configure()
    if settings.is_production and db.dialect() == "sqlite":
        logger.error("PRODUCTION is using SQLite: data is lost on every deploy. Set DATABASE_URL to a Postgres instance.")
    from app.services import jobs

    jobs.recover_on_startup()  # унасан ажлыг үргэлжлүүлнэ; ажилгүй "processing" хурлыг алдаатай болгоно
    jobs.ensure_workers()
    yield
    await jobs.shutdown()  # ажиллаж буй ажил "running" хэвээр үлдэж, дараагийн эхлэлд үргэлжилнэ
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
app.add_middleware(RequestLogMiddleware)
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
    allow_headers=["Authorization", "Content-Type", "If-Match", "X-Request-ID"],
    expose_headers=["Content-Disposition", "X-Request-ID"],
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


@app.exception_handler(access.AccessDenied)
async def access_denied(request: Request, exc: access.AccessDenied) -> JSONResponse:
    return JSONResponse({"detail": "FORBIDDEN: Танд энэ үйлдлийг хийх эрх байхгүй"}, status_code=403)


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness: процесс хариу өгч байна. Нууц/тохиргооны мэдээлэл байхгүй, DB-д хандахгүй."""
    return {"status": "ok"}


@app.get("/api/readyz")
def readyz(user: UserResponse = Depends(current_user)) -> dict:
    """Readiness (нэвтэрсэн хэрэглэгчид): DB, хадгалах зай, provider-ийн тохиргоог шалгана. Гадагш (Qwen) дуудлага хийхгүй → зардалгүй."""
    import shutil
    from pathlib import Path

    from sqlalchemy import text

    checks: dict = {}
    try:
        with db.session_scope() as s:
            s.execute(text("SELECT 1"))
        checks["database"] = {"ok": True, "dialect": db.dialect(), "persistent": db.dialect() != "sqlite"}
    except Exception:  # noqa: BLE001
        logger.exception("readyz: database check failed")
        checks["database"] = {"ok": False}
    root = Path(settings.audio_storage_path)
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".readyz"
        probe.write_text("ok")
        probe.unlink()
        free = shutil.disk_usage(root).free
        checks["storage"] = {"ok": free >= settings.min_free_disk_bytes, "free_mb": free // 1_000_000}
    except OSError:
        checks["storage"] = {"ok": False}
    from app.services import audio_store

    checks["storage"]["durable"] = audio_store.enabled()
    if not audio_store.enabled() and settings.is_production:
        checks["storage"]["warning"] = "Аудио зөвхөн сервер дээрх дискэнд: deploy бүрт арилна (AUDIO_STORAGE_BACKEND=supabase)"
    checks["storage"]["pending_purges"] = len(audio_store.pending_purges())
    checks["providers"] = {
        "asr": settings.asr_provider,
        "llm": settings.llm_provider,
        "qwen_key_configured": bool(settings.qwen_api_key) or (settings.asr_provider == "demo" and settings.llm_provider == "demo"),
    }
    ok = checks["database"]["ok"] and checks["storage"]["ok"] and checks["providers"]["qwen_key_configured"]
    return {"status": "ready" if ok else "degraded", "checks": checks}


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    # Дэлгэрэнгүйг зөвхөн сервер дээр бүртгэнэ; клиентэд код л очно
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "INTERNAL_ERROR: Серверт алдаа гарлаа"}, status_code=500)
