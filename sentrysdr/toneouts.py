"""Tone-outs: the two-tone sequential pages (Motorola Quick Call II and the like) and long single
tones that alert fire stations.

The node finds any page in a call's audio and reports its measured tones; it needs no table.
The master names them from its Tone-outs table (match). Only targets with tone-out decoding on
build a Detector, so a target with it off costs nothing.

Every 50 ms the detector estimates the audio's pitch from its zero crossings and checks the
frame really is one pure tone, with a narrow detector (Goertzel) at that pitch. Page tones are
steady sine waves that drown out everything else on the channel, so this is enough, and it is
cheap: a few operations a sample, in plain Python.
"""
from __future__ import annotations

import math
import sys
from array import array
from dataclasses import dataclass

from . import pipelines

FRAME_SECONDS = 0.05
LOW_HZ, HIGH_HZ = 250.0, 3200.0  # QC2's tones run from about 288 to 2900 Hz
PURITY = 0.6  # the share of a frame's power in the one tone
STEADY = 0.012  # frames within 1.2% of the run's pitch belong to it
GAP_FRAMES = 3  # a run survives this many odd frames (150 ms): fading, a click
A_MIN, A_MAX = 0.6, 2.5  # QC2: A for about 1 s, then B for about 3 s
B_MIN = 1.8
LONG_MIN = 4.5  # one long tone on its own: a group or all-call page
BETWEEN_MAX = 0.4  # the gap allowed between A and B
MATCH = 0.015  # a heard tone matches a table tone within 1.5%, as most decoders
MAX_PAGES = 10


@dataclass
class Run:
    start: float  # seconds into the call
    hz: float
    frames: int
    misses: int = 0

    @property
    def seconds(self) -> float:
        return self.frames * FRAME_SECONDS


class Detector:
    """Fed a call's 16-bit audio as it arrives; pages() lists what it heard."""

    def __init__(self, rate: int):
        self.rate = rate
        self.frame = int(FRAME_SECONDS * rate)
        self.buffer: list[int] = []
        self.frames = 0
        self.run: Run | None = None
        self.runs: list[Run] = []  # finished runs long enough to be part of a page
        # The Low tone filter's stages (250 Hz) take out a CTCSS tone, which would bend the zero
        # crossings, while costing the lowest page tones (about 288 Hz) only about 6 dB.
        self.stages = pipelines.tone_filter_stages("low", rate)
        self.state = [[0.0, 0.0, 0.0, 0.0] for _ in self.stages]

    def feed(self, chunk: bytes) -> None:
        samples = array("h")
        samples.frombytes(chunk[: len(chunk) & ~1])
        if sys.byteorder == "big":
            samples.byteswap()
        values: list = samples.tolist()
        for (b0, b1, b2, a1, a2), state in zip(self.stages, self.state):
            x1, x2, y1, y2 = state
            for index, x in enumerate(values):
                y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
                x2, x1, y2, y1 = x1, x, y1, y
                values[index] = y
            state[:] = (x1, x2, y1, y2)
        out = self.buffer
        out.extend(values)
        while len(out) >= self.frame:
            self._frame(out[: self.frame])
            del out[: self.frame]

    def _frame(self, values: list[float]) -> None:
        at = self.frames * FRAME_SECONDS
        self.frames += 1
        hz = _pure_tone(values, self.rate)
        run = self.run
        if run is not None and hz is not None and abs(hz - run.hz) <= STEADY * run.hz:
            run.hz += (hz - run.hz) / (run.frames + 1)  # the run's pitch: the average of its frames
            run.frames += 1
            run.misses = 0
            return
        if run is not None:
            run.misses += 1
            if run.misses <= GAP_FRAMES and hz is None:
                return
            self._end_run()
        if hz is not None:
            self.run = Run(at, hz, 1)

    def _end_run(self) -> None:
        if self.run is not None and self.run.seconds >= A_MIN:
            self.runs.append(self.run)
        self.run = None

    def pages(self) -> list[dict]:
        """[{"a": Hz, "b": Hz or None, "at": seconds into the call}], in order."""
        self._end_run()
        found, runs, i = [], self.runs, 0
        while i < len(runs) and len(found) < MAX_PAGES:
            first = runs[i]
            following = runs[i + 1] if i + 1 < len(runs) else None
            gap = following.start - (first.start + first.seconds) if following else None
            if (following is not None and A_MIN <= first.seconds <= A_MAX and following.seconds >= B_MIN
                    and gap is not None and gap <= BETWEEN_MAX and abs(following.hz - first.hz) > STEADY * first.hz):
                found.append({"a": round(first.hz, 1), "b": round(following.hz, 1), "at": round(first.start, 2)})
                i += 2
                continue
            if first.seconds >= LONG_MIN:
                found.append({"a": round(first.hz, 1), "b": None, "at": round(first.start, 2)})
            i += 1
        return found


