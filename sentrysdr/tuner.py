"""The web tuner's signal processing: a waterfall, and each listener's own receiver.

Raw 8-bit IQ from rtl_tcp comes in. Each block of it gets one FFT, and every listener
cuts its channel straight out of that FFT, already shifted and decimated
(overlap-save). Extra listeners on one tuner are therefore cheap: a small inverse FFT
each, not a filter over the whole span.

Nothing here does any IO, so it can be tested with made-up signals. numpy is needed;
it's optional for SentrySDR as a whole, so import this module only for a tuner.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# Sample rates an RTL-SDR can do, in Hz, that the tuner offers.
SAMPLE_RATES = (250_000, 1_024_000, 1_536_000, 1_800_000, 2_048_000, 2_400_000)
DEFAULT_RATE = 2_048_000
MODES = ("nfm", "am", "wfm", "wfm_stereo", "usb", "lsb", "cw")
# Each mode: default bandwidth, the range it can be set to, and the receiver's own rate.
MODE_SETTINGS = {
    "nfm": {"bw": 12_500, "min": 2_500, "max": 25_000, "if": 32_000},
    "am": {"bw": 10_000, "min": 2_000, "max": 20_000, "if": 24_000},
    "wfm": {"bw": 180_000, "min": 50_000, "max": 220_000, "if": 256_000},
    "wfm_stereo": {"bw": 180_000, "min": 120_000, "max": 220_000, "if": 256_000},
    "usb": {"bw": 2_700, "min": 500, "max": 5_000, "if": 12_000},
    "lsb": {"bw": 2_700, "min": 500, "max": 5_000, "if": 12_000},
    "cw": {"bw": 500, "min": 100, "max": 2_000, "if": 12_000},
}
CW_PITCH_HZ = 700
SSB_LOW_HZ = 300  # SSB audio starts here, so the carrier's hum is left out
WATERFALL_BINS = (512, 1024, 2048)
WATERFALL_FPS = 12
DB_FLOOR = -130.0  # waterfall values are sent as bytes, in half-dB steps up from here


def block_size(rate: int) -> int:
    """FFT size for a sample rate: about 32 ms, a power of two."""
    return 1 << max(10, round(math.log2(rate * 0.032)))


def iq_from_u8(raw: bytes) -> np.ndarray:
    """rtl_tcp's interleaved unsigned bytes as complex samples, about -1 to 1."""
    data = np.frombuffer(raw, dtype=np.uint8)
    data = data[: len(data) & ~1].astype(np.float32)
    data -= 127.4
    data *= 1 / 128
    return data.view(np.complex64)


def raised_cosine(freqs: np.ndarray, low: float, high: float, transition: float) -> np.ndarray:
    """1 between low and high, falling smoothly to 0 over `transition` Hz outside them."""
    response = np.zeros(len(freqs), dtype=np.float32)
    inside = (freqs >= low) & (freqs <= high)
    response[inside] = 1
    for edge, outward in ((low, -1), (high, 1)):
        distance = (freqs - edge) * outward
        ramp = (distance > 0) & (distance < transition)
        response[ramp] = 0.5 * (1 + np.cos(np.pi * distance[ramp] / transition))
    return response


