"""Build argument lists for the external radio tools.

Commands are always argv lists, never shell strings. ``sanitize_spec`` validates
every value first, because on a slave the spec arrives over the network.
"""
from __future__ import annotations

import math
import sys
from array import array
from dataclasses import dataclass

from . import feeds
from .config import Config

ANALOG = {"nfm", "fm", "am", "wfm"}  # nfm: 12.5 kHz channels; fm: 25 kHz (5 kHz deviation)
# Values are DSD-FME decode flags.
DIGITAL = {"p25p1": "-f1", "p25p2": "-f2", "dmr": "-fs", "nxdn48": "-fi", "nxdn96": "-fn", "dstar": "-fd", "ysf": "-fy"}
# DSD-FME writes one channel per time slot for these modes, and mono for the rest.
TWO_SLOT = {"p25p2", "dmr"}
MODULATIONS = ANALOG | set(DIGITAL) | {"adsb", "ais", "acars", "rtltcp", "tuner", "multi"}
# Multi-channel: one SDR records several voice targets inside its span, with the web tuner's DSP.
MULTI_MODES = {"nfm": 12_500, "fm": 16_000, "am": 10_000}  # each mode's channel width, Hz
MULTI_MAX_CHANNELS = 16  # the most any SDR can be set to, under Admin > SDR sources
MULTI_DEFAULT_CHANNELS = 8  # an SDR's limit until an administrator changes it: check its machine first
MULTI_RATES = (250_000, 1_024_000, 1_536_000, 1_800_000, 2_048_000, 2_400_000)  # the lowest that fits is used
MULTI_USABLE = 0.84  # of the span: the stick's response falls off toward the edges
MULTI_CENTER_GUARD_HZ = 10_000  # kept clear of the centre spike, beyond each channel's own width
RTL_TCP_MIN_PORT = 1024
# ACARS: one stick decodes several channels, so they must fit in what it can sample.
ACARS_MIN_HZ = 118_000_000
ACARS_MAX_HZ = 137_000_000
ACARS_MAX_SPAN_HZ = 2_000_000
ACARS_MAX_CHANNELS = 8
PCM_RATE = 16000
# High-pass filters that take out a CTCSS tone (67.0 to 254.1 Hz), as a radio does before its
# speaker. "low" and "medium" are biquad stages (Q 0.707) at a cutoff, the same as ffmpeg's
# highpass=poles=2: low leaves some of a loud tone, medium removes most tones but leaves about
# 23 dB of the highest. "high" is as steep as a radio's: see _steep_highpass.
TONE_FILTERS = {"low": (250.0, 3), "medium": (300.0, 5), "high": None}  # (cutoff Hz, stages)
TONE_STOP_HZ, TONE_PASS_HZ = 254.5, 300.0  # high: everything up to the top tone, 254.1 Hz, is cut
TONE_STOP_DB, TONE_PASS_DB = 50.0, 1.0
TONE_FILTER_MODES = {"nfm", "fm"}  # FM channels are what carry CTCSS
# Wideband FM needs more than PCM_RATE to fit 5 kHz deviation; rtl_fm resamples it to PCM_RATE.
FM_WIDE_RATE = 24000
DIGITAL_INPUT_RATE = 48000
DSD_OUTPUT_RATE = 8000
FREQ_MIN_HZ = 500_000
FREQ_MAX_HZ = 2_200_000_000
# Noise peaks above its average and drifts as a stick warms up. In testing, quiet readings
# stayed under 1.3x within a run, but 1.6x still let one stick open by itself: 2x is 6 dB.
SQUELCH_MARGIN_DB = 6.0
# Squelch is in dB: a channel's power against full scale, as the web tuner's meter and squelch
# show it. None is off, recording everything. rtl_fm gets it converted to its own scale.
SQUELCH_DB_MIN = -130.0
SQUELCH_DB_MAX = 0.0
RTL_FM_MINIMUM_RATE = 1_000_000  # rtl_fm's MINIMUM_RATE: it reads the stick at least this fast
VOLUME_MIN_DB = -20.0
VOLUME_MAX_DB = 30.0


class SpecError(ValueError):
    pass


