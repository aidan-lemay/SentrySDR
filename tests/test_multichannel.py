"""Multi-channel SDRs: several voice targets on one stick, recorded with the web tuner's DSP."""
from __future__ import annotations

import sys
from datetime import datetime

import pytest

np = pytest.importorskip("numpy")

from sentrysdr import engine, pipelines  # noqa: E402
from sentrysdr.models import Recorder, SdrProfile  # noqa: E402

from .conftest import make_config  # noqa: E402
from .test_engine_nodes import install_tool, supervisor, wait_for  # noqa: E402
from .test_radio import make_source, make_target  # noqa: E402


# --- fitting channels into one span -----------------------------------------------------------
def test_one_channel_uses_the_lowest_rate_with_the_centre_spike_moved_off_it():
    plan = pipelines.multi_plan([(146_940_000, "fm")])
    assert plan["rate"] == 250_000
    assert abs(plan["center_hz"] - 146_940_000) - 8_000 >= pipelines.MULTI_CENTER_GUARD_HZ


def test_channels_far_apart_need_a_wider_rate_and_stay_inside_it():
    channels = [(154_310_000, "nfm"), (154_430_000, "nfm"), (155_295_000, "nfm")]
    plan = pipelines.multi_plan(channels)
    reach = pipelines.MULTI_USABLE * plan["rate"] / 2
    assert plan["rate"] > 250_000
    for hz, mode in channels:
        offset = abs(hz - plan["center_hz"])
        assert offset + pipelines.MULTI_MODES[mode] / 2 <= reach
        assert offset - pipelines.MULTI_MODES[mode] / 2 >= pipelines.MULTI_CENTER_GUARD_HZ


def test_a_channel_on_the_middle_moves_the_centre_off_it():
    plan = pipelines.multi_plan([(154_000_000, "nfm"), (154_500_000, "nfm"), (155_000_000, "nfm")])
    assert plan is not None and abs(plan["center_hz"] - 154_500_000) > 16_000


@pytest.mark.parametrize("channels, why", [
    ([(118_300_000, "am"), (121_700_000, "am")], "span 3.400 MHz"),
    ([(146_000_000 + i * 25_000, "fm") for i in range(9)], "at most 8"),  # the default limit
    ([(146_940_000, "wfm")], "NFM, FM and AM"),
])
def test_what_doesnt_fit_says_why(channels, why):
    assert why in pipelines.multi_problem(channels)


def multi_spec(channels, **overrides):
    plan = pipelines.multi_plan([(c["frequency_hz"], c["modulation"]) for c in channels])
    return pipelines.sanitize_spec({
        "recorder_id": 1, "recorder_name": "S", "source_id": 1, "source_name": "S", "target_id": channels[0]["target_id"],
        "target_name": f"{len(channels)} channels", "kind": "multi", "modulation": "multi",
        "frequency_hz": plan["center_hz"], "rate": plan["rate"], "multi": channels, **overrides,
    })


def channel(target_id, hz, mode="nfm", squelch=-40.0, **extra):
    return {"recorder_id": target_id, "recorder_name": f"r{target_id}", "target_id": target_id, "target_name": f"T{target_id}",
            "frequency_hz": hz, "modulation": mode, "squelch": squelch, **extra}


def test_multi_specs_are_checked():
    good = multi_spec([channel(1, 146_940_000), channel(2, 147_000_000)])
    assert good["kind"] == "multi" and len(good["multi"]) == 2
    with pytest.raises(pipelines.SpecError):
        multi_spec([channel(1, 146_940_000), channel(1, 147_000_000)])  # the same target twice
    with pytest.raises(pipelines.SpecError):
        pipelines.sanitize_spec({**good, "multi": [channel(1, 150_000_000)]})  # outside the span
    with pytest.raises(pipelines.SpecError):
        pipelines.sanitize_spec({**good, "multi": [channel(1, 146_940_000, mode="dmr")]})
    with pytest.raises(pipelines.SpecError):
        pipelines.sanitize_spec({**good, "multi": [channel(1, 146_940_000, squelch=5)]})


