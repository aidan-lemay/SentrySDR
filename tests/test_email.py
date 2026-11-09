"""Email alerts. Nothing is ever really sent: delivery is replaced by a list."""
from __future__ import annotations

import re
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from sentrysdr import notify
from sentrysdr.models import AcarsMessage, Recording, Target, User

from .conftest import client_for


@pytest.fixture
def outbox(ctx, monkeypatch):
    class Outbox(list):
        fake = None
    sent = Outbox()

    def fake(settings, message):
        if getattr(fake, "fail", False):
            raise notify.EmailError("The mail server refused or couldn't be reached: 535 bad login")
        sent.append(message)

    monkeypatch.setattr(notify, "deliver", fake)
    for key, value in (("email_alerts_enabled", "true"), ("smtp_host", "mail.example.test"), ("smtp_from", "SentrySDR <alerts@example.test>")):
        ctx.set_setting(key, value)
    sent.fake = fake
    return sent


class Clock:
    """Moves time.time forward, for the gap between emails."""
    def __init__(self, monkeypatch):
        self.offset = 0.0
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + self.offset)

    def forward(self, seconds):
        self.offset += seconds


def link_params(message, path):
    body = message.get_content()
    match = re.search(rf"{re.escape(path)}\?u=(\d+)&t=([A-Za-z0-9_\-]+)", body)
    assert match, body
    return int(match.group(1)), match.group(2)


def confirm(client, outbox):
    """Turn on email alerts and click the confirmation link."""
    reply = client.put("/api/me/email-alerts", json={"enabled": True, "window": 5}).json()
    assert reply["confirmation_sent"] and not reply["confirmed"]
    u, t = link_params(outbox[-1], "#/email-confirm")
    response = client.post("/api/email/confirm", json={"u": u, "t": t})
    assert response.status_code == 200, response.text
    outbox.clear()


def add_call(ctx, target_id=None, transcript=None, name="County Fire", important=False):
    call = str(uuid.uuid4())
    now = time.time()
    with ctx.db() as session:
        session.add(Recording(id=call, started_at=now - 10, ended_at=now, duration=9, rel_path=f"{call}.mp3",
                              target_id=target_id, target_name=name, frequency_hz=154_430_000, transcript=transcript,
                              transcript_at=now if transcript else None, important=important or None,
                              important_at=now if important else None))
        session.commit()
    return call


def add_message(ctx, text="", tail="N123UA", flight="UA0123"):
    with ctx.db() as session:
        message = AcarsMessage(received_at=time.time(), serial="SER1", frequency_hz=131_550_000, tail=tail, flight=flight,
                               label="H1", text=text, has_text=bool(text))
        session.add(message)
        session.commit()
        return message.id


def add_target(ctx, name="Tower"):
    with ctx.db() as session:
        target = Target(name=name, frequency_hz=120_500_000, modulation="am")
        session.add(target)
        session.commit()
        return target.id


# --- confirming the address -------------------------------------------------
def test_nothing_is_emailed_before_the_address_is_confirmed(ctx, listener, outbox):
    listener.post("/api/me/keywords", json={"word": "fire", "email": True}).raise_for_status()
    reply = listener.put("/api/me/email-alerts", json={"enabled": True, "window": 5}).json()
    assert reply["confirmation_sent"] and reply["waiting"]
    assert outbox[-1]["To"] == "listen@example.com" and "confirm" in outbox[-1]["Subject"]
    add_call(ctx, transcript="Structure fire on Main Street")
    outbox.clear()
    assert ctx.send_alert_emails() == 0 and outbox == []


def test_confirm_link_works_once_and_only_with_its_token(ctx, app, listener, outbox):
    listener.put("/api/me/email-alerts", json={"enabled": True}).raise_for_status()
    u, t = link_params(outbox[-1], "#/email-confirm")
    stranger = client_for(app)  # the link works signed out
    assert stranger.post("/api/email/confirm", json={"u": u, "t": t + "x"}).status_code == 400
    assert stranger.post("/api/email/confirm", json={"u": u, "t": t}).status_code == 200
    assert stranger.post("/api/email/confirm", json={"u": u, "t": t}).status_code == 200  # a reload still says confirmed
    assert listener.get("/api/me/email-alerts").json()["confirmed"] is True


