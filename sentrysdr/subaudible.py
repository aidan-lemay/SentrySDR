"""Which CTCSS tone or DCS code a call carries, for tone squelch and for tagging calls.

The sub-audible band (below 300 Hz) is low-passed and kept at a low rate while a call is on
air, up to COLLECT_SECONDS (the whole call for tone squelch). When the call ends, identify()
names the tone, and kept_spans() finds where a tone squelch's tone is. Plain
Python, so it runs anywhere; it costs nothing on a target with tone squelch off, and nothing
after the first few seconds of a call.

CTCSS: one narrow detector (Goertzel) per standard tone over the whole stretch. Three seconds
separates tones 2.4 Hz apart, the closest in the table, with room to spare.

DCS: 134.4 bit/s, a 23-bit Golay codeword sent over and over. The word is the code's 9 bits
(three octal digits), the fixed bits 100, then 11 check bits; least significant bit first.
Every 23 bits in a row are some rotation of the word, so each rotation is looked up. The
inverse of one code's word is another code's (023 inverted is 047): which is "N" and which "I"
depends on the receiver's polarity, which is why tags give the polarity too.
"""
from __future__ import annotations

import cmath
import math
import re
from array import array
import sys

CTCSS_TONES = (
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4, 100.0, 103.5, 107.2, 110.9,
    114.8, 118.8, 123.0, 127.3, 131.8, 136.5, 141.3, 146.2, 150.0, 151.4, 156.7, 159.8, 162.2, 165.5,
    167.9, 171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5, 206.5, 210.7,
    218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
)
DCS_CODES = (
    "023", "025", "026", "031", "032", "036", "043", "047", "051", "053", "054", "065", "071", "072", "073",
    "074", "114", "115", "116", "122", "125", "131", "132", "134", "143", "145", "152", "155", "156", "162",
    "165", "172", "174", "205", "212", "223", "225", "226", "243", "244", "245", "246", "251", "252", "255",
    "261", "263", "265", "266", "271", "274", "306", "311", "315", "325", "331", "332", "343", "346", "351",
    "356", "364", "365", "371", "411", "412", "413", "423", "431", "432", "445", "446", "452", "454", "455",
    "462", "464", "465", "466", "503", "506", "516", "523", "526", "532", "546", "565", "606", "612", "624",
    "627", "631", "632", "654", "662", "664", "703", "712", "723", "731", "732", "734", "743", "754",
)
DCS_BAUD = 134.4
GOLAY_POLY = 0xC75  # x^11 + x^10 + x^6 + x^5 + x^4 + x^2 + 1

INPUT_RATE = 16_000
DECIMATE = 10
RATE = INPUT_RATE // DECIMATE  # 1600 Hz: the band of interest is under 300 Hz
LOWPASS_HZ = 300.0
COLLECT_SECONDS = 3.0
MIN_SECONDS = 0.4  # less than this can't tell neighbouring tones apart: no answer
# A tone counts when it holds at least this share of the band's power, and beats the next
# strongest tone (not counting its own neighbours) by this much.
CTCSS_MIN_SHARE = 0.15
CTCSS_MIN_LEAD = 4.0
DCS_MIN_WORDS = 3  # the same word seen this many times, about half a second

RULE_RE = re.compile(r"^(detect|ctcss:\d{2,3}\.\d|dcs:\d{3}[NI]?)$")


def clean_rule(value) -> str | None:
    """A target's tone squelch: None (off), "detect", "ctcss:179.9" or "dcs:023N". Raises ValueError."""
    if value in (None, "", "off"):
        return None
    if not isinstance(value, str) or not RULE_RE.match(value):
        raise ValueError("bad tone squelch")
    kind, _, code = value.partition(":")
    if kind == "ctcss" and float(code) not in CTCSS_TONES:
        raise ValueError("not a standard CTCSS tone")
    if kind == "dcs" and code[:3] not in DCS_CODES:
        raise ValueError("not a standard DCS code")
    return value


