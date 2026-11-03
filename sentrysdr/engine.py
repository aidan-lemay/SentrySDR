"""Supervises the external radio tools: one worker per configured SDR serial.

Call detection relies on rtl_fm writing nothing while its squelch is closed
(verified on real hardware): a gap longer than ``hang_seconds`` ends the call.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
import uuid
from array import array
from pathlib import Path
from typing import Awaitable, Callable

from . import pipelines, sdr
from .config import Config

log = logging.getLogger("sentrysdr.engine")

READ_SIZE = 4096
RESTART_MIN = 2.0
RESTART_MAX = 60.0
AIS_EXPIRY_SECONDS = 900
RADIO_KEYS = ("kind", "frequency_hz", "modulation", "squelch", "gain_db", "ppm")

CallSink = Callable[[dict, Path], Awaitable[None]]
PcmSink = Callable[[str, bytes, int], None]
TrackSink = Callable[[str, str, dict], None]
Status = Callable[[str, dict], None]


async def stop_process(proc: asyncio.subprocess.Process | None, timeout: float = 5.0) -> None:
    if proc is None or proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


async def _log_stderr(serial: str, stream, tail: list[str]) -> None:
    # Reads chunks, not lines: readline fails on a very long line, and if this stops
    # draining the pipe fills and the tool hangs.
    pending = b""
    while chunk := await stream.read(4096):
        lines = (pending + chunk).split(b"\n")
        pending = lines.pop()[-4096:]
        for line in lines:
            text = line.decode(errors="replace").strip()[:500]
            if text:
                tail.append(text)
                del tail[:-5]
                log.debug("[%s] %s", serial, text)


class PcmFramer:
    """Turn arbitrary pipe reads into whole mono 16-bit samples.

    A read can end partway through a sample, which would shift every later byte and
    turn the audio into static. Two-slot modes carry one slot per channel, and usually
    only one slot is active, so they're summed rather than averaged to keep the level.
    """

    def __init__(self, channels: int = 1):
        self.frame_bytes = 2 * channels
        self.channels = channels
        self.leftover = b""

    def __call__(self, chunk: bytes) -> bytes:
        data = self.leftover + chunk
        whole = len(data) - len(data) % self.frame_bytes
        self.leftover = data[whole:]
        data = data[:whole]
        if self.channels == 1 or not data:
            return data
        samples = array("h")
        samples.frombytes(data)
        if sys.byteorder == "big":
            samples.byteswap()
        mixed = array("h", (max(-32768, min(32767, left + right)) for left, right in zip(samples[::2], samples[1::2])))
        if sys.byteorder == "big":
            mixed.byteswap()
        return mixed.tobytes()


class CallWriter:
    """Accumulates PCM for one call, then encodes it to MP3."""

    def __init__(self, cfg: Config, spec: dict, serial: str, rate: int, tmp_dir: Path, sink: CallSink):
        self.cfg, self.spec, self.serial, self.rate, self.sink = cfg, spec, serial, rate, sink
        self.id = str(uuid.uuid4())
        self.started_at = time.time()
        self.raw_path = tmp_dir / f"{self.id}.raw"
        self.handle = open(self.raw_path, "wb")
        self.bytes = 0

    @property
    def duration(self) -> float:
        return self.bytes / (2 * self.rate)

    def write(self, chunk: bytes) -> None:
        self.handle.write(chunk)
        self.bytes += len(chunk)

    async def finish(self) -> None:
        self.handle.close()
        try:
            if self.duration < self.cfg.min_call_seconds:
                return
            mp3 = self.raw_path.with_suffix(".mp3")
            proc = await asyncio.create_subprocess_exec(
                *pipelines.encode_args(self.cfg, str(self.raw_path), self.rate, str(mp3)),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                log.error("[%s] encoding failed: %s", self.serial, err.decode(errors="replace")[:300])
                mp3.unlink(missing_ok=True)
                return
            spec = self.spec
            meta = {
                "id": self.id,
                "serial": self.serial,
                "source_id": spec["source_id"],
                "source_name": spec["source_name"],
                "target_id": spec["target_id"],
                "target_name": spec["target_name"],
                "recorder_name": spec["recorder_name"],
                "frequency_hz": spec["frequency_hz"],
                "modulation": spec["modulation"],
                "started_at": self.started_at,
                "ended_at": self.started_at + self.duration,
                "duration": self.duration,
            }
            await self.sink(meta, mp3)
        finally:
            self.raw_path.unlink(missing_ok=True)


class Worker:
    """Owns one SDR. Runs whichever pipeline its current spec requires."""

    def __init__(self, supervisor: "Supervisor", serial: str):
        self.sup = supervisor
        self.cfg = supervisor.cfg
        self.serial = serial
        self.spec: dict | None = None
        self.task: asyncio.Task | None = None
        self.state = "idle"
        self.error = ""
        self.in_call = False
        self.call_started: float | None = None
        self.last_call_at: float | None = None

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "in_call": self.in_call,
            "call_started": self.call_started,
            "last_call_at": self.last_call_at,
            "recorder_id": self.spec["recorder_id"] if self.spec else None,
            "target_id": self.spec["target_id"] if self.spec else None,
        }

    def _publish(self, state: str | None = None, error: str | None = None) -> None:
        if state is not None:
            self.state = state
        if error is not None:
            self.error = error
        self.sup.status(self.serial, self.snapshot())

    async def apply(self, spec: dict | None) -> None:
        current = self.spec
        same_radio = (
            current is not None and spec is not None
            and all(current.get(k) == spec.get(k) for k in RADIO_KEYS)
        )
        if same_radio:
            self.spec = spec  # names changed only: no retune, no dropped audio
            return
        if current is None and spec is None:
            return
        await self.stop()
        self.spec = spec
        if spec is None:
            self._publish("idle", "")
        else:
            self.task = asyncio.create_task(self._run(spec), name=f"sdr-{self.serial}")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def _run(self, spec: dict) -> None:
        delay = RESTART_MIN
        pipeline = {"adsb": self._run_adsb, "ais": self._run_ais}.get(spec["kind"], self._run_audio)
        while True:
            started = time.monotonic()
            error = ""
            try:
                devices = await asyncio.to_thread(self.sup.enumerate)
                index = sdr.index_for_serial(self.serial, devices)
                if index is None:
                    if sdr.has_duplicate(self.serial, devices):
                        error = "duplicate serial: set unique serials with rtl_eeprom -s"
                    else:
                        error = "SDR not connected"
                else:
                    error = await pipeline(index, spec)
            except asyncio.CancelledError:
                raise
            except FileNotFoundError as exc:
                error = f"tool not installed: {exc.filename}"
            except Exception as exc:  # keep supervising through any fault
                log.exception("[%s] pipeline failure", self.serial)
                error = str(exc)[:300]
            if time.monotonic() - started > RESTART_MAX:
                delay = RESTART_MIN
            self._publish("error", error or "pipeline exited")
            log.warning("[%s] restarting in %.0fs: %s", self.serial, delay, self.error)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RESTART_MAX)

    async def _run_audio(self, index: int, spec: dict) -> str:
        cfg = self.cfg
        rate = pipelines.pcm_rate(spec)
        framer = PcmFramer(pipelines.pcm_channels(spec))
        digital = spec["modulation"] in pipelines.DIGITAL
        tail: list[str] = []
        procs: list[asyncio.subprocess.Process] = []
        tasks: list[asyncio.Task] = []
        writer: CallWriter | None = None
        try:
            if digital:
                read_fd, write_fd = os.pipe()
                try:
                    tuner = await asyncio.create_subprocess_exec(
                        *pipelines.rtl_fm_args(cfg, index, spec),
                        stdout=write_fd, stderr=asyncio.subprocess.PIPE,
                    )
                    procs.append(tuner)
                    tasks.append(asyncio.create_task(_log_stderr(self.serial, tuner.stderr, tail)))
                    proc = await asyncio.create_subprocess_exec(
                        *pipelines.dsd_args(cfg, spec),
                        stdin=read_fd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        env=cfg.tool_env(),
                    )
                finally:
                    os.close(read_fd)
                    os.close(write_fd)
            else:
                proc = await asyncio.create_subprocess_exec(
                    *pipelines.rtl_fm_args(cfg, index, spec),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
            procs.append(proc)
            tasks.append(asyncio.create_task(_log_stderr(self.serial, proc.stderr, tail)))
            self._publish("listening", "")

            while True:
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(READ_SIZE), cfg.hang_seconds)
                except asyncio.TimeoutError:
                    if writer:
                        writer = await self._end_call(writer)
                    continue
                if not chunk:
                    break
                chunk = framer(chunk)
                if not chunk:
                    continue
                if writer is None:
                    writer = CallWriter(cfg, self.spec or spec, self.serial, rate, self.sup.tmp_dir, self.sup.call_sink)
                    self.in_call, self.call_started = True, writer.started_at
                    self._publish()
                writer.write(chunk)
                self.sup.pcm(self.serial, chunk, rate)
                if writer.duration >= cfg.max_call_seconds:
                    writer = await self._end_call(writer)  # stuck carriers and continuous mode
            await asyncio.gather(*(p.wait() for p in procs), return_exceptions=True)
            detail = tail[-1] if tail else ""
            return f"radio process exited ({proc.returncode}) {detail}".strip()
        finally:
            for proc_ in procs:
                await stop_process(proc_)
            for task in tasks:
                task.cancel()
            if writer:
                # Shielded so a retune or shutdown still saves the call in progress.
                await asyncio.shield(self._end_call(writer))

    async def _end_call(self, writer: CallWriter) -> None:
        self.in_call, self.call_started = False, None
        self.last_call_at = time.time()
        self._publish()
        try:
            await writer.finish()
        except Exception:
            log.exception("[%s] failed to save call %s", self.serial, writer.id)
        return None

    async def _run_adsb(self, index: int, spec: dict) -> str:
        json_dir = tempfile.mkdtemp(prefix=f"adsb-{self.serial}-", dir=self.sup.tmp_dir)
        tail: list[str] = []
        proc = await asyncio.create_subprocess_exec(
            *pipelines.readsb_args(self.cfg, index, spec, json_dir),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=self.cfg.tool_env(),
        )
        reader = asyncio.create_task(_log_stderr(self.serial, proc.stderr, tail))
        path = Path(json_dir) / "aircraft.json"
        self._publish("listening", "")
        try:
            while proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), 2.0)
                except asyncio.TimeoutError:
                    pass
                with contextlib.suppress(OSError, ValueError):
                    data = json.loads(await asyncio.to_thread(path.read_text))
                    self.sup.tracks("adsb", self.serial, {"aircraft": data.get("aircraft", [])})
            detail = tail[-1] if tail else ""
            return f"readsb exited ({proc.returncode}) {detail}".strip()
        finally:
            await stop_process(proc)
            reader.cancel()
            shutil.rmtree(json_dir, ignore_errors=True)

    async def _run_ais(self, index: int, spec: dict) -> str:
        tail: list[str] = []
        proc = await asyncio.create_subprocess_exec(
            *pipelines.ais_args(self.cfg, index, spec),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.cfg.tool_env(),
        )
        reader = asyncio.create_task(_log_stderr(self.serial, proc.stderr, tail))
        vessels: dict[str, dict] = {}
        last_push = 0.0
        self._publish("listening", "")
        try:
            while line := await proc.stdout.readline():
                merge_ais_message(vessels, line)
                now = time.time()
                if now - last_push >= 2.0:
                    last_push = now
                    for mmsi in [m for m, v in vessels.items() if now - v["seen_at"] > AIS_EXPIRY_SECONDS]:
                        del vessels[mmsi]
                    self.sup.tracks("ais", self.serial, {"vessels": list(vessels.values())})
            await proc.wait()
            detail = tail[-1] if tail else ""
            return f"AIS-catcher exited ({proc.returncode}) {detail}".strip()
        finally:
            await stop_process(proc)
            reader.cancel()


AIS_FIELDS = ("lat", "lon", "speed", "heading", "course", "shipname", "callsign", "destination", "shiptype")


def merge_ais_message(vessels: dict[str, dict], line: bytes) -> None:
    """Merge one AIS-catcher JSON message; static and position data arrive separately."""
    try:
        message = json.loads(line)
        mmsi = str(message["mmsi"])
    except (ValueError, KeyError, TypeError):
        return
    vessel = vessels.setdefault(mmsi, {"mmsi": mmsi})
    for field in AIS_FIELDS:
        if field in message:
            vessel[field] = message[field]
    vessel["seen_at"] = time.time()


class Supervisor:
    """Applies desired assignments, and only ever touches configured serials."""

    def __init__(self, cfg: Config, call_sink: CallSink, pcm: PcmSink, tracks: TrackSink, status: Status,
                 enumerate_fn: Callable[[], list[str]] = sdr.enumerate_serials):
        self.cfg, self.call_sink, self.pcm, self.tracks, self.status = cfg, call_sink, pcm, tracks, status
        self.enumerate = enumerate_fn
        self.tmp_dir = cfg.recordings_dir / ".tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        for leftover in self.tmp_dir.iterdir():
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)
            else:
                leftover.unlink(missing_ok=True)
        self.workers = {serial: Worker(self, serial) for serial in cfg.serials}

    async def apply(self, assignments: dict[str, dict | None]) -> None:
        for serial, worker in self.workers.items():
            await worker.apply(assignments.get(serial))

    async def shutdown(self) -> None:
        await asyncio.gather(*(w.stop() for w in self.workers.values()), return_exceptions=True)
