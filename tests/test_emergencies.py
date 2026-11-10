"""Aircraft and ship emergencies: spotting, recording, alerts and email. Nothing is really sent."""
from __future__ import annotations

import time

from sentrysdr import emergencies, notify
from sentrysdr.engine import merge_ais_message
from sentrysdr.models import TrackEmergency

from .test_email import Clock, confirm, outbox  # noqa: F401  (outbox is a fixture)
from .test_radio import make_source


def aircraft(squawk="7700", hexid="a1b2c3", emergency=None, lat=43.2, lon=-77.6):
    item = {"hex": hexid, "flight": "UAL123  ", "squawk": squawk, "lat": lat, "lon": lon, "alt_baro": 12000}
    if emergency:
        item["emergency"] = emergency
    return item


def feed(ctx, clock, items, times=1, step=2, serial="SER1", kind="adsb"):
    field = "aircraft" if kind == "adsb" else "vessels"
    for _ in range(times):
        ctx.update_tracks(kind, ctx.local_node_id, serial, {field: items})
        clock.forward(step)


def events(ctx):
    with ctx.db() as session:
        return list(session.query(TrackEmergency).order_by(TrackEmergency.id))


# --- spotting ---------------------------------------------------------------------
def test_a_squawk_counts_only_once_it_has_lasted(ctx, monkeypatch):
    clock = Clock(monkeypatch)
    feed(ctx, clock, [aircraft()], times=1)
    feed(ctx, clock, [aircraft(squawk="1200")], times=1)  # a blip
    assert events(ctx) == []
    feed(ctx, clock, [aircraft()], times=6)  # 12 seconds of it
    [event] = events(ctx)
    assert event.codes == ["squawk_7700"] and event.name == "UAL123" and event.altitude == 12000
    assert ctx.emergencies.active("adsb") == {"a1b2c3": "Squawk 7700 (general emergency)"}


def test_ordinary_squawks_and_statuses_are_ignored(ctx, monkeypatch):
    clock = Clock(monkeypatch)
    feed(ctx, clock, [aircraft(squawk="7000", emergency="none"), aircraft(squawk="7777", hexid="bbbbbb")], times=8)
    assert events(ctx) == []


def test_emergency_status_and_squawk_are_one_event(ctx, monkeypatch):
    clock = Clock(monkeypatch)
    feed(ctx, clock, [aircraft(emergency="general")], times=6)
    [event] = events(ctx)
    assert set(event.codes) == {"squawk_7700", "adsb_general"}
    assert emergencies.describe(event.codes) == "Squawk 7700 (general emergency)"
    assert emergencies.describe(["adsb_minfuel", "squawk_7700"]) == "Squawk 7700 (general emergency), Minimum fuel"


def test_several_receivers_hear_one_emergency(ctx, admin, monkeypatch):
    clock = Clock(monkeypatch)
    for _ in range(6):
        ctx.update_tracks("adsb", ctx.local_node_id, "SER1", {"aircraft": [aircraft()]})
        ctx.update_tracks("adsb", ctx.local_node_id, "SER2", {"aircraft": [aircraft()]})
        clock.forward(2)
    assert len(events(ctx)) == 1


def test_it_ends_and_a_new_one_is_a_new_event(ctx, monkeypatch):
    clock = Clock(monkeypatch)
    feed(ctx, clock, [aircraft()], times=6)
    feed(ctx, clock, [aircraft(squawk="1200")], times=1)
    clock.forward(emergencies.END_SECONDS + 1)
    feed(ctx, clock, [aircraft(squawk="1200")], times=1)
    [ended] = events(ctx)
    assert ended.ended_at is not None and ctx.emergencies.active("adsb") == {}
    feed(ctx, clock, [aircraft()], times=6)
    assert len(events(ctx)) == 2


def test_a_restart_carries_on_the_same_event(ctx, monkeypatch):
    clock = Clock(monkeypatch)
    feed(ctx, clock, [aircraft()], times=6)
    ctx.emergencies = emergencies.Emergencies(ctx.db)  # as after a restart
    feed(ctx, clock, [aircraft()], times=6)
    assert len(events(ctx)) == 1


