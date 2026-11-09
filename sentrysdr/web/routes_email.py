"""Email alerts: each user's own settings, confirming their address, watches, and the admin's mail server."""
from __future__ import annotations

import hmac
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .. import notify, security
from ..acars import normalize_tail
from ..models import EmailLog, Target, User, UserWatch
from .deps import admin_user, ctx_of, current_user, effective_permissions, get_db, ip_of

router = APIRouter(prefix="/api")

LINK_LIMIT = 20  # confirm and off attempts per address per hour
LINK_WINDOW = 3600
TEST_LIMIT = 5
AIRCRAFT_RE = re.compile(r"^[A-Z0-9]{2,10}$")


def _state(ctx, user: User) -> dict:
    waiting = bool(user.email_confirm_hash and (user.email_confirm_expires or 0) > time.time()
                   and user.email_confirm_address == user.email and user.email_verified != user.email)
    perms = effective_permissions(user)
    return {
        "server_enabled": ctx.get_setting("email_alerts_enabled") == "true" and notify.smtp_settings(ctx).ready,
        "enabled": bool(user.email_alerts), "address": user.email,
        "confirmed": bool(user.email_verified and user.email_verified == user.email), "waiting": waiting,
        "window": user.email_window if user.email_window in notify.WINDOWS else notify.DEFAULT_WINDOW,
        "windows": list(notify.WINDOWS), "important": bool(user.email_important),
        "calls": "listen_recordings" in perms, "messages": "maps" in perms,
    }


def _send_confirmation(ctx, db, user: User) -> None:
    """A link to the user's current address. Limited, so nobody can use it to flood an inbox."""
    settings = notify.smtp_settings(ctx)
    if ctx.get_setting("email_alerts_enabled") != "true" or not settings.ready:
        raise HTTPException(409, "Email alerts aren't turned on on this server. Ask an admin.")
    recent = db.scalar(select(func.count()).select_from(EmailLog).where(
        EmailLog.user_id == user.id, EmailLog.kind == "confirm", EmailLog.sent_at > time.time() - 3600))
    if recent >= notify.CONFIRM_LIMIT:
        raise HTTPException(429, "Several confirmation emails were sent in the last hour. Check your spam folder, or try later.")
    token = security.new_token()
    body = (f"Someone, hopefully you, asked SentrySDR to send alert emails to {user.email}.\n\n"
            f"To confirm, open this link within 24 hours:\n{notify.confirm_link(ctx, user.id, token)}\n\n"
            "If it wasn't you, ignore this email: nothing will be sent.\n")
    try:
        notify.deliver(settings, notify.build_message(settings, user.email, "SentrySDR: confirm your email for alerts", body))
    except notify.EmailError as exc:
        raise HTTPException(502, str(exc)) from None
    user.email_confirm_hash = security.hash_token(token)
    user.email_confirm_address = user.email
    user.email_confirm_expires = time.time() + notify.CONFIRM_SECONDS
    db.add(EmailLog(user_id=user.id, kind="confirm"))
    db.commit()


# --- a user's own email settings ---------------------------------------------
@router.get("/me/email-alerts")
def get_email_alerts(request: Request, user: User = Depends(current_user)):
    return _state(ctx_of(request), user)


class EmailAlertsBody(BaseModel):
    enabled: bool
    window: int = notify.DEFAULT_WINDOW
    important: bool = False


