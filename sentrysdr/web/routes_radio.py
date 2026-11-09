"""Sources, targets, recorders, assignment, locks, live audio and maps."""
from __future__ import annotations

import asyncio
import json
import uuid
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from .. import aio, capabilities, feeds, pipelines, schedule
from ..config import SERIAL_RE
from ..context import Context
from ..engine import MeasureError
from ..models import ANTENNA_UNITS, Antenna, BroadcastifySystem, Node, Recorder, SdrProfile, Source, Target, User, now
from .deps import admin_user, ctx_of, current_user, effective_permissions, get_db, ip_of, require
from .routes_messages import annotate_aircraft

router = APIRouter(prefix="/api")

MEASURE_LIMIT = 30
MEASURE_WINDOW = 3600
LIVE_MAX_TOTAL = 50
LIVE_MAX_PER_USER = 3
MAX_ANTENNA_RANGES = 20
_live_counts: dict[int, int] = {}


# --- antennas --------------------------------------------------------------
class RangeBody(BaseModel):
    low_hz: int = Field(ge=0, le=10_000_000_000)
    high_hz: int = Field(ge=0, le=10_000_000_000)


class AntennaBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(default="", max_length=60)
    length: float | None = Field(default=None, gt=0, le=100_000)
    length_unit: str = "in"
    notes: str = Field(default="", max_length=500)
    ranges: list[RangeBody] = Field(default_factory=list, max_length=MAX_ANTENNA_RANGES)

    @field_validator("length_unit")
    @classmethod
    def _unit(cls, value: str) -> str:
        if value not in ANTENNA_UNITS:
            raise ValueError(f"unit must be one of {', '.join(ANTENNA_UNITS)}")
        return value

    def clean_ranges(self) -> list[list[int]]:
        for item in self.ranges:
            if item.low_hz > item.high_hz:
                raise HTTPException(422, "A range starts above where it ends")
        return [list(pair) for pair in sorted({(r.low_hz, r.high_hz) for r in self.ranges})]


def antenna_covers(antenna: Antenna | None, frequency_hz: int) -> bool:
    """An antenna with no ranges entered isn't judged."""
    if antenna is None or not antenna.ranges:
        return True
    return any(low <= frequency_hz <= high for low, high in antenna.ranges)


def _antenna_view(db, antenna: Antenna) -> dict:
    users = db.scalars(select(Source.name).where(Source.antenna_id == antenna.id).order_by(Source.name)).all()
    return {
        "id": antenna.id, "name": antenna.name, "kind": antenna.kind, "length": antenna.length,
        "length_unit": antenna.length_unit, "notes": antenna.notes, "ranges": antenna.ranges or [],
        "sources": list(users),
    }


@router.get("/antennas")
def list_antennas(_user: User = Depends(current_user), db=Depends(get_db)):
    return [_antenna_view(db, a) for a in db.scalars(select(Antenna).order_by(Antenna.name))]


def _apply_antenna(antenna: Antenna, body: AntennaBody) -> None:
    antenna.ranges = body.clean_ranges()
    antenna.name, antenna.kind, antenna.notes = body.name.strip(), body.kind.strip(), body.notes
    antenna.length, antenna.length_unit = body.length, body.length_unit


