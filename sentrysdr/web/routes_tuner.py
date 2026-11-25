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
from ..models import BookmarkFolder, Node, Source, User, UserBookmark
from ..pipelines import FREQ_MAX_HZ, FREQ_MIN_HZ
from ..security import client_ip, origin_permitted
from ..tuner_hub import TunerError
from .deps import _current_session, ctx_of, current_user, effective_permissions, get_db

log = logging.getLogger("sentrysdr.tuner")
router = APIRouter(prefix="/api")

MAX_MESSAGE = 4096
RECHECK_SECONDS = 15  # how often an open tuner checks its viewer may still use it


def tuner_user(user: User = Depends(current_user)) -> User:
    """Listening on a tuner needs Listen live; owning one needs the tuner permission."""
    if not effective_permissions(user) & {"tuner", "listen_live"}:
        raise HTTPException(403, "Missing permission: listen_live")
    return user


def _tuner_sources(ctx, db) -> list[tuple[Source, Node]]:
    """SDRs a tuner can open now: tuner targets, and recording multi-channel SDRs, view-only."""
    assigned = ctx.assignments()
    found = []
    for source in db.scalars(select(Source).order_by(Source.name)):
        spec = assigned.get(source.node_id, {}).get(source.serial)
        if spec and spec["kind"] in ("tuner", "multi"):
            node = db.get(Node, source.node_id)
            if node is not None:
                found.append((source, node))
    return found


