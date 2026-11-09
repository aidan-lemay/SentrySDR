"""Scheduling, assignment, locks, recordings, uploads, retention and pipelines."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import time
import uuid
from array import array
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from sentrysdr import capabilities, feeds, pipelines, schedule, sdr, uploaders
from sentrysdr.context import BROADCASTIFY_RETRY_SECONDS, UPLOAD_MAX_ATTEMPTS
from sentrysdr.engine import MeasureError, VolumeControl, merge_ais_message
from sentrysdr.models import AcarsMessage, Antenna, AuditLog, BroadcastifySystem, Node, Recorder, Recording, SdrProfile, Source, Target, UploadJob, make_session_factory

from .conftest import ADMIN_EMAIL, PASSWORD, add_user, client_for

TUESDAY_2230 = datetime(2026, 11, 3, 22, 30)
TUESDAY_2300 = datetime(2026, 11, 3, 23, 0)


def rec(id, source_id=1, days=None, start=None, end=None, enabled=True):
    return SimpleNamespace(id=id, source_id=source_id, schedule_days=days, schedule_start=start, schedule_end=end, enabled=enabled)


# --- use case: "10pm to 11pm on Tuesdays", overriding then reverting -------
def test_schedule_overrides_base_then_reverts():
    base, net = rec(1), rec(2, days=[1], start="22:00", end="23:00")
    assert schedule.resolve([base, net], TUESDAY_2230)[1] is net
    assert schedule.resolve([base, net], TUESDAY_2300)[1] is base


def test_schedule_on_unassigned_source_then_idle():
    net = rec(2, days=[1], start="22:00", end="23:00")
    assert schedule.resolve([net], TUESDAY_2230)[1] is net
    assert schedule.resolve([net], TUESDAY_2300) == {}


def test_schedule_wrong_day_inactive():
    assert schedule.resolve([rec(2, days=[0], start="22:00", end="23:00")], TUESDAY_2230) == {}


def test_window_past_midnight_belongs_to_start_day():
    window = dict(days=[1], start="23:00", end="01:00")
    assert schedule.is_window_active(window["days"], window["start"], window["end"], datetime(2026, 11, 3, 23, 30))
    assert schedule.is_window_active(window["days"], window["start"], window["end"], datetime(2026, 11, 4, 0, 30))
    assert not schedule.is_window_active(window["days"], window["start"], window["end"], datetime(2026, 11, 4, 1, 30))
    assert not schedule.is_window_active(window["days"], window["start"], window["end"], datetime(2026, 11, 3, 0, 30))


def test_disabled_schedule_ignored_and_tie_is_deterministic():
    base = rec(1)
    assert schedule.resolve([base, rec(2, days=[1], start="22:00", end="23:00", enabled=False)], TUESDAY_2230)[1] is base
    first, second = rec(5, days=[1], start="22:00", end="23:00"), rec(6, days=[1], start="22:00", end="23:00")
    assert schedule.resolve([second, first], TUESDAY_2230)[1] is first


@pytest.mark.parametrize("value,ok", [("22:00", True), ("24:00", False), ("7:00", False), ("aa:bb", False), ("22:60", False)])
def test_valid_time(value, ok):
    assert schedule.valid_time(value) is ok


# --- API helpers -----------------------------------------------------------
def make_source(admin, serial="SER1", name="Source 1", **extra):
    response = admin.post("/api/sources", json={"serial": serial, "name": name, **extra})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def make_target(client, name="K1ABC", hz=444_444_000, **extra):
    response = client.post("/api/targets", json={"name": name, "frequency_hz": hz, **extra})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def source_row(client, source_id):
    return next(s for s in client.get("/api/sources").json() if s["id"] == source_id)


# --- sources only from configured serials ----------------------------------
def test_source_refuses_unconfigured_serial(admin):
    assert admin.post("/api/sources", json={"serial": "NOT-LISTED", "name": "x"}).status_code == 422


def test_source_refuses_shell_metacharacters(admin):
    assert admin.post("/api/sources", json={"serial": "a;rm -rf /", "name": "x"}).status_code == 422


def test_duplicate_source_refused(admin):
    make_source(admin)
    assert admin.post("/api/sources", json={"serial": "SER1", "name": "again"}).status_code == 409


def test_all_sources_visible_to_any_user(admin, listener):
    make_source(admin)
    assert [s["name"] for s in listener.get("/api/sources").json()] == ["Source 1"]


# --- assignment, locks, overrides ------------------------------------------
def test_operator_assigns_and_it_becomes_desired_state(ctx, admin, operator):
    source = make_source(admin)
    target = make_target(operator)
    assert operator.post(f"/api/sources/{source}/assign", json={"target_id": target}).status_code == 200
    spec = ctx.assignments()[ctx.local_node_id]["SER1"]
    assert spec["frequency_hz"] == 444_444_000 and spec["target_name"] == "K1ABC"
    assert source_row(operator, source)["active_target"]["name"] == "K1ABC"


def test_listener_cannot_assign_or_create_targets(admin, listener):
    source = make_source(admin)
    target = make_target(admin)
    assert listener.post(f"/api/sources/{source}/assign", json={"target_id": target}).status_code == 403
    assert listener.post("/api/targets", json={"name": "x", "frequency_hz": 444_000_000}).status_code == 403


def test_permission_override_grants_listener_assign(ctx, app, admin):
    user_id = add_user(ctx, "granted@example.com")
    source, target = make_source(admin), make_target(admin)
    admin.patch(f"/api/admin/users/{user_id}", json={"perm_overrides": {"assign": True, "download": False}})
    client = client_for(app, "granted@example.com")
    assert client.post(f"/api/sources/{source}/assign", json={"target_id": target}).status_code == 200
    assert "download" not in client.get("/api/me").json()["permissions"]


def lock(admin, source):
    assert admin.post(f"/api/sources/{source}/lock").status_code == 200


def test_a_locked_assignment_is_admin_only(ctx, admin, operator):
    source, first, second = make_source(admin), make_target(admin), make_target(admin, "Other", 145_000_000)
    url = f"/api/sources/{source}/assign"
    admin.post(url, json={"target_id": first})
    lock(admin, source)
    assert source_row(operator, source)["locked"] is True
    for body in ({"target_id": second}, {"target_id": None}):
        reply = operator.post(url, json=body)
        assert reply.status_code == 403 and "administrator" in reply.json()["detail"]
    assert ctx.assignments()[ctx.local_node_id]["SER1"]["target_id"] == first
    assert any(e["action"] == "recorder.lock_denied" for e in admin.get("/api/admin/audit").json())
    assert admin.post(url, json={"target_id": second}).status_code == 200
    assert ctx.assignments()[ctx.local_node_id]["SER1"]["target_id"] == second
    assert source_row(operator, source)["locked"] is True  # reassigning keeps the lock


def test_old_clients_cannot_use_a_password_to_get_past_a_lock(admin, operator):
    source, first, second = make_source(admin), make_target(admin), make_target(admin, "Other", 145_000_000)
    admin.post(f"/api/sources/{source}/assign", json={"target_id": first})
    lock(admin, source)
    reply = operator.post(f"/api/sources/{source}/assign", json={"target_id": second, "lock_password": "anything"})
    assert reply.status_code == 403


def test_unlocking_lets_users_change_it_again(ctx, admin, operator):
    source, first, second = make_source(admin), make_target(admin), make_target(admin, "Other", 145_000_000)
    url = f"/api/sources/{source}/assign"
    admin.post(url, json={"target_id": first})
    lock(admin, source)
    assert admin.delete(f"/api/sources/{source}/lock").status_code == 200
    assert source_row(operator, source)["locked"] is False
    assert operator.post(url, json={"target_id": second}).status_code == 200


def test_only_admins_can_lock_or_unlock(admin, operator, listener, app):
    source, target = make_source(admin), make_target(admin)
    admin.post(f"/api/sources/{source}/assign", json={"target_id": target})
    for client in (operator, listener):
        assert client.post(f"/api/sources/{source}/lock").status_code == 403
    lock(admin, source)
    for client in (operator, listener):
        assert client.delete(f"/api/sources/{source}/lock").status_code == 403
    assert client_for(app).post(f"/api/sources/{source}/lock").status_code == 401
    assert source_row(admin, source)["locked"] is True


def test_nothing_to_lock_without_an_assignment(admin):
    assert admin.post(f"/api/sources/{make_source(admin)}/lock").status_code == 409


def test_schedules_cannot_bypass_a_lock(admin, operator):
    source, first, second = make_source(admin), make_target(admin), make_target(admin, "Other", 145_000_000)
    other = make_source(admin, "SER2", "Other stick")
    admin.post(f"/api/sources/{source}/assign", json={"target_id": first})
    body = {"name": "sneak", "source_id": source, "target_id": second, "schedule_days": [1],
            "schedule_start": "22:00", "schedule_end": "23:00"}
    existing = admin.post("/api/recorders", json=body).json()["id"]
    elsewhere = admin.post("/api/recorders", json={**body, "name": "free", "source_id": other}).json()["id"]
    lock(admin, source)
    assert operator.post("/api/recorders", json=body).status_code == 403
    assert operator.put(f"/api/recorders/{existing}", json=body).status_code == 403
    assert operator.delete(f"/api/recorders/{existing}").status_code == 403
    assert operator.put(f"/api/recorders/{elsewhere}", json={**body, "source_id": source}).status_code == 403  # moving one in
    assert operator.put(f"/api/recorders/{existing}", json={**body, "source_id": other}).status_code == 403  # moving one out
    assert admin.delete(f"/api/recorders/{existing}").status_code == 200


def test_editing_target_cannot_retune_a_locked_recorder(admin, operator):
    source, target = make_source(admin), make_target(admin)
    admin.post(f"/api/sources/{source}/assign", json={"target_id": target})
    lock(admin, source)
    body = {"name": "K1ABC", "frequency_hz": 146_520_000}
    assert operator.put(f"/api/targets/{target}", json=body).status_code == 403
    assert operator.delete(f"/api/targets/{target}").status_code == 403
    assert admin.put(f"/api/targets/{target}", json=body).status_code == 200


def test_old_lock_passwords_become_plain_locks_and_are_deleted(tmp_path):
    db = tmp_path / "old.db"
    with make_session_factory(db)() as session:
        session.add(Node(name="n"))
        session.flush()
        node = session.scalar(select(Node)).id
        session.add(Source(node_id=node, serial="S", name="S"))
        session.add(Target(name="T", frequency_hz=441_000_000))
        session.commit()
    connection = sqlite3.connect(db)
    connection.execute("ALTER TABLE recorders DROP COLUMN locked")
    connection.execute("ALTER TABLE recorders ADD COLUMN lock_hash VARCHAR(255)")
    for name, lock_hash in (("locked", "$argon2id$secret-hash"), ("open", None)):
        connection.execute(
            "INSERT INTO recorders (name, source_id, target_id, enabled, assigned_by, assigned_at, lock_hash) VALUES (?, 1, 1, 1, '', 0, ?)",
            (name, lock_hash),
        )
    connection.commit()
    connection.close()
    for _ in range(2):  # a second start must change nothing
        with make_session_factory(db)() as session:
            found = {r.name: bool(r.locked) for r in session.scalars(select(Recorder))}
        assert found == {"locked": True, "open": False}
    connection = sqlite3.connect(db)
    columns = [row[1] for row in connection.execute("PRAGMA table_info(recorders)")]
    connection.close()
    assert "lock_hash" not in columns and "locked" in columns


def make_antenna(admin, name="Dual band", ranges=((144_000_000, 148_000_000), (420_000_000, 450_000_000)), **extra):
    body = {"name": name, "ranges": [{"low_hz": lo, "high_hz": hi} for lo, hi in ranges], **extra}
    response = admin.post("/api/antennas", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_antenna_with_several_ranges_warns_outside_all_of_them(admin):
    antenna = make_antenna(admin, kind="Ground plane", length=19.25, length_unit="in")
    assert antenna["ranges"] == [[144_000_000, 148_000_000], [420_000_000, 450_000_000]]
    source = make_source(admin, antenna_id=antenna["id"])
    uhf, vhf = make_target(admin, "UHF", 444_850_000), make_target(admin, "VHF", 146_520_000)
    air = make_target(admin, "Air", 120_000_000, modulation="am")
    for inside in (uhf, vhf):
        assert admin.post(f"/api/sources/{source}/assign", json={"target_id": inside}).json()["warning"] is None
    reply = admin.post(f"/api/sources/{source}/assign", json={"target_id": air})
    assert reply.status_code == 200 and "outside every range of Dual band" in reply.json()["warning"]
    listed = next(s for s in admin.get("/api/sources").json() if s["id"] == source)
    assert listed["antenna_name"] == "Dual band" and listed["antenna_ranges"] == antenna["ranges"]


def test_antenna_without_ranges_never_warns(admin):
    source = make_source(admin, antenna_id=make_antenna(admin, ranges=())["id"])
    target = make_target(admin, "Air", 120_000_000, modulation="am")
    assert admin.post(f"/api/sources/{source}/assign", json={"target_id": target}).json()["warning"] is None


def test_one_antenna_feeds_several_sdrs_and_removal_unbinds(admin):
    antenna = make_antenna(admin)
    first = make_source(admin, antenna_id=antenna["id"])
    second = make_source(admin, "SER2", "Source 2", antenna_id=antenna["id"])
    listed = next(a for a in admin.get("/api/antennas").json() if a["id"] == antenna["id"])
    assert listed["sources"] == ["Source 1", "Source 2"]
    assert admin.delete(f"/api/antennas/{antenna['id']}").status_code == 200
    assert {s["id"]: s["antenna_id"] for s in admin.get("/api/sources").json()} == {first: None, second: None}


def test_antenna_validation_and_admin_only(admin, operator):
    bad = admin.post("/api/antennas", json={"name": "X", "ranges": [{"low_hz": 450_000_000, "high_hz": 420_000_000}]})
    assert bad.status_code == 422
    assert admin.post("/api/antennas", json={"name": "X", "length": 3, "length_unit": "furlong"}).status_code == 422
    too_many = [{"low_hz": i, "high_hz": i + 1} for i in range(21)]
    assert admin.post("/api/antennas", json={"name": "X", "ranges": too_many}).status_code == 422
    assert admin.post("/api/sources", json={"serial": "SER1", "name": "S", "antenna_id": 999}).status_code == 422
    assert operator.post("/api/antennas", json={"name": "Mine"}).status_code == 403
    antenna = make_antenna(admin)
    assert operator.put(f"/api/antennas/{antenna['id']}", json={"name": "Mine"}).status_code == 403
    assert operator.delete(f"/api/antennas/{antenna['id']}").status_code == 403
    assert operator.get("/api/antennas").status_code == 200


def test_old_source_antenna_fields_become_antennas(tmp_path):
    db = tmp_path / "old.db"
    make_session_factory(db)
    connection = sqlite3.connect(db)
    connection.execute("INSERT INTO nodes (id, name, is_local, status, serials, created_at) VALUES (1, 'n', 1, 'active', '[]', 0)")
    rows = [(1, "A", "Discone", 25_000_000, 1_300_000_000), (2, "B", "Discone", 25_000_000, 1_300_000_000),
            (3, "C", "", 380_000_000, None), (4, "D", "", None, None)]
    for sid, name, kind, low, high in rows:
        connection.execute(
            "INSERT INTO sources (id, node_id, serial, name, description, antenna, freq_min_hz, freq_max_hz, ppm) "
            "VALUES (?, 1, ?, ?, '', ?, ?, ?, 0)", (sid, f"S{sid}", name, kind, low, high))
    connection.commit()
    connection.close()
    with make_session_factory(db)() as session:
        sources = {s.name: s for s in session.scalars(select(Source))}
        antennas = {a.id: a for a in session.scalars(select(Antenna))}
    assert sources["A"].antenna_id == sources["B"].antenna_id  # same description, so one antenna
    assert antennas[sources["A"].antenna_id].ranges == [[25_000_000, 1_300_000_000]]
    assert antennas[sources["C"].antenna_id].name == "C antenna"
    assert antennas[sources["C"].antenna_id].ranges == [[380_000_000, pipelines.FREQ_MAX_HZ]]
    assert sources["D"].antenna_id is None and len(antennas) == 2
    assert all(s.antenna == "" and s.freq_min_hz is None for s in sources.values())
    make_session_factory(db)  # runs again on every start, and must not make more
    with make_session_factory(db)() as session:
        assert len(session.scalars(select(Antenna)).all()) == 2


def test_scheduled_recorder_end_to_end(ctx, admin, operator):
    source, base, net = make_source(admin), make_target(admin), make_target(admin, "Net", 444_450_000)
    operator.post(f"/api/sources/{source}/assign", json={"target_id": base})
    response = operator.post("/api/recorders", json={
        "name": "Tuesday net", "source_id": source, "target_id": net, "schedule_days": [1],
        "schedule_start": "22:00", "schedule_end": "23:00",
    })
    assert response.status_code == 201, response.text
    during = ctx.assignments(TUESDAY_2230.replace(tzinfo=ctx.cfg.tz))
    after = ctx.assignments(TUESDAY_2300.replace(tzinfo=ctx.cfg.tz))
    assert during[ctx.local_node_id]["SER1"]["target_id"] == net
    assert after[ctx.local_node_id]["SER1"]["target_id"] == base


def test_invalid_schedule_refused(admin):
    source, target = make_source(admin), make_target(admin)
    body = {"name": "x", "source_id": source, "target_id": target, "schedule_days": [9], "schedule_start": "22:00", "schedule_end": "23:00"}
    assert admin.post("/api/recorders", json=body).status_code == 422
    assert admin.post("/api/recorders", json={**body, "schedule_days": [1], "schedule_end": "99:00"}).status_code == 422


# --- recordings ------------------------------------------------------------
def store(ctx, tmp_path, *, target_id=None, source_id=None, started=None, target_name="K1ABC"):
    started = started or time.time()
    audio = tmp_path / f"{uuid.uuid4()}.mp3"
    audio.write_bytes(b"ID3fake-mp3-data")
    meta = {
        "id": str(uuid.uuid4()), "serial": "SER1", "source_id": source_id, "source_name": "Source 1",
        "target_id": target_id, "target_name": target_name, "recorder_name": "r", "frequency_hz": 444_444_000,
        "modulation": "nfm", "started_at": started, "ended_at": started + 3, "duration": 3.0,
    }
    assert ctx.store_call(meta, audio, "node")
    return meta["id"]


def test_recordings_survive_deleting_their_setup(ctx, tmp_path, admin):
    source, target = make_source(admin), make_target(admin)
    call = store(ctx, tmp_path, source_id=source, target_id=target)
    admin.delete(f"/api/sources/{source}")
    admin.delete(f"/api/targets/{target}")
    items = admin.get("/api/recordings", params={"search": "K1ABC"}).json()["items"]
    assert [i["id"] for i in items] == [call]
    assert admin.get(f"/api/recordings/{call}/audio").status_code == 200


def test_listener_plays_and_downloads_but_cannot_protect_or_delete(ctx, tmp_path, listener):
    call = store(ctx, tmp_path)
    assert listener.get(f"/api/recordings/{call}/audio").content == b"ID3fake-mp3-data"
    response = listener.get(f"/api/recordings/{call}/download")
    assert "attachment" in response.headers["content-disposition"]
    assert listener.post(f"/api/recordings/{call}/protect", json={"protected": True}).status_code == 403
    assert listener.delete(f"/api/recordings/{call}").status_code == 403


def test_download_permission_can_be_removed(ctx, tmp_path, app, admin):
    user_id = add_user(ctx, "nodl@example.com")
    admin.patch(f"/api/admin/users/{user_id}", json={"perm_overrides": {"download": False}})
    client = client_for(app, "nodl@example.com")
    call = store(ctx, tmp_path)
    assert client.get(f"/api/recordings/{call}/download").status_code == 403


def test_stored_path_outside_recordings_refused(ctx, tmp_path, admin):
    call = store(ctx, tmp_path)
    (tmp_path / "secret.txt").write_text("top secret")
    with ctx.db() as session:
        session.get(Recording, call).rel_path = "../secret.txt"
        session.commit()
    assert admin.get(f"/api/recordings/{call}/audio").status_code == 404


def test_retention_keeps_protected_and_recent(ctx, tmp_path, admin):
    old = time.time() - 100 * 86400
    expired, kept = store(ctx, tmp_path, started=old), store(ctx, tmp_path, started=old)
    recent = store(ctx, tmp_path)
    admin.post(f"/api/recordings/{kept}/protect", json={"protected": True})
    with ctx.db() as session:
        expired_path = ctx.recording_path(session.get(Recording, expired))
    assert ctx.apply_retention()["recordings"] == 1
    assert not expired_path.exists()
    ids = {i["id"] for i in admin.get("/api/recordings").json()["items"]}
    assert ids == {kept, recent}


def test_retention_is_configurable_and_zero_keeps_forever(ctx, tmp_path, admin):
    store(ctx, tmp_path, started=time.time() - 1000 * 86400)
    admin.patch("/api/admin/settings", json={"recording_retention_days": 0})
    assert ctx.apply_retention()["recordings"] == 0
    admin.patch("/api/admin/settings", json={"recording_retention_days": 30})
    assert ctx.apply_retention()["recordings"] == 1


def test_audit_retention(ctx, admin):
    admin.patch("/api/admin/settings", json={"audit_retention_days": 1})
    assert ctx.apply_retention(now=time.time() + 3 * 86400)["audit"] > 0


def test_protected_recording_cannot_be_deleted(ctx, tmp_path, admin):
    call = store(ctx, tmp_path)
    admin.post(f"/api/recordings/{call}/protect", json={"protected": True})
    assert admin.delete(f"/api/recordings/{call}").status_code == 409


# --- uploads ---------------------------------------------------------------
async def run_uploads(ctx, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await ctx.process_uploads(client)


async def test_rdio_upload_format_and_retry(ctx, tmp_path, admin):
    target = make_target(admin, rdio_enabled=True, rdio_system=11, rdio_talkgroup=54241)
    admin.patch("/api/admin/settings", json={"rdio_server": "https://rdio.example.com/", "rdio_api_key": "k1"})
    call = store(ctx, tmp_path, target_id=target)
    seen = []

    def failing(request):
        return httpx.Response(500)

    def working(request):
        seen.append(request)
        return httpx.Response(200, text="Call imported successfully")

    assert await run_uploads(ctx, failing) == 1
    with ctx.db() as session:
        job = session.query(UploadJob).filter_by(recording_id=call).one()
        assert (job.status, job.attempts) == ("pending", 1)
        job.next_attempt_at = 0
        session.commit()
    assert await run_uploads(ctx, working) == 1
    body = seen[0].read().decode(errors="replace")
    assert str(seen[0].url) == "https://rdio.example.com/api/call-upload"
    for field in ('name="key"', 'name="audio"', 'name="system"', 'name="talkgroup"', 'name="dateTime"', "54241"):
        assert field in body
    with ctx.db() as session:
        assert session.query(UploadJob).filter_by(recording_id=call).one().status == "done"


def test_clearcut_form_matches_trunk_recorder_plugin():
    recording = SimpleNamespace(frequency_hz=444_444_000, started_at=1000.4, ended_at=1003.4, duration=3.0)
    form = uploaders.clearcut_form(recording, SimpleNamespace(clearcut_id=77), "key")
    assert set(form) == {"freq", "error_count", "spike_count", "start_time", "stop_time", "call_length",
                         "talkgroup_num", "emergency", "api_key", "source_list"}
    assert form["talkgroup_num"] == "77" and form["start_time"] == "1000" and form["source_list"] == "[]"


def test_upload_error_does_not_leak_response_body():
    with pytest.raises(uploaders.UploadError) as err:
        uploaders._check("rdio-scanner", httpx.Response(403, text="bad key: k1-secret"))
    assert "k1-secret" not in str(err.value)


def test_no_job_when_target_upload_disabled(ctx, tmp_path, admin):
    call = store(ctx, tmp_path, target_id=make_target(admin))
    with ctx.db() as session:
        assert session.query(UploadJob).filter_by(recording_id=call).count() == 0


# --- pipelines -------------------------------------------------------------
def spec(**overrides):
    return pipelines.sanitize_spec({
        "recorder_id": 1, "source_id": 1, "target_id": 1, "frequency_hz": 444_444_000,
        "modulation": "nfm", "squelch": 50, **overrides,
    })


def test_nfm_uses_tuner_agc_when_gain_unset(ctx):
    args = pipelines.rtl_fm_args(ctx.cfg, 2, spec())
    assert args[1:5] == ["-d", "2", "-f", "444444000"] and "-g" not in args
    assert args[args.index("-l") + 1] == "50"


@pytest.mark.parametrize("modulation", ["nfm", "am", "wfm", "dmr", "p25p1"])
def test_every_rtl_fm_mode_uses_the_targets_squelch(ctx, modulation):
    args = pipelines.rtl_fm_args(ctx.cfg, 0, spec(modulation=modulation, squelch=137))
    assert args[args.index("-l") + 1] == "137"


def test_measure_reads_at_the_targets_rate_and_gain(ctx):
    args = pipelines.measure_args(ctx.cfg, 1, 444_850_000, "wfm", 40.2, 3)
    assert args[args.index("-M") + 1] == "raw" and args[args.index("-s") + 1] == "170000"
    assert args[args.index("-g") + 1] == "40.2" and args[args.index("-p") + 1] == "3"
    assert "-g" not in pipelines.measure_args(ctx.cfg, 1, 444_850_000, "nfm", None)


def iq(level, seconds, rate=16000):
    return array("h", [level, -level] * int(rate * seconds)).tobytes()


def test_signal_levels_skip_settling_and_match_rms():
    levels = pipelines.signal_levels(iq(9000, 1) + iq(100, 2), 16000)
    assert len(levels) == 20 and all(value == pytest.approx(100) for value in levels)


def test_suggested_squelch_ignores_a_brief_transmission():
    pcm = iq(0, 1) + iq(64, 9) + iq(3000, 1)
    assert pipelines.suggest_squelch(pipelines.signal_levels(pcm, 16000)) == 128


def test_signal_levels_ignore_a_partial_sample_and_too_little_audio():
    assert pipelines.signal_levels(iq(100, 0.5) + b"\x01", 16000) == []


def test_manual_gain_and_ppm_passed(ctx):
    args = pipelines.rtl_fm_args(ctx.cfg, 0, spec(gain_db=28.0, ppm=3))
    assert args[args.index("-g") + 1] == "28" and args[args.index("-p") + 1] == "3"


@pytest.mark.parametrize("bad", [
    {"modulation": "nfm; rm -rf /"}, {"frequency_hz": 1}, {"frequency_hz": "abc"},
    {"squelch": -1}, {"gain_db": 999}, {"ppm": 99999}, {"kind": "shell"},
])
def test_sanitize_rejects_bad_values(bad):
    with pytest.raises(pipelines.SpecError):
        spec(**bad)


def test_decoders_open_no_network_ports(ctx):
    adsb = pipelines.readsb_args(ctx.cfg, 0, spec(kind="adsb", modulation="adsb"), "/tmp/x")
    ais = pipelines.ais_args(ctx.cfg, 0, spec(kind="ais", modulation="ais"))
    assert not any(arg.startswith("--net") for arg in adsb)
    assert "-N" not in ais and "-S" not in ais


# --- serial mapping --------------------------------------------------------
def test_numeric_serial_resolves_to_its_own_index():
    # rtl_fm would read "00000001" as index 1, which is someone else's dongle.
    assert sdr.index_for_serial("00000001", ["AAA", "BBB", "00000001"]) == 2


def test_missing_and_duplicate_serials_are_refused():
    assert sdr.index_for_serial("NOPE", ["AAA"]) is None
    assert sdr.index_for_serial("00000001", ["00000001", "00000001"]) is None


def test_ais_messages_merge_by_vessel():
    vessels = {}
    merge_ais_message(vessels, b'{"mmsi": 123, "lat": 1.5, "lon": 2.5}')
    merge_ais_message(vessels, b'{"mmsi": 123, "shipname": "ALICE"}')
    merge_ais_message(vessels, b"not json")
    assert vessels["123"]["lat"] == 1.5 and vessels["123"]["shipname"] == "ALICE" and len(vessels) == 1


def test_real_ais_catcher_output_is_understood():
    # AIS-catcher -o 5 output for a known message, trimmed to the fields that matter.
    line = (b'{"class":"AIS","device":"AIS-catcher","channel":"B","type":1,"mmsi":477553000,"status":5,'
            b'"speed":0,"lon":-122.34584,"lat":47.582832,"course":51,"heading":181}')
    vessels = {}
    merge_ais_message(vessels, line)
    vessel = vessels["477553000"]
    assert (vessel["lat"], vessel["lon"], vessel["heading"]) == (47.582832, -122.34584, 181)


def test_tracks_merge_across_receivers_and_expire(ctx):
    ctx.update_tracks("adsb", 1, "A", {"aircraft": [{"hex": "abc", "flight": "OLD"}]})
    ctx.update_tracks("adsb", 2, "B", {"aircraft": [{"hex": "abc", "flight": "NEW"}, {"hex": "def"}]})
    tracks = {a["hex"]: a for a in ctx.current_tracks("adsb")}
    assert tracks["abc"]["flight"] == "NEW" and "def" in tracks
    ctx.tracks["adsb"]["2:B"]["at"] = 0
    assert [a["flight"] for a in ctx.current_tracks("adsb")] == ["OLD"]


# --- ClearCut transcripts --------------------------------------------------
CC_TARGET = dict(clearcut_enabled=True, clearcut_system="us-ny-test", clearcut_id=1077)


def uploaded_call(ctx, tmp_path, admin, started=None):
    admin.patch("/api/admin/settings", json={"clearcut_server": "https://cc.example.com", "clearcut_api_key": "k"})
    target = next((t["id"] for t in admin.get("/api/targets").json() if t["name"] == "CC"), None) or make_target(admin, "CC", **CC_TARGET)
    call = store(ctx, tmp_path, target_id=target, started=started)
    with ctx.db() as session:
        session.query(UploadJob).filter_by(recording_id=call).one().status = "done"
        session.commit()
    return call


def cc_handler(calls, seen, transcribed=None):
    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path == "/api/v1/calls/transcribe":
            return httpx.Response(200, json=transcribed or {})
        return httpx.Response(200, json=calls)
    return handler


def started_of(ctx, call):
    with ctx.db() as session:
        return session.get(Recording, call).started_at


def row_of(ctx, call):
    with ctx.db() as session:
        return session.get(Recording, call)


async def run_sync(ctx, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await ctx.sync_transcripts(client)


def remote_call(started, text=None, id="7523045465739231232"):
    transcript = {"text": text, "parts": [], "callId": id, "audioFile": "a.m4a"} if text is not None else None
    return {"id": id, "startTime": int(started), "endTime": int(started) + 3, "talkgroup": 1077, "transcript": transcript}


async def test_transcript_copied_from_clearcut(ctx, tmp_path, admin):
    call = uploaded_call(ctx, tmp_path, admin)
    seen = []
    assert await run_sync(ctx, cc_handler([remote_call(started_of(ctx, call), "Engine 5 responding")], seen)) == 1
    assert seen == [("GET", "/api/v1/calls", {"system": "us-ny-test", "talkgroup": "1077"})]
    assert row_of(ctx, call).transcript == "Engine 5 responding"
    assert admin.get("/api/recordings", params={"search": "Engine 5"}).json()["total"] == 1
    # Done with this call: no further requests to ClearCut.
    seen.clear()
    await run_sync(ctx, cc_handler([], seen))
    assert seen == []


async def test_other_calls_are_not_matched(ctx, tmp_path, admin):
    call = uploaded_call(ctx, tmp_path, admin)
    assert await run_sync(ctx, cc_handler([remote_call(started_of(ctx, call) + 60, "someone else")], [])) == 0
    assert row_of(ctx, call).transcript is None


async def test_call_id_kept_while_transcript_is_pending(ctx, tmp_path, admin):
    call = uploaded_call(ctx, tmp_path, admin)
    assert await run_sync(ctx, cc_handler([remote_call(started_of(ctx, call))], [])) == 0
    assert row_of(ctx, call).clearcut_call_id == "7523045465739231232"


async def test_one_request_per_talkgroup(ctx, tmp_path, admin):
    uploaded_call(ctx, tmp_path, admin)
    uploaded_call(ctx, tmp_path, admin)
    seen = []
    await run_sync(ctx, cc_handler([], seen))
    assert len(seen) == 1


async def test_old_calls_and_unconfigured_server_make_no_requests(ctx, tmp_path, admin):
    uploaded_call(ctx, tmp_path, admin, started=time.time() - 3 * 86400)
    seen = []
    await run_sync(ctx, cc_handler([], seen))
    assert seen == []
    admin.patch("/api/admin/settings", json={"clearcut_server": ""})
    uploaded = uploaded_call(ctx, tmp_path, admin)
    admin.patch("/api/admin/settings", json={"clearcut_server": ""})
    await run_sync(ctx, cc_handler([], seen))
    assert uploaded and seen == []


async def test_call_not_yet_uploaded_is_not_looked_up(ctx, tmp_path, admin):
    call = uploaded_call(ctx, tmp_path, admin)
    with ctx.db() as session:
        session.query(UploadJob).filter_by(recording_id=call).one().status = "pending"
        session.commit()
    seen = []
    await run_sync(ctx, cc_handler([], seen))
    assert seen == []


@pytest.mark.parametrize("body", [{"not": "a list"}, ["text"], [{"startTime": "x"}]])
async def test_odd_clearcut_replies_are_harmless(ctx, tmp_path, admin, body):
    call = uploaded_call(ctx, tmp_path, admin)
    await run_sync(ctx, lambda request: httpx.Response(200, json=body))
    assert row_of(ctx, call).transcript is None


async def test_clearcut_outage_is_harmless(ctx, tmp_path, admin):
    call = uploaded_call(ctx, tmp_path, admin)
    await run_sync(ctx, lambda request: httpx.Response(500))
    await run_sync(ctx, lambda request: httpx.Response(200, text="<html>"))
    assert row_of(ctx, call).transcript is None


def test_transcript_text_is_capped_and_blank_is_none():
    assert uploaders.transcript_text({"transcript": {"text": "x" * 99999}}) == "x" * uploaders.MAX_TRANSCRIPT_CHARS
    assert uploaders.transcript_text({"transcript": {"text": "   "}}) is None
    assert uploaders.transcript_text({"transcript": None}) is None
    assert uploaders.transcript_text({"transcript": {"text": 5}}) is None


def test_request_transcript_looks_up_call_then_posts_id(ctx, tmp_path, admin):
    call = uploaded_call(ctx, tmp_path, admin)
    remote = remote_call(started_of(ctx, call))
    seen = []
    ctx.http_transport = httpx.MockTransport(cc_handler([remote], seen, {**remote, "transcript": {"text": "Copy that"}}))
    response = admin.post(f"/api/recordings/{call}/transcribe")
    assert response.status_code == 200, response.text
    assert response.json()["transcript"] == "Copy that"
    assert seen[-1] == ("POST", "/api/v1/calls/transcribe", {"id": remote["id"]})


def test_request_transcript_refused_when_clearcut_not_listing_it_yet(ctx, tmp_path, admin):
    call = uploaded_call(ctx, tmp_path, admin)
    seen = []
    ctx.http_transport = httpx.MockTransport(cc_handler([], seen))
    assert admin.post(f"/api/recordings/{call}/transcribe").status_code == 409
    assert all(method == "GET" for method, _, _ in seen)


def test_request_transcript_needs_permission_and_an_upload(ctx, tmp_path, admin, listener):
    call = uploaded_call(ctx, tmp_path, admin)
    assert listener.post(f"/api/recordings/{call}/transcribe").status_code == 403
    assert admin.post(f"/api/recordings/{store(ctx, tmp_path)}/transcribe").status_code == 409


def test_existing_transcript_makes_no_request(ctx, tmp_path, admin):
    call = uploaded_call(ctx, tmp_path, admin)
    with ctx.db() as session:
        session.get(Recording, call).transcript = "already here"
        session.commit()
    seen = []
    ctx.http_transport = httpx.MockTransport(cc_handler([], seen))
    assert admin.post(f"/api/recordings/{call}/transcribe").json()["transcript"] == "already here"
    assert seen == []


def test_transcribe_requests_rate_limited(ctx, tmp_path, admin):
    call = uploaded_call(ctx, tmp_path, admin)
    ctx.http_transport = httpx.MockTransport(cc_handler([], []))
    codes = [admin.post(f"/api/recordings/{call}/transcribe").status_code for _ in range(31)]
    assert codes[:30] == [409] * 30 and codes[30] == 429


def test_clearcut_target_needs_system_and_talkgroup(admin):
    body = {"name": "x", "frequency_hz": 444_000_000, "clearcut_enabled": True}
    assert admin.post("/api/targets", json={**body, "clearcut_id": 5}).status_code == 422
    assert admin.post("/api/targets", json={**body, "clearcut_system": "a", "clearcut_id": 5}).status_code == 201
    assert admin.post("/api/targets", json={**body, "clearcut_system": "../x", "clearcut_id": 5}).status_code == 422


def test_upgrade_adds_new_columns_to_an_old_database(tmp_path):
    import sqlite3
    from sentrysdr.models import make_session_factory
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE targets (id INTEGER PRIMARY KEY, name VARCHAR(120))")
    connection.execute("INSERT INTO targets (id, name) VALUES (1, 'kept')")
    connection.commit()
    connection.close()
    make_session_factory(path)
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT name, clearcut_system FROM targets").fetchall() == [("kept", None)]
    connection.close()


# --- saved profiles and noise measurement -----------------------------------
def local_setup(admin, ctx, serial="SER1", target_mhz=441.0, **target):
    source = admin.post("/api/sources", json={"serial": serial, "name": f"Stick {serial}", "gain_db": 10.0}).json()["id"]
    target_id = admin.post("/api/targets", json={
        "name": f"T{target_mhz}", "frequency_hz": int(target_mhz * 1e6), "modulation": "nfm", "squelch": 50, **target,
    }).json()["id"]
    return source, target_id


def running_spec(ctx, serial):
    return ctx.assignments()[ctx.local_node_id][serial]


def test_profile_sets_squelch_and_gain_for_that_stick_and_target_only(admin, ctx):
    one, target = local_setup(admin, ctx)
    two = admin.post("/api/sources", json={"serial": "SER2", "name": "Other", "gain_db": 20.0}).json()["id"]
    for source in (one, two):
        admin.post(f"/api/sources/{source}/assign", json={"target_id": target})
    saved = admin.put(f"/api/sources/{one}/profiles/{target}", json={"squelch": 98, "gain_db": 19.2, "noise_level": 62})
    assert saved.status_code == 200 and saved.json()["measured_at"]
    first, second = running_spec(ctx, "SER1"), running_spec(ctx, "SER2")
    assert (first["squelch"], first["gain_db"]) == (98, 19.2)
    assert (second["squelch"], second["gain_db"]) == (50, 20.0)
    view = next(s for s in admin.get("/api/sources").json() if s["id"] == one)
    assert view["active_target"]["from_profile"] and view["active_target"]["squelch"] == 98
    assert view["profiles"][0]["squelch"] == 98


def test_profile_with_empty_gain_means_agc_not_the_source_gain(admin, ctx):
    source, target = local_setup(admin, ctx)
    admin.post(f"/api/sources/{source}/assign", json={"target_id": target})
    admin.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 101, "gain_db": None})
    assert running_spec(ctx, "SER1")["gain_db"] is None


def test_saving_again_replaces_and_removing_restores_defaults(admin, ctx):
    source, target = local_setup(admin, ctx)
    admin.post(f"/api/sources/{source}/assign", json={"target_id": target})
    path = f"/api/sources/{source}/profiles/{target}"
    admin.put(path, json={"squelch": 98})
    admin.put(path, json={"squelch": 120, "gain_db": 7.1})
    view = next(s for s in admin.get("/api/sources").json() if s["id"] == source)
    assert len(view["profiles"]) == 1 and view["profiles"][0]["squelch"] == 120
    assert view["profiles"][0]["measured_at"] is None  # typed in, not measured
    assert admin.delete(path).status_code == 200
    assert (running_spec(ctx, "SER1")["squelch"], running_spec(ctx, "SER1")["gain_db"]) == (50, 10.0)
    assert admin.delete(path).status_code == 404


def test_retuning_a_target_removes_profiles_but_renaming_keeps_them(admin, ctx):
    source, target = local_setup(admin, ctx)
    admin.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 98})
    body = {"frequency_hz": 441_000_000, "modulation": "nfm", "squelch": 50}
    reply = admin.put(f"/api/targets/{target}", json={"name": "Renamed", **body}).json()
    assert reply["profiles_cleared"] == 0
    reply = admin.put(f"/api/targets/{target}", json={"name": "Renamed", **{**body, "frequency_hz": 444_850_000}}).json()
    assert reply["profiles_cleared"] == 1
    assert next(s for s in admin.get("/api/sources").json() if s["id"] == source)["profiles"] == []


def test_deleting_a_source_or_target_removes_its_profiles(admin, ctx):
    source, target = local_setup(admin, ctx)
    admin.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 98})
    admin.delete(f"/api/targets/{target}")
    with ctx.db() as session:
        assert session.scalar(select(SdrProfile)) is None
    target = admin.post("/api/targets", json={"name": "T", "frequency_hz": 441_000_000, "modulation": "nfm"}).json()["id"]
    admin.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 98})
    admin.delete(f"/api/sources/{source}")
    with ctx.db() as session:
        assert session.scalar(select(SdrProfile)) is None


@pytest.mark.parametrize("squelch", [-1, 100001])
def test_profile_values_are_validated(admin, ctx, squelch):
    source, target = local_setup(admin, ctx)
    assert admin.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": squelch}).status_code == 422
    assert admin.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 1, "gain_db": 99}).status_code == 422


def test_only_audio_targets_have_profiles(admin, ctx):
    source, _ = local_setup(admin, ctx)
    adsb = admin.post("/api/targets", json={"name": "A", "kind": "adsb", "frequency_hz": 1_090_000_000, "modulation": "adsb"}).json()["id"]
    assert admin.put(f"/api/sources/{source}/profiles/{adsb}", json={"squelch": 9}).status_code == 422
    assert admin.post(f"/api/sources/{source}/measure", json={"target_id": adsb}).status_code == 422


def fake_supervisor(ctx, result=None, error=None):
    calls = []

    class Fake:
        async def measure(self, serial, **request):
            calls.append((serial, request))
            if error:
                raise MeasureError(error)
            return result or {"low": 61, "typical": 62, "high": 70, "suggested": 98, "moved": False}

    ctx.supervisor = Fake()
    return calls


def test_measure_uses_the_targets_frequency_and_sources_ppm(admin, ctx):
    source = admin.post("/api/sources", json={"serial": "SER1", "name": "S", "ppm": 4}).json()["id"]
    target = admin.post("/api/targets", json={"name": "T", "frequency_hz": 444_850_000, "modulation": "wfm"}).json()["id"]
    calls = fake_supervisor(ctx)
    reply = admin.post(f"/api/sources/{source}/measure", json={"target_id": target, "gain_db": 19.2})
    assert reply.status_code == 200 and reply.json()["suggested"] == 98 and reply.json()["gain_db"] == 19.2
    assert calls == [("SER1", {"frequency_hz": 444_850_000, "modulation": "wfm", "gain_db": 19.2, "ppm": 4})]
    assert any(e["action"] == "source.measured" for e in admin.get("/api/admin/audit").json())


def test_measure_failure_is_explained(admin, ctx):
    source, target = local_setup(admin, ctx)
    fake_supervisor(ctx, error="This SDR isn't connected, or its serial is duplicated.")
    reply = admin.post(f"/api/sources/{source}/measure", json={"target_id": target})
    assert reply.status_code == 409 and "isn't connected" in reply.json()["detail"]


def test_measure_is_rate_limited_per_user(admin, ctx):
    source, target = local_setup(admin, ctx)
    fake_supervisor(ctx)
    codes = [admin.post(f"/api/sources/{source}/measure", json={"target_id": target}).status_code for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


def test_listeners_cannot_measure_or_save_profiles(admin, ctx, app):
    source, target = local_setup(admin, ctx)
    calls = fake_supervisor(ctx)
    add_user(ctx, "listener@example.com")
    client = client_for(app, "listener@example.com")
    assert client.post(f"/api/sources/{source}/measure", json={"target_id": target}).status_code == 403
    assert client.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 1}).status_code == 403
    assert client.delete(f"/api/sources/{source}/profiles/{target}").status_code == 403
    anonymous = client_for(app)
    assert anonymous.post(f"/api/sources/{source}/measure", json={"target_id": target}).status_code == 401
    assert anonymous.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 1}).status_code == 401
    assert calls == []


def test_a_lock_covers_measuring_and_profiles_for_everyone_but_admins(admin, ctx, app):
    source, target = local_setup(admin, ctx)
    admin.post(f"/api/sources/{source}/assign", json={"target_id": target})
    admin.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 98})
    lock(admin, source)
    calls = fake_supervisor(ctx)
    add_user(ctx, "op@example.com", role="operator")
    operator = client_for(app, "op@example.com")
    assert operator.post(f"/api/sources/{source}/measure", json={"target_id": target}).status_code == 403
    assert operator.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 1}).status_code == 403
    assert operator.delete(f"/api/sources/{source}/profiles/{target}").status_code == 403
    assert calls == [] and running_spec(ctx, "SER1")["squelch"] == 98
    assert admin.post(f"/api/sources/{source}/measure", json={"target_id": target}).status_code == 200
    assert admin.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 110}).status_code == 200


def test_operator_can_measure_and_save_on_an_unlocked_source(admin, ctx, app):
    source, target = local_setup(admin, ctx)
    fake_supervisor(ctx)
    add_user(ctx, "op@example.com", role="operator")
    operator = client_for(app, "op@example.com")
    assert operator.post(f"/api/sources/{source}/measure", json={"target_id": target}).status_code == 200
    assert operator.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 98}).status_code == 200


@pytest.mark.parametrize("bad", [
    {"modulation": "adsb"}, {"modulation": "nfm; rm -rf /"}, {"frequency_hz": 1}, {"frequency_hz": "abc"},
    {"gain_db": 99}, {"gain_db": "x"}, {"ppm": 9999}, {"frequency_hz": None},
])
def test_measure_requests_from_the_network_are_validated(bad):
    good = {"frequency_hz": 441_000_000, "modulation": "nfm", "gain_db": 19.2, "ppm": 0}
    assert pipelines.sanitize_measure(good)["gain_db"] == 19.2
    with pytest.raises(pipelines.SpecError):
        pipelines.sanitize_measure({**good, **bad})


def pending_future(ctx, node_id=7, op="measure"):
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    ctx.pending_measures["abc"] = (node_id, future, op)
    return loop, future


GOOD_RESULT = {"low": 61, "typical": 62, "high": 70, "suggested": 98, "moved": False}


def test_a_node_cannot_answer_another_nodes_measurement(ctx):
    loop, future = pending_future(ctx)
    ctx.finish_measure(8, {"id": "abc", "ok": True, "result": GOOD_RESULT})
    ctx.finish_measure(7, {"id": "other", "ok": True, "result": GOOD_RESULT})
    assert not future.done()
    ctx.finish_measure(7, {"id": "abc", "ok": True, "result": GOOD_RESULT})
    assert future.result()["suggested"] == 98
    loop.close()


@pytest.mark.parametrize("message", [{"ok": True, "result": {"low": "x"}}, {"ok": True}, {"ok": True, "result": 5}, {"ok": False, "error": "busy"}])
def test_bad_measurement_replies_become_errors(ctx, message):
    loop, future = pending_future(ctx)
    ctx.finish_measure(7, {"id": "abc", **message})
    assert isinstance(future.exception(), MeasureError)
    loop.close()


def test_detect_reply_from_a_node_is_cleaned(ctx):
    loop, future = pending_future(ctx, op="detect")
    ctx.finish_measure(7, {"id": "abc", "ok": True, "result": {
        "tuner": "R820T" * 20, "gain_steps": [0.0, 49.6, 900, 49.6], "freq_min_hz": 24_000_000, "freq_max_hz": -5}})
    assert future.result() == {"tuner": ("R820T" * 20)[:40], "gain_steps": [0.0, 49.6], "freq_min_hz": 24_000_000, "freq_max_hz": None}
    loop.close()


@pytest.mark.parametrize("result", [5, {"gain_steps": ["x"]}, {"gain_steps": 7}])
def test_bad_detect_replies_become_errors(ctx, result):
    loop, future = pending_future(ctx, op="detect")
    ctx.finish_measure(7, {"id": "abc", "ok": True, "result": result})
    assert isinstance(future.exception(), MeasureError)
    loop.close()


# --- stick capabilities -------------------------------------------------------
R820T_OUTPUT = """Found 3 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 12345678
Using device 2: Generic RTL2832U OEM
Found Rafael Micro R820T tuner
Supported gain values (29): 0.0 0.9 1.4 2.7 3.7 7.7 8.7 12.5 14.4 15.7 16.6 19.7 20.7 22.9 25.4 28.0 29.7 32.8 33.8 36.4 37.2 38.6 40.2 42.1 43.4 43.9 44.5 48.0 49.6 
No E4000 tuner found, aborting.
"""


def test_rtl_test_output_parsed_from_real_sticks():
    found = capabilities.parse_rtl_test(R820T_OUTPUT)
    assert found["tuner"] == "R820T" and len(found["gain_steps"]) == 29 and found["gain_steps"][-1] == 49.6
    assert (found["freq_min_hz"], found["freq_max_hz"]) == (24_000_000, 1_766_000_000)
    fc = capabilities.parse_rtl_test("Found Fitipower FC0012 tuner\nSupported gain values (5): -9.9 -4.0 7.1 17.9 19.2 \n")
    assert fc["tuner"] == "FC0012" and fc["gain_steps"] == [-9.9, -4.0, 7.1, 17.9, 19.2] and fc["freq_max_hz"] == 948_600_000
    assert capabilities.parse_rtl_test("usb_open error -3")["tuner"] == ""
    assert capabilities.parse_rtl_test("Found Mystery X9 tuner")["freq_min_hz"] is None


def test_nearest_gain_step():
    steps = [0.0, 19.7, 40.2, 49.6]
    assert capabilities.nearest_step(steps, 40.2) is None and capabilities.nearest_step(steps, 40) == 40.2
    assert capabilities.nearest_step([], 40) is None and capabilities.nearest_step(steps, None) is None


def stick(admin, **extra):
    return make_source(admin, tuner="FC0012", gain_steps=[-9.9, -4.0, 7.1, 17.9, 19.2],
                       cap_min_hz=22_000_000, cap_max_hz=948_600_000, uses=["voice", "adsb"], gain_db=19.2, **extra)


def test_incompatible_targets_warn_but_still_assign(admin):
    source = stick(admin)
    ok = make_target(admin, "Fine", 444_850_000)
    high = make_target(admin, "GPS", 1_575_420_000)
    dmr = make_target(admin, "DMR", 441_000_000, modulation="dmr")
    ais = make_target(admin, "Ships", 162_000_000, kind="ais", modulation="ais")
    assert admin.post(f"/api/sources/{source}/assign", json={"target_id": ok}).json()["warning"] is None
    for target, text in ((high, "outside the stick's frequency range"), (dmr, "needs Digital voice"), (ais, "needs AIS")):
        reply = admin.post(f"/api/sources/{source}/assign", json={"target_id": target})
        assert reply.status_code == 200 and text in reply.json()["warning"], reply.text
    listed = next(s for s in admin.get("/api/sources").json() if s["id"] == source)
    assert listed["base_target_id"] == ais and listed["tuner"] == "FC0012" and listed["uses"] == ["adsb", "voice"]


def test_gain_must_be_a_step_once_steps_are_known(admin):
    steps = [7.1, 17.9, 19.2]
    refused = admin.post("/api/sources", json={"serial": "SER1", "name": "S", "gain_steps": steps, "gain_db": 18})
    assert refused.status_code == 422 and "nearest is 17.9 dB" in refused.text
    source = make_source(admin, gain_steps=steps, gain_db=17.9)
    assert admin.post("/api/sources", json={"serial": "SER2", "name": "T", "gain_steps": steps}).status_code == 201  # AGC is fine
    target = make_target(admin)
    assert admin.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 80, "gain_db": 10}).status_code == 422
    saved = admin.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 80, "gain_db": 7.1})
    assert saved.status_code == 200 and saved.json()["warning"] is None
    assert admin.post(f"/api/sources/{source}/assign", json={"target_id": target}).json()["warning"] is None


def test_a_gain_saved_before_detection_can_stay(admin):
    source = make_source(admin, gain_db=40.2)  # no steps known yet
    target = make_target(admin)
    admin.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 80, "gain_db": 33}).raise_for_status()
    body = {"serial": "SER1", "name": "S", "gain_steps": [7.1, 17.9, 19.2]}
    kept = admin.put(f"/api/sources/{source}", json={**body, "gain_db": 40.2})
    assert kept.status_code == 200 and "will use 19.2 dB" in kept.json()["warning"]  # detected later: still saves
    assert admin.put(f"/api/sources/{source}", json={**body, "gain_db": 30}).status_code == 422  # but not a new off-step value
    assert admin.put(f"/api/sources/{source}/profiles/{target}", json={"squelch": 90, "gain_db": 33}).status_code == 200
    assert "so it will get 19.2 dB" in admin.post(f"/api/sources/{source}/assign", json={"target_id": target}).json()["warning"]


def test_unset_capabilities_are_not_judged(admin):
    source = make_source(admin)
    target = make_target(admin, "Ships", 162_000_000, kind="ais", modulation="ais")
    assert admin.post(f"/api/sources/{source}/assign", json={"target_id": target}).json()["warning"] is None


def test_broadcast_fm_counts_for_wfm(admin):
    source = make_source(admin, uses=["broadcast_fm"])
    target = make_target(admin, "Radio", 98_900_000, modulation="wfm")
    assert admin.post(f"/api/sources/{source}/assign", json={"target_id": target}).json()["warning"] is None


@pytest.mark.parametrize("bad", [{"uses": ["teleport"]}, {"gain_steps": [99]}, {"cap_min_hz": 500, "cap_max_hz": 100}, {"tuner": "x" * 41}])
def test_capability_validation(admin, bad):
    assert admin.post("/api/sources", json={"serial": "SER1", "name": "S", **bad}).status_code == 422


def test_detect_is_admin_only_and_checks_the_serial(admin, operator):
    assert operator.post("/api/sources/detect", json={"serial": "SER1"}).status_code == 403
    assert admin.post("/api/sources/detect", json={"serial": "NOPE"}).status_code == 404


def test_ui_lists_the_same_uses():
    lib = (Path(__file__).resolve().parent.parent / "web" / "src" / "lib.js").read_text()
    for key in capabilities.USES:
        assert f"['{key}'," in lib


# --- Broadcastify Calls -------------------------------------------------------
BCFY_KEY = "bcfy-secret-key-123"
SIGNED = "https://uploads.example.com/call/abc?signature=xyz"


def bcfy_target(admin, slot=3, **overrides):
    system = admin.post("/api/admin/broadcastify", json={"name": "Home", "system_id": 4242, "api_key": BCFY_KEY})
    assert system.status_code == 201
    target = admin.post("/api/targets", json={
        "name": "Fire Dispatch", "frequency_hz": 154_430_000, "modulation": "nfm",
        "broadcastify_enabled": True, "broadcastify_system_ref": system.json()["id"], "broadcastify_slot": slot, **overrides,
    })
    assert target.status_code == 201, target.text
    return system.json()["id"], target.json()["id"]


def bcfy_recording(ctx, target_id, tmp_path, started=1_800_000_000.0, duration=6.5, modulation="nfm"):
    """A recording stored as a real call, with audio ffmpeg can read."""
    wav = tmp_path / f"{uuid.uuid4().hex}.mp3"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ar", "16000", "-ac", "1", str(wav)], check=True)
    meta = {
        "id": uuid.uuid4().hex, "recorder_id": 1, "recorder_name": "r", "source_id": None, "source_name": "s", "target_id": target_id,
        "target_name": "Fire Dispatch", "serial": "SER1", "frequency_hz": 154_430_000, "modulation": modulation,
        "started_at": started, "ended_at": started + duration, "duration": duration,
    }
    assert ctx.store_call(meta, wav, "local")
    return meta["id"]


def bcfy_transport(seen, reply=f"0 {SIGNED}", put_status=200, post_status=200):
    def handler(request):
        seen.append(request)
        request.read()
        if request.method == "POST":
            return httpx.Response(post_status, text=reply)
        return httpx.Response(put_status)
    return httpx.MockTransport(handler)


async def run_bcfy(ctx, seen, **kwargs):
    async with httpx.AsyncClient(transport=bcfy_transport(seen, **kwargs)) as client:
        return await ctx.process_uploads(client)


def bcfy_job(ctx, call_id):
    with ctx.db() as session:
        return session.scalar(select(UploadJob).where(UploadJob.recording_id == call_id, UploadJob.destination == "broadcastify"))


def form_fields(request):
    """The name, filename and content of each multipart part."""
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    parts = {}
    for part in request.content.split(b"--" + boundary):
        head, _, body = part.partition(b"\r\n\r\n")
        if b"name=" in head:
            name = head.split(b'name="')[1].split(b'"')[0].decode()
            parts[name] = {"head": head.decode(), "body": body.rstrip(b"\r\n").decode()}
    return parts


async def test_broadcastify_registers_then_puts_aac_audio(admin, ctx, tmp_path):
    _, target = bcfy_target(admin)
    call = bcfy_recording(ctx, target, tmp_path)
    seen = []
    assert await run_bcfy(ctx, seen) == 1
    post, put = seen
    assert post.method == "POST" and str(post.url) == "https://api.broadcastify.com/call-upload"
    fields = form_fields(post)
    assert fields["systemId"]["body"] == "4242" and fields["apiKey"]["body"] == BCFY_KEY
    assert fields["callDuration"]["body"] == "6.500"
    assert fields["ts"]["body"] == "1800000000" and fields["tg"]["body"] == "3" and fields["src"]["body"] == "0"
    assert fields["freq"]["body"] == "154.430000" and fields["enc"]["body"] == "m4a"
    assert 'filename="call_meta.json"' in fields["metadata"]["head"] and "application/json" in fields["metadata"]["head"]
    meta = json.loads(fields["metadata"]["body"])
    assert meta["talkgroup"] == 3 and meta["freq"] == 154_430_000 and meta["audio_type"] == "analog" and meta["encrypted"] == 0
    assert meta["stop_time"] == 1_800_000_006 and meta["call_length_ms"] == 6500
    assert meta["stop_time_ms"] - meta["start_time_ms"] == 6500 and meta["talkgroup_tag"] == "Fire Dispatch"
    assert put.method == "PUT" and str(put.url) == SIGNED and put.headers["content-type"] == "audio/aac"
    assert b"ftyp" in put.content[:32]  # an m4a file
    assert "trunk" not in post.headers["user-agent"].lower()
    job = bcfy_job(ctx, call)
    assert job.status == "done" and job.last_error == ""


async def test_broadcastify_skipped_counts_as_done_without_uploading(admin, ctx, tmp_path):
    _, target = bcfy_target(admin)
    call = bcfy_recording(ctx, target, tmp_path)
    seen = []
    await run_bcfy(ctx, seen, reply="1 SKIPPED-DUPLICATE")
    job = bcfy_job(ctx, call)
    assert len(seen) == 1 and job.status == "done" and "SKIPPED" in job.last_error


async def test_broadcastify_rejected_fails_at_once_with_the_reason(admin, ctx, tmp_path):
    _, target = bcfy_target(admin)
    call = bcfy_recording(ctx, target, tmp_path)
    await run_bcfy(ctx, [], reply="1 REJECTED-CALL-SKEW")
    job = bcfy_job(ctx, call)
    assert job.status == "failed" and "REJECTED-CALL-SKEW" in job.last_error and job.attempts == 1
    assert await run_bcfy(ctx, []) == 0  # not tried again
    assert "REJECTED-CALL-SKEW" in admin.get("/api/recordings").json()["items"][0]["uploads"][0]["detail"]


@pytest.mark.parametrize("kwargs", [
    {"reply": "7 SOMETHING-ELSE"}, {"post_status": 500}, {"put_status": 403}, {"reply": "0 http://plain.example.com/x"}, {"reply": "0"},
])
async def test_broadcastify_other_failures_retry_later(admin, ctx, tmp_path, kwargs):
    _, target = bcfy_target(admin)
    call = bcfy_recording(ctx, target, tmp_path)
    await run_bcfy(ctx, [], **kwargs)
    job = bcfy_job(ctx, call)
    assert job.status == "pending" and job.attempts == 1 and job.next_attempt_at > time.time() and job.last_error


async def test_broadcastify_never_stores_the_key_or_signed_address(admin, ctx, tmp_path):
    _, target = bcfy_target(admin)
    call = bcfy_recording(ctx, target, tmp_path)
    await run_bcfy(ctx, [], reply=f"7 bad {BCFY_KEY} see {SIGNED}")
    job = bcfy_job(ctx, call)
    assert BCFY_KEY not in job.last_error and "signature" not in job.last_error and "[key]" in job.last_error


async def test_broadcastify_only_uses_the_targets_system_and_slot(admin, ctx, tmp_path):
    bcfy_target(admin, slot=3)
    second = admin.post("/api/admin/broadcastify", json={"name": "Field", "system_id": 77, "api_key": "other-key"}).json()["id"]
    target = admin.post("/api/targets", json={
        "name": "Other", "frequency_hz": 441_000_000, "modulation": "nfm",
        "broadcastify_enabled": True, "broadcastify_system_ref": second, "broadcastify_slot": 9,
    }).json()["id"]
    bcfy_recording(ctx, target, tmp_path)
    seen = []
    await run_bcfy(ctx, seen)
    fields = form_fields(seen[0])
    assert fields["systemId"]["body"] == "77" and fields["apiKey"]["body"] == "other-key"
    assert json.loads(fields["metadata"]["body"])["talkgroup"] == 9


def test_target_without_broadcastify_queues_no_job(admin, ctx, tmp_path):
    target = admin.post("/api/targets", json={"name": "T", "frequency_hz": 441_000_000, "modulation": "nfm"}).json()["id"]
    assert bcfy_job(ctx, bcfy_recording(ctx, target, tmp_path)) is None


@pytest.mark.parametrize("bad", [
    {"broadcastify_system_ref": None}, {"broadcastify_slot": None}, {"broadcastify_system_ref": 99}, {"broadcastify_slot": 0},
])
def test_broadcastify_target_settings_are_validated(admin, bad):
    system = admin.post("/api/admin/broadcastify", json={"name": "H", "system_id": 1, "api_key": "k"}).json()["id"]
    body = {"name": "T", "frequency_hz": 441_000_000, "modulation": "nfm", "broadcastify_enabled": True,
            "broadcastify_system_ref": system, "broadcastify_slot": 1}
    assert admin.post("/api/targets", json={**body, **bad}).status_code == 422


def test_adsb_target_cannot_upload_to_broadcastify(admin):
    system = admin.post("/api/admin/broadcastify", json={"name": "H", "system_id": 1, "api_key": "k"}).json()["id"]
    assert admin.post("/api/targets", json={
        "name": "A", "kind": "adsb", "frequency_hz": 1_090_000_000, "modulation": "adsb",
        "broadcastify_enabled": True, "broadcastify_system_ref": system, "broadcastify_slot": 1,
    }).status_code == 422


def test_broadcastify_keys_are_sealed_and_never_returned(admin, ctx, app):
    system, _ = bcfy_target(admin)
    with ctx.db() as session:
        assert BCFY_KEY not in session.get(BroadcastifySystem, system).api_key_sealed
    add_user(ctx, "op@example.com", role="operator")
    operator = client_for(app, "op@example.com")
    for reply in (admin.get("/api/admin/broadcastify"), operator.get("/api/broadcastify-systems"), admin.get("/api/targets"), admin.get("/api/admin/audit")):
        assert reply.status_code == 200 and BCFY_KEY not in reply.text
    assert operator.get("/api/broadcastify-systems").json()[0]["system_id"] == 4242


def test_only_admins_manage_broadcastify_systems(admin, ctx, app):
    system, _ = bcfy_target(admin)
    add_user(ctx, "op@example.com", role="operator")
    add_user(ctx, "l@example.com")
    operator, listener = client_for(app, "op@example.com"), client_for(app, "l@example.com")
    for client in (operator, listener):
        assert client.get("/api/admin/broadcastify").status_code == 403
        assert client.post("/api/admin/broadcastify", json={"name": "x", "system_id": 1, "api_key": "k"}).status_code == 403
        assert client.delete(f"/api/admin/broadcastify/{system}").status_code == 403
        assert client.post(f"/api/admin/broadcastify/{system}/test").status_code == 403
    assert listener.get("/api/broadcastify-systems").status_code == 403
    assert client_for(app).get("/api/broadcastify-systems").status_code == 401


def test_editing_a_system_keeps_its_key_unless_replaced(admin, ctx):
    system, _ = bcfy_target(admin)
    assert admin.put(f"/api/admin/broadcastify/{system}", json={"name": "New", "system_id": 5}).status_code == 200
    with ctx.db() as session:
        assert ctx.secrets.open(session.get(BroadcastifySystem, system).api_key_sealed) == BCFY_KEY
    admin.put(f"/api/admin/broadcastify/{system}", json={"name": "New", "system_id": 5, "api_key": "replaced"})
    with ctx.db() as session:
        assert ctx.secrets.open(session.get(BroadcastifySystem, system).api_key_sealed) == "replaced"
    assert admin.post("/api/admin/broadcastify", json={"name": "No key", "system_id": 1}).status_code == 422


def test_a_system_in_use_cannot_be_deleted(admin):
    system, target = bcfy_target(admin)
    assert admin.delete(f"/api/admin/broadcastify/{system}").status_code == 409
    admin.delete(f"/api/targets/{target}")
    assert admin.delete(f"/api/admin/broadcastify/{system}").status_code == 200


def test_credential_check_sends_no_call_and_hides_the_key(admin, ctx):
    system, _ = bcfy_target(admin)
    seen = []

    def handler(request):
        request.read()
        seen.append(request)
        return httpx.Response(200, text=f"100 bad key {BCFY_KEY} {SIGNED}")

    ctx.http_transport = httpx.MockTransport(handler)
    reply = admin.post(f"/api/admin/broadcastify/{system}/test")
    assert reply.status_code == 200 and "100 bad key" in reply.json()["reply"]
    assert BCFY_KEY not in reply.text and "signature" not in reply.text
    fields = form_fields(seen[0])
    assert fields["test"]["body"] == "1" and fields["systemId"]["body"] == "4242" and "metadata" not in fields
    assert len(seen) == 1


def test_a_database_from_before_broadcastify_upgrades_and_keeps_its_targets(tmp_path):
    db = tmp_path / "old.db"
    make_session_factory(db)
    connection = sqlite3.connect(db)
    connection.execute("INSERT INTO targets (name, kind, frequency_hz, modulation, squelch, clearcut_enabled, rdio_enabled) VALUES ('Old', 'audio', 441000000, 'nfm', 60, 0, 0)")
    for column in ("broadcastify_enabled", "broadcastify_system_ref", "broadcastify_slot"):
        connection.execute(f"ALTER TABLE targets DROP COLUMN {column}")
    connection.execute("DROP TABLE broadcastify_systems")
    connection.commit()
    connection.close()
    with make_session_factory(db)() as session:
        target = session.scalar(select(Target))
        assert target.name == "Old" and not target.broadcastify_enabled and target.broadcastify_slot is None
        session.add(BroadcastifySystem(name="n", system_id=1, api_key_sealed="x"))
        session.commit()


# --- one bad call must not stop the others -----------------------------------
async def run_replies(ctx, replies):
    answers = iter(replies)

    def handler(request):
        request.read()
        if request.method == "PUT":
            return httpx.Response(200)
        reply = next(answers)
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, text=reply)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await ctx.process_uploads(client)


@pytest.mark.parametrize("bad", [
    "1 REJECTED-CALL-SKEW", "0 https://[bad-address", "garbage", "", "\x00\xff", RuntimeError("anything"), ValueError("anything"),
])
async def test_a_bad_reply_never_holds_up_the_next_call(admin, ctx, tmp_path, bad):
    _, target = bcfy_target(admin)
    first = bcfy_recording(ctx, target, tmp_path, started=1_800_000_000.0)
    second = bcfy_recording(ctx, target, tmp_path, started=1_800_000_100.0)
    await run_replies(ctx, [bad, f"0 {SIGNED}"])
    assert bcfy_job(ctx, second).status == "done"
    job = bcfy_job(ctx, first)
    assert job.status in ("failed", "pending") and job.last_error and job.attempts == 1


async def test_unexpected_errors_stop_retrying_after_the_limit(admin, ctx, tmp_path, monkeypatch):
    _, target = bcfy_target(admin)
    call = bcfy_recording(ctx, target, tmp_path)

    async def explode(*args, **kwargs):
        raise RuntimeError("secret detail https://signed.example.com")

    monkeypatch.setattr(ctx, "_attempt_upload", explode)
    async with httpx.AsyncClient(transport=bcfy_transport([])) as client:
        for _ in range(UPLOAD_MAX_ATTEMPTS):
            with ctx.db() as session:
                session.get(UploadJob, bcfy_job(ctx, call).id).next_attempt_at = 0
                session.commit()
            await ctx.process_uploads(client)
    job = bcfy_job(ctx, call)
    assert job.status == "failed" and job.last_error == "Unexpected error: RuntimeError"


async def test_a_rejected_call_leaves_the_recording_usable(admin, ctx, tmp_path):
    _, target = bcfy_target(admin)
    call = bcfy_recording(ctx, target, tmp_path)
    await run_replies(ctx, ["1 REJECTED"])
    assert admin.get(f"/api/recordings/{call}/audio").status_code == 200
    assert admin.get("/api/recordings").json()["items"][0]["id"] == call


# --- duplicate channels -------------------------------------------------------
def second_slot_body(system, slot=3, frequency=155_000_000, name="Other"):
    return {"name": name, "frequency_hz": frequency, "modulation": "nfm", "broadcastify_enabled": True,
            "broadcastify_system_ref": system, "broadcastify_slot": slot}


def test_a_slot_can_only_belong_to_one_target(admin):
    system, target = bcfy_target(admin, slot=3)
    reply = admin.post("/api/targets", json=second_slot_body(system))
    assert reply.status_code == 409 and "Fire Dispatch" in reply.json()["detail"]
    assert admin.post("/api/targets", json=second_slot_body(system, slot=4)).status_code == 201
    other = admin.post("/api/admin/broadcastify", json={"name": "B", "system_id": 2, "api_key": "k"}).json()["id"]
    assert admin.post("/api/targets", json=second_slot_body(other, slot=3)).status_code == 201


def test_a_target_can_keep_its_own_slot_but_not_take_another(admin):
    system, target = bcfy_target(admin, slot=3)
    keep = second_slot_body(system, frequency=154_430_000, name="Fire Dispatch")
    assert admin.put(f"/api/targets/{target}", json=keep).status_code == 200
    other = admin.post("/api/targets", json=second_slot_body(system, slot=4)).json()["id"]
    assert admin.put(f"/api/targets/{other}", json=second_slot_body(system, slot=3)).status_code == 409


def test_a_disabled_target_does_not_hold_its_slot(admin):
    system, target = bcfy_target(admin, slot=3)
    assert admin.put(f"/api/targets/{target}", json={**second_slot_body(system, frequency=154_430_000), "broadcastify_enabled": False}).status_code == 200
    assert admin.post("/api/targets", json=second_slot_body(system, name="New")).status_code == 201


def test_same_frequency_and_mode_warns_but_saves(admin):
    body = {"name": "A", "frequency_hz": 441_000_000, "modulation": "nfm"}
    first = admin.post("/api/targets", json=body).json()
    assert first["warning"] is None
    second = admin.post("/api/targets", json={**body, "name": "B"})
    assert second.status_code == 201 and "A" in second.json()["warning"]
    assert admin.post("/api/targets", json={**body, "name": "C", "modulation": "am"}).json()["warning"] is None
    assert admin.put(f"/api/targets/{first['id']}", json={**body, "name": "A", "frequency_hz": 442_000_000}).json()["warning"] is None


def test_assigning_a_channel_another_sdr_records_warns(admin, ctx):
    one = make_source(admin, "SER1", "Stick one")
    two = make_source(admin, "SER2", "Stick two")
    target = admin.post("/api/targets", json={"name": "T", "frequency_hz": 441_000_000, "modulation": "nfm"}).json()["id"]
    assert admin.post(f"/api/sources/{one}/assign", json={"target_id": target}).json()["warning"] is None
    reply = admin.post(f"/api/sources/{two}/assign", json={"target_id": target})
    assert reply.status_code == 200 and "Stick one" in reply.json()["warning"]
    assert admin.post(f"/api/sources/{one}/assign", json={"target_id": target}).json()["warning"]  # names the other stick
    assert ctx.assignments()[ctx.local_node_id]["SER2"]["target_id"] == target


def overlap_call(ctx, target, tmp_path, serial, started, node="local", duration=6.0):
    wav = tmp_path / f"{uuid.uuid4().hex}.mp3"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ar", "16000", "-ac", "1", str(wav)], check=True)
    call = uuid.uuid4().hex
    assert ctx.store_call({
        "id": call, "target_id": target, "target_name": "T", "serial": serial, "frequency_hz": 154_430_000, "modulation": "nfm",
        "started_at": started, "ended_at": started + duration, "duration": duration,
    }, wav, node)
    return call


async def test_only_the_first_sdrs_copy_of_a_call_is_uploaded(admin, ctx, tmp_path):
    _, target = bcfy_target(admin)
    first = overlap_call(ctx, target, tmp_path, "SER1", 1_800_000_000.0)
    second = overlap_call(ctx, target, tmp_path, "SER2", 1_800_000_000.4)
    from_slave = overlap_call(ctx, target, tmp_path, "SER1", 1_800_000_001.0, node="pi")
    seen = []
    await run_bcfy(ctx, seen)
    assert len([r for r in seen if r.method == "POST"]) == 1
    assert bcfy_job(ctx, first).last_error == ""
    for call in (second, from_slave):
        job = bcfy_job(ctx, call)
        assert job.status == "done" and job.last_error.startswith("Skipped")
    assert len(admin.get("/api/recordings").json()["items"]) == 3  # every recording is kept


def test_separate_calls_and_the_same_stick_are_never_treated_as_duplicates(admin, ctx, tmp_path):
    _, target = bcfy_target(admin)
    calls = [
        overlap_call(ctx, target, tmp_path, "SER1", 1_800_000_000.0),
        overlap_call(ctx, target, tmp_path, "SER2", 1_800_000_006.0),  # starts as the last one ends
        overlap_call(ctx, target, tmp_path, "SER2", 1_800_000_009.0),  # same stick, overlapping
    ]
    assert all(bcfy_job(ctx, call).status == "pending" for call in calls)


def test_a_different_channel_at_the_same_time_is_not_a_duplicate(admin, ctx, tmp_path):
    system, one = bcfy_target(admin)
    two = admin.post("/api/targets", json=second_slot_body(system, slot=4)).json()["id"]
    overlap_call(ctx, one, tmp_path, "SER1", 1_800_000_000.0)
    assert bcfy_job(ctx, overlap_call(ctx, two, tmp_path, "SER2", 1_800_000_000.0)).status == "pending"


def test_a_skipped_clearcut_copy_is_not_matched_for_a_transcript(admin, ctx, tmp_path):
    target = admin.post("/api/targets", json={
        "name": "T", "frequency_hz": 154_430_000, "modulation": "nfm", "clearcut_enabled": True, "clearcut_system": "s", "clearcut_id": 5,
    }).json()["id"]
    overlap_call(ctx, target, tmp_path, "SER1", 1_800_000_000.0)
    second = overlap_call(ctx, target, tmp_path, "SER2", 1_800_000_000.0)
    with ctx.db() as session:
        assert session.get(Recording, second).clearcut_system is None


# --- volume -------------------------------------------------------------------
def tone(level, count=8000):
    return array("h", [level if i % 40 < 20 else -level for i in range(count)])


def test_zero_db_leaves_audio_byte_for_byte():
    chunk = tone(1000).tobytes()
    assert VolumeControl()(chunk, 0) is chunk
    assert VolumeControl()(chunk, 0.0) is chunk


@pytest.mark.parametrize("db,factor", [(6.0206, 2.0), (12.0412, 4.0), (-6.0206, 0.5)])
def test_volume_scales_audio(db, factor):
    out = array("h")
    out.frombytes(VolumeControl()(tone(1000).tobytes(), db))
    rms = lambda a: (sum(x * x for x in a) / len(a)) ** 0.5
    assert rms(out[4000:]) == pytest.approx(rms(tone(1000)[4000:]) * factor, rel=0.05)


def test_boost_removes_a_dc_step_instead_of_clipping_it():
    samples = array("h", [8000 + (500 if i % 40 < 20 else -500) for i in range(16000)])
    out = array("h")
    out.frombytes(VolumeControl()(samples.tobytes(), 12))
    settled = out[2000:]
    assert abs(sum(settled) / len(settled)) < 100
    assert max(settled) < 32767 and min(settled) > -32768


def test_boost_limits_at_full_scale_without_wrapping():
    out = array("h")
    out.frombytes(VolumeControl()(tone(30000).tobytes(), 30))
    assert max(out) == 32767 and min(out) == -32768
    assert all(a * b >= 0 or abs(a - b) > 30000 for a, b in zip(out[2000:2019], out[2001:2020]))


def test_the_filter_carries_across_chunks():
    data = tone(1000, 8000).tobytes()
    whole = VolumeControl()(data, 6)
    control = VolumeControl()
    split = control(data[:6000], 6) + control(data[6000:], 6)
    assert whole == split


def test_volume_is_sent_to_the_radio_and_validated(admin, ctx):
    source = make_source(admin)
    target = admin.post("/api/targets", json={"name": "T", "frequency_hz": 441_000_000, "modulation": "nfm", "volume_db": 12}).json()
    admin.post(f"/api/sources/{source}/assign", json={"target_id": target["id"]})
    assert target["volume_db"] == 12 and ctx.assignments()[ctx.local_node_id]["SER1"]["volume_db"] == 12
    for bad in (-21, 31):
        assert admin.post("/api/targets", json={"name": "B", "frequency_hz": 441_000_000, "modulation": "nfm", "volume_db": bad}).status_code == 422


@pytest.mark.parametrize("bad", [{"volume_db": 31}, {"volume_db": -21}, {"volume_db": "loud"}, {"volume_db": float("nan")}])
def test_volume_from_the_network_is_validated(bad):
    with pytest.raises(pipelines.SpecError):
        spec(**bad)


def test_a_slave_without_volume_in_its_saved_assignment_still_loads():
    assert spec()["volume_db"] == 0
    assert spec(volume_db=None)["volume_db"] == 0


def test_a_database_from_before_volume_upgrades(tmp_path):
    db = tmp_path / "old.db"
    make_session_factory(db)
    connection = sqlite3.connect(db)
    connection.execute("INSERT INTO targets (name, kind, frequency_hz, modulation, squelch, clearcut_enabled, rdio_enabled) VALUES ('Old', 'audio', 441000000, 'nfm', 60, 0, 0)")
    connection.execute("ALTER TABLE targets DROP COLUMN volume_db")
    connection.commit()
    connection.close()
    with make_session_factory(db)() as session:
        assert session.scalar(select(Target)).volume_db is None


# --- from Broadcastify's published API --------------------------------------------
async def test_broadcastify_form_and_metadata_agree_on_the_call(admin, ctx, tmp_path):
    _, target = bcfy_target(admin, slot=7)
    bcfy_recording(ctx, target, tmp_path, started=1_800_000_123.75, duration=6.5)
    seen = []
    await run_bcfy(ctx, seen)
    fields = form_fields(seen[0])
    meta = json.loads(fields["metadata"]["body"])
    assert int(fields["ts"]["body"]) == meta["start_time"] == 1_800_000_123
    assert fields["tg"]["body"] == str(meta["talkgroup"]) == "7"
    assert float(fields["freq"]["body"]) * 1e6 == pytest.approx(meta["freq"])


async def test_a_bad_api_key_is_not_retried(admin, ctx, tmp_path):
    _, target = bcfy_target(admin)
    call = bcfy_recording(ctx, target, tmp_path)
    seen = []
    await run_bcfy(ctx, seen, reply="100 NO-API-KEY-SPECIFIED")
    job = bcfy_job(ctx, call)
    assert job.status == "failed" and "API key" in job.last_error and "NO-API-KEY" in job.last_error
    assert len(seen) == 1 and await run_bcfy(ctx, seen) == 0


async def test_the_documented_duplicate_reply_counts_as_done(admin, ctx, tmp_path):
    _, target = bcfy_target(admin)
    call = bcfy_recording(ctx, target, tmp_path)
    await run_bcfy(ctx, [], reply="1 SKIPPED---ALREADY-RECEIVED-THIS-CALL")
    job = bcfy_job(ctx, call)
    assert job.status == "done" and job.last_error.startswith("Skipped")


def flaky_put(seen, failures):
    answers = iter(failures)

    def handler(request):
        request.read()
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, text=f"0 {SIGNED}")
        answer = next(answers, None)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer or 200)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("failures", [[503], [503, 503], [httpx.ConnectError("x")], [httpx.ReadTimeout("x"), 500]])
async def test_audio_upload_retries_the_same_address_without_registering_again(admin, ctx, tmp_path, monkeypatch, failures):
    monkeypatch.setattr(uploaders, "BROADCASTIFY_PUT_DELAY", 0)
    _, target = bcfy_target(admin)
    call = bcfy_recording(ctx, target, tmp_path)
    seen = []
    async with httpx.AsyncClient(transport=flaky_put(seen, failures)) as client:
        await ctx.process_uploads(client)
    assert [r.method for r in seen].count("POST") == 1
    assert [r.method for r in seen].count("PUT") == len(failures) + 1
    assert bcfy_job(ctx, call).status == "done"


async def test_an_expired_address_is_not_retried_and_failures_hide_the_address(admin, ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(uploaders, "BROADCASTIFY_PUT_DELAY", 0)
    _, target = bcfy_target(admin)
    call = bcfy_recording(ctx, target, tmp_path)
    seen = []
    async with httpx.AsyncClient(transport=flaky_put(seen, [403])) as client:
        await ctx.process_uploads(client)
    assert [r.method for r in seen].count("PUT") == 1
    job = bcfy_job(ctx, call)
    assert job.status == "pending" and "403" in job.last_error

    seen.clear()
    with ctx.db() as session:
        row = session.get(UploadJob, job.id)
        row.next_attempt_at = 0
        session.commit()
    secret = httpx.ConnectError(f"could not reach {SIGNED}")
    async with httpx.AsyncClient(transport=flaky_put(seen, [secret, secret, secret])) as client:
        await ctx.process_uploads(client)
    job = bcfy_job(ctx, call)
    assert "signature" not in job.last_error and "ConnectError" in job.last_error


async def test_no_frequency_entry_is_final_and_keeps_its_reason(admin, ctx, tmp_path):
    _, target = bcfy_target(admin)
    call = bcfy_recording(ctx, target, tmp_path)
    seen = []
    await run_bcfy(ctx, seen, reply="1 No-Freq-Table-Entry-For-Upload")
    job = bcfy_job(ctx, call)
    assert job.status == "failed" and "No-Freq-Table-Entry" in job.last_error and "slot" in job.last_error
    assert len(seen) == 1 and await run_bcfy(ctx, seen) == 0
    detail = admin.get("/api/recordings").json()["items"][0]["uploads"][0]["detail"]
    assert "No-Freq-Table-Entry" in detail and "SKEW" not in detail


async def test_any_other_code_1_reply_is_final(admin, ctx, tmp_path):
    _, target = bcfy_target(admin)
    call = bcfy_recording(ctx, target, tmp_path)
    await run_bcfy(ctx, [], reply="1 Something-New")
    job = bcfy_job(ctx, call)
    assert job.status == "failed" and "Something-New" in job.last_error


async def test_broadcastify_retries_inside_its_time_window(admin, ctx, tmp_path):
    _, target = bcfy_target(admin)
    call = bcfy_recording(ctx, target, tmp_path)
    for attempt in range(1, 4):
        with ctx.db() as session:
            row = session.get(UploadJob, bcfy_job(ctx, call).id)
            row.next_attempt_at = 0
            session.commit()
        await run_bcfy(ctx, [], post_status=500)
        job = bcfy_job(ctx, call)
        assert job.attempts == attempt and job.next_attempt_at - time.time() <= BROADCASTIFY_RETRY_SECONDS


async def test_other_uploads_still_back_off_slowly(admin, ctx, tmp_path):
    target = admin.post("/api/targets", json={"name": "T", "frequency_hz": 441_000_000, "modulation": "nfm",
                                              "rdio_enabled": True, "rdio_system": 1, "rdio_talkgroup": 1}).json()["id"]
    call = bcfy_recording(ctx, target, tmp_path)
    ctx.set_setting("rdio_server", "https://rdio.example.com")
    ctx.set_setting("rdio_api_key", ctx.secrets.seal("k"))
    await run_bcfy(ctx, [], post_status=500)
    with ctx.db() as session:
        job = session.scalar(select(UploadJob).where(UploadJob.recording_id == call))
    assert job.destination == "rdio" and job.next_attempt_at - time.time() > 30


# --- ACARS --------------------------------------------------------------------
NA = [130_025_000, 130_450_000, 131_125_000, 131_550_000]


def test_acars_channels_must_fit_one_stick():
    assert pipelines.acars_channel_problem(NA) is None
    assert "span 2.425 MHz" in pipelines.acars_channel_problem([129_125_000, *NA])
    assert "at least one" in pipelines.acars_channel_problem([])
    assert "118 and 137" in pipelines.acars_channel_problem([162_000_000])
    assert "at most 8" in pipelines.acars_channel_problem([130_000_000 + i * 10_000 for i in range(9)])


def test_acars_args_output_json_to_stdout_only():
    cfg = SimpleNamespace(binary=lambda name: None)
    args = pipelines.acars_args(cfg, 2, {"channels": [131_550_000, 130_025_000], "gain_db": 40.2, "ppm": 3})
    assert args == ["acarsdec", "--output", "json:file:path=-", "--rtlsdr", "2", "-g", "40.2", "-p", "3", "130.025", "131.550"]
    assert not any("udp" in a or "mqtt" in a for a in args)


def make_acars_target(client, name="ACARS", channels=NA, **extra):
    body = {"name": name, "kind": "acars", "modulation": "acars", "frequency_hz": channels[0], "acars_channels": channels, **extra}
    response = client.post("/api/targets", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_acars_target_api(admin):
    made = make_acars_target(admin, channels=[131_550_000, 130_025_000, 130_025_000])
    assert made["acars_channels"] == [130_025_000, 131_550_000] and made["frequency_hz"] == 130_025_000
    wide = admin.post("/api/targets", json={"name": "W", "kind": "acars", "modulation": "acars", "frequency_hz": 129_125_000,
                                            "acars_channels": [129_125_000, *NA]})
    assert wide.status_code == 422 and "one stick covers at most 2 MHz" in wide.text
    audio = admin.post("/api/targets", json={"name": "A", "frequency_hz": 444_000_000, "acars_channels": NA})
    assert audio.status_code == 422
    assert admin.post("/api/targets", json={"name": "A", "frequency_hz": 444_000_000, "airframes_enabled": True}).status_code == 422


def test_acars_assignment_reaches_the_spec(ctx, admin):
    source = make_source(admin)
    target = make_acars_target(admin)["id"]
    admin.post(f"/api/sources/{source}/assign", json={"target_id": target}).raise_for_status()
    spec = ctx.assignments()[ctx.local_node_id]["SER1"]
    assert spec["kind"] == "acars" and spec["channels"] == NA
    assert pipelines.sanitize_spec(spec)["channels"] == NA


def test_acars_stick_without_the_use_warns(admin):
    source = make_source(admin, uses=["voice"])
    target = make_acars_target(admin)["id"]
    assert "needs ACARS" in admin.post(f"/api/sources/{source}/assign", json={"target_id": target}).json()["warning"]


RAW = {"timestamp": 1.0, "station_id": "X", "channel": 0, "freq": 131.55, "level": -21.5, "error": 0, "mode": "2",
       "label": "H1", "block_id": "4", "ack": False, "tail": ".N123UA", "flight": "UA0123", "msgno": "M01A",
       "text": "POSN 4740N12218W", "app": {"name": "acarsdec", "ver": "4"}, "evil": "x" * 10}


class UdpCatcher:
    def __init__(self):
        import socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(3)
        self.address = f"127.0.0.1:{self.sock.getsockname()[1]}"

    def receive(self):
        return json.loads(self.sock.recv(65536))


def run_ingest(ctx, items):
    async def go():
        ctx.loop = asyncio.get_running_loop()
        for node_id, serial, data in items:
            ctx.update_tracks("acars", node_id, serial, data)
        await asyncio.sleep(0.3)
        await asyncio.gather(*ctx.tasks, return_exceptions=True)
    asyncio.run(go())


def test_acars_messages_stored_once_and_fed_to_airframes(ctx, admin):
    source = make_source(admin, name="Air stick")
    target = make_acars_target(admin, airframes_enabled=True)["id"]
    catcher = UdpCatcher()
    ctx.set_setting("airframes_feed", catcher.address)
    ctx.set_setting("airframes_station_id", "KE-KSEA-ACARS")
    data = {"message": RAW, "target_id": target}
    run_ingest(ctx, [(ctx.local_node_id, "SER1", data), (ctx.local_node_id, "SER2", data)])  # two sticks, one channel
    sent = catcher.receive()
    assert sent["station_id"] == "KE-KSEA-ACARS" and sent["text"] == RAW["text"] and sent["tail"] == ".N123UA"
    assert "evil" not in sent and sent["app"]["name"] == "SentrySDR"
    import socket
    with pytest.raises(socket.timeout):
        catcher.receive()  # the duplicate wasn't fed
    with ctx.db() as session:
        rows = session.scalars(select(AcarsMessage)).all()
    assert len(rows) == 1
    row = rows[0]
    assert (row.tail, row.flight, row.frequency_hz, row.source_name, row.target_name, row.fed) == ("N123UA", "UA0123", 131_550_000, "Air stick", "ACARS", True)


def test_acars_not_fed_without_a_station_or_when_off(ctx, admin):
    make_source(admin)
    on = make_acars_target(admin, "On", airframes_enabled=True)["id"]
    off = make_acars_target(admin, "Off", channels=[131_525_000])["id"]
    catcher = UdpCatcher()
    ctx.set_setting("airframes_feed", catcher.address)
    run_ingest(ctx, [(ctx.local_node_id, "SER1", {"message": {**RAW, "msgno": "A"}, "target_id": on})])  # no station ID yet
    ctx.set_setting("airframes_station_id", "KE-KSEA-ACARS")
    run_ingest(ctx, [(ctx.local_node_id, "SER1", {"message": {**RAW, "msgno": "B"}, "target_id": off})])
    import socket
    with pytest.raises(socket.timeout):
        catcher.receive()
    with ctx.db() as session:
        assert [m.fed for m in session.scalars(select(AcarsMessage))] == [None, None]


def test_bad_acars_input_is_cleaned(ctx, admin):
    make_source(admin)
    weird = {"freq": "nan", "level": "loud", "tail": None, "text": "a\x00b" + "x" * 5000, "label": ["H1"]}
    run_ingest(ctx, [(ctx.local_node_id, "SER1", {"message": weird, "target_id": "x"}),
                     (ctx.local_node_id, "SER1", {"message": "not a dict"})])
    with ctx.db() as session:
        row = session.scalars(select(AcarsMessage)).one()
    assert row.frequency_hz == 0 and row.level is None and row.target_id is None and len(row.text) == 4000 and "\x00" not in row.text


def add_message(ctx, **fields):
    with ctx.db() as session:
        values = {"received_at": time.time(), "tail": "N1", "flight": "UA1", "text": "HELLO", "has_text": True, **fields}
        session.add(AcarsMessage(**values))
        session.commit()


def test_messages_feed_filters(ctx, admin, listener):
    add_message(ctx, text="POSITION REPORT")
    add_message(ctx, text="", has_text=False, tail="N2")
    add_message(ctx, text="FUEL 12000", tail="N3", flight="DL22")
    got = listener.get("/api/messages").json()
    assert got["total"] == 2 and {m["tail"] for m in got["items"]} == {"N1", "N3"}
    assert listener.get("/api/messages?show_empty=true").json()["total"] == 3
    assert [m["tail"] for m in listener.get("/api/messages?search=fuel").json()["items"]] == ["N3"]
    assert [m["tail"] for m in listener.get("/api/messages?search=DL22").json()["items"]] == ["N3"]
    assert listener.get("/api/messages?tail=.N-1").json()["total"] == 1


def test_messages_need_the_maps_permission(ctx, listener, admin):
    user = next(u for u in admin.get("/api/admin/users").json() if u["role"] == "listener")
    admin.patch(f"/api/admin/users/{user['id']}", json={"perm_overrides": {"maps": False}}).raise_for_status()
    assert listener.get("/api/messages").status_code == 403


def test_old_messages_pruned(ctx):
    add_message(ctx, received_at=time.time() - 40 * 86400)
    add_message(ctx)
    assert ctx.apply_retention()["messages"] == 1


def test_map_aircraft_link_to_their_messages(ctx, app, admin, tmp_path):
    import gzip
    db2 = tmp_path / "tar1090" / "db2"
    db2.mkdir(parents=True)
    (db2 / "A.js").write_bytes(gzip.compress(json.dumps({"children": ["A0"], "12345": ["N9"]}).encode()))
    (db2 / "A0.js").write_bytes(gzip.compress(json.dumps({"children": [], "0001": ["N1", "C172"]}).encode()))
    app.state.tar1090_dir = tmp_path / "tar1090"
    add_message(ctx, tail="N1")
    add_message(ctx, tail="N1", text="", has_text=False)
    ctx.update_tracks("adsb", ctx.local_node_id, "SER1", {"aircraft": [{"hex": "a00001"}, {"hex": "a12345"}, {"hex": "bbbbbb"}]})
    items = {a["hex"]: a for a in admin.get("/api/maps/adsb").json()["items"]}
    assert items["a00001"]["registration"] == "N1" and items["a00001"]["acars_count"] == 1 and items["a00001"]["acars_tail"] == "N1"
    assert items["a12345"]["registration"] == "N9" and "acars_count" not in items["a12345"]
    assert "registration" not in items["bbbbbb"]


def test_retired_uses_dropped(tmp_path, admin):
    db = tmp_path / "uses.db"
    make_session_factory(db)
    connection = sqlite3.connect(db)
    connection.execute("INSERT INTO nodes (id, name, is_local, status, serials, created_at) VALUES (1, 'n', 1, 'active', '[]', 0)")
    connection.execute("INSERT INTO sources (id, node_id, serial, name, description, antenna, ppm, uses) VALUES (1, 1, 'S', 'S', '', '', 0, ?)",
                       (json.dumps(["goes", "voice", "pager"]),))
    connection.commit()
    connection.close()
    with make_session_factory(db)() as session:
        assert session.get(Source, 1).uses == ["voice"]
    saved = admin.post("/api/sources", json={"serial": "SER1", "name": "S", "uses": ["meteor", "acars"]})
    assert saved.status_code == 201
    assert next(s for s in admin.get("/api/sources").json() if s["name"] == "S")["uses"] == ["acars"]


@pytest.mark.parametrize("body", [{"airframes_station_id": "bad id!"}, {"airframes_feed": "http://x"}, {"airframes_feed": "host"}])
def test_airframes_settings_checked(admin, body):
    assert admin.patch("/api/admin/settings", json=body).status_code == 422


def test_airframes_settings_saved(admin):
    reply = admin.patch("/api/admin/settings", json={"airframes_station_id": "KE-KSEA-ACARS", "airframes_feed": "feed.acars.io:5550",
                                                       "message_retention_days": 14})
    assert reply.status_code == 200
    got = admin.get("/api/admin/settings").json()
    assert (got["airframes_station_id"], got["message_retention_days"]) == ("KE-KSEA-ACARS", "14")


# --- ADS-B and AIS feeding ------------------------------------------------------------
UUID = "0f6c4a8e-1b2d-4c3e-9f00-123456789abc"


def test_readsb_feed_args_are_outbound_and_loopback_only():
    args = feeds.readsb_args({"aggregators": ["adsb_lol", "adsbx"], "beast_port": 30005}, UUID)
    assert args[:3] == ["--net", "--net-bind-address", "127.0.0.1"]
    connectors = [args[i + 1] for i, a in enumerate(args) if a == "--net-connector"]
    assert connectors == [
        f"feed.adsb.lol,30004,beast_reduce_plus_out,in.adsb.lol,1337,uuid={UUID}",
        "feed1.adsbexchange.com,30004,beast_reduce_out,feed2.adsbexchange.com,64004",
    ]
    assert args[args.index("--net-bo-port") + 1] == "30005" and args[args.index("--uuid") + 1] == UUID
    for port_option in ("--net-ro-port", "--net-sbs-port", "--net-bi-port", "--net-ri-port", "--net-api-port"):
        assert port_option not in args
    assert feeds.readsb_args({}, UUID) == [] and feeds.readsb_args({"aggregators": []}, None) == []


def test_readsb_args_without_feeds_has_no_networking():
    cfg = SimpleNamespace(binary=lambda name: None)
    args = pipelines.readsb_args(cfg, 0, {"feeds": {}}, "/tmp/x")
    assert "--net" not in args
    with_feed = pipelines.readsb_args(cfg, 0, {"feeds": {"aggregators": ["adsb_fi"]}, "feeder_uuid": UUID}, "/tmp/x")
    assert "feed.adsb.fi,30004,beast_reduce_plus_out,feed.adsb.fi,64004" in with_feed


def test_ais_feed_args():
    cfg = SimpleNamespace(binary=lambda name: None)
    spec = {"feeds": {"community": True, "udp": [{"service": "aishub", "host": "data.aishub.net", "port": 4321}]}}
    args = pipelines.ais_args(cfg, 1, spec)
    assert args[args.index("-u"):] == ["-u", "data.aishub.net", "4321", "-X"]


@pytest.mark.parametrize("kind,raw,problem", [
    ("adsb", {"aggregators": ["flightradar"]}, "Unknown aggregator"),
    ("adsb", {"beast_port": 80}, "between 1025"),
    ("adsb", {"beast_port": "x"}, "number"),
    ("ais", {"udp": [{"service": "marinetraffic", "host": "evil host; rm", "port": 1}]}, "host name"),
    ("ais", {"udp": [{"service": "nope", "host": "a.b", "port": 1}]}, "Unknown AIS"),
    ("ais", {"udp": [{"service": "aishub", "host": "a.b", "port": 70000}]}, "between 1"),
    ("ais", {"udp": [{"service": "aishub", "host": "a.b", "port": 1}] * 7}, "At most 6"),
    ("audio", {"aggregators": ["adsbx"]}, "Only ADS-B and AIS"),
    ("adsb", ["adsbx"], "object"),
])
def test_bad_feed_settings(kind, raw, problem):
    with pytest.raises(feeds.FeedError, match=problem):
        feeds.clean(kind, raw)


def test_feed_settings_reach_the_spec_and_are_checked_again(ctx, admin):
    source = make_source(admin)
    body = {"name": "Planes", "kind": "adsb", "modulation": "adsb", "frequency_hz": 1_090_000_000,
            "feeds": {"aggregators": ["adsbx", "adsb_fi", "adsbx"], "beast_port": 30005}}
    made = admin.post("/api/targets", json=body)
    assert made.status_code == 201 and made.json()["feeds"] == {"aggregators": ["adsb_fi", "adsbx"], "beast_port": 30005}
    admin.post(f"/api/sources/{source}/assign", json={"target_id": made.json()["id"]}).raise_for_status()
    spec = ctx.assignments()[ctx.local_node_id]["SER1"]
    assert spec["feeds"]["aggregators"] == ["adsb_fi", "adsbx"] and feeds.valid_uuid(spec["feeder_uuid"])
    assert pipelines.sanitize_spec(spec)["feeds"] == spec["feeds"]
    with pytest.raises(pipelines.SpecError):
        pipelines.sanitize_spec({**spec, "feeds": {"aggregators": ["attacker"]}})  # what a slave would refuse
    assert pipelines.sanitize_spec({**spec, "feeder_uuid": "not-a-uuid"})["feeder_uuid"] is None
    voice = admin.post("/api/targets", json={"name": "V", "frequency_hz": 444_000_000, "feeds": {"aggregators": ["adsbx"]}})
    assert voice.status_code == 422


def test_each_source_has_its_own_stable_feeder_id(ctx, admin):
    first, second = make_source(admin), make_source(admin, "SER2", "Two")
    with ctx.db() as session:
        ids = [session.get(Source, first).feeder_uuid, session.get(Source, second).feeder_uuid]
    assert all(feeds.valid_uuid(i) for i in ids) and ids[0] != ids[1]
    admin.put(f"/api/sources/{first}", json={"serial": "SER1", "name": "Renamed"}).raise_for_status()
    with ctx.db() as session:
        assert session.get(Source, first).feeder_uuid == ids[0]


def test_older_sources_get_a_feeder_id(tmp_path):
    db = tmp_path / "f.db"
    make_session_factory(db)
    connection = sqlite3.connect(db)
    connection.execute("INSERT INTO nodes (id, name, is_local, status, serials, created_at) VALUES (1, 'n', 1, 'active', '[]', 0)")
    connection.execute("INSERT INTO sources (id, node_id, serial, name, description, antenna, ppm) VALUES (1, 1, 'S', 'S', '', '', 0)")
    connection.commit()
    connection.close()
    with make_session_factory(db)() as session:
        assert feeds.valid_uuid(session.get(Source, 1).feeder_uuid)


def test_slave_status_keeps_note_and_feeds(ctx):
    from sentrysdr.web.routes_nodes import handle_agent_message
    handle_agent_message(ctx, 7, ["SER1"], {"t": "status", "sources": {"SER1": {
        "state": "listening", "note": "USB errors on start", "feeds": {
            "aggregators": {"adsbx": True, "evil": True}, "local_clients": 2, "sending": ["aishub", "x"]}}}})
    status = ctx.status[(7, "SER1")]
    assert status["note"] == "USB errors on start"
    assert status["feeds"] == {"aggregators": {"adsbx": True}, "local_clients": 2, "sending": ["aishub"]}


def test_ui_lists_the_same_feed_services():
    lib = (Path(__file__).resolve().parent.parent / "web" / "src" / "lib.js").read_text()
    for key in [*feeds.ADSB_AGGREGATORS, *feeds.AIS_SERVICES]:
        assert f"['{key}'," in lib


def test_feeder_id_shown_to_admins_only(ctx, admin, operator):
    source = make_source(admin)
    with ctx.db() as session:
        expected = session.get(Source, source).feeder_uuid
    assert next(s for s in admin.get("/api/sources").json() if s["id"] == source)["feeder_uuid"] == expected
    assert "feeder_uuid" not in next(s for s in operator.get("/api/sources").json() if s["id"] == source)



def test_message_search_treats_wildcards_literally(ctx, listener):
    add_message(ctx, text="100% FUEL")
    add_message(ctx, text="NOTHING HERE")
    assert listener.get("/api/messages?search=%25").json()["total"] == 1
    assert listener.get("/api/messages?search=_").json()["total"] == 0


# --- rtl_tcp ------------------------------------------------------------------------
@pytest.mark.parametrize("bind,allow,ok", [
    ("127.0.0.1", False, True), ("192.168.1.20", False, True), ("100.80.1.2", False, True), ("::1", False, True),
    ("0.0.0.0", False, False), ("8.8.8.8", False, False), ("pi.local", False, False), ("0.0.0.0", True, True),
])
def test_rtl_tcp_bind_is_checked(bind, allow, ok):
    from sentrysdr.config import Config, ConfigError, validate
    cfg = Config(rtl_tcp_bind=bind, rtl_tcp_allow_public=allow)
    if ok:
        validate(cfg)
    else:
        with pytest.raises(ConfigError):
            validate(cfg)


def test_rtl_tcp_args_use_the_local_address():
    cfg = SimpleNamespace(binary=lambda name: None, rtl_tcp_bind="192.168.1.20")
    args = pipelines.rtltcp_args(cfg, 2, {"rtl_tcp_port": 1235, "frequency_hz": 162_550_000, "gain_db": 40.2, "ppm": -3})
    assert args == ["rtl_tcp", "-a", "192.168.1.20", "-p", "1235", "-d", "2", "-f", "162550000", "-g", "40.2", "-P", "-3"]
    assert "-g" not in pipelines.rtltcp_args(cfg, 0, {"rtl_tcp_port": 1234, "frequency_hz": 1, "gain_db": None})


def test_rtl_tcp_spec_needs_a_port():
    base = {"recorder_id": 1, "source_id": 1, "target_id": 1, "frequency_hz": 100_000_000, "modulation": "rtltcp", "kind": "rtltcp"}
    assert pipelines.sanitize_spec({**base, "rtl_tcp_port": 1234})["rtl_tcp_port"] == 1234
    for bad in (None, 80, 70000, "x"):
        with pytest.raises(pipelines.SpecError):
            pipelines.sanitize_spec({**base, "rtl_tcp_port": bad})
    voice = {**base, "kind": "audio", "modulation": "nfm", "rtl_tcp_port": 1234}
    assert pipelines.sanitize_spec(voice)["rtl_tcp_port"] is None


def rtltcp_target(client, name="Raw IQ", hz=162_550_000):
    return client.post("/api/targets", json={"name": name, "kind": "rtltcp", "modulation": "rtltcp", "frequency_hz": hz})


def test_each_sdr_gets_its_own_rtl_tcp_port(ctx, admin):
    first, second = make_source(admin), make_source(admin, "SER2", "Two")
    ports = {s["id"]: s["rtl_tcp_port"] for s in admin.get("/api/sources").json()}
    assert ports == {first: 1234, second: 1235}
    clash = admin.put(f"/api/sources/{second}", json={"serial": "SER2", "name": "Two", "rtl_tcp_port": 1234})
    assert clash.status_code == 409 and "already used by Source 1" in clash.text
    assert admin.put(f"/api/sources/{second}", json={"serial": "SER2", "name": "Two", "rtl_tcp_port": 1300}).status_code == 200
    assert admin.put(f"/api/sources/{second}", json={"serial": "SER2", "name": "Two"}).status_code == 200  # blank keeps it
    assert {s["id"]: s["rtl_tcp_port"] for s in admin.get("/api/sources").json()}[second] == 1300
    target = rtltcp_target(admin).json()["id"]
    admin.post(f"/api/sources/{second}/assign", json={"target_id": target}).raise_for_status()
    spec = ctx.assignments()[ctx.local_node_id]["SER2"]
    assert spec["kind"] == "rtltcp" and spec["rtl_tcp_port"] == 1300
    assert pipelines.sanitize_spec(spec)["rtl_tcp_port"] == 1300


def test_ports_are_per_machine(ctx, admin):
    with ctx.db() as session:
        session.add(Node(name="Field", status="active", serials=["FIELD1"]))
        session.commit()
        field = session.scalar(select(Node).where(Node.name == "Field")).id
    make_source(admin)
    admin.post("/api/sources", json={"node_id": field, "serial": "FIELD1", "name": "Far"}).raise_for_status()
    assert sorted(s["rtl_tcp_port"] for s in admin.get("/api/sources").json()) == [1234, 1234]


def test_only_admins_use_rtl_tcp(ctx, admin, operator):
    source = make_source(admin)
    assert rtltcp_target(operator).status_code == 403
    target = rtltcp_target(admin).json()["id"]
    voice = make_target(admin)
    assert operator.post(f"/api/sources/{source}/assign", json={"target_id": target}).status_code == 403
    schedule = {"name": "Night", "source_id": source, "target_id": target, "schedule_days": [1], "schedule_start": "22:00", "schedule_end": "23:00"}
    assert operator.post("/api/recorders", json=schedule).status_code == 403
    assert operator.put(f"/api/targets/{target}", json={"name": "Mine", "kind": "rtltcp", "modulation": "rtltcp", "frequency_hz": 100_000_000}).status_code == 403
    assert operator.delete(f"/api/targets/{target}").status_code == 403
    assert admin.post(f"/api/sources/{source}/assign", json={"target_id": target}).status_code == 200
    # An operator can still take the stick off rtl_tcp, which closes the port.
    assert operator.post(f"/api/sources/{source}/assign", json={"target_id": voice}).status_code == 200
    made = admin.post("/api/recorders", json=schedule)
    assert made.status_code == 201
    assert operator.delete(f"/api/recorders/{made.json()['id']}").status_code == 403


def test_older_sources_get_rtl_tcp_ports(tmp_path):
    db = tmp_path / "p.db"
    make_session_factory(db)
    connection = sqlite3.connect(db)
    connection.execute("INSERT INTO nodes (id, name, is_local, status, serials, created_at) VALUES (1, 'n', 1, 'active', '[]', 0)")
    for i in (1, 2):
        connection.execute("INSERT INTO sources (id, node_id, serial, name, description, antenna, ppm) VALUES (?, 1, ?, 'S', '', '', 0)", (i, f"S{i}"))
    connection.commit()
    connection.close()
    with make_session_factory(db)() as session:
        assert sorted(s.rtl_tcp_port for s in session.scalars(select(Source))) == [1234, 1235]


def test_slave_rtl_tcp_status_is_cleaned(ctx):
    from sentrysdr.web.routes_nodes import handle_agent_message
    for raw, expected in (({"address": "192.168.1.20:1234", "client": 1}, {"address": "192.168.1.20:1234", "client": True}),
                          ({"address": "<b>evil</b>:1234", "client": True}, {}), ({"address": "10.0.0.1:99999"}, {}), ("x", {})):
        handle_agent_message(ctx, 7, ["S"], {"t": "status", "sources": {"S": {"state": "listening", "rtl_tcp": raw}}})
        assert ctx.status[(7, "S")]["rtl_tcp"] == expected


# --- restarting by hand ---------------------------------------------------------------
def test_restart_routes_are_admin_only_and_reach_the_node(ctx, admin, operator):
    source = make_source(admin)
    assert operator.post(f"/api/sources/{source}/restart").status_code == 403
    assert operator.post(f"/api/nodes/{ctx.local_node_id}/restart").status_code == 403
    # Radios aren't running in these tests, which is reported, not a crash.
    assert admin.post(f"/api/sources/{source}/restart").status_code == 409
    sent = []
    with ctx.db() as session:
        session.add(Node(name="Field", status="active", serials=["FIELD1"]))
        session.commit()
        field = session.scalar(select(Node).where(Node.name == "Field")).id
    far = admin.post("/api/sources", json={"node_id": field, "serial": "FIELD1", "name": "Far"}).json()["id"]
    assert admin.post(f"/api/sources/{far}/restart").status_code == 409  # offline
    ctx.node_links[field] = SimpleNamespace(send=sent.append)
    assert admin.post(f"/api/sources/{far}/restart").status_code == 200
    assert admin.post(f"/api/nodes/{field}/restart").status_code == 200
    assert sent == [{"t": "restart", "serial": "FIELD1"}, {"t": "restart_service"}]
    with ctx.db() as session:
        actions = [a.action for a in session.scalars(select(AuditLog))]
    assert "source.restarted" in actions and "node.service_restarted" in actions


# --- machine health -------------------------------------------------------------------
import sentrysdr as sentrysdr_package
from sentrysdr import health as health_module
from sentrysdr.web.routes_nodes import clean_health


def add_slave(ctx, name="Field", online=True, build=None, version="0.1.0", last_seen=None):
    with ctx.db() as session:
        node = Node(name=name, status="active", serials=["FIELD1"], build=build, version=version,
                    last_seen_at=last_seen if last_seen is not None else time.time())
        session.add(node)
        session.commit()
        node_id = node.id
    if online:
        ctx.node_links[node_id] = SimpleNamespace(send=lambda m: None)
    return node_id


def warnings_text(client):
    return " | ".join(w["text"] for w in client.get("/api/warnings").json())


def test_node_versions_shown_and_compared(ctx, admin):
    add_slave(ctx, "Same", build=sentrysdr_package.BUILD_ID, version=sentrysdr_package.__version__)
    add_slave(ctx, "Old", build="0123456789ab", version="0.1.0")
    nodes = {n["name"]: n for n in admin.get("/api/nodes").json()}
    local = next(n for n in nodes.values() if n["is_local"])
    assert local["version"] == sentrysdr_package.__version__ and local["matches_master"] is True
    assert nodes["Same"]["matches_master"] is True and nodes["Old"]["matches_master"] is False
    assert "Old runs a different version (0.1.0)" in warnings_text(admin) and "Same runs" not in warnings_text(admin)


def test_health_warnings(ctx, admin, operator, listener, monkeypatch):
    add_slave(ctx, "Gone", online=False, last_seen=time.time() - 3600)
    add_slave(ctx, "Briefly", online=False, last_seen=time.time() - 30)
    skewed = add_slave(ctx, "Skewed", build=sentrysdr_package.BUILD_ID)
    ctx.node_health[skewed] = clean_health({"time": time.time() - 40, "clock_synced": True, "disk": {"free": 10**12, "total": 2 * 10**12}})
    unsynced = add_slave(ctx, "Unsynced", build=sentrysdr_package.BUILD_ID)
    ctx.node_health[unsynced] = clean_health({"time": time.time(), "clock_synced": False, "disk": {"free": 10**8, "total": 10**11}})
    monkeypatch.setattr(health_module, "clock_synced", lambda: True)
    monkeypatch.setattr(health_module, "disk", lambda path: {"free": 10**12, "total": 2 * 10**12})
    text = warnings_text(admin)
    assert "Gone is offline since" in text and "Briefly" not in text
    assert "Skewed's clock is 40 seconds off" in text
    assert "Unsynced's clock isn't synced" in text and "Unsynced has 0.1 GB free" in text
    assert text == warnings_text(operator)
    assert listener.get("/api/warnings").json() == []


def test_sdr_faults_and_failed_uploads_warn(ctx, admin, monkeypatch):
    monkeypatch.setattr(health_module, "clock_synced", lambda: True)
    monkeypatch.setattr(health_module, "disk", lambda path: {"free": 10**12, "total": 2 * 10**12})
    source = make_source(admin, name="Roof stick")
    admin.post(f"/api/sources/{source}/assign", json={"target_id": make_target(admin)}).raise_for_status()
    ctx.set_status(ctx.local_node_id, "SER1", {"state": "error", "error": "SDR disconnected"})
    assert "Roof stick: SDR disconnected" in warnings_text(admin)
    with ctx.db() as session:
        session.add(Recording(id=str(uuid.uuid4()), started_at=time.time() - 60, ended_at=time.time(), duration=5, rel_path="x.mp3"))
        session.flush()
        rec_id = session.scalars(select(Recording)).first().id
        session.add_all([UploadJob(recording_id=rec_id, destination="rdio", status="failed"),
                         UploadJob(recording_id=rec_id, destination="clearcut", status="failed")])
        session.commit()
    assert "2 uploads failed in the last day" in warnings_text(admin)


def test_clean_health_report():
    report = clean_health({"time": "nonsense", "clock_synced": "yes", "disk": {"free": -1, "total": 5}})
    assert report["skew"] is None and report["clock_synced"] is None and "disk" not in report


def test_kernel_clock_reading_works_here():
    assert health_module.clock_synced() in (True, False, None)


def add_recording(ctx, age_days, size, protected=False):
    call = str(uuid.uuid4())
    path = ctx.cfg.recordings_dir / f"{call}.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ID3" + b"x" * 10)
    with ctx.db() as session:
        session.add(Recording(id=call, started_at=time.time() - age_days * 86400, ended_at=time.time(), duration=5,
                              rel_path=path.name, size_bytes=size, protected=protected))
        session.commit()
    return call, path


def test_size_limit_removes_oldest_unprotected_first(ctx, admin):
    ctx.set_setting("recording_retention_days", "0")
    oldest_protected = add_recording(ctx, 10, 400_000_000, protected=True)
    old, old_path = add_recording(ctx, 9, 400_000_000)
    middle, _ = add_recording(ctx, 5, 400_000_000)
    new, _ = add_recording(ctx, 1, 400_000_000)
    admin.patch("/api/admin/settings", json={"recording_retention_gb": 1}).raise_for_status()
    assert ctx.apply_retention()["over_size"] == 2
    with ctx.db() as session:
        left = {r.id for r in session.scalars(select(Recording))}
    assert left == {oldest_protected[0], new} and not old_path.exists()
    assert admin.get("/api/admin/settings").json()["recordings_bytes"] == 800_000_000


def test_protected_recordings_over_the_limit_warn(ctx, admin, monkeypatch):
    monkeypatch.setattr(health_module, "clock_synced", lambda: True)
    monkeypatch.setattr(health_module, "disk", lambda path: {"free": 10**12, "total": 2 * 10**12})
    add_recording(ctx, 1, 2_000_000_000, protected=True)
    ctx.set_setting("recording_retention_gb", "1")
    ctx.apply_retention()
    assert "Protected recordings alone (2.0 GB) exceed the 1 GB limit" in warnings_text(admin)


def test_backup_needs_the_password_and_holds_the_database(ctx, admin, operator, tmp_path):
    import io, tarfile
    assert operator.post("/api/admin/backup", json={"password": PASSWORD}).status_code == 403
    assert admin.post("/api/admin/backup", json={"password": "wrong-password-123"}).status_code == 403
    reply = admin.post("/api/admin/backup", json={"password": PASSWORD})
    assert reply.status_code == 200 and reply.headers["content-type"] == "application/gzip"
    with tarfile.open(fileobj=io.BytesIO(reply.content)) as tar:
        names = tar.getnames()
        tar.extractall(tmp_path / "restore", filter="data")
    assert sorted(names) == ["secret.key", "sentrysdr.db"]
    copy = sqlite3.connect(tmp_path / "restore" / "sentrysdr.db")
    assert copy.execute("SELECT count(*) FROM users WHERE email = ?", (ADMIN_EMAIL,)).fetchone()[0] == 1
    copy.close()
    assert (tmp_path / "restore" / "secret.key").read_bytes() == (ctx.cfg.data_dir / "secret.key").read_bytes()
    assert list(ctx.cfg.data_dir.glob("backup-*")) == []  # the temporary copy is gone
    with ctx.db() as session:
        assert session.scalar(select(AuditLog).where(AuditLog.action == "admin.backup_downloaded"))


# --- one-off schedules and what happens when a schedule ends --------------------------------
from zoneinfo import ZoneInfo


def test_one_off_window():
    tz = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 22, 9, 0, tzinfo=tz).timestamp()
    once = SimpleNamespace(id=2, source_id=1, enabled=True, schedule_days=[], schedule_start=None, schedule_end=None,
                           once_start=start, once_end=start + 7200)
    base = rec(1)
    assert schedule.resolve([base, once], datetime(2026, 11, 22, 10, 30, tzinfo=tz))[1] is once
    assert schedule.resolve([base, once], datetime(2026, 11, 22, 11, 0, tzinfo=tz))[1] is base
    assert schedule.resolve([base, once], datetime(2026, 11, 29, 10, 0, tzinfo=tz))[1] is base  # not weekly
    assert schedule.finished(once, datetime(2026, 11, 22, 11, 0, tzinfo=tz)) and not schedule.finished(base, datetime(2026, 11, 22, 11, 0, tzinfo=tz))


def once_body(source, target, start="2030-11-22T09:00", end="2030-11-22T11:00", **extra):
    return {"name": "Event", "source_id": source, "target_id": target, "repeat": "once", "once_start": start, "once_end": end, **extra}


def test_one_off_schedule_api(ctx, admin):
    source, target = make_source(admin), make_target(admin)
    made = admin.post("/api/recorders", json=once_body(source, target))
    assert made.status_code == 201, made.text
    view = made.json()
    expected = datetime(2030, 11, 22, 9, 0, tzinfo=ctx.cfg.tz).timestamp()
    assert view["repeat"] == "once" and view["once_start"] == expected and view["once_end"] == expected + 7200 and view["on_end"] == "return"
    for bad, problem in ((dict(start="2020-01-01T09:00", end="2020-01-01T10:00"), "already passed"),
                         (dict(start="2030-11-22T11:00", end="2030-11-22T09:00"), "after the start"),
                         (dict(start="2030-11-01T00:00", end="2030-12-15T00:00"), "at most 31 days"),
                         (dict(start="tomorrow", end="2030-11-22T09:00"), "date and time")):
        reply = admin.post("/api/recorders", json=once_body(source, target, **bad))
        assert reply.status_code == 422 and problem in reply.text, (bad, reply.text)
    weekly = {"name": "W", "source_id": source, "target_id": target, "schedule_days": [], "schedule_start": "22:00", "schedule_end": "23:00"}
    assert admin.post("/api/recorders", json=weekly).status_code == 422
    assert admin.post("/api/recorders", json={**weekly, "on_end": "sideways", "schedule_days": [1]}).status_code == 422


def make_once(ctx, admin, source, target, on_end, start_offset=-60, length=3600):
    with ctx.db() as session:
        recorder = Recorder(name=f"Once {on_end}", source_id=source, target_id=target, schedule_days=[], on_end=on_end,
                            once_start=time.time() + start_offset, once_end=time.time() + start_offset + length)
        session.add(recorder)
        session.commit()
        return recorder.id


def end_now(ctx, recorder_id):
    with ctx.db() as session:
        recorder = session.get(Recorder, recorder_id)
        recorder.once_end = time.time() - 1
        session.commit()


def base_target(ctx, source):
    with ctx.db() as session:
        base = next((r for r in session.scalars(select(Recorder).where(Recorder.source_id == source)) if r.schedule_days is None), None)
        return base.target_id if base else None


def test_keep_replaces_the_base_and_return_leaves_it(ctx, admin):
    first, second = make_source(admin), make_source(admin, "SER2", "Two")
    usual, event = make_target(admin, "Usual"), make_target(admin, "Event", 446_000_000)
    for source in (first, second):
        admin.post(f"/api/sources/{source}/assign", json={"target_id": usual}).raise_for_status()
    keep, back = make_once(ctx, admin, first, event, "keep"), make_once(ctx, admin, second, event, "return")
    ctx.track_schedule_ends()  # both take over
    end_now(ctx, keep); end_now(ctx, back)
    assert ctx.track_schedule_ends() == ["Source 1: Event"]
    assert base_target(ctx, first) == event and base_target(ctx, second) == usual
    assert ctx.assignments()[ctx.local_node_id]["SER1"]["target_id"] == event


def test_keep_on_an_unassigned_sdr_assigns_it(ctx, admin):
    source, event = make_source(admin), make_target(admin, "Event")
    keep = make_once(ctx, admin, source, event, "keep")
    ctx.track_schedule_ends(); end_now(ctx, keep); ctx.track_schedule_ends()
    assert base_target(ctx, source) == event
    other = make_source(admin, "SER2", "Two")
    back = make_once(ctx, admin, other, event, "return")
    ctx.track_schedule_ends(); end_now(ctx, back); ctx.track_schedule_ends()
    assert base_target(ctx, other) is None


def test_turning_a_schedule_off_early_doesnt_keep(ctx, admin):
    source, event = make_source(admin), make_target(admin, "Event")
    keep = make_once(ctx, admin, source, event, "keep")
    ctx.track_schedule_ends()
    with ctx.db() as session:
        session.get(Recorder, keep).enabled = False
        session.commit()
    assert ctx.track_schedule_ends() == [] and base_target(ctx, source) is None


def test_keep_skips_a_locked_assignment(ctx, admin):
    source, usual, event = make_source(admin), make_target(admin, "Usual"), make_target(admin, "Event", 446_000_000)
    admin.post(f"/api/sources/{source}/assign", json={"target_id": usual}).raise_for_status()
    admin.post(f"/api/sources/{source}/lock").raise_for_status()
    keep = make_once(ctx, admin, source, event, "keep")
    ctx.track_schedule_ends(); end_now(ctx, keep)
    assert ctx.track_schedule_ends() == [] and base_target(ctx, source) == usual
    with ctx.db() as session:
        assert session.scalar(select(AuditLog).where(AuditLog.action == "schedule.keep_skipped"))


def test_a_schedule_that_ended_while_down_still_keeps(ctx, admin):
    source, event = make_source(admin), make_target(admin, "Event")
    keep = make_once(ctx, admin, source, event, "keep", start_offset=-7200, length=3600)  # already over
    with ctx.db() as session:
        session.get(Recorder, keep).active_since = time.time() - 7000  # it was running when the service stopped
        session.commit()
    assert ctx.track_schedule_ends() == ["Source 1: Event"]
    assert ctx.track_schedule_ends() == []  # only once