def test_rtl_sdr_reads_the_plan():
    cfg = make_config(pytest.importorskip("pathlib").Path("/tmp"))
    args = pipelines.rtl_sdr_args(cfg, 0, multi_spec([channel(1, 146_940_000)], gain_db=28.0, ppm=2))
    assert args[args.index("-s") + 1] == "250000" and args[args.index("-g") + 1] == "28" and args[args.index("-p") + 1] == "2"
    assert int(args[args.index("-b") + 1]) % 16384 == 0


def test_the_resampler_gives_exactly_16_khz_and_keeps_a_tone():
    rate = 15_994.140625  # a channel rate the tuner really gives
    resample = engine.Resampler(rate)
    t = np.arange(int(rate * 2)) / rate
    tone = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    out = np.concatenate([resample(tone[i:i + 997]) for i in range(0, len(tone), 997)])
    assert abs(len(out) - 32_000) <= 2
    spectrum = np.abs(np.fft.rfft(out[1000:17000] * np.hanning(16000)))
    assert abs(np.argmax(spectrum) * 16000 / 16000 - 1000) <= 2


# --- through the API ---------------------------------------------------------------------------
def nfm(admin, name, mhz, **extra):
    return make_target(admin, name=name, hz=int(mhz * 1e6), modulation="nfm", **extra)


def test_a_source_switches_to_multi_channel_and_takes_channels(admin, ctx):
    source = make_source(admin)
    one, two = nfm(admin, "Fire", 154.43), nfm(admin, "EMS", 155.295)
    assert admin.post(f"/api/sources/{source}/channels", json={"target_id": one}).status_code == 409  # single channel still
    admin.post(f"/api/sources/{source}/assign", json={"target_id": one}).raise_for_status()
    admin.put(f"/api/sources/{source}/channel-mode", json={"multichannel": True}).raise_for_status()
    assert admin.post(f"/api/sources/{source}/channels", json={"target_id": one}).status_code == 409  # already there
    admin.post(f"/api/sources/{source}/channels", json={"target_id": two}).raise_for_status()
    spec = ctx.assignments()[ctx.local_node_id]["SER1"]
    assert spec["kind"] == "multi" and [c["target_name"] for c in spec["multi"]] == ["Fire", "EMS"]
    view = next(s for s in admin.get("/api/sources").json() if s["id"] == source)
    assert view["multichannel"] and [c["name"] for c in view["channels"]] == ["Fire", "EMS"]
    assert view["active_target"]["kind"] == "multi" and view["plan"]["rate"] == spec["rate"]
    # Assigning one target is for single-channel SDRs; clearing removes every channel.
    assert admin.post(f"/api/sources/{source}/assign", json={"target_id": one}).status_code == 409
    admin.delete(f"/api/sources/{source}/channels/{one}").raise_for_status()
    assert [c["target_name"] for c in ctx.assignments()[ctx.local_node_id]["SER1"]["multi"]] == ["EMS"]
    admin.post(f"/api/sources/{source}/assign", json={"target_id": None}).raise_for_status()
    assert "SER1" not in ctx.assignments().get(ctx.local_node_id, {})


def test_channels_that_dont_fit_or_arent_voice_are_refused(admin, ctx):
    source = make_source(admin)
    admin.put(f"/api/sources/{source}/channel-mode", json={"multichannel": True}).raise_for_status()
    admin.post(f"/api/sources/{source}/channels", json={"target_id": nfm(admin, "Low", 154.0)}).raise_for_status()
    far = admin.post(f"/api/sources/{source}/channels", json={"target_id": nfm(admin, "Far", 160.0)})
    assert far.status_code == 422 and "covers about" in far.json()["detail"]
    digital = make_target(admin, name="P25", hz=154_100_000, modulation="p25p1")
    assert admin.post(f"/api/sources/{source}/channels", json={"target_id": digital}).status_code == 422


