"""The web tuner's Morse decoder: CW, and IDs sent as a tone in FM and AM, from made-up signals."""
from __future__ import annotations

import json

import pytest

np = pytest.importorskip("numpy")

from sentrysdr import morse, tuner, tuner_session  # noqa: E402
from sentrysdr.tuner_hub import TunerHub, clean_listen  # noqa: E402

from .test_tuner import CENTER, run  # noqa: E402

RATE = 250_000  # the tuner's lowest rate keeps these quick
OFFSET = 50_000


def decode(keys) -> morse.MorseTiming:
    timing = morse.MorseTiming()
    timing.feed(keys)
    return timing


# --- timing -----------------------------------------------------------------------------
@pytest.mark.parametrize("wpm", [5, 12, 20, 30, 40])
def test_keyed_text_decodes_at_any_speed(wpm):
    timing = decode(morse.keying("CQ CQ DE K2GXT", wpm))
    assert timing.text.strip() == "CQ CQ DE K2GXT"
    assert timing.wpm == pytest.approx(wpm, rel=0.15)


def test_uneven_hand_sent_timing_still_decodes():
    rng = np.random.default_rng(3)
    keys = morse.keying("PARIS PARIS 73", 15)
    # Stretch or shrink every mark and gap by up to 25%, as a straight key does.
    runs, current, length = [], keys[0], 0
    for key in keys:
        if key == current:
            length += 1
        else:
            runs.append((current, length))
            current, length = key, 1
    runs.append((current, length))
    uneven = []
    for key, length in runs:
        uneven += [key] * max(1, round(length * rng.uniform(0.75, 1.25)))
    assert decode(uneven).text.strip() == "PARIS PARIS 73"


def test_short_dropouts_dont_split_a_dash():
    keys = morse.keying("TEST", 20)
    middle = keys.index(True) + 10
    keys[middle] = False  # one hop of fading in the middle of the first dash
    assert decode(keys).text.strip() == "TEST"


def test_a_glitch_before_the_first_letter_doesnt_spoil_the_rest():
    keys = [False] * 20 + [True] * 2 + [False] * 100 + morse.keying("BDL", 7)
    assert decode(keys).text.strip() == "BDL"


def test_long_nonstop_sending_keeps_its_text_and_stays_small():
    sent = " ".join(["CQ DE K2GXT"] * 40)
    timing = decode(morse.keying(sent, 25))
    assert len(timing.events) <= morse.MorseTiming.MAX_EVENTS
    text = timing.text.strip()
    assert len(text) >= 80 and sent.endswith(text)  # the latest text, with nothing lost or garbled


def test_a_carrier_is_not_morse():
    keys = [False] * 50 + [True] * 600 + [False] * 400  # 3 seconds of carrier
    assert decode(keys).text == ""


def test_unknown_patterns_show_as_a_star():
    timing = morse.MorseTiming()
    unit = 12  # hops
    keys = [False] * 20
    for _ in range(9):  # nine dots: no such letter
        keys += [True] * unit + [False] * unit
    timing.feed(keys + [False] * 100)
    assert timing.text.strip() == "*"


# --- through the receiver ------------------------------------------------------------------
def envelope(text: str, wpm: float, rate: int, lead: float = 0.5) -> np.ndarray:
    keys = np.array([False] * int(lead / morse.HOP_SECONDS) + morse.keying(text, wpm), dtype=np.float64)
    return np.repeat(keys, round(rate * morse.HOP_SECONDS))


def test_cw_mode_decodes_a_keyed_carrier():
    env = envelope("K2GXT TEST", 20, RATE)
    t = np.arange(len(env)) / RATE
    signal = 0.05 * env * np.exp(2j * np.pi * OFFSET * t)
    receiver, _ = run(tuner.Settings(CENTER + OFFSET, "cw", morse=True), signal, rate=RATE)
    view = receiver.morse.view()
    assert view["text"].strip() == "K2GXT TEST" and view["wpm"] == pytest.approx(20, rel=0.15)


def test_cw_noise_alone_decodes_nothing():
    rng = np.random.default_rng(5)
    noise = 0.01 * (rng.standard_normal(RATE * 3) + 1j * rng.standard_normal(RATE * 3))
    receiver, _ = run(tuner.Settings(CENTER + OFFSET, "cw", morse=True), noise, rate=RATE)
    assert receiver.morse.view()["text"].strip() == ""


