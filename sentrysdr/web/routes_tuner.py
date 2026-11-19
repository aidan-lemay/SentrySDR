"""The web tuner: a websocket per browser, and a list of the SDRs it can open.

The websocket is signed in with the session cookie, like the rest of the app. A cookie
is sent with any request to this site, including a websocket opened by someone else's
page, so the Origin header must be this site's own before anything happens.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .. import bookmarks
from ..models import Node, Source, User, UserBookmark
from ..pipelines import FREQ_MAX_HZ, FREQ_MIN_HZ
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


# --- bookmarks ------------------------------------------------------------------------------
class BookmarkBody(BaseModel):
    name: str = Field(default="", max_length=bookmarks.NAME_LENGTH)
    frequency_hz: int = Field(ge=FREQ_MIN_HZ, le=FREQ_MAX_HZ)
    mode: str = Field(default="nfm", pattern="^(" + "|".join(bookmarks.MODES) + ")$")
    bandwidth_hz: int | None = Field(default=None, gt=0, le=250_000)
    folder: str = Field(default="", max_length=bookmarks.FOLDER_LENGTH)
    notes: str = Field(default="", max_length=bookmarks.NOTES_LENGTH)


def _bookmark_view(b: UserBookmark) -> dict:
    return {"id": b.id, "name": b.name, "frequency_hz": b.frequency_hz, "mode": b.mode,
            "bandwidth_hz": b.bandwidth_hz, "folder": b.folder, "notes": b.notes}


def _mine(db, user: User) -> list[UserBookmark]:
    return list(db.scalars(select(UserBookmark).where(UserBookmark.user_id == user.id)
                           .order_by(UserBookmark.folder, UserBookmark.frequency_hz, UserBookmark.id)))


def _count(db, user: User) -> int:
    return db.scalar(select(func.count()).select_from(UserBookmark).where(UserBookmark.user_id == user.id))


@router.get("/me/bookmarks")
def list_bookmarks(user: User = Depends(require("tuner")), db=Depends(get_db)):
    return [_bookmark_view(b) for b in _mine(db, user)]


@router.post("/me/bookmarks", status_code=201)
def add_bookmark(body: BookmarkBody, user: User = Depends(require("tuner")), db=Depends(get_db)):
    if _count(db, user) >= bookmarks.MAX_BOOKMARKS:
        raise HTTPException(422, f"You have {bookmarks.MAX_BOOKMARKS} bookmarks, the most there can be. Remove some first.")
    item = bookmarks.clean(body.model_dump())
    bookmark = UserBookmark(user_id=user.id, **item)
    db.add(bookmark)
    db.commit()
    return _bookmark_view(bookmark)


def _own(db, user: User, bookmark_id: int) -> UserBookmark:
    bookmark = db.get(UserBookmark, bookmark_id)
    if bookmark is None or bookmark.user_id != user.id:
        raise HTTPException(404, "Bookmark not found")
    return bookmark


@router.put("/me/bookmarks/{bookmark_id}")
def edit_bookmark(bookmark_id: int, body: BookmarkBody, user: User = Depends(require("tuner")), db=Depends(get_db)):
    bookmark = _own(db, user, bookmark_id)
    for key, value in bookmarks.clean(body.model_dump()).items():
        setattr(bookmark, key, value)
    db.commit()
    return _bookmark_view(bookmark)


@router.delete("/me/bookmarks/{bookmark_id}")
def remove_bookmark(bookmark_id: int, user: User = Depends(require("tuner")), db=Depends(get_db)):
    db.delete(_own(db, user, bookmark_id))
    db.commit()
    return {"ok": True}


class ImportBody(BaseModel):
    format: str = Field(pattern="^(sentrysdr|gqrx|sdrpp)$")
    text: str = Field(max_length=900_000)
    replace: bool = False


@router.post("/me/bookmarks/import")
def import_bookmarks(body: ImportBody, user: User = Depends(require("tuner")), db=Depends(get_db)):
    """Add bookmarks from a file, skipping ones already there. `replace` clears the old ones first."""
    try:
        found = bookmarks.parse(body.text, body.format)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(422, "That file isn't in the format chosen.")
    if not found:
        raise HTTPException(422, "No bookmarks found in that file. Check the format chosen matches it.")
    if body.replace:
        for old in _mine(db, user):
            db.delete(old)
        db.flush()
    existing = {(b.name, b.frequency_hz, b.mode) for b in _mine(db, user)}
    room = bookmarks.MAX_BOOKMARKS - len(existing)
    added = skipped = 0
    for item in found:
        if (item["name"], item["frequency_hz"], item["mode"]) in existing:
            skipped += 1
            continue
        if added >= room:
            break
        db.add(UserBookmark(user_id=user.id, **item))
        existing.add((item["name"], item["frequency_hz"], item["mode"]))
        added += 1
    db.commit()
    return {"added": added, "skipped": skipped, "left_out": len(found) - added - skipped}


@router.get("/me/bookmarks/export")
def export_bookmarks(format: str = Query(default="sentrysdr", pattern="^(sentrysdr|gqrx|sdrpp)$"),
                     user: User = Depends(require("tuner")), db=Depends(get_db)):
    text, filename, media = bookmarks.export([_bookmark_view(b) for b in _mine(db, user)], format)
    return Response(text, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
