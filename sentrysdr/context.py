"""Shared master-side services: settings, audit, ingest, tracks, background loops."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import signal
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, func, or_, select

from . import aio
from . import BUILD_ID, __version__, acars, capabilities, coverage, emergencies, health, notify, pipelines, schedule, uploaders
from .config import Config
from .engine import UNPLUGGED, MeasureError, Supervisor
from .live import LiveHub
from .models import AcarsMessage, AuditLog, TrackEmergency, BroadcastifySystem, Node, Recorder, Recording, SdrProfile, Setting, Source, Target, UploadJob, UserSession, make_session_factory
from .sdr import enumerate_serials
from .security import RateLimiter, SecretBox

log = logging.getLogger("sentrysdr")
MEASURE_TIMEOUT = 45.0
RESTART_DELAY_SECONDS = 1.0  # long enough for the reply to reach the browser first

SETTING_DEFAULTS = {
    "audit_retention_days": "365",
    "recording_retention_days": "90",
    "registration_open": "true",
    "rdio_server": "",
    "clearcut_server": "",
    "clearcut_page_url": "",
    "broadcastify_server": "https://api.broadcastify.com/call-upload",
    "map_tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "message_retention_days": "30",
    "recording_retention_gb": "0",
    "airframes_station_id": "",
    "airframes_feed": acars.AIRFRAMES_DEFAULT,
    "email_alerts_enabled": "false",
    "email_daily_cap": "50",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_security": "starttls",
    "smtp_username": "",
    "smtp_from": "",
}
SECRET_SETTINGS = {"rdio_api_key", "clearcut_api_key", "smtp_password"}
EMAIL_SECONDS = 30
ACKNOWLEDGEABLE = {"uploads.failed"}  # warnings about things that happened, rather than a state that clears itself
EMAIL_RETRY_SECONDS = 300
TRACK_EXPIRY_SECONDS = 60
MAX_TRACK_ITEMS = 3000
# Per SDR, one-minute window. Busy ACARS sites reach a few messages a minute; this is far above that.
ACARS_LIMIT_PER_MINUTE = 600
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
_UNSAFE_TEXT = re.compile(r"[^\x20-\x7e]|[<>&\"'`\\]")
_CONTROL_TEXT = re.compile(r"[\x00-\x1f\x7f]")


def clean_track_value(value, depth: int = 0, strict: bool = True):
    """Aircraft and ship data can come from a slave. Aircraft are also shown by tar1090,
    a third-party page, so their text keeps only plain characters. Ship names may have
    apostrophes and only appear on SentrySDR's own map, which never parses them as HTML."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) and abs(value) < 1e12 else None
    if isinstance(value, str):
        return (_UNSAFE_TEXT if strict else _CONTROL_TEXT).sub("", value)[:64]
    if depth < 2 and isinstance(value, list):
        return [clean_track_value(v, depth + 1, strict) for v in value[:32]]
    if depth < 2 and isinstance(value, dict):
        return {k: clean_track_value(v, depth + 1, strict) for k, v in list(value.items())[:64]
                if isinstance(k, str) and _FIELD_RE.match(k)}
    return None


def clean_track_items(items, strict: bool = True) -> list[dict]:
    if not isinstance(items, list):
        return []
    return [clean_track_value(item, 0, strict) for item in items[:MAX_TRACK_ITEMS] if isinstance(item, dict)]
