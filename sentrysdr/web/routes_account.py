"""Sign-up, sign-in, and a user's management of their own account."""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from .. import security
from ..models import AuditLog, User, UserSession
from .deps import (
    SESSION_SECONDS, clear_session_cookie, ctx_of, current_user, effective_permissions, email_banned,
    get_db, hash_token, ip_of, session_user, set_session_cookie, COOKIE_NAME,
)

router = APIRouter(prefix="/api")

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s.]{2,}$")
LOGIN_WINDOW = 900
LOGIN_LIMIT_ACCOUNT = 8
LOGIN_LIMIT_IP = 30
SIGNUP_LIMIT_IP = 5
SIGNUP_WINDOW = 3600


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address")
    return email


def check_new_password(password: str) -> None:
    problem = security.password_problem(password)
    if problem:
        raise HTTPException(422, problem)


def confirm_password(ctx, user: User, password: str, message: str) -> None:
    """Checks a signed-in user's password, with the same limit as signing in, so a stolen session can't guess it."""
    key = f"password:{user.id}"
    if ctx.limiter.blocked(key, LOGIN_LIMIT_ACCOUNT, LOGIN_WINDOW):
        raise HTTPException(429, "Too many attempts. Try again later.")
    if not security.verify_password(user.password_hash, password):
        ctx.limiter.hit(key, LOGIN_WINDOW)
        raise HTTPException(403, message)


def user_view(user: User) -> dict:
    return {
        "id": user.id, "email": user.email, "name": user.name, "role": user.role,
        "status": user.status, "permissions": sorted(effective_permissions(user)),
        "must_change_password": user.must_change_password, "totp_enabled": user.totp_enabled,
    }


def revoke_sessions(db, user_id: int, keep_hash: str | None = None) -> None:
    query = delete(UserSession).where(UserSession.user_id == user_id)
    if keep_hash:
        query = query.where(UserSession.token_hash != keep_hash)
    db.execute(query)


class SignupBody(BaseModel):
    email: str = Field(max_length=254)
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(max_length=security.MAX_PASSWORD_LENGTH)


@router.post("/auth/signup", status_code=202)
def signup(body: SignupBody, request: Request, db=Depends(get_db)):
    ctx = ctx_of(request)
    ip = ip_of(request)
    if ctx.get_setting("registration_open") != "true":
        raise HTTPException(403, "Account requests are closed")
    limit_key = f"signup:{ip}"
    if ctx.limiter.blocked(limit_key, SIGNUP_LIMIT_IP, SIGNUP_WINDOW):
        raise HTTPException(429, "Too many requests. Try again later.")
    ctx.limiter.hit(limit_key, SIGNUP_WINDOW)
    email = normalize_email(body.email)
    check_new_password(body.password)
    # The same reply for every case, so this cannot be used to discover accounts.
    reply = {"detail": "Request received. An administrator must approve it before you can sign in."}
    password_hash = security.hash_password(body.password)
    if email_banned(ctx, email) or db.scalar(select(User).where(User.email == email)):
        ctx.audit("signup.refused", email=email, ip=ip)
        return reply
    db.add(User(email=email, name=body.name.strip(), password_hash=password_hash, status="pending", signup_ip=ip, last_ip=ip))
    db.commit()
    ctx.audit("signup.requested", email=email, ip=ip)
    return reply


class LoginBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=security.MAX_PASSWORD_LENGTH)
    totp: str | None = Field(default=None, max_length=12)


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response, db=Depends(get_db)):
    ctx = ctx_of(request)
    ip = ip_of(request)
    email = body.email.strip().lower()
    account_key, ip_key = f"login:acct:{email}", f"login:ip:{ip}"
    if ctx.limiter.blocked(account_key, LOGIN_LIMIT_ACCOUNT, LOGIN_WINDOW) or ctx.limiter.blocked(ip_key, LOGIN_LIMIT_IP, LOGIN_WINDOW):
        ctx.audit("login.rate_limited", email=email, ip=ip)
        raise HTTPException(429, "Too many attempts. Try again later.")

    user = db.scalar(select(User).where(User.email == email))

    def failed(reason: str, detail: str = "Incorrect email or password", status: int = 401):
        ctx.limiter.hit(account_key, LOGIN_WINDOW)
        ctx.limiter.hit(ip_key, LOGIN_WINDOW)
        ctx.audit("login.failed", email=email, ip=ip, details={"reason": reason})
        raise HTTPException(status, detail)

    if not security.verify_password(user.password_hash if user else None, body.password):
        failed("bad_credentials")
    # Everything below is only revealed to someone who knows the password.
    if email_banned(ctx, email):
        failed("banned", "This account is not allowed to sign in", 403)
    if user.status == "pending":
        failed("pending", "Your account is waiting for administrator approval", 403)
    if user.status != "active":
        failed(user.status, "This account is not allowed to sign in", 403)
    if user.totp_enabled:
        if not body.totp:
            raise HTTPException(401, "totp_required")
        secret = ctx.secrets.open(user.totp_secret_enc or "")
        step = security.verify_totp(secret, body.totp, user.totp_last_step) if secret else None
        if step is None:
            failed("bad_totp", "Incorrect authentication code")
        user.totp_last_step = step

    if security.needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(body.password)
    token = security.new_token()
    db.add(UserSession(
        token_hash=security.hash_token(token), user_id=user.id, expires_at=time.time() + SESSION_SECONDS,
        ip=ip, user_agent=(request.headers.get("user-agent") or "")[:255],
    ))
    user.last_login_at, user.last_ip = time.time(), ip
    db.commit()
    ctx.limiter.reset(account_key)
    ctx.audit("login.success", user=user, ip=ip)
    set_session_cookie(request, response, token)
    return user_view(user)


