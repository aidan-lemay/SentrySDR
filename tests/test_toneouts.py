"""Tone-outs: pages found in a call, named from the table, and emailed to the people who follow them."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import numpy as np
import pytest

from sentrysdr import notify, pipelines, toneouts
from sentrysdr.models import Recording, ToneOut, User, UserWatch

from .conftest import make_config
from .test_engine_nodes import install_tool, spec, supervisor
from .test_radio import make_target, store

FS = 16_000


def tone(hz: float | None, seconds: float, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * FS)) / FS
    return amplitude * np.sin(2 * np.pi * hz * t) if hz else np.zeros(len(t))


def voice(seconds: float, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(int(seconds * FS))
    spectrum = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(len(noise), 1 / FS)
    spectrum[(freqs < 150) | (freqs > 3000)] = 0
    out = np.fft.irfft(spectrum, len(noise))
    return 0.5 * out / np.max(np.abs(out))


def heard(signal: np.ndarray, pl: float = 0.08, noise: float = 0.01) -> list[dict]:
    rng = np.random.default_rng(2)
    signal = signal + pl * np.sin(2 * np.pi * 179.9 * np.arange(len(signal)) / FS) + noise * rng.standard_normal(len(signal))
    detector = toneouts.Detector(FS)
    pcm = (np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes()
    for start in range(0, len(pcm), 6400):
        detector.feed(pcm[start:start + 6400])
    return detector.pages()


def test_a_qc2_page_is_found_with_its_tones():
    pages = heard(np.concatenate([tone(None, 0.5), tone(1006.9, 1.0), tone(1530.0, 3.0), voice(3)]))
    assert len(pages) == 1
    assert pages[0]["a"] == pytest.approx(1006.9, abs=2) and pages[0]["b"] == pytest.approx(1530.0, abs=2)
    assert pages[0]["at"] == pytest.approx(0.5, abs=0.1)


def test_the_ends_of_the_band():
    pages = heard(np.concatenate([tone(288.5, 1.0), tone(2468.2, 3.0)]))
    assert [(round(p["a"]), round(p["b"])) for p in pages] == [(288, 2468)] or [(round(p["a"]), round(p["b"])) for p in pages] == [(289, 2468)]


def test_stations_paged_back_to_back():
    pages = heard(np.concatenate([tone(1006.9, 1.0), tone(1530.0, 3.0), tone(None, 0.2), tone(1153.4, 1.0), tone(1305.4, 3.0)]))
    assert [(round(p["a"]), round(p["b"])) for p in pages] == [(1007, 1530), (1153, 1305)]


def test_a_long_tone_page():
    assert [(round(p["a"]), p["b"]) for p in heard(np.concatenate([tone(1006.9, 8.0), voice(2)]))] == [(1007, None)]


def test_voice_and_short_tones_are_not_pages():
    assert heard(voice(8)) == []
    assert heard(np.concatenate([tone(1000, 0.3), voice(1), tone(1500, 1.0), voice(2)])) == []  # a beep and a short tone
    assert heard(np.concatenate([tone(1006.9, 1.0), tone(1530.0, 1.0)])) == []  # B too short


def test_a_quiet_or_noisy_page_is_still_found():
    assert heard(np.concatenate([tone(1006.9, 1.0, 0.05), tone(1530.0, 3.0, 0.05)]))
    assert heard(np.concatenate([tone(1006.9, 1.0, 0.3), tone(1530.0, 3.0, 0.3)]), noise=0.075)  # about 9 dB signal to noise


def test_it_is_cheap():
    signal = np.concatenate([tone(1006.9, 1.0), tone(1530.0, 3.0), voice(6)])
    started = time.perf_counter()
    heard(signal)
    assert time.perf_counter() - started < 1.0  # 10 s of audio; about 70 ms on a laptop


def test_naming_from_the_table():
    table = [SimpleNamespace(id=1, name="Station 5", a_hz=1006.9, b_hz=1530.0),
             SimpleNamespace(id=2, name="Station 6", a_hz=1006.9, b_hz=1563.2),
             SimpleNamespace(id=3, name="All call", a_hz=1006.9, b_hz=None)]
    pages = [{"a": 1010.0, "b": 1528.0, "at": 0}, {"a": 1006.9, "b": 1560.0, "at": 4}, {"a": 1007.0, "b": None, "at": 9},
             {"a": 700.0, "b": 900.0, "at": 12}]
    assert [p["name"] for p in toneouts.match(pages, table)] == ["Station 5", "Station 6", "All call", None]
    assert toneouts.describe(toneouts.match(pages, table)[3]) == "Unknown tone-out (700 / 900 Hz)"


def test_pages_from_a_slave_are_checked():
    raw = [{"a": 1006.9, "b": 1530.0, "at": 1}, {"a": "x"}, {"a": 50000, "b": None}, "nope", {"a": 400, "b": None, "at": -5}]
    assert toneouts.clean_pages(raw) == [{"a": 1006.9, "b": 1530.0, "at": 1.0}]
    assert toneouts.clean_pages("junk") == []


# --- the table ----------------------------------------------------------------------
def test_admins_keep_the_table(admin, operator):
    made = admin.post("/api/admin/tone-outs", json={"name": "Station 5", "a_hz": 1006.9, "b_hz": 1530.0})
    assert made.status_code == 201
    assert admin.post("/api/admin/tone-outs", json={"name": "Copy", "a_hz": 1010, "b_hz": 1528}).status_code == 409
    assert admin.post("/api/admin/tone-outs", json={"name": "Same", "a_hz": 1006.9, "b_hz": 1010}).status_code == 422
    assert admin.post("/api/admin/tone-outs", json={"name": "Low", "a_hz": 100}).status_code == 422
    assert operator.post("/api/admin/tone-outs", json={"name": "x", "a_hz": 1000}).status_code == 403
    assert [r["name"] for r in operator.get("/api/tone-outs").json()] == ["Station 5"]
    row = made.json()["id"]
    assert admin.put(f"/api/admin/tone-outs/{row}", json={"name": "Station 5 (Engine)", "a_hz": 1006.9, "b_hz": 1530.0}).status_code == 200
    assert admin.delete(f"/api/admin/tone-outs/{row}").status_code == 200


def test_csv_import(admin):
    text = "Name,A,B,Notes\nStation 5,1006.9,1530.0,Engine 5\nStation 6,1006.9,1563.2\nAll call,1006.9,,\nbad line\nStation 5 again,1006.9,1530\n"
    reply = admin.post("/api/admin/tone-outs/import", json={"text": text}).json()
    assert (reply["added"], reply["skipped"]) == (3, 1) and len(reply["problems"]) == 1
    names = {r["name"]: r for r in admin.get("/api/admin/tone-outs").json()}
    assert names["All call"]["b_hz"] is None and names["Station 5"]["notes"] == "Engine 5"


def test_targets_save_the_switch(admin):
    make_target(admin, "Dispatch", decode_tone_outs=True)
    make_target(admin, "Ships", hz=162_000_000, kind="ais", modulation="ais", decode_tone_outs=True)
    views = {t["name"]: t["decode_tone_outs"] for t in admin.get("/api/targets").json()}
    assert views == {"Dispatch": True, "Ships": False}


def test_a_stored_call_is_named_from_the_table(admin, ctx, tmp_path):
    admin.post("/api/admin/tone-outs", json={"name": "Station 5", "a_hz": 1006.9, "b_hz": 1530.0})
    target = make_target(admin, "Dispatch")
    call = store_with_pages(ctx, tmp_path, target, [{"a": 1008.1, "b": 1531.0, "at": 0.4}])
    with ctx.db() as session:
        assert session.get(Recording, call).tone_outs[0]["name"] == "Station 5"
    shown = next(r for r in admin.get("/api/recordings").json()["items"] if r["id"] == call)
    assert shown["tone_outs"][0]["label"] == "Station 5 (1008.1 / 1531 Hz)"


def store_with_pages(ctx, tmp_path, target, pages, name="Dispatch"):
    import uuid
    audio = tmp_path / f"{uuid.uuid4()}.mp3"
    audio.write_bytes(b"ID3fake")
    started = time.time()
    meta = {"id": str(uuid.uuid4()), "serial": "SER1", "source_id": None, "source_name": "S", "target_id": target,
            "target_name": name, "recorder_name": "r", "frequency_hz": 453_750_000, "modulation": "nfm",
            "started_at": started, "ended_at": started + 6, "duration": 6.0, "pages": pages}
    assert ctx.store_call(meta, audio, "node")
    return meta["id"]


# --- emails -------------------------------------------------------------------------
def follower(ctx, value):
    with ctx.db() as session:
        user = session.scalar(__import__("sqlalchemy").select(User).where(User.email == "admin@example.com")) or session.scalars(
            __import__("sqlalchemy").select(User)).first()
        user.email_alerts, user.email_verified, user.email_window = True, user.email, 60
        user.email_last_sent_at = time.time()  # just emailed: anything else would wait an hour
        user.email_since = time.time() - 60
        session.add(UserWatch(user_id=user.id, kind="toneout", value=value))
        session.commit()
        return user.id


def test_a_followed_tone_out_is_emailed_straight_away(admin, ctx, tmp_path):
    row = admin.post("/api/admin/tone-outs", json={"name": "Station 5", "a_hz": 1006.9, "b_hz": 1530.0}).json()["id"]
    target = make_target(admin, "QV028 Colchester")
    follower(ctx, str(row))
    ctx.set_setting("email_alerts_enabled", "true")
    store_with_pages(ctx, tmp_path, target, [{"a": 1006.9, "b": 1530.0, "at": 0.4}], name="QV028 Colchester")
    store(ctx, tmp_path, target_id=target)  # an ordinary call: not an alert
    due = notify.pending(ctx)
    assert len(due) == 1
    _, _, subject, body, _, items = due[0]
    assert subject == "SentrySDR Tone-out: Station 5 on QV028 Colchester"
    assert "a tone-out you follow" in body and len(items) == 1


def test_any_tone_out_includes_unknown_ones_and_others_are_not_sent(admin, ctx, tmp_path):
    admin.post("/api/admin/tone-outs", json={"name": "Station 5", "a_hz": 1006.9, "b_hz": 1530.0})
    other = admin.post("/api/admin/tone-outs", json={"name": "Station 9", "a_hz": 600.9, "b_hz": 1153.4}).json()["id"]
    target = make_target(admin, "Dispatch")
    ctx.set_setting("email_alerts_enabled", "true")
    user = follower(ctx, str(other))
    store_with_pages(ctx, tmp_path, target, [{"a": 1006.9, "b": 1530.0, "at": 0}])
    assert notify.pending(ctx) == []  # Station 5, which they don't follow
    with ctx.db() as session:
        session.add(UserWatch(user_id=user, kind="toneout", value="any"))
        session.commit()
    store_with_pages(ctx, tmp_path, target, [{"a": 700.0, "b": 900.0, "at": 0}])
    due = notify.pending(ctx)
    assert len(due) == 1 and len(due[0][5]) == 2  # any: Station 5's call now counts, and the unknown one
    assert "Tone-out: unknown on Dispatch" in due[0][3]


def test_people_follow_tone_outs(admin, operator, listener):
    row = admin.post("/api/admin/tone-outs", json={"name": "Station 5", "a_hz": 1006.9, "b_hz": 1530.0}).json()["id"]
    assert admin.post("/api/me/watches", json={"kind": "toneout", "value": str(row)}).status_code == 201
    assert admin.post("/api/me/watches", json={"kind": "toneout", "value": str(row)}).status_code == 409
    assert admin.post("/api/me/watches", json={"kind": "toneout", "value": "any"}).status_code == 201
    assert admin.post("/api/me/watches", json={"kind": "toneout", "value": "999"}).status_code == 404
    labels = {w["label"] for w in admin.get("/api/me/watches").json()}
    assert labels == {"Station 5", "Any tone-out"}
    admin.delete(f"/api/admin/tone-outs/{row}").raise_for_status()
    assert {w["label"] for w in admin.get("/api/me/watches").json()} == {"Any tone-out"}


# --- end to end on a single-channel SDR ----------------------------------------------
FAKE_PAGE = """\
#!/usr/bin/env python3
import math, struct, sys, time
def tone(hz, seconds):
    return b"".join(struct.pack("<h", int(16000 * math.sin(2 * math.pi * hz * i / 16000))) for i in range(int(seconds * 16000)))
