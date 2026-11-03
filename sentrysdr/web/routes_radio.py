"""Sources, targets, recorders, assignment, locks, live audio and maps."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from .. import pipelines, schedule, security
from ..config import SERIAL_RE
from ..context import Context
from ..models import Node, Recorder, Source, Target, User, now
from .deps import admin_user, ctx_of, current_user, get_db, ip_of, require

router = APIRouter(prefix="/api")

LOCK_WINDOW = 900
LOCK_LIMIT = 8
LIVE_MAX_TOTAL = 50
LIVE_MAX_PER_USER = 3
_live_counts: dict[int, int] = {}


# --- sources ---------------------------------------------------------------
class SourceBody(BaseModel):
    node_id: int | None = None
    serial: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    antenna: str = Field(default="", max_length=120)
    freq_min_hz: int | None = Field(default=None, ge=0)
    freq_max_hz: int | None = Field(default=None, ge=0)
    gain_db: float | None = Field(default=None, ge=0, le=60)
    ppm: int = Field(default=0, ge=-500, le=500)

    @field_validator("serial")
    @classmethod
    def _serial(cls, value: str) -> str:
        if not SERIAL_RE.match(value):
            raise ValueError("invalid serial")
        return value


def _source_view(ctx: Context, db, source: Source, winners: dict, recorders: list[Recorder]) -> dict:
    node = db.get(Node, source.node_id)
    base = next((r for r in recorders if r.source_id == source.id and r.schedule_days is None), None)
    active = winners.get(source.id)
    target = db.get(Target, active.target_id) if active else None
    online = ctx.node_online(node) if node else False
    status = ctx.status.get((source.node_id, source.serial), {}) if online else {}
    return {
        "id": source.id, "node_id": source.node_id, "node_name": node.name if node else "",
        "node_online": online, "serial": source.serial, "name": source.name,
        "description": source.description, "antenna": source.antenna,
        "freq_min_hz": source.freq_min_hz, "freq_max_hz": source.freq_max_hz,
        "gain_db": source.gain_db, "ppm": source.ppm,
        "state": status.get("state", "offline" if not online else "idle"),
        "error": status.get("error", ""), "in_call": status.get("in_call", False),
        "call_started": status.get("call_started"), "last_call_at": status.get("last_call_at"),
        "locked": bool(base and base.lock_hash),
        "base_recorder_id": base.id if base else None,
        "base_target_id": base.target_id if base else None,
        "assigned_by": base.assigned_by if base else "",
        "active_recorder_id": active.id if active else None,
        "active_recorder_name": active.name if active else None,
        "active_is_schedule": bool(active and active.schedule_days is not None),
        "active_target": {
            "id": target.id, "name": target.name, "frequency_hz": target.frequency_hz,
            "modulation": target.modulation, "kind": target.kind,
        } if target else None,
    }


def _winners(ctx: Context, db) -> tuple[dict, list[Recorder]]:
    recorders = list(db.scalars(select(Recorder)))
    return schedule.resolve(recorders, datetime.now(ctx.cfg.tz)), recorders


@router.get("/sources")
def list_sources(request: Request, _user: User = Depends(current_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    winners, recorders = _winners(ctx, db)
    sources = db.scalars(select(Source).order_by(Source.name))
    return [_source_view(ctx, db, s, winners, recorders) for s in sources]


@router.get("/sources/available")
def available_serials(request: Request, _admin: User = Depends(admin_user), db=Depends(get_db)):
    """Configured serials per node, marking those not yet defined as sources."""
    ctx = ctx_of(request)
    connected = ctx.enumerate()
    defined = {(s.node_id, s.serial) for s in db.scalars(select(Source))}
    result = []
    for node in db.scalars(select(Node).where(Node.status == "active")):
        for serial in node.serials or []:
            result.append({
                "node_id": node.id, "node_name": node.name, "serial": serial,
                "defined": (node.id, serial) in defined,
                "connected": serial in connected if node.is_local else ctx.node_online(node),
            })
    return result


def _validate_source(ctx: Context, db, body: SourceBody) -> Node:
    node = db.get(Node, body.node_id or ctx.local_node_id)
    if node is None or node.status != "active":
        raise HTTPException(404, "Node not found")
    if body.serial not in (node.serials or []):
        raise HTTPException(422, "That serial isn't in the node's sdr.serials configuration")
    if body.freq_min_hz and body.freq_max_hz and body.freq_min_hz > body.freq_max_hz:
        raise HTTPException(422, "Minimum frequency is above maximum frequency")
    return node


@router.post("/sources", status_code=201)
def create_source(body: SourceBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    node = _validate_source(ctx, db, body)
    if db.scalar(select(Source).where(Source.node_id == node.id, Source.serial == body.serial)):
        raise HTTPException(409, "That SDR is already defined")
    values = body.model_dump()
    values["node_id"] = node.id
    source = Source(**values)
    db.add(source)
    db.commit()
    ctx.audit("source.created", user=admin, ip=ip_of(request), target=source.name)
    ctx.kick()
    return {"id": source.id}


@router.put("/sources/{source_id}")
def update_source(source_id: int, body: SourceBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "Source not found")
    node = _validate_source(ctx, db, body)
    values = body.model_dump()
    values["node_id"] = node.id
    for key, value in values.items():
        setattr(source, key, value)
    db.commit()
    ctx.audit("source.updated", user=admin, ip=ip_of(request), target=source.name)
    ctx.kick()
    return {"id": source.id}


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "Source not found")
    name = source.name
    db.delete(source)  # recordings stay: they hold their own copies of the names
    db.commit()
    ctx.audit("source.deleted", user=admin, ip=ip_of(request), target=name)
    ctx.kick()
    return {"detail": "Deleted"}


# --- targets ---------------------------------------------------------------
class TargetBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = "audio"
    frequency_hz: int = Field(ge=pipelines.FREQ_MIN_HZ, le=pipelines.FREQ_MAX_HZ)
    modulation: str = "nfm"
    squelch: int = Field(default=50, ge=0, le=100000)
    clearcut_enabled: bool = False
    clearcut_system: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    clearcut_id: int | None = None
    rdio_enabled: bool = False
    rdio_system: int | None = None
    rdio_talkgroup: int | None = None


def _validate_target(body: TargetBody) -> None:
    allowed = {"audio": pipelines.ANALOG | set(pipelines.DIGITAL), "adsb": {"adsb"}, "ais": {"ais"}}
    if body.modulation not in allowed.get(body.kind, set()):
        raise HTTPException(422, f"Modulation {body.modulation} doesn't fit a {body.kind} target")
    if body.clearcut_enabled and (body.clearcut_id is None or not body.clearcut_system):
        raise HTTPException(422, "ClearCut upload needs a system ID and a talkgroup")
    if body.rdio_enabled and (body.rdio_system is None or body.rdio_talkgroup is None):
        raise HTTPException(422, "RDIO upload needs a system and a talkgroup")


def _target_view(target: Target) -> dict:
    return {c.name: getattr(target, c.name) for c in Target.__table__.columns}


@router.get("/targets")
def list_targets(_user: User = Depends(current_user), db=Depends(get_db)):
    return [_target_view(t) for t in db.scalars(select(Target).order_by(Target.name))]


@router.post("/targets", status_code=201)
def create_target(body: TargetBody, request: Request, user: User = Depends(require("manage_targets")), db=Depends(get_db)):
    ctx = ctx_of(request)
    _validate_target(body)
    target = Target(**body.model_dump())
    db.add(target)
    db.commit()
    ctx.audit("target.created", user=user, ip=ip_of(request), target=target.name)
    return _target_view(target)


def _active_uses_locked(db, target_id: int) -> bool:
    return bool(db.scalar(select(Recorder.id).where(Recorder.target_id == target_id, Recorder.lock_hash.is_not(None))))


@router.put("/targets/{target_id}")
def update_target(target_id: int, body: TargetBody, request: Request, user: User = Depends(require("manage_targets")), db=Depends(get_db)):
    ctx = ctx_of(request)
    target = db.get(Target, target_id)
    if target is None:
        raise HTTPException(404, "Target not found")
    # Otherwise a locked recorder could be retuned by editing its target.
    if user.role != "admin" and _active_uses_locked(db, target_id):
        raise HTTPException(403, "A locked recorder uses this target. Ask an administrator.")
    _validate_target(body)
    for key, value in body.model_dump().items():
        setattr(target, key, value)
    db.commit()
    ctx.audit("target.updated", user=user, ip=ip_of(request), target=target.name)
    ctx.kick()
    return _target_view(target)


@router.delete("/targets/{target_id}")
def delete_target(target_id: int, request: Request, user: User = Depends(require("manage_targets")), db=Depends(get_db)):
    ctx = ctx_of(request)
    target = db.get(Target, target_id)
    if target is None:
        raise HTTPException(404, "Target not found")
    if user.role != "admin" and _active_uses_locked(db, target_id):
        raise HTTPException(403, "A locked recorder uses this target. Ask an administrator.")
    name = target.name
    db.delete(target)
    db.commit()
    ctx.audit("target.deleted", user=user, ip=ip_of(request), target=name)
    ctx.kick()
    return {"detail": "Deleted"}


# --- locks -----------------------------------------------------------------
def _check_lock(ctx: Context, request: Request, user: User, recorder: Recorder | None, password: str | None) -> None:
    """Admins bypass locks. Everyone else needs the password, rate limited."""
    if recorder is None or not recorder.lock_hash or user.role == "admin":
        return
    key = f"lock:{user.id}"
    if ctx.limiter.blocked(key, LOCK_LIMIT, LOCK_WINDOW):
        raise HTTPException(429, "Too many attempts. Try again later.")
    if not password or not security.verify_password(recorder.lock_hash, password):
        ctx.limiter.hit(key, LOCK_WINDOW)
        ctx.audit("recorder.lock_denied", user=user, ip=ip_of(request), target=recorder.name)
        raise HTTPException(403, "locked")


def _base_for(db, source_id: int) -> Recorder | None:
    for recorder in db.scalars(select(Recorder).where(Recorder.source_id == source_id)):
        if recorder.schedule_days is None:
            return recorder
    return None


def _range_warning(source: Source, target: Target) -> str | None:
    if target.frequency_hz and (
        (source.freq_min_hz and target.frequency_hz < source.freq_min_hz)
        or (source.freq_max_hz and target.frequency_hz > source.freq_max_hz)
    ):
        return f"{target.name} is outside the antenna range set for {source.name}"
    return None


class AssignBody(BaseModel):
    target_id: int | None = None
    lock_password: str | None = Field(default=None, max_length=256)


@router.post("/sources/{source_id}/assign")
def assign(source_id: int, body: AssignBody, request: Request, user: User = Depends(require("assign")), db=Depends(get_db)):
    """Set or clear a source's base target. A lock blocks this without its password."""
    ctx = ctx_of(request)
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "Source not found")
    base = _base_for(db, source_id)
    _check_lock(ctx, request, user, base, body.lock_password)
    warning = None
    if body.target_id is None:
        if base:
            db.delete(base)
        target_name = None
    else:
        target = db.get(Target, body.target_id)
        if target is None:
            raise HTTPException(404, "Target not found")
        warning = _range_warning(source, target)
        target_name = target.name
        if base is None:
            db.add(Recorder(name=f"{source.name}: {target.name}", source_id=source.id, target_id=target.id,
                            assigned_by=user.email, assigned_at=now()))
        else:
            base.target_id, base.name, base.assigned_by, base.assigned_at = target.id, f"{source.name}: {target.name}", user.email, now()
    db.commit()
    ctx.audit("source.assigned", user=user, ip=ip_of(request), target=source.name, details={"target": target_name})
    ctx.kick()
    return {"detail": "Assigned", "warning": warning}