def test_switching_back_to_single_keeps_the_first_channel(admin, ctx):
    source = make_source(admin)
    admin.put(f"/api/sources/{source}/channel-mode", json={"multichannel": True}).raise_for_status()
    for name, mhz in (("A", 154.1), ("B", 154.2), ("C", 154.3)):
        admin.post(f"/api/sources/{source}/channels", json={"target_id": nfm(admin, name, mhz)}).raise_for_status()
    reply = admin.put(f"/api/sources/{source}/channel-mode", json={"multichannel": False}).json()
    assert len(reply["removed"]) == 2
    spec = ctx.assignments()[ctx.local_node_id]["SER1"]
    assert spec["kind"] == "audio" and spec["target_name"] == "A"


def test_switching_to_multi_channel_refuses_a_target_it_cant_record(admin, ctx):
    source = make_source(admin)
    adsb = admin.post("/api/targets", json={"name": "ADS-B", "kind": "adsb", "frequency_hz": 1_090_000_000, "modulation": "adsb"}).json()["id"]
    admin.post(f"/api/sources/{source}/assign", json={"target_id": adsb}).raise_for_status()
    assert admin.put(f"/api/sources/{source}/channel-mode", json={"multichannel": True}).status_code == 409


def test_each_channel_keeps_its_own_squelch_and_the_sdr_its_gain(admin, ctx):
    source = make_source(admin, gain_db=20.7)
    one, two = nfm(admin, "Fire", 154.43, squelch=-45.0), nfm(admin, "EMS", 155.295, squelch=-30.0)
    admin.put(f"/api/sources/{source}/channel-mode", json={"multichannel": True}).raise_for_status()
    for target in (one, two):
        admin.post(f"/api/sources/{source}/channels", json={"target_id": target}).raise_for_status()
    admin.put(f"/api/sources/{source}/profiles/{two}", json={"squelch": -52.0, "gain_db": 40.2}).raise_for_status()
    spec = ctx.assignments()[ctx.local_node_id]["SER1"]
    assert [c["squelch"] for c in spec["multi"]] == [-45.0, -52.0]
    assert spec["gain_db"] == 20.7  # one gain for the stick: a profile's gain doesn't apply here


def test_a_schedule_takes_a_multi_channel_sdr_over(admin, ctx):
    source = make_source(admin)
    admin.put(f"/api/sources/{source}/channel-mode", json={"multichannel": True}).raise_for_status()
    admin.post(f"/api/sources/{source}/channels", json={"target_id": nfm(admin, "Fire", 154.43)}).raise_for_status()
    weather = nfm(admin, "Weather", 162.55)
    with ctx.db() as session:
        session.add(Recorder(name="Weather window", source_id=source, target_id=weather,
                             schedule_days=list(range(7)), schedule_start="00:00", schedule_end="23:59"))
        session.commit()
    spec = ctx.assignments(datetime.now(ctx.cfg.tz).replace(hour=12))[ctx.local_node_id]["SER1"]
    assert spec["kind"] == "multi" and [c["target_name"] for c in spec["multi"]] == ["Weather"]


def test_a_lock_covers_every_channel(admin, ctx, operator):
    source = make_source(admin)
    admin.put(f"/api/sources/{source}/channel-mode", json={"multichannel": True}).raise_for_status()
    admin.post(f"/api/sources/{source}/channels", json={"target_id": nfm(admin, "Fire", 154.43)}).raise_for_status()
    admin.post(f"/api/sources/{source}/lock").raise_for_status()
    other = nfm(admin, "EMS", 155.295)
    assert operator.post(f"/api/sources/{source}/channels", json={"target_id": other}).status_code == 403
    admin.post(f"/api/sources/{source}/channels", json={"target_id": other}).raise_for_status()
    with ctx.db() as session:
        assert all(r.locked for r in session.query(Recorder).filter_by(source_id=source))  # the new one too


def test_live_audio_for_a_channel_needs_it_to_be_one(admin, ctx):
    source = make_source(admin)
    admin.put(f"/api/sources/{source}/channel-mode", json={"multichannel": True}).raise_for_status()
    fire = nfm(admin, "Fire", 154.43)
    admin.post(f"/api/sources/{source}/channels", json={"target_id": fire}).raise_for_status()
    assert admin.get(f"/api/live/{source}", params={"target_id": fire + 100}).status_code == 404
    assert ctx.live_key(ctx.local_node_id, "SER1", fire) == f"{ctx.local_node_id}:SER1#{fire}"


