"""The web tuner's signal processing, fed made-up signals with known answers."""
from __future__ import annotations

import time

import pytest

np = pytest.importorskip("numpy")

from sentrysdr import tuner  # noqa: E402

RATE = 2_048_000
CENTER = 100_000_000


def to_u8(signal: np.ndarray, noise: float = 0.002, seed: int = 1) -> bytes:
    """A complex signal as rtl_tcp's bytes, with a little noise."""
    rng = np.random.default_rng(seed)
    signal = signal + noise * (rng.standard_normal(len(signal)) + 1j * rng.standard_normal(len(signal)))
    out = np.empty(2 * len(signal), dtype=np.float64)
    out[0::2], out[1::2] = signal.real, signal.imag
    return np.clip(np.round(out * 127.5 + 127.5), 0, 255).astype(np.uint8).tobytes()


def times(seconds: float, rate: int = RATE) -> np.ndarray:
    return np.arange(int(seconds * rate)) / rate


def run(settings: tuner.Settings, signal: np.ndarray, rate: int = RATE, chunk: int = 262_144):
    """Feed the signal through a Tuner in rtl_tcp-sized pieces; return the receiver and its audio."""
    t = tuner.Tuner(rate, CENTER)
    t.listen(1, settings)
    raw = to_u8(signal)
    pieces = []
    for start in range(0, len(raw), chunk):
        _, audio = t.feed(raw[start:start + chunk])
        pieces += audio[1]
    return t.receivers[1], np.concatenate(pieces)


def strongest(audio: np.ndarray, rate: float, skip: float = 0.3) -> float:
    """The loudest frequency in the audio, after the filters settle."""
    audio = audio[int(skip * rate):]
    spectrum = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
    spectrum[:int(len(audio) * 50 / rate)] = 0  # ignore DC and hum
    return np.argmax(spectrum) * rate / len(audio)


def tone_level(audio: np.ndarray, rate: float, hz: float, skip: float = 0.3) -> float:
    audio = audio[int(skip * rate):]
    n = np.arange(len(audio))
    return float(np.abs(np.mean(audio * np.exp(-2j * np.pi * hz * n / rate))))


# --- the modes ----------------------------------------------------------------------
def test_nfm_hears_its_tone():
    t = times(1.0)
    offset = 200_000
    phase = 2 * np.pi * 3_000 * np.cumsum(np.sin(2 * np.pi * 1_000 * t)) / RATE
    receiver, audio = run(tuner.Settings(CENTER + offset, "nfm"), 0.5 * np.exp(1j * (2 * np.pi * offset * t + phase)))
    assert abs(strongest(audio, receiver.audio_rate) - 1_000) < 10
    assert receiver.level_db > -10


def test_am_hears_its_tone():
    t = times(1.0)
    offset = -150_000
    signal = 0.3 * (1 + 0.5 * np.sin(2 * np.pi * 800 * t)) * np.exp(2j * np.pi * offset * t)
    receiver, audio = run(tuner.Settings(CENTER + offset, "am"), signal)
    assert abs(strongest(audio, receiver.audio_rate) - 800) < 10


@pytest.mark.parametrize("mode,wanted,unwanted", [("usb", 1_000, -2_000), ("lsb", -1_500, 1_200)])
def test_sideband_picks_its_side(mode, wanted, unwanted):
    t = times(1.0)
    listen = CENTER + 300_123  # not on a whole FFT bin
    offset = listen - CENTER
    signal = 0.2 * np.exp(2j * np.pi * (offset + wanted) * t) + 0.2 * np.exp(2j * np.pi * (offset + unwanted) * t)
    receiver, audio = run(tuner.Settings(listen, mode), signal)
    rate = receiver.audio_rate
    assert abs(strongest(audio, rate) - abs(wanted)) < 10
    rejection = 20 * np.log10(tone_level(audio, rate, abs(wanted)) / max(tone_level(audio, rate, abs(unwanted)), 1e-9))
    assert rejection > 40, rejection


def test_cw_carrier_is_a_700_hz_note():
    t = times(1.0)
    listen = CENTER - 400_037
    receiver, audio = run(tuner.Settings(listen, "cw"), 0.2 * np.exp(2j * np.pi * (listen - CENTER) * t))
    assert abs(strongest(audio, receiver.audio_rate) - tuner.CW_PITCH_HZ) < 10


def test_squelch_closes_on_noise_and_opens_on_a_signal():
    t = times(0.6)
    quiet, quiet_audio = run(tuner.Settings(CENTER + 100_000, "nfm", squelch_db=-40), 0 * t + 0j)
    assert not quiet.open and not np.any(quiet_audio)
    loud, _ = run(tuner.Settings(CENTER + 100_000, "nfm", squelch_db=-40), 0.3 * np.exp(2j * np.pi * 100_000 * t))
    assert loud.open


def test_outside_the_span_is_silent_and_retuning_brings_it_in():
    t = tuner.Tuner(RATE, CENTER)
    t.listen(1, tuner.Settings(CENTER + 1_500_000, "nfm"))
    assert not t.receivers[1].inside
    _, audio = t.feed(to_u8(0.3 * np.exp(2j * np.pi * 50_000 * times(0.2))))
    assert all(not np.any(a) for a in audio[1])
    t.set_center(CENTER + 1_400_000)
    assert t.receivers[1].inside