@router.get("/tuners")
def list_tuners(request: Request, _user: User = Depends(tuner_user), db=Depends(get_db)):
    """SDRs that can be opened as a tuner now, and who's on each."""
    ctx = ctx_of(request)
    items = []
    for source, node in _tuner_sources(ctx, db):
        room = ctx.tuners.rooms.get((source.node_id, source.serial))
        controller = room.viewers.get(room.controller) if room else None
        status = ctx.status.get((source.node_id, source.serial), {})
        items.append({"source_id": source.id, "name": source.name, "node_name": node.name,
                      "view_only": bool(room and room.state.get("view_only")) or bool(source.multichannel),
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
        perms = effective_permissions(user)
        if user.must_change_password or not perms & {"tuner", "listen_live"}:
            return "You don't have permission to use the tuner."
        source = db.get(Source, source_id)
        if source is None:
            return "That SDR doesn't exist."
        if not any(s.id == source_id for s, _ in _tuner_sources(ctx, db)):
            return f"{source.name} isn't set up as a tuner right now. Assign it a tuner target, or make it multi-channel, on the Sources page."
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
        viewer = ctx.tuners.join(key, user.id, user.name or user.email, user.role == "admin",
                                 can_own="tuner" in effective_permissions(user))
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
    folders: list[str] = Field(default_factory=list, max_length=bookmarks.MAX_FOLDERS_EACH)
    notes: str = Field(default="", max_length=bookmarks.NOTES_LENGTH)


def folders_of(b: UserBookmark) -> list[str]:
    """A bookmark's folders. One saved before there could be several has its one folder."""
    if b.folders is not None:
        return list(b.folders)
    return [b.folder] if b.folder else []


def _bookmark_view(b: UserBookmark) -> dict:
    return {"id": b.id, "name": b.name, "frequency_hz": b.frequency_hz, "mode": b.mode,
            "bandwidth_hz": b.bandwidth_hz, "folders": folders_of(b), "notes": b.notes}


def _mine(db, user: User) -> list[UserBookmark]:
    return list(db.scalars(select(UserBookmark).where(UserBookmark.user_id == user.id)
                           .order_by(UserBookmark.frequency_hz, UserBookmark.id)))


def _count(db, user: User) -> int:
    return db.scalar(select(func.count()).select_from(UserBookmark).where(UserBookmark.user_id == user.id))


def _colors(db, user: User) -> dict[str, str]:
    return {f.name: f.color for f in db.scalars(select(BookmarkFolder).where(BookmarkFolder.user_id == user.id))}


def _ensure_folders(db, user: User, names, colors: dict[str, str] | None = None) -> None:
    """Give every folder in use a color: the file's, or the next of the defaults."""
    have = _colors(db, user)
    for name in names:
        if name in have:
            if colors and name in colors and colors[name] != have[name]:
                db.scalar(select(BookmarkFolder).where(BookmarkFolder.user_id == user.id, BookmarkFolder.name == name)).color = colors[name]
            continue
        color = (colors or {}).get(name) or bookmarks.DEFAULT_COLOURS[len(have) % len(bookmarks.DEFAULT_COLOURS)]
        db.add(BookmarkFolder(user_id=user.id, name=name, color=color))
        have[name] = color
    db.flush()


def _store(bookmark: UserBookmark, item: dict) -> None:
    for key in ("name", "frequency_hz", "mode", "bandwidth_hz", "notes"):
        setattr(bookmark, key, item[key])
    bookmark.folders = item["folders"]
    bookmark.folder = item["folders"][0] if item["folders"] else ""


@router.get("/me/bookmarks")
def list_bookmarks(user: User = Depends(tuner_user), db=Depends(get_db)):
    return [_bookmark_view(b) for b in _mine(db, user)]


@router.post("/me/bookmarks", status_code=201)
def add_bookmark(body: BookmarkBody, user: User = Depends(tuner_user), db=Depends(get_db)):
    if _count(db, user) >= bookmarks.MAX_BOOKMARKS:
        raise HTTPException(422, f"You have {bookmarks.MAX_BOOKMARKS} bookmarks, the most there can be. Remove some first.")
    item = bookmarks.clean(body.model_dump())
    bookmark = UserBookmark(user_id=user.id)
    _store(bookmark, item)
    db.add(bookmark)
    _ensure_folders(db, user, item["folders"])
    db.commit()
    return _bookmark_view(bookmark)


def _own(db, user: User, bookmark_id: int) -> UserBookmark:
    bookmark = db.get(UserBookmark, bookmark_id)
    if bookmark is None or bookmark.user_id != user.id:
        raise HTTPException(404, "Bookmark not found")
    return bookmark


@router.put("/me/bookmarks/{bookmark_id}")
def edit_bookmark(bookmark_id: int, body: BookmarkBody, user: User = Depends(tuner_user), db=Depends(get_db)):
    bookmark = _own(db, user, bookmark_id)
    item = bookmarks.clean(body.model_dump())
    _store(bookmark, item)
    _ensure_folders(db, user, item["folders"])
    db.commit()
    return _bookmark_view(bookmark)


@router.delete("/me/bookmarks/{bookmark_id}")
def remove_bookmark(bookmark_id: int, user: User = Depends(tuner_user), db=Depends(get_db)):
    db.delete(_own(db, user, bookmark_id))
    db.commit()
    return {"ok": True}


# --- folders ----------------------------------------------------------------------------------
@router.get("/me/bookmark-folders")
def list_folders(user: User = Depends(tuner_user), db=Depends(get_db)):
    """Every folder a bookmark is in, with its color and how many bookmarks are in it."""
    counts: dict[str, int] = {}
    for b in _mine(db, user):
        for name in folders_of(b):
            counts[name] = counts.get(name, 0) + 1
    _ensure_folders(db, user, sorted(counts))
    db.commit()
    colors = _colors(db, user)
    return [{"name": name, "color": colors[name], "count": counts[name]} for name in sorted(counts, key=str.lower)]


class FolderBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=bookmarks.FOLDER_LENGTH)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


def _rename(db, user: User, old: str, new: str | None) -> None:
    """Move every bookmark from one folder to another (or out of it, with None)."""
    for b in _mine(db, user):
        names = folders_of(b)
        if old in names:
            names = [new if n == old else n for n in names if n is not None]
            _store(b, {**_bookmark_view(b), "folders": bookmarks.clean_folders([n for n in names if n])})


@router.put("/me/bookmark-folders/{name:path}")
def edit_folder(name: str, body: FolderBody, user: User = Depends(tuner_user), db=Depends(get_db)):
    """Recolour a folder, or rename it: every bookmark in it moves with it."""
    folder = db.scalar(select(BookmarkFolder).where(BookmarkFolder.user_id == user.id, BookmarkFolder.name == name))
    if folder is None:
        raise HTTPException(404, "Folder not found")
    if body.color:
        folder.color = body.color.lower()
    new = body.name.strip() if body.name else None
    if new and new != name:
        if db.scalar(select(BookmarkFolder).where(BookmarkFolder.user_id == user.id, BookmarkFolder.name == new)):
            raise HTTPException(422, f"You already have a folder called {new}.")
        _rename(db, user, name, new)
        folder.name = new
    db.commit()
    return {"name": folder.name, "color": folder.color}


@router.delete("/me/bookmark-folders/{name:path}")
def remove_folder(name: str, user: User = Depends(tuner_user), db=Depends(get_db)):
    """Take the folder off every bookmark in it. The bookmarks themselves stay."""
    _rename(db, user, name, None)
    folder = db.scalar(select(BookmarkFolder).where(BookmarkFolder.user_id == user.id, BookmarkFolder.name == name))
    if folder is not None:
        db.delete(folder)
    db.commit()
    return {"ok": True}


# --- import and export -----------------------------------------------------------------------------
class ImportBody(BaseModel):
    format: str = Field(pattern="^(sentrysdr|gqrx|sdrpp)$")
    text: str = Field(max_length=900_000)
    replace: bool = False


@router.post("/me/bookmarks/import")
def import_bookmarks(body: ImportBody, user: User = Depends(tuner_user), db=Depends(get_db)):
    """Add bookmarks from a file, skipping ones already there. `replace` clears the old ones first."""
    try:
        found, colors = bookmarks.parse(body.text, body.format)
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
    used: list[str] = []
    for item in found:
        if (item["name"], item["frequency_hz"], item["mode"]) in existing:
            skipped += 1
            continue
        if added >= room:
            break
        bookmark = UserBookmark(user_id=user.id)
        _store(bookmark, item)
        db.add(bookmark)
        existing.add((item["name"], item["frequency_hz"], item["mode"]))
        used += [f for f in item["folders"] if f not in used]
        added += 1
    _ensure_folders(db, user, used, colors)
    db.commit()
    return {"added": added, "skipped": skipped, "left_out": len(found) - added - skipped}


@router.get("/me/bookmarks/export")
def export_bookmarks(format: str = Query(default="sentrysdr", pattern="^(sentrysdr|gqrx|sdrpp)$"),
                     user: User = Depends(tuner_user), db=Depends(get_db)):
    text, filename, media = bookmarks.export([_bookmark_view(b) for b in _mine(db, user)], _colors(db, user), format)
    return Response(text, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
