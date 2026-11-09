"""Health warnings: the banner for admins and operators, and each admin's mutes."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from ..models import User, WarningMute
from .deps import admin_user, ctx_of, current_user, get_db, ip_of

router = APIRouter(prefix="/api")

MAX_MUTE_HOURS = 24 * 365


def _mutes(db, user: User) -> dict[str, WarningMute]:
    """This user's mutes still in force. Ones that have run out are removed."""
    db.execute(delete(WarningMute).where(WarningMute.user_id == user.id, WarningMute.until.is_not(None),
                                         WarningMute.until <= time.time()))
    db.commit()
    return {m.key: m for m in db.scalars(select(WarningMute).where(WarningMute.user_id == user.id))}


@router.get("/warnings")
def banner_warnings(request: Request, user: User = Depends(current_user), db=Depends(get_db)):
    """What the banner shows: not set quiet, and not muted by this user. Others see an empty list."""
    if user.role not in ("admin", "operator"):
        return []
    mutes = _mutes(db, user) if user.role == "admin" else {}
    return [w for w in ctx_of(request).health_warnings() if not w["quiet"] and w["key"] not in mutes]


@router.get("/admin/warnings")
def all_warnings(request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    """Every current warning, muted or quiet ones too, and this admin's mutes for warnings that have cleared."""
    mutes = _mutes(db, admin)
    current = ctx_of(request).health_warnings()
    for warning in current:
        mute = mutes.get(warning["key"])
        warning["muted"] = mute is not None
        warning["muted_until"] = mute.until if mute else None
    keys = {w["key"] for w in current}
    cleared = [{"key": m.key, "text": m.text, "muted_until": m.until} for m in mutes.values() if m.key not in keys]
    return {"current": current, "cleared": sorted(cleared, key=lambda m: m["text"])}


class MuteBody(BaseModel):
    key: str = Field(min_length=1, max_length=120)
    hours: int | None = Field(default=None, ge=1, le=MAX_MUTE_HOURS)  # None mutes until unmuted


@router.post("/admin/warnings/mute")
def mute(body: MuteBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    warning = next((w for w in ctx.health_warnings() if w["key"] == body.key), None)
    if warning is None:
        raise HTTPException(404, "That warning has cleared")
    until = time.time() + body.hours * 3600 if body.hours else None
    existing = db.scalar(select(WarningMute).where(WarningMute.user_id == admin.id, WarningMute.key == body.key))
    if existing is None:
        db.add(WarningMute(user_id=admin.id, key=body.key, text=warning["text"][:500], until=until))
    else:
        existing.text, existing.until = warning["text"][:500], until
    db.commit()
    ctx.audit("warning.muted", user=admin, ip=ip_of(request), target=body.key, details={"hours": body.hours})
    return {"key": body.key, "muted_until": until}


class UnmuteBody(BaseModel):
    key: str = Field(min_length=1, max_length=120)


@router.post("/admin/warnings/unmute")
def unmute(body: UnmuteBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    db.execute(delete(WarningMute).where(WarningMute.user_id == admin.id, WarningMute.key == body.key))
    db.commit()
    ctx_of(request).audit("warning.unmuted", user=admin, ip=ip_of(request), target=body.key)
    return {"detail": "Unmuted"}
