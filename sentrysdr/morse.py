"""Morse decoding for the web tuner: CW, and Morse IDs sent as a tone in FM or AM audio.

Two front ends turn a receiver's signal into key up or key down every few milliseconds:

- CwKeyer, for CW mode: the channel is already filtered to the signal, so its power is the
  keying. Thresholds follow the noise floor and the peaks.
- ToneKeyer, for FM and AM: a short FFT of the audio looks for one steady tone, such as a
  repeater's ID or a VOR's 1020 Hz ident. Key down needs most of the voice band's energy in
  that tone, at about the same pitch as before, which speech rarely manages.

MorseTiming then learns the dot length from what it hears, splits letters and words by the
gaps, and looks the letters up. Nothing here does any IO. numpy is needed.
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np

HOP_SECONDS = 0.005  # key up or down is decided this often
DEBOUNCE_HOPS = 2  # a change must last this long, so a dropout doesn't split a dash
UNIT_MIN, UNIT_MAX = 0.012, 0.3  # a dot from 100 WPM down to 4 WPM
CARRIER_SECONDS = 1.5  # a mark longer than this is a carrier, not Morse
TEXT_LENGTH = 100

MORSE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F", "--.": "G", "....": "H",
    "..": "I", ".---": "J", "-.-": "K", ".-..": "L", "--": "M", "-.": "N", "---": "O", ".--.": "P",
    "--.-": "Q", ".-.": "R", "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X",
    "-.--": "Y", "--..": "Z",
    "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4", ".....": "5", "-....": "6",
    "--...": "7", "---..": "8", "----.": "9",
    ".-.-.-": ".", "--..--": ",", "..--..": "?", "-..-.": "/", "-...-": "=", "-....-": "-", ".-.-.": "+",
    "-.--.": "(", "-.--.-": ")", ".----.": "'", ".-..-.": '"', "---...": ":", "-.-.-.": ";", ".--.-.": "@",
    "-.-.--": "!", ".-...": "&", "...-.-": "<SK>", "........": "<ERR>",
}
TEXT_TO_MORSE = {text: code for code, text in MORSE.items()}


class MorseTiming:
    """Key up and down, one value per hop, into text.

    Marks and gaps are kept and decoded with the latest dot length, so the first letters come out
    right once the speed has been learned. After a pause or a carrier, what's been heard is
    decoded for good, so a new station at another speed doesn't rewrite the last one.
    """

    PAUSE_SECONDS = 3.0
    MAX_EVENTS = 600  # sent without a pause for this long, the text so far is decoded for good

    def __init__(self, hop: float = HOP_SECONDS, mark_bias: float = 0.0):
        self.hop = hop
        self.mark_bias = mark_bias  # how much longer a keyer makes each mark than it was sent
        self.key = False
        self.candidate = False
        self.candidate_hops = 0
        self.run = 0  # hops in the current state
        self.marks: deque[float] = deque(maxlen=24)  # recent mark lengths, to learn the dot from
        self.unit: float | None = None
        self.events: list[list] = []  # ["mark" or "gap", seconds], since the last pause
        self.done = ""  # decoded for good

    @property
    def wpm(self) -> int | None:
        return round(1.2 / self.unit) if self.unit else None  # PARIS: a dot at 1 WPM is 1.2 s

    @property
    def text(self) -> str:
        trailing = [["gap", self.run * self.hop]] if not self.key and self.events else []
        return (self.done + decode_events(self.events + trailing, self.unit))[-TEXT_LENGTH:]

    def feed(self, keys) -> None:
        for down in keys:
            self._hop(bool(down))

    def _hop(self, down: bool) -> None:
        # Debounce: a change counts once it has lasted DEBOUNCE_HOPS, from where it began.
        if down != self.key:
            self.candidate_hops = self.candidate_hops + 1 if down == self.candidate else 1
            self.candidate = down
            if self.candidate_hops >= DEBOUNCE_HOPS:
                # The change began candidate_hops ago: those hops belong to the new state.
                self._ended(self.key, (self.run - self.candidate_hops + 1) * self.hop)
                self.key, self.run, self.candidate_hops = down, self.candidate_hops, 0
                return
        else:
            self.candidate_hops = 0
        self.run += 1
        if not self.key and self.events and self.run * self.hop >= self.PAUSE_SECONDS:
            self._settle()

    def _ended(self, was_down: bool, seconds: float) -> None:
        if seconds <= 0:
            return
        if not was_down:
            if self.events:
                self.events.append(["gap", seconds + self.mark_bias])
                if len(self.events) > self.MAX_EVENTS and self.unit is not None:
                    self._settle_words()
            return
        seconds = max(self.hop, seconds - self.mark_bias)
        if seconds > CARRIER_SECONDS:
            self._settle()  # a carrier, or someone tuning up
            return
        if self.unit is None or seconds >= 0.3 * self.unit:  # shorter is a glitch: don't learn from it
            self.marks.append(seconds)
            self._learn()
        self.events.append(["mark", seconds])

    def _settle_words(self) -> None:
        """Decode for good up to the last gap between words, keeping the rest."""
        ends = [i for i, (kind, seconds) in enumerate(self.events) if kind == "gap" and seconds >= 5 * self.unit]
        if not ends:
            return self._settle()
        kept, self.events = self.events[ends[-1] + 1:], self.events[:ends[-1] + 1]
        self._settle()
        self.events = kept

    def _settle(self) -> None:
        text = decode_events(self.events, self.unit).strip()
        if text:
            self.done = (self.done + text + " ")[-TEXT_LENGTH:]
        self.events = []

    def _learn(self) -> None:
        """The dot length: the short group of recent marks, where dots and dashes differ about threefold."""
        marks = sorted(self.marks)
        if len(marks) >= 2:
            # Once there are a few marks, each group needs two, so one glitch can't be "the dots".
            least = 2 if len(marks) >= 4 else 1
            splits = range(least - 1, len(marks) - least)
            ratios = {i: marks[i + 1] / marks[i] for i in splits}
            split = max(ratios, key=ratios.__getitem__) if ratios else None
            if split is not None and ratios[split] >= 1.8:
                self.unit = float(np.median(marks[:split + 1]))
            elif self.unit is None or not 0.5 * self.unit <= marks[len(marks) // 2] <= 4 * self.unit:
                self.unit = float(np.median(marks))  # all alike: take them as dots until a dash shows
        elif self.unit is None:
            self.unit = marks[0]
        self.unit = min(UNIT_MAX, max(UNIT_MIN, self.unit))


def tidy_events(events: list[list], unit: float) -> list[list]:
    """Marks and gaps under a third of a dot are noise: a dip inside a dash, or a click inside a gap.

    Each joins the event before it, and the event after it then continues that one.
    """
    out: list[list] = []
    for kind, seconds in events:
        if not out:
            if kind == "mark" and seconds >= 0.3 * unit:  # nothing before a leading glitch to join
                out.append([kind, seconds])
        elif seconds < 0.3 * unit or kind == out[-1][0]:
            out[-1][1] += seconds
        else:
            out.append([kind, seconds])
    return out


def decode_events(events: list[list], unit: float | None) -> str:
    """Marks and gaps as text, at this dot length. A gap of 2 dots ends a letter, 5 a word."""
    if unit is None:
        return ""
    text, letter = "", ""
    events = tidy_events(events, unit)
    for kind, seconds in events:
        if kind == "mark":
            letter += "." if seconds < 2 * unit else "-"
        elif seconds >= 2 * unit and letter:
            text += MORSE.get(letter, "*")
            letter = ""
            if seconds >= 5 * unit:
                text += " "
    return text + (MORSE.get(letter, "*") if letter and events and events[-1][0] == "gap" and events[-1][1] >= 2 * unit else "")


class CwKeyer:
    """CW mode: the carrier's power, against thresholds that follow the noise and the peaks.

    A short FFT of the channel finds the carrier wherever it sits in the filter, and only the
    bins around it count, about 150 Hz: much less noise than the whole channel.
    """

    MIN_SNR_DB = 10.0
    WINDOW_SECONDS = 0.02  # 50 Hz bins
    WIDENS_HOPS = 1.0  # the window makes each mark about this much longer, and each gap shorter
    SETTLE_HOPS = 40  # 200 ms: learn the noise before deciding anything
    CARRIER_DB = 6.0  # over half a second, the carrier's bin must stand this far above a typical bin

    def __init__(self, rate: float, bandwidth: float = 500.0):
        self.per_hop = max(1, round(rate * HOP_SECONDS))
        self.size = max(8, round(rate * self.WINDOW_SECONDS))
        self.window = np.hanning(self.size).astype(np.float32)
        freqs = np.fft.fftfreq(self.size, 1 / rate)
        # Inside the receiver's filter only: bins outside it are nearly empty, and would make any noise
        # inside it look like a carrier. At least 7 bins, for something to compare the carrier with.
        self.search = np.flatnonzero(np.abs(freqs) <= max(bandwidth / 2, 3.5 / self.WINDOW_SECONDS))
        self.average = np.zeros(len(self.search))  # each bin's power, smoothed: where the carrier is
        self.buffer = np.zeros(self.size - self.per_hop, dtype=np.complex64)
        self.floor: float | None = None
        self.peak: float | None = None
        self.heard = 0  # hops since the signal began: the floor settles quickly at first
        self.down = False

    def push(self, iq: np.ndarray) -> list[bool]:
        self.buffer = np.concatenate((self.buffer, iq.astype(np.complex64, copy=False)))
        hops = (len(self.buffer) - self.size) // self.per_hop + 1
        if hops <= 0:
            return []
        starts = np.arange(hops) * self.per_hop
        frames = self.buffer[starts[:, None] + np.arange(self.size)] * self.window
        self.buffer = self.buffer[hops * self.per_hop:]
        power = (np.abs(np.fft.fft(frames, axis=1)) ** 2)[:, self.search]
        keys = []
        for row in power:
            if row.sum() < 1e-12:  # the filters' zeros while they start: not the noise floor
                keys.append(False)
                continue
            self.heard += 1
            self.average += (row - self.average) * 0.01  # about half a second: spans a word's gap
            k = int(np.argmax(self.average))
            carrier = self.average[k] >= 10 ** (self.CARRIER_DB / 10) * float(np.median(self.average))
            p = 10 * math.log10(float(row[max(0, k - 1):k + 2].sum()) + 1e-20)
            if self.floor is None:
                self.floor = self.peak = p
            if self.heard <= self.SETTLE_HOPS:  # the filters are still starting: just learn the noise
                self.floor += (p - self.floor) * 0.2
                self.peak = self.floor
                keys.append(False)
                continue
            # The floor settles on the usual noise, falling quickly and rising slowly, so it isn't
            # pulled down by the noise's dips or up by a long dash. Peaks rise at once and fall 6 dB a second.
            self.floor += (p - self.floor) * (0.05 if p < self.floor else 0.005)
            self.peak = p if p > self.peak else self.peak - 0.03
            span = self.peak - self.floor
            if span < self.MIN_SNR_DB or not carrier:  # noise alone has no steady carrier to follow
                self.down = False
            elif self.down:
                self.down = p > self.floor + 0.25 * span
            else:
                self.down = p > self.floor + 0.5 * span
            keys.append(self.down)
        return keys


class ToneKeyer:
    """FM or AM audio: key down while one steady tone holds most of the voice band."""

    LOW_HZ, HIGH_HZ = 300.0, 1500.0  # where Morse tones are
    BAND_LOW_HZ, BAND_HIGH_HZ = 250.0, 3000.0  # what they must stand out from
    MIN_SHARE = 0.8  # a keyed tone has nearly all of it; a voice's strongest harmonic rarely does
    MIN_POWER = 1e-7  # -70 dB: not digital silence

    def __init__(self, rate: float):
        self.rate = rate
        self.per_hop = max(1, round(rate * HOP_SECONDS))
        self.size = max(16, round(rate * 0.016))  # 16 ms: about 60 Hz between bins
        self.window = np.hanning(self.size).astype(np.float32)
        freqs = np.fft.rfftfreq(self.size, 1 / rate)
        self.freqs = freqs
        self.tone_bins = np.flatnonzero((freqs >= self.LOW_HZ) & (freqs <= self.HIGH_HZ))
        self.band = (freqs >= self.BAND_LOW_HZ) & (freqs <= self.BAND_HIGH_HZ)
        self.buffer = np.zeros(self.size - self.per_hop, dtype=np.float32)
        self.pitches: deque[int] = deque(maxlen=15)  # bins where a tone has been seen lately
        self.tone_hz: float | None = None

    def push(self, audio: np.ndarray) -> list[bool]:
        self.buffer = np.concatenate((self.buffer, audio.astype(np.float32, copy=False)))
        hops = (len(self.buffer) - self.size) // self.per_hop + 1
        if hops <= 0 or not len(self.tone_bins):
            return []
        starts = np.arange(hops) * self.per_hop
        frames = self.buffer[starts[:, None] + np.arange(self.size)] * self.window
        self.buffer = self.buffer[hops * self.per_hop:]
        power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
        band = power[:, self.band].sum(axis=1)
        tones = power[:, self.tone_bins]
        best = self.tone_bins[np.argmax(tones, axis=1)]
        keys = []
        for i, k in enumerate(best):
            # A Hann-windowed tone falls in its bin and the one either side.
            share = float(power[i, k - 1:k + 2].sum() / band[i]) if band[i] > 0 else 0.0
            tone = share >= self.MIN_SHARE and band[i] * 2 / (self.window.sum() ** 2) >= self.MIN_POWER
            if tone:
                self.pitches.append(int(k))
            # Once a pitch is established, only that pitch counts: speech moves about, a Morse tone doesn't.
            settled = len(self.pitches) >= 5
            if settled:
                usual = int(np.median(self.pitches))
                tone = tone and abs(int(k) - usual) <= 1
                self.tone_hz = float(self.freqs[usual])
            keys.append(tone)
        return keys


class MorseReader:
    """One listener's Morse decoder, fed from CW mode's channel or from FM or AM audio."""

    def __init__(self, rate: float, from_audio: bool, bandwidth: float = 500.0):
        self.keyer = ToneKeyer(rate) if from_audio else CwKeyer(rate, bandwidth)
        bias = 0.0 if from_audio else CwKeyer.WIDENS_HOPS * HOP_SECONDS
        self.timing = MorseTiming(mark_bias=bias)

    def push(self, samples: np.ndarray) -> None:
        self.timing.feed(self.keyer.push(samples))

    def view(self) -> dict:
        tone = getattr(self.keyer, "tone_hz", None)
        return {"text": self.timing.text, "wpm": self.timing.wpm, "tone_hz": round(tone) if tone else None}


def keying(text: str, wpm: float, hop: float = HOP_SECONDS) -> list[bool]:
    """Text as key up and down per hop, at a speed in WPM. For tests."""
    unit = round(1.2 / wpm / hop)
    keys: list[bool] = []
    for w, word in enumerate(text.upper().split()):
        if w:
            keys += [False] * (7 * unit)
        for c, letter in enumerate(word):
            if c:
                keys += [False] * (3 * unit)
            for s, symbol in enumerate(TEXT_TO_MORSE[letter]):
                if s:
                    keys += [False] * unit
                keys += [True] * (unit if symbol == "." else 3 * unit)
    return keys + [False] * (10 * unit)


def tone_audio(text: str, wpm: float, rate: float, pitch: float = 800.0, amplitude: float = 0.3) -> np.ndarray:
    """Text as a keyed tone, as a repeater sends its ID. For tests."""
    per_hop = round(rate * HOP_SECONDS)
    envelope = np.repeat(np.array(keying(text, wpm), dtype=np.float32), per_hop)
    t = np.arange(len(envelope)) / rate
    return (amplitude * envelope * np.sin(2 * math.pi * pitch * t)).astype(np.float32)