data = tone(1006.9, 1.0) + tone(1530.0, 3.0)
for start in range(0, len(data), 3200):
    sys.stdout.buffer.write(data[start:start + 3200]); sys.stdout.flush(); time.sleep(0.01)
time.sleep(60)
"""


@pytest.mark.parametrize("on", [True, False])
async def test_a_page_reaches_the_calls_metadata_only_when_decoding(tmp_path, on):
    install_tool(tmp_path / "bin", "rtl_fm", FAKE_PAGE)
    calls = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[tmp_path / "bin"], hang_seconds=0.4), calls)
    try:
        await sup.apply({"SER1": spec(tone_outs=on, tone_filter="high")})
        for _ in range(200):
            if calls:
                break
            await asyncio.sleep(0.05)
    finally:
        await sup.shutdown()
    pages = calls[0][0].get("pages")
    if on:
        assert pages and pages[0]["a"] == pytest.approx(1006.9, abs=3) and pages[0]["b"] == pytest.approx(1530, abs=3)
    else:
        assert pages is None


def test_a_spec_only_decodes_for_analog_audio():
    base = {"recorder_id": 1, "source_id": 1, "target_id": 1, "frequency_hz": 453_750_000, "modulation": "nfm", "tone_outs": True}
    assert pipelines.sanitize_spec(base)["tone_outs"] is True
    assert pipelines.sanitize_spec({**base, "modulation": "dmr"})["tone_outs"] is False
