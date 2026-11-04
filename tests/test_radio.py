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
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from sentrysdr import pipelines, schedule, sdr, uploaders
from sentrysdr.engine import MeasureError, merge_ais_message
from sentrysdr.models import BroadcastifySystem, Recording, SdrProfile, Target, UploadJob, make_session_factory

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


def test_lock_requires_password_for_users(ctx, admin, operator):
    source, first, second = make_source(admin), make_target(admin), make_target(admin, "Other", 145_000_000)
    admin.post(f"/api/sources/{source}/assign", json={"target_id": first})
    assert admin.post(f"/api/sources/{source}/lock", json={"password": "open-sesame"}).status_code == 200

    url = f"/api/sources/{source}/assign"
    assert operator.post(url, json={"target_id": second}).json()["detail"] == "locked"
    assert operator.post(url, json={"target_id": second, "lock_password": "wrong"}).status_code == 403
    assert ctx.assignments()[ctx.local_node_id]["SER1"]["target_id"] == first
    assert operator.post(url, json={"target_id": second, "lock_password": "open-sesame"}).status_code == 200
    assert ctx.assignments()[ctx.local_node_id]["SER1"]["target_id"] == second


def test_admin_overrides_lock_without_password(ctx, admin):
    source, first, second = make_source(admin), make_target(admin), make_target(admin, "Other", 145_000_000)
    admin.post(f"/api/sources/{source}/assign", json={"target_id": first})
    admin.post(f"/api/sources/{source}/lock", json={"password": "open-sesame"})
    assert admin.post(f"/api/sources/{source}/assign", json={"target_id": second}).status_code == 200


def test_lock_password_guessing_rate_limited(admin, operator):
    source, target = make_source(admin), make_target(admin)
    admin.post(f"/api/sources/{source}/assign", json={"target_id": target})
    admin.post(f"/api/sources/{source}/lock", json={"password": "open-sesame"})
    url = f"/api/sources/{source}/assign"
    codes = [operator.post(url, json={"target_id": target, "lock_password": f"guess{i}"}).status_code for i in range(9)]
    assert codes[:8] == [403] * 8 and codes[8] == 429


def test_schedule_cannot_bypass_a_lock(admin, operator):
    source, first, second = make_source(admin), make_target(admin), make_target(admin, "Other", 145_000_000)
    admin.post(f"/api/sources/{source}/assign", json={"target_id": first})
    admin.post(f"/api/sources/{source}/lock", json={"password": "open-sesame"})
    body = {"name": "sneak", "source_id": source, "target_id": second, "schedule_days": [1],
            "schedule_start": "22:00", "schedule_end": "23:00"}
    assert operator.post("/api/recorders", json=body).status_code == 403
    assert operator.post("/api/recorders", json={**body, "lock_password": "open-sesame"}).status_code == 201


def test_editing_target_cannot_retune_a_locked_recorder(admin, operator):
    source, target = make_source(admin), make_target(admin)
    admin.post(f"/api/sources/{source}/assign", json={"target_id": target})
    admin.post(f"/api/sources/{source}/lock", json={"password": "open-sesame"})
    body = {"name": "K1ABC", "frequency_hz": 146_520_000}
    assert operator.put(f"/api/targets/{target}", json=body).status_code == 403
    assert operator.delete(f"/api/targets/{target}").status_code == 403
    assert admin.put(f"/api/targets/{target}", json=body).status_code == 200


def test_lock_hash_never_exposed(admin, listener):
    source, target = make_source(admin), make_target(admin)
    admin.post(f"/api/sources/{source}/assign", json={"target_id": target})
    admin.post(f"/api/sources/{source}/lock", json={"password": "open-sesame"})
    for path in ("/api/sources", "/api/recorders"):
        assert "argon2" not in listener.get(path).text
    assert source_row(listener, source)["locked"] is True


def test_out_of_range_target_warns(admin):
    source = make_source(admin, freq_min_hz=380_000_000, freq_max_hz=460_000_000)
    target = make_target(admin, "Air", 120_000_000, modulation="am")
    assert "outside the antenna range" in admin.post(f"/api/sources/{source}/assign", json={"target_id": target}).json()["warning"]


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
    assert pipelines.suggest_squelch(pipelines.signal_levels(pcm, 16000)) == 103


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
    admin.post(f"/api/sources/{source}/lock", json={"password": "secret-lock"})
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


def pending_future(ctx, node_id=7):
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    ctx.pending_measures["abc"] = (node_id, future)
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
    {"reply": "100 NO-API-KEY-SPECIFIED"}, {"post_status": 500}, {"put_status": 403}, {"reply": "0 http://plain.example.com/x"}, {"reply": "0"},
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
