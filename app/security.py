"""HTTP давхарга: rate limit, security header, WebSocket origin шалгалт."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections import deque
from typing import Deque, Dict, Optional, Tuple

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.config import settings

WINDOW_SEC = 60
MAX_KEYS = 10_000  # санах ойг хязгаарлана: энэнээс олон IP/хэрэглэгч хадгалахгүй

_AUTH_PATHS = {"/api/auth/login", "/api/auth/register"}
_EXPENSIVE = re.compile(
    r"^/api/(ask$|meetings/[^/]+/(process|regenerate-summary|upload|topics|transcript/replace)$)"
)


def client_ip(request: Request) -> str:
    """Proxy-ийн ард бол X-Forwarded-For-ийн СҮҮЛИЙН хаяг (proxy өөрөө нэмсэн). Эхнийх нь клиентээс хуурч болно."""
    if settings.proxy_trusted:
        xff = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


def _user_key(request: Request) -> str:
    """Хүнд endpoint-ийн хязгаарыг хэрэглэгч бүрээр (токены hash) тоолно; токен байхгүй бол IP."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and len(auth) > 14:
        return "u:" + hashlib.sha256(auth[7:].encode()).hexdigest()[:16]
    return "ip:" + client_ip(request)


class SlidingWindow:
    def __init__(self) -> None:
        self.hits: Dict[Tuple[str, str], Deque[float]] = {}
        self._calls = 0

    def allow(self, bucket: str, key: str, limit: int, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        self._calls += 1
        if self._calls % 500 == 0 or len(self.hits) > MAX_KEYS:
            self._sweep(now)
        dq = self.hits.setdefault((bucket, key), deque())
        while dq and now - dq[0] >= WINDOW_SEC:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True

    def _sweep(self, now: float) -> None:
        for k in [k for k, dq in self.hits.items() if not dq or now - dq[-1] >= WINDOW_SEC]:
            del self.hits[k]
        if len(self.hits) > MAX_KEYS:  # hамгийн хуучин (сүүлд ашигласан) түлхүүрүүдийг хасна
            for k in sorted(self.hits, key=lambda k: self.hits[k][-1])[: len(self.hits) - MAX_KEYS]:
                del self.hits[k]


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, **_ignored) -> None:
        super().__init__(app)
        self.limiter = SlidingWindow()

    @property
    def requests(self):  # тестүүд төлөвийг цэвэрлэхэд ашиглана
        return self.limiter.hits

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        ip = client_ip(request)

        checks = [("global", ip, settings.rate_limit_per_min)]
        if path in _AUTH_PATHS:
            checks.append(("auth", ip, settings.rate_limit_auth_per_min))
        elif _EXPENSIVE.match(path):
            checks.append(("expensive", _user_key(request), settings.rate_limit_expensive_per_min))

        for bucket, key, limit in checks:
            if not self.limiter.allow(bucket, key, limit):
                return JSONResponse(
                    {"detail": "RATE_LIMITED: Хэт олон хүсэлт илгээлээ. Түр хүлээгээд дахин оролдоно уу."},
                    status_code=429,
                    headers={"Retry-After": str(WINDOW_SEC)},
                )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        proto = request.headers.get("x-forwarded-proto", request.url.scheme) if settings.proxy_trusted else request.url.scheme
        if proto == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


def ws_origin_allowed(origin: Optional[str]) -> bool:
    """Хөтчөөс ирсэн (Origin header-тэй) холболтыг зөвхөн зөвшөөрөгдсөн сайтаас авна. Origin-гүй (хөтөч биш) клиент токенээр л нэвтэрнэ."""
    if not origin:
        return True
    allowed = settings.ws_origins()
    return "*" in allowed or origin.rstrip("/") in allowed


_access_log = logging.getLogger("minuto.access")


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Хүсэлт бүрт X-Request-ID өгч, JSON мөр лог бичнэ.

    Зориуд БИЧИХГҮЙ зүйл: query string (?token=, ?ticket=, ?sig= агуулж болно), header, body (бичвэр, нууц үг).
    """

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex[:16]
        rid = re.sub(r"[^A-Za-z0-9_.-]", "", rid) or uuid.uuid4().hex[:16]
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            _access_log.info(
                json.dumps(
                    {
                        "event": "request", "request_id": rid, "method": request.method, "path": request.url.path,
                        "status": status, "duration_ms": round((time.perf_counter() - start) * 1000, 1), "ip": client_ip(request),
                    },
                    ensure_ascii=False,
                )
            )