def label(found: tuple[str, str] | None) -> str | None:
    """How a call's tone is shown: "179.9 Hz" or "DCS 023N"."""
    if found is None:
        return None
    kind, code = found
    return f"{code} Hz" if kind == "ctcss" else f"DCS {code}"


def matches(rule: str | None, found: tuple[str, str] | None) -> bool:
    """Whether a call with what was found passes a tone squelch rule."""
    if rule in (None, "detect"):
        return True
    if found is None:
        return False
    kind, _, code = rule.partition(":")
    if kind != found[0]:
        return False
    if kind == "ctcss":
        return float(code) == float(found[1])
    if len(code) == 4:  # a polarity was given: exactly that
        return found[1] == code
    # No polarity: this code either way up. Inverted, a code reads as its partner (023 as 047).
    return found[1][:3] in (code, dcs_partner(code))


# --- collecting while the call is on air ---------------------------------------------------
def _lowpass(cutoff: float, rate: float) -> tuple[float, float, float, float, float]:
    w = 2 * math.pi * cutoff / rate
    alpha = math.sin(w) / (2 * math.sqrt(0.5))
    cos = math.cos(w)
    a0 = 1 + alpha
    return ((1 - cos) / 2 / a0, (1 - cos) / a0, (1 - cos) / 2 / a0, -2 * cos / a0, (1 - alpha) / a0)


class Collector:
    """Keeps a call's sub-audible band at RATE: four poles at 300 Hz, then every tenth sample.

    Takes 16 kHz 16-bit audio (feed) or samples already at a low rate (extend). Stops working
    once it has COLLECT_SECONDS.
    """

    def __init__(self, rate: float = RATE, seconds: float = COLLECT_SECONDS):
        self.rate = rate
        self.samples: list[float] = []
        self.limit = int(seconds * rate)
        self.stages = [_lowpass(LOWPASS_HZ, INPUT_RATE)] * 2
        self.state = [[0.0, 0.0, 0.0, 0.0] for _ in self.stages]
        self.phase = 0

    @property
    def full(self) -> bool:
        return len(self.samples) >= self.limit

    def feed(self, chunk: bytes) -> None:
        if self.full:
            return
        pcm = array("h")
        pcm.frombytes(chunk)
        if sys.byteorder == "big":
            pcm.byteswap()
        values: list = pcm.tolist()
        for (b0, b1, b2, a1, a2), state in zip(self.stages, self.state):
            x1, x2, y1, y2 = state
            for index, x in enumerate(values):
                y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
                x2, x1, y2, y1 = x1, x, y1, y
                values[index] = y
            state[:] = (x1, x2, y1, y2)
        start = (-self.phase) % DECIMATE
        kept = values[start::DECIMATE]
        self.phase = (self.phase + len(values)) % DECIMATE
        self.samples.extend(v / 32768 for v in kept[: self.limit - len(self.samples)])

    def extend(self, samples) -> None:
        if not self.full:
            self.samples.extend(float(v) for v in list(samples)[: self.limit - len(self.samples)])

    def identify(self) -> tuple[str, str] | None:
        return identify(self.samples, self.rate)


# --- deciding when it ends -------------------------------------------------------------
def identify(samples: list[float], rate: float) -> tuple[str, str] | None:
    """("ctcss", "179.9"), ("dcs", "023N"), or None when neither is clearly there."""
    if len(samples) < MIN_SECONDS * rate:
        return None
    mean = sum(samples) / len(samples)
    centred = [s - mean for s in samples]
    return _ctcss(centred, rate) or _dcs(centred, rate)