UPLOAD_MAX_ATTEMPTS = 12
BROADCASTIFY_RETRY_SECONDS = 10
DUPLICATE_UPLOAD_NOTE = "Skipped: another SDR on this channel already recorded this call."
# ClearCut only lists recent calls, so there's no point looking for much longer.
TRANSCRIPT_WINDOW_SECONDS = 86400
TRANSCRIPT_POLL_SECONDS = 60
TRANSCRIPT_MAX_LOOKUPS = 20
NODE_OFFLINE_SECONDS = 45
NODE_OFFLINE_WARN_SECONDS = 300
CLOCK_SKEW_WARN_SECONDS = 5
DISK_WARN_BYTES = 2_000_000_000
DISK_WARN_FRACTION = 0.05


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
        self.node_health: dict[int, dict] = {}  # each slave's latest clock and disk report
        self.trails = coverage.Trails()
        self._coverage: dict | None = None  # {source id: {"home": [lat, lon], "sectors": [...]}}, saved now and then
        self._coverage_saved = 0.0
        self._receivers: tuple[float, dict] = (0.0, {})  # (when read, {(node, serial): (source id, lat, lon)})
        self.tracks: dict[str, dict[str, dict]] = {"adsb": {}, "ais": {}}
        self.live = LiveHub(cfg, self._live_demand)
        self.node_links: dict[int, object] = {}
        self.acars_duplicates = acars.Duplicates()
        self.airframes = acars.AirframesFeed()
        self.pending_measures: dict[str, tuple[int, asyncio.Future, str]] = {}
        self.bans_cache: tuple[float, list] = (0.0, [])
        self._tile_origin: str | None = None
        self.local_node_id = self._ensure_local_node()
        self.supervisor: Supervisor | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.wake: asyncio.Event | None = None
        self.tasks: list[asyncio.Task] = []
        self.emergencies = emergencies.Emergencies(self.db)
        self.email_error = ""  # the last alert email failure, until one succeeds
        self.email_retry_at = 0.0
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

    def warning_acknowledged(self, key: str) -> float:
        try:
            return float(json.loads(self.get_setting("warning_acks") or "{}").get(key) or 0)
        except (ValueError, AttributeError):
            return 0.0

    def acknowledge_warning(self, key: str, at: float) -> None:
        try:
            acks = json.loads(self.get_setting("warning_acks") or "{}")
        except ValueError:
            acks = {}
        acks = acks if isinstance(acks, dict) else {}
        acks[key] = at
        self.set_setting("warning_acks", json.dumps(acks))

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
        if kind == "acars":
            key = f"acars:{node_id}:{serial}"
            if self.limiter.blocked(key, ACARS_LIMIT_PER_MINUTE, 60):
                return  # a flood, whatever its cause, mustn't fill the database
            self.limiter.hit(key, 60)
            self.ingest_acars(node_id, serial, data)
            return
        field = "aircraft" if kind == "adsb" else "vessels"
        data = {field: clean_track_items(data.get(field), strict=kind == "adsb")}
        if kind == "adsb":
            self.trails.add(data["aircraft"])
            self._widen_coverage(node_id, serial, data["aircraft"])
        self.emergencies.check(kind, node_id, serial, data[field])
        self.tracks.setdefault(kind, {})[f"{node_id}:{serial}"] = {"at": time.time(), "data": data}

    def ingest_acars(self, node_id: int, serial: str, data: dict) -> None:
        """Store one decoded ACARS message, and feed it on if its target says so."""
        raw = data.get("message")
        if not isinstance(raw, dict):
            return
        message = acars.clean(raw)
        if not self.acars_duplicates.first(message):
            return  # another SDR on this channel got it first
        try:
            target_id = int(data.get("target_id"))
        except (TypeError, ValueError):
            target_id = None
        with self.db() as session:
            node = session.get(Node, node_id)
            source = session.scalar(select(Source).where(Source.node_id == node_id, Source.serial == serial))
            target = session.get(Target, target_id) if target_id else None
            if target is not None and target.kind != "acars":
                target = None
            station = self.get_setting("airframes_station_id")
            feed = bool(target and target.airframes_enabled and station)
            session.add(AcarsMessage(
                received_at=time.time(), node_name=node.name if node else "", serial=serial[:64],
                source_name=source.name if source else "", target_id=target.id if target else None,
                target_name=target.name if target else "", fed=feed or None, **message,
            ))
            session.commit()
        if feed and self.loop is not None:
            payload = acars.feed_payload(raw, station)
            task = self.loop.create_task(self.airframes.send(self.get_setting("airframes_feed"), payload))
            self.tasks.append(task)
            task.add_done_callback(lambda done: done in self.tasks and self.tasks.remove(done))

    # --- receiver locations and coverage ------------------------------------
    def receiver_locations(self) -> dict:
        """(node id, serial) -> (source id, lat, lon) for SDRs with a location. Read at most once a minute."""
        read_at, cached = self._receivers
        if time.monotonic() - read_at < 60:
            return cached
        with self.db() as session:
            cached = {(s.node_id, s.serial): (s.id, s.lat, s.lon) for s in session.scalars(select(Source))
                      if s.lat is not None and s.lon is not None}
        self._receivers = (time.monotonic(), cached)
        return cached

    def coverage(self) -> dict:
        if self._coverage is None:
            try:
                saved = json.loads(self.get_setting("coverage") or "{}")
            except ValueError:
                saved = {}
            self._coverage = saved if isinstance(saved, dict) else {}
        return self._coverage

    def _widen_coverage(self, node_id: int, serial: str, aircraft: list[dict]) -> None:
        found = self.receiver_locations().get((node_id, serial))
        if not found:
            return
        source_id, lat, lon = found
        record = self.coverage().get(str(source_id))
        if not record or record.get("home") != [lat, lon]:
            # A new or moved receiver starts again: its old reach was from somewhere else.
            record = self.coverage()[str(source_id)] = {"home": [lat, lon], "sectors": [0.0] * coverage.SECTORS}
        if coverage.update_sectors(record["sectors"], (lat, lon), aircraft) and time.time() - self._coverage_saved > 600:
            self.save_coverage()

    def save_coverage(self) -> None:
        self._coverage_saved = time.time()
        self.set_setting("coverage", json.dumps(self.coverage()))

    def forget_receiver(self, source_id: int) -> None:
        self.coverage().pop(str(source_id), None)
        self._receivers = (0.0, {})
        self.save_coverage()

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
                        "channels": target.acars_channels or [] if target.kind == "acars" else [],
                        "feeds": target.feeds or {} if target.kind in ("adsb", "ais") else {},
                        "feeder_uuid": source.feeder_uuid,
                        "rtl_tcp_port": source.rtl_tcp_port if target.kind == "rtltcp" else None,
                    })
                except pipelines.SpecError as exc:
                    log.error("recorder %s has an invalid configuration: %s", recorder.id, exc)
                    continue
                result.setdefault(source.node_id, {})[source.serial] = spec
            return result

    async def restart_sdr(self, node: Node, serial: str) -> None:
        if node.is_local:
            if self.supervisor is None:
                raise MeasureError("Radios aren't running on this server.")
            await self.supervisor.restart(serial)
            return
        link = self.node_links.get(node.id)
        if link is None:
            raise MeasureError("That node is offline.")
        link.send({"t": "restart", "serial": serial})

    def restart_service(self, node: Node) -> None:
        """Ask a node's service to stop cleanly. Under systemd (Restart=always) it starts again by itself."""
        if node.is_local:
            if self.loop is None:
                raise MeasureError("The service isn't running its background tasks.")
            self.loop.call_later(RESTART_DELAY_SECONDS, os.kill, os.getpid(), signal.SIGTERM)
            return
        link = self.node_links.get(node.id)
        if link is None:
            raise MeasureError("That node is offline.")
        link.send({"t": "restart_service"})

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
        self.pending_measures[request_id] = (node.id, future, request.get("op", "measure"))
        try:
            link.request_measure(request_id, serial, request)
            return await aio.wait_for(future, MEASURE_TIMEOUT)
        except asyncio.TimeoutError:
            raise MeasureError("The node didn't answer. Check that it runs the same version as this server.") from None
        finally:
            self.pending_measures.pop(request_id, None)

    def finish_measure(self, node_id: int, message: dict) -> None:
        pending = self.pending_measures.get(str(message.get("id")))
        if pending is None or pending[0] != node_id or pending[1].done():
            return  # late, unknown, or from a node that wasn't asked
        future, op = pending[1], pending[2]
        if not message.get("ok"):
            future.set_exception(MeasureError(str(message.get("error") or "The measurement failed.")[:300]))
            return
        try:
            result = message["result"]
            if op == "detect":
                future.set_result(capabilities.clean_detected(result))
                return
            future.set_result({
                "low": int(result["low"]), "typical": int(result["typical"]), "high": int(result["high"]),
                "suggested": int(result["suggested"]), "moved": bool(result["moved"]),
            })
        except (AttributeError, KeyError, TypeError, ValueError):
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
        for coroutine in (self._scheduler_loop, self._upload_loop, self._transcript_loop, self._retention_loop, self._email_loop):
            self.tasks.append(asyncio.create_task(coroutine(), name=coroutine.__name__))

    async def stop(self) -> None:
        if self._coverage:
            self.save_coverage()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.live.shutdown()
        if self.supervisor:
            await self.supervisor.shutdown()

    async def _email_loop(self) -> None:
        """Alert emails. A mail server that fails is left alone for a few minutes, and an admin is warned."""
        while True:
            await asyncio.sleep(EMAIL_SECONDS)
            try:
                await asyncio.to_thread(self.send_alert_emails)
            except Exception:
                log.exception("alert email pass failed")

    def send_alert_emails(self, send=None) -> int:
        if time.time() < self.email_retry_at:
            return 0
        failures: list[str] = []

        def deliver(settings, message):
            try:
                (send or notify.deliver)(settings, message)
            except notify.EmailError as exc:
                failures.append(str(exc))
                raise

        sent = notify.send_due(self, deliver)
        if failures:
            self.email_error, self.email_retry_at = failures[-1], time.time() + EMAIL_RETRY_SECONDS
        elif sent:
            self.email_error = ""
        return sent

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self.apply_assignments()
            except Exception:
                log.exception("scheduler pass failed")
            try:
                await aio.wait_for(self.wake.wait(), 5.0)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def apply_assignments(self) -> None:
        await asyncio.to_thread(self.track_schedule_ends)
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
            job.status, job.failed_at = "failed", time.time()
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
            job.status, job.last_error, job.failed_at = "failed", "recording or target no longer exists", time.time()
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
            job.status, job.last_error, job.failed_at = "failed", str(exc)[:400], time.time()
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
                        recording.transcript_at = time.time()
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
        removed = {"recordings": 0, "audit": 0, "messages": 0, "over_size": 0}
        message_days = int(self.get_setting("message_retention_days") or 0)
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
            removed["sessions"] = session.execute(delete(UserSession).where(UserSession.expires_at < now)).rowcount
            limit_gb = float(self.get_setting("recording_retention_gb") or 0)
            if limit_gb > 0:
                removed["over_size"] = self._trim_to_size(session, int(limit_gb * 1e9))
            if message_days > 0:
                result = session.execute(delete(AcarsMessage).where(AcarsMessage.received_at < now - message_days * 86400))
                removed["messages"] = result.rowcount
                result = session.execute(delete(TrackEmergency).where(TrackEmergency.first_seen < now - message_days * 86400))
                removed["emergencies"] = result.rowcount
            if audit_days > 0:
                result = session.execute(delete(AuditLog).where(AuditLog.ts < now - audit_days * 86400))
                removed["audit"] = result.rowcount
                self._expire_audit_files(now - audit_days * 86400)
            session.commit()
        return removed

    def _trim_to_size(self, session, limit: int) -> int:
        """Delete the oldest unprotected recordings until all of them fit in limit bytes."""
        total = session.scalar(select(func.coalesce(func.sum(Recording.size_bytes), 0)))
        removed = 0
        while total > limit:
            batch = list(session.scalars(select(Recording).where(Recording.protected.is_(False))
                                         .order_by(Recording.started_at).limit(200)))
            if not batch:
                break  # only protected recordings are left
            for recording in batch:
                if total <= limit:
                    break
                path = self.recording_path(recording)
                if path:
                    path.unlink(missing_ok=True)
                total -= recording.size_bytes or 0
                session.delete(recording)
                removed += 1
            session.flush()
        return removed

    def recordings_size(self) -> int:
        with self.db() as session:
            return session.scalar(select(func.coalesce(func.sum(Recording.size_bytes), 0)))

    def track_schedule_ends(self, local_now: datetime | None = None) -> list[str]:
        """Note when schedules take over and let go. One set to keep its target becomes the base when it ends.

        active_since is stored, so a schedule that ends while the service is down is still handled.
        Returns what was kept, for tests and the audit log.
        """
        local_now = local_now or datetime.now(self.cfg.tz)
        kept, events = [], []
        with self.db() as session:
            recorders = list(session.scalars(select(Recorder)))
            winners = {r.id for r in schedule.resolve(recorders, local_now).values()}
            for rec in recorders:
                if rec.schedule_days is None:
                    continue
                if rec.id in winners:
                    rec.active_since = rec.active_since or local_now.timestamp()
                    continue
                if rec.active_since is None:
                    continue
                ran_out = not schedule.window_active(rec, local_now)  # not turned off or overtaken early
                rec.active_since = None
                if ran_out and rec.on_end == "keep":
                    kept.append(self._keep_schedule_target(session, rec, events))
            session.commit()
        for action, target, details in events:  # after the commit: audit() writes through its own connection
            self.audit(action, target=target, details=details)
        return [k for k in kept if k]

    def _keep_schedule_target(self, session, rec: Recorder, events: list) -> str | None:
        source, target = session.get(Source, rec.source_id), session.get(Target, rec.target_id)
        if source is None or target is None:
            return None
        base = next((r for r in session.scalars(select(Recorder).where(Recorder.source_id == rec.source_id))
                     if r.schedule_days is None), None)
        if base is not None and base.locked:
            events.append(("schedule.keep_skipped", rec.name, {"reason": "the assignment is locked"}))
            return None
        label = f"{source.name}: {target.name}"
        if base is None:
            session.add(Recorder(name=label, source_id=source.id, target_id=target.id,
                                 assigned_by=f"schedule: {rec.name}"[:254], assigned_at=time.time()))
        else:
            base.target_id, base.name, base.assigned_by, base.assigned_at = target.id, label, f"schedule: {rec.name}"[:254], time.time()
        events.append(("schedule.kept", rec.name, {"source": source.name, "target": target.name}))
        return label

    # --- health ------------------------------------------------------------
    def health_warnings(self) -> list[dict]:
        """Problems an admin or operator should know about, muted or not.

        Each has a stable key (what it's about, so a mute follows it), a level, text and a page to look at.
        quiet says why nobody is warned, when a node or source is set not to warn while it's offline.
        """
        warnings: list[dict] = []
        now_ = time.time()

        def add(key: str, level: str, text: str, page: str, quiet: str | None = None) -> None:
            warnings.append({"key": key, "level": level, "text": text, "page": page, "quiet": quiet,
                             "acknowledge": key in ACKNOWLEDGEABLE})

        with self.db() as session:
            for node in session.scalars(select(Node).where(Node.status == "active")):
                online = self.node_online(node)
                if not node.is_local and not online and (now_ - (node.last_seen_at or 0)) > NODE_OFFLINE_WARN_SECONDS:
                    since = f" since {datetime.fromtimestamp(node.last_seen_at, self.cfg.tz):%Y-%m-%d %H:%M}" if node.last_seen_at else ""
                    add(f"node.offline:{node.id}", "bad", f"{node.name} is offline{since}.", "#/admin",
                        "Set not to warn when it's offline, under Nodes." if node.quiet_offline else None)
                if not node.is_local and online and node.build != BUILD_ID:
                    add(f"node.version:{node.id}", "warn", f"{node.name} runs a different version ({node.version or 'unknown'}) from this server "
                                f"({__version__}). Upgrade it so measuring, detecting and restarts work.", "#/admin")
                report = {"clock_synced": health.clock_synced(), "disk": health.disk(self.cfg.recordings_dir), "skew": 0} \
                    if node.is_local else (self.node_health.get(node.id) if online else None)
                if not report:
                    continue
                if report.get("clock_synced") is False:
                    add(f"node.clock:{node.id}", "warn", f"{node.name}'s clock isn't synced. Broadcastify rejects calls with wrong times; check timedatectl.", "#/admin")
                elif report.get("skew") is not None and abs(report["skew"]) > CLOCK_SKEW_WARN_SECONDS:
                    add(f"node.clock:{node.id}", "warn", f"{node.name}'s clock is {abs(report['skew']):.0f} seconds off this server's.", "#/admin")
                disk = report.get("disk")
                if disk and (disk["free"] < DISK_WARN_BYTES or disk["free"] < disk["total"] * DISK_WARN_FRACTION):
                    add(f"node.disk:{node.id}", "bad", f"{node.name} has {disk['free'] / 1e9:.1f} GB free of {disk['total'] / 1e9:.0f} GB."
                               f"{' Set a size limit for recordings under Admin > Settings.' if node.is_local else ''}", "#/admin")
            winners = schedule.resolve(list(session.scalars(select(Recorder))), datetime.now(self.cfg.tz))
            for source in session.scalars(select(Source).order_by(Source.name)):
                node = session.get(Node, source.node_id)
                status = self.status.get((source.node_id, source.serial), {})
                if source.id in winners and node and self.node_online(node) and status.get("state") == "error":
                    error = status.get("error") or "fault"
                    quiet = "Set not to warn while its stick is unplugged, under SDR sources." \
                        if source.quiet_offline and error in UNPLUGGED else None
                    add(f"source.fault:{source.id}", "bad", f"{source.name}: {error}", "#/sources", quiet)
            # Failed uploads can't be fixed from here, so they can be acknowledged: only later failures warn again.
            acked = self.warning_acknowledged("uploads.failed")
            failures = dict(session.execute(
                select(UploadJob.destination, func.count()).join(Recording).where(
                    UploadJob.status == "failed", Recording.started_at > now_ - 86400,
                    func.coalesce(UploadJob.failed_at, Recording.ended_at) > acked)
                .group_by(UploadJob.destination)).all())
            failed = sum(failures.values())
            if failed:
                names = {"broadcastify": "Broadcastify", "rdio": "RDIO", "clearcut": "ClearCut"}
                where = ", ".join(f"{n} to {names.get(d, d)}" for d, n in sorted(failures.items()))
                since = f"since it was acknowledged at {datetime.fromtimestamp(acked, self.cfg.tz):%H:%M}" if acked else "in the last day"
                add("uploads.failed", "warn", f"{failed} upload{'s' if failed != 1 else ''} failed {since} ({where}). "
                                              "The reason shows on each recording.", "#/recordings")
            if self.email_error and self.get_setting("email_alerts_enabled") == "true":
                add("email.failing", "warn", f"Alert emails aren't being sent. {self.email_error}", "#/admin?tab=email")
            limit_gb = float(self.get_setting("recording_retention_gb") or 0)
            if limit_gb > 0:
                protected = session.scalar(select(func.coalesce(func.sum(Recording.size_bytes), 0)).where(Recording.protected.is_(True)))
                if protected > limit_gb * 1e9:
                    add("recordings.limit", "warn", f"Protected recordings alone ({protected / 1e9:.1f} GB) exceed the {limit_gb:g} GB limit, "
                                "so nothing more can be removed to stay under it.", "#/recordings")
        return warnings

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
