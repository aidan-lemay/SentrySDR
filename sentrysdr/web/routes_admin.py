"""Administrator routes: users, bans, audit log, settings."""
from __future__ import annotations

import ipaddress
import secrets as pysecrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from .. import security
from ..context import SECRET_SETTINGS, SETTING_DEFAULTS
from ..models import AuditLog, Ban, User, UserSession
from .deps import ALL_PERMISSIONS, admin_user, ctx_of, effective_permissions, get_db, ip_of
from .routes_account import check_new_password, normalize_email, revoke_sessions

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


@router.get("/settings")
def get_settings(request: Request):
    ctx = ctx_of(request)
    values = {key: ctx.get_setting(key) for key in SETTING_DEFAULTS}
    # Stored API keys are write-only: the interface learns only whether one is set.
    values.update({f"{key}_set": bool(ctx.get_setting(key)) for key in SECRET_SETTINGS})
    values["recordings_dir"] = str(ctx.cfg.recordings_dir)
    values["log_dir"] = str(ctx.cfg.log_dir)
    return values


class SettingsBody(BaseModel):
    audit_retention_days: int | None = Field(default=None, ge=0, le=36500)
    recording_retention_days: int | None = Field(default=None, ge=0, le=36500)
    registration_open: bool | None = None
    rdio_server: str | None = Field(default=None, max_length=500)
    rdio_api_key: str | None = Field(default=None, max_length=500)
    clearcut_server: str | None = Field(default=None, max_length=500)
    clearcut_api_key: str | None = Field(default=None, max_length=500)
    clearcut_page_url: str | None = Field(default=None, max_length=500)
    map_tile_url: str | None = Field(default=None, max_length=500)


def _check_url(value: str) -> None:
    if value and (urlsplit(value).scheme not in ("http", "https") or not urlsplit(value).hostname):
        raise HTTPException(422, f"Not a valid http(s) URL: {value}")


@router.patch("/settings")
def update_settings(body: SettingsBody, request: Request, admin: User = Depends(admin_user)):
    ctx = ctx_of(request)
    changed = []
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
