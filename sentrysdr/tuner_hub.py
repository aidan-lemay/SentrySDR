"""The master's side of web tuners: who's watching each one, who controls it, and routing.

A tuner runs on the node its SDR is plugged into, the master or a slave. Browsers only
ever talk to the master. The hub sends their commands on to the node, and hands
what comes back (waterfall rows to everyone watching, audio to its own listener)
to each browser's queue.

One person at a time owns each tuner: its center frequency, sample rate and gain.
Nobody owns it until someone with the tuner permission takes it. They keep it until
they release it, hand it to another listener who has that permission, or leave. An
admin can take it from anyone. Everyone else picks their own frequency and mode
inside the span.

This module needs no numpy, so a master can relay a slave's tuner without it.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

from .pipelines import FREQ_MAX_HZ, FREQ_MIN_HZ

log = logging.getLogger("sentrysdr.tuner")

# Keep in step with tuner.py, which a master without numpy can't import.
SAMPLE_RATES = (250_000, 1_024_000, 1_536_000, 1_800_000, 2_048_000, 2_400_000)
MODES = ("nfm", "am", "wfm", "wfm_stereo", "usb", "lsb", "cw")
MAX_VIEWERS = 8
QUEUE_SIZE = 96  # frames waiting for one browser; past this its frames are dropped, not buffered
# Waterfall rows stop being queued once this many frames are waiting, so on a link too slow for
# everything the rest of the queue is kept for audio and messages: a missing row is barely seen,
# a missing piece of audio is heard.
WATERFALL_ROOM = QUEUE_SIZE // 2
SYNC_SECONDS = 3.0
COMMANDS_PER_SECOND = 30


@dataclass
class Viewer:
    id: int
    user_id: int
    name: str
    is_admin: bool
    can_own: bool = False
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))
    listen: dict | None = None
    joined_at: float = field(default_factory=time.monotonic)
    allowance: float = COMMANDS_PER_SECOND
    checked_at: float = field(default_factory=time.monotonic)

    def put(self, item: tuple[str, object], waterfall: bool = False) -> None:
        if waterfall and self.queue.qsize() >= WATERFALL_ROOM:
            return  # falling behind: let audio through first
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            pass  # a slow connection loses frames rather than holding up everyone else

    def allowed(self) -> bool:
        """At most COMMANDS_PER_SECOND, so dragging across the waterfall can't flood the node."""
        now = time.monotonic()
        self.allowance = min(COMMANDS_PER_SECOND, self.allowance + (now - self.checked_at) * COMMANDS_PER_SECOND)
        self.checked_at = now
        if self.allowance < 1:
            return False
        self.allowance -= 1
        return True


@dataclass
class Room:
    viewers: dict[int, Viewer] = field(default_factory=dict)
    controller: int | None = None
    state: dict = field(default_factory=lambda: {"running": False})


class TunerError(Exception):
    """Refused; the message is shown to the person."""


def clean_listen(raw: dict) -> dict | None:
    """A listener's choice from a browser, with only known fields, or None."""
    try:
        listen = {"frequency_hz": int(raw["frequency_hz"]), "mode": str(raw.get("mode", "nfm")),
                  "deemphasis_us": int(raw.get("deemphasis_us", 75)), "morse": raw.get("morse") is True}
        if raw.get("bandwidth_hz") is not None:
            listen["bandwidth_hz"] = int(raw["bandwidth_hz"])
        if raw.get("squelch_db") is not None:
            listen["squelch_db"] = round(float(raw["squelch_db"]), 1)
    except (KeyError, TypeError, ValueError):
        return None
    if not FREQ_MIN_HZ <= listen["frequency_hz"] <= FREQ_MAX_HZ or listen["mode"] not in MODES:
        return None
    if listen["deemphasis_us"] not in (50, 75):
        return None
    if not 0 < listen.get("bandwidth_hz", 1) <= 250_000 or not -130 <= listen.get("squelch_db", 0) <= 0:
        return None
    return listen


