"""Shared master-side services: settings, audit, ingest, tracks, background loops."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, or_, select

from . import pipelines, schedule, uploaders
from .config import Config
from .engine import MeasureError, Supervisor
from .live import LiveHub
from .models import AuditLog, BroadcastifySystem, Node, Recorder, Recording, SdrProfile, Setting, Source, Target, UploadJob, make_session_factory
from .sdr import enumerate_serials
from .security import RateLimiter, SecretBox

log = logging.getLogger("sentrysdr")
MEASURE_TIMEOUT = 45.0

SETTING_DEFAULTS = {
    "audit_retention_days": "365",
    "recording_retention_days": "90",
    "registration_open": "true",
    "rdio_server": "",
    "clearcut_server": "",
    "clearcut_page_url": "",
    "broadcastify_server": "https://api.broadcastify.com/call-upload",
    "map_tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
}
SECRET_SETTINGS = {"rdio_api_key", "clearcut_api_key"}
TRACK_EXPIRY_SECONDS = 60
UPLOAD_MAX_ATTEMPTS = 12
BROADCASTIFY_RETRY_SECONDS = 10
DUPLICATE_UPLOAD_NOTE = "Skipped: another SDR on this channel already recorded this call."
# ClearCut only lists recent calls, so there's no point looking for much longer.
TRANSCRIPT_WINDOW_SECONDS = 86400
TRANSCRIPT_POLL_SECONDS = 60
TRANSCRIPT_MAX_LOOKUPS = 20
NODE_OFFLINE_SECONDS = 45


def setup_logging(cfg: Config) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("sentrysdr")
    root.setLevel(logging.INFO)
    if root.handlers:
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = logging.handlers.TimedRotatingFileHandler(
        cfg.log_dir / "sentrysdr.log", when="midnight", backupCount=30, utc=True,
    )
    file_handler.setFormatter(formatter)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(stream)


class Context:
    def __init__(self, cfg: Config, enumerate_fn=enumerate_serials):
        self.cfg = cfg
        for path in (cfg.data_dir, cfg.recordings_dir, cfg.log_dir):
            path.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            cfg.data_dir.chmod(0o750)
        self.db = make_session_factory(cfg.data_dir / "sentrysdr.db")
        self.secrets = SecretBox(cfg.data_dir / "secret.key")
        self.limiter = RateLimiter()
        self.enumerate = enumerate_fn
        self.status: dict[tuple[int, str], dict] = {}
        self.tracks: dict[str, dict[str, dict]] = {"adsb": {}, "ais": {}}
        self.live = LiveHub(cfg, self._live_demand)
        self.node_links: dict[int, object] = {}
        self.pending_measures: dict[str, tuple[int, asyncio.Future]] = {}
        self.bans_cache: tuple[float, list] = (0.0, [])
        self._tile_origin: str | None = None
        self.local_node_id = self._ensure_local_node()
        self.supervisor: Supervisor | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.wake: asyncio.Event | None = None
        self.tasks: list[asyncio.Task] = []
        self.http_transport = None  # replaced in tests so nothing reaches a real server
        self.audit_log = logging.getLogger("sentrysdr.audit")

    def _ensure_local_node(self) -> int:
        with self.db() as session:
            node = session.scalar(select(Node).where(Node.is_local))
            if node is None:
                node = Node(name=self.cfg.node_name, is_local=True, status="active")
                session.add(node)
            node.serials = list(self.cfg.serials)
            node.name = self.cfg.node_name
            session.commit()
            return node.id

    # --- settings ---------------------------------------------------------
    def get_setting(self, key: str) -> str:
        with self.db() as session:
            row = session.get(Setting, key)
            return row.value if row else SETTING_DEFAULTS.get(key, "")

    def set_setting(self, key: str, value: str) -> None:
        with self.db() as session:
            row = session.get(Setting, key)
            if row:
                row.value = value
            else:
                session.add(Setting(key=key, value=value))
            session.commit()
        if key == "map_tile_url":
            self._tile_origin = None

    def tile_origin(self) -> str:
        """Origin of the map tile server, cached because every response's CSP uses it."""
        if self._tile_origin is None:
            parts = urlsplit(self.get_setting("map_tile_url").replace("{s}", "a"))
            self._tile_origin = f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""
        return self._tile_origin

    def get_secret(self, key: str) -> str:
        stored = self.get_setting(key)
        return (self.secrets.open(stored) or "") if stored else ""

    # --- audit ------------------------------------------------------------
    def audit(self, action: str, *, user=None, email: str | None = None, ip: str | None = None,
              target: str | None = None, details: dict | None = None) -> None:
        entry = AuditLog(
            user_id=user.id if user else None,
            email=user.email if user else email,
            ip=ip, action=action, target=target, details=details or {},
        )
        with self.db() as session:
            session.add(entry)
            session.commit()
        self.audit_log.info(json.dumps(
            {"action": action, "email": entry.email, "ip": ip, "target": target, "details": details or {}},
            default=str,
        ))

    # --- status helpers ---------------------------------------------------
    def set_status(self, node_id: int, serial: str, status: dict) -> None:
        self.status[(node_id, serial)] = {**status, "updated_at": time.time()}

    def node_online(self, node: Node) -> bool:
        if node.is_local:
            return True
        return node.id in self.node_links

    def live_key(self, node_id: int, serial: str) -> str:
        return f"{node_id}:{serial}"

    def _live_demand(self, key: str, on: bool) -> None:
        node_id, _, serial = key.partition(":")
        link = self.node_links.get(int(node_id))
        if link is not None:
            link.request_live(serial, on)

    def update_tracks(self, kind: str, node_id: int, serial: str, data: dict) -> None:
        self.tracks.setdefault(kind, {})[f"{node_id}:{serial}"] = {"at": time.time(), "data": data}

    def current_tracks(self, kind: str) -> list[dict]:
        """Merge every receiver's view. The freshest report wins for duplicates."""
        field, identity = ("aircraft", "hex") if kind == "adsb" else ("vessels", "mmsi")
        cutoff = time.time() - TRACK_EXPIRY_SECONDS
        merged: dict[str, dict] = {}
        for report in sorted(self.tracks.get(kind, {}).values(), key=lambda r: r["at"]):
            if report["at"] >= cutoff:
                for item in report["data"].get(field, []):
                    if isinstance(item, dict) and item.get(identity) is not None:
                        merged[str(item[identity])] = item
        return list(merged.values())

    # --- desired state ----------------------------------------------------
    def assignments(self, local_now: datetime | None = None) -> dict[int, dict[str, dict]]:
        """Compute {node_id: {serial: spec}} from recorders and the clock."""
        local_now = local_now or datetime.now(self.cfg.tz)
        with self.db() as session:
            recorders = list(session.scalars(select(Recorder)))
            winners = schedule.resolve(recorders, local_now)
            result: dict[int, dict[str, dict]] = {}
            for source_id, recorder in winners.items():
                source = session.get(Source, source_id)
                target = session.get(Target, recorder.target_id)
                if source is None or target is None:
                    continue
                profile = session.scalar(select(SdrProfile).where(
                    SdrProfile.source_id == source.id, SdrProfile.target_id == target.id,
                ))
                try:
                    spec = pipelines.sanitize_spec({
                        "recorder_id": recorder.id, "recorder_name": recorder.name,
                        "source_id": source.id, "source_name": source.name,
                        "target_id": target.id, "target_name": target.name,
                        "kind": target.kind, "frequency_hz": target.frequency_hz,
                        "modulation": target.modulation,
                        "squelch": profile.squelch if profile else target.squelch,
                        "gain_db": profile.gain_db if profile else source.gain_db,
                        "ppm": source.ppm, "volume_db": target.volume_db or 0,
                    })
                except pipelines.SpecError as exc:
                    log.error("recorder %s has an invalid configuration: %s", recorder.id, exc)
                    continue
                result.setdefault(source.node_id, {})[source.serial] = spec
            return result

    async def measure(self, node: Node, serial: str, request: dict) -> dict:
        """Measure on the node that holds the SDR. Raises MeasureError with a user-facing reason."""
        if node.is_local:
            if self.supervisor is None:
                raise MeasureError("Radios aren't running on this server.")
            return await self.supervisor.measure(serial, **request)
        link = self.node_links.get(node.id)
        if link is None:
            raise MeasureError("That node is offline.")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending_measures[request_id] = (node.id, future)
        try:
            link.request_measure(request_id, serial, request)
            return await asyncio.wait_for(future, MEASURE_TIMEOUT)
        except asyncio.TimeoutError:
            raise MeasureError("The node didn't answer. Check that it runs the same version as this server.") from None
        finally:
            self.pending_measures.pop(request_id, None)

    def finish_measure(self, node_id: int, message: dict) -> None:
        pending = self.pending_measures.get(str(message.get("id")))
        if pending is None or pending[0] != node_id or pending[1].done():
            return  # late, unknown, or from a node that wasn't asked
        future = pending[1]
        if not message.get("ok"):
            future.set_exception(MeasureError(str(message.get("error") or "The measurement failed.")[:300]))
            return
        try:
            result = message["result"]
            future.set_result({
                "low": int(result["low"]), "typical": int(result["typical"]), "high": int(result["high"]),
                "suggested": int(result["suggested"]), "moved": bool(result["moved"]),
            })
        except (KeyError, TypeError, ValueError):
            future.set_exception(MeasureError("The node sent an unreadable result."))

    def kick(self) -> None:
        """Ask the scheduler to re-evaluate now. Safe from any thread."""
        if self.loop and self.wake:
            self.loop.call_soon_threadsafe(self.wake.set)

    # --- call ingest ------------------------------------------------------
    def store_call(self, meta: dict, audio: Path, node_name: str, move: bool = True) -> bool:
        """Persist a finished call. Returns False for a duplicate id (safe retry)."""
        call_id = meta["id"]
        day = datetime.fromtimestamp(meta["started_at"], self.cfg.tz).strftime("%Y/%m/%d")
        relative = Path(day) / f"{call_id}.mp3"
        destination = self.cfg.recordings_dir / relative
        with self.db() as session:
            if session.get(Recording, call_id):
                return False
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(audio), destination) if move else shutil.copy(audio, destination)
            recording = Recording(
                id=call_id, node_name=node_name, serial=meta.get("serial", ""),
                source_id=meta.get("source_id"), source_name=meta.get("source_name", ""),
                target_id=meta.get("target_id"), target_name=meta.get("target_name", ""),
                recorder_name=meta.get("recorder_name", ""),
                frequency_hz=meta.get("frequency_hz", 0), modulation=meta.get("modulation", ""),
                started_at=meta["started_at"], ended_at=meta["ended_at"], duration=meta["duration"],
                rel_path=str(relative), size_bytes=destination.stat().st_size,
            )
            session.add(recording)
            target = session.get(Target, meta["target_id"]) if meta.get("target_id") else None
            if target is not None:
                # The recording stays, but a feed should only get one copy of a call.
                duplicate = self._heard_on_another_sdr(session, recording)
                extra = {"status": "done", "last_error": DUPLICATE_UPLOAD_NOTE} if duplicate else {}
                if target.clearcut_enabled and target.clearcut_id is not None:
                    session.add(UploadJob(recording_id=call_id, destination="clearcut", **extra))
                    if not duplicate:
                        recording.clearcut_system = target.clearcut_system
                        recording.clearcut_talkgroup = target.clearcut_id
                if target.rdio_enabled and target.rdio_system is not None and target.rdio_talkgroup is not None:
                    session.add(UploadJob(recording_id=call_id, destination="rdio", **extra))
                if target.broadcastify_enabled and target.broadcastify_system_ref is not None and target.broadcastify_slot is not None:
                    session.add(UploadJob(recording_id=call_id, destination="broadcastify", **extra))
            session.commit()
        return True

    @staticmethod
    def _heard_on_another_sdr(session, recording: Recording) -> bool:
        """True if a different SDR on the same target already has an overlapping call."""
        return session.scalar(select(Recording.id).where(
            Recording.id != recording.id,
            Recording.target_id == recording.target_id,
            Recording.started_at < recording.ended_at,
            Recording.ended_at > recording.started_at,
            or_(Recording.node_name != recording.node_name, Recording.serial != recording.serial),
        ).limit(1)) is not None

    def recording_path(self, recording: Recording) -> Path | None:
        """Resolve a stored path, refusing anything outside the recordings folder."""
        root = self.cfg.recordings_dir.resolve()
        path = (root / recording.rel_path).resolve()
        return path if path.is_relative_to(root) and path.is_file() else None

    # --- background loops -------------------------------------------------
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.wake = asyncio.Event()

        async def call_sink(meta: dict, path: Path) -> None:
            await asyncio.to_thread(self.store_call, meta, path, self.cfg.node_name)

        node_id = self.local_node_id
        self.supervisor = Supervisor(
            self.cfg, call_sink,
            pcm=lambda serial, data, rate: self.live.feed(self.live_key(node_id, serial), data, rate),
            tracks=lambda kind, serial, data: self.update_tracks(kind, node_id, serial, data),
            status=lambda serial, data: self.set_status(node_id, serial, data),
            enumerate_fn=self.enumerate,
        )
        for coroutine in (self._scheduler_loop, self._upload_loop, self._transcript_loop, self._retention_loop):
            self.tasks.append(asyncio.create_task(coroutine(), name=coroutine.__name__))

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.live.shutdown()
        if self.supervisor:
            await self.supervisor.shutdown()

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self.apply_assignments()
            except Exception:
                log.exception("scheduler pass failed")
            try:
                await asyncio.wait_for(self.wake.wait(), 5.0)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def apply_assignments(self) -> None:
        desired = await asyncio.to_thread(self.assignments)
        await self.supervisor.apply(desired.get(self.local_node_id, {}))
        for node_id, link in list(self.node_links.items()):
            await link.send_desired(desired.get(node_id, {}))

    async def _upload_loop(self) -> None:
        async with httpx.AsyncClient(timeout=uploaders.UPLOAD_TIMEOUT, follow_redirects=False) as client:
            while True:
                try:
                    if not await self.process_uploads(client):
                        await asyncio.sleep(5)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("upload pass failed")
                    await asyncio.sleep(30)

    async def process_uploads(self, client: httpx.AsyncClient) -> int:
        with self.db() as session:
            jobs = list(session.scalars(
                select(UploadJob).where(UploadJob.status == "pending", UploadJob.next_attempt_at <= time.time()).limit(10)
            ))
            for job in jobs:
                try:
                    await self._attempt_upload(session, client, job)
                except Exception as exc:
                    # One bad call must never hold up the calls queued behind it.
                    log.exception("upload %s to %s hit an unexpected error", job.recording_id, job.destination)
                    session.rollback()
                    self._retry_later(job, f"Unexpected error: {type(exc).__name__}")
                session.commit()
            return len(jobs)

    def _retry_later(self, job: UploadJob, reason: str) -> None:
        job.attempts += 1
        job.last_error = reason[:400]
        if job.attempts >= UPLOAD_MAX_ATTEMPTS:
            job.status = "failed"
        elif job.destination == "broadcastify":
            # Broadcastify refuses calls that arrive late: 64 seconds was already too long.
            job.next_attempt_at = time.time() + BROADCASTIFY_RETRY_SECONDS
        else:
            job.next_attempt_at = time.time() + min(30 * 2 ** job.attempts, 3600)

    async def _attempt_upload(self, session, client: httpx.AsyncClient, job: UploadJob) -> None:
        recording = session.get(Recording, job.recording_id)
        audio = self.recording_path(recording) if recording else None
        target = session.get(Target, recording.target_id) if recording and recording.target_id else None
        if audio is None or target is None:
            job.status, job.last_error = "failed", "recording or target no longer exists"
            return
        note = ""
        try:
            server = self.get_setting(f"{job.destination}_server")
            if job.destination == "broadcastify":
                system = session.get(BroadcastifySystem, target.broadcastify_system_ref)
                key = self.secrets.open(system.api_key_sealed) if system else ""
                if not server or not key:
                    raise uploaders.UploadError("broadcastify server or system is not configured")
                note = await uploaders.send_broadcastify(self.cfg, client, server, system.system_id, key, recording, target, audio)
            else:
                key = self.get_secret(f"{job.destination}_api_key")
                if not server or not key:
                    raise uploaders.UploadError(f"{job.destination} server or key is not configured")
                if job.destination == "rdio":
                    await uploaders.send_rdio(client, server, key, recording, target, audio)
                else:
                    await uploaders.send_clearcut(self.cfg, client, server, key, recording, target, audio)
        except uploaders.UploadRejected as exc:
            job.attempts += 1
            job.status, job.last_error = "failed", str(exc)[:400]
            log.warning("upload %s to %s rejected: %s", job.recording_id, job.destination, job.last_error)
        except (uploaders.UploadError, httpx.HTTPError, OSError) as exc:
            self._retry_later(job, str(exc) or type(exc).__name__)
            log.warning("upload %s to %s failed: %s", job.recording_id, job.destination, job.last_error)
        else:
            job.status, job.last_error = "done", note

    async def _transcript_loop(self) -> None:
        async with httpx.AsyncClient(timeout=uploaders.UPLOAD_TIMEOUT, follow_redirects=False) as client:
            while True:
                try:
                    await self.sync_transcripts(client)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("transcript pass failed")
                await asyncio.sleep(TRANSCRIPT_POLL_SECONDS)

    async def sync_transcripts(self, client: httpx.AsyncClient) -> int:
        """Copy finished transcripts from ClearCut. One request per talkgroup with calls waiting."""
        server = self.get_setting("clearcut_server")
        if not server:
            return 0
        found = 0
        with self.db() as session:
            waiting = list(session.scalars(
                select(Recording).join(UploadJob, UploadJob.recording_id == Recording.id).where(
                    UploadJob.destination == "clearcut", UploadJob.status == "done",
                    Recording.transcript.is_(None), Recording.clearcut_system.is_not(None),
                    Recording.started_at >= time.time() - TRANSCRIPT_WINDOW_SECONDS,
                )
            ))
            groups: dict[tuple[str, int], list[Recording]] = {}
            for recording in waiting:
                groups.setdefault((recording.clearcut_system, recording.clearcut_talkgroup), []).append(recording)
            for (system, talkgroup), recordings in list(groups.items())[:TRANSCRIPT_MAX_LOOKUPS]:
                try:
                    calls = await uploaders.clearcut_calls(client, server, system, talkgroup)
                except (uploaders.UploadError, httpx.HTTPError) as exc:
                    log.warning("transcript lookup for %s/%s failed: %s", system, talkgroup, exc)
                    continue
                for recording in recordings:
                    call = uploaders.match_call(calls, recording.started_at)
                    if call is None:
                        continue
                    recording.clearcut_call_id = str(call.get("id") or "")[:64] or recording.clearcut_call_id
                    text = uploaders.transcript_text(call)
                    if text:
                        recording.transcript = text
                        found += 1
                session.commit()
        return found

    async def _retention_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.apply_retention)
            except Exception:
                log.exception("retention pass failed")
            await asyncio.sleep(3600)

    def apply_retention(self, now: float | None = None) -> dict:
        now = now or time.time()
        removed = {"recordings": 0, "audit": 0}
        recording_days = int(self.get_setting("recording_retention_days") or 0)
        audit_days = int(self.get_setting("audit_retention_days") or 0)
        with self.db() as session:
            if recording_days > 0:
                cutoff = now - recording_days * 86400
                for recording in session.scalars(
                    select(Recording).where(Recording.started_at < cutoff, Recording.protected.is_(False))
                ):
                    path = self.recording_path(recording)
                    if path:
                        path.unlink(missing_ok=True)
                    session.delete(recording)
                    removed["recordings"] += 1
            if audit_days > 0:
                result = session.execute(delete(AuditLog).where(AuditLog.ts < now - audit_days * 86400))
                removed["audit"] = result.rowcount
                self._expire_audit_files(now - audit_days * 86400)
            session.commit()
        return removed

    def _expire_audit_files(self, cutoff: float) -> None:
        for path in self.cfg.log_dir.glob("audit.log.*"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)


def setup_audit_file(cfg: Config) -> None:
    """A separate append-only audit file in the sysadmin's chosen log folder."""
    audit = logging.getLogger("sentrysdr.audit")
    if audit.handlers:
        return
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.TimedRotatingFileHandler(cfg.log_dir / "audit.log", when="midnight", utc=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    audit.addHandler(handler)
    audit.propagate = False
    with contextlib.suppress(OSError):
        os.chmod(cfg.log_dir / "audit.log", 0o640)