def test_distance_from_the_sdr_that_heard_it(ctx, admin, monkeypatch):
    clock = Clock(monkeypatch)
    make_source(admin, lat=43.0, lon=-77.6)
    feed(ctx, clock, [aircraft(lat=43.9, lon=-77.6)], times=6)
    assert 95 < events(ctx)[0].distance_km < 105


def test_ais_beacons_and_statuses_count_at_once(ctx, monkeypatch):
    clock = Clock(monkeypatch)
    vessels = [{"mmsi": "970123456", "lat": 43.3, "lon": -77.5}, {"mmsi": "972000001"}, {"mmsi": "974111222"},
               {"mmsi": "366123456", "shipname": "STUCK", "status": 6}, {"mmsi": "366999999", "status": 0}]
    feed(ctx, clock, vessels, kind="ais")
    assert {code for e in events(ctx) for code in e.codes} == {"ais_sart", "ais_mob", "ais_epirb", "ais_aground"}


def test_ais_keeps_the_navigation_status():
    vessels = {}
    merge_ais_message(vessels, b'{"mmsi": 366123456, "status": 6, "lat": 1, "lon": 2}')
    assert vessels["366123456"]["status"] == 6


def test_odd_data_from_a_slave_doesnt_break_it(ctx, monkeypatch):
    clock = Clock(monkeypatch)
    feed(ctx, clock, [{"hex": None, "squawk": "7700"}, {"squawk": 7700}, {"hex": "x" * 200, "squawk": "7700", "lat": "north"}], times=6)
    feed(ctx, clock, [{"mmsi": None, "status": "aground"}, {"mmsi": "12"}], kind="ais")
    assert all(len(e.ident) <= 16 and e.lat is None for e in events(ctx))


# --- the map and the record ----------------------------------------------------------
def test_map_marks_it_and_the_record_lists_it(ctx, listener, monkeypatch):
    clock = Clock(monkeypatch)
    feed(ctx, clock, [aircraft(), aircraft(squawk="1200", hexid="ffffff")], times=6)
    items = {i["hex"]: i for i in listener.get("/api/maps/adsb").json()["items"]}
    assert items["a1b2c3"]["emergency"].startswith("Squawk 7700") and "emergency" not in items["ffffff"]
    record = listener.get("/api/emergencies").json()
    assert record["total"] == 1 and record["items"][0]["title"] == "UAL123"


def test_the_record_needs_maps(ctx, admin, listener):
    me = listener.get("/api/me").json()
    admin.patch(f"/api/admin/users/{me['id']}", json={"perm_overrides": {"maps": False}}).raise_for_status()
    assert listener.get("/api/emergencies").status_code == 403


def test_kept_as_long_as_messages(ctx, monkeypatch):
    clock = Clock(monkeypatch)
    feed(ctx, clock, [aircraft()], times=6)
    ctx.set_setting("message_retention_days", "7")
    assert ctx.apply_retention(now=time.time() + 8 * 86400)["emergencies"] == 1


# --- each user's choice -----------------------------------------------------------------
def test_choices_shape_alerts(ctx, listener, monkeypatch):
    clock = Clock(monkeypatch)
    choices = listener.get("/api/me/emergency-alerts").json()
    chosen = {c["code"] for c in choices["codes"] if c["chosen"]}
    assert "squawk_7700" in chosen and "ais_nuc" not in chosen and choices["km"] is None
    feed(ctx, clock, [aircraft(squawk="7600")], times=6)
    assert [i["kind"] for i in listener.get("/api/alerts").json()["items"]] == ["emergency"]
    listener.put("/api/me/emergency-alerts", json={"codes": ["squawk_7700"]}).raise_for_status()
    assert listener.get("/api/alerts").json()["items"] == []
    assert listener.put("/api/me/emergency-alerts", json={"codes": ["squawk_1234"]}).status_code == 422
    assert listener.put("/api/me/emergency-alerts", json={"codes": [], "km": 33}).status_code == 422


