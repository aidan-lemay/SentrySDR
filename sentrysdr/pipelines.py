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

ANALOG = {"nfm", "am", "wfm"}
# Values are DSD-FME decode flags.
DIGITAL = {"p25p1": "-f1", "p25p2": "-f2", "dmr": "-fs", "nxdn48": "-fi", "nxdn96": "-fn", "dstar": "-fd", "ysf": "-fy"}
# DSD-FME writes one channel per time slot for these modes, and mono for the rest.
TWO_SLOT = {"p25p2", "dmr"}
MODULATIONS = ANALOG | set(DIGITAL) | {"adsb", "ais", "acars"}
# ACARS: one stick decodes several channels, so they must fit in what it can sample.
ACARS_MIN_HZ = 118_000_000
ACARS_MAX_HZ = 137_000_000
ACARS_MAX_SPAN_HZ = 2_000_000
ACARS_MAX_CHANNELS = 8
PCM_RATE = 16000
DIGITAL_INPUT_RATE = 48000
DSD_OUTPUT_RATE = 8000
FREQ_MIN_HZ = 500_000
FREQ_MAX_HZ = 2_200_000_000
# Noise peaks above its average and drifts as a stick warms up. In testing, quiet readings
# stayed under 1.3x within a run, but 1.6x still let one stick open by itself.
SQUELCH_MARGIN = 2.0
VOLUME_MIN_DB = -20.0
VOLUME_MAX_DB = 30.0


class SpecError(ValueError):
    pass


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
            "squelch": int(spec.get("squelch", 0)),
            "gain_db": None if spec.get("gain_db") is None else float(spec["gain_db"]),
            "ppm": int(spec.get("ppm", 0)),
            "volume_db": float(spec.get("volume_db") or 0),
            "channels": [int(hz) for hz in spec.get("channels") or []],
            "feeds": spec.get("feeds") or {},
            "feeder_uuid": feeds.valid_uuid(spec.get("feeder_uuid")),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise SpecError(f"bad assignment: {exc}") from exc
    if not VOLUME_MIN_DB <= clean["volume_db"] <= VOLUME_MAX_DB:
        raise SpecError("volume out of range")
    if clean["kind"] not in ("audio", "adsb", "ais", "acars"):
        raise SpecError("bad kind")
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
    if not 0 <= clean["squelch"] <= 100000:
        raise SpecError("squelch out of range")
    if not -500 <= clean["ppm"] <= 500:
        raise SpecError("ppm out of range")
    if clean["gain_db"] is not None and not 0 <= clean["gain_db"] <= 60:
        raise SpecError("gain out of range")
    return clean


def rtl_fm_args(cfg: Config, index: int, spec: dict) -> list[str]:
    modulation = spec["modulation"]
    args = [cfg.binary("rtl_fm") or "rtl_fm", "-d", str(index), "-f", str(spec["frequency_hz"])]
    if modulation == "nfm":
        args += ["-M", "fm", "-s", str(PCM_RATE), "-l", str(spec["squelch"]), "-E", "dc"]
    elif modulation == "am":
        args += ["-M", "am", "-s", str(PCM_RATE), "-l", str(spec["squelch"]), "-E", "dc"]
    elif modulation == "wfm":
        args += ["-M", "fm", "-s", "170k", "-A", "fast", "-r", str(PCM_RATE), "-l", str(spec["squelch"]), "-E", "deemp"]
    elif modulation in DIGITAL:
        # Decoders need the unfiltered discriminator signal at 48 kHz.
        args += ["-M", "fm", "-s", str(DIGITAL_INPUT_RATE), "-l", str(spec["squelch"])]
    else:
        raise SpecError(f"{modulation} is not an rtl_fm mode")
    if spec.get("gain_db") is not None:  # omitting -g selects tuner AGC
        args += ["-g", f"{spec['gain_db']:g}"]
    if spec.get("ppm"):
        args += ["-p", str(spec["ppm"])]
    return args + ["-"]


def measure_rate(modulation: str) -> int:
    """rtl_fm's sample rate for a mode. Its squelch scale differs between rates."""
    if modulation == "wfm":
        return 170_000
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


def suggest_squelch(levels: list[float]) -> int:
    """A level above the noise. Uses the quiet end so a brief call doesn't inflate it."""
    return math.ceil(levels[len(levels) // 10] * SQUELCH_MARGIN)


def measure_summary(levels: list[float]) -> dict:
    low, typical, high = levels[len(levels) // 10], levels[len(levels) // 2], levels[-1]
    return {
        "low": round(low), "typical": round(typical), "high": round(high),
        "suggested": suggest_squelch(levels),
        "moved": high > low * 2,  # something probably transmitted during the reading
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
