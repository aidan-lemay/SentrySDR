"""What an SDR stick can do: its tuner, gain steps, frequency range and uses.

Everything here only produces warnings. A stick can still be given any target.
"""
from __future__ import annotations

import re

from .pipelines import ANALOG, DIGITAL

# Usual tuning range of each tuner with librtlsdr, in Hz. Edges vary a little by stick.
TUNER_RANGES = {
    "R820T": (24_000_000, 1_766_000_000),
    "R820T2": (24_000_000, 1_766_000_000),
    "R828D": (24_000_000, 1_766_000_000),
    "FC0012": (22_000_000, 948_600_000),
    "FC0013": (22_000_000, 1_100_000_000),
    "FC2580": (146_000_000, 924_000_000),
    "E4000": (52_000_000, 2_200_000_000),
}

# Uses SentrySDR records, so a target of one of these kinds can be checked.
RECORDED_USES = {"voice": "Analog voice", "digital": "Digital voice", "adsb": "ADS-B", "ais": "AIS", "acars": "ACARS"}
# Labels for other work the stick is set up for. Nothing checks these.
OTHER_USES = {"broadcast_fm": "Broadcast FM"}
USES = {**RECORDED_USES, **OTHER_USES}
# Offered once, then removed. Saved values are dropped when the database opens.
RETIRED_USES = {"goes", "noaa_apt", "meteor", "pager", "trunk_cc"}

MAX_GAIN_STEPS = 64
GAIN_MATCH_DB = 0.05


def short_tuner(found: str) -> str:
    """'Rafael Micro R820T' -> 'R820T'."""
    return found.split()[-1] if found.split() else ""


def parse_rtl_test(text: str) -> dict:
    """Read the tuner and gain steps from rtl_test's output."""
    tuner = re.search(r"Found (.+?) tuner", text)
    gains = re.search(r"Supported gain values \(\d+\):([-\d. ]+)", text)
    steps = []
    if gains:
        for value in gains.group(1).split():
            try:
                steps.append(round(float(value), 1))
            except ValueError:
                continue
    name = short_tuner(tuner.group(1)) if tuner else ""
    low, high = TUNER_RANGES.get(name, (None, None))
    return {"tuner": name, "gain_steps": sorted(set(steps))[:MAX_GAIN_STEPS], "freq_min_hz": low, "freq_max_hz": high}


def clean_detected(result: dict) -> dict:
    """Check a detection result. From a slave it arrives over the network."""
    tuner = str(result.get("tuner") or "")[:40]
    steps = []
    for value in list(result.get("gain_steps") or [])[:MAX_GAIN_STEPS]:
        value = float(value)
        if -20 <= value <= 70:
            steps.append(round(value, 1))
    def hz(value):
        return int(value) if value is not None and 0 <= int(value) <= 10_000_000_000 else None
    return {"tuner": tuner, "gain_steps": sorted(set(steps)),
            "freq_min_hz": hz(result.get("freq_min_hz")), "freq_max_hz": hz(result.get("freq_max_hz"))}


def nearest_step(steps: list[float] | None, gain: float | None) -> float | None:
    """The step the stick will really use, when gain isn't one of them. None when it is, or can't tell."""
    if not steps or gain is None:
        return None
    nearest = min(steps, key=lambda step: abs(step - gain))
    return None if abs(nearest - gain) <= GAIN_MATCH_DB else nearest


def use_for(kind: str, modulation: str) -> set[str]:
    """Which ticked uses would cover a target. Empty means nothing to check."""
    if kind == "rtltcp":
        return set()  # raw samples: any stick can serve them
    if kind in ("adsb", "ais", "acars"):
        return {kind}
    if modulation in DIGITAL:
        return {"digital"}
    if modulation == "wfm":
        return {"voice", "broadcast_fm"}
    if modulation in ANALOG:
        return {"voice"}
    return set()


def problems(source, frequency_hz: int, kind: str, modulation: str, gain_db: float | None) -> list[str]:
    """Why this stick may not suit a target. Unset capabilities aren't judged."""
    found = []
    low, high = source.cap_min_hz, source.cap_max_hz
    if (low is not None and frequency_hz < low) or (high is not None and frequency_hz > high):
        found.append("is outside the stick's frequency range")
    uses = set(source.uses or [])
    needed = use_for(kind, modulation)
    if uses and needed and not uses & needed:
        label = RECORDED_USES.get(sorted(needed & set(RECORDED_USES))[0], kind)
        found.append(f"needs {label}, which the stick isn't set up for")
    nearest = nearest_step(source.gain_steps, gain_db)
    if nearest is not None and kind == "audio":
        found.append(f"uses {gain_db:g} dB gain, which isn't one of the stick's steps, so it will get {nearest:g} dB")
    return found