@router.post("/auth/logout")
def logout(request: Request, response: Response, db=Depends(get_db)):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == hash_token(token)))
        db.commit()
    clear_session_cookie(request, response)
    return {"detail": "Signed out"}


@router.get("/me")
def me(request: Request, user: User = Depends(session_user)):
    ctx = ctx_of(request)
    return {
        **user_view(user),
        "clearcut_page_url": ctx.get_setting("clearcut_page_url"),
        "map_tile_url": ctx.get_setting("map_tile_url"),
        "timezone": ctx.cfg.timezone,
        "tar1090_available": getattr(request.app.state, "tar1090_dir", None) is not None,
    }


class ProfileBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    current_password: str | None = Field(default=None, max_length=security.MAX_PASSWORD_LENGTH)


@router.patch("/me")
def update_me(body: ProfileBody, request: Request, user: User = Depends(current_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    user = db.merge(user)
    if body.name is not None:
        user.name = body.name.strip()
    if body.email is not None:
        email = normalize_email(body.email)
        if email != user.email:
            # Changing the sign-in email requires proof that it's really the owner.
            confirm_password(ctx, user, body.current_password or "", "Enter your current password to change your email")
            if email_banned(ctx, email) or db.scalar(select(User).where(func.lower(User.email) == email)):
                raise HTTPException(409, "That email can't be used")
            user.email = email
    db.commit()
    ctx.audit("account.updated", user=user, ip=ip_of(request))
    return user_view(user)


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=security.MAX_PASSWORD_LENGTH)
    new_password: str = Field(max_length=security.MAX_PASSWORD_LENGTH)


@router.post("/me/password")
def change_password(body: PasswordBody, request: Request, user: User = Depends(session_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    user = db.merge(user)
    confirm_password(ctx, user, body.current_password, "Current password is incorrect")
    check_new_password(body.new_password)
    if body.new_password == body.current_password:
        raise HTTPException(422, "Choose a different password")
    user.password_hash = security.hash_password(body.new_password)
    user.must_change_password = False
    revoke_sessions(db, user.id, hash_token(request.cookies.get(COOKIE_NAME, "")))
    db.commit()
    ctx.audit("account.password_changed", user=user, ip=ip_of(request))
    return {"detail": "Password changed"}


class ConfirmPassword(BaseModel):
    password: str = Field(max_length=security.MAX_PASSWORD_LENGTH)


@router.post("/me/totp/setup")
def totp_setup(body: ConfirmPassword, request: Request, user: User = Depends(current_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    user = db.merge(user)
    confirm_password(ctx, user, body.password, "Password is incorrect")
    if user.totp_enabled:
        raise HTTPException(409, "Two-factor authentication is already on")
    secret = security.new_totp_secret()
    user.totp_secret_enc = ctx.secrets.seal(secret)
    db.commit()
    return {"secret": secret, "uri": security.totp_uri(secret, user.email)}


class TotpCode(BaseModel):
    code: str = Field(max_length=12)


class DisableTotp(BaseModel):
    password: str = Field(max_length=security.MAX_PASSWORD_LENGTH)
    code: str = Field(default="", max_length=12)


@router.post("/me/totp/enable")
def totp_enable(body: TotpCode, request: Request, user: User = Depends(current_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    user = db.merge(user)
    if user.totp_enabled:
        raise HTTPException(409, "Two-factor authentication is already on")
    key = f"totp:{user.id}"
    if ctx.limiter.blocked(key, LOGIN_LIMIT_ACCOUNT, LOGIN_WINDOW):
        raise HTTPException(429, "Too many attempts. Try again later.")
    secret = ctx.secrets.open(user.totp_secret_enc or "")
    step = security.verify_totp(secret, body.code, 0) if secret else None
    if step is None:
        ctx.limiter.hit(key, LOGIN_WINDOW)
        raise HTTPException(422, "Incorrect code")
    user.totp_enabled, user.totp_last_step = True, step
    db.commit()
    ctx.audit("account.totp_enabled", user=user, ip=ip_of(request))
    return user_view(user)


@router.post("/me/totp/disable")
def totp_disable(body: DisableTotp, request: Request, user: User = Depends(current_user), db=Depends(get_db)):
    """Needs the password and a current code: a stolen session plus a leaked password isn't enough."""
    ctx = ctx_of(request)
    user = db.merge(user)
    confirm_password(ctx, user, body.password, "Password is incorrect")
    if user.totp_enabled:
        key = f"totp:{user.id}"
        if ctx.limiter.blocked(key, LOGIN_LIMIT_ACCOUNT, LOGIN_WINDOW):
            raise HTTPException(429, "Too many attempts. Try again later.")
        secret = ctx.secrets.open(user.totp_secret_enc or "")
        step = security.verify_totp(secret, body.code, user.totp_last_step) if secret else None
        if step is None:
            ctx.limiter.hit(key, LOGIN_WINDOW)
            raise HTTPException(403, "Enter a current code from your authenticator app")
    user.totp_enabled, user.totp_secret_enc, user.totp_last_step = False, None, 0
    db.commit()
    ctx.audit("account.totp_disabled", user=user, ip=ip_of(request))
    return user_view(user)


@router.get("/me/audit")
def my_audit(user: User = Depends(current_user), db=Depends(get_db)):
    rows = db.scalars(select(AuditLog).where(AuditLog.user_id == user.id).order_by(AuditLog.ts.desc()).limit(100))
    return [{"ts": r.ts, "action": r.action, "ip": r.ip, "target": r.target} for r in rows]
