"""Tone squelch: finding a call's CTCSS tone or DCS code, tagging the call, and keeping only the right ones."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from sentrysdr import pipelines, subaudible, tuner
from sentrysdr.models import Recording, Target

from .conftest import make_config
from .test_engine_nodes import install_tool, spec, supervisor, wait_for
from .test_radio import make_target, store
from .test_tuner import CENTER, RATE, run, times

FS = 16_000
rng = np.random.default_rng(7)


def voice(seconds: float = 3.0) -> np.ndarray:
    """Loud, bursty noise from 150 Hz to 3 kHz: speech's band, with low notes that overlap the tones."""
    t = np.arange(int(seconds * FS)) / FS
    spectrum = np.fft.rfft(rng.standard_normal(len(t)))
    freqs = np.fft.rfftfreq(len(t), 1 / FS)
    spectrum[(freqs < 150) | (freqs > 3000)] = 0
    noise = np.fft.irfft(spectrum, len(t))
    return 0.6 * noise / np.max(np.abs(noise)) * (np.sin(2 * np.pi * 3 * t) > -0.4)


def dcs(code: str, seconds: float = 3.0, invert: bool = False, amplitude: float = 0.12) -> np.ndarray:
    t = np.arange(int(seconds * FS)) / FS
    word = subaudible._golay_word(code) ^ (0x7FFFFF if invert else 0)
    nrz = np.array([(word >> i) & 1 for i in range(23)])[(np.floor(t * subaudible.DCS_BAUD)).astype(int) % 23] * 2 - 1.0
    spectrum = np.fft.rfft(nrz)
    spectrum[np.fft.rfftfreq(len(t), 1 / FS) > 300] = 0
    return amplitude * np.fft.irfft(spectrum, len(t))


def tone(hz: float, seconds: float = 3.0, amplitude: float = 0.12) -> np.ndarray:
    return amplitude * np.sin(2 * np.pi * hz * np.arange(int(seconds * FS)) / FS)


def found(audio: np.ndarray) -> tuple[str, str] | None:
    collector = subaudible.Collector()
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    for start in range(0, len(pcm), 6400):
        collector.feed(pcm[start:start + 6400])
    return collector.identify()


@pytest.mark.parametrize("hz", [67.0, 100.0, 151.4, 177.3, 179.9, 203.5, 254.1])
def test_each_ctcss_tone_is_found_under_voice(hz):
    assert found(tone(hz) + voice()) == ("ctcss", f"{hz:.1f}")


def test_neighbouring_tones_are_told_apart():
    """159.8 and 162.2 Hz are the closest pair in the table, 2.4 Hz apart."""
    assert found(tone(159.8) + voice()) == ("ctcss", "159.8")
    assert found(tone(162.2) + voice()) == ("ctcss", "162.2")


def test_a_quiet_tone_is_still_found():
    assert found(tone(179.9, amplitude=0.05) + voice()) == ("ctcss", "179.9")


def test_voice_alone_has_no_tone():
    for _ in range(5):
        assert found(voice()) is None


def test_too_short_a_call_has_no_answer():
    assert found(tone(179.9, seconds=0.3)) is None


@pytest.mark.parametrize("code", ["023", "051", "205", "265", "664", "743", "754"])
def test_each_dcs_code_is_found_under_voice(code):
    assert found(dcs(code) + voice()) == ("dcs", f"{code}N")


def test_an_inverted_code_reads_as_its_partner():
    assert subaudible.dcs_partner("023") == "047" and subaudible.dcs_partner("047") == "023"
    assert found(dcs("023", invert=True) + voice()) == ("dcs", "047N")


def test_every_code_has_one_word_and_the_table_knows_all_its_rotations():
    words = {subaudible._golay_word(code) for code in subaudible.DCS_CODES}
    assert len(words) == len(subaudible.DCS_CODES)
    for code in subaudible.DCS_CODES:
        for rotation in subaudible._rotations(subaudible._golay_word(code)):
            assert subaudible.DCS_TABLE[rotation][:3] in (code, subaudible.dcs_partner(code)) or subaudible.DCS_TABLE[rotation].endswith("N")


