"""Administrator routes: users, bans, audit log, settings."""
from __future__ import annotations

import ipaddress
import os
import secrets as pysecrets
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from .. import acars, health, security, uploaders
from ..context import SECRET_SETTINGS, SETTING_DEFAULTS
from ..models import AuditLog, Ban, BroadcastifySystem, Target, User, UserSession
from .deps import ALL_PERMISSIONS, admin_user, ctx_of, effective_permissions, get_db, ip_of
from .routes_account import check_new_password, confirm_password, normalize_email, revoke_sessions

router = APIRouter(prefix="/api/admin", dependencies=[Depends(admin_user)])


def admin_view(user: User) -> dict:
    return {
        "id": user.id, "email": user.email, "name": user.name, "status": user.status, "role": user.role,
        "perm_overrides": user.perm_overrides or {}, "permissions": sorted(effective_permissions(user)),
        "totp_enabled": user.totp_enabled, "must_change_password": user.must_change_password,
        "created_at": user.created_at, "last_login_at": user.last_login_at,
        "last_ip": user.last_ip, "signup_ip": user.signup_ip,
    }


def _get_user(db, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    return user


def _active_admins(db) -> int:
    return len(list(db.scalars(select(User.id).where(User.role == "admin", User.status == "active"))))


@router.get("/users")
def list_users(status: str | None = None, db=Depends(get_db)):
    query = select(User).order_by(User.created_at.desc())
    if status:
        query = query.where(User.status == status)
    return [admin_view(u) for u in db.scalars(query)]


class UserPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    role: str | None = None
    status: str | None = None
    perm_overrides: dict[str, bool] | None = None


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserPatch, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    user = _get_user(db, user_id)
    changes = body.model_dump(exclude_none=True)
    if body.role is not None and body.role not in ("admin", "operator", "listener"):
        raise HTTPException(422, "Unknown role")
    if body.status is not None and body.status not in ("active", "denied", "disabled"):
        raise HTTPException(422, "Unknown status")
    if body.perm_overrides is not None and set(body.perm_overrides) - set(ALL_PERMISSIONS):
        raise HTTPException(422, "Unknown permission")
    removes_admin = user.role == "admin" and user.status == "active" and (
        (body.role not in (None, "admin")) or (body.status not in (None, "active"))
    )
    if removes_admin and _active_admins(db) <= 1:
        raise HTTPException(409, "This is the last active administrator")
    if body.name is not None:
        user.name = body.name.strip()
    if body.email is not None:
        email = normalize_email(body.email)
        if email != user.email and db.scalar(select(User).where(User.email == email)):
            raise HTTPException(409, "Another account already uses that email")
        user.email = email
    if body.role is not None:
        user.role = body.role
    if body.perm_overrides is not None:
        user.perm_overrides = body.perm_overrides
    if body.status is not None:
        user.status = body.status
        if body.status != "active":
            revoke_sessions(db, user.id)
    db.commit()
    ctx.audit("admin.user_updated", user=admin, ip=ip_of(request), target=user.email, details=changes)
    return admin_view(user)


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    """Issue a one-time temporary password, shown once, that must then be changed."""
    ctx = ctx_of(request)
    user = _get_user(db, user_id)
    temporary = pysecrets.token_urlsafe(15)
    check_new_password(temporary)
    user.password_hash = security.hash_password(temporary)
    user.must_change_password = True
    revoke_sessions(db, user.id)
    db.commit()
    ctx.audit("admin.password_reset", user=admin, ip=ip_of(request), target=user.email)
    return {"temporary_password": temporary}


@router.post("/users/{user_id}/reset-totp")
def reset_totp(user_id: int, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    user = _get_user(db, user_id)
    user.totp_enabled, user.totp_secret_enc, user.totp_last_step = False, None, 0
    revoke_sessions(db, user.id)
    db.commit()
    ctx.audit("admin.totp_reset", user=admin, ip=ip_of(request), target=user.email)
    return admin_view(user)


@router.get("/users/{user_id}/sessions")
def user_sessions(user_id: int, db=Depends(get_db)):
    rows = db.scalars(select(UserSession).where(UserSession.user_id == user_id).order_by(UserSession.created_at.desc()))
    return [{"created_at": s.created_at, "expires_at": s.expires_at, "ip": s.ip, "user_agent": s.user_agent} for s in rows]


@router.get("/audit")
def audit_log(
    user_id: int | None = None, search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=200, le=1000), before: float | None = None, db=Depends(get_db),
):
    query = select(AuditLog).order_by(AuditLog.ts.desc()).limit(limit)
    if user_id is not None:
        query = query.where(AuditLog.user_id == user_id)
    if before is not None:
        query = query.where(AuditLog.ts < before)
    if search:
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(or_(
            AuditLog.action.like(pattern, escape="\\"), AuditLog.email.like(pattern, escape="\\"),
            AuditLog.ip.like(pattern, escape="\\"), AuditLog.target.like(pattern, escape="\\"),
        ))
    return [
        {"id": r.id, "ts": r.ts, "user_id": r.user_id, "email": r.email, "ip": r.ip,
         "action": r.action, "target": r.target, "details": r.details}
        for r in db.scalars(query)
    ]


class BanBody(BaseModel):
    kind: str
    value: str = Field(min_length=1, max_length=254)
    reason: str = Field(default="", max_length=255)


@router.get("/bans")
def list_bans(db=Depends(get_db)):
    return [
        {"id": b.id, "kind": b.kind, "value": b.value, "reason": b.reason, "created_by": b.created_by, "created_at": b.created_at}
        for b in db.scalars(select(Ban).order_by(Ban.created_at.desc()))
    ]


@router.post("/bans", status_code=201)
def add_ban(body: BanBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    my_ip = ip_of(request)
    if body.kind == "email":
        value = normalize_email(body.value)
        if value == admin.email:
            raise HTTPException(409, "You can't ban your own account")
    elif body.kind == "ip":
        try:
            network = ipaddress.ip_network(body.value.strip(), strict=False)
        except ValueError as exc:
            raise HTTPException(422, "Enter a valid IP address or CIDR range") from exc
        value = str(network)
        try:
            if ipaddress.ip_address(my_ip) in network:
                raise HTTPException(409, "That would ban your own address")
        except ValueError:
            pass
    else:
        raise HTTPException(422, "Ban kind must be email or ip")
    db.add(Ban(kind=body.kind, value=value, reason=body.reason, created_by=admin.email))
    if body.kind == "email":
        banned = db.scalar(select(User).where(User.email == value))
        if banned:
            revoke_sessions(db, banned.id)
    db.commit()
    ctx.bans_cache = (0.0, [])
    ctx.audit("admin.ban_added", user=admin, ip=my_ip, target=value, details={"kind": body.kind, "reason": body.reason})
    return {"detail": "Banned"}


@router.delete("/bans/{ban_id}")
def remove_ban(ban_id: int, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    ban = db.get(Ban, ban_id)
    if ban is None:
        raise HTTPException(404, "Ban not found")
    value = ban.value
    db.delete(ban)
    db.commit()
    ctx.bans_cache = (0.0, [])
    ctx.audit("admin.ban_removed", user=admin, ip=ip_of(request), target=value)
    return {"detail": "Removed"}


# --- Broadcastify Calls nodes -------------------------------------------------
def _system_view(system: BroadcastifySystem) -> dict:
    return {"id": system.id, "name": system.name, "system_id": system.system_id, "key_set": bool(system.api_key_sealed)}


class BroadcastifyBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    system_id: int = Field(ge=1)
    api_key: str | None = Field(default=None, max_length=500)  # leave out to keep the saved key


@router.get("/broadcastify")
def list_broadcastify(db=Depends(get_db)):
    return [_system_view(s) for s in db.scalars(select(BroadcastifySystem).order_by(BroadcastifySystem.name))]


@router.post("/broadcastify", status_code=201)
def create_broadcastify(body: BroadcastifyBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    if not body.api_key:
        raise HTTPException(422, "An API key is required")
    system = BroadcastifySystem(name=body.name, system_id=body.system_id, api_key_sealed=ctx.secrets.seal(body.api_key))
    db.add(system)
    db.commit()
    ctx.audit("broadcastify.created", user=admin, ip=ip_of(request), target=system.name)
    return _system_view(system)


@router.put("/broadcastify/{system_ref}")
def update_broadcastify(system_ref: int, body: BroadcastifyBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    system = db.get(BroadcastifySystem, system_ref)
    if system is None:
        raise HTTPException(404, "System not found")
    system.name, system.system_id = body.name, body.system_id
    if body.api_key:
        system.api_key_sealed = ctx.secrets.seal(body.api_key)
    db.commit()
    ctx.audit("broadcastify.updated", user=admin, ip=ip_of(request), target=system.name,
              details={"key_changed": bool(body.api_key)})
    return _system_view(system)


@router.delete("/broadcastify/{system_ref}")
def delete_broadcastify(system_ref: int, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    system = db.get(BroadcastifySystem, system_ref)
    if system is None:
        raise HTTPException(404, "System not found")
    if db.scalar(select(Target.id).where(Target.broadcastify_system_ref == system_ref, Target.broadcastify_enabled.is_(True))):
        raise HTTPException(409, "A target still uploads to this system. Change that target first.")
    name = system.name
    db.delete(system)
    db.commit()
    ctx.audit("broadcastify.deleted", user=admin, ip=ip_of(request), target=name)
    return {"detail": "Deleted"}


@router.post("/broadcastify/{system_ref}/test")
async def test_broadcastify(system_ref: int, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    """Check saved credentials with Broadcastify. Sends no call."""
    ctx = ctx_of(request)
    system = db.get(BroadcastifySystem, system_ref)
    if system is None:
        raise HTTPException(404, "System not found")
    server, key, number, name = ctx.get_setting("broadcastify_server"), ctx.secrets.open(system.api_key_sealed), system.system_id, system.name
    db.commit()
    if not server or not key:
        raise HTTPException(409, "The Broadcastify server or this system's key isn't set")
    ctx.audit("broadcastify.tested", user=admin, ip=ip_of(request), target=name)
    try:
        async with httpx.AsyncClient(transport=ctx.http_transport, timeout=uploaders.UPLOAD_TIMEOUT, follow_redirects=False) as client:
            return {"reply": await uploaders.broadcastify_test(client, server, number, key)}
    except (uploaders.UploadError, httpx.HTTPError) as exc:
        raise HTTPException(502, str(exc)[:200] or "Couldn't reach Broadcastify") from None


@router.get("/settings")
def get_settings(request: Request):
    ctx = ctx_of(request)
    values = {key: ctx.get_setting(key) for key in SETTING_DEFAULTS}
    # Stored API keys are write-only: the interface learns only whether one is set.
    values.update({f"{key}_set": bool(ctx.get_setting(key)) for key in SECRET_SETTINGS})
    values["recordings_dir"] = str(ctx.cfg.recordings_dir)
    values["log_dir"] = str(ctx.cfg.log_dir)
    values["recordings_bytes"] = ctx.recordings_size()
    values["disk"] = health.disk(ctx.cfg.recordings_dir)
    return values


class SettingsBody(BaseModel):
    audit_retention_days: int | None = Field(default=None, ge=0, le=36500)
    recording_retention_days: int | None = Field(default=None, ge=0, le=36500)
    recording_retention_gb: float | None = Field(default=None, ge=0, le=1_000_000)
    registration_open: bool | None = None
    rdio_server: str | None = Field(default=None, max_length=500)
    rdio_api_key: str | None = Field(default=None, max_length=500)
    clearcut_server: str | None = Field(default=None, max_length=500)
    clearcut_api_key: str | None = Field(default=None, max_length=500)
    clearcut_page_url: str | None = Field(default=None, max_length=500)
    broadcastify_server: str | None = Field(default=None, max_length=500)
    map_tile_url: str | None = Field(default=None, max_length=500)
    message_retention_days: int | None = Field(default=None, ge=0, le=36500)
    airframes_station_id: str | None = Field(default=None, max_length=40)
    airframes_feed: str | None = Field(default=None, max_length=260)


def _check_url(value: str) -> None:
    if value and (urlsplit(value).scheme not in ("http", "https") or not urlsplit(value).hostname):
        raise HTTPException(422, f"Not a valid http(s) URL: {value}")


@router.patch("/settings")
def update_settings(body: SettingsBody, request: Request, admin: User = Depends(admin_user)):
    ctx = ctx_of(request)
    changed = []
    if body.airframes_station_id and not acars.STATION_RE.match(body.airframes_station_id):
        raise HTTPException(422, "A station ID is letters, numbers and dashes, like KE-KSEA-ACARS")
    if body.airframes_feed is not None and not acars.FEED_RE.match(body.airframes_feed):
        raise HTTPException(422, "The airframes.io address is host:port, like feed.acars.io:5550")
    for key, value in body.model_dump(exclude_none=True).items():
        if key in SECRET_SETTINGS:
            ctx.set_setting(key, ctx.secrets.seal(value) if value else "")
        elif isinstance(value, bool):
            ctx.set_setting(key, "true" if value else "false")
        else:
            if key.endswith(("_server", "_url")):
                _check_url(str(value).replace("{s}", "a"))
            ctx.set_setting(key, str(value))
        changed.append(key)
    ctx.audit("admin.settings_updated", user=admin, ip=ip_of(request), details={"keys": changed})
    return get_settings(request)


# --- backup ----------------------------------------------------------------
class BackupBody(BaseModel):
    password: str = Field(max_length=security.MAX_PASSWORD_LENGTH)


@router.post("/backup")
def backup(body: BackupBody, request: Request, background: BackgroundTasks, admin: User = Depends(admin_user)):
    """The database and the key that decrypts its secrets, as one file. Recordings aren't included.

    It holds every account, setting and API key, so it needs the admin's password again.
    """
    ctx = ctx_of(request)
    confirm_password(ctx, admin, body.password, "Password is incorrect")
    folder = Path(tempfile.mkdtemp(prefix="backup-", dir=ctx.cfg.data_dir))
    background.add_task(shutil.rmtree, folder, True)
    copy = folder / "sentrysdr.db"
    source = sqlite3.connect(ctx.cfg.data_dir / "sentrysdr.db")
    try:
        target = sqlite3.connect(copy)
        with target:
            source.backup(target)  # consistent while the service keeps writing
        target.close()
    finally:
        source.close()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = folder / f"sentrysdr-backup-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(copy, arcname="sentrysdr.db")
        key = ctx.cfg.data_dir / "secret.key"
        if key.is_file():
            tar.add(key, arcname="secret.key")
    os.chmod(archive, 0o600)
    ctx.audit("admin.backup_downloaded", user=admin, ip=ip_of(request))
    return FileResponse(archive, media_type="application/gzip", filename=archive.name,
                        headers={"Cache-Control": "no-store"})