# --- recording, with a fake rtl_sdr ------------------------------------------------------------
FAKE_RTL_SDR = """#!{python}
import sys, time
import numpy as np
args = sys.argv
center, rate = int(args[args.index("-f") + 1]), int(args[args.index("-s") + 1])
rng = np.random.default_rng(1)
block = rate // 20  # 50 ms
first, second = {first}, {second}  # Hz
for n in range(int(3.2 * 20)):
    t = (n * block + np.arange(block)) / rate
    x = 0.005 * (rng.standard_normal(block) + 1j * rng.standard_normal(block))
    if 0.5 <= t[0] < 1.5:  # NFM: a 1 kHz tone at 2.5 kHz deviation
        x += 0.3 * np.exp(1j * (2 * np.pi * (first - center) * t + 2.5 * np.sin(2 * np.pi * 1000 * t)))
    if 2.5 <= t[0] < 2.7:  # a 0.2 s blip on the first channel: too short to keep
        x += 0.3 * np.exp(1j * 2 * np.pi * (first - center) * t)
    if 1.0 <= t[0] < 2.0:  # AM: a 700 Hz tone
        x += 0.3 * (1 + 0.5 * np.sin(2 * np.pi * 700 * t)) * np.exp(2j * np.pi * (second - center) * t)
    out = np.empty(2 * block)
    out[0::2], out[1::2] = x.real, x.imag
    sys.stdout.buffer.write(np.clip(np.round(out * 127.5 + 127.5), 0, 255).astype(np.uint8).tobytes())
    sys.stdout.flush()
    time.sleep(0.02)
time.sleep(30)
"""


async def test_each_channel_records_its_own_calls_even_when_they_overlap(tmp_path):
    first, second = 154_430_000, 154_680_000
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_sdr", FAKE_RTL_SDR.format(python=sys.executable, first=first, second=second))
    cfg = make_config(tmp_path, bin_dirs=[bins], min_call_seconds=0.5)  # the real default: the blip is shorter
    calls, statuses, live = [], {}, {}
    sup = supervisor(cfg, calls, pcm=lambda key, data, rate: live.setdefault(key, 0) or live.__setitem__(key, live[key] + len(data)),
                     statuses=statuses)
    spec = multi_spec([channel(1, first, "nfm"), channel(2, second, "am")])
    try:
        await sup.apply({"SER1": spec})
        assert await wait_for(lambda: len(calls) >= 2, timeout=30)
    finally:
        await sup.shutdown()
    by_target = {meta["target_id"]: meta for meta, _, _ in calls}
    assert set(by_target) == {1, 2} and len(calls) == 2
    assert by_target[1]["target_name"] == "T1" and by_target[1]["frequency_hz"] == first and by_target[1]["modulation"] == "nfm"
    assert by_target[2]["modulation"] == "am"
    for meta in by_target.values():
        assert 0.7 <= meta["duration"] <= 1.6
    assert by_target[1]["started_at"] < by_target[2]["started_at"] < by_target[1]["ended_at"]  # they overlapped
    assert {"SER1#1", "SER1#2"} <= set(live)
    channels = [s.get("channels") for s in statuses["SER1"] if s.get("channels")]
    assert channels and {"1", "2"} <= set(channels[-1])
    loads = [s["load"] for s in statuses["SER1"] if s.get("load") is not None]
    assert loads and 0 < loads[-1] < 1  # processing time over radio time: well under real time
    # On air once per kept call: the blip after the first call never shows.
    on_air = [bool((c.get("1") or {}).get("in_call")) for c in channels]
    assert sum(1 for before, after in zip([False] + on_air, on_air) if after and not before) == 1