class LockBody(BaseModel):
    password: str = Field(min_length=4, max_length=256)


@router.post("/sources/{source_id}/lock")
def lock_source(source_id: int, body: LockBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    source = db.get(Source, source_id)
    base = _base_for(db, source_id)
    if source is None or base is None:
        raise HTTPException(409, "Assign a target before locking this source")
    base.lock_hash = security.hash_password(body.password)
    db.commit()
    ctx.audit("source.locked", user=admin, ip=ip_of(request), target=source.name)
    return {"detail": "Locked"}


@router.delete("/sources/{source_id}/lock")
def unlock_source(source_id: int, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    base = _base_for(db, source_id)
    if base is not None:
        base.lock_hash = None
        db.commit()
    ctx.audit("source.unlocked", user=admin, ip=ip_of(request), target=str(source_id))
    return {"detail": "Unlocked"}


# --- scheduled recorders ---------------------------------------------------
class RecorderBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source_id: int
    target_id: int
    enabled: bool = True
    schedule_days: list[int] = Field(min_length=1, max_length=7)
    schedule_start: str
    schedule_end: str
    lock_password: str | None = Field(default=None, max_length=256)


def _validate_schedule(body: RecorderBody) -> None:
    if any(day not in range(7) for day in body.schedule_days):
        raise HTTPException(422, "Days must be 0 (Monday) to 6 (Sunday)")
    if not schedule.valid_time(body.schedule_start) or not schedule.valid_time(body.schedule_end):
        raise HTTPException(422, "Times must be HH:MM")


def _recorder_view(db, recorder: Recorder) -> dict:
    source, target = db.get(Source, recorder.source_id), db.get(Target, recorder.target_id)
    return {
        "id": recorder.id, "name": recorder.name, "enabled": recorder.enabled,
        "source_id": recorder.source_id, "source_name": source.name if source else "",
        "target_id": recorder.target_id, "target_name": target.name if target else "",
        "frequency_hz": target.frequency_hz if target else None,
        "schedule_days": recorder.schedule_days, "schedule_start": recorder.schedule_start,
        "schedule_end": recorder.schedule_end, "assigned_by": recorder.assigned_by,
    }


@router.get("/recorders")
def list_recorders(_user: User = Depends(current_user), db=Depends(get_db)):
    return [_recorder_view(db, r) for r in db.scalars(select(Recorder).order_by(Recorder.id))]


def _save_recorder(ctx, request, user, db, body: RecorderBody, recorder: Recorder | None):
    _validate_schedule(body)
    source, target = db.get(Source, body.source_id), db.get(Target, body.target_id)
    if source is None or target is None:
        raise HTTPException(404, "Source or target not found")
    # A schedule overrides the base assignment, so it must honour that lock too.
    _check_lock(ctx, request, user, _base_for(db, body.source_id), body.lock_password)
    if recorder is None:
        recorder = Recorder(name=body.name, source_id=source.id, target_id=target.id)
        db.add(recorder)
    recorder.name, recorder.source_id, recorder.target_id = body.name, source.id, target.id
    recorder.enabled = body.enabled
    recorder.schedule_days = sorted(set(body.schedule_days))
    recorder.schedule_start, recorder.schedule_end = body.schedule_start, body.schedule_end
    recorder.assigned_by = user.email
    db.commit()
    ctx.audit("recorder.saved", user=user, ip=ip_of(request), target=recorder.name)
    ctx.kick()
    return {**_recorder_view(db, recorder), "warning": _range_warning(source, target)}


@router.post("/recorders", status_code=201)
def create_recorder(body: RecorderBody, request: Request, user: User = Depends(require("assign")), db=Depends(get_db)):
    return _save_recorder(ctx_of(request), request, user, db, body, None)


def _scheduled_recorder(db, recorder_id: int) -> Recorder:
    recorder = db.get(Recorder, recorder_id)
    if recorder is None or recorder.schedule_days is None:
        raise HTTPException(404, "Scheduled recorder not found")
    return recorder


@router.put("/recorders/{recorder_id}")
def update_recorder(recorder_id: int, body: RecorderBody, request: Request, user: User = Depends(require("assign")), db=Depends(get_db)):
    ctx = ctx_of(request)
    recorder = _scheduled_recorder(db, recorder_id)
    _check_lock(ctx, request, user, _base_for(db, recorder.source_id), body.lock_password)
    return _save_recorder(ctx, request, user, db, body, recorder)


@router.delete("/recorders/{recorder_id}")
def delete_recorder(recorder_id: int, request: Request,
                    user: User = Depends(require("assign")), db=Depends(get_db)):
    ctx = ctx_of(request)
    recorder = _scheduled_recorder(db, recorder_id)
    _check_lock(ctx, request, user, _base_for(db, recorder.source_id), request.headers.get("x-lock-password"))
    name = recorder.name
    db.delete(recorder)
    db.commit()
    ctx.audit("recorder.deleted", user=user, ip=ip_of(request), target=name)
    ctx.kick()
    return {"detail": "Deleted"}


# --- live audio ------------------------------------------------------------
@router.get("/live/{source_id}")
async def live_audio(source_id: int, request: Request, user: User = Depends(require("listen_live")), db=Depends(get_db)):
    ctx = ctx_of(request)
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "Source not found")
    key = ctx.live_key(source.node_id, source.serial)
    db.close()  # don't pin a database connection for the length of the stream
    if sum(_live_counts.values()) >= LIVE_MAX_TOTAL or _live_counts.get(user.id, 0) >= LIVE_MAX_PER_USER:
        raise HTTPException(429, "Too many live streams open")
    queue = await ctx.live.subscribe(key)
    _live_counts[user.id] = _live_counts.get(user.id, 0) + 1
    ctx.audit("live.started", user=user, ip=ip_of(request), target=source.name)

    async def stream():
        try:
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), 15)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        return
        finally:
            _live_counts[user.id] -= 1
            if _live_counts[user.id] <= 0:
                _live_counts.pop(user.id, None)
            await asyncio.shield(ctx.live.unsubscribe(key, queue))

    return StreamingResponse(stream(), media_type="audio/mpeg", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    })


