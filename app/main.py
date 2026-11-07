"""FastAPI app: serves the dashboard, market data, accounts and saved strategies.

Run it with `python -m app` (see app/__main__.py) or
`uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers` when deployed.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import ROOT, get_settings
from app.routers import auth, market, strategies
from app.security import CSRFMiddleware

log = logging.getLogger("payoff")
INDEX = ROOT / "static" / "index.html"

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
])


class HostCheck(TrustedHostMiddleware):
    """Rejects unknown Host headers (DNS rebinding), except on /healthz so the hosting
    platform's health checks pass whatever hostname they use."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/healthz":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def create_app(connect_source: bool = True) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if connect_source:
            try:
                source = market.make_source(settings.data_source, settings.strikes)
                market.set_source(source)
                source.check()
                log.info("Market data: %s", source.name)
            except Exception as err:  # keep serving accounts and saved strategies
                log.error("Couldn't connect to %s: %s", settings.data_source, err)
        yield

    app = FastAPI(title="Payoff", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(HostCheck, allowed_hosts=settings.host_allowlist)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # Errors come back as {"error": "..."}, which is what the page reads.
    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = [str(p) for p in first.get("loc", [])[1:]]
        if loc == ["email"]:
            return JSONResponse({"error": "Enter a valid email address."}, status_code=422)
        where = ".".join(loc)
        msg = first.get("msg", "Invalid request")
        return JSONResponse({"error": f"{where}: {msg}" if where else msg}, status_code=422)

    app.include_router(auth.router)
    app.include_router(strategies.router)
    app.include_router(market.router)

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def index():
        return FileResponse(INDEX, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    return app


app = create_app()