def test_an_old_link_doesnt_confirm_a_new_address(ctx, listener, outbox):
    listener.put("/api/me/email-alerts", json={"enabled": True}).raise_for_status()
    u, t = link_params(outbox[-1], "#/email-confirm")
    with ctx.db() as session:
        session.query(User).filter_by(id=u).update({"email": "someone.else@example.com"})
        session.commit()
    assert listener.post("/api/email/confirm", json={"u": u, "t": t}).status_code == 400
    assert listener.get("/api/me/email-alerts").json()["confirmed"] is False


def test_an_expired_link_doesnt_confirm(ctx, listener, outbox, monkeypatch):
    clock = Clock(monkeypatch)
    listener.put("/api/me/email-alerts", json={"enabled": True}).raise_for_status()
    u, t = link_params(outbox[-1], "#/email-confirm")
    clock.forward(notify.CONFIRM_SECONDS + 1)
    assert listener.post("/api/email/confirm", json={"u": u, "t": t}).status_code == 400


def test_confirmation_emails_are_limited(listener, outbox):
    listener.put("/api/me/email-alerts", json={"enabled": True}).raise_for_status()
    assert listener.post("/api/me/email-alerts/resend").status_code == 200
    assert listener.post("/api/me/email-alerts/resend").status_code == 200
    assert listener.post("/api/me/email-alerts/resend").status_code == 429
    assert len(outbox) == 3


def test_a_new_address_needs_confirming_again(ctx, listener, outbox):
    confirm(listener, outbox)
    listener.post("/api/me/keywords", json={"word": "fire", "email": True}).raise_for_status()
    with ctx.db() as session:
        session.query(User).filter_by(email="listen@example.com").update({"email": "moved@example.com"})
        session.commit()
    add_call(ctx, transcript="fire")
    assert ctx.send_alert_emails() == 0
    state = listener.get("/api/me/email-alerts").json()
    assert state["confirmed"] is False and state["address"] == "moved@example.com"


def test_nothing_when_the_server_has_email_off(ctx, listener, outbox):
    confirm(listener, outbox)
    listener.post("/api/me/keywords", json={"word": "fire", "email": True}).raise_for_status()
    ctx.set_setting("email_alerts_enabled", "false")
    add_call(ctx, transcript="fire")
    assert ctx.send_alert_emails() == 0
    ctx.set_setting("email_alerts_enabled", "true")
    ctx.set_setting("smtp_host", "")
    assert ctx.send_alert_emails() == 0 and outbox == []


# --- what triggers an email, and batching -------------------------------------
def test_keyword_email_then_batched(ctx, listener, outbox, monkeypatch):
    clock = Clock(monkeypatch)
    confirm(listener, outbox)
    listener.post("/api/me/keywords", json={"word": "fire", "email": True}).raise_for_status()
    listener.post("/api/me/keywords", json={"word": "smoke"}).raise_for_status()  # flagged in the app only
    first = add_call(ctx, transcript="Engine 41, structure fire, 12 Main Street")
    add_call(ctx, transcript="smoke showing")
    assert ctx.send_alert_emails() == 1
    message = outbox[-1]
    body = message.get_content()
    assert message["Subject"] == "SentrySDR: County Fire (fire)"
    assert "structure fire" in body and f"https://sentry.example.com/#/recordings?call={first}" in body
    assert "smoke" not in body and '"fire"' in body
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    # Within the gap: held, then sent together.
    add_call(ctx, transcript="second fire")
    add_call(ctx, transcript="third fire")
    assert ctx.send_alert_emails() == 0
    clock.forward(5 * 60 + 1)
    assert ctx.send_alert_emails() == 1
    assert outbox[-1]["Subject"] == "SentrySDR: 2 new alerts"
    clock.forward(5 * 60 + 1)
    assert ctx.send_alert_emails() == 0  # each goes once


def test_nothing_from_before_alerts_were_turned_on(ctx, listener, outbox):
    add_call(ctx, transcript="old fire")
    time.sleep(0.01)
    confirm(listener, outbox)
    listener.post("/api/me/keywords", json={"word": "fire", "email": True}).raise_for_status()
    assert ctx.send_alert_emails() == 0