@pytest.mark.parametrize("rule, heard, passes", [
    (None, None, True), ("detect", None, True), ("detect", ("ctcss", "179.9"), True),
    ("ctcss:179.9", ("ctcss", "179.9"), True), ("ctcss:179.9", ("ctcss", "177.3"), False),
    ("ctcss:179.9", None, False), ("ctcss:179.9", ("dcs", "023N"), False),
    ("dcs:023", ("dcs", "023N"), True), ("dcs:023", ("dcs", "047N"), True), ("dcs:023", ("dcs", "025N"), False),
    ("dcs:023N", ("dcs", "047N"), False), ("dcs:023N", ("dcs", "023N"), True),
])
def test_rules(rule, heard, passes):
    assert subaudible.matches(rule, heard) is passes


@pytest.mark.parametrize("bad", ["ctcss:180.0", "dcs:999", "dcs:023X", "ctcss", "loud", 5])
def test_bad_rules_are_refused(bad):
    with pytest.raises(ValueError):
        subaudible.clean_rule(bad)


def test_specs_carry_tone_squelch_only_for_fm():
    base = {"recorder_id": 1, "source_id": 1, "target_id": 1, "frequency_hz": 453_462_500, "modulation": "nfm"}
    assert pipelines.sanitize_spec({**base, "tone_squelch": "ctcss:179.9"})["tone_squelch"] == "ctcss:179.9"
    assert pipelines.sanitize_spec({**base, "modulation": "am", "tone_squelch": "ctcss:179.9"})["tone_squelch"] is None
    assert pipelines.sanitize_spec({**base, "tone_squelch": "off"})["tone_squelch"] is None
    with pytest.raises(pipelines.SpecError):
        pipelines.sanitize_spec({**base, "tone_squelch": "ctcss:1.0"})


def test_targets_save_tone_squelch(admin, ctx):
    one = make_target(admin, "Col FD Blue", hz=453_462_500, tone_squelch="ctcss:179.9")
    two = make_target(admin, "TN FD M/ASvc", hz=453_350_000, tone_squelch="dcs:664")
    make_target(admin, "Tower", hz=120_000_000, modulation="am", tone_squelch="ctcss:179.9")
    views = {t["name"]: t["tone_squelch"] for t in admin.get("/api/targets").json()}
    assert views == {"Col FD Blue": "ctcss:179.9", "TN FD M/ASvc": "dcs:664", "Tower": "off"}
    with ctx.db() as session:
        assert session.get(Target, one).tone_squelch == "ctcss:179.9" and session.get(Target, two).tone_squelch == "dcs:664"
    for bad in ("ctcss:180.0", "dcs:999", "anything"):
        assert admin.post("/api/targets", json={"name": "x", "frequency_hz": 453_000_000, "tone_squelch": bad}).status_code == 422


def test_a_calls_tone_is_kept_on_its_recording(admin, ctx, tmp_path):
    target = make_target(admin)
    meta_call = store(ctx, tmp_path, target_id=target)
    with ctx.db() as session:
        assert session.get(Recording, meta_call).tone is None
    # As a slave sends it: only a well-formed tag is kept.
    from sentrysdr.web.routes_nodes import TONE_LABEL_RE
    assert TONE_LABEL_RE.fullmatch("179.9 Hz") and TONE_LABEL_RE.fullmatch("DCS 023N")
    assert not TONE_LABEL_RE.fullmatch("<b>179.9 Hz</b>")


# --- on a single-channel SDR, end to end ------------------------------------------------
FAKE_TONES = """\
#!/usr/bin/env python3
import math, struct, sys, time
def burst(hz):
    data = b"".join(struct.pack("<h", int(32767 * (0.12 * math.sin(2 * math.pi * hz * i / 16000)
                                               + 0.3 * math.sin(2 * math.pi * 1000 * i / 16000)))) for i in range(16000))
    for start in range(0, len(data), 3200):
        sys.stdout.buffer.write(data[start:start + 3200]); sys.stdout.flush(); time.sleep(0.02)
for hz in (179.9, 151.4):
    burst(hz)
    time.sleep(0.8)  # the squelch closes between them
time.sleep(60)
"""