# --- maps ------------------------------------------------------------------
@router.get("/maps/{kind}")
def map_tracks(kind: str, request: Request, _user: User = Depends(require("maps"))):
    if kind not in ("adsb", "ais"):
        raise HTTPException(404, "Unknown map")
    return {"items": ctx_of(request).current_tracks(kind)}


def _tar1090_json(ctx: Context, name: str) -> Response:
    """The two JSON files tar1090 polls, built from all receivers' merged data."""
    if name == "aircraft.json":
        body = {"now": time.time(), "messages": 0, "aircraft": ctx.current_tracks("adsb")}
    elif name == "receiver.json":
        body = {"version": "SentrySDR", "refresh": 1000, "history": 0}
    else:
        raise HTTPException(404, "Not found")
    return Response(json.dumps(body), media_type="application/json")


def _tar1090_dir(request: Request) -> Path:
    directory = getattr(request.app.state, "tar1090_dir", None)
    if directory is None:
        raise HTTPException(404, "tar1090 is not installed")
    return Path(directory).resolve()


@router.get("/tar1090/{path:path}", include_in_schema=False)
def tar1090_static(path: str, request: Request, _user: User = Depends(require("maps"))):
    root = _tar1090_dir(request)
    if path.startswith("data/"):
        return _tar1090_json(ctx_of(request), path.removeprefix("data/"))
    candidate = (root / (path or "index.html")).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(candidate)
