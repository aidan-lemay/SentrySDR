"""Web tuner bookmarks in and out of other programs' formats: SentrySDR's own JSON, GQRX and SDR++.

A bookmark can be in several folders, as GQRX's tags are, and each folder has a color.
Each format keeps less than SentrySDR does, so a round trip through GQRX or SDR++ can
lose notes, or turn a mode into its nearest equivalent. SentrySDR's JSON keeps everything.
Parsing never trusts the file: anything malformed is skipped, and the caller limits sizes.
"""
from __future__ import annotations

import csv
import io
import json
import re

from .pipelines import FREQ_MAX_HZ, FREQ_MIN_HZ

MODES = ("nfm", "am", "wfm", "wfm_stereo", "usb", "lsb", "cw")
FORMATS = ("sentrysdr", "gqrx", "sdrpp")
MAX_BOOKMARKS = 2000
MAX_FOLDERS_EACH = 10
NAME_LENGTH, FOLDER_LENGTH, NOTES_LENGTH = 120, 60, 500
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
# New folders take these in turn: a categorical palette checked for color-blind separation.
DEFAULT_COLOURS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")

# GQRX's names for its modes. Ones SentrySDR doesn't have come in as the nearest it does.
GQRX_OUT = {"nfm": "Narrow FM", "am": "AM", "wfm": "WFM (mono)", "wfm_stereo": "WFM (stereo)",
            "usb": "USB", "lsb": "LSB", "cw": "CW-U"}
GQRX_IN = {"narrow fm": "nfm", "am": "am", "am-sync": "am", "wfm (mono)": "wfm", "wfm (stereo)": "wfm_stereo",
           "wfm (oirt)": "wfm", "usb": "usb", "lsb": "lsb", "cw-u": "cw", "cw-l": "cw", "cw": "cw"}
GQRX_UNTAGGED = "Untagged"
# SDR++'s radio module numbers its modes.
SDRPP_OUT = {"nfm": 0, "wfm": 1, "wfm_stereo": 1, "am": 2, "usb": 4, "cw": 5, "lsb": 6}
SDRPP_IN = {0: "nfm", 1: "wfm", 2: "am", 3: "am", 4: "usb", 5: "cw", 6: "lsb"}


def clean_folders(raw) -> list[str]:
    """Folder names, trimmed and without repeats, in the order given."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    found: list[str] = []
    for name in raw:
        name = str(name or "").strip()[:FOLDER_LENGTH]
        if name and name not in found and name != GQRX_UNTAGGED:
            found.append(name)
    return found[:MAX_FOLDERS_EACH]


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
    folders = clean_folders(item.get("folders") if item.get("folders") is not None else item.get("folder"))
    return {"name": name or f"{frequency / 1e6:.4f} MHz", "frequency_hz": frequency, "mode": mode, "bandwidth_hz": bandwidth,
            "folders": folders, "notes": str(item.get("notes") or "").strip()[:NOTES_LENGTH]}


def clean_colors(raw) -> dict[str, str]:
    """{folder: '#rrggbb'} from a file, keeping only well-formed ones."""
    if not isinstance(raw, dict):
        return {}
    return {name: str(color).lower() for name, color in raw.items()
            if clean_folders([name]) == [str(name).strip()[:FOLDER_LENGTH]] and COLOR.match(str(color))}


# --- out ----------------------------------------------------------------------------
def export(items: list[dict], colors: dict[str, str], fmt: str) -> tuple[str, str, str]:
    """(text, filename, media type) for a list of bookmarks and their folders' colors."""
    used = sorted({f for b in items for f in b["folders"]})
    if fmt == "gqrx":
        lines = ["# Tag name          ;  color", f"{GQRX_UNTAGGED:<20}; #c0c0c0"]
        lines += [f"{_csv_safe(tag):<20}; {colors.get(tag, '#c0c0c0')}" for tag in used]
        lines += ["", "# Frequency ; Name                     ; Modulation          ;  Bandwidth; Tags"]
        for b in items:
            tags = ",".join(_csv_safe(f).replace(",", " ") for f in b["folders"]) or GQRX_UNTAGGED
            lines.append(f"{b['frequency_hz']:>14}; {_csv_safe(b['name']):<25}; {GQRX_OUT[b['mode']]:<20}; "
                         f"{b['bandwidth_hz'] or 0:>10}; {tags}")
        return "\n".join(lines) + "\n", "bookmarks.csv", "text/csv"
    if fmt == "sdrpp":
        lists: dict[str, dict] = {}
        for b in items:
            for folder in b["folders"] or ["General"]:  # SDR++ has lists, not tags: one copy in each
                entries = lists.setdefault(folder, {"bookmarks": {}, "showOnWaterfall": True})["bookmarks"]
                name, n = b["name"], 2
                while name in entries:  # SDR++ keys bookmarks by name
                    name, n = f"{b['name']} ({n})", n + 1
                entries[name] = {"frequency": float(b["frequency_hz"]), "bandwidth": float(b["bandwidth_hz"] or 12_500),
                                 "mode": SDRPP_OUT[b["mode"]]}
        return json.dumps({"lists": lists}, indent=2), "sdrpp-bookmarks.json", "application/json"
    body = {"format": "sentrysdr-bookmarks", "version": 2, "folders": {f: colors[f] for f in used if f in colors}, "bookmarks": items}
    return json.dumps(body, indent=2), "sentrysdr-bookmarks.json", "application/json"


def _csv_safe(text: str) -> str:
    """GQRX splits on ';' and has no quoting, so one in a name would shift every column."""
    return text.replace(";", ",").replace("\n", " ").strip()


# --- in -----------------------------------------------------------------------------
def parse(text: str, fmt: str) -> tuple[list[dict], dict[str, str]]:
    """(bookmarks, {folder: color}) from a file in the given format."""
    if fmt == "gqrx":
        found, colors = _parse_gqrx(text)
    elif fmt == "sdrpp":
        found, colors = _parse_sdrpp(text), {}
    else:
        found, colors = _parse_sentrysdr(text)
    return [b for b in (clean(item) for item in found) if b is not None][:MAX_BOOKMARKS], clean_colors(colors)


def _parse_sentrysdr(text: str) -> tuple[list[dict], dict]:
    data = json.loads(text)
    items = data.get("bookmarks") if isinstance(data, dict) else data
    colors = data.get("folders") if isinstance(data, dict) else {}
    return ([i for i in items if isinstance(i, dict)] if isinstance(items, list) else []), colors


def _parse_gqrx(text: str) -> tuple[list[dict], dict]:
    found, colors = [], {}
    for row in csv.reader(io.StringIO(text), delimiter=";"):
        cells = [c.strip() for c in row]
        if not cells or not cells[0] or cells[0].startswith("#"):
            continue
        if len(cells) == 2 and not cells[0].isdigit():  # the tag table: name; color
            colors[cells[0]] = cells[1]
            continue
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        tags = [t.strip() for t in cells[4].split(",")] if len(cells) > 4 else []
        found.append({"frequency_hz": cells[0], "name": cells[1], "mode": GQRX_IN.get(cells[2].lower(), "nfm"),
                      "bandwidth_hz": cells[3] if cells[3].isdigit() else None, "folders": tags,
                      "notes": cells[5] if len(cells) > 5 else ""})
    return found, colors


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
                              "mode": SDRPP_IN.get(entry.get("mode"), "nfm"), "folders": [folder] if folder else []})
    return found