def test_an_fm_repeaters_id_decodes_from_its_audio():
    env = envelope("WR2ROC", 18, RATE)
    t = np.arange(len(env)) / RATE
    tone = env * np.sin(2 * np.pi * 900 * t)
    phase = 2 * np.pi * 3_000 * np.cumsum(tone) / RATE  # 3 kHz deviation, under the carrier
    signal = 0.1 * np.exp(1j * (2 * np.pi * OFFSET * t + phase))
    receiver, _ = run(tuner.Settings(CENTER + OFFSET, "nfm", morse=True), signal, rate=RATE)
    view = receiver.morse.view()
    assert view["text"].strip() == "WR2ROC" and view["tone_hz"] == pytest.approx(900, abs=70)


def test_a_vor_ident_decodes_from_am_despite_its_navigation_tones():
    # A VOR: 30 Hz and a 9960 Hz subcarrier at 30% each, the ident at 1020 Hz and only 10%, at 7 WPM.
    env = envelope("BDL", 7, RATE)
    t = np.arange(len(env)) / RATE
    modulation = 0.3 * np.cos(2 * np.pi * 30 * t) + 0.3 * np.cos(2 * np.pi * 9960 * t) + 0.1 * env * np.sin(2 * np.pi * 1020 * t)
    signal = 0.05 * (1 + modulation) * np.exp(2j * np.pi * OFFSET * t)
    receiver, _ = run(tuner.Settings(CENTER + OFFSET, "am", morse=True), signal, rate=RATE)
    view = receiver.morse.view()
    assert view["text"].strip() == "BDL" and view["tone_hz"] == pytest.approx(1020, abs=70)


def test_speech_like_audio_decodes_nothing():
    """A voice: a gliding pitch with harmonics, in syllables. It must not turn into letters."""
    rate = 16_000
    rng = np.random.default_rng(9)
    t = np.arange(rate * 4) / rate
    pitch = 120 + 60 * np.sin(2 * np.pi * 0.7 * t) + 20 * np.sin(2 * np.pi * 3.1 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / rate
    voice = sum(rng.uniform(0.2, 1) / h * np.sin(h * phase) for h in range(1, 25))
    syllables = 0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 4 * t))
    audio = (0.2 * voice * syllables + 0.003 * rng.standard_normal(len(t))).astype(np.float32)
    reader = morse.MorseReader(rate, from_audio=True)
    for start in range(0, len(audio), 400):
        reader.push(audio[start:start + 400])
    assert reader.view()["text"].strip() == ""


def test_morse_is_off_unless_asked_and_only_in_its_modes():
    assert tuner.Receiver(RATE, 8192, tuner.Settings(CENTER, "cw"), CENTER).morse is None
    assert tuner.Receiver(RATE, 8192, tuner.Settings(CENTER, "wfm", morse=True), CENTER).morse is None
    assert tuner.Receiver(RATE, 8192, tuner.Settings(CENTER, "am", morse=True), CENTER).morse is not None


# --- the setting, from the browser to the SDR's machine and back ------------------------------------
def test_the_setting_and_its_text_pass_through():
    assert clean_listen({"frequency_hz": CENTER, "mode": "cw", "morse": True})["morse"] is True
    assert clean_listen({"frequency_hz": CENTER, "mode": "cw", "morse": "yes"})["morse"] is False  # only a real true
    assert tuner_session.clean_settings({"frequency_hz": CENTER, "mode": "cw", "morse": True}).morse is True
    hub = TunerHub(lambda *_: True)
    viewer = hub.join((1, "S"), 1, "Ada", is_admin=False)
    while not viewer.queue.empty():
        viewer.queue.get_nowait()
    found = {"text": "CQ DE K2GXT", "wpm": 20, "tone_hz": None}
    hub.from_node(1, "S", "json", {"t": "rx", "listener": viewer.id, "level_db": -50, "morse": found, "other": 1})
    sent = json.loads(viewer.queue.get_nowait()[1])
    assert sent["morse"] == found and "other" not in sent
