"""The web tuner: a websocket per browser, and a list of the SDRs it can open.

The websocket is signed in with the session cookie, like the rest of the app. A cookie
is sent with any request to this site, including a websocket opened by someone else's
page, so the Origin header must be this site's own before anything happens.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from ..models import Node, Source, User
from ..security import client_ip, origin_permitted
from ..tuner_hub import TunerError
from .deps import _current_session, ctx_of, effective_permissions, get_db, require

log = logging.getLogger("sentrysdr.tuner")
router = APIRouter(prefix="/api")

MAX_MESSAGE = 4096
RECHECK_SECONDS = 15  # how often an open tuner checks its viewer may still use it


def _tuner_sources(ctx, db) -> list[tuple[Source, Node]]:
    """SDRs assigned a tuner target right now."""
    assigned = ctx.assignments()
    found = []
    for source in db.scalars(select(Source).order_by(Source.name)):
        spec = assigned.get(source.node_id, {}).get(source.serial)
        if spec and spec["kind"] == "tuner":
            node = db.get(Node, source.node_id)
            if node is not None:
                found.append((source, node))
    return found


@router.get("/tuners")
def list_tuners(request: Request, _user: User = Depends(require("tuner")), db=Depends(get_db)):
    """SDRs that can be opened as a tuner now, and who's on each."""
    ctx = ctx_of(request)
    items = []
    for source, node in _tuner_sources(ctx, db):
        room = ctx.tuners.rooms.get((source.node_id, source.serial))
        controller = room.viewers.get(room.controller) if room else None
        status = ctx.status.get((source.node_id, source.serial), {})
        items.append({"source_id": source.id, "name": source.name, "node_name": node.name,
                      "state": status.get("state", ""), "error": status.get("error", ""),
                      "viewers": len(room.viewers) if room else 0, "controller": controller.name if controller else None})
    return items


def _allowed(ctx, websocket: WebSocket, source_id: int) -> tuple[User, tuple[int, str], str] | str:
    """Who's asking and for which tuner, or why not."""
    with ctx.db() as db:
        found = _current_session(websocket, db)
        if found is None:
            return "Sign in again to use the tuner."
        user = found[0]
        if user.must_change_password or "tuner" not in effective_permissions(user):
            return "You don't have permission to use the tuner."
        source = db.get(Source, source_id)
        if source is None:
            return "That SDR doesn't exist."
        if not any(s.id == source_id for s, _ in _tuner_sources(ctx, db)):
            return f"{source.name} isn't set up as a tuner right now. Assign it a tuner target on the Sources page."
        db.expunge(user)
        return user, (source.node_id, source.serial), source.name


@router.websocket("/tuner/{source_id}/ws")
async def tuner_socket(websocket: WebSocket, source_id: int):
    ctx = websocket.app.state.ctx
    origin = websocket.headers.get("origin")
    if origin is None or not origin_permitted(ctx.cfg, origin, websocket.headers):
        await websocket.close(code=1008)
        return
    allowed = await asyncio.to_thread(_allowed, ctx, websocket, source_id)
    await websocket.accept()
    if isinstance(allowed, str):
        await websocket.send_text(json.dumps({"t": "error", "detail": allowed, "fatal": True}))
        await websocket.close(code=4403)
        return
    user, key, source_name = allowed
    try:
        viewer = ctx.tuners.join(key, user.id, user.name or user.email, user.role == "admin")
    except TunerError as exc:
        await websocket.send_text(json.dumps({"t": "error", "detail": str(exc), "fatal": True}))
        await websocket.close(code=4429)
        return
    ip = client_ip(ctx.cfg, websocket.client.host if websocket.client else None, websocket.headers)
    ctx.audit("tuner.opened", user=user, ip=ip, target=source_name)
    await websocket.send_text(json.dumps({"t": "hello", "source": source_name, "you": viewer.id}))

    async def sender():
        while True:
            kind, data = await viewer.queue.get()
            if kind == "bytes":
                await websocket.send_bytes(data)
            else:
                await websocket.send_text(data)

    async def receiver():
        while True:
            text = await websocket.receive_text()
            if len(text) > MAX_MESSAGE:
                continue
            try:
                message = json.loads(text)
                if isinstance(message, dict):
                    ctx.tuners.command(key, viewer, message)
            except ValueError:
                continue
            except TunerError as exc:
                viewer.put(("text", json.dumps({"t": "error", "detail": str(exc)})))

    async def recheck():
        # A disabled account, a signed-out session or a reassigned SDR ends an open tuner too.
        while True:
            await asyncio.sleep(RECHECK_SECONDS)
            still = await asyncio.to_thread(_allowed, ctx, websocket, source_id)
            if isinstance(still, str):
                await websocket.send_text(json.dumps({"t": "error", "detail": still, "fatal": True}))
                return

    tasks = [asyncio.create_task(c()) for c in (sender, receiver, recheck)]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        ctx.tuners.leave(key, viewer)
        try:
            await websocket.close()
        except RuntimeError:
            pass