def test_channel_watch_and_its_target_removed(ctx, admin, listener, outbox, monkeypatch):
    clock = Clock(monkeypatch)
    confirm(listener, outbox)
    tower = add_target(ctx, "Tower 120.5")
    watch = listener.post("/api/me/watches", json={"kind": "channel", "target_id": tower}).json()
    assert watch["label"] == "Tower 120.5"
    listener.post("/api/me/keywords", json={"word": "mayday", "email": True}).raise_for_status()
    add_call(ctx, target_id=tower, name="Tower 120.5")  # no transcript needed
    assert ctx.send_alert_emails() == 1 and "a channel you watch" in outbox[-1].get_content()
    # Renamed, then removed: the watch follows it, then goes with it, and the notifier carries on.
    admin.put(f"/api/targets/{tower}", json={"name": "Tower renamed", "frequency_hz": 120_600_000, "modulation": "am"})
    assert listener.get("/api/me/watches").json()[0]["label"] == "Tower renamed"
    assert admin.delete(f"/api/targets/{tower}").status_code == 200
    assert listener.get("/api/me/watches").json() == []
    add_call(ctx, target_id=tower, name="Tower renamed")  # a late call saved after the target went
    add_call(ctx, transcript="mayday mayday")
    clock.forward(301)
    assert ctx.send_alert_emails() == 1
    body = outbox[-1].get_content()
    assert "mayday" in body and "a channel you watch" not in body


def test_a_removed_sdr_doesnt_affect_watches(ctx, admin, listener, outbox):
    from tests.test_radio import make_source
    confirm(listener, outbox)
    tower = add_target(ctx)
    listener.post("/api/me/watches", json={"kind": "channel", "target_id": tower}).raise_for_status()
    source = make_source(admin)
    admin.post(f"/api/sources/{source}/assign", json={"target_id": tower}).raise_for_status()
    assert admin.delete(f"/api/sources/{source}").status_code == 200
    add_call(ctx, target_id=tower, name="Tower")
    assert ctx.send_alert_emails() == 1


def test_one_users_bad_data_doesnt_stop_others(ctx, app, admin, listener, outbox, monkeypatch):
    confirm(listener, outbox)
    confirm(admin, outbox)
    listener.post("/api/me/keywords", json={"word": "fire", "email": True}).raise_for_status()
    admin.post("/api/me/keywords", json={"word": "fire", "email": True}).raise_for_status()
    real = notify.find_matches

    def broken_for_listener(ctx_, session, user, perms, since):
        if user.email == "listen@example.com":
            raise ValueError("odd data")
        return real(ctx_, session, user, perms, since)

    monkeypatch.setattr(notify, "find_matches", broken_for_listener)
    add_call(ctx, transcript="fire")
    assert ctx.send_alert_emails() == 1 and outbox[-1]["To"] == "admin@example.com"


def test_aircraft_watch_and_message_keywords(ctx, listener, outbox, monkeypatch):
    clock = Clock(monkeypatch)
    confirm(listener, outbox)
    assert listener.post("/api/me/watches", json={"kind": "aircraft", "value": ".n123-ua"}).json()["label"] == "N123UA"
    assert listener.post("/api/me/watches", json={"kind": "aircraft", "value": "x"}).status_code == 422
    listener.post("/api/me/keywords", json={"word": "diversion", "applies": "messages", "email": True}).raise_for_status()
    add_message(ctx, text="OOOI OUT 1412", tail="N123UA")
    assert ctx.send_alert_emails() == 1 and "an aircraft you watch" in outbox[-1].get_content()
    clock.forward(301)
    message_id = add_message(ctx, text="REQUEST DIVERSION KSEA", tail="N9876D", flight="DL1234")
    assert ctx.send_alert_emails() == 1
    assert f"#/messages?id={message_id}" in outbox[-1].get_content()
    assert listener.get(f"/api/messages?id={message_id}").json()["total"] == 1


def test_marked_important_only_if_asked(ctx, listener, outbox, monkeypatch):
    clock = Clock(monkeypatch)
    confirm(listener, outbox)
    add_call(ctx, important=True)
    assert ctx.send_alert_emails() == 0
    listener.put("/api/me/email-alerts", json={"enabled": True, "window": 5, "important": True}).raise_for_status()
    clock.forward(1)
    add_call(ctx, important=True)
    assert ctx.send_alert_emails() == 1 and "marked important" in outbox[-1].get_content()


def test_permissions_still_apply(ctx, admin, listener, outbox):
    confirm(listener, outbox)
    listener.post("/api/me/keywords", json={"word": "fire", "email": True}).raise_for_status()
    me = listener.get("/api/me").json()
    admin.patch(f"/api/admin/users/{me['id']}", json={"perm_overrides": {"listen_recordings": False, "maps": False}}).raise_for_status()
    add_call(ctx, transcript="fire")
    add_message(ctx, text="fire")
    assert ctx.send_alert_emails() == 0
    assert listener.post("/api/me/watches", json={"kind": "aircraft", "value": "N1"}).status_code == 403