def test_suggestions_are_targets_that_fit_and_arent_on_it_yet(admin, ctx):
    source = make_source(admin)
    assert admin.get(f"/api/sources/{source}/channel-suggestions").json() == []  # single channel: none
    admin.put(f"/api/sources/{source}/channel-mode", json={"multichannel": True}).raise_for_status()
    fire = nfm(admin, "Fire", 154.43)
    admin.post(f"/api/sources/{source}/channels", json={"target_id": fire}).raise_for_status()
    near, nearer = nfm(admin, "Near", 154.9), nfm(admin, "Nearer", 154.31)
    nfm(admin, "Far", 160.0)
    make_target(admin, name="P25", hz=154_100_000, modulation="p25p1")
    found = admin.get(f"/api/sources/{source}/channel-suggestions").json()
    assert [t["name"] for t in found] == ["Nearer", "Near"]  # nearest the channels first
    admin.post(f"/api/sources/{source}/channels", json={"target_id": near}).raise_for_status()
    assert [t["name"] for t in admin.get(f"/api/sources/{source}/channel-suggestions").json()] == ["Nearer"]
    assert nearer


def test_an_open_squelch_rides_through_a_small_dip():
    from sentrysdr import tuner
    from .test_tuner import CENTER, run
    rate, offset = 250_000, 50_000
    t = np.arange(rate) / rate
    steady = 0.1 * np.exp(2j * np.pi * offset * t)
    level = run(tuner.Settings(CENTER + offset, "nfm"), steady, rate=rate)[0].level_db
    dip = np.where(t < 0.5, 1.0, 10 ** (-1.5 / 20))  # 1.5 dB quieter for the second half
    receiver, audio = run(tuner.Settings(CENTER + offset, "nfm", squelch_db=round(level - 0.5, 1)), steady * dip, rate=rate)
    assert receiver.open and np.any(audio[-2000:])  # still open: without the margin it would have closed


def test_squelch_and_volume_change_without_a_retune():
    spec = multi_spec([channel(1, 146_940_000), channel(2, 147_000_000)])
    quieter = multi_spec([channel(1, 146_940_000, squelch=-30.0), channel(2, 147_000_000, volume_db=6)])
    moved = multi_spec([channel(1, 146_940_000), channel(2, 147_060_000)])
    assert engine.multi_layout(spec) == engine.multi_layout(quieter)
    assert engine.multi_layout(spec) != engine.multi_layout(moved)


async def test_a_running_channel_is_measured_live_without_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "MEASURE_SECONDS", 1.0)
    first, second = 154_430_000, 154_680_000
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_sdr", FAKE_RTL_SDR.format(python=sys.executable, first=first, second=second))
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), [], statuses={})
    try:
        await sup.apply({"SER1": multi_spec([channel(1, first, "nfm"), channel(2, second, "am")], gain_db=28.0)})
        assert await wait_for(lambda: sup.workers["SER1"].state == "listening")
        task = sup.workers["SER1"].task
        result = await sup.measure("SER1", frequency_hz=second, modulation="am", gain_db=None, ppm=0)
        assert result["live"] and result["gain_db"] == 28.0
        assert -130 <= result["low"] <= result["typical"] <= result["high"] <= 0
        assert sup.workers["SER1"].task is task  # the same run: nothing was paused or restarted
    finally:
        await sup.shutdown()


def test_a_slaves_measurement_keeps_its_decimals(ctx):
    import asyncio
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    ctx.pending_measures["m1"] = (7, future, "measure")
    ctx.finish_measure(7, {"id": "m1", "ok": True, "result": {"low": -39.4, "typical": -38.9, "high": -31.2, "suggested": -33.0,
                                                               "moved": True, "live": True, "gain_db": 28.0}})
    assert future.result() == {"low": -39.4, "typical": -38.9, "high": -31.2, "suggested": -33.0, "moved": True,
                               "live": True, "gain_db": 28.0}
    loop.close()