# --- broadcast FM, stereo and RDS ------------------------------------------------------------
def rds_block(data: int, offset: str) -> list[int]:
    word = (data << 10) | (tuner.rds_checkword(data) ^ tuner.RDS_OFFSETS[offset])
    return [(word >> (25 - i)) & 1 for i in range(26)]


def rds_bits(pi: int, ps: str, rt: str) -> list[int]:
    groups = []
    for segment in range(4):
        b = (0 << 12) | (5 << 5) | segment  # group 0A, programme type 5
        groups.append((pi, b, 0xE0CD, (ord(ps[segment * 2]) << 8) | ord(ps[segment * 2 + 1])))
    rt = rt.ljust(64)
    for segment in range(16):
        b = (2 << 12) | (5 << 5) | segment
        chars = rt[segment * 4:segment * 4 + 4]
        groups.append((pi, b, (ord(chars[0]) << 8) | ord(chars[1]), (ord(chars[2]) << 8) | ord(chars[3])))
    bits = []
    for a, b, c, d in groups:
        bits += rds_block(a, "A") + rds_block(b, "B") + rds_block(c, "C") + rds_block(d, "D")
    return bits


def fm_broadcast(seconds: float, left_hz: float, right_hz: float | None, rds: list[int] | None, offset: int) -> np.ndarray:
    t = times(seconds)
    theta = 2 * np.pi * 19_000 * t
    left = 0.8 * np.sin(2 * np.pi * left_hz * t)
    right = 0.8 * np.sin(2 * np.pi * right_hz * t) if right_hz else 0 * t
    mpx = 0.9 * ((left + right) / 2 + (left - right) / 2 * np.sin(2 * theta)) + 0.09 * np.sin(theta)
    if rds:
        differential, previous = [], 0
        for bit in (rds * 100)[: int(seconds * tuner.RDS_RATE) + 2]:
            previous ^= bit
            differential.append(previous)
        position = t * tuner.RDS_RATE
        symbol = np.array(differential)[position.astype(int)] * 2 - 1
        biphase = np.where(position % 1 < 0.5, symbol, -symbol)
        mpx += 0.05 * biphase * np.sin(3 * theta)
    phase = 2 * np.pi * 75_000 * np.cumsum(mpx) / RATE
    return 0.5 * np.exp(1j * (2 * np.pi * offset * t + phase))


def test_wfm_mono_hears_the_programme():
    receiver, audio = run(tuner.Settings(CENTER + 400_000, "wfm"), fm_broadcast(1.0, 1_000, 1_000, None, 400_000))
    assert abs(strongest(audio, receiver.audio_rate) - 1_000) < 10


def test_wfm_stereo_separates_left_and_right():
    receiver, audio = run(tuner.Settings(CENTER - 300_000, "wfm_stereo"), fm_broadcast(1.2, 1_000, None, None, -300_000))
    assert receiver.stereo
    left, right = audio[0::2], audio[1::2]
    rate = receiver.audio_rate
    separation = 20 * np.log10(tone_level(left, rate, 1_000) / max(tone_level(right, rate, 1_000), 1e-9))
    assert separation > 20, separation


def test_rds_names_the_station():
    bits = rds_bits(0x54A8, "SENTRY  ", "Testing the web tuner\r")
    receiver, _ = run(tuner.Settings(CENTER + 250_000, "wfm_stereo"), fm_broadcast(4.0, 1_000, 500, bits, 250_000))
    text = receiver.rds.text.view()
    assert text["pi"] == "54A8" and text["ps"] == "SENTRY" and text["pty"] == 5
    assert text["rt"] == "Testing the web tuner", text


def test_rds_checkword_matches_the_standard():
    # Every valid block leaves exactly its offset word as the syndrome.
    for offset in ("A", "B", "C", "D"):
        bits = rds_block(0x1234, offset)
        word = int("".join(map(str, bits)), 2)
        assert tuner.SYNDROMES[tuner.rds_syndrome(word)] == offset


# --- the rest ---------------------------------------------------------------------------
def test_waterfall_shows_a_carrier_where_it_is():
    t = tuner.Tuner(RATE, CENTER)
    rows, _ = t.feed(to_u8(0.3 * np.exp(2j * np.pi * 512_000 * times(0.1))))
    row = np.frombuffer(rows[-1], dtype=np.uint8)
    assert len(row) == 1024 and abs(int(np.argmax(row)) - (512 + 256)) <= 1


def test_mulaw_round_trip():
    x = np.linspace(-1, 1, 1001, dtype=np.float32)
    back = tuner.mulaw_decode(tuner.mulaw(x))
    loud = np.abs(x) > 0.01
    assert np.max(np.abs(back - x)[loud] / np.abs(x)[loud]) < 0.025  # mu-law's steps are a fixed fraction
    assert np.max(np.abs(back - x)[~loud]) < 5e-4 and abs(back[500]) < 1e-3


def test_many_listeners_keep_up_with_real_time():
    """Two seconds of samples, four listeners and a waterfall, well inside two seconds."""
    t = tuner.Tuner(RATE, CENTER)
    for i, mode in enumerate(("nfm", "am", "usb", "wfm")):
        t.listen(i, tuner.Settings(CENTER + 100_000 * (i + 1), mode))
    raw = to_u8(0.2 * np.exp(2j * np.pi * 100_000 * times(2.0)))
    started = time.perf_counter()
    for start in range(0, len(raw), 262_144):
        t.feed(raw[start:start + 262_144])
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, elapsed
