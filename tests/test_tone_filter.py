"""The tone filter: a target's CTCSS tone taken out of its audio, off | low (250 Hz) | high (300 Hz)."""
from __future__ import annotations

import math
import shutil
import subprocess
import sys

import numpy as np
import pytest

from sentrysdr import pipelines, tuner
from sentrysdr.engine import AudioFilter
from sentrysdr.models import Target

from .test_radio import make_target
from .test_tuner import CENTER, RATE, run, times, tone_level

PL_HZ = 179.9


class ToneFilter:
    """The tone filter alone: an audio filter for a mode without de-emphasis."""

    def __init__(self):
        self.audio = AudioFilter()

    def __call__(self, chunk: bytes, mode, rate: int) -> bytes:
        return self.audio(chunk, "raw", mode, rate)  # the tone in the call that set off ClearCut's tone decoder


def pcm(*tones: tuple[float, float], seconds: float = 2.0, rate: int = pipelines.PCM_RATE) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return sum(amp * np.sin(2 * np.pi * hz * t) for hz, amp in tones)


def to_bytes(signal: np.ndarray) -> bytes:
    return (np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes()


def from_bytes(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype(np.float64) / 32767


def level_db(signal: np.ndarray, hz: float, rate: int = pipelines.PCM_RATE) -> float:
    settled = signal[rate // 2:]
    n = np.arange(len(settled))
    return 20 * math.log10(abs(np.mean(settled * np.exp(-2j * np.pi * hz * n / rate))) + 1e-12)


@pytest.mark.parametrize("mode, least_cut", [("low", 18), ("medium", 45), ("high", 50)])
def test_the_filter_takes_out_a_pl_tone_and_keeps_the_voice(mode, least_cut):
    before = pcm((PL_HZ, 0.3), (1000, 0.3))
    after = from_bytes(ToneFilter()(to_bytes(before), mode, pipelines.PCM_RATE))
    assert level_db(before, PL_HZ) - level_db(after, PL_HZ) >= least_cut
    assert abs(level_db(before, 1000) - level_db(after, 1000)) < 0.5
    assert abs(level_db(before, 1000) - level_db(after, 1000) + 20 * math.log10(pipelines.tone_filter_gain(mode, 1000))) < 0.1


def test_off_passes_the_audio_untouched():
    audio = to_bytes(pcm((PL_HZ, 0.3)))
    assert ToneFilter()(audio, None, pipelines.PCM_RATE) == audio


def test_chunks_join_without_a_seam():
    audio = to_bytes(pcm((PL_HZ, 0.3), (700, 0.3), seconds=1))
    whole = ToneFilter()(audio, "high", pipelines.PCM_RATE)
    chunked = ToneFilter()
    pieces = b"".join(chunked(audio[i:i + 4096], "high", pipelines.PCM_RATE) for i in range(0, len(audio), 4096))
    assert pieces == whole


def test_a_loud_tone_doesnt_wrap_around():
    loud = to_bytes(pcm((PL_HZ, 0.2), (400, 0.99)))
    out = from_bytes(ToneFilter()(loud, "high", pipelines.PCM_RATE))
    assert np.max(np.abs(np.diff(out))) < 0.5  # a wrapped sample would jump by nearly 2


CTCSS_HZ = [67.0, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8,
            123.0, 127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9, 171.3, 173.8, 177.3,
            179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8,
            250.3, 254.1]


@pytest.mark.parametrize("rate", [pipelines.PCM_RATE, pipelines.FM_WIDE_RATE])
def test_high_takes_out_every_ctcss_tone_and_none_of_the_voice(rate):
    for hz in CTCSS_HZ:
        assert 20 * math.log10(pipelines.tone_filter_gain("high", hz, rate)) < -pipelines.TONE_STOP_DB + 0.5, hz
    for hz in (300, 350, 500, 1000, 2000, 3000):
        assert 20 * math.log10(pipelines.tone_filter_gain("high", hz, rate)) > -pipelines.TONE_PASS_DB, hz
    assert all(a2 < 1 for *_, a2 in pipelines.tone_filter_stages("high", rate))  # every pole inside: stable


def test_medium_leaves_some_of_the_highest_tones():
    assert 20 * math.log10(pipelines.tone_filter_gain("medium", 254.1)) > -30
    assert 20 * math.log10(pipelines.tone_filter_gain("medium", 179.9)) < -45


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.parametrize("mode", ["low", "medium"])
def test_it_matches_the_ffmpeg_filter_that_was_listened_to(mode):
    cutoff, stages = pipelines.TONE_FILTERS[mode]
    audio = to_bytes(pcm((PL_HZ, 0.3), (1000, 0.3), (450, 0.2), seconds=1))
    chain = ",".join([f"highpass=f={cutoff:g}:poles=2"] * stages)
    ffmpeg = subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(pipelines.PCM_RATE), "-ac", "1", "-i", "pipe:0",
                             "-af", chain, "-f", "s16le", "pipe:1"], input=audio, capture_output=True, check=True).stdout
    ours = from_bytes(ToneFilter()(audio, mode, pipelines.PCM_RATE))
    theirs = from_bytes(ffmpeg)[:len(ours)]
    # ffmpeg rounds to 16 bits between stages, which this doesn't: about 70 dB down, inaudible.
    assert np.max(np.abs(ours[:len(theirs)] - theirs)) < 16 / 32767


def test_the_multi_channel_receiver_filters_in_its_fft():
    t = times(1.0)
    offset = 200_000
    message = np.sin(2 * np.pi * PL_HZ * t) * 0.3 + np.sin(2 * np.pi * 1_000 * t)
    phase = 2 * np.pi * 2_500 * np.cumsum(message) / RATE
    signal = 0.5 * np.exp(1j * (2 * np.pi * offset * t + phase))
    plain, plain_audio = run(tuner.Settings(CENTER + offset, "nfm"), signal)
    filtered, filtered_audio = run(tuner.Settings(CENTER + offset, "nfm", tone_filter="medium"), signal)
    cut = 20 * math.log10(tone_level(plain_audio, plain.audio_rate, PL_HZ) / tone_level(filtered_audio, filtered.audio_rate, PL_HZ))
    kept = 20 * math.log10(tone_level(plain_audio, plain.audio_rate, 1000) / tone_level(filtered_audio, filtered.audio_rate, 1000))
    assert cut > 40 and abs(kept) < 1.5


def test_a_receivers_filter_changes_without_rebuilding_it():
    receiver = tuner.Receiver(RATE, 8192, tuner.Settings(CENTER, "nfm"), CENTER)
    decimator, before = receiver.audio, receiver.audio.gain.copy()
    receiver.set_tone_filter("high")
    assert receiver.audio is decimator and not np.allclose(receiver.audio.gain, before)
    receiver.set_tone_filter(None)
    assert np.allclose(receiver.audio.gain, before)


def test_specs_carry_the_filter_only_for_fm():
    base = {"recorder_id": 1, "source_id": 1, "target_id": 1, "frequency_hz": 460_000_000, "modulation": "nfm", "tone_filter": "high"}
    assert pipelines.sanitize_spec(base)["tone_filter"] == "high"
    assert pipelines.sanitize_spec({**base, "modulation": "am"})["tone_filter"] is None
    assert pipelines.sanitize_spec({**base, "tone_filter": None})["tone_filter"] is None
    with pytest.raises(pipelines.SpecError):
        pipelines.sanitize_spec({**base, "tone_filter": "loud"})


def test_targets_save_the_filter_mode(admin, ctx):
    fm = make_target(admin, "FD", modulation="nfm", tone_filter="high")
    medium = make_target(admin, "FD2", hz=461_000_000, modulation="fm", tone_filter="medium")
    am = make_target(admin, "Tower", hz=120_000_000, modulation="am", tone_filter="high")
    plain = make_target(admin, "Old", hz=446_000_000)
    views = {t["name"]: t["tone_filter"] for t in admin.get("/api/targets").json()}
    assert views == {"FD": "high", "FD2": "medium", "Tower": "off", "Old": "off"}
    with ctx.db() as session:
        assert (session.get(Target, fm).tone_filter, session.get(Target, am).tone_filter, session.get(Target, plain).tone_filter) == ("high", None, None)
    assert admin.post("/api/targets", json={"name": "x", "frequency_hz": 460_000_000, "tone_filter": "max"}).status_code == 422


def test_the_filter_reaches_the_sdrs_spec(admin, ctx):
    admin.post("/api/sources", json={"serial": "SER1", "name": "S"}).raise_for_status()
    target = make_target(admin, "FD", modulation="nfm", tone_filter="low")
    admin.post("/api/sources/1/assign", json={"target_id": target}).raise_for_status()
    specs = [spec for node in ctx.assignments().values() for spec in node.values()]
    assert [spec["tone_filter"] for spec in specs] == ["low"]


@pytest.mark.skipif(sys.platform == "win32", reason="timing")
def test_the_python_filter_keeps_up_easily():
    import time
    audio = to_bytes(pcm((PL_HZ, 0.3), (1000, 0.3), seconds=2))
    started = time.perf_counter()
    ToneFilter()(audio, "high", pipelines.PCM_RATE)
    assert time.perf_counter() - started < 0.5  # 2 s of audio; about 15 ms on a laptop


def test_the_multi_channel_receiver_takes_out_the_top_tone_on_high():
    t = times(1.0)
    offset = 200_000
    message = np.sin(2 * np.pi * 254.1 * t) * 0.3 + np.sin(2 * np.pi * 1_000 * t)
    signal = 0.5 * np.exp(1j * (2 * np.pi * offset * t + 2 * np.pi * 2_500 * np.cumsum(message) / RATE))
    plain, plain_audio = run(tuner.Settings(CENTER + offset, "nfm"), signal)
    filtered, filtered_audio = run(tuner.Settings(CENTER + offset, "nfm", tone_filter="high"), signal)
    cut = 20 * math.log10(tone_level(plain_audio, plain.audio_rate, 254.1) / tone_level(filtered_audio, filtered.audio_rate, 254.1))
    kept = 20 * math.log10(tone_level(plain_audio, plain.audio_rate, 1000) / tone_level(filtered_audio, filtered.audio_rate, 1000))
    assert cut > 40 and abs(kept) < 1.5