# --- how many channels: a limit per SDR, and a check of its machine ----------------------------
def test_each_sdr_has_its_own_limit(admin, ctx):
    source = make_source(admin)
    admin.put(f"/api/sources/{source}", json={"serial": "SER1", "name": "Source 1", "max_channels": 2}).raise_for_status()
    admin.put(f"/api/sources/{source}/channel-mode", json={"multichannel": True}).raise_for_status()
    for name, mhz in (("A", 154.1), ("B", 154.2)):
        admin.post(f"/api/sources/{source}/channels", json={"target_id": nfm(admin, name, mhz)}).raise_for_status()
    third = admin.post(f"/api/sources/{source}/channels", json={"target_id": nfm(admin, "C", 154.3)})
    assert third.status_code == 422 and "at most 2" in third.json()["detail"] and "Check this machine" in third.json()["detail"]
    assert admin.get(f"/api/sources/{source}/channel-suggestions").json() == []  # full: nothing else fits
    view = next(v for v in admin.get("/api/sources").json() if v["id"] == source)
    assert view["max_channels"] == 2
    # Lowered after the fact, the extra channel is left out, not dropped from the assignment.
    admin.put(f"/api/sources/{source}", json={"serial": "SER1", "name": "Source 1", "max_channels": 1}).raise_for_status()
    assert [c["target_name"] for c in ctx.assignments()[ctx.local_node_id]["SER1"]["multi"]] == ["A"]
    view = next(v for v in admin.get("/api/sources").json() if v["id"] == source)
    assert [c["fits"] for c in view["channels"]] == [True, False]
    assert admin.put(f"/api/sources/{source}", json={"serial": "SER1", "name": "Source 1", "max_channels": 17}).status_code == 422


def test_the_machine_check_recommends_a_number():
    from sentrysdr import tuner
    found = tuner.benchmark(pipelines.MULTI_MAX_CHANNELS, seconds=0.2)
    assert found["overhead"] == tuner.BENCHMARK_OVERHEAD
    for span in ("typical", "widest"):
        part = found[span]
        assert part["rate"] == tuner.BENCHMARK_RATES[span] and 0 <= part["fixed"] < 2 and 0 <= part["per_channel"] < 1
        assert 0 <= part["recommended"] <= pipelines.MULTI_MAX_CHANNELS
        if part["recommended"]:
            assert part["fixed"] + part["recommended"] * part["per_channel"] <= tuner.BENCHMARK_TARGET_LOAD
    assert found["typical"]["recommended"] >= found["widest"]["recommended"]  # a narrower span costs less


async def test_the_machine_check_runs_on_the_node_without_its_stick(tmp_path):
    sup = supervisor(make_config(tmp_path), [])
    try:
        found = await sup.measure("SER1", op="benchmark")
        assert found["typical"]["recommended"] >= 1  # a desktop running these tests manages at least one
    finally:
        await sup.shutdown()


def test_a_slaves_check_is_cleaned():
    from sentrysdr.context import clean_benchmark
    span = {"rate": 2_400_000, "fixed": 0.05, "per_channel": 0.01, "recommended": 16}
    good = {"typical": {**span, "rate": 1_024_000}, "widest": span, "target_load": 0.5, "overhead": 3.0}
    assert clean_benchmark(good) == good
    for bad in ({**good, "widest": {**span, "recommended": 99}}, {**good, "typical": {**span, "fixed": "x"}},
                {**good, "widest": {**span, "per_channel": float("nan")}}, {**good, "widest": {**span, "rate": 7}}, {"rate": 1}):
        with pytest.raises((ValueError, KeyError, TypeError)):
            clean_benchmark(bad)


def test_a_kept_schedule_becomes_one_more_channel(ctx, admin):
    from .test_radio import end_now, make_once
    source = make_source(admin)
    admin.put(f"/api/sources/{source}/channel-mode", json={"multichannel": True}).raise_for_status()
    fire = nfm(admin, "Fire", 154.43)
    admin.post(f"/api/sources/{source}/channels", json={"target_id": fire}).raise_for_status()
    near, far = nfm(admin, "EMS", 155.295), nfm(admin, "Far", 160.0)
    keep_near, keep_far = make_once(ctx, admin, source, near, "keep"), make_once(ctx, admin, source, far, "keep", length=7200)
    ctx.track_schedule_ends()
    end_now(ctx, keep_near)
    assert ctx.track_schedule_ends() == ["Source 1: EMS"]
    with ctx.db() as session:  # Far's schedule still has the SDR for now, but EMS joined its channels and Fire stayed
        assert [r.target_id for r in session.query(Recorder).filter_by(source_id=source, schedule_days=None).order_by(Recorder.id)] == [fire, near]
    end_now(ctx, keep_far)
    assert ctx.track_schedule_ends() == []  # doesn't fit: skipped, and the audit log says why
    assert [c["target_name"] for c in ctx.assignments()[ctx.local_node_id]["SER1"]["multi"]] == ["Fire", "EMS"]