class TunerHub:
    def __init__(self, send: Callable[[int, str, dict], bool]):
        """`send(node_id, serial, command)` delivers a command to a node's tuner; False if it can't."""
        self.send = send
        self.rooms: dict[tuple[int, str], Room] = {}
        self.ids = itertools.count(1)
        self.task: asyncio.Task | None = None

    # --- browsers -------------------------------------------------------------------------
    def join(self, key: tuple[int, str], user_id: int, name: str, is_admin: bool, can_own: bool = True) -> Viewer:
        room = self.rooms.setdefault(key, Room())
        if len(room.viewers) >= MAX_VIEWERS:
            raise TunerError(f"This tuner already has {MAX_VIEWERS} people on it. Try again later.")
        viewer = Viewer(next(self.ids), user_id, name, is_admin, can_own or is_admin)
        room.viewers[viewer.id] = viewer
        self._sync(key)
        self._announce(key)
        return viewer

    def leave(self, key: tuple[int, str], viewer: Viewer) -> None:
        room = self.rooms.get(key)
        if room is None or room.viewers.pop(viewer.id, None) is None:
            return
        self.send(key[0], key[1], {"op": "leave", "listener": viewer.id})
        if room.controller == viewer.id:
            room.controller = None  # free again: someone takes it, it isn't handed on by itself
        if not room.viewers:
            self._sync(key)
            del self.rooms[key]
            return
        self._announce(key)

    def command(self, key: tuple[int, str], viewer: Viewer, message: dict) -> None:
        """A browser's command, checked here before it goes anywhere."""
        room = self.rooms.get(key)
        if room is None or viewer.id not in room.viewers:
            return
        if not viewer.allowed():
            return
        op = message.get("t")
        if op == "listen":
            listen = clean_listen(message)
            if listen is None:
                raise TunerError("That isn't a frequency and mode the tuner can use.")
            viewer.listen = listen
            self.send(key[0], key[1], {"op": "listen", "listener": viewer.id, **listen})
            return
        if op == "claim":
            if not viewer.can_own:
                raise TunerError("You can listen, but you don't have permission to tune this SDR.")
            if room.controller is not None and room.controller != viewer.id and not viewer.is_admin:
                owner = room.viewers.get(room.controller)
                raise TunerError(f"{owner.name if owner else 'Someone'} has this tuner. They can hand it to you.")
            room.controller = viewer.id
            self._announce(key)
            return
        if op in ("release", "handover"):
            if room.controller != viewer.id:
                raise TunerError("You don't have this tuner.")
            if op == "release":
                room.controller = None
            else:
                to = room.viewers.get(message.get("to"))
                if to is None or not to.can_own:
                    raise TunerError("They can't take the tuner: they've left, or don't have permission to tune.")
                room.controller = to.id
            self._announce(key)
            return
        if op not in ("center", "rate", "gain"):
            return
        if room.controller != viewer.id:
            raise TunerError("Only the tuner's owner can change that. You can still choose your own frequency inside its span.")
        if op == "center":
            hz = message.get("hz")
            if not isinstance(hz, int) or not FREQ_MIN_HZ <= hz <= FREQ_MAX_HZ:
                raise TunerError(f"Choose a center frequency from {FREQ_MIN_HZ / 1e6:g} to {FREQ_MAX_HZ / 1e6:g} MHz.")
            self.send(key[0], key[1], {"op": "center", "hz": hz})
        elif op == "rate":
            if message.get("hz") not in SAMPLE_RATES:
                raise TunerError("That sample rate isn't one the tuner offers.")
            self.send(key[0], key[1], {"op": "rate", "hz": message["hz"]})
        else:
            db = message.get("db")
            if db is not None and (not isinstance(db, (int, float)) or not 0 <= db <= 60):
                raise TunerError("Gain goes from 0 to 60 dB, or automatic.")
            self.send(key[0], key[1], {"op": "gain", "db": db})

    # --- nodes ------------------------------------------------------------------------------
    def from_node(self, node_id: int, serial: str, kind: str, data: object) -> None:
        """Output from a node's tuner: a binary frame, or a JSON message as a dict."""
        room = self.rooms.get((node_id, serial))
        if room is None:
            return
        if kind == "frame" and isinstance(data, (bytes, bytearray)) and data:
            if data[:1] == b"W":
                for viewer in room.viewers.values():
                    viewer.put(("bytes", bytes(data)), waterfall=True)
            elif data[:1] == b"A" and len(data) >= 5:
                viewer = room.viewers.get(struct.unpack_from("<I", data, 1)[0])
                if viewer is not None:
                    viewer.put(("bytes", bytes(data)))
        elif kind == "json" and isinstance(data, dict):
            if data.get("t") == "state":
                was_running = room.state.get("running")
                room.state = {key: data.get(key) for key in ("running", "center_hz", "rate", "gain_db", "error")}
                if room.state["running"] and not was_running:
                    self._sync((node_id, serial))  # a fresh session: tell it who's listening
                self._announce((node_id, serial))
            elif data.get("t") == "rx":
                viewer = room.viewers.get(data.get("listener"))
                if viewer is not None:
                    viewer.put(("text", json.dumps({key: data.get(key) for key in ("t", "level_db", "open", "inside", "stereo", "rds", "morse")})))

    def node_gone(self, node_id: int) -> None:
        for (owner, serial), room in self.rooms.items():
            if owner == node_id:
                room.state = {"running": False, "error": "That SDR's machine is offline."}
                self._announce((owner, serial))

    # --- keeping nodes and browsers up to date ------------------------------------------------
    def _sync(self, key: tuple[int, str]) -> None:
        room = self.rooms.get(key)
        viewers = room.viewers.values() if room else []
        self.send(key[0], key[1], {"op": "sync", "watching": bool(viewers),
                                   "listeners": {str(v.id): v.listen for v in viewers if v.listen}})

    def _announce(self, key: tuple[int, str]) -> None:
        room = self.rooms.get(key)
        if room is None:
            return
        controller = room.viewers.get(room.controller)
        # The owner sees who it could hand the tuner to; nobody else needs the list.
        others = [{"id": v.id, "name": v.name} for v in room.viewers.values() if v.can_own and v.id != room.controller]
        for viewer in room.viewers.values():
            mine = viewer.id == room.controller
            viewer.put(("text", json.dumps({
                "t": "state", **room.state, "viewers": len(room.viewers),
                "controller": controller.name if controller else None, "you_control": mine,
                "can_claim": viewer.can_own and controller is None,
                "can_take": viewer.is_admin and controller is not None and not mine,
                "handover": others if mine else [],
            })))

    async def run(self) -> None:
        """Every few seconds, tell each node the full list again, so a dropped message or a restart heals itself."""
        while True:
            await asyncio.sleep(SYNC_SECONDS)
            for key in list(self.rooms):
                self._sync(key)