@router.put("/me/email-alerts")
def set_email_alerts(body: EmailAlertsBody, request: Request, user: User = Depends(current_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    if body.window not in notify.WINDOWS:
        raise HTTPException(422, f"Choose {', '.join(map(str, notify.WINDOWS))} minutes")
    user = db.merge(user)
    turning_on = body.enabled and not user.email_alerts
    user.email_alerts, user.email_window, user.email_important = body.enabled or None, body.window, body.important or None
    if turning_on:
        user.email_since = time.time()  # nothing from before they asked
    db.commit()
    sent = False
    if body.enabled and user.email_verified != user.email and not _state(ctx, user)["waiting"]:
        _send_confirmation(ctx, db, user)
        sent = True
    ctx.audit("email_alerts.updated", user=user, ip=ip_of(request), details={"enabled": body.enabled})
    return {**_state(ctx, user), "confirmation_sent": sent}


@router.post("/me/email-alerts/resend")
def resend_confirmation(request: Request, user: User = Depends(current_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    user = db.merge(user)
    if user.email_verified == user.email:
        raise HTTPException(409, "Your address is already confirmed")
    _send_confirmation(ctx, db, user)
    return {**_state(ctx, user), "confirmation_sent": True}


# --- links from emails: no sign-in, the token is the proof ---------------------
class LinkBody(BaseModel):
    u: int
    t: str = Field(min_length=1, max_length=100)


def _link_limit(ctx, request: Request) -> None:
    key = f"email-link:{ip_of(request)}"
    if ctx.limiter.blocked(key, LINK_LIMIT, LINK_WINDOW):
        raise HTTPException(429, "Too many attempts. Try again later.")
    ctx.limiter.hit(key, LINK_WINDOW)


@router.post("/email/confirm")
def confirm_email(body: LinkBody, request: Request, db=Depends(get_db)):
    ctx = ctx_of(request)
    _link_limit(ctx, request)
    user = db.get(User, body.u)
    valid = (user is not None and user.email_confirm_hash and (user.email_confirm_expires or 0) > time.time()
             and user.email_confirm_address == user.email
             and hmac.compare_digest(user.email_confirm_hash, security.hash_token(body.t)))
    if not valid:
        raise HTTPException(400, "This link has expired, or is for an address you've since changed. Send a new one from the Alerts page.")
    # The link stays good until it expires, so opening it again (a reload, a second device) still says confirmed.
    if user.email_verified != user.email:
        user.email_verified = user.email
        if user.email_alerts:
            user.email_since = time.time()
        db.commit()
        ctx.audit("email_alerts.confirmed", user=user, ip=ip_of(request))
    return {"detail": f"Confirmed. Alert emails go to {user.email}."}


@router.post("/email/off")
async def email_off(request: Request, u: int | None = Query(default=None), t: str | None = Query(default=None, max_length=100)):
    """From the link in each email, or a mail app's one-click unsubscribe (which posts to the address itself)."""
    ctx = ctx_of(request)
    if u is None or not t:
        try:
            data = LinkBody.model_validate(await request.json())
        except Exception:
            raise HTTPException(400, "This link isn't complete") from None
        u, t = data.u, data.t
    _link_limit(ctx, request)
    with ctx.db() as db:
        user = db.get(User, u)
        if user is None or not user.email_verified or not hmac.compare_digest(notify.off_token(ctx, user), t):
            raise HTTPException(400, "This link doesn't work any more. Turn emails off on the Alerts page instead.")
        user.email_alerts = None
        db.commit()
        ctx.audit("email_alerts.turned_off_by_link", user=user, ip=ip_of(request))
        return {"detail": f"Alert emails to {user.email} are off. Turn them back on from the Alerts page."}


# --- watches: channels and aircraft ----------------------------------------------
def _watch_view(db, watch: UserWatch) -> dict:
    target = db.get(Target, watch.target_id) if watch.target_id else None
    return {"id": watch.id, "kind": watch.kind, "target_id": watch.target_id,
            "label": target.name if target else watch.value}


@router.get("/me/watches")
def list_watches(user: User = Depends(current_user), db=Depends(get_db)):
    rows = db.scalars(select(UserWatch).where(UserWatch.user_id == user.id).order_by(UserWatch.kind, UserWatch.id))
    return [_watch_view(db, w) for w in rows]


class WatchBody(BaseModel):
    kind: str
    target_id: int | None = None
    value: str = Field(default="", max_length=16)


@router.post("/me/watches", status_code=201)
def add_watch(body: WatchBody, user: User = Depends(current_user), db=Depends(get_db)):
    perms = effective_permissions(user)
    existing = list(db.scalars(select(UserWatch).where(UserWatch.user_id == user.id)))
    if len(existing) >= notify.MAX_WATCHES:
        raise HTTPException(409, f"At most {notify.MAX_WATCHES} each")
    if body.kind == "channel":
        if "listen_recordings" not in perms:
            raise HTTPException(403, "Missing permission: listen_recordings")
        if body.target_id is None or db.get(Target, body.target_id) is None:
            raise HTTPException(404, "Target not found")
        if any(w.kind == "channel" and w.target_id == body.target_id for w in existing):
            raise HTTPException(409, "You already watch that channel")
        watch = UserWatch(user_id=user.id, kind="channel", target_id=body.target_id)
    elif body.kind == "aircraft":
        if "maps" not in perms:
            raise HTTPException(403, "Missing permission: maps")
        value = normalize_tail(body.value)
        if not AIRCRAFT_RE.match(value):
            raise HTTPException(422, "A registration or flight is 2 to 10 letters and numbers, like N123UA or UA0123")
        if any(w.kind == "aircraft" and w.value == value for w in existing):
            raise HTTPException(409, "You already watch that aircraft")
        watch = UserWatch(user_id=user.id, kind="aircraft", value=value)
    else:
        raise HTTPException(422, "A watch is a channel or an aircraft")
    db.add(watch)
    db.commit()
    return _watch_view(db, watch)


@router.delete("/me/watches/{watch_id}")
def remove_watch(watch_id: int, user: User = Depends(current_user), db=Depends(get_db)):
    watch = db.get(UserWatch, watch_id)
    if watch is None or watch.user_id != user.id:
        raise HTTPException(404, "Watch not found")
    db.delete(watch)
    db.commit()
    return {"detail": "Removed"}


# --- the admin's test email --------------------------------------------------------
@router.post("/admin/email/test")
def test_email(request: Request, admin: User = Depends(admin_user)):
    """To the admin's own address, with the saved settings."""
    ctx = ctx_of(request)
    key = f"email-test:{admin.id}"
    if ctx.limiter.blocked(key, TEST_LIMIT, LINK_WINDOW):
        raise HTTPException(429, "Several test emails were sent in the last hour. Try later.")
    ctx.limiter.hit(key, LINK_WINDOW)
    settings = notify.smtp_settings(ctx)
    message = notify.build_message(settings, admin.email, "SentrySDR: test email",
                                   "This test from SentrySDR means alert emails can be sent with these settings.\n")
    try:
        notify.deliver(settings, message)
    except notify.EmailError as exc:
        raise HTTPException(502, str(exc)) from None
    ctx.audit("admin.email_test", user=admin, ip=ip_of(request))
    return {"detail": f"Sent to {admin.email}. If it isn't there in a minute, check the spam folder."}