def test_distance_choice(ctx, admin, listener, monkeypatch):
    clock = Clock(monkeypatch)
    make_source(admin, lat=43.0, lon=-77.6)
    feed(ctx, clock, [aircraft(lat=44.5, lon=-77.6)], times=6)  # about 167 km away
    listener.put("/api/me/emergency-alerts", json={"codes": ["squawk_7700"], "km": 100}).raise_for_status()
    assert listener.get("/api/alerts").json()["items"] == []
    listener.put("/api/me/emergency-alerts", json={"codes": ["squawk_7700"], "km": 200}).raise_for_status()
    assert len(listener.get("/api/alerts").json()["items"]) == 1


# --- email ------------------------------------------------------------------------------
def test_emergency_email_skips_the_gap(ctx, listener, outbox, monkeypatch):  # noqa: F811
    clock = Clock(monkeypatch)
    confirm(listener, outbox)
    listener.post("/api/me/keywords", json={"word": "fire", "email": True}).raise_for_status()
    listener.put("/api/me/email-alerts", json={"enabled": True, "window": 60}).raise_for_status()
    from .test_email import add_call
    add_call(ctx, transcript="fire")
    assert ctx.send_alert_emails() == 1  # the first, at once
    add_call(ctx, transcript="second fire")
    assert ctx.send_alert_emails() == 0  # waits for the gap
    feed(ctx, clock, [aircraft()], times=6)
    assert ctx.send_alert_emails() == 0  # emergencies aren't emailed until asked for
    listener.put("/api/me/emergency-alerts", json={"codes": ["squawk_7700"], "email": True}).raise_for_status()
    assert ctx.send_alert_emails() == 1  # an emergency doesn't wait, and brings the waiting call with it
    message = outbox[-1]
    assert message["Subject"].startswith("SentrySDR EMERGENCY: UAL123: Squawk 7700") and "and 1 more alert" in message["Subject"]
    body = message.get_content()
    assert body.index("EMERGENCY") < body.index("second fire") and "ICAO A1B2C3" in body and "12000 ft" in body
    assert ctx.send_alert_emails() == 0  # once


def test_emergency_email_needs_maps(ctx, admin, listener, outbox, monkeypatch):  # noqa: F811
    clock = Clock(monkeypatch)
    confirm(listener, outbox)
    listener.put("/api/me/emergency-alerts", json={"codes": ["squawk_7700"], "email": True}).raise_for_status()
    me = listener.get("/api/me").json()
    admin.patch(f"/api/admin/users/{me['id']}", json={"perm_overrides": {"maps": False}}).raise_for_status()
    feed(ctx, clock, [aircraft()], times=6)
    assert ctx.send_alert_emails() == 0


def test_readsb_saying_none_isnt_an_emergency_on_the_map(ctx, listener, monkeypatch):
    clock = Clock(monkeypatch)
    plain = {"hex": "ffffff", "flight": "N1", "lat": 43.0, "lon": -77.0, "emergency": "none"}
    feed(ctx, clock, [plain, {**plain, "hex": "eeeeee", "squawk": "1200"}], times=6)
    items = listener.get("/api/maps/adsb").json()["items"]
    assert len(items) == 2 and all("emergency" not in i for i in items)
    assert events(ctx) == []


import pytest  # noqa: E402


@pytest.mark.parametrize("squawk,meaning", [("7500", "unlawful interference"), ("7600", "radio failure"), ("7700", "general emergency")])
def test_each_emergency_squawk_is_labelled_and_alerts(ctx, listener, monkeypatch, squawk, meaning):
    clock = Clock(monkeypatch)
    feed(ctx, clock, [aircraft(squawk=squawk)], times=6)
    [item] = listener.get("/api/maps/adsb").json()["items"]
    assert item["emergency"].startswith(f"Squawk {squawk} ({meaning}")
    [alert] = listener.get("/api/alerts").json()["items"]
    assert alert["kind"] == "emergency"
    listener.put("/api/me/emergency-alerts", json={"codes": [c for c in ("squawk_7500", "squawk_7600", "squawk_7700") if c != f"squawk_{squawk}"]}).raise_for_status()
    assert listener.get("/api/alerts").json()["items"] == []
