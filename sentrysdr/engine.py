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

from . import acars, capabilities, feeds, pipelines, sdr
from .config import Config

log = logging.getLogger("sentrysdr.engine")

READ_SIZE = 4096
RESTART_MIN = 2.0
RESTART_MAX = 60.0
AIS_EXPIRY_SECONDS = 900
SQUELCH_STUCK_SECONDS = 600
SQUELCH_STUCK_WARNING = "Squelch has been open for over 10 minutes. If nothing is transmitting, measure this SDR and raise its squelch."
MEASURE_SECONDS = 6.0
MEASURE_ATTEMPTS = 3
DETECT_SECONDS = 8.0
FEED_CHECK_EVERY = 5  # ADS-B loop turns (2 s each) between feed connection checks
# Signs that a stick didn't open cleanly: USB timeouts (-7), librtlsdr resetting it,
# or a setup step failing. Seen on real hardware after a tool was killed without
# closing the stick. It may then send nothing, which looks the same as a closed squelch.
USB_FAULT_MARKERS = ("failed with -7", "Resetting device", "Failed to set")
# A tool prints these as it exits, and the errors that follow them are normal.
EXIT_MARKERS = ("User cancel", "Signal caught")
RESET_SETTLE_SECONDS = 3.0
RESET_RESTART_GAP = 60.0  # at most one automatic restart a minute, so a stick that always resets can't loop
RADIO_KEYS = ("kind", "frequency_hz", "modulation", "squelch", "gain_db", "ppm")

CallSink = Callable[[dict, Path], Awaitable[None]]
PcmSink = Callable[[str, bytes, int], None]
TrackSink = Callable[[str, str, dict], None]
Status = Callable[[str, dict], None]


class MeasureError(Exception):
    """A noise measurement could not be taken. The message is shown to the user."""


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


async def _log_stderr(serial: str, stream, tail: list[str], on_reset: Callable[[], None] | None = None) -> None:
    # Reads chunks, not lines: readline fails on a very long line, and if this stops
    # draining the pipe fills and the tool hangs.
    pending = b""
    exiting = False
    while chunk := await stream.read(4096):
        lines = (pending + chunk).split(b"\n")
        pending = lines.pop()[-4096:]
        for line in lines:
            text = line.decode(errors="replace").strip()[:500]
            if text:
                tail.append(text)
                del tail[:-5]
                log.debug("[%s] %s", serial, text)
                exiting = exiting or any(marker in text for marker in EXIT_MARKERS)
                if on_reset and not exiting and any(marker in text for marker in USB_FAULT_MARKERS):
                    on_reset()


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