class Decimator:
    """A filter that also lowers the rate, done in the frequency domain (overlap-save).

    `response(freqs)` gives the filter's complex gain at each frequency in Hz, so a
    de-emphasis curve or a one-sided passband is just a different function. Input and
    output are blocks of any length; state carries between calls.
    """

    def __init__(self, rate: float, out_rate: float, response, n: int | None = None, real_output: bool = False):
        self.rate = rate
        self.n = n or block_size(int(rate))
        self.m = max(8, int(round(self.n * out_rate / rate / 8)) * 8)  # a multiple of 8: see the trim below
        self.out_rate = rate * self.m / self.n
        self.overlap = self.n // 4
        self.hop = self.n - self.overlap
        # The filters are zero-phase, so each block's output is spoilt at both ends: an eighth is
        # trimmed off each. Every Decimator therefore delays by n/8 input samples.
        self.trim = self.m // 8
        self.delay = self.n // 8
        freqs = np.fft.fftshift(np.fft.fftfreq(self.m, 1 / self.out_rate))
        self.gain = np.fft.ifftshift(response(freqs).astype(np.complex64)) * np.float32(self.m / self.n)
        self.index = np.fft.ifftshift(np.arange(-self.m // 2, self.m // 2)) % self.n
        self.buffer = np.zeros(self.overlap, dtype=np.complex64)
        self.real_output = real_output

    def push(self, samples: np.ndarray) -> np.ndarray:
        self.buffer = np.concatenate((self.buffer, samples.astype(np.complex64, copy=False)))
        out = []
        while len(self.buffer) >= self.n:
            spectrum = np.fft.fft(self.buffer[: self.n])
            y = np.fft.ifft(spectrum[self.index] * self.gain)[self.trim:self.m - self.trim]
            out.append(y.real if self.real_output else y)
            self.buffer = self.buffer[self.hop:]
        if not out:
            return np.zeros(0, dtype=np.float32 if self.real_output else np.complex64)
        return np.concatenate(out).astype(np.float32 if self.real_output else np.complex64)


def deemphasis(tau: float):
    """The broadcast FM de-emphasis curve: 75 microseconds in the Americas, 50 elsewhere."""
    return lambda f: 1 / (1 + 2j * np.pi * f * tau)


def mulaw(audio: np.ndarray) -> bytes:
    """Audio from -1 to 1 as 8-bit mu-law (G.711), which halves what's sent."""
    x = np.clip(audio, -1, 1)
    magnitude = np.log1p(255 * np.abs(x)) / np.log1p(255)
    code = np.round(magnitude * 127).astype(np.int16)
    return np.where(x < 0, 127 - code, 255 - code).astype(np.uint8).tobytes()


def mulaw_decode(data: bytes) -> np.ndarray:
    """The browser's side of mulaw(), for tests."""
    code = np.frombuffer(data, dtype=np.uint8).astype(np.int16)
    negative = code < 128
    magnitude = np.where(negative, 127 - code, 255 - code) / 127
    x = (np.power(256, magnitude) - 1) / 255
    return np.where(negative, -x, x).astype(np.float32)


class Agc:
    """Holds audio near a steady level, changing gain once per block so there are no clicks.

    Quick to turn down for a loud signal, slow to turn back up afterwards.
    """

    def __init__(self, target: float = 0.3, max_gain: float = 1000.0):
        self.target, self.max_gain, self.gain = target, max_gain, 1.0

    def apply(self, audio: np.ndarray) -> np.ndarray:
        if not len(audio):
            return audio
        peak = float(np.max(np.abs(audio))) or 1e-9
        wanted = min(self.max_gain, self.target / peak)
        new = wanted if wanted < self.gain else self.gain + (wanted - self.gain) * 0.05
        ramp = np.linspace(self.gain, new, len(audio), dtype=np.float32)
        self.gain = new
        return audio * ramp


# --- RDS ------------------------------------------------------------------------
RDS_RATE = 1187.5
RDS_POLY = 0x5B9  # g(x) = x^10 + x^8 + x^7 + x^5 + x^4 + x^3 + 1
RDS_OFFSETS = {"A": 0x0FC, "B": 0x198, "C": 0x168, "C'": 0x350, "D": 0x1B4}
RDS_ORDER = ("A", "B", "C", "D")


def rds_checkword(data: int) -> int:
    """The 10-bit check for a 16-bit RDS block, before its offset word is added."""
    reg = data << 10
    for bit in range(25, 9, -1):
        if reg & (1 << bit):
            reg ^= RDS_POLY << (bit - 10)
    return reg & 0x3FF


def rds_syndrome(block: int) -> int:
    """What a received 26-bit block's check leaves over: the offset word if it arrived intact."""
    return rds_checkword(block >> 10) ^ (block & 0x3FF)


SYNDROMES = {offset: name for name, offset in RDS_OFFSETS.items()}


@dataclass
class RdsText:
    """What has been decoded from a station so far."""
    pi: int | None = None
    pty: int | None = None
    ps: list[str] = field(default_factory=lambda: [" "] * 8)
    rt: list[str] = field(default_factory=lambda: [" "] * 64)
    rt_ab: int | None = None
    groups: int = 0

    def view(self) -> dict:
        def clean(chars):
            return "".join(c if " " <= c <= "~" else " " for c in chars).strip()
        return {"pi": f"{self.pi:04X}" if self.pi is not None else "", "ps": clean(self.ps),
                "rt": clean(self.rt).split("\r")[0].strip(), "pty": self.pty, "groups": self.groups}


class RdsDecoder:
    """RDS from a stereo FM receiver's multiplex signal and its 19 kHz pilot.

    The 57 kHz subcarrier and its bit clock are both locked to the pilot, so the carrier
    comes from cubing the pilot rather than from a separate loop.
    """

    def __init__(self, rate: float):
        self.down = Decimator(rate, RDS_RATE * 16, lambda f: raised_cosine(f, -2400, 2400, 600), n=4096)
        self.samples = np.zeros(0, dtype=np.float32)
        self.bits: list[int] = []
        self.last_symbol = 0
        self.register = 0
        self.synced_at: int | None = None  # bit count when the last good block ended
        self.count = 0
        self.group: list[int | None] = [None] * 4
        self.text = RdsText()
        self.phase = 0.0

    def push(self, mpx: np.ndarray, carrier57: np.ndarray) -> None:
        baseband = self.down.push(mpx * np.conj(carrier57))
        if not len(baseband):
            return
        # The subcarrier's phase against the cubed pilot is fixed but unknown: squaring removes
        # the data's sign flips, leaving twice that phase to undo.
        angle = np.angle(np.mean(baseband.astype(np.complex128) ** 2)) / 2
        self.phase += 0.2 * ((angle - self.phase + np.pi / 2) % np.pi - np.pi / 2)
        real = (baseband * np.exp(-1j * self.phase)).real.astype(np.float32)
        self.samples = np.concatenate((self.samples, real))
        per_bit = self.down.out_rate / RDS_RATE
        bits = int(len(self.samples) // per_bit) - 1
        if bits <= 0:
            return
        # Biphase: each bit is a half-symbol one way, then the other. Pick the sampling point that
        # makes that difference largest over this stretch, then read one bit per symbol from there.
        half = per_bit / 2
        best, best_energy = 0.0, -1.0
        for offset in np.linspace(0, per_bit, 16, endpoint=False):
            starts = (offset + np.arange(bits) * per_bit).astype(int)
            first = self.samples[np.clip(starts + int(half / 2), 0, len(self.samples) - 1)]
            second = self.samples[np.clip(starts + int(half * 1.5), 0, len(self.samples) - 1)]
            energy = float(np.sum((first - second) ** 2))
            if energy > best_energy:
                best, best_energy = offset, energy
        starts = (best + np.arange(bits) * per_bit).astype(int)
        values = self.samples[starts + int(half / 2)] - self.samples[starts + int(half * 1.5)]
        for value in values:
            symbol = 1 if value > 0 else 0
            self._bit(symbol ^ self.last_symbol)  # differentially coded
            self.last_symbol = symbol
        self.samples = self.samples[int(starts[-1] + per_bit):]

    def _bit(self, bit: int) -> None:
        self.register = ((self.register << 1) | bit) & 0x3FFFFFF
        self.count += 1
        synced = self.synced_at is not None
        if synced and (self.count - self.synced_at) % 26:
            return  # in sync: only look where a block should end
        name = SYNDROMES.get(rds_syndrome(self.register))
        if name is None:
            if synced and self.count - self.synced_at >= 26 * 8:
                self.synced_at = None  # eight bad blocks in a row: search every bit again
            return
        position = 2 if name == "C'" else RDS_ORDER.index(name)
        if position == 0:
            self.group = [None] * 4
        self.group[position] = self.register >> 10
        self.synced_at = self.count
        if position == 3:
            self._group(self.group)
            self.group = [None] * 4

    def _group(self, blocks: list[int | None]) -> None:
        a, b, c, d = blocks
        if b is None:
            return
        self.text.groups += 1
        if a is not None:
            self.text.pi = a
        kind, version = b >> 12, (b >> 11) & 1
        self.text.pty = (b >> 5) & 0x1F
        if kind == 0 and d is not None:
            segment = b & 0x3
            self.text.ps[segment * 2:segment * 2 + 2] = [chr(d >> 8), chr(d & 0xFF)]
        elif kind == 2:
            ab = (b >> 4) & 1
            if self.text.rt_ab is not None and ab != self.text.rt_ab:
                self.text.rt = [" "] * 64  # the station started a new text
            self.text.rt_ab = ab
            segment = b & 0xF
            if version == 0 and c is not None and d is not None:
                self.text.rt[segment * 4:segment * 4 + 4] = [chr(c >> 8), chr(c & 0xFF), chr(d >> 8), chr(d & 0xFF)]
            elif version == 1 and d is not None:
                self.text.rt[segment * 2:segment * 2 + 2] = [chr(d >> 8), chr(d & 0xFF)]


# --- one listener -----------------------------------------------------------------------
@dataclass
class Settings:
    """What one listener chose. The frequency is absolute, in Hz."""
    frequency_hz: int
    mode: str = "nfm"
    bandwidth_hz: int | None = None
    squelch_db: float | None = None  # None: always open
    deemphasis_us: int = 75


class Receiver:
    """One listener's channel: cut out of the shared FFT, demodulated, as mu-law audio."""

    def __init__(self, rate: int, n: int, settings: Settings, center_hz: int):
        self.rate, self.n = rate, n
        self.overlap = n // 4
        self.hop = n - self.overlap
        self.settings = settings
        mode = MODE_SETTINGS[settings.mode]
        bw = settings.bandwidth_hz or mode["bw"]
        self.bw = min(mode["max"], max(mode["min"], bw))
        self.m = max(8, int(round(n * mode["if"] / rate / 8)) * 8)
        self.if_rate = rate * self.m / n
        self.trim = self.m // 8  # as in Decimator
        self.index_base = np.fft.ifftshift(np.arange(-self.m // 2, self.m // 2))
        self.retune(center_hz)
        self.block = 0
        self.mix_phase = 0.0
        self.bfo_phase = 0.0
        self.dc = 0.0
        self.last = np.complex64(1)
        self.agc = Agc()
        self.level_db = DB_FLOOR
        self.open = True
        self.stereo = False
        self.rds: RdsDecoder | None = None
        self.audio_rate = self.if_rate
        tau = settings.deemphasis_us * 1e-6
        if settings.mode == "nfm":
            self.audio = Decimator(self.if_rate, 16_000, lambda f: raised_cosine(f, -3500, 3500, 500), n=2048, real_output=True)
            self.audio_rate = self.audio.out_rate
        elif settings.mode in ("wfm", "wfm_stereo"):
            audio_shape = lambda f: raised_cosine(f, -15_000, 15_000, 1500) * deemphasis(tau)(f)  # noqa: E731
            self.audio = Decimator(self.if_rate, 32_000, audio_shape, n=8192, real_output=True)
            self.audio_rate = self.audio.out_rate
            if settings.mode == "wfm_stereo":
                self.diff = Decimator(self.if_rate, 32_000, audio_shape, n=8192, real_output=True)
                self.pilot = Decimator(self.if_rate, self.if_rate, lambda f: raised_cosine(f, 18_800, 19_200, 300), n=8192)
                # The pilot comes out of its filter n/8 samples late: hold the multiplex back as much.
                self.delay = np.zeros(self.pilot.delay, dtype=np.float32)
                self.rds = RdsDecoder(self.if_rate)

    def retune(self, center_hz: int) -> None:
        """The tuner's centre moved: find this channel in the new span."""
        self.center_hz = center_hz
        offset = self.settings.frequency_hz - center_hz
        self.inside = abs(offset) + self.bw / 2 < self.rate / 2
        self.k0 = int(round(offset * self.n / self.rate))
        self.fine_hz = offset - self.k0 * self.rate / self.n
        self.index = (self.index_base + self.k0) % self.n
        freqs = np.fft.ifftshift(np.fft.fftshift(np.fft.fftfreq(self.m, 1 / self.if_rate)))
        mode = self.settings.mode
        if mode == "usb":
            low, high = SSB_LOW_HZ, self.bw
        elif mode == "lsb":
            low, high = -self.bw, -SSB_LOW_HZ
        else:
            low, high = -self.bw / 2, self.bw / 2
        low, high = low + self.fine_hz, high + self.fine_hz  # the cut is in whole bins; the passband isn't
        transition = max(50.0, min(2_000.0, self.bw * 0.1))
        self.passband = raised_cosine(freqs, low, high, transition)
        self.gain = (self.passband * np.float32(self.m / self.n)).astype(np.complex64)

    def process(self, spectrum: np.ndarray) -> np.ndarray:
        """One block of the shared FFT in; this listener's audio out, -1 to 1."""
        self.block += 1
        if not self.inside:  # outside the tuner's span: silence, keeping every filter's timing
            self.level_db = DB_FLOOR
            return self._demodulate(np.zeros(self.m - 2 * self.trim, dtype=np.complex64)) * 0
        cut = spectrum[self.index]
        power = float(np.sum(np.abs(cut) ** 2 * self.passband)) / (self.n * self.n)
        self.level_db = 10 * math.log10(power + 1e-20)
        y = np.fft.ifft(cut * self.gain)[self.trim:self.m - self.trim]
        # Taking whole bins is a frequency shift relative to each block's start: undo its jump between blocks.
        y *= np.exp(-2j * np.pi * ((self.k0 * self.hop * self.block) % self.n) / self.n)
        if self.fine_hz:
            steps = np.arange(len(y)) * (-2 * np.pi * self.fine_hz / self.if_rate) + self.mix_phase
            y *= np.exp(1j * steps)
            self.mix_phase = float((steps[-1] - 2 * np.pi * self.fine_hz / self.if_rate) % (2 * np.pi))
        audio = self._demodulate(y.astype(np.complex64))
        squelch = self.settings.squelch_db
        self.open = squelch is None or self.level_db >= squelch
        return audio if self.open else np.zeros_like(audio)

    def _demodulate(self, y: np.ndarray) -> np.ndarray:
        mode = self.settings.mode
        if mode in ("nfm", "wfm", "wfm_stereo"):
            joined = np.concatenate(([self.last], y))
            self.last = y[-1]
            phase = np.angle(joined[1:] * np.conj(joined[:-1])).astype(np.float32)
            deviation = 5_000 if mode == "nfm" else 75_000
            mpx = phase * np.float32(self.if_rate / (2 * np.pi * deviation))
            if mode != "wfm_stereo":
                return self.audio.push(mpx) * (0.7 if mode == "nfm" else 1.0)
            return self._stereo(mpx)
        if mode == "am":
            envelope = np.abs(y).astype(np.float32)
            self.dc = 0.9 * self.dc + 0.1 * float(np.mean(envelope))  # the carrier, taken out slowly
            return self.agc.apply(envelope - np.float32(self.dc))
        if mode == "cw":
            step = 2 * np.pi * CW_PITCH_HZ / self.if_rate
            steps = np.arange(len(y)) * step + self.bfo_phase
            self.bfo_phase = float((steps[-1] + step) % (2 * np.pi))
            return self.agc.apply((y * np.exp(1j * steps)).real.astype(np.float32))
        return self.agc.apply(y.real.astype(np.float32))  # usb, lsb: the passband chose the side

    def _stereo(self, mpx: np.ndarray) -> np.ndarray:
        pilot = self.pilot.push(mpx)
        self.delay = np.concatenate((self.delay, mpx))
        usable = min(len(pilot), len(self.delay))
        section = self.delay[:usable]  # the multiplex, lined up with the pilot
        self.delay = self.delay[usable:]
        p = pilot[:usable]
        mono = self.audio.push(section)
        if not usable:
            self.stereo = False
            return np.repeat(mono, 2)
        unit = p / np.maximum(np.abs(p), 1e-9)
        # The pilot is sin(t) and the stereo subcarrier sin(2t): from the pilot's analytic form
        # e^j(t - pi/2), squaring gives -e^j2t, whose imaginary part is -sin(2t).
        carrier38 = -(unit * unit).imag.astype(np.float32)
        diff = self.diff.push(section * 2 * carrier38)
        if self.rds is not None:
            self.rds.push(section, unit * unit * unit)
        self.stereo = float(np.mean(np.abs(p))) > 0.02
        n = min(len(mono), len(diff))
        if not self.stereo:
            diff = np.zeros(n, dtype=np.float32)
        stereo = np.empty(2 * n, dtype=np.float32)
        stereo[0::2], stereo[1::2] = mono[:n] + diff[:n], mono[:n] - diff[:n]
        return stereo


class Spectrum:
    """Waterfall rows: averaged power across the span, in dB."""

    def __init__(self, bins: int = 1024):
        self.set_bins(bins)

    def set_bins(self, bins: int) -> None:
        self.bins = bins if bins in WATERFALL_BINS else 1024
        self.size = self.bins * 2
        self.window = np.hanning(self.size).astype(np.float32)
        self.scale = 1 / float(np.sum(self.window)) ** 2

    def row(self, samples: np.ndarray, averages: int = 4) -> bytes:
        """The latest samples as one row of bytes, half a dB each, DB_FLOOR at 0."""
        need = self.size * averages
        if len(samples) < self.size:
            return bytes(self.bins)
        samples = samples[-need:]
        count = len(samples) // self.size
        blocks = samples[: count * self.size].reshape(count, self.size) * self.window
        power = np.mean(np.abs(np.fft.fftshift(np.fft.fft(blocks, axis=1), axes=1)) ** 2, axis=0) * self.scale
        power = power.reshape(self.bins, 2).mean(axis=1)
        db = 10 * np.log10(power + 1e-20)
        return np.clip((db - DB_FLOOR) * 2, 0, 255).astype(np.uint8).tobytes()


class Tuner:
    """Everything one tuner does with its samples, for any number of listeners.

    feed() takes IQ bytes and returns what to send: waterfall rows for everyone, and
    each listener's audio. Thread-safe use is the caller's job.
    """

    def __init__(self, rate: int = DEFAULT_RATE, center_hz: int = 100_000_000):
        self.receivers: dict[int, Receiver] = {}
        self.spectrum = Spectrum()
        self.center_hz = center_hz
        self.set_rate(rate)

    def set_rate(self, rate: int) -> None:
        self.rate = rate
        self.n = block_size(rate)
        self.overlap = self.n // 4
        self.hop = self.n - self.overlap
        self.buffer = np.zeros(self.overlap, dtype=np.complex64)
        self.dc = np.complex64(0)
        self.since_row = 0.0
        self.recent = np.zeros(0, dtype=np.complex64)
        for listener, receiver in list(self.receivers.items()):
            self.receivers[listener] = Receiver(rate, self.n, receiver.settings, self.center_hz)

    def set_center(self, center_hz: int) -> None:
        self.center_hz = center_hz
        self.buffer[:] = 0
        for receiver in self.receivers.values():
            receiver.retune(center_hz)

    def listen(self, listener: int, settings: Settings) -> None:
        self.receivers[listener] = Receiver(self.rate, self.n, settings, self.center_hz)

    def leave(self, listener: int) -> None:
        self.receivers.pop(listener, None)

    def feed(self, raw: bytes) -> tuple[list[bytes], dict[int, list[np.ndarray]]]:
        samples = iq_from_u8(raw)
        if not len(samples):
            return [], {}
        # The stick's DC offset makes a spike at the centre: take out a slowly tracked average.
        mean = samples.mean()
        self.dc = self.dc * np.complex64(0.9) + mean * np.complex64(0.1)
        samples = samples - self.dc
        rows: list[bytes] = []
        self.recent = np.concatenate((self.recent, samples))[-self.spectrum.size * 4:]
        self.since_row += len(samples) / self.rate
        if self.since_row >= 1 / WATERFALL_FPS:
            self.since_row = min(self.since_row - 1 / WATERFALL_FPS, 1 / WATERFALL_FPS)  # carry over, never bunch up
            rows.append(self.spectrum.row(self.recent))
        audio: dict[int, list[np.ndarray]] = {listener: [] for listener in self.receivers}
        if not self.receivers:
            return rows, audio
        self.buffer = np.concatenate((self.buffer, samples))
        while len(self.buffer) >= self.n:
            spectrum = np.fft.fft(self.buffer[: self.n])
            for listener, receiver in self.receivers.items():
                audio[listener].append(receiver.process(spectrum))
            self.buffer = self.buffer[self.hop:]
        return rows, audio
