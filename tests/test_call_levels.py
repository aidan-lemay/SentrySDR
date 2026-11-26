"""Evening out call levels: one gain per call, from its speech, within limits."""
from __future__ import annotations

import asyncio
import math
import subprocess

import numpy as np
import pytest

from sentrysdr import pipelines

from .conftest import make_config
from .test_engine_nodes import install_tool, spec, supervisor
from .test_radio import make_target

FS = 16_000


def speech(level_db: float, seconds: float = 3.0, gaps: bool = True) -> np.ndarray:
    """A 'voice' at an RMS level while talking, with quiet gaps between words."""
    t = np.arange(int(seconds * FS)) / FS
    voice = np.sin(2 * np.pi * 400 * t) + 0.5 * np.sin(2 * np.pi * 1100 * t)
    voice *= 10 ** (level_db / 20) / np.sqrt(np.mean(voice ** 2))
    if gaps:
        voice *= np.where(np.sin(2 * np.pi * 1.5 * t) > 0, 1.0, 0.01)  # half the time a near-silent gap
    return voice


def pcm(signal: np.ndarray) -> bytes:
    return (np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes()


def gain(signal: np.ndarray, volume_db: float = 0.0) -> float:
    return pipelines.call_level_gain(pcm(signal), FS, volume_db)


def test_a_quiet_call_is_brought_up_to_the_target():
    assert gain(speech(-30)) == pytest.approx(10, abs=0.3)


def test_the_gaps_between_words_dont_count():
    """Half of this call is near silence. Averaging it in would read 3 dB low and over-boost."""
    assert gain(speech(-26, gaps=True)) == pytest.approx(gain(speech(-26, gaps=False)), abs=0.3)


def test_a_hot_call_is_brought_down():
    assert gain(speech(-8)) == pytest.approx(-12, abs=0.3)  # capped at 12 dB of cut


def test_boost_is_capped():
    assert gain(speech(-50)) == pipelines.LEVEL_MAX_BOOST_DB


def test_peaks_never_clip():
    """Mostly quiet, with one loud shout: the shout decides how far it can go."""
    call = speech(-35)
    call[FS:FS + 400] = 0.7 * np.sin(np.arange(400) * 0.3)
    boost = gain(call)
    assert 20 * math.log10(0.7) + boost <= pipelines.LEVEL_PEAK_DB + 0.05


def test_volume_trims_the_target():
    louder = speech(-24) * 10 ** (6 / 20)  # volume +6 dB already applied to the samples
    assert gain(louder, volume_db=6) == pytest.approx(gain(speech(-24)), abs=0.3)


def test_silence_is_left_alone():
    assert pipelines.call_level_gain(b"\0\0" * FS, FS) == 0.0
    assert pipelines.call_level_gain(b"", FS) == 0.0


def test_encode_applies_the_gain():
    args = pipelines.encode_args(make_config_stub(), "in.raw", FS, "out.mp3", 4.5)
    assert args[args.index("-af") + 1] == "volume=4.50dB"
    assert "-af" not in pipelines.encode_args(make_config_stub(), "in.raw", FS, "out.mp3")


def make_config_stub():
    class Stub:
        @staticmethod
        def binary(_name):
            return "ffmpeg"
    return Stub()


def test_targets_save_it(admin):
    make_target(admin, "Level", level_calls=True)
    make_target(admin, "Plain", hz=446_000_000)
    make_target(admin, "Ships", hz=162_000_000, kind="ais", modulation="ais", level_calls=True)
    views = {t["name"]: t["level_calls"] for t in admin.get("/api/targets").json()}
    assert views == {"Level": True, "Plain": False, "Ships": False}


FAKE_QUIET = """\
#!/usr/bin/env python3
import math, struct, sys, time
data = b"".join(struct.pack("<h", int(32767 * 0.05 * math.sin(2 * math.pi * 500 * i / 16000))) for i in range(24000))
for start in range(0, len(data), 3200):
    sys.stdout.buffer.write(data[start:start + 3200]); sys.stdout.flush(); time.sleep(0.02)
time.sleep(60)
"""


def mp3_level_db(path) -> float:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(FS), "-"],
                         capture_output=True, check=True).stdout
    samples = np.frombuffer(raw, dtype="<i2").astype(float) / 32768
    return 20 * math.log10(np.sqrt(np.mean(samples[FS // 4:-FS // 4] ** 2)))


@pytest.mark.parametrize("level_calls", [False, True])
async def test_a_recorded_call_comes_out_at_the_target_level(tmp_path, level_calls):
    install_tool(tmp_path / "bin", "rtl_fm", FAKE_QUIET)
    saved = []

    async def keep(meta, path):
        copy = tmp_path / f"{meta['id']}.mp3"
        copy.write_bytes(path.read_bytes())
        saved.append(copy)

    sup = supervisor(make_config(tmp_path, bin_dirs=[tmp_path / "bin"], hang_seconds=0.4), [])
    sup.call_sink = keep
    try:
        await sup.apply({"SER1": spec(modulation="am", level_calls=level_calls)})
        for _ in range(100):
            if saved:
                break
            await asyncio.sleep(0.05)
    finally:
        await sup.shutdown()
    level = mp3_level_db(saved[0])
    quiet = 20 * math.log10(0.05 / math.sqrt(2))  # about -29 dB as it arrived: within the 12 dB of boost
    assert level == pytest.approx(pipelines.LEVEL_TARGET_DB if level_calls else quiet, abs=1.0)