def _ctcss(samples: list[float], rate: float) -> tuple[str, str] | None:
    n = len(samples)
    window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]
    shaped = [s * w for s, w in zip(samples, window)]
    total = sum(s * s for s in samples) / n
    if total <= 1e-12:
        return None
    scale = sum(window)
    powers = []
    for tone in CTCSS_TONES:
        coeff = 2 * math.cos(2 * math.pi * tone / rate)
        s1 = s2 = 0.0
        for x in shaped:
            s1, s2 = x + coeff * s1 - s2, s1
        amplitude = 2 * abs(s1 - cmath.exp(-2j * math.pi * tone / rate) * s2) / scale
        powers.append(amplitude * amplitude / 2)
    best = max(range(len(powers)), key=powers.__getitem__)
    rivals = [p for i, p in enumerate(powers) if abs(CTCSS_TONES[i] - CTCSS_TONES[best]) > 1.5]
    if powers[best] < CTCSS_MIN_SHARE * total or powers[best] < CTCSS_MIN_LEAD * max(rivals, default=0.0):
        return None
    return "ctcss", f"{CTCSS_TONES[best]:.1f}"


def _golay_word(code: str) -> int:
    data = 0x800 | int(code, 8)
    remainder = data << 11
    for bit in range(22, 10, -1):
        if remainder & (1 << bit):
            remainder ^= GOLAY_POLY << (bit - 11)
    return data | (remainder << 12)


def _rotations(word: int) -> list[int]:
    return [((word >> k) | (word << (23 - k))) & 0x7FFFFF for k in range(23)]


def _dcs_table() -> dict[int, str]:
    table: dict[int, str] = {}
    for polarity in ("N", "I"):  # normal first: an inverted word that is also some code's normal word reads as that one
        for code in DCS_CODES:
            word = _golay_word(code)
            if polarity == "I":
                word ^= 0x7FFFFF
            for rotation in _rotations(word):
                table.setdefault(rotation, code + polarity)
    return table


DCS_TABLE = _dcs_table()


def dcs_partner(code: str) -> str | None:
    """The code another code reads as when inverted: 023 and 047 are each other's."""
    found = DCS_TABLE.get(_golay_word(code) ^ 0x7FFFFF)
    return found[:3] if found else None


def _dcs(samples: list[float], rate: float) -> tuple[str, str] | None:
    counts = _dcs_counts(samples, rate)
    if not counts:
        return None
    count, code = max((n, c) for c, n in counts.items())
    return ("dcs", code) if count >= DCS_MIN_WORDS else None


def _dcs_counts(samples: list[float], rate: float) -> dict[str, int]:
    """How many whole codewords of each code, at the bit timing that finds the most."""
    period = rate / DCS_BAUD
    best: dict[str, int] = {}
    for step in range(8):  # where a bit starts is unknown: try eight places in one
        offset = period * (step + 0.5) / 8
        bits = []
        position = offset + period / 2
        while position < len(samples):
            bits.append(1 if samples[int(position)] > 0 else 0)
            position += period
        if len(bits) < 23:
            continue
        counts: dict[str, int] = {}
        word = 0
        for index, bit in enumerate(bits):
            word = ((word >> 1) | (bit << 22)) & 0x7FFFFF  # least significant bit first
            if index >= 22 and index % 23 == 22:  # whole words only, so one stretch isn't counted 23 times
                code = DCS_TABLE.get(word)
                if code:
                    counts[code] = counts.get(code, 0) + 1
        if sum(counts.values()) > sum(best.values()):
            best = counts
    return best


# --- tone squelch, as a scanner does it -------------------------------------------------
# A scanner opens only while the right tone is there: its audio closes when the tone stops,
# cutting off a repeater's tail, and stays shut for anyone else on the channel. Here the whole
# call's band is kept, and when it ends each STEP_SECONDS is checked with a WINDOW_SECONDS
# window around it. Stretches with the tone are kept (gaps under BRIDGE_SECONDS bridged); the
# rest of the recording is cut.
STEP_SECONDS = 0.25
WINDOW_SECONDS = 1.0  # long enough to tell neighbouring tones 2.4 Hz apart
EDGE_TRIM = 0.25  # see kept_spans
BRIDGE_SECONDS = 0.75  # a fade this short doesn't split a call
WINDOW_MIN_SHARE = 0.1  # the target tone's share of the band in a window
WINDOW_MIN_LEAD = 4.0  # over each neighbouring tone in the table
WINDOW_DCS_WORDS = 2  # of the 5 or so a window holds
MAX_COLLECT_SECONDS = 600.0