# --- schedules: take over, or add a channel ------------------------------------------------------
def schedule_on(ctx, source, target, mode=None, name="Window"):
    with ctx.db() as session:
        recorder = Recorder(name=name, source_id=source, target_id=target, schedule_days=list(range(7)),
                            schedule_start="00:00", schedule_end="23:59", schedule_mode=mode)
        session.add(recorder)
        session.commit()
        return recorder.id


def running_names(ctx):
    spec = ctx.assignments(datetime.now(ctx.cfg.tz).replace(hour=12))[ctx.local_node_id]["SER1"]
    return [c["target_name"] for c in spec["multi"]] if spec["kind"] == "multi" else [spec["target_name"]]


def multi_source(admin, *channels):
    source = make_source(admin)
    admin.put(f"/api/sources/{source}/channel-mode", json={"multichannel": True}).raise_for_status()
    for name, mhz in channels:
        admin.post(f"/api/sources/{source}/channels", json={"target_id": nfm(admin, name, mhz)}).raise_for_status()
    return source


def test_every_channel_comes_back_after_a_takeover(admin, ctx):
    source = multi_source(admin, ("Fire", 154.43), ("EMS", 155.295))
    window = schedule_on(ctx, source, nfm(admin, "Weather", 162.55))
    assert running_names(ctx) == ["Weather"]
    with ctx.db() as session:
        session.get(Recorder, window).enabled = False  # its window over, or turned off
        session.commit()
    assert running_names(ctx) == ["Fire", "EMS"]


def test_a_schedule_can_add_a_channel_while_the_others_carry_on(admin, ctx):
    source = multi_source(admin, ("Fire", 154.43), ("EMS", 155.295))
    adding = schedule_on(ctx, source, nfm(admin, "Event", 154.9), mode="add")
    assert running_names(ctx) == ["Fire", "EMS", "Event"]
    view = next(s for s in admin.get("/api/sources").json() if s["id"] == source)
    assert [(c["name"], c["schedule"]) for c in view["channels"]] == [("Fire", None), ("EMS", None), ("Event", "Window")]
    assert not view["taken_over"] and view["active_target"]["kind"] == "multi"
    with ctx.db() as session:
        assert ctx.running_schedules(session, list(session.query(Recorder)), datetime.now(ctx.cfg.tz).replace(hour=12)) == {adding}


def test_an_added_channel_that_doesnt_fit_takes_over_instead(admin, ctx):
    source = multi_source(admin, ("Fire", 154.43))
    schedule_on(ctx, source, nfm(admin, "Far", 160.0), mode="add")
    assert running_names(ctx) == ["Far"]
    view = next(s for s in admin.get("/api/sources").json() if s["id"] == source)
    assert view["taken_over"]


def test_a_takeover_beats_an_add_and_a_single_channel_sdr_is_taken_over_either_way(admin, ctx):
    source = multi_source(admin, ("Fire", 154.43))
    schedule_on(ctx, source, nfm(admin, "Event", 154.9), mode="add", name="Add")
    schedule_on(ctx, source, nfm(admin, "Weather", 162.55), name="Take")
    assert running_names(ctx) == ["Weather"]
    single = make_source(admin, "SER2", "Two")
    admin.post(f"/api/sources/{single}/assign", json={"target_id": nfm(admin, "Usual", 146.94)}).raise_for_status()
    schedule_on(ctx, single, nfm(admin, "Other", 147.0), mode="add")
    spec = ctx.assignments(datetime.now(ctx.cfg.tz).replace(hour=12))[ctx.local_node_id]["SER2"]
    assert spec["kind"] == "audio" and spec["target_name"] == "Other"