@router.post("/antennas", status_code=201)
def create_antenna(body: AntennaBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    antenna = Antenna()
    _apply_antenna(antenna, body)
    db.add(antenna)
    db.commit()
    ctx_of(request).audit("antenna.created", user=admin, ip=ip_of(request), target=antenna.name)
    return _antenna_view(db, antenna)


@router.put("/antennas/{antenna_id}")
def update_antenna(antenna_id: int, body: AntennaBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    antenna = db.get(Antenna, antenna_id)
    if antenna is None:
        raise HTTPException(404, "Antenna not found")
    _apply_antenna(antenna, body)
    db.commit()
    ctx_of(request).audit("antenna.updated", user=admin, ip=ip_of(request), target=antenna.name)
    return _antenna_view(db, antenna)


@router.delete("/antennas/{antenna_id}")
def delete_antenna(antenna_id: int, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    antenna = db.get(Antenna, antenna_id)
    if antenna is None:
        raise HTTPException(404, "Antenna not found")
    name = antenna.name
    for source in db.scalars(select(Source).where(Source.antenna_id == antenna_id)):
        source.antenna_id = None
    db.delete(antenna)
    db.commit()
    ctx_of(request).audit("antenna.deleted", user=admin, ip=ip_of(request), target=name)
    return {"detail": "Deleted"}


# --- sources ---------------------------------------------------------------
class SourceBody(BaseModel):
    node_id: int | None = None
    serial: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    antenna_id: int | None = None
    gain_db: float | None = Field(default=None, ge=0, le=60)
    ppm: int = Field(default=0, ge=-500, le=500)
    tuner: str | None = Field(default=None, max_length=40)
    gain_steps: list[float] = Field(default_factory=list, max_length=capabilities.MAX_GAIN_STEPS)
    cap_min_hz: int | None = Field(default=None, ge=0, le=10_000_000_000)
    cap_max_hz: int | None = Field(default=None, ge=0, le=10_000_000_000)
    uses: list[str] = Field(default_factory=list, max_length=len(capabilities.USES) + len(capabilities.RETIRED_USES))

    @field_validator("gain_steps")
    @classmethod
    def _steps(cls, value: list[float]) -> list[float]:
        if any(not -20 <= step <= 70 for step in value):
            raise ValueError("gain steps must be between -20 and 70 dB")
        return sorted({round(step, 1) for step in value})

    @field_validator("uses")
    @classmethod
    def _uses(cls, value: list[str]) -> list[str]:
        value = [use for use in value if use not in capabilities.RETIRED_USES]  # from a page opened before they went
        unknown = set(value) - set(capabilities.USES)
        if unknown:
            raise ValueError(f"unknown uses: {', '.join(sorted(unknown))}")
        return sorted(set(value))

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
    profiles = list(db.scalars(select(SdrProfile).where(SdrProfile.source_id == source.id)))
    profile = next((p for p in profiles if target and p.target_id == target.id), None)
    online = ctx.node_online(node) if node else False
    status = ctx.status.get((source.node_id, source.serial), {}) if online else {}
    antenna = db.get(Antenna, source.antenna_id) if source.antenna_id else None
    return {
        "id": source.id, "node_id": source.node_id, "node_name": node.name if node else "",
        "node_online": online, "serial": source.serial, "name": source.name,
        "description": source.description, "antenna_id": antenna.id if antenna else None,
        "antenna_name": antenna.name if antenna else "", "antenna_ranges": antenna.ranges if antenna else [],
        "gain_db": source.gain_db, "ppm": source.ppm,
        "tuner": source.tuner or "", "gain_steps": source.gain_steps or [],
        "cap_min_hz": source.cap_min_hz, "cap_max_hz": source.cap_max_hz, "uses": source.uses or [],
        "state": status.get("state", "offline" if not online else "idle"),
        "error": status.get("error", ""), "in_call": status.get("in_call", False),
        "note": status.get("note", ""), "feeds": status.get("feeds") or {},
        "call_started": status.get("call_started"), "last_call_at": status.get("last_call_at"),
        "locked": bool(base and base.locked),
        "base_recorder_id": base.id if base else None,
        "base_target_id": base.target_id if base else None,
        "assigned_by": base.assigned_by if base else "",
        "active_recorder_id": active.id if active else None,
        "active_recorder_name": active.name if active else None,
        "active_is_schedule": bool(active and active.schedule_days is not None),
        "active_target": {
            "id": target.id, "name": target.name, "frequency_hz": target.frequency_hz,
            "modulation": target.modulation, "kind": target.kind,
            "squelch": profile.squelch if profile else target.squelch,
            "gain_db": profile.gain_db if profile else source.gain_db,
            "from_profile": profile is not None,
        } if target else None,
        "profiles": [_profile_view(p) for p in profiles],
    }


def _profile_view(profile: SdrProfile) -> dict:
    return {
        "target_id": profile.target_id, "squelch": profile.squelch, "gain_db": profile.gain_db,
        "noise_level": profile.noise_level, "measured_at": profile.measured_at,
        "updated_by": profile.updated_by,
    }


def _winners(ctx: Context, db) -> tuple[dict, list[Recorder]]:
    recorders = list(db.scalars(select(Recorder)))
    return schedule.resolve(recorders, datetime.now(ctx.cfg.tz)), recorders


@router.get("/sources")
def list_sources(request: Request, user: User = Depends(current_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    winners, recorders = _winners(ctx, db)
    sources = db.scalars(select(Source).order_by(Source.name))
    views = []
    for source in sources:
        view = _source_view(ctx, db, source, winners, recorders)
        # Admins only: with the ID, someone else could feed aggregators as this receiver.
        if user.role == "admin":
            view["feeder_uuid"] = source.feeder_uuid
        if "assign" not in effective_permissions(user):
            view["assigned_by"] = ""  # other people's email addresses
        views.append(view)
    return views


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
    if body.antenna_id is not None and db.get(Antenna, body.antenna_id) is None:
        raise HTTPException(422, "That antenna doesn't exist")
    if body.cap_min_hz is not None and body.cap_max_hz is not None and body.cap_min_hz > body.cap_max_hz:
        raise HTTPException(422, "The stick's frequency range starts above where it ends")
    return node


def _refuse_off_step(steps: list[float] | None, gain: float | None, unchanged: float | None = None) -> None:
    """Once a stick's steps are known, only those can be chosen. A gain saved before then may stay."""
    if gain is None or (unchanged is not None and abs(gain - unchanged) < 1e-9):
        return
    nearest = capabilities.nearest_step(steps, gain)
    if nearest is not None:
        raise HTTPException(422, f"{gain:g} dB isn't one of this stick's gain steps. The nearest is {nearest:g} dB.")


def _gain_warning(source: Source) -> str | None:
    nearest = capabilities.nearest_step(source.gain_steps, source.gain_db)
    if nearest is None:
        return None
    return f"{source.gain_db:g} dB isn't one of this stick's gain steps, so it will use {nearest:g} dB."


@router.post("/sources", status_code=201)
def create_source(body: SourceBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    node = _validate_source(ctx, db, body)
    _refuse_off_step(body.gain_steps, body.gain_db)
    if db.scalar(select(Source).where(Source.node_id == node.id, Source.serial == body.serial)):
        raise HTTPException(409, "That SDR is already defined")
    values = body.model_dump()
    values["node_id"] = node.id
    source = Source(**values, feeder_uuid=str(uuid.uuid4()))
    db.add(source)
    db.commit()
    ctx.audit("source.created", user=admin, ip=ip_of(request), target=source.name)
    ctx.kick()
    return {"id": source.id, "warning": _gain_warning(source)}


@router.put("/sources/{source_id}")
def update_source(source_id: int, body: SourceBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "Source not found")
    node = _validate_source(ctx, db, body)
    _refuse_off_step(body.gain_steps, body.gain_db, source.gain_db)
    values = body.model_dump()
    values["node_id"] = node.id
    for key, value in values.items():
        setattr(source, key, value)
    db.commit()
    ctx.audit("source.updated", user=admin, ip=ip_of(request), target=source.name)
    ctx.kick()
    return {"id": source.id, "warning": _gain_warning(source)}


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
    volume_db: float = Field(default=0, ge=pipelines.VOLUME_MIN_DB, le=pipelines.VOLUME_MAX_DB)
    clearcut_enabled: bool = False
    clearcut_system: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    clearcut_id: int | None = None
    rdio_enabled: bool = False
    rdio_system: int | None = None
    rdio_talkgroup: int | None = None
    broadcastify_enabled: bool = False
    broadcastify_system_ref: int | None = None
    broadcastify_slot: int | None = Field(default=None, ge=1)
    acars_channels: list[int] | None = Field(default_factory=list, max_length=pipelines.ACARS_MAX_CHANNELS)
    airframes_enabled: bool = False
    feeds: dict | None = None


def _validate_target(body: TargetBody, db, target_id: int | None = None) -> None:
    allowed = {"audio": pipelines.ANALOG | set(pipelines.DIGITAL), "adsb": {"adsb"}, "ais": {"ais"}, "acars": {"acars"}}
    if body.modulation not in allowed.get(body.kind, set()):
        raise HTTPException(422, f"Modulation {body.modulation} doesn't fit a {body.kind} target")
    if body.kind == "acars":
        body.acars_channels = sorted(set(body.acars_channels or []))
        problem = pipelines.acars_channel_problem(body.acars_channels)
        if problem:
            raise HTTPException(422, problem)
        body.frequency_hz = body.acars_channels[0]  # the lowest, for range checks and lists
    else:
        if body.acars_channels or body.airframes_enabled:  # None is fine too
            raise HTTPException(422, "Only ACARS targets have channels or feed airframes.io")
        body.acars_channels = None
    try:
        body.feeds = feeds.clean(body.kind, body.feeds) or None
    except feeds.FeedError as exc:
        raise HTTPException(422, str(exc)) from None
    if body.clearcut_enabled and (body.clearcut_id is None or not body.clearcut_system):
        raise HTTPException(422, "ClearCut upload needs a system ID and a talkgroup")
    if body.rdio_enabled and (body.rdio_system is None or body.rdio_talkgroup is None):
        raise HTTPException(422, "RDIO upload needs a system and a talkgroup")
    if body.broadcastify_enabled:
        if body.kind != "audio":
            raise HTTPException(422, "Only audio targets can upload to Broadcastify")
        if body.broadcastify_system_ref is None or body.broadcastify_slot is None:
            raise HTTPException(422, "Broadcastify upload needs a system and a slot")
        if db.get(BroadcastifySystem, body.broadcastify_system_ref) is None:
            raise HTTPException(422, "That Broadcastify system doesn't exist")
        taken = db.scalar(select(Target).where(
            Target.broadcastify_enabled.is_(True), Target.broadcastify_system_ref == body.broadcastify_system_ref,
            Target.broadcastify_slot == body.broadcastify_slot, Target.id != (target_id or 0),
        ))
        if taken:
            raise HTTPException(409, f"Slot {body.broadcastify_slot} on that system is already used by {taken.name}")


def _same_channel_warning(db, body: TargetBody, target_id: int | None = None) -> str | None:
    others = db.scalars(select(Target.name).where(
        Target.frequency_hz == body.frequency_hz, Target.modulation == body.modulation, Target.id != (target_id or 0),
    )).all()
    if others and body.kind == "audio":
        return f"Same frequency and mode as {', '.join(others)}. If both run, this channel is recorded twice."
    return None


def _target_view(target: Target) -> dict:
    return {c.name: getattr(target, c.name) for c in Target.__table__.columns}


@router.get("/broadcastify-systems")
def broadcastify_choices(_user: User = Depends(require("manage_targets")), db=Depends(get_db)):
    """What a target can upload to. Keys stay with administrators, and on the server."""
    return [{"id": s.id, "name": s.name, "system_id": s.system_id}
            for s in db.scalars(select(BroadcastifySystem).order_by(BroadcastifySystem.name))]


# Upload IDs and feed addresses (some services' ports identify your station) are only for
# people who manage targets. Everyone else gets what the pages they can see actually use.
TARGET_PRIVATE_FIELDS = (
    "clearcut_system", "clearcut_id", "rdio_system", "rdio_talkgroup", "broadcastify_system_ref", "broadcastify_slot", "feeds",
)


@router.get("/targets")
def list_targets(user: User = Depends(current_user), db=Depends(get_db)):
    views = [_target_view(t) for t in db.scalars(select(Target).order_by(Target.name))]
    if "manage_targets" not in effective_permissions(user):
        for view in views:
            for field in TARGET_PRIVATE_FIELDS:
                view[field] = None
    return views


@router.post("/targets", status_code=201)
def create_target(body: TargetBody, request: Request, user: User = Depends(require("manage_targets")), db=Depends(get_db)):
    ctx = ctx_of(request)
    _validate_target(body, db)
    warning = _same_channel_warning(db, body)
    target = Target(**body.model_dump())
    db.add(target)
    db.commit()
    ctx.audit("target.created", user=user, ip=ip_of(request), target=target.name)
    return {**_target_view(target), "warning": warning}


def _active_uses_locked(db, target_id: int) -> bool:
    return bool(db.scalar(select(Recorder.id).where(Recorder.target_id == target_id, Recorder.locked.is_(True))))


@router.put("/targets/{target_id}")
def update_target(target_id: int, body: TargetBody, request: Request, user: User = Depends(require("manage_targets")), db=Depends(get_db)):
    ctx = ctx_of(request)
    target = db.get(Target, target_id)
    if target is None:
        raise HTTPException(404, "Target not found")
    # Otherwise a locked recorder could be retuned by editing its target.
    if user.role != "admin" and _active_uses_locked(db, target_id):
        raise HTTPException(403, "A locked recorder uses this target. Ask an administrator.")
    _validate_target(body, db, target_id)
    warning = _same_channel_warning(db, body, target_id)
    # A measurement only describes the frequency and mode it was taken on.
    retuned = (body.frequency_hz, body.modulation) != (target.frequency_hz, target.modulation)
    cleared = list(db.scalars(select(SdrProfile).where(SdrProfile.target_id == target_id))) if retuned else []
    for profile in cleared:
        db.delete(profile)
    for key, value in body.model_dump().items():
        setattr(target, key, value)
    db.commit()
    ctx.audit("target.updated", user=user, ip=ip_of(request), target=target.name,
              details={"profiles_cleared": len(cleared)} if cleared else None)
    ctx.kick()
    return {**_target_view(target), "profiles_cleared": len(cleared), "warning": warning}


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
LOCKED_MESSAGE = "This assignment is locked. Only an administrator can change it."


def _check_lock(ctx: Context, request: Request, user: User, recorder: Recorder | None) -> None:
    """Only administrators can change a locked assignment."""
    if recorder is None or not recorder.locked or user.role == "admin":
        return
    ctx.audit("recorder.lock_denied", user=user, ip=ip_of(request), target=recorder.name)
    raise HTTPException(403, LOCKED_MESSAGE)


def _base_for(db, source_id: int) -> Recorder | None:
    for recorder in db.scalars(select(Recorder).where(Recorder.source_id == source_id)):
        if recorder.schedule_days is None:
            return recorder
    return None


def _range_warning(db, source: Source, target: Target) -> str | None:
    antenna = db.get(Antenna, source.antenna_id) if source.antenna_id else None
    if antenna_covers(antenna, target.frequency_hz):
        return None
    return f"{target.name} is outside every range of {antenna.name}, the antenna on {source.name}, so reception may be poor."


def _other_sdr_warning(db, source: Source, target: Target) -> str | None:
    """Two SDRs on one channel can be deliberate, so this warns. Uploads are guarded separately."""
    recorders = db.scalars(select(Recorder).where(Recorder.source_id != source.id, Recorder.enabled.is_(True))).all()
    names = []
    for recorder in recorders:
        other = db.get(Target, recorder.target_id)
        if other and other.kind == "audio" and (other.frequency_hz, other.modulation) == (target.frequency_hz, target.modulation):
            other_source = db.get(Source, recorder.source_id)
            if other_source and other_source.name not in names:
                names.append(other_source.name)
    if not names or target.kind != "audio":
        return None
    return f"{', '.join(names)} can also record this channel. If both run together, only the first copy of each call is uploaded."


def _stick_warning(db, source: Source, target: Target) -> str | None:
    profile = db.scalar(select(SdrProfile).where(SdrProfile.source_id == source.id, SdrProfile.target_id == target.id))
    gain = profile.gain_db if profile else source.gain_db
    found = capabilities.problems(source, target.frequency_hz, target.kind, target.modulation, gain)
    if not found:
        return None
    return f"{target.name} {' and '.join(found)} on {source.name}. It will still be tried."


def _warnings(db, source: Source, target: Target) -> str | None:
    found = [w for w in (_stick_warning(db, source, target), _range_warning(db, source, target),
                         _other_sdr_warning(db, source, target)) if w]
    return " ".join(found) or None


class AssignBody(BaseModel):
    target_id: int | None = None


@router.post("/sources/{source_id}/assign")
def assign(source_id: int, body: AssignBody, request: Request, user: User = Depends(require("assign")), db=Depends(get_db)):
    """Set or clear a source's base target. A lock blocks this without its password."""
    ctx = ctx_of(request)
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "Source not found")
    base = _base_for(db, source_id)
    _check_lock(ctx, request, user, base)
    warning = None
    if body.target_id is None:
        if base:
            db.delete(base)
        target_name = None
    else:
        target = db.get(Target, body.target_id)
        if target is None:
            raise HTTPException(404, "Target not found")
        warning = _warnings(db, source, target)
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


@router.post("/sources/{source_id}/lock")
def lock_source(source_id: int, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    source = db.get(Source, source_id)
    base = _base_for(db, source_id)
    if source is None or base is None:
        raise HTTPException(409, "Assign a target before locking this source")
    base.locked = True
    db.commit()
    ctx.audit("source.locked", user=admin, ip=ip_of(request), target=source.name)
    return {"detail": "Locked"}


@router.delete("/sources/{source_id}/lock")
def unlock_source(source_id: int, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    base = _base_for(db, source_id)
    if base is not None:
        base.locked = False
        db.commit()
    ctx.audit("source.unlocked", user=admin, ip=ip_of(request), target=str(source_id))
    return {"detail": "Unlocked"}


# --- noise measurement and saved profiles -----------------------------------
def _source_and_audio_target(db, source_id: int, target_id: int) -> tuple[Source, Target]:
    source, target = db.get(Source, source_id), db.get(Target, target_id)
    if source is None or target is None:
        raise HTTPException(404, "Source or target not found")
    if target.kind != "audio":
        raise HTTPException(422, "Only audio targets use squelch")
    return source, target


def _refuse_if_locked(db, user: User, source_id: int) -> None:
    """Measuring interrupts recording and a profile changes it, so a lock must cover both."""
    locked = db.scalar(select(Recorder.id).where(Recorder.source_id == source_id, Recorder.locked.is_(True)))
    if locked and user.role != "admin":
        raise HTTPException(403, "A locked recorder uses this source. Ask an administrator.")


class MeasureBody(BaseModel):
    target_id: int
    gain_db: float | None = Field(default=None, ge=0, le=60)


@router.post("/sources/{source_id}/measure")
async def measure_source(source_id: int, body: MeasureBody, request: Request, user: User = Depends(require("assign")), db=Depends(get_db)):
    """Read the noise on a target's frequency. Pauses this SDR's recording for a few seconds."""
    ctx = ctx_of(request)
    source, target = _source_and_audio_target(db, source_id, body.target_id)
    _refuse_if_locked(db, user, source_id)
    node = db.get(Node, source.node_id)
    if node is None or not ctx.node_online(node):
        raise HTTPException(409, "That node is offline.")
    key = f"measure:{user.id}"
    if ctx.limiter.blocked(key, MEASURE_LIMIT, MEASURE_WINDOW):
        raise HTTPException(429, "Too many measurements. Try again later.")
    ctx.limiter.hit(key, MEASURE_WINDOW)
    measure_request = {
        "frequency_hz": target.frequency_hz, "modulation": target.modulation,
        "gain_db": body.gain_db, "ppm": source.ppm,
    }
    names = {"target": source.name, "details": {"target": target.name, "gain_db": body.gain_db}}
    db.commit()  # ends the read transaction: the reading takes seconds
    ctx.audit("source.measured", user=user, ip=ip_of(request), **names)
    try:
        result = await ctx.measure(node, source.serial, measure_request)
    except MeasureError as exc:
        raise HTTPException(409, str(exc)) from None
    return {**result, "gain_db": body.gain_db}


class DetectBody(BaseModel):
    node_id: int | None = None
    serial: str = Field(min_length=1, max_length=64)


@router.post("/sources/detect")
async def detect_stick(body: DetectBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    """Read a stick's tuner and gain steps. Pauses its recording for a few seconds. Saves nothing."""
    ctx = ctx_of(request)
    node = db.get(Node, body.node_id or ctx.local_node_id)
    if node is None or node.status != "active" or body.serial not in (node.serials or []):
        raise HTTPException(404, "That SDR isn't configured on that node")
    if not ctx.node_online(node):
        raise HTTPException(409, "That node is offline.")
    key = f"measure:{admin.id}"
    if ctx.limiter.blocked(key, MEASURE_LIMIT, MEASURE_WINDOW):
        raise HTTPException(429, "Too many measurements. Try again later.")
    ctx.limiter.hit(key, MEASURE_WINDOW)
    db.commit()  # ends the read transaction: detection takes seconds
    ctx.audit("source.detected", user=admin, ip=ip_of(request), target=body.serial)
    try:
        return await ctx.measure(node, body.serial, {"op": "detect"})
    except MeasureError as exc:
        raise HTTPException(409, str(exc)) from None


class ProfileBody(BaseModel):
    squelch: int = Field(ge=0, le=100000)
    gain_db: float | None = Field(default=None, ge=0, le=60)
    noise_level: float | None = Field(default=None, ge=0, le=100000)


@router.put("/sources/{source_id}/profiles/{target_id}")
def save_profile(source_id: int, target_id: int, body: ProfileBody, request: Request, user: User = Depends(require("assign")), db=Depends(get_db)):
    ctx = ctx_of(request)
    source, target = _source_and_audio_target(db, source_id, target_id)
    _refuse_if_locked(db, user, source_id)
    profile = db.scalar(select(SdrProfile).where(SdrProfile.source_id == source_id, SdrProfile.target_id == target_id))
    _refuse_off_step(source.gain_steps, body.gain_db, profile.gain_db if profile else None)
    if profile is None:
        profile = SdrProfile(source_id=source_id, target_id=target_id)
        db.add(profile)
    profile.squelch, profile.gain_db, profile.noise_level = body.squelch, body.gain_db, body.noise_level
    profile.measured_at = time.time() if body.noise_level is not None else None
    profile.updated_by = user.email
    db.commit()
    ctx.audit("profile.saved", user=user, ip=ip_of(request), target=source.name,
              details={"target": target.name, "squelch": body.squelch, "gain_db": body.gain_db})
    ctx.kick()
    nearest = capabilities.nearest_step(source.gain_steps, body.gain_db)
    warning = f"{body.gain_db:g} dB isn't one of this stick's gain steps, so it will use {nearest:g} dB." if nearest is not None else None
    return {**_profile_view(profile), "warning": warning}


@router.delete("/sources/{source_id}/profiles/{target_id}")
def delete_profile(source_id: int, target_id: int, request: Request, user: User = Depends(require("assign")), db=Depends(get_db)):
    ctx = ctx_of(request)
    _refuse_if_locked(db, user, source_id)
    profile = db.scalar(select(SdrProfile).where(SdrProfile.source_id == source_id, SdrProfile.target_id == target_id))
    if profile is None:
        raise HTTPException(404, "Profile not found")
    db.delete(profile)
    db.commit()
    ctx.audit("profile.deleted", user=user, ip=ip_of(request), target=str(source_id), details={"target_id": target_id})
    ctx.kick()
    return {"detail": "Deleted"}


# --- scheduled recorders ---------------------------------------------------
class RecorderBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source_id: int
    target_id: int
    enabled: bool = True
    schedule_days: list[int] = Field(min_length=1, max_length=7)
    schedule_start: str
    schedule_end: str


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
def list_recorders(user: User = Depends(current_user), db=Depends(get_db)):
    views = [_recorder_view(db, r) for r in db.scalars(select(Recorder).order_by(Recorder.id))]
    if "assign" not in effective_permissions(user):
        for view in views:
            view["assigned_by"] = ""  # other people's email addresses
    return views


def _save_recorder(ctx, request, user, db, body: RecorderBody, recorder: Recorder | None):
    _validate_schedule(body)
    source, target = db.get(Source, body.source_id), db.get(Target, body.target_id)
    if source is None or target is None:
        raise HTTPException(404, "Source or target not found")
    # A schedule overrides the base assignment, so it must honour that lock too.
    _check_lock(ctx, request, user, _base_for(db, body.source_id))
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
    return {**_recorder_view(db, recorder), "warning": _warnings(db, source, target)}


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
    _check_lock(ctx, request, user, _base_for(db, recorder.source_id))
    return _save_recorder(ctx, request, user, db, body, recorder)


@router.delete("/recorders/{recorder_id}")
def delete_recorder(recorder_id: int, request: Request,
                    user: User = Depends(require("assign")), db=Depends(get_db)):
    ctx = ctx_of(request)
    recorder = _scheduled_recorder(db, recorder_id)
    _check_lock(ctx, request, user, _base_for(db, recorder.source_id))
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
                    yield await aio.wait_for(queue.get(), 15)
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
def map_tracks(kind: str, request: Request, _user: User = Depends(require("maps")), db=Depends(get_db)):
    if kind not in ("adsb", "ais"):
        raise HTTPException(404, "Unknown map")
    items = ctx_of(request).current_tracks(kind)
    if kind == "adsb":
        items = annotate_aircraft(request, db, items)
    return {"items": items}


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