def test_daily_cap(ctx, listener, outbox, monkeypatch):
    clock = Clock(monkeypatch)
    ctx.set_setting("email_daily_cap", "2")
    confirm(listener, outbox)
    listener.post("/api/me/keywords", json={"word": "fire", "email": True}).raise_for_status()
    for _ in range(3):
        add_call(ctx, transcript="fire")
        ctx.send_alert_emails()
        clock.forward(301)
    assert len(outbox) == 2 and "most alert emails" in outbox[-1].get_content()
    clock.forward(86400)
    add_call(ctx, transcript="fire")
    assert ctx.send_alert_emails() == 1


def test_names_cant_add_headers(ctx, listener, outbox):
    confirm(listener, outbox)
    listener.post("/api/me/keywords", json={"word": "fire", "email": True}).raise_for_status()
    add_call(ctx, transcript="fire", name="Evil\r\nBcc: victim@example.com")
    assert ctx.send_alert_emails() == 1
    assert outbox[-1]["Bcc"] is None and "\n" not in outbox[-1]["Subject"]


# --- turning off ------------------------------------------------------------------
def test_off_link_and_one_click_unsubscribe(ctx, app, listener, outbox):
    confirm(listener, outbox)
    listener.post("/api/me/keywords", json={"word": "fire", "email": True}).raise_for_status()
    add_call(ctx, transcript="fire")
    ctx.send_alert_emails()
    u, t = link_params(outbox[-1], "#/email-off")
    header = outbox[-1]["List-Unsubscribe"].strip("<>")
    assert header.startswith("https://sentry.example.com/api/email/off?")
    # A mail app's one-click: a form post, with no SentrySDR header or Origin.
    bare = TestClient(app, base_url="https://sentry.example.com")
    assert bare.post(f"/api/email/off?u={u}&t=wrong", data={"List-Unsubscribe": "One-Click"}).status_code == 400
    assert bare.post(header.replace("https://sentry.example.com", ""), data={"List-Unsubscribe": "One-Click"}).status_code == 200
    assert listener.get("/api/me/email-alerts").json()["enabled"] is False
    # The page's own button sends JSON.
    listener.put("/api/me/email-alerts", json={"enabled": True}).raise_for_status()
    assert client_for(app).post("/api/email/off", json={"u": u, "t": t}).status_code == 200


# --- failures ----------------------------------------------------------------------
def test_a_failing_mail_server_warns_and_backs_off(ctx, admin, listener, outbox):
    confirm(listener, outbox)
    listener.post("/api/me/keywords", json={"word": "fire", "email": True}).raise_for_status()
    add_call(ctx, transcript="fire")
    outbox.fake.fail = True
    assert ctx.send_alert_emails() == 0
    assert any(w["key"] == "email.failing" and "535" in w["text"] for w in admin.get("/api/warnings").json())
    outbox.fake.fail = False
    assert ctx.send_alert_emails() == 0  # waits a few minutes before trying again
    ctx.email_retry_at = 0
    assert ctx.send_alert_emails() == 1  # the same alert, not lost
    assert not any(w["key"] == "email.failing" for w in admin.get("/api/warnings").json())


# --- admin settings ---------------------------------------------------------------------
def test_mail_settings(ctx, admin, operator, outbox):
    reply = admin.patch("/api/admin/settings", json={"smtp_host": "smtp.example.test", "smtp_port": 465, "smtp_security": "tls",
                                                       "smtp_username": "alerts", "smtp_password": "hunter2"}).json()
    assert reply["smtp_password_set"] is True and "smtp_password" not in reply
    assert ctx.get_setting("smtp_password") != "hunter2" and notify.smtp_settings(ctx).password == "hunter2"
    assert admin.patch("/api/admin/settings", json={"smtp_security": "none"}).status_code == 422  # a remote server in the clear
    assert admin.patch("/api/admin/settings", json={"smtp_host": "localhost", "smtp_security": "none"}).status_code == 200
    assert admin.patch("/api/admin/settings", json={"smtp_from": "not an address"}).status_code == 422
    assert admin.patch("/api/admin/settings", json={"smtp_host": "evil\r\nhost"}).status_code == 422
    assert admin.post("/api/admin/email/test").status_code == 200 and outbox[-1]["To"] == "admin@example.com"
    assert operator.post("/api/admin/email/test").status_code == 403


def test_plain_text_only_to_this_machine():
    settings = notify.SmtpSettings("smtp.example.test", 25, "none", "", "", "a@example.test")
    with pytest.raises(notify.EmailError, match="this machine"):
        notify.deliver(settings, notify.build_message(settings, "b@example.test", "x", "y"))
