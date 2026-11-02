"""The slave role. It opens no listening sockets and only dials out to the master.

It keeps recording through a lost link: the last assignments are saved to disk
and finished calls wait in a local spool until they can be uploaded.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets

from . import pipelines
from .config import Config
from .engine import Supervisor
from .sdr import enumerate_serials

log = logging.getLogger("sentrysdr.slave")

RECONNECT_MIN = 2.0
RECONNECT_MAX = 60.0
STATUS_SECONDS = 5
MAX_WS_MESSAGE = 8 * 1024 * 1024


def encode_live_frame(serial: str, rate: int, pcm: bytes) -> bytes:
    raw = serial.encode()
    return bytes([len(raw)]) + raw + rate.to_bytes(4, "big") + pcm


def ws_url(master_url: str) -> str:
    parts = urlsplit(master_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + "/api/nodes/agent/ws", "", ""))


def api_url(master_url: str, path: str) -> str:
    return master_url.rstrip("/") + path


class Slave:
    def __init__(self, cfg: Config, enumerate_fn=enumerate_serials):
        self.cfg = cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.key_file = cfg.data_dir / "node.key"
        self.state_file = cfg.data_dir / "assignments.json"
        self.spool = cfg.data_dir / "spool"
        self.spool.mkdir(parents=True, exist_ok=True)
        self.status: dict[str, dict] = {}
        self.live_on: set[str] = set()
        self.outgoing: asyncio.Queue | None = None
        self.spool_event: asyncio.Event | None = None
        self.supervisor = Supervisor(
            cfg, self._spool_call, self._on_pcm, self._on_tracks, self._on_status, enumerate_fn,
        )

    # --- engine callbacks -------------------------------------------------
    async def _spool_call(self, meta: dict, path: Path) -> None:
        await asyncio.to_thread(self._write_spool, meta, path)
        if self.spool_event:
            self.spool_event.set()

    def _write_spool(self, meta: dict, path: Path) -> None:
        shutil.move(str(path), self.spool / f"{meta['id']}.mp3")
        temporary = self.spool / f"{meta['id']}.json.tmp"
        temporary.write_text(json.dumps(meta))
        temporary.rename(self.spool / f"{meta['id']}.json")  # .json appears only once the call is complete
        self._enforce_spool_limit()

    def _enforce_spool_limit(self) -> None:
        """Bound disk use on a long outage by dropping the oldest calls."""
        limit = self.cfg.spool_max_mb * 1024 * 1024
        calls = sorted(self.spool.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in calls)
        for path in calls:
            if total <= limit:
                break
            total -= path.stat().st_size
            log.warning("spool over %s MB, dropping oldest call %s", self.cfg.spool_max_mb, path.stem)
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)

    def _on_pcm(self, serial: str, pcm: bytes, rate: int) -> None:
        if serial in self.live_on:
            self._send(encode_live_frame(serial, rate, pcm))

    def _on_tracks(self, kind: str, serial: str, data: dict) -> None:
        self._send(json.dumps({"t": "tracks", "kind": kind, "serial": serial, "data": data}))

    def _on_status(self, serial: str, status: dict) -> None:
        self.status[serial] = status

    def _send(self, message) -> None:
        if self.outgoing is not None and not self.outgoing.full():
            self.outgoing.put_nowait(message)

    # --- credentials ------------------------------------------------------
    def device_key(self) -> str | None:
        with contextlib.suppress(OSError):
            return self.key_file.read_text().strip() or None
        return None

    def _save_key(self, key: str) -> None:
        fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(key)

    async def ensure_enrolled(self, client: httpx.AsyncClient) -> str:
        """Swap the one-time token for a device key, retrying until it's possible."""
        delay = RECONNECT_MIN
        while True:
            key = self.device_key()
            if key:
                return key
            if not self.cfg.enrollment_token:
                log.error("no device key and no master.enrollment_token configured; waiting")
            else:
                try:
                    response = await client.post(
                        api_url(self.cfg.master_url, "/api/nodes/agent/enroll"),
                        json={"token": self.cfg.enrollment_token},
                    )
                    if response.status_code == 200:
                        self._save_key(response.json()["key"])
                        log.info("enrolled with master")
                        continue
                    log.error("enrolment refused: HTTP %s", response.status_code)
                except httpx.HTTPError as exc:
                    log.warning("cannot reach master to enrol: %s", exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    # --- saved assignments ------------------------------------------------
    def load_assignments(self) -> dict:
        with contextlib.suppress(OSError, ValueError):
            return self.clean_assignments(json.loads(self.state_file.read_text()))
        return {}

    def save_assignments(self, assignments: dict) -> None:
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(assignments))
        temporary.rename(self.state_file)

    def clean_assignments(self, assignments: dict) -> dict:
        """Accept only configured serials and valid specs, whatever the master sends."""
        clean = {}
        for serial, spec in (assignments or {}).items():
            if serial not in self.cfg.serials:
                log.warning("ignoring assignment for unconfigured serial %s", serial)
                continue
            try:
                clean[serial] = pipelines.sanitize_spec(spec)
            except pipelines.SpecError as exc:
                log.error("ignoring invalid assignment for %s: %s", serial, exc)
        return clean

    # --- spool upload -----------------------------------------------------
    async def upload_spool(self, client: httpx.AsyncClient, key: str) -> None:
        for meta_path in sorted(self.spool.glob("*.json")):
            audio = meta_path.with_suffix(".mp3")
            if not audio.exists():
                meta_path.unlink(missing_ok=True)
                continue
            with audio.open("rb") as handle:
                response = await client.post(
                    api_url(self.cfg.master_url, "/api/nodes/agent/calls"),
                    headers={"Authorization": f"Bearer {key}"},
                    data={"meta": meta_path.read_text()},
                    files={"audio": (audio.name, handle, "audio/mpeg")},
                )
            if response.status_code == 200:
                audio.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
            elif response.status_code in (413, 422):
                log.error("master rejected call %s: HTTP %s; discarding", meta_path.stem, response.status_code)
                audio.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
            else:
                raise httpx.HTTPError(f"upload returned HTTP {response.status_code}")

    async def _spool_loop(self, client: httpx.AsyncClient, key: str) -> None:
        while True:
            try:
                await self.upload_spool(client, key)
            except httpx.HTTPError as exc:
                log.warning("spool upload deferred: %s", exc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.spool_event.wait(), 30)
            self.spool_event.clear()

    # --- master link ------------------------------------------------------
    async def _link_once(self, key: str) -> None:
        async with websockets.connect(
            ws_url(self.cfg.master_url),
            additional_headers={"Authorization": f"Bearer {key}"},
            max_size=MAX_WS_MESSAGE, ping_interval=20, ping_timeout=20,
        ) as socket:
            await socket.send(json.dumps({"t": "hello", "name": self.cfg.node_name, "serials": self.cfg.serials}))
            log.info("connected to master")
            self.outgoing = asyncio.Queue(512)

            async def sender():
                while True:
                    await socket.send(await self.outgoing.get())

            async def status_loop():
                while True:
                    await socket.send(json.dumps({"t": "status", "sources": self.status}))
                    await asyncio.sleep(STATUS_SECONDS)

            async def receiver():
                async for raw in socket:
                    if isinstance(raw, bytes):
                        continue
                    await self.handle_message(json.loads(raw))

            tasks = [asyncio.create_task(c()) for c in (sender, status_loop, receiver)]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                self.outgoing = None
                self.live_on.clear()

    async def handle_message(self, message: dict) -> None:
        kind = message.get("t")
        if kind == "desired":
            clean = self.clean_assignments(message.get("assignments"))
            await asyncio.to_thread(self.save_assignments, clean)
            await self.supervisor.apply(clean)
        elif kind == "live":
            serial = message.get("serial")
            if serial in self.cfg.serials:
                (self.live_on.add if message.get("on") else self.live_on.discard)(serial)
        elif kind == "revoked":
            log.error("master revoked this node; removing the device key")
            self.key_file.unlink(missing_ok=True)

    async def run(self) -> None:
        self.spool_event = asyncio.Event()
        await self.supervisor.apply(self.load_assignments())  # keep recording while offline
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            key = await self.ensure_enrolled(client)
            spool_task = asyncio.create_task(self._spool_loop(client, key))
            delay = RECONNECT_MIN
            try:
                while True:
                    started = time.monotonic()
                    try:
                        await self._link_once(key)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.warning("master link lost: %s", exc)
                    if time.monotonic() - started > RECONNECT_MAX:
                        delay = RECONNECT_MIN
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, RECONNECT_MAX)
            finally:
                spool_task.cancel()
                await self.supervisor.shutdown()