# --- multi-channel ---------------------------------------------------------------------
def multi_plan(channels: list[tuple[int, str]]) -> dict | None:
    """Where to tune for these (frequency, mode) channels: {"center_hz", "rate"}, or None if they don't fit.

    The lowest rate that fits, to keep CPU down. Every channel must sit inside the usable part of
    the span and clear of the centre spike.
    """
    if not channels or len(channels) > MULTI_MAX_CHANNELS:
        return None
    if any(mode not in MULTI_MODES for _, mode in channels):
        return None
    spread = [(hz, MULTI_MODES[mode] / 2) for hz, mode in channels]
    low, high = min(hz - half for hz, half in spread), max(hz + half for hz, half in spread)
    edges = sorted({e for hz, half in spread for e in (hz - half, hz + half)})
    guard = MULTI_CENTER_GUARD_HZ
    # Candidates: the middle, each gap between channels, and just outside either end.
    candidates = [(low + high) / 2] + [(a + b) / 2 for a, b in zip(edges, edges[1:])]
    candidates += [low - guard - 1_000, high + guard + 1_000]
    candidates.sort(key=lambda c: abs(c - (low + high) / 2))
    for rate in MULTI_RATES:
        reach = MULTI_USABLE * rate / 2
        for center in candidates:
            center = round(center / 100) * 100
            if all(abs(hz - center) + half <= reach and abs(hz - center) - half >= guard for hz, half in spread):
                return {"center_hz": int(center), "rate": rate}
    return None


def multi_problem(channels: list[tuple[int, str]], limit: int = MULTI_DEFAULT_CHANNELS) -> str | None:
    """Why these channels can't share one SDR, or None. `limit` is that SDR's maximum."""
    if len(channels) > limit:
        return (f"This SDR is set to record at most {limit} channels. An administrator can change that under "
                f"Admin > SDR sources, where Check this machine shows how many it can handle.")
    if any(mode not in MULTI_MODES for _, mode in channels):
        return "Multi-channel records NFM, FM and AM targets only."
    if multi_plan(channels) is None:
        span = (max(hz for hz, _ in channels) - min(hz for hz, _ in channels)) / 1e6
        reach = MULTI_USABLE * MULTI_RATES[-1] / 1e6
        return (f"Those channels span {span:.3f} MHz, but one SDR covers about {reach:.1f} MHz. "
                "Use another SDR for the farthest one.")
    return None


def clean_tone_filter(value, modulation: str) -> str | None:
    """A tone filter that applies to this mode, or None. Refuses one that isn't a filter."""
    if value in (None, "", "off"):
        return None
    if value not in TONE_FILTERS:
        raise SpecError("bad tone filter")
    return value if modulation in TONE_FILTER_MODES else None


Biquad = tuple[float, float, float, float, float]  # (b0, b1, b2, a1, a2), with a0 = 1


def highpass_stage(cutoff_hz: float, rate: float) -> Biquad:
    """One biquad high-pass, Q 0.707. From the Audio EQ Cookbook, as ffmpeg's."""
    w = 2 * math.pi * cutoff_hz / rate
    alpha = math.sin(w) / (2 * math.sqrt(0.5))
    cos = math.cos(w)
    a0 = 1 + alpha
    return ((1 + cos) / 2 / a0, -(1 + cos) / a0, (1 + cos) / 2 / a0, -2 * cos / a0, (1 - alpha) / a0)