HYSTERESIS = 0.7  # of the frame's RMS: a rise counts from below -h to above +h, so noise can't add any


def _pure_tone(values: list[float], rate: int) -> float | None:
    """The frame's pitch, if it is one pure tone in the page band; otherwise None."""
    power = sum(v * v for v in values)
    if power <= len(values) * 1e4:  # quieter than about -50 dBFS: nothing to decode
        return None
    h = HYSTERESIS * math.sqrt(power / len(values))
    rises, first, last, low = 0, None, None, False
    previous = values[0]
    for index in range(1, len(values)):
        value = values[index]
        if value < -h:
            low = True
        elif low and value > h:
            # Where it went over +h, between the two samples: the same point in every cycle.
            where = index - 1 + (h - previous) / (value - previous)
            if first is None:
                first = where
            last = where
            rises += 1
            low = False
        previous = value
    if rises < 3:
        return None
    hz = (rises - 1) / ((last - first) / rate)
    if not LOW_HZ <= hz <= HIGH_HZ:
        return None
    # Goertzel at that pitch: how much of the frame's power is that one tone.
    coeff = 2 * math.cos(2 * math.pi * hz / rate)
    s1 = s2 = 0.0
    for value in values:
        s1, s2 = value + coeff * s1 - s2, s1
    tone = (s1 * s1 + s2 * s2 - coeff * s1 * s2) * 2 / len(values)
    return hz if tone >= PURITY * power else None


def clean_pages(raw) -> list[dict]:
    """Pages as a slave reports them: anything malformed is dropped."""
    pages = []
    for page in (raw if isinstance(raw, list) else [])[:MAX_PAGES]:
        try:
            a = float(page["a"])
            b = None if page.get("b") is None else float(page["b"])
            at = float(page.get("at") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if LOW_HZ <= a <= HIGH_HZ and (b is None or LOW_HZ <= b <= HIGH_HZ) and 0 <= at <= 86400:
            pages.append({"a": round(a, 1), "b": None if b is None else round(b, 1), "at": round(at, 2)})
    return pages


def close(heard: float, listed: float) -> bool:
    return abs(heard - listed) <= MATCH * listed


def match(pages: list[dict], table) -> list[dict]:
    """Name each page from the table: rows with id, name, a_hz and b_hz (None for a long tone)."""
    named = []
    for page in pages:
        best = None
        for row in table:
            if (row.b_hz is None) != (page["b"] is None):
                continue
            if close(page["a"], row.a_hz) and (row.b_hz is None or close(page["b"], row.b_hz)):
                error = abs(page["a"] - row.a_hz) / row.a_hz + (abs(page["b"] - row.b_hz) / row.b_hz if row.b_hz else 0)
                if best is None or error < best[0]:
                    best = (error, row)
        named.append({**page, "id": best[1].id if best else None, "name": best[1].name if best else None})
    return named


def describe(page: dict) -> str:
    tones = f"{page['a']:g}" + (f" / {page['b']:g}" if page.get("b") else "") + " Hz"
    return f"{page['name']} ({tones})" if page.get("name") else f"Unknown tone-out ({tones})"
