"""Web tuner bookmarks in and out of other programs' formats: SentrySDR's own JSON, GQRX and SDR++.

Each format keeps less than SentrySDR does, so a round trip through GQRX or SDR++ can lose
notes, or turn a mode into its nearest equivalent. SentrySDR's JSON keeps everything.
Parsing never trusts the file: anything malformed is skipped, and the caller limits sizes.
"""
from __future__ import annotations

import csv
import io
import json

from .pipelines import FREQ_MAX_HZ, FREQ_MIN_HZ

MODES = ("nfm", "am", "wfm", "wfm_stereo", "usb", "lsb", "cw")
FORMATS = ("sentrysdr", "gqrx", "sdrpp")
MAX_BOOKMARKS = 2000
NAME_LENGTH, FOLDER_LENGTH, NOTES_LENGTH = 120, 60, 500

# GQRX's names for its modes. Ones SentrySDR doesn't have come in as the nearest it does.
GQRX_OUT = {"nfm": "Narrow FM", "am": "AM", "wfm": "WFM (mono)", "wfm_stereo": "WFM (stereo)",
            "usb": "USB", "lsb": "LSB", "cw": "CW-U"}
GQRX_IN = {"narrow fm": "nfm", "am": "am", "am-sync": "am", "wfm (mono)": "wfm", "wfm (stereo)": "wfm_stereo",
           "wfm (oirt)": "wfm", "usb": "usb", "lsb": "lsb", "cw-u": "cw", "cw-l": "cw", "cw": "cw"}
GQRX_COLOUR = "#c0c0c0"
# SDR++'s radio module numbers its modes.
SDRPP_OUT = {"nfm": 0, "wfm": 1, "wfm_stereo": 1, "am": 2, "usb": 4, "cw": 5, "lsb": 6}
SDRPP_IN = {0: "nfm", 1: "wfm", 2: "am", 3: "am", 4: "usb", 5: "cw", 6: "lsb"}


def clean(item: dict) -> dict | None:
    """One bookmark with only known fields, trimmed, or None if it can't be used."""
    try:
        frequency = int(round(float(item["frequency_hz"])))
        name = str(item.get("name") or "").strip()[:NAME_LENGTH]
        mode = str(item.get("mode") or "nfm")
        bandwidth = item.get("bandwidth_hz")
        bandwidth = None if bandwidth in (None, "", 0) else int(round(float(bandwidth)))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not FREQ_MIN_HZ <= frequency <= FREQ_MAX_HZ or mode not in MODES:
        return None
    if bandwidth is not None and not 0 < bandwidth <= 250_000:
        bandwidth = None
    return {"name": name or f"{frequency / 1e6:.4f} MHz", "frequency_hz": frequency, "mode": mode, "bandwidth_hz": bandwidth,
            "folder": str(item.get("folder") or "").strip()[:FOLDER_LENGTH],
            "notes": str(item.get("notes") or "").strip()[:NOTES_LENGTH]}


# --- out ----------------------------------------------------------------------------
def export(items: list[dict], fmt: str) -> tuple[str, str, str]:
    """(text, filename, media type) for a list of bookmarks."""
    if fmt == "gqrx":
        tags = sorted({b["folder"] or "Untagged" for b in items} | {"Untagged"})
        lines = ["# Tag name          ;  color"] + [f"{_csv_safe(tag):<20}; {GQRX_COLOUR}" for tag in tags]
        lines += ["", "# Frequency ; Name                     ; Modulation          ;  Bandwidth; Tags"]
        for b in items:
            lines.append(f"{b['frequency_hz']:>14}; {_csv_safe(b['name']):<25}; {GQRX_OUT[b['mode']]:<20}; "
                         f"{b['bandwidth_hz'] or 0:>10}; {_csv_safe(b['folder'] or 'Untagged')}")
        return "\n".join(lines) + "\n", "bookmarks.csv", "text/csv"
    if fmt == "sdrpp":
        lists: dict[str, dict] = {}
        for b in items:
            entries = lists.setdefault(b["folder"] or "General", {"bookmarks": {}, "showOnWaterfall": True})["bookmarks"]
            name, n = b["name"], 2
            while name in entries:  # SDR++ keys bookmarks by name
                name, n = f"{b['name']} ({n})", n + 1
            entries[name] = {"frequency": float(b["frequency_hz"]), "bandwidth": float(b["bandwidth_hz"] or 12_500),
                             "mode": SDRPP_OUT[b["mode"]]}
        return json.dumps({"lists": lists}, indent=2), "sdrpp-bookmarks.json", "application/json"
    return (json.dumps({"format": "sentrysdr-bookmarks", "version": 1, "bookmarks": items}, indent=2),
            "sentrysdr-bookmarks.json", "application/json")


def _csv_safe(text: str) -> str:
    """GQRX splits on ';' and has no quoting, so one in a name would shift every column."""
    return text.replace(";", ",").replace("\n", " ").strip()


# --- in -----------------------------------------------------------------------------
def parse(text: str, fmt: str) -> list[dict]:
    if fmt == "gqrx":
        found = _parse_gqrx(text)
    elif fmt == "sdrpp":
        found = _parse_sdrpp(text)
    else:
        found = _parse_sentrysdr(text)
    return [b for b in (clean(item) for item in found) if b is not None][:MAX_BOOKMARKS]


def _parse_sentrysdr(text: str) -> list[dict]:
    data = json.loads(text)
    items = data.get("bookmarks") if isinstance(data, dict) else data
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _parse_gqrx(text: str) -> list[dict]:
    found = []
    for row in csv.reader(io.StringIO(text), delimiter=";"):
        cells = [c.strip() for c in row]
        if len(cells) < 4 or cells[0].startswith("#") or not cells[0].isdigit():
            continue  # the tag table, comments and headers
        tags = [t.strip() for t in cells[4].split(",")] if len(cells) > 4 else []
        folder = next((t for t in tags if t and t != "Untagged"), "")
        found.append({"frequency_hz": cells[0], "name": cells[1], "mode": GQRX_IN.get(cells[2].lower(), "nfm"),
                      "bandwidth_hz": cells[3] if cells[3].isdigit() else None, "folder": folder,
                      "notes": cells[5] if len(cells) > 5 else ""})
    return found


def _parse_sdrpp(text: str) -> list[dict]:
    data = json.loads(text)
    if not isinstance(data, dict):
        return []
    lists = data.get("lists") if isinstance(data.get("lists"), dict) else {"": data}
    found = []
    for folder, body in lists.items():
        entries = body.get("bookmarks") if isinstance(body, dict) else None
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            if isinstance(entry, dict):
                found.append({"name": name, "frequency_hz": entry.get("frequency"), "bandwidth_hz": entry.get("bandwidth"),
                              "mode": SDRPP_IN.get(entry.get("mode"), "nfm"), "folder": folder})
    return found