def _steep_highpass(rate: float) -> list[Biquad]:
    """A Chebyshev type II high-pass: at least TONE_STOP_DB down up to TONE_STOP_HZ, and within
    TONE_PASS_DB from TONE_PASS_HZ up, with no ripple there. Closed-form poles and zeros, so no
    filter-design library is needed: the low-pass prototype, turned high-pass, then bilinear.
    """
    ratio = math.sqrt((10 ** (TONE_STOP_DB / 10) - 1) / (10 ** (TONE_PASS_DB / 10) - 1))
    order = math.ceil(math.acosh(ratio) / math.acosh(TONE_PASS_HZ / TONE_STOP_HZ))
    order += order % 2  # whole biquads
    mu = math.asinh(math.sqrt(10 ** (TONE_STOP_DB / 10) - 1)) / order
    stop = 2 * rate * math.tan(math.pi * TONE_STOP_HZ / rate)  # pre-warped, so the edge lands where it should

    def bilinear(s: complex) -> complex:
        return (1 + s / (2 * rate)) / (1 - s / (2 * rate))

    stages = []
    for k in range(1, order // 2 + 1):
        theta = math.pi * (2 * k - 1) / (2 * order)
        pole = bilinear(stop * complex(-math.sinh(mu) * math.sin(theta), math.cosh(mu) * math.cos(theta)))
        zero = bilinear(stop * -1j * math.cos(theta))  # on the unit circle: a notch in the stopband
        b, a = (1.0, -2 * zero.real, abs(zero) ** 2), (1.0, -2 * pole.real, abs(pole) ** 2)
        gain = (a[0] - a[1] + a[2]) / (b[0] - b[1] + b[2])  # each stage passes high frequencies at unity
        stages.append((b[0] * gain, b[1] * gain, b[2] * gain, a[1], a[2]))
    return stages


_STAGES: dict[tuple[str, float], list[Biquad]] = {}


def tone_filter_stages(mode: str, rate: float) -> list[Biquad]:
    """The biquads for a tone filter at a sample rate."""
    key = (mode, float(rate))
    if key not in _STAGES:
        if mode == "high":
            _STAGES[key] = _steep_highpass(rate)
        else:
            cutoff, count = TONE_FILTERS[mode]
            _STAGES[key] = [highpass_stage(cutoff, rate)] * count
    return _STAGES[key]


def tone_filter_gain(mode: str, freq_hz: float, rate: float = PCM_RATE) -> float:
    """How much of a frequency a tone filter passes, 0 to 1."""
    z = complex(math.cos(2 * math.pi * freq_hz / rate), -math.sin(2 * math.pi * freq_hz / rate))  # z^-1
    total = 1.0
    for b0, b1, b2, a1, a2 in tone_filter_stages(mode, rate):
        total *= abs((b0 + b1 * z + b2 * z * z) / (1 + a1 * z + a2 * z * z))
    return total


def _clean_channel(raw: dict) -> dict:
    channel = {
        "recorder_id": int(raw["recorder_id"]), "recorder_name": str(raw.get("recorder_name", ""))[:120],
        "target_id": int(raw["target_id"]), "target_name": str(raw.get("target_name", ""))[:120],
        "frequency_hz": int(raw["frequency_hz"]), "modulation": str(raw["modulation"]),
        "squelch": None if raw.get("squelch") is None else round(float(raw["squelch"]), 1),
        "volume_db": float(raw.get("volume_db") or 0),
    }
    channel["tone_filter"] = clean_tone_filter(raw.get("tone_filter"), channel["modulation"])
    if channel["modulation"] not in MULTI_MODES:
        raise SpecError("bad channel modulation")
    if channel["squelch"] is not None and not SQUELCH_DB_MIN <= channel["squelch"] <= SQUELCH_DB_MAX:
        raise SpecError("channel squelch out of range")
    if not VOLUME_MIN_DB <= channel["volume_db"] <= VOLUME_MAX_DB:
        raise SpecError("channel volume out of range")
    return channel


def sanitize_spec(spec: dict) -> dict:
    """Validate an assignment. Raises SpecError on anything unexpected."""
    try:
        clean = {
            "recorder_id": int(spec["recorder_id"]),
            "recorder_name": str(spec.get("recorder_name", ""))[:120],
            "source_id": int(spec["source_id"]),
            "source_name": str(spec.get("source_name", ""))[:120],
            "target_id": int(spec["target_id"]),
            "target_name": str(spec.get("target_name", ""))[:120],
            "kind": str(spec.get("kind", "audio")),
            "frequency_hz": int(spec["frequency_hz"]),
            "modulation": str(spec["modulation"]),
            "squelch": None if spec.get("squelch") is None else round(float(spec["squelch"]), 1),
            "gain_db": None if spec.get("gain_db") is None else float(spec["gain_db"]),
            "ppm": int(spec.get("ppm", 0)),
            "volume_db": float(spec.get("volume_db") or 0),
            "channels": [int(hz) for hz in spec.get("channels") or []],
            "feeds": spec.get("feeds") or {},
            "feeder_uuid": feeds.valid_uuid(spec.get("feeder_uuid")),
            "rtl_tcp_port": int(spec["rtl_tcp_port"]) if spec.get("rtl_tcp_port") is not None else None,
            "multi": [_clean_channel(c) for c in spec.get("multi") or []],
            "rate": int(spec["rate"]) if spec.get("rate") is not None else None,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise SpecError(f"bad assignment: {exc}") from exc
    if not VOLUME_MIN_DB <= clean["volume_db"] <= VOLUME_MAX_DB:
        raise SpecError("volume out of range")
    clean["tone_filter"] = clean_tone_filter(spec.get("tone_filter"), clean["modulation"])
    if clean["kind"] not in ("audio", "adsb", "ais", "acars", "rtltcp", "tuner", "multi"):
        raise SpecError("bad kind")
    if clean["kind"] == "multi":
        # frequency_hz is the SDR's centre; each channel must be inside the rate it reads at.
        if clean["rate"] not in MULTI_RATES or not 1 <= len(clean["multi"]) <= MULTI_MAX_CHANNELS:
            raise SpecError("bad multi-channel assignment")
        if len({c["target_id"] for c in clean["multi"]}) != len(clean["multi"]):
            raise SpecError("a channel appears twice")
        for channel in clean["multi"]:
            if abs(channel["frequency_hz"] - clean["frequency_hz"]) + MULTI_MODES[channel["modulation"]] / 2 > clean["rate"] / 2:
                raise SpecError("a channel is outside the span")
    elif clean["multi"] or clean["rate"] is not None:
        raise SpecError("only multi-channel uses channels and a rate")
    if clean["kind"] == "rtltcp":
        if clean["rtl_tcp_port"] is None or not RTL_TCP_MIN_PORT <= clean["rtl_tcp_port"] <= 65535:
            raise SpecError("rtl_tcp needs a port from 1024 to 65535")
    else:
        clean["rtl_tcp_port"] = None
    if clean["kind"] == "acars":
        problem = acars_channel_problem(clean["channels"])
        if problem:
            raise SpecError(problem)
    elif clean["channels"]:
        raise SpecError("only ACARS uses channels")
    try:
        clean["feeds"] = feeds.clean(clean["kind"], clean["feeds"])
    except feeds.FeedError as exc:
        raise SpecError(str(exc)) from exc
    if clean["modulation"] not in MODULATIONS:
        raise SpecError("bad modulation")
    if not FREQ_MIN_HZ <= clean["frequency_hz"] <= FREQ_MAX_HZ:
        raise SpecError("frequency out of range")
    if clean["squelch"] is not None and not SQUELCH_DB_MIN <= clean["squelch"] <= SQUELCH_DB_MAX:
        raise SpecError("squelch out of range")
    if not -500 <= clean["ppm"] <= 500:
        raise SpecError("ppm out of range")
    if clean["gain_db"] is not None and not 0 <= clean["gain_db"] <= 60:
        raise SpecError("gain out of range")
    return clean


# --- squelch: dB, and rtl_fm's own scale ---------------------------------------------------
# rtl_fm reads the stick at `rate * d` (d = MINIMUM_RATE // rate + 1), takes 127 off each byte,
# and sums every d samples into one, so a steady signal comes out d times larger. Its squelch
# (-l) is the RMS of those I and Q values. The tuner's dB is the channel's mean |I + jQ|^2, with
# each byte scaled to +-1 (divided by 128). So a level L is 20 log10(L * sqrt(2) / (128 d)) dB.
def rtl_fm_decimation(rate: int) -> int:
    return RTL_FM_MINIMUM_RATE // rate + 1


def level_to_db(level: float, rate: int) -> float:
    """rtl_fm's squelch scale, at this rtl_fm sample rate, as dB."""
    if level <= 0:
        return SQUELCH_DB_MIN
    db = 20 * math.log10(level * math.sqrt(2) / (128 * rtl_fm_decimation(rate)))
    return max(SQUELCH_DB_MIN, round(db, 1))


def db_to_level(db: float, rate: int) -> int:
    """dB as rtl_fm's squelch scale. Never 0, which would turn its squelch off."""
    return max(1, round(128 * rtl_fm_decimation(rate) * 10 ** (db / 20) / math.sqrt(2)))


def rtl_fm_squelch(spec: dict) -> str:
    if spec["squelch"] is None:
        return "0"  # off
    return str(db_to_level(spec["squelch"], measure_rate(spec["modulation"])))


def rtl_fm_args(cfg: Config, index: int, spec: dict) -> list[str]:
    modulation = spec["modulation"]
    squelch = rtl_fm_squelch(spec)
    args = [cfg.binary("rtl_fm") or "rtl_fm", "-d", str(index), "-f", str(spec["frequency_hz"])]
    if modulation == "nfm":
        args += ["-M", "fm", "-s", str(PCM_RATE), "-l", squelch, "-E", "dc"]
    elif modulation == "fm":
        args += ["-M", "fm", "-s", str(FM_WIDE_RATE), "-r", str(PCM_RATE), "-l", squelch, "-E", "dc"]
    elif modulation == "am":
        args += ["-M", "am", "-s", str(PCM_RATE), "-l", squelch, "-E", "dc"]
    elif modulation == "wfm":
        args += ["-M", "fm", "-s", "170k", "-A", "fast", "-r", str(PCM_RATE), "-l", squelch, "-E", "deemp"]
    elif modulation in DIGITAL:
        # Decoders need the unfiltered discriminator signal at 48 kHz.
        args += ["-M", "fm", "-s", str(DIGITAL_INPUT_RATE), "-l", squelch]
    else:
        raise SpecError(f"{modulation} is not an rtl_fm mode")
    if spec.get("gain_db") is not None:  # omitting -g selects tuner AGC
        args += ["-g", f"{spec['gain_db']:g}"]
    if spec.get("ppm"):
        args += ["-p", str(spec["ppm"])]
    return args + ["-"]


def measure_rate(modulation: str) -> int:
    """rtl_fm's sample rate for a mode, which its squelch scale depends on. Matches rtl_fm_args."""
    if modulation == "wfm":
        return 170_000
    if modulation == "fm":
        return FM_WIDE_RATE
    return DIGITAL_INPUT_RATE if modulation in DIGITAL else PCM_RATE


def measure_args(cfg: Config, index: int, frequency_hz: int, modulation: str, gain_db: float | None, ppm: int = 0) -> list[str]:
    # Raw mode outputs the same filtered samples that rtl_fm's squelch compares to -l.
    args = [
        cfg.binary("rtl_fm") or "rtl_fm", "-d", str(index), "-f", str(frequency_hz),
        "-M", "raw", "-s", str(measure_rate(modulation)), "-l", "0",
    ]
    if gain_db is not None:
        args += ["-g", f"{gain_db:g}"]
    if ppm:
        args += ["-p", str(ppm)]
    return args + ["-"]


def signal_levels(pcm: bytes, rate: int, settle_seconds: float = 1.0) -> list[float]:
    """Sorted RMS of each 100 ms of interleaved I/Q, on the scale of rtl_fm's squelch."""
    samples = array("h")
    samples.frombytes(pcm[:len(pcm) & ~1])
    if sys.byteorder == "big":
        samples.byteswap()
    block = 2 * rate // 10
    levels = []
    for start in range(int(2 * rate * settle_seconds), len(samples) - block + 1, block):
        window = samples[start:start + block]
        mean = sum(window) / block
        levels.append(math.sqrt(sum((value - mean) ** 2 for value in window) / block))
    return sorted(levels)


def suggest_squelch(levels: list[float], rate: int) -> float:
    """A squelch in whole dB above the noise. Uses the quiet end so a brief call doesn't inflate it."""
    return float(min(SQUELCH_DB_MAX, math.ceil(level_to_db(levels[len(levels) // 10], rate) + SQUELCH_MARGIN_DB)))


def measure_summary(levels: list[float], rate: int) -> dict:
    """The noise in dB, on the same scale as the squelch and the web tuner's meter."""
    low, typical, high = levels[len(levels) // 10], levels[len(levels) // 2], levels[-1]
    return {
        "low": level_to_db(low, rate), "typical": level_to_db(typical, rate), "high": level_to_db(high, rate),
        "suggested": suggest_squelch(levels, rate),
        "moved": high > low * 2,  # something probably transmitted during the reading
    }


def db_summary(levels_db: list[float]) -> dict:
    """measure_summary's answer from channel levels already in dB, as a multi-channel SDR measures them."""
    levels = sorted(levels_db)
    low, typical, high = levels[len(levels) // 10], levels[len(levels) // 2], levels[-1]
    return {
        "low": round(low, 1), "typical": round(typical, 1), "high": round(high, 1),
        "suggested": float(min(SQUELCH_DB_MAX, math.ceil(low + SQUELCH_MARGIN_DB))),
        "moved": high > low + 6,  # something probably transmitted during the reading
    }


def sanitize_measure(request: dict) -> dict:
    """Validate a measure request. On a slave it arrives over the network."""
    try:
        clean = {
            "frequency_hz": int(request["frequency_hz"]),
            "modulation": str(request["modulation"]),
            "gain_db": None if request.get("gain_db") is None else float(request["gain_db"]),
            "ppm": int(request.get("ppm", 0)),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise SpecError(f"bad measure request: {exc}") from exc
    if clean["modulation"] not in ANALOG | set(DIGITAL):
        raise SpecError("bad modulation")
    if not FREQ_MIN_HZ <= clean["frequency_hz"] <= FREQ_MAX_HZ:
        raise SpecError("frequency out of range")
    if not -500 <= clean["ppm"] <= 500:
        raise SpecError("ppm out of range")
    if clean["gain_db"] is not None and not 0 <= clean["gain_db"] <= 60:
        raise SpecError("gain out of range")
    return clean


def dsd_args(cfg: Config, spec: dict) -> list[str]:
    return [cfg.binary("dsd-fme") or "dsd-fme", "-i", "-", "-o", "-", DIGITAL[spec["modulation"]]]


def pcm_rate(spec: dict) -> int:
    return DSD_OUTPUT_RATE if spec["modulation"] in DIGITAL else PCM_RATE


def pcm_channels(spec: dict) -> int:
    return 2 if spec["modulation"] in TWO_SLOT else 1


def acars_channel_problem(channels: list[int]) -> str | None:
    """Why a set of ACARS channels can't run on one stick, or None."""
    if not channels:
        return "An ACARS target needs at least one channel"
    if len(channels) > ACARS_MAX_CHANNELS:
        return f"An ACARS target can have at most {ACARS_MAX_CHANNELS} channels"
    if any(not ACARS_MIN_HZ <= hz <= ACARS_MAX_HZ for hz in channels):
        return "ACARS channels must be between 118 and 137 MHz"
    span = max(channels) - min(channels)
    if span > ACARS_MAX_SPAN_HZ:
        return (f"Those channels span {span / 1e6:.3f} MHz, but one stick covers at most "
                f"{ACARS_MAX_SPAN_HZ / 1e6:g} MHz. Remove the outermost channel, or use a second target on another stick.")
    return None


def acars_args(cfg: Config, index: int, spec: dict) -> list[str]:
    # JSON to stdout only: no UDP, MQTT or other network outputs from acarsdec itself.
    args = [cfg.binary("acarsdec") or "acarsdec", "--output", "json:file:path=-", "--rtlsdr", str(index)]
    if spec.get("gain_db") is not None:
        args += ["-g", f"{spec['gain_db']:g}"]
    if spec.get("ppm"):
        args += ["-p", str(spec["ppm"])]
    return args + [f"{hz / 1e6:.3f}" for hz in sorted(spec["channels"])]


def rtltcp_args(cfg: Config, index: int, spec: dict) -> list[str]:
    """rtl_tcp on this machine's configured address. That comes from the local config, never from the master."""
    args = [cfg.binary("rtl_tcp") or "rtl_tcp", "-a", cfg.rtl_tcp_bind, "-p", str(spec["rtl_tcp_port"]),
            "-d", str(index), "-f", str(spec["frequency_hz"])]
    if spec.get("gain_db"):  # rtl_tcp treats 0 as automatic, which is also what no -g means
        args += ["-g", f"{spec['gain_db']:g}"]
    if spec.get("ppm"):
        args += ["-P", str(spec["ppm"])]
    return args


def rtl_sdr_args(cfg: Config, index: int, spec: dict) -> list[str]:
    """Raw 8-bit IQ on stdout, for multi-channel. Blocks of about 50 ms keep live audio prompt."""
    block = max(16384, round(spec["rate"] * 2 * 0.05 / 16384) * 16384)
    args = [cfg.binary("rtl_sdr") or "rtl_sdr", "-d", str(index), "-f", str(spec["frequency_hz"]),
            "-s", str(spec["rate"]), "-b", str(block)]
    if spec.get("gain_db") is not None:
        args += ["-g", f"{spec['gain_db']:g}"]
    if spec.get("ppm"):
        args += ["-p", str(spec["ppm"])]
    return args + ["-"]


def tuner_args(cfg: Config, index: int, spec: dict, port: int, rate: int, center_hz: int) -> list[str]:
    """rtl_tcp for the web tuner: always loopback and a private port, with SentrySDR as its only client."""
    args = [cfg.binary("rtl_tcp") or "rtl_tcp", "-a", "127.0.0.1", "-p", str(port), "-d", str(index),
            "-f", str(center_hz), "-s", str(rate)]
    if spec.get("ppm"):
        args += ["-P", str(spec["ppm"])]
    return args


def readsb_args(cfg: Config, index: int, spec: dict, json_dir: str) -> list[str]:
    # Networking only when feeding is on, and then outbound plus loopback only: see feeds.readsb_args.
    args = [
        cfg.binary("readsb") or "readsb", "--device-type", "rtlsdr", "--device", str(index),
        "--write-json", json_dir, "--write-json-every", "1", "--quiet",
    ]
    if spec.get("gain_db") is not None:
        args += ["--gain", f"{spec['gain_db']:g}"]
    if spec.get("ppm"):
        args += ["--ppm", str(spec["ppm"])]
    return args + feeds.readsb_args(spec.get("feeds") or {}, spec.get("feeder_uuid"))


def ais_args(cfg: Config, index: int, spec: dict) -> list[str]:
    # -o 5 prints one JSON object per message; no -N, so no web server or port.
    args = [cfg.binary("AIS-catcher") or "AIS-catcher", f"-d:{index}", "-o", "5"]
    if spec.get("ppm"):
        args += ["-p", str(spec["ppm"])]
    if spec.get("gain_db") is not None:
        args += ["-gr", "TUNER", f"{spec['gain_db']:g}"]
    return args + feeds.ais_args(spec.get("feeds") or {})


def encode_args(cfg: Config, raw_path: str, rate: int, out_path: str) -> list[str]:
    return [
        cfg.binary("ffmpeg") or "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", raw_path,
        "-codec:a", "libmp3lame", "-b:a", "32k", out_path,
    ]


def live_encoder_args(cfg: Config, rate: int) -> list[str]:
    return [
        cfg.binary("ffmpeg") or "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
        "-codec:a", "libmp3lame", "-b:a", "32k", "-flush_packets", "1", "-f", "mp3", "pipe:1",
    ]


def broadcastify_audio_args(cfg: Config, in_path: str, out_path: str) -> list[str]:
    # The same encoding trunk-recorder sends to Broadcastify.
    return [
        cfg.binary("ffmpeg") or "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", in_path, "-vn", "-c:a", "aac", "-ar", "16000", "-ac", "1", "-b:a", "32k",
        "-movflags", "+faststart", out_path,
    ]


def m4a_args(cfg: Config, in_path: str, out_path: str) -> list[str]:
    # ClearCut's reference client (trunk-recorder) sends AAC in an m4a container.
    return [
        cfg.binary("ffmpeg") or "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", in_path, "-codec:a", "aac", "-b:a", "32k", out_path,
    ]
