"""Email alerts: each user's own, sent only to an address they've confirmed.

What triggers one, per user: their keywords ticked for email, channels and aircraft they watch,
and (if they ask) anything marked important. Only what their permissions let them see.
The first match is emailed at once; later ones wait for the user's chosen gap and go together.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from urllib.parse import urlencode

from sqlalchemy import and_, delete, func, or_, select

from . import emergencies, keywords
from .models import AcarsMessage, AlertSent, EmailLog, Recording, TrackEmergency, User, UserKeyword, UserWatch

log = logging.getLogger("sentrysdr.email")

WINDOWS = (5, 15, 60)  # minutes a user can choose between emails
DEFAULT_WINDOW = 15
ITEMS_PER_EMAIL = 25
MAX_WATCHES = 50
LOOKBACK_SECONDS = 24 * 3600  # never email anything older, even after an outage
SENT_KEEP_SECONDS = 3 * 86400
CONFIRM_SECONDS = 24 * 3600
CONFIRM_LIMIT = 3  # confirmation emails per user per hour
SMTP_TIMEOUT = 20
SECURITY = ("starttls", "tls", "none")
TEXT_LIMIT = 1200
_HEADER_UNSAFE = re.compile(r"[\r\n\x00-\x1f\x7f]")


class EmailError(Exception):
    pass


# --- sending -----------------------------------------------------------------
@dataclass
class SmtpSettings:
    host: str
    port: int
    security: str
    username: str
    password: str
    sender: str

    @property
    def ready(self) -> bool:
        return bool(self.host and self.sender)


def smtp_settings(ctx) -> SmtpSettings:
    try:
        port = int(ctx.get_setting("smtp_port") or 587)
    except ValueError:
        port = 587
    sealed = ctx.get_setting("smtp_password")
    return SmtpSettings(
        host=ctx.get_setting("smtp_host").strip(), port=port,
        security=ctx.get_setting("smtp_security") if ctx.get_setting("smtp_security") in SECURITY else "starttls",
        username=ctx.get_setting("smtp_username"), password=(ctx.secrets.open(sealed) or "") if sealed else "",
        sender=ctx.get_setting("smtp_from").strip(),
    )


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def header_text(value: str, limit: int = 150) -> str:
    """Names in a subject come from targets and aircraft: no line breaks, so no added headers."""
    return _HEADER_UNSAFE.sub(" ", value or "").strip()[:limit]


def build_message(settings: SmtpSettings, to: str, subject: str, body: str, headers: dict | None = None) -> EmailMessage:
    message = EmailMessage()
    name, address = parseaddr(settings.sender)
    message["From"] = formataddr((name or "SentrySDR", address))
    message["To"] = to
    message["Subject"] = header_text(subject)
    message["Message-ID"] = make_msgid(domain=address.rpartition("@")[2] or None)
    message["Auto-Submitted"] = "auto-generated"  # out-of-office replies shouldn't answer it
    for key, value in (headers or {}).items():
        message[key] = header_text(value, 500)
    message.set_content(body)
    return message


def deliver(settings: SmtpSettings, message: EmailMessage) -> None:
    """Blocking: run it in a thread. Certificates are checked; plain text only to this machine."""
    if not settings.ready:
        raise EmailError("Email isn't set up: add a mail server and From address under Admin > Email")
    if settings.security == "none" and not is_loopback(settings.host):
        raise EmailError("Without TLS, mail can only go to a server on this machine")
    context = ssl.create_default_context()
    try:
        if settings.security == "tls":
            server = smtplib.SMTP_SSL(settings.host, settings.port, timeout=SMTP_TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(settings.host, settings.port, timeout=SMTP_TIMEOUT)
        with server:
            if settings.security == "starttls":
                server.starttls(context=context)
            if settings.username:
                server.login(settings.username, settings.password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise EmailError(f"The mail server refused or couldn't be reached: {str(exc)[:200]}") from None


# --- links -------------------------------------------------------------------
def confirm_link(ctx, user_id: int, token: str) -> str:
    return f"{ctx.cfg.public_url.rstrip('/')}/#/email-confirm?{urlencode({'u': user_id, 't': token})}"


def off_token(ctx, user: User) -> str:
    """Tied to the confirmed address, so a link from before an address change stops working."""
    return ctx.secrets.mac(f"email-off:{user.id}:{user.email_verified or ''}")


def off_link(ctx, user: User, api: bool = False) -> str:
    query = urlencode({"u": user.id, "t": off_token(ctx, user)})
    base = ctx.cfg.public_url.rstrip("/")
    return f"{base}/api/email/off?{query}" if api else f"{base}/#/email-off?{query}"


def can_email(user: User) -> bool:
    return bool(user.status == "active" and user.email_alerts and user.email_verified and user.email_verified == user.email)


# --- matching ----------------------------------------------------------------
def _local(ctx, epoch: float) -> str:
    return datetime.fromtimestamp(epoch, ctx.cfg.tz).strftime("%Y-%m-%d %H:%M")


def find_matches(ctx, session, user: User, perms: set[str], since: float) -> list[dict]:
    """What this user should be emailed about now, oldest first. Already-sent items are left out."""
    sent = {(kind, item) for kind, item in session.execute(
        select(AlertSent.kind, AlertSent.item_id).where(AlertSent.user_id == user.id, AlertSent.sent_at >= since - 3600))}
    words = list(session.scalars(select(UserKeyword).where(UserKeyword.user_id == user.id, UserKeyword.email.is_(True))))
    watches = list(session.scalars(select(UserWatch).where(UserWatch.user_id == user.id)))
    items: list[dict] = []

    if "listen_recordings" in perms:
        call_words = [k.word for k in words if k.applies in ("calls", "both")]
        channels = [w.target_id for w in watches if w.kind == "channel" and w.target_id]
        arrived = func.coalesce(Recording.transcript_at, Recording.ended_at)
        expression = keywords.pattern(call_words)
        conditions = []
        if expression:
            conditions.append(and_(Recording.transcript.regexp_match(expression), arrived > since))
        if channels:
            conditions.append(and_(Recording.target_id.in_(channels), Recording.ended_at > since))
        if user.email_important:
            conditions.append(and_(Recording.important.is_(True), Recording.important_at > since))
        if conditions:
            for r in session.scalars(select(Recording).where(or_(*conditions)).order_by(Recording.started_at).limit(200)):
                if ("call", r.id) in sent:
                    continue
                matched = keywords.matches(call_words, r.transcript)
                why = [f'"{w}"' for w in matched]
                if r.target_id in channels:
                    why.append("a channel you watch")
                if r.important and user.email_important and (r.important_at or 0) > since:
                    why.append("marked important")
                if not why:
                    continue
                items.append({
                    "kind": "call", "id": r.id, "at": r.started_at, "why": why, "matched": matched,
                    "title": r.target_name or "Call", "detail": f"{(r.frequency_hz or 0) / 1e6:.4f}".rstrip("0").rstrip(".") + " MHz",
                    "text": r.transcript or "", "link": f"{ctx.cfg.public_url.rstrip('/')}/#/recordings?call={r.id}",
                })

    if "maps" in perms:
        message_words = [k.word for k in words if k.applies in ("messages", "both")]
        aircraft = {w.value for w in watches if w.kind == "aircraft" and w.value}
        expression = keywords.pattern(message_words)
        conditions = []
        if expression:
            conditions.append(and_(AcarsMessage.text.regexp_match(expression), AcarsMessage.received_at > since))
        if aircraft:
            conditions.append(and_(or_(AcarsMessage.tail.in_(aircraft), func.upper(AcarsMessage.flight).in_(aircraft)),
                                   AcarsMessage.received_at > since))
        if user.email_important:
            conditions.append(and_(AcarsMessage.important.is_(True), AcarsMessage.important_at > since))
        if conditions:
            for m in session.scalars(select(AcarsMessage).where(or_(*conditions)).order_by(AcarsMessage.received_at).limit(200)):
                if ("message", str(m.id)) in sent:
                    continue
                matched = keywords.matches(message_words, m.text)
                why = [f'"{w}"' for w in matched]
                if m.tail in aircraft or (m.flight or "").upper() in aircraft:
                    why.append("an aircraft you watch")
                if m.important and user.email_important and (m.important_at or 0) > since:
                    why.append("marked important")
                if not why:
                    continue
                items.append({
                    "kind": "message", "id": str(m.id), "at": m.received_at, "why": why, "matched": matched,
                    "title": " ".join(filter(None, (m.flight, m.tail))) or "ACARS message",
                    "detail": f"ACARS, label {m.label}" if m.label else "ACARS",
                    "text": m.text or "", "link": f"{ctx.cfg.public_url.rstrip('/')}/#/messages?id={m.id}",
                })

    if "maps" in perms and user.email_emergencies:
        codes, max_km = emergencies.wanted(user.emergency_codes), user.emergency_km
        for e in session.scalars(select(TrackEmergency).where(TrackEmergency.first_seen > since).order_by(TrackEmergency.first_seen)):
            if ("emergency", str(e.id)) in sent or not emergencies.matches_user(e, codes, max_km):
                continue
            where = [f"near {e.lat:.4f}, {e.lon:.4f}" if e.lat is not None and e.lon is not None else None,
                     f"{e.altitude:.0f} ft" if e.altitude is not None else None,
                     f"{e.distance_km:.0f} km from {e.source_name}" if e.distance_km is not None else f"heard by {e.source_name}"]
            items.append({
                "kind": "emergency", "id": str(e.id), "at": e.first_seen, "matched": [],
                "why": ["an aircraft emergency" if e.kind == "adsb" else "a ship emergency"],
                "title": "EMERGENCY: " + (" ".join(filter(None, (e.name, e.callsign))) or e.ident.upper()),
                "detail": emergencies.describe(e.codes or []),
                "text": ", ".join(filter(None, where)) + f"\n{'ICAO' if e.kind == 'adsb' else 'MMSI'} {e.ident.upper()}",
                "link": f"{ctx.cfg.public_url.rstrip('/')}/#/alerts?tab=emergencies",
            })

    items.sort(key=lambda i: i["at"])
    return items


def compose(ctx, user: User, items: list[dict], last_today: bool) -> tuple[str, str]:
    # Emergencies first, in the subject and the email.
    items = sorted(items, key=lambda i: (i["kind"] != "emergency", i["at"]))
    shown = items[:ITEMS_PER_EMAIL]
    first = items[0]
    if first["kind"] == "emergency":
        subject = f"SentrySDR {first['title']}: {first['detail']}"
        if len(items) > 1:
            subject += f", and {len(items) - 1} more alert{'s' if len(items) > 2 else ''}"
    elif len(items) == 1:
        subject = f"SentrySDR: {first['title']}" + (f" ({', '.join(first['matched'])})" if first["matched"] else "")
    else:
        subject = f"SentrySDR: {len(items)} new alerts"
    parts = []
    for item in shown:
        text = item["text"].strip()
        if len(text) > TEXT_LIMIT:
            text = text[:TEXT_LIMIT].rstrip() + "..."
        parts.append("\n".join(filter(None, (
            f"{item['title']}, {item['detail']}",
            _local(ctx, item["at"]),
            "Why: " + ", ".join(item["why"]),
            "" if not text else "\n" + "\n".join("  " + line for line in text.splitlines()) + "\n",
            f"Open: {item['link']}",
        ))))
    body = "\n\n----\n\n".join(parts)
    if len(items) > len(shown):
        body += f"\n\n{len(items) - len(shown)} more are on the Alerts page: {ctx.cfg.public_url.rstrip('/')}/#/alerts"
    if last_today:
        body += "\n\nThat's the most alert emails this server sends anyone in a day. More matches show on the Alerts page until tomorrow."
    body += (
        f"\n\n--\nYou get these because you turned on email alerts in SentrySDR. Times are in {ctx.cfg.tz}.\n"
        f"Change them: {ctx.cfg.public_url.rstrip('/')}/#/alerts?tab=settings\n"
        f"Turn them off: {off_link(ctx, user)}\n"
    )
    return subject, body


# --- the loop ----------------------------------------------------------------
def daily_cap(ctx) -> int:
    try:
        return max(1, int(ctx.get_setting("email_daily_cap") or 50))
    except ValueError:
        return 50


def sent_today(session, user_id: int, kind: str = "alert") -> int:
    return session.scalar(select(func.count()).select_from(EmailLog).where(
        EmailLog.user_id == user_id, EmailLog.kind == kind, EmailLog.sent_at > time.time() - 86400))


def pending(ctx) -> list[tuple[int, str, str, dict, list[dict]]]:
    """Emails due now: (user id, address, subject, body, headers, items). Doesn't send."""
    from .web.deps import effective_permissions  # the web package imports this module's context
    if ctx.get_setting("email_alerts_enabled") != "true":
        return []
    now = time.time()
    cap = daily_cap(ctx)
    out = []
    with ctx.db() as session:
        session.execute(delete(AlertSent).where(AlertSent.sent_at < now - SENT_KEEP_SECONDS))
        session.execute(delete(EmailLog).where(EmailLog.sent_at < now - SENT_KEEP_SECONDS))
        session.commit()
        for user in session.scalars(select(User).where(User.email_alerts.is_(True), User.status == "active")):
            if not can_email(user):
                continue
            window = (user.email_window if user.email_window in WINDOWS else DEFAULT_WINDOW) * 60
            waiting = now - (user.email_last_sent_at or 0) < window  # only an emergency goes before the gap is up
            if waiting and not user.email_emergencies:
                continue
            count = sent_today(session, user.id)
            if count >= cap:
                continue
            since = max(user.email_since or now, now - LOOKBACK_SECONDS)
            try:
                items = find_matches(ctx, session, user, effective_permissions(user), since)
                if not items or (waiting and not any(i["kind"] == "emergency" for i in items)):
                    continue
                subject, body = compose(ctx, user, items, last_today=count + 1 >= cap)
            except Exception:
                # One user's odd data (a watch, a keyword, a half-saved call) mustn't stop everyone else's emails.
                log.exception("couldn't work out alert emails for user %s", user.id)
                continue
            headers = {"List-Unsubscribe": f"<{off_link(ctx, user, api=True)}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
            out.append((user.id, user.email, subject, body, headers, items[:ITEMS_PER_EMAIL]))
    return out


def record_sent(ctx, user_id: int, items: list[dict]) -> None:
    now = time.time()
    with ctx.db() as session:
        for item in items:
            session.add(AlertSent(user_id=user_id, kind=item["kind"], item_id=item["id"], sent_at=now))
        session.add(EmailLog(user_id=user_id, kind="alert", sent_at=now))
        user = session.get(User, user_id)
        if user is not None:
            user.email_last_sent_at = now
        session.commit()


def send_due(ctx, send=deliver) -> int:
    """One pass: email everyone who has something due. Blocking; returns how many emails went."""
    sent = 0
    settings = smtp_settings(ctx)
    if not settings.ready:
        return 0
    for user_id, address, subject, body, headers, items in pending(ctx):
        try:
            send(settings, build_message(settings, address, subject, body, headers))
        except EmailError as exc:
            log.warning("alert email to user %s failed: %s", user_id, exc)
            continue  # tried again next pass
        try:
            record_sent(ctx, user_id, items)
        except Exception:
            log.exception("alert email to user %s went, but couldn't be recorded", user_id)
        sent += 1
    return sent
