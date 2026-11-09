"""FastAPI application and security middleware."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import Headers, MutableHeaders

from sqlalchemy import select

from ..context import Context
from ..models import Node
from ..security import (
    client_ip, hash_token, host_of, host_permitted, is_direct_plain_http, origin_permitted, peer_permitted,
)
from . import routes_account, routes_admin, routes_alerts, routes_messages, routes_nodes, routes_radio, routes_recordings, routes_warnings
from .deps import ip_banned

WEB_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_API_BODY = 1_000_000
MAX_CALL_BODY = 256 * 1024 * 1024
CALL_UPLOAD_PATH = "/api/nodes/agent/calls"


def app_csp(tile_origin: str) -> str:
    """Scripts stay same-origin; the only outside host allowed is the map tile server."""
    return (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data: {tile_origin}; media-src 'self'; connect-src 'self'; "
        "worker-src 'self'; manifest-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )


# tar1090 is a third-party page built around inline scripts and external map tiles.
TAR1090_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; "
    "form-action 'none'; frame-ancestors 'self'"
)


def _deny(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


class SecurityMiddleware:
    """Host and Origin checks, IP bans, body limits and security headers.

    Written as plain ASGI so that streaming live audio is not buffered and a
    client disconnect is seen promptly.
    """

    def __init__(self, app, ctx: Context):
        self.app, self.ctx = app, ctx
        self.cfg = ctx.cfg

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        peer = scope["client"][0] if scope.get("client") else None
        if not peer_permitted(self.cfg, peer):
            return await self._reject(scope, receive, send, 403, "Local network access only")
        if not host_permitted(self.cfg, host_of(headers)):
            return await self._reject(scope, receive, send, 400, "Invalid host")
        ip = client_ip(self.cfg, peer, headers)
        if ip_banned(self.ctx, ip):
            return await self._reject(scope, receive, send, 403, "Access denied")
        if scope["type"] == "websocket":
            return await self.app(scope, receive, send)

        method = scope["method"]
        path = scope["path"]
        if method not in SAFE_METHODS and not path.startswith("/api/nodes/agent/"):
            origin = headers.get("origin")
            if origin is not None:
                if not origin_permitted(self.cfg, origin, headers):
                    return await self._reject(scope, receive, send, 403, "Cross-origin request refused")
            elif headers.get("x-sentrysdr") != "1":
                return await self._reject(scope, receive, send, 403, "Missing request header")

        # The body is parsed before route dependencies run, so a call upload must
        # prove its device key here or anyone could spool large files to disk.
        limit = MAX_API_BODY
        if path == CALL_UPLOAD_PATH:
            if not self._valid_device_key(headers.get("authorization")):
                return await self._reject(scope, receive, send, 401, "Invalid device key")
            limit = MAX_CALL_BODY
        length = headers.get("content-length")
        if length and length.isdigit() and int(length) > limit:
            return await self._reject(scope, receive, send, 413, "Request too large")

        received = 0

        async def receive_limited():
            # Also covers chunked requests, which have no Content-Length to check.
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    return {"type": "http.disconnect"}
            return message

        is_tar1090 = path.startswith("/api/tar1090")
        policy = TAR1090_CSP if is_tar1090 else app_csp(self.ctx.tile_origin())

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                out["X-Content-Type-Options"] = "nosniff"
                out["X-Frame-Options"] = "SAMEORIGIN" if is_tar1090 else "DENY"
                out["Referrer-Policy"] = "no-referrer"
                out["Content-Security-Policy"] = policy
                out["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                out["Cross-Origin-Opener-Policy"] = "same-origin"
                if urlsplit(self.cfg.public_url).scheme == "https" and not is_direct_plain_http(self.cfg, peer, headers):
                    out["Strict-Transport-Security"] = "max-age=31536000"
                if path.startswith("/api/") and "cache-control" not in out:
                    out["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive_limited, send_with_headers)

    def _valid_device_key(self, authorization: str | None) -> bool:
        if not authorization or not authorization.startswith("Bearer "):
            return False
        with self.ctx.db() as session:
            node = session.scalar(select(Node).where(Node.key_hash == hash_token(authorization[7:].strip())))
            return node is not None and node.status == "active"

    async def _reject(self, scope, receive, send, status: int, detail: str):
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        await _deny(status, detail)(scope, receive, send)


def create_app(ctx: Context, run_background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if run_background:
            await ctx.start()
        yield
        if run_background:
            await ctx.stop()

    app = FastAPI(title="SentrySDR", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.ctx = ctx
    app.add_middleware(SecurityMiddleware, ctx=ctx)

    for module in (routes_account, routes_admin, routes_radio, routes_recordings, routes_messages, routes_alerts, routes_warnings, routes_nodes):
        app.include_router(module.router)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.api_route("/api/{_rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def api_not_found(_rest: str):
        raise HTTPException(404, "Not found")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str, request: Request):
        dist = (getattr(request.app.state, "web_dist", None) or WEB_DIST).resolve()
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist):
            headers = {"Cache-Control": "no-cache"} if candidate.name in ("sw.js", "index.html") else None
            return FileResponse(candidate, headers=headers)
        index = dist / "index.html"
        if not index.is_file():
            raise HTTPException(503, "Web interface is not built. Run: npm --prefix web run build")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app
