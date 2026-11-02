"""Request helpers: database session, current user, permissions, client address."""
from __future__ import annotations

import ipaddress
import time

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from ..context import Context
from ..models import Ban, User, UserSession
from ..security import client_ip, hash_token, is_direct_loopback

COOKIE_NAME = "sentrysdr_session"
SESSION_SECONDS = 14 * 86400

ROLE_PERMISSIONS = {
    "admin": {"listen_live", "listen_recordings", "download", "assign", "manage_targets", "protect", "maps", "transcribe"},
    "operator": {"listen_live", "listen_recordings", "download", "assign", "manage_targets", "protect", "maps", "transcribe"},
    "listener": {"listen_live", "listen_recordings", "download", "maps"},
}
ALL_PERMISSIONS = sorted(ROLE_PERMISSIONS["admin"])


def effective_permissions(user: User) -> set[str]:
    perms = set(ROLE_PERMISSIONS.get(user.role, set()))
    if user.role == "admin":
        return perms  # an admin cannot be locked out by a stale override
    for name, allowed in (user.perm_overrides or {}).items():
        if name in ALL_PERMISSIONS:
            (perms.add if allowed else perms.discard)(name)
    return perms


def ctx_of(request: Request) -> Context:
    return request.app.state.ctx


def get_db(request: Request):
    with ctx_of(request).db() as session:
        yield session


def ip_of(request: Request) -> str:
    ctx = ctx_of(request)
    return client_ip(ctx.cfg, request.client.host if request.client else None, request.headers)


def load_bans(ctx: Context) -> list[Ban]:
    expires, bans = ctx.bans_cache
    if time.monotonic() < expires:
        return bans
    with ctx.db() as session:
        bans = list(session.scalars(select(Ban)))
    ctx.bans_cache = (time.monotonic() + 5, bans)
    return bans


def ip_banned(ctx: Context, ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for ban in load_bans(ctx):
        if ban.kind != "ip":
            continue
        try:
            network = ipaddress.ip_network(ban.value, strict=False)
        except ValueError:
            continue
        if addr.version == network.version and addr in network:
            return True
    return False


def email_banned(ctx: Context, email: str) -> bool:
    email = email.lower()
    return any(ban.kind == "email" and ban.value == email for ban in load_bans(ctx))


def set_session_cookie(request: Request, response, token: str) -> None:
    ctx = ctx_of(request)
    peer = request.client.host if request.client else None
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_SECONDS, path=ctx.cfg.base_path,
        httponly=True, samesite="strict",
        secure=not is_direct_loopback(ctx.cfg, peer, request.headers),
    )


def clear_session_cookie(request: Request, response) -> None:
    response.delete_cookie(COOKIE_NAME, path=ctx_of(request).cfg.base_path)


def _current_session(request: Request, db) -> tuple[User, UserSession] | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    session_row = db.get(UserSession, hash_token(token))
    now = time.time()
    if session_row is None or session_row.expires_at < now:
        return None
    user = db.get(User, session_row.user_id)
    if user is None or user.status != "active" or email_banned(ctx_of(request), user.email):
        return None
    if session_row.expires_at - now < SESSION_SECONDS / 2:
        session_row.expires_at = now + SESSION_SECONDS
        db.commit()
    return user, session_row


def session_user(request: Request, db=Depends(get_db)) -> User:
    """Any signed-in user, even one who must still change their password."""
    found = _current_session(request, db)
    if found is None:
        raise HTTPException(401, "Not signed in")
    return found[0]


def current_user(user: User = Depends(session_user)) -> User:
    if user.must_change_password:
        raise HTTPException(403, "password_change_required")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Administrator access required")
    return user


def require(permission: str):
    def dependency(user: User = Depends(current_user)) -> User:
        if permission not in effective_permissions(user):
            raise HTTPException(403, f"Missing permission: {permission}")
        return user
    return dependency