def test_saving_an_add_schedule_says_when_it_will_take_over(admin, ctx):
    source = multi_source(admin, ("Fire", 154.43))
    window = {"name": "W", "source_id": source, "repeat": "weekly", "schedule_days": [0, 1, 2, 3, 4, 5, 6],
              "schedule_start": "08:00", "schedule_end": "09:00", "mode": "add"}
    near = admin.post("/api/recorders", json={**window, "target_id": nfm(admin, "Event", 154.9)}).json()
    assert near["mode"] == "add" and not near.get("warning")
    far = admin.post("/api/recorders", json={**window, "target_id": nfm(admin, "Far", 160.0)}).json()
    assert "takes the SDR over" in far["warning"]
    assert admin.post("/api/recorders", json={**window, "target_id": nfm(admin, "X", 154.2), "mode": "sideways"}).status_code == 422


# --- the web tuner on a recording multi-channel SDR, view-only ----------------------------------
async def test_the_tuner_can_watch_and_listen_while_it_records(tmp_path):
    import json
    first, second = 154_430_000, 154_680_000
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_sdr", FAKE_RTL_SDR.format(python=sys.executable, first=first, second=second))
    calls, out = [], []
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), calls, statuses={})
    sup.tuner_out = lambda serial, kind, data: out.append((kind, data))
    spec = multi_spec([channel(1, first, "nfm"), channel(2, second, "am")])
    try:
        await sup.apply({"SER1": spec})
        assert await wait_for(lambda: sup.workers["SER1"].tuner is not None)
        listen = {"frequency_hz": first, "mode": "nfm", "deemphasis_us": 75}
        assert sup.tuner_command("SER1", {"op": "sync", "watching": True, "listeners": {"5": listen}})
        sup.tuner_command("SER1", {"op": "center", "hz": 160_000_000})  # ignored: it's recording
        assert await wait_for(lambda: len(calls) >= 2, timeout=30)
    finally:
        await sup.shutdown()
    frames = [data for kind, data in out if kind == "frame"]
    assert any(f[:1] == b"W" for f in frames)  # the waterfall
    audio = [f for f in frames if f[:1] == b"A"]
    assert audio and all(int.from_bytes(f[1:5], "little") == 5 for f in audio)  # its own channel, to its listener
    states = [d for kind, d in out if kind == "json" and d.get("t") == "state" and d.get("running")]
    assert states and states[0]["view_only"] and states[0]["center_hz"] == spec["frequency_hz"]
    assert [c["name"] for c in states[0]["channels"]] == ["T1", "T2"]
    assert any(kind == "json" and d.get("t") == "rx" and d.get("listener") == 5 for kind, d in out)
    assert {meta["target_id"] for meta, _, _ in calls} == {1, 2}  # recording carried on
    json.dumps([d for kind, d in out if kind == "json"])  # what goes to the master is plain JSON


def test_nobody_can_take_a_view_only_tuner():
    from sentrysdr.tuner_hub import TunerError, TunerHub
    hub = TunerHub(lambda *_: True)
    viewer = hub.join((1, "S"), 1, "Ada", is_admin=True, can_own=True)
    hub.from_node(1, "S", "json", {"t": "state", "running": True, "center_hz": 154_500_000, "rate": 1_024_000,
                                   "view_only": True, "channels": [{"frequency_hz": 154_430_000, "name": "Fire", "modulation": "nfm"}]})
    with pytest.raises(TunerError, match="recording"):
        hub.command((1, "S"), viewer, {"t": "claim"})
    import json
    state = json.loads([item for item in list(viewer.queue._queue) if item[0] == "text"][-1][1])
    assert state["view_only"] and not state["can_claim"] and not state["can_take"] and state["channels"][0]["name"] == "Fire"


def test_a_multi_channel_sdr_is_listed_as_a_tuner(admin, ctx):
    source = multi_source(admin, ("Fire", 154.43))
    listed = admin.get("/api/tuners").json()
    assert [t["source_id"] for t in listed] == [source] and listed[0]["view_only"]
