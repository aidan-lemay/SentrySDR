"""Administrator routes: users, bans, audit log, settings."""
from __future__ import annotations

import ipaddress
import re
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

from .. import acars, health, notify, security, uploaders
from ..context import SECRET_SETTINGS, SETTING_DEFAULTS
from ..models import AuditLog, Ban, UploadSystem, Target, User, UserSession
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


# --- upload systems: Broadcastify Calls nodes, ClearCut and RDIO Scanner systems ------
SERVICE_NAMES = {"broadcastify": "Broadcastify", "clearcut": "ClearCut", "rdio": "RDIO"}
CLEARCUT_SYSTEM_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Which target fields point at a system, for each service.
SYSTEM_REF_FIELDS = {"broadcastify": ("broadcastify_enabled", "broadcastify_system_ref"),
                     "clearcut": ("clearcut_enabled", "clearcut_system_ref"),
                     "rdio": ("rdio_enabled", "rdio_system_ref")}


def system_view(system: UploadSystem) -> dict:
    return {"id": system.id, "service": system.kind, "name": system.name, "system_id": system.label,
            "key_set": bool(system.api_key_sealed)}


class UploadSystemBody(BaseModel):
    service: str = Field(default="broadcastify", pattern=r"^(broadcastify|clearcut|rdio)$")
    name: str = Field(min_length=1, max_length=120)
    system_id: str | int  # a number, or ClearCut's text ID; checked by _system_number
    api_key: str | None = Field(default=None, max_length=500)  # leave out to keep the saved key


def _system_number(service: str, value) -> tuple[int, str | None]:
    """The system ID as stored: (system_id, code). ClearCut's is text, the others' numbers."""
    text_value = str(value).strip()[:65]  # longer than any ID, so too long is still refused
    if service == "clearcut":
        if not CLEARCUT_SYSTEM_RE.match(text_value):
            raise HTTPException(422, "A ClearCut system ID is letters, numbers, dots, dashes and underscores, like us-ny-monroe")
        return 0, text_value
    if not text_value.isascii() or not text_value.isdigit() or int(text_value) < 1 or int(text_value) > 2**31 - 1:
        raise HTTPException(422, f"A {SERVICE_NAMES[service]} system ID is a whole number")
    return int(text_value), None


def _refuse_duplicate(db, service: str, number: int, code: str | None, system_ref: int | None = None) -> None:
    same = db.scalar(select(UploadSystem).where(
        UploadSystem.service == service, UploadSystem.system_id == number,
        UploadSystem.code.is_(None) if code is None else UploadSystem.code == code,
        UploadSystem.id != (system_ref or 0)))
    if same:
        raise HTTPException(409, f"{same.name} is already that {SERVICE_NAMES[service]} system")


@router.get("/upload-systems")
def list_upload_systems(db=Depends(get_db)):
    return [system_view(s) for s in db.scalars(select(UploadSystem).order_by(UploadSystem.service, UploadSystem.name))]


@router.post("/upload-systems", status_code=201)
def create_upload_system(body: UploadSystemBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    number, code = _system_number(body.service, body.system_id)
    if not body.api_key:
        raise HTTPException(422, "An API key is required")
    _refuse_duplicate(db, body.service, number, code)
    system = UploadSystem(service=body.service, name=body.name, system_id=number, code=code,
                          api_key_sealed=ctx.secrets.seal(body.api_key))
    db.add(system)
    db.commit()
    ctx.audit("upload_system.created", user=admin, ip=ip_of(request), target=system.name, details={"service": body.service})
    return system_view(system)


@router.put("/upload-systems/{system_ref}")
def update_upload_system(system_ref: int, body: UploadSystemBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    system = db.get(UploadSystem, system_ref)
    if system is None:
        raise HTTPException(404, "System not found")
    if body.service != system.kind:
        raise HTTPException(422, "A system can't move to another service. Add a new one instead.")
    number, code = _system_number(system.kind, body.system_id)
    _refuse_duplicate(db, system.kind, number, code, system_ref)
    system.name, system.system_id, system.code = body.name, number, code
    if body.api_key:
        system.api_key_sealed = ctx.secrets.seal(body.api_key)
    db.commit()
    ctx.audit("upload_system.updated", user=admin, ip=ip_of(request), target=system.name,
              details={"service": system.kind, "key_changed": bool(body.api_key)})
    return system_view(system)


@router.delete("/upload-systems/{system_ref}")
def delete_upload_system(system_ref: int, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    system = db.get(UploadSystem, system_ref)
    if system is None:
        raise HTTPException(404, "System not found")
    enabled, ref = SYSTEM_REF_FIELDS[system.kind]
    user = db.scalar(select(Target.name).where(getattr(Target, ref) == system_ref, getattr(Target, enabled).is_(True)))
    if user:
        raise HTTPException(409, f"{user} still uploads to this system. Change that target first.")
    name, service = system.name, system.kind
    db.delete(system)
    db.commit()
    ctx.audit("upload_system.deleted", user=admin, ip=ip_of(request), target=name, details={"service": service})
    return {"detail": "Deleted"}


@router.post("/upload-systems/{system_ref}/test")
async def test_upload_system(system_ref: int, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    """Check saved credentials with Broadcastify. Sends no call. The others have no such check."""
    ctx = ctx_of(request)
    system = db.get(UploadSystem, system_ref)
    if system is None:
        raise HTTPException(404, "System not found")
    if system.kind != "broadcastify":
        raise HTTPException(409, f"{SERVICE_NAMES[system.kind]} has no way to check credentials without sending a call")
    server, number, name = ctx.get_setting("broadcastify_server"), system.system_id, system.name
    key = ctx.secrets.open(system.api_key_sealed) if system.api_key_sealed else ""
    db.commit()
    if not server or not key:
        raise HTTPException(409, "The Broadcastify server or this system's key isn't set")
    ctx.audit("upload_system.tested", user=admin, ip=ip_of(request), target=name, details={"service": "broadcastify"})
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
    clearcut_server: str | None = Field(default=None, max_length=500)
    clearcut_page_url: str | None = Field(default=None, max_length=500)
    broadcastify_server: str | None = Field(default=None, max_length=500)
    map_tile_url: str | None = Field(default=None, max_length=500)
    message_retention_days: int | None = Field(default=None, ge=0, le=36500)
    airframes_station_id: str | None = Field(default=None, max_length=40)
    airframes_feed: str | None = Field(default=None, max_length=260)
    email_alerts_enabled: bool | None = None
    email_daily_cap: int | None = Field(default=None, ge=1, le=1000)
    smtp_host: str | None = Field(default=None, max_length=253, pattern=r"^[A-Za-z0-9.\-:\[\]]*$")
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_security: str | None = Field(default=None, pattern=r"^(starttls|tls|none)$")
    smtp_username: str | None = Field(default=None, max_length=254)
    smtp_password: str | None = Field(default=None, max_length=500)
    smtp_from: str | None = Field(default=None, max_length=254)


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
    if body.smtp_from and not re.fullmatch(r"[^\r\n<>@]*<?[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+>?", body.smtp_from.strip()):
        raise HTTPException(422, "The From address should look like alerts@example.com, or SentrySDR <alerts@example.com>")
    security_choice = body.smtp_security or ctx.get_setting("smtp_security")
    host = body.smtp_host if body.smtp_host is not None else ctx.get_setting("smtp_host")
    if security_choice == "none" and host and not notify.is_loopback(host.strip()):
        raise HTTPException(422, "Without TLS, the mail server must be on this machine (localhost), or the password would cross the network unencrypted")
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