@pytest.mark.parametrize("rule, expected", [
    ("detect", ["179.9 Hz", "151.4 Hz"]),
    ("ctcss:179.9", ["179.9 Hz"]),
    ("dcs:023", []),
])
async def test_a_single_channel_sdr_keeps_only_the_right_calls(tmp_path, rule, expected):
    install_tool(tmp_path / "bin", "rtl_fm", FAKE_TONES)
    calls = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[tmp_path / "bin"], hang_seconds=0.4), calls)
    try:
        await sup.apply({"SER1": spec(tone_squelch=rule, tone_filter="high")})
        await asyncio.sleep(4.5)  # both bursts and the gaps after them: a dropped call never arrives
    finally:
        await sup.shutdown()
    assert [meta.get("tone") for meta, *_ in calls] == expected


async def test_tone_squelch_off_collects_nothing(tmp_path):
    install_tool(tmp_path / "bin", "rtl_fm", FAKE_TONES)
    calls = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[tmp_path / "bin"], hang_seconds=0.4), calls)
    try:
        await sup.apply({"SER1": spec()})
        await wait_for(lambda: len(calls) >= 2, timeout=6)
    finally:
        await sup.shutdown()
    assert len(calls) == 2 and all("tone" not in meta for meta, *_ in calls)


# --- on a multi-channel SDR's receiver ------------------------------------------------
def fm(message: np.ndarray, offset: int = 200_000) -> np.ndarray:
    t = times(len(message) / RATE)
    return 0.5 * np.exp(1j * (2 * np.pi * offset * t + 2 * np.pi * 2_500 * np.cumsum(message) / RATE))


@pytest.mark.parametrize("kind, code", [("ctcss", "179.9"), ("dcs", "664")])
def test_the_multi_channel_receiver_keeps_the_band_for_tone_squelch(kind, code):
    t = times(3.0)
    if kind == "ctcss":
        sub = 0.3 * np.sin(2 * np.pi * float(code) * t)
    else:
        word = subaudible._golay_word(code)
        nrz = np.array([(word >> i) & 1 for i in range(23)])[(np.floor(t * subaudible.DCS_BAUD)).astype(int) % 23] * 2 - 1.0
        spectrum = np.fft.rfft(nrz)
        spectrum[np.fft.rfftfreq(len(t), 1 / RATE) > 300] = 0
        sub = 0.3 * np.fft.irfft(spectrum, len(t))
    message = sub + np.sin(2 * np.pi * 1000 * t)
    # Filtered as hard as it goes: tone squelch must still see the tone.
    tuned = tuner.Tuner(RATE, CENTER)
    tuned.listen(1, tuner.Settings(CENTER + 200_000, "nfm", tone_filter="high", subaudio=True))
    from .test_tuner import to_u8
    raw = to_u8(fm(message))
    receiver = tuned.receivers[1]
    collector = subaudible.Collector(receiver.sub_rate)
    for start in range(0, len(raw), 262_144):
        tuned.feed(raw[start:start + 262_144])
        collector.extend(np.concatenate(receiver.sub_out) if receiver.sub_out else [])
        receiver.sub_out = []
    assert collector.identify() == (kind, code if kind == "ctcss" else code + "N")


def test_the_band_is_only_kept_when_asked():
    receiver = tuner.Receiver(RATE, 8192, tuner.Settings(CENTER, "nfm"), CENTER)
    assert receiver.sub is None
    receiver.set_subaudio(True)
    assert receiver.sub is not None and receiver.sub_rate > 600
    receiver.set_subaudio(False)
    assert receiver.sub is None and receiver.sub_out == []
    assert tuner.Receiver(RATE, 8192, tuner.Settings(CENTER, "am", subaudio=True), CENTER).sub is None


