"""One running tuner on a node: rtl_tcp's samples through tuner.Tuner, and the commands that steer it.

The worker starts rtl_tcp on loopback, then hands its port to a TunerSession. The session
reads samples and does the signal processing in its own thread, so the event loop never
waits on numpy. Commands come in from the event loop and are applied in that thread
between blocks; what comes out (waterfall rows, audio, levels) goes back through `emit`,
called on the event loop.

Commands come from the master, which has already checked who sent them, but they're
checked again here: a bad one is dropped, never allowed to stop the tuner.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import socket
import struct
import threading
import time
from typing import Callable

import numpy as np

from . import tuner
from .pipelines import FREQ_MAX_HZ, FREQ_MIN_HZ

log = logging.getLogger("sentrysdr.tuner")

READ_SECONDS = 0.05  # samples handled at a time
STATUS_SECONDS = 0.5  # how often each listener's level, stereo and RDS are sent
STATE_SECONDS = 2.0
CONNECT_SECONDS = 8.0
RETUNE_SETTLE_SECONDS = 0.03  # samples still in flight from the old frequency are dropped
MAX_LISTENERS = 16

# rtl_tcp's commands: one byte, then a big-endian 32-bit value.
CMD_FREQ, CMD_RATE, CMD_GAIN_MODE, CMD_GAIN, CMD_PPM = 0x01, 0x02, 0x03, 0x04, 0x05


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def waterfall_frame(center_hz: int, rate: int, row: bytes) -> bytes:
    return b"W" + struct.pack("<QI", center_hz, rate) + row


def audio_frame(listener: int, rate: float, channels: int, level_db: float, is_open: bool, audio: bytes) -> bytes:
    return b"A" + struct.pack("<IfBfB", listener, rate, channels, level_db, is_open) + audio


def clean_settings(raw: dict) -> tuner.Settings | None:
    """A listener's settings from a command, or None if they don't make sense."""
    try:
        mode = str(raw.get("mode", "nfm"))
        frequency = int(raw["frequency_hz"])
        bandwidth = raw.get("bandwidth_hz")
        squelch = raw.get("squelch_db")
        deemphasis = int(raw.get("deemphasis_us", 75))
        settings = tuner.Settings(
            frequency_hz=frequency, mode=mode,
            bandwidth_hz=None if bandwidth is None else int(bandwidth),
            squelch_db=None if squelch is None else float(squelch),
            deemphasis_us=deemphasis,
        )
    except (KeyError, TypeError, ValueError):
        return None
    if mode not in tuner.MODES or not FREQ_MIN_HZ <= frequency <= FREQ_MAX_HZ or deemphasis not in (50, 75):
        return None
    if settings.squelch_db is not None and not tuner.DB_FLOOR <= settings.squelch_db <= 0:
        return None
    if settings.bandwidth_hz is not None and not 0 < settings.bandwidth_hz <= 250_000:
        return None
    return settings


class TunerSession:
    def __init__(self, port: int, center_hz: int, rate: int, gain_db: float | None,
                 emit: Callable[[str, object], None], loop: asyncio.AbstractEventLoop):
        self.port = port
        self.center_hz, self.rate, self.gain_db = center_hz, rate, gain_db
        self.emit_on_loop = emit
        self.loop = loop
        self.commands: queue.Queue = queue.Queue(256)
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.error = ""
        self.watching = False
        self.listening: dict[int, tuner.Settings] = {}

    # --- from the event loop ------------------------------------------------------
    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name=f"tuner-{self.port}", daemon=True)
        self.thread.start()

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def command(self, message: dict) -> None:
        try:
            self.commands.put_nowait(message)
        except queue.Full:
            log.warning("tuner command queue full; dropping %s", message.get("op"))

    async def stop(self) -> None:
        self.stopping.set()
        if self.thread is not None:
            await asyncio.to_thread(self.thread.join, 5)

    def state(self) -> dict:
        return {"t": "state", "running": True, "center_hz": self.center_hz, "rate": self.rate,
                "gain_db": self.gain_db, "error": self.error}

    # --- in the session's thread -----------------------------------------------------
    def _emit(self, kind: str, data: object) -> None:
        try:
            self.loop.call_soon_threadsafe(self.emit_on_loop, kind, data)
        except RuntimeError:  # the event loop has closed: the service is stopping
            self.stopping.set()

    def _connect(self) -> socket.socket:
        deadline = time.monotonic() + CONNECT_SECONDS
        while True:
            try:
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                break
            except OSError:
                if time.monotonic() > deadline or self.stopping.is_set():
                    raise
                time.sleep(0.2)
        sock.settimeout(2)
        header = b""
        while len(header) < 12:  # "RTL0", the tuner type and how many gains it has
            chunk = sock.recv(12 - len(header))
            if not chunk:
                raise OSError("rtl_tcp closed the connection")
            header += chunk
        return sock

    def _send(self, sock: socket.socket, command: int, value: int) -> None:
        sock.sendall(struct.pack(">BI", command, value & 0xFFFFFFFF))

    def _set_gain(self, sock: socket.socket) -> None:
        if self.gain_db is None:
            self._send(sock, CMD_GAIN_MODE, 0)
        else:
            self._send(sock, CMD_GAIN_MODE, 1)
            self._send(sock, CMD_GAIN, int(round(self.gain_db * 10)))

    def _apply(self, sock: socket.socket, dsp: tuner.Tuner, message: dict) -> int:
        """One command. Returns how many bytes of samples to throw away after it."""
        op = message.get("op")
        if op == "center":
            hz = message.get("hz")
            if isinstance(hz, int) and FREQ_MIN_HZ <= hz <= FREQ_MAX_HZ:
                self.center_hz = hz
                self._send(sock, CMD_FREQ, hz)
                dsp.set_center(hz)
                self._state()
                return int(self.rate * RETUNE_SETTLE_SECONDS) * 2
        elif op == "rate":
            hz = message.get("hz")
            if hz in tuner.SAMPLE_RATES:
                self.rate = hz
                self._send(sock, CMD_RATE, hz)
                dsp.set_rate(hz)
                self._state()
                return int(self.rate * RETUNE_SETTLE_SECONDS) * 2
        elif op == "gain":
            gain = message.get("db")
            if gain is None or (isinstance(gain, (int, float)) and 0 <= gain <= 60):
                self.gain_db = None if gain is None else float(gain)
                self._set_gain(sock)
                self._state()
        elif op == "listen":
            listener, settings = message.get("listener"), clean_settings(message)
            if isinstance(listener, int) and settings is not None and (listener in self.listening or len(self.listening) < MAX_LISTENERS):
                self.listening[listener] = settings
                dsp.listen(listener, settings)
        elif op == "leave":
            self.listening.pop(message.get("listener"), None)
            dsp.leave(message.get("listener"))
        elif op == "sync":
            # The master's full list of who's watching. Catches up after a restart or a dropped link.
            wanted = message.get("listeners") if isinstance(message.get("listeners"), dict) else {}
            self.watching = bool(message.get("watching"))
            keep = {}
            for key, raw in list(wanted.items())[:MAX_LISTENERS]:
                settings = clean_settings(raw) if isinstance(raw, dict) else None
                if settings is not None and str(key).isdigit():
                    keep[int(key)] = settings
            for listener in list(self.listening):
                if listener not in keep:
                    dsp.leave(listener)
            for listener, settings in keep.items():
                if self.listening.get(listener) != settings:
                    dsp.listen(listener, settings)
            self.listening = keep
            self._state()
        return 0

    def _state(self) -> None:
        self._emit("json", self.state())

    def _run(self) -> None:
        sock = None
        try:
            sock = self._connect()
            self._send(sock, CMD_RATE, self.rate)
            self._send(sock, CMD_FREQ, self.center_hz)
            self._set_gain(sock)
            dsp = tuner.Tuner(self.rate, self.center_hz)
            self._state()
            discard = 0
            last_status = last_state = time.monotonic()
            while not self.stopping.is_set():
                while True:
                    try:
                        discard = max(discard, self._apply(sock, dsp, self.commands.get_nowait()))
                    except queue.Empty:
                        break
                want = int(self.rate * READ_SECONDS) * 2
                try:
                    data = sock.recv(want)
                except socket.timeout:
                    continue
                if not data:
                    self.error = "rtl_tcp stopped sending samples"
                    break
                if discard:
                    cut = min(discard, len(data))
                    discard -= cut
                    data = data[cut:]
                if not data or not self.watching:
                    continue  # nobody's looking: read and drop, so rtl_tcp doesn't back up
                rows, audio = dsp.feed(data)
                for row in rows:
                    self._emit("frame", waterfall_frame(self.center_hz, self.rate, row))
                for listener, pieces in audio.items():
                    receiver = dsp.receivers.get(listener)
                    if receiver is None or not pieces:
                        continue
                    joined = np.concatenate(pieces)
                    if len(joined):
                        channels = 2 if receiver.settings.mode == "wfm_stereo" else 1
                        self._emit("frame", audio_frame(listener, receiver.audio_rate, channels, receiver.level_db,
                                                        receiver.open, tuner.mulaw(joined)))
                now = time.monotonic()
                if now - last_status >= STATUS_SECONDS:
                    last_status = now
                    for listener, receiver in dsp.receivers.items():
                        status = {"t": "rx", "listener": listener, "level_db": round(receiver.level_db, 1),
                                  "open": receiver.open, "inside": receiver.inside, "stereo": receiver.stereo}
                        if receiver.rds is not None:
                            status["rds"] = receiver.rds.text.view()
                        self._emit("json", status)
                if now - last_state >= STATE_SECONDS:
                    last_state = now
                    self._state()
        except OSError as exc:
            if not self.stopping.is_set():
                self.error = f"couldn't read from rtl_tcp: {exc}"[:200]
        except Exception as exc:  # keep the error for the worker; never take the service down
            log.exception("tuner failed")
            self.error = f"tuner failed: {exc}"[:200]
        finally:
            if sock is not None:
                sock.close()