class VolumeControl:
    """Scale 16-bit audio. At 0 dB it passes the audio through untouched.

    A tuning offset puts a large DC step on each call, and scaling that would clip the
    voice for the first seconds, so a boost also removes DC. Peaks limit at full scale.
    """

    DC_POLE = 0.995  # about 13 Hz at 16 kHz, well below voice

    def __init__(self):
        self.previous_in = 0.0
        self.previous_out = 0.0

    def __call__(self, chunk: bytes, db: float) -> bytes:
        if not db:
            self.previous_in = self.previous_out = 0.0
            return chunk
        samples = array("h")
        samples.frombytes(chunk)
        if sys.byteorder == "big":
            samples.byteswap()
        factor = 10 ** (db / 20)
        previous_in, previous_out, pole = self.previous_in, self.previous_out, self.DC_POLE
        for index, value in enumerate(samples):
            previous_out = value - previous_in + pole * previous_out
            previous_in = value
            scaled = int(previous_out * factor)
            samples[index] = 32767 if scaled > 32767 else -32768 if scaled < -32768 else scaled
        self.previous_in, self.previous_out = previous_in, previous_out
        if sys.byteorder == "big":
            samples.byteswap()
        return samples.tobytes()


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
        self.measuring = False
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
        self.note = ""  # an informational warning that outlives the error field
        self.feed_status: dict = {}
        self.note_at: float | None = None
        self._reset_seen = asyncio.Event()
        self._last_reset_restart = -RESET_RESTART_GAP

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "in_call": self.in_call,
            "call_started": self.call_started,
            "last_call_at": self.last_call_at,
            "note": self.note,
            "feeds": self.feed_status,
            "note_at": self.note_at,
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
        if self.measuring:
            self.spec = spec  # started when the measurement finishes
            return
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

    @contextlib.asynccontextmanager
    async def _paused(self, state: str):
        """Stop this SDR's assignment, hand over its device index, then resume."""
        if self.measuring:
            raise MeasureError("This SDR is busy with another measurement. Try again in a moment.")
        self.measuring = True
        try:
            await self.stop()  # saves a call in progress
            self.in_call, self.call_started = False, None
            self._publish(state, "")
            devices = await asyncio.to_thread(self.sup.enumerate)
            index = sdr.index_for_serial(self.serial, devices)
            if index is None:
                raise MeasureError("This SDR isn't connected, or its serial is duplicated.")
            yield index
        finally:
            self.measuring = False
            if self.spec is not None:
                self.task = asyncio.create_task(self._run(self.spec), name=f"sdr-{self.serial}")
            else:
                self._publish("idle", "")

    async def measure(self, frequency_hz: int, modulation: str, gain_db: float | None, ppm: int) -> dict:
        """Pause this SDR's assignment, read the noise level, then resume."""
        async with self._paused("measuring") as index:
            args = pipelines.measure_args(self.cfg, index, frequency_hz, modulation, gain_db, ppm)
            return await self._read_levels(args, pipelines.measure_rate(modulation))

    async def detect(self) -> dict:
        """Pause this SDR's assignment, read its tuner and gain steps, then resume."""
        async with self._paused("detecting") as index:
            args = [self.cfg.binary("rtl_test") or "rtl_test", "-d", str(index), "-t"]
            for attempt in range(MEASURE_ATTEMPTS):
                if attempt:
                    await asyncio.sleep(1.0)  # the stick can take a moment to be released
                text = await self._read_text(args, DETECT_SECONDS)
                found = capabilities.parse_rtl_test(text)
                if found["tuner"]:
                    return found
            last = [line for line in text.splitlines() if line.strip()]
            raise MeasureError(f"Couldn't read the tuner. {last[-1] if last else ''}".strip()[:300])

    async def _read_text(self, args: list[str], seconds: float) -> str:
        """Run a tool briefly and return what it printed. Stops early once the gain steps are out."""
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=self.cfg.tool_env(),
        )
        out = b""
        deadline = time.monotonic() + seconds
        try:
            while (remaining := deadline - time.monotonic()) > 0 and len(out) < 65536:
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(4096), remaining)
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                out += chunk
                if b"Supported gain values" in out and out.endswith(b"\n"):
                    break  # an E4000 would go on to a long benchmark
        finally:
            await stop_process(proc)  # SIGTERM lets rtl_test close the stick cleanly
        return out.decode(errors="replace")

    async def _read_levels(self, args: list[str], rate: int) -> dict:
        tail: list[str] = []
        for attempt in range(MEASURE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(1.0)  # the stick can take a moment to be released
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            drain = asyncio.create_task(_log_stderr(self.serial, proc.stderr, tail))
            pcm = bytearray()
            deadline = time.monotonic() + MEASURE_SECONDS + 1  # the first second is discarded
            try:
                while (remaining := deadline - time.monotonic()) > 0:
                    try:
                        chunk = await asyncio.wait_for(proc.stdout.read(65536), remaining)
                    except asyncio.TimeoutError:
                        break
                    if not chunk:
                        break
                    pcm += chunk
            finally:
                await stop_process(proc)
                drain.cancel()
            levels = await asyncio.to_thread(pipelines.signal_levels, bytes(pcm), rate)
            if levels:
                return pipelines.measure_summary(levels)
        raise MeasureError(f"No samples from the SDR. {tail[-1] if tail else ''}".strip()[:300])

    def _on_reset(self) -> None:
        self._reset_seen.set()

    def _set_note(self, text: str) -> None:
        self.note, self.note_at = text, (time.time() if text else None)
        self._publish()

    async def _watch_resets(self, pipeline: Awaitable[str]) -> str | None:
        """Run a pipeline. If the stick had USB errors on open, restart it once so it opens cleanly.

        Returns None to restart straight away, otherwise the pipeline's exit error.
        """
        self._reset_seen.clear()
        task = asyncio.ensure_future(pipeline)
        watcher = asyncio.create_task(self._reset_seen.wait())
        try:
            await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                return task.result()
            await asyncio.sleep(RESET_SETTLE_SECONDS)  # let the open finish before stopping it
            clock = time.strftime("%H:%M")
            if time.monotonic() - self._last_reset_restart < RESET_RESTART_GAP:
                log.warning("[%s] USB errors again on start; left running", self.serial)
                self._set_note(f"USB errors on start twice in a row at {clock}. If it records nothing, check its USB connection.")
                return await task
            self._last_reset_restart = time.monotonic()
            log.warning("[%s] USB errors on start; restarting it so it opens cleanly", self.serial)
            self._set_note(f"USB errors on start at {clock}, so it was restarted. If it records nothing, check its USB connection.")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return None
        finally:
            watcher.cancel()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _run(self, spec: dict) -> None:
        delay = RESTART_MIN
        pipeline = {"adsb": self._run_adsb, "ais": self._run_ais, "acars": self._run_acars}.get(spec["kind"], self._run_audio)
        self._set_note("")  # a new assignment starts with a clean slate
        self.feed_status = {}
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
                    error = await self._watch_resets(pipeline(index, spec))
                    if error is None:
                        await asyncio.sleep(1.0)  # the stick can take a moment to be released
                        continue
            except asyncio.CancelledError:
                raise
            except FileNotFoundError as exc:
                # The filename can be missing here, so the whole message names the tool better.
                error = f"tool not installed: {exc.filename or exc}"[:300]
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
        volume = VolumeControl()
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
                    tasks.append(asyncio.create_task(_log_stderr(self.serial, tuner.stderr, tail, self._on_reset)))
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
            tasks.append(asyncio.create_task(_log_stderr(self.serial, proc.stderr, tail, self._on_reset)))
            self._publish("listening", "")
            open_since: float | None = None
            warn_stuck = spec["squelch"] > 0 and not digital

            while True:
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(READ_SIZE), cfg.hang_seconds)
                except asyncio.TimeoutError:
                    open_since = None
                    if writer:
                        writer = await self._end_call(writer)
                    if self.error:
                        self._publish(error="")
                    continue
                if not chunk:
                    break
                chunk = framer(chunk)
                if not chunk:
                    continue
                # Read each time: changing a target's volume needs no retune.
                chunk = volume(chunk, (self.spec or spec).get("volume_db", 0))
                if writer is None:
                    writer = CallWriter(cfg, self.spec or spec, self.serial, rate, self.sup.tmp_dir, self.sup.call_sink)
                    self.in_call, self.call_started = True, writer.started_at
                    self._publish()
                writer.write(chunk)
                self.sup.pcm(self.serial, chunk, rate)
                # Measured across max_call_seconds splits, which don't close the squelch.
                open_since = open_since or time.monotonic()
                if warn_stuck and not self.error and time.monotonic() - open_since >= SQUELCH_STUCK_SECONDS:
                    self._publish(error=SQUELCH_STUCK_WARNING)
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
        reader = asyncio.create_task(_log_stderr(self.serial, proc.stderr, tail, self._on_reset))
        path = Path(json_dir) / "aircraft.json"
        self._publish("listening", "")
        settings = spec.get("feeds") or {}
        addresses = {key: await asyncio.to_thread(feeds.resolve, feeds.aggregator_hosts(key))
                     for key in settings.get("aggregators") or []}
        checks = 0
        try:
            while proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), 2.0)
                except asyncio.TimeoutError:
                    pass
                if (addresses or settings.get("beast_port")) and checks % FEED_CHECK_EVERY == 0:
                    self._check_feeds(proc.pid, addresses, settings.get("beast_port"))
                checks += 1
                with contextlib.suppress(OSError, ValueError):
                    data = json.loads(await asyncio.to_thread(path.read_text))
                    self.sup.tracks("adsb", self.serial, {"aircraft": data.get("aircraft", [])})
            detail = tail[-1] if tail else ""
            return f"readsb exited ({proc.returncode}) {detail}".strip()
        finally:
            await stop_process(proc)
            reader.cancel()
            shutil.rmtree(json_dir, ignore_errors=True)

    async def _run_acars(self, index: int, spec: dict) -> str:
        tail: list[str] = []
        proc = await asyncio.create_subprocess_exec(
            *pipelines.acars_args(self.cfg, index, spec),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.cfg.tool_env(),
            limit=acars.MAX_LINE * 2,
        )
        reader = asyncio.create_task(_log_stderr(self.serial, proc.stderr, tail, self._on_reset))
        self._publish("listening", "")
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError:  # a line over the limit; readline has already dropped it
                    continue
                if not line:
                    break
                message = acars.parse_line(line)
                if message is not None:
                    self.last_call_at = time.time()
                    self.sup.tracks("acars", self.serial, {"message": message, "target_id": spec["target_id"]})
            await proc.wait()
            detail = tail[-1] if tail else ""
            return f"acarsdec exited ({proc.returncode}) {detail}".strip()
        finally:
            await stop_process(proc)
            reader.cancel()

    def _check_feeds(self, pid: int, addresses: dict[str, set[str]], beast_port: int | None) -> None:
        """Which aggregators readsb is connected to, and how many local programs read its Beast port."""
        connections = feeds.established(pid)
        remotes = {remote[0] for local, remote in connections if local[1] != beast_port}
        status = {"aggregators": {key: bool(found & remotes) for key, found in addresses.items()}}
        if beast_port:
            status["local_clients"] = sum(1 for local, _ in connections if local[1] == beast_port)
        if status != self.feed_status:
            self.feed_status = status
            self._publish()

    async def _run_ais(self, index: int, spec: dict) -> str:
        tail: list[str] = []
        proc = await asyncio.create_subprocess_exec(
            *pipelines.ais_args(self.cfg, index, spec),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.cfg.tool_env(),
        )
        reader = asyncio.create_task(_log_stderr(self.serial, proc.stderr, tail, self._on_reset))
        settings = spec.get("feeds") or {}
        if settings.get("udp") or settings.get("community"):
            # UDP has no connection to check, so this only says what is being sent.
            self.feed_status = {"sending": [d["service"] for d in settings.get("udp") or []] + (["community"] if settings.get("community") else [])}
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

    async def measure(self, serial: str, **request) -> dict:
        worker = self.workers.get(serial)
        if worker is None:
            raise MeasureError("That SDR isn't in sdr.serials on this node.")
        if request.get("op") == "detect":
            return await worker.detect()
        return await worker.measure(**request)

    async def shutdown(self) -> None:
        await asyncio.gather(*(w.stop() for w in self.workers.values()), return_exceptions=True)