def _power(shaped: list[float], hz: float, rate: float, scale: float) -> float:
    coeff = 2 * math.cos(2 * math.pi * hz / rate)
    s1 = s2 = 0.0
    for x in shaped:
        s1, s2 = x + coeff * s1 - s2, s1
    amplitude = 2 * abs(s1 - cmath.exp(-2j * math.pi * hz / rate) * s2) / scale
    return amplitude * amplitude / 2


def _neighbours(tone: float) -> list[float]:
    index = CTCSS_TONES.index(tone)
    return [CTCSS_TONES[i] for i in (index - 1, index + 1) if 0 <= i < len(CTCSS_TONES)]


def kept_spans(samples: list[float], rate: float, rule: str) -> tuple[list[tuple[float, float]], tuple[str, str] | None]:
    """Where in a call the rule's tone or code is, as [(start s, end s)], and what was heard there.

    Only for a CTCSS or DCS rule. Empty when the tone never shows: the call is dropped.
    """
    kind, _, code = rule.partition(":")
    step, width = int(STEP_SECONDS * rate), int(WINDOW_SECONDS * rate)
    if len(samples) < width // 2 or step < 1:
        return [], None
    window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (width - 1)) for i in range(width)]
    tone = float(code) if kind == "ctcss" else 0.0
    neighbours = _neighbours(tone) if kind == "ctcss" else []
    present: list[bool] = []
    heard: dict[str, int] = {}
    for centre in range(step // 2, len(samples), step):
        start = max(0, min(centre - width // 2, len(samples) - width))
        chunk = samples[start:start + width]
        mean = sum(chunk) / len(chunk)
        chunk = [x - mean for x in chunk]
        if kind == "ctcss":
            shape = window[: len(chunk)]
            shaped = [x * w for x, w in zip(chunk, shape)]
            scale = sum(shape)
            total = sum(x * x for x in chunk) / len(chunk)
            target = _power(shaped, tone, rate, scale)
            rivals = [_power(shaped, n, rate, scale) for n in neighbours]
            present.append(total > 1e-12 and target >= WINDOW_MIN_SHARE * total
                           and target >= WINDOW_MIN_LEAD * max(rivals, default=0.0))
        else:
            counts = _dcs_counts(chunk, rate)
            wanted = {c: n for c, n in counts.items() if matches(rule, ("dcs", c))}
            present.append(sum(wanted.values()) >= WINDOW_DCS_WORDS)
            for c, n in wanted.items():
                heard[c] = heard.get(c, 0) + n
    spans: list[list[float]] = []
    for index, on in enumerate(present):
        if not on:
            continue
        begin, end = index * STEP_SECONDS, (index + 1) * STEP_SECONDS
        if spans and begin - spans[-1][1] <= BRIDGE_SECONDS:
            spans[-1][1] = end
        else:
            spans.append([begin, end])
    length = len(samples) / rate
    # A window still sees the tone when centred up to about a step outside it, so an edge inside
    # the call comes out a step wide. One at the call's own start or end is left as it is.
    spans = [(s + EDGE_TRIM if s > 0 else s, min(e, length) - (EDGE_TRIM if e < length else 0)) for s, e in spans]
    spans = [(s, e) for s, e in spans if e > s]
    if not spans:
        return [], None
    found = ("ctcss", f"{tone:.1f}") if kind == "ctcss" else ("dcs", max(heard, key=heard.get)) if heard else None
    return spans, found