# --- as a scanner does it: only the stretches with the tone --------------------------------
def spans(audio: np.ndarray, rule: str):
    collector = subaudible.Collector(seconds=60)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    for start in range(0, len(pcm), 6400):
        collector.feed(pcm[start:start + 6400])
    return subaudible.kept_spans(collector.samples, collector.rate, rule)


def kept(found_spans) -> float:
    return sum(end - start for start, end in found_spans[0])


def test_a_repeater_tail_is_cut_off():
    call = np.concatenate([tone(179.9, 3.0) + voice(3.0), voice(1.5) * 0.2])  # then the tail: carrier, no tone
    result = spans(call, "ctcss:179.9")
    assert kept(result) == pytest.approx(3.0, abs=0.3) and result[1] == ("ctcss", "179.9")


def test_another_user_in_the_same_call_is_cut_out():
    call = np.concatenate([tone(179.9, 3.0) + voice(3.0), tone(118.8, 3.0) + voice(3.0)])
    result = spans(call, "ctcss:179.9")
    assert len(result[0]) == 1 and result[0][0][0] == 0 and result[0][0][1] == pytest.approx(3.0, abs=0.3)
    other = spans(call, "ctcss:118.8")
    assert other[0][0][0] == pytest.approx(3.0, abs=0.3)  # the other way round: only the second user


def test_a_short_fade_doesnt_split_the_call():
    with_tone = tone(179.9, 2.0) + voice(2.0)
    call = np.concatenate([with_tone, voice(0.4), with_tone])
    result = spans(call, "ctcss:179.9")
    assert len(result[0]) == 1 and kept(result) == pytest.approx(4.4, abs=0.3)


def test_a_call_without_the_tone_keeps_nothing():
    assert spans(tone(118.8) + voice(), "ctcss:179.9") == ([], None)
    assert spans(voice(), "ctcss:179.9") == ([], None)


def test_neighbouring_tones_dont_open_each_other():
    assert spans(tone(162.2) + voice(), "ctcss:159.8")[0] == []
    assert kept(spans(tone(159.8) + voice(), "ctcss:159.8")) == pytest.approx(3.0, abs=0.3)


def test_dcs_stretches():
    call = np.concatenate([dcs("664") + voice(), voice(1.5) * 0.2])
    result = spans(call, "dcs:664")
    assert kept(result) == pytest.approx(3.0, abs=0.4) and result[1] == ("dcs", "664N")
    assert spans(dcs("023") + voice(), "dcs:664")[0] == []


FAKE_LATE_TONE = """\
#!/usr/bin/env python3
import math, struct, sys, time
def burst(seconds, pl):
    return b"".join(struct.pack("<h", int(32767 * ((0.12 * math.sin(2 * math.pi * pl * i / 16000) if pl else 0)
                                              + 0.3 * math.sin(2 * math.pi * 1000 * i / 16000)))) for i in range(int(seconds * 16000)))
data = burst(1.5, None) + burst(2.0, 179.9) + burst(1.5, None)  # someone else, then the right user, then a tail
for start in range(0, len(data), 3200):
    sys.stdout.buffer.write(data[start:start + 3200]); sys.stdout.flush(); time.sleep(0.01)
time.sleep(60)
"""


async def test_a_single_channel_sdr_records_only_the_toned_part(tmp_path):
    install_tool(tmp_path / "bin", "rtl_fm", FAKE_LATE_TONE)
    calls = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[tmp_path / "bin"], hang_seconds=0.4), calls)
    started = __import__("time").time()
    try:
        await sup.apply({"SER1": spec(tone_squelch="ctcss:179.9", tone_filter="high")})
        for _ in range(200):
            if calls:
                break
            await asyncio.sleep(0.05)
    finally:
        await sup.shutdown()
    meta = calls[0][0]
    assert meta["tone"] == "179.9 Hz" and meta["duration"] == pytest.approx(2.0, abs=0.4)
    assert meta["started_at"] - started == pytest.approx(1.5, abs=0.6)  # its start moved to where the tone began
