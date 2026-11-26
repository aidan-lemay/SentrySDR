"""NFM and FM de-emphasis: the treble boost every two-way radio sends with, cut back as a receiver does."""
from __future__ import annotations

import math

import numpy as np
import pytest

from sentrysdr import pipelines, tuner
from sentrysdr.engine import AudioFilter

from .test_tone_filter import from_bytes, level_db, pcm, to_bytes
from .test_tuner import CENTER, RATE, run, times, tone_level


def response_db(hz: float, modulation: str = "nfm") -> float:
    before = pcm((hz, 0.1))
    return level_db(from_bytes(AudioFilter()(to_bytes(before), modulation, None, pipelines.PCM_RATE)), hz) - level_db(before, hz)


@pytest.mark.parametrize("modulation", ["nfm", "fm"])
def test_fm_is_de_emphasised_with_its_reference_unchanged(modulation):
    ref = pipelines.DEEMPHASIS_REFERENCE_HZ
    assert abs(response_db(ref, modulation)) < 0.2
    expected = lambda hz: 20 * math.log10(math.hypot(1, ref / 212.2) / math.hypot(1, hz / 212.2))  # noqa: E731
    for hz in (300, 500, 1000, 2000, 3000):
        assert abs(response_db(hz, modulation) - expected(hz)) < 0.5, hz
    assert response_db(3000, modulation) < -11


@pytest.mark.parametrize("modulation", ["am", "wfm", "dmr"])
def test_other_modes_pass_untouched(modulation):
    audio = to_bytes(pcm((3000, 0.3)))
    assert AudioFilter()(audio, modulation, None, pipelines.PCM_RATE) == audio


def test_a_loud_voice_with_a_loud_tone_doesnt_clip_in_between():
    """De-emphasis lifts a CTCSS tone about 13 dB before the tone filter takes it out. Nothing is rounded in between."""
    loud = to_bytes(pcm((PL_HZ := 179.9, 0.3), (1000, 0.6)))
    out = from_bytes(AudioFilter()(loud, "nfm", "high", pipelines.PCM_RATE))
    assert np.max(np.abs(out[pipelines.PCM_RATE // 2:])) < 0.7
    assert level_db(out, PL_HZ) < level_db(from_bytes(loud), PL_HZ) - 35


def test_chunks_join_without_a_seam():
    audio = to_bytes(pcm((179.9, 0.2), (700, 0.3), seconds=1))
    whole = AudioFilter()(audio, "nfm", "medium", pipelines.PCM_RATE)
    chunked = AudioFilter()
    assert b"".join(chunked(audio[i:i + 3000], "nfm", "medium", pipelines.PCM_RATE) for i in range(0, len(audio), 3000)) == whole


def fm(*tones, offset=200_000, carrier_error=0.0):
    t = times(1.0)
    message = sum(amp * np.sin(2 * np.pi * hz * t) for hz, amp in tones)
    phase = 2 * np.pi * 2_500 * np.cumsum(message) / RATE
    return 0.5 * np.exp(1j * (2 * np.pi * (offset + carrier_error) * t + phase))


def test_the_multi_channel_receiver_de_emphasises_too():
    receiver, audio = run(tuner.Settings(CENTER + 200_000, "nfm"), fm((1000, 0.5), (3000, 0.5)))
    tilt = 20 * math.log10(tone_level(audio, receiver.audio_rate, 3000) / tone_level(audio, receiver.audio_rate, 1000))  # 1 kHz to 3 kHz
    assert abs(tilt - 20 * math.log10(math.hypot(1, 1000 / 212.2) / math.hypot(1, 3000 / 212.2))) < 1


def test_the_multi_channel_receiver_takes_out_a_carriers_dc():
    receiver, audio = run(tuner.Settings(CENTER + 200_000, "nfm"), fm((1000, 0.5), carrier_error=800))
    settled = audio[int(0.3 * receiver.audio_rate):]
    assert abs(np.mean(settled)) < 0.01 and np.max(np.abs(settled)) < 1


def test_a_single_channel_sdrs_dc_is_taken_out_before_de_emphasis():
    offset = to_bytes(np.full(pipelines.PCM_RATE, 0.05) + pcm((1000, 0.2), seconds=1))
    out = from_bytes(AudioFilter()(offset, "nfm", None, pipelines.PCM_RATE))
    assert abs(np.mean(out[pipelines.PCM_RATE // 2:])) < 0.002
