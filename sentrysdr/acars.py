"""ACARS messages: cleaning what acarsdec prints, spotting duplicates, and feeding airframes.io.

acarsdec's JSON fields are documented at https://github.com/f00b4r0/acarsdec. A
message can arrive from a slave over the network, so every field is checked.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import time

log = logging.getLogger("sentrysdr.acars")

MAX_LINE = 16384
MAX_TEXT = 4000
DUPLICATE_SECONDS = 30.0
AIRFRAMES_DEFAULT = "feed.acars.io:5550"  # from docs.airframes.io, "Install acarsdec"
STATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,39}$")
FEED_RE = re.compile(r"^(?P<host>[A-Za-z0-9.-]{1,253}):(?P<port>\d{1,5})$")
# What is passed on to airframes.io: acarsdec's own fields, nothing else a node sent.
FEED_FIELDS = (
    "timestamp", "channel", "freq", "level", "noise", "error", "mode", "label", "block_id", "ack",
    "tail", "flight", "msgno", "text", "sublabel", "assstat", "end",
)
# Presets the target form offers. North America fits one stick; its fifth channel, 129.125, doesn't.
PRESETS = {
    "na": [130_025_000, 130_450_000, 131_125_000, 131_550_000],
    "eu": [131_525_000, 131_725_000, 131_825_000],
}


def _text(value, limit: int) -> str:
    return "" if value is None else str(value).replace("\x00", "")[:limit]


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) < 1e12 else None  # no NaN or huge values


def parse_line(line: bytes) -> dict | None:
    """One line of acarsdec output, or None if it isn't a JSON object."""
    if len(line) > MAX_LINE:
        return None
    try:
        value = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def clean(raw: dict) -> dict:
    """The fields SentrySDR stores, checked and trimmed."""
    freq = _number(raw.get("freq"))
    text = _text(raw.get("text"), MAX_TEXT)
    return {
        "frequency_hz": int(round(freq * 1e6)) if freq and 0 < freq < 10_000 else 0,
        "level": _number(raw.get("level")),
        "mode": _text(raw.get("mode"), 4),
        "label": _text(raw.get("label"), 4),
        "block_id": _text(raw.get("block_id"), 4),
        "msgno": _text(raw.get("msgno"), 8),
        "tail": normalize_tail(raw.get("tail")),
        "flight": _text(raw.get("flight"), 16).strip(),
        "text": text,
        "has_text": bool(text.strip()),
    }


def normalize_tail(value) -> str:
    """ACARS pads registrations with dots, e.g. '.N123UA'. Compare without them, or dashes."""
    return _text(value, 16).strip().lstrip(".").replace("-", "").upper()


def feed_payload(raw: dict, station_id: str) -> bytes:
    body = {key: raw[key] for key in FEED_FIELDS if key in raw and isinstance(raw[key], (str, int, float, bool))}
    body["station_id"] = station_id
    body["app"] = {"name": "SentrySDR", "ver": "1"}
    return json.dumps(body, separators=(",", ":")).encode()[:MAX_LINE]


class Duplicates:
    """Two sticks on one channel hear the same message. Keep the first."""

    def __init__(self, window: float = DUPLICATE_SECONDS):
        self.window = window
        self.seen: dict[tuple, float] = {}

    def first(self, message: dict, now: float | None = None) -> bool:
        now = now or time.monotonic()
        if len(self.seen) > 5000:
            self.seen = {key: at for key, at in self.seen.items() if now - at < self.window}
        key = (message["frequency_hz"], message["tail"], message["flight"], message["label"], message["msgno"], message["text"])
        last = self.seen.get(key)
        self.seen[key] = now
        return last is None or now - last >= self.window


class AirframesFeed:
    """Sends messages to airframes.io over UDP. Never waits on the network for long."""

    RESOLVE_SECONDS = 600

    def __init__(self):
        self._address: tuple | None = None
        self._resolved_for = ""
        self._resolved_at = 0.0
        self._sock: socket.socket | None = None

    async def _resolve(self, target: str) -> tuple | None:
        match = FEED_RE.match(target)
        if not match or not 0 < int(match["port"]) < 65536:
            return None
        if target == self._resolved_for and time.monotonic() - self._resolved_at < self.RESOLVE_SECONDS:
            return self._address
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(match["host"], int(match["port"]), type=socket.SOCK_DGRAM), 5)
        self._address = infos[0][4] if infos else None
        self._family = infos[0][0] if infos else None
        self._resolved_for, self._resolved_at = target, time.monotonic()
        return self._address

    async def send(self, target: str, payload: bytes) -> bool:
        try:
            address = await self._resolve(target)
            if address is None:
                return False
            if self._sock is None or self._sock.family != self._family:
                if self._sock:
                    self._sock.close()
                self._sock = socket.socket(self._family, socket.SOCK_DGRAM)
                self._sock.setblocking(False)
            self._sock.sendto(payload, address)
            return True
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("airframes.io feed failed: %s", exc)
            return False


class Registrations:
    """Aircraft registrations by ICAO hex, from the database bundled with tar1090.

    Files are split by hex prefix: each lists the longer prefixes that have their
    own file under "children". Only a few files are kept loaded at once.
    """

    MAX_FILES = 8

    def __init__(self, db_dir):
        self.db_dir = db_dir
        self._files: dict[str, dict] = {}

    def _load(self, prefix: str) -> dict:
        if prefix in self._files:
            self._files[prefix] = self._files.pop(prefix)  # most recently used goes last
            return self._files[prefix]
        try:
            import gzip
            data = json.loads(gzip.decompress((self.db_dir / f"{prefix}.js").read_bytes()))
        except (OSError, ValueError, EOFError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        self._files[prefix] = data
        while len(self._files) > self.MAX_FILES:
            self._files.pop(next(iter(self._files)))
        return data

    def lookup(self, hex_code) -> str:
        hex_code = str(hex_code or "").strip().upper().lstrip("~")
        if not re.fullmatch(r"[0-9A-F]{6}", hex_code):
            return ""
        prefix = hex_code[0]
        data = self._load(prefix)
        while hex_code[: len(prefix) + 1] in (data.get("children") or []) and len(prefix) < 5:
            prefix = hex_code[: len(prefix) + 1]
            data = self._load(prefix)
        entry = data.get(hex_code[len(prefix):])
        return str(entry[0]) if isinstance(entry, list) and entry and entry[0] else ""
