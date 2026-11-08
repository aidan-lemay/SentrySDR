"""Database schema. Timestamps are UTC epoch seconds."""
from __future__ import annotations

import json
import time
from pathlib import Path

from sqlalchemy import JSON, ForeignKey, Index, String, create_engine, event, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


def now() -> float:
    return time.time()


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|active|denied|disabled
    role: Mapped[str] = mapped_column(String(16), default="listener")  # admin|operator|listener
    perm_overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    must_change_password: Mapped[bool] = mapped_column(default=False)
    totp_secret_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(default=False)
    totp_last_step: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[float] = mapped_column(default=now)
    last_login_at: Mapped[float | None] = mapped_column(nullable=True)
    last_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signup_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class UserSession(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[float] = mapped_column(default=now)
    expires_at: Mapped[float] = mapped_column()
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Ban(Base):
    __tablename__ = "bans"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))  # email|ip
    value: Mapped[str] = mapped_column(String(254))
    reason: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(254), default="")
    created_at: Mapped[float] = mapped_column(default=now)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(default=now, index=True)
    user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Node(Base):
    """A SentrySDR instance. The master has one local node; slaves enrol."""
    __tablename__ = "nodes"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    is_local: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|active|revoked
    key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    enroll_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    enroll_expires_at: Mapped[float | None] = mapped_column(nullable=True)
    serials: Mapped[list] = mapped_column(JSON, default=list)
    last_seen_at: Mapped[float | None] = mapped_column(nullable=True)
    last_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(default=now)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    serial: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(500), default="")
    # Retired: these became antennas. Kept empty so older databases still match.
    antenna: Mapped[str] = mapped_column(String(120), default="")
    freq_min_hz: Mapped[int | None] = mapped_column(nullable=True)
    freq_max_hz: Mapped[int | None] = mapped_column(nullable=True)
    gain_db: Mapped[float | None] = mapped_column(nullable=True)  # None = tuner AGC
    ppm: Mapped[int] = mapped_column(default=0)
    antenna_id: Mapped[int | None] = mapped_column(nullable=True)  # Antenna.id, cleared when it's removed
    # What the stick can do. Unset values aren't checked. Not freq_min_hz/freq_max_hz,
    # which are retired: filling those would turn them into an antenna on the next start.
    tuner: Mapped[str | None] = mapped_column(String(40), nullable=True)
    gain_steps: Mapped[list | None] = mapped_column(JSON, nullable=True)
    cap_min_hz: Mapped[int | None] = mapped_column(nullable=True)
    cap_max_hz: Mapped[int | None] = mapped_column(nullable=True)
    uses: Mapped[list | None] = mapped_column(JSON, nullable=True)
    feeder_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)  # identifies this receiver to aggregators
    __table_args__ = (Index("uq_source_node_serial", "node_id", "serial", unique=True),)


ANTENNA_UNITS = ("in", "ft", "cm", "m")


class Antenna(Base):
    """A physical antenna. Several SDRs can share one through a splitter."""
    __tablename__ = "antennas"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(60), default="")  # discone, dipole...
    length: Mapped[float | None] = mapped_column(nullable=True)
    length_unit: Mapped[str] = mapped_column(String(4), default="in")
    notes: Mapped[str] = mapped_column(String(500), default="")
    ranges: Mapped[list] = mapped_column(JSON, default=list)  # [[low_hz, high_hz], ...], sorted


class Target(Base):
    __tablename__ = "targets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(8), default="audio")  # audio|adsb|ais
    frequency_hz: Mapped[int] = mapped_column()
    modulation: Mapped[str] = mapped_column(String(12), default="nfm")
    squelch: Mapped[int] = mapped_column(default=50)  # 0 = continuous recording
    volume_db: Mapped[float | None] = mapped_column(nullable=True)  # applied to the audio
    clearcut_enabled: Mapped[bool] = mapped_column(default=False)
    clearcut_system: Mapped[str | None] = mapped_column(String(64), nullable=True)
    clearcut_id: Mapped[int | None] = mapped_column(nullable=True)  # talkgroup number
    rdio_enabled: Mapped[bool] = mapped_column(default=False)
    rdio_system: Mapped[int | None] = mapped_column(nullable=True)
    rdio_talkgroup: Mapped[int | None] = mapped_column(nullable=True)
    broadcastify_enabled: Mapped[bool | None] = mapped_column(nullable=True)
    broadcastify_system_ref: Mapped[int | None] = mapped_column(nullable=True)  # BroadcastifySystem.id
    broadcastify_slot: Mapped[int | None] = mapped_column(nullable=True)  # the node's frequency slot
    acars_channels: Mapped[list | None] = mapped_column(JSON, nullable=True)  # Hz; frequency_hz is the lowest
    airframes_enabled: Mapped[bool | None] = mapped_column(nullable=True)  # feed messages to airframes.io
    feeds: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # ADS-B and AIS sharing, see feeds.clean


class BroadcastifySystem(Base):
    """One Broadcastify Calls node. Its API key is sealed and never leaves the server."""
    __tablename__ = "broadcastify_systems"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    system_id: Mapped[int] = mapped_column()
    api_key_sealed: Mapped[str] = mapped_column(String(2000))


class SdrProfile(Base):
    """The squelch and gain measured for one stick on one target.

    Noise depends on the stick, the frequency and the gain, so the two values are only
    meaningful together. A profile replaces the target's squelch and the source's gain.
    """
    __tablename__ = "sdr_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), index=True)
    squelch: Mapped[int] = mapped_column()
    gain_db: Mapped[float | None] = mapped_column(nullable=True)  # None = tuner AGC
    noise_level: Mapped[float | None] = mapped_column(nullable=True)
    measured_at: Mapped[float | None] = mapped_column(nullable=True)
    updated_by: Mapped[str] = mapped_column(String(320), default="")
    __table_args__ = (Index("uq_profile_source_target", "source_id", "target_id", unique=True),)


class Recorder(Base):
    """Pairs a target with a source. No schedule = the source's base assignment."""
    __tablename__ = "recorders"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), index=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    schedule_days: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 0=Mon..6=Sun
    schedule_start: Mapped[str | None] = mapped_column(String(5), nullable=True)  # HH:MM
    schedule_end: Mapped[str | None] = mapped_column(String(5), nullable=True)
    locked: Mapped[bool | None] = mapped_column(nullable=True)  # only admins may change it
    assigned_by: Mapped[str] = mapped_column(String(254), default="")
    assigned_at: Mapped[float] = mapped_column(default=now)


class Recording(Base):
    """A call. Names are snapshots, so it outlives its source/target/recorder."""
    __tablename__ = "recordings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    node_name: Mapped[str] = mapped_column(String(120), default="")
    serial: Mapped[str] = mapped_column(String(64), default="")
    source_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    source_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    target_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    target_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    recorder_name: Mapped[str] = mapped_column(String(120), default="")
    frequency_hz: Mapped[int] = mapped_column(default=0, index=True)
    modulation: Mapped[str] = mapped_column(String(12), default="")
    started_at: Mapped[float] = mapped_column(index=True)
    ended_at: Mapped[float] = mapped_column()
    duration: Mapped[float] = mapped_column()
    rel_path: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(default=0)
    protected: Mapped[bool] = mapped_column(default=False, index=True)
    transcript: Mapped[str | None] = mapped_column(nullable=True)
    # Snapshots, so transcripts can still be found after the target is deleted.
    clearcut_system: Mapped[str | None] = mapped_column(String(64), nullable=True)
    clearcut_talkgroup: Mapped[int | None] = mapped_column(nullable=True)
    clearcut_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AcarsMessage(Base):
    """One decoded ACARS message. Copies of the names are kept, like recordings."""
    __tablename__ = "acars_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    received_at: Mapped[float] = mapped_column(index=True)
    node_name: Mapped[str] = mapped_column(String(120), default="")
    serial: Mapped[str] = mapped_column(String(64), default="")
    source_name: Mapped[str] = mapped_column(String(120), default="")
    target_id: Mapped[int | None] = mapped_column(nullable=True)
    target_name: Mapped[str] = mapped_column(String(120), default="")
    frequency_hz: Mapped[int] = mapped_column(default=0)
    level: Mapped[float | None] = mapped_column(nullable=True)
    mode: Mapped[str] = mapped_column(String(4), default="")
    label: Mapped[str] = mapped_column(String(4), default="", index=True)
    block_id: Mapped[str] = mapped_column(String(4), default="")
    msgno: Mapped[str] = mapped_column(String(8), default="")
    tail: Mapped[str] = mapped_column(String(16), default="", index=True)  # without the leading dot
    flight: Mapped[str] = mapped_column(String(16), default="", index=True)
    text: Mapped[str] = mapped_column(String(4000), default="")
    has_text: Mapped[bool] = mapped_column(default=False, index=True)
    fed: Mapped[bool | None] = mapped_column(nullable=True)  # sent to airframes.io


class UploadJob(Base):
    __tablename__ = "upload_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"), index=True)
    destination: Mapped[str] = mapped_column(String(16))  # clearcut|rdio
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending|done|failed
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[float] = mapped_column(default=now)
    last_error: Mapped[str] = mapped_column(String(500), default="")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(2000))


def _add_missing_columns(engine) -> None:
    """Add columns new in this version. Only nullable columns may be added later."""
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in existing:
                    kind = column.type.compile(dialect=engine.dialect)
                    connection.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {kind}'))


def _retire_lock_passwords(engine) -> None:
    """Locks used to have passwords. Keep what's locked, but don't keep the hashes."""
    if "lock_hash" not in {column["name"] for column in inspect(engine).get_columns("recorders")}:
        return
    with engine.begin() as connection:
        connection.execute(text("UPDATE recorders SET locked = 1 WHERE lock_hash IS NOT NULL"))
        connection.execute(text("UPDATE recorders SET lock_hash = NULL"))
    try:
        with engine.begin() as connection:
            connection.execute(text('ALTER TABLE recorders DROP COLUMN lock_hash'))
    except OperationalError:
        pass  # an older SQLite: the column stays, empty and unused


def _convert_source_antennas(engine) -> None:
    """Sources used to hold an antenna type and one range. Turn those into antennas."""
    from .pipelines import FREQ_MAX_HZ
    with engine.begin() as connection:
        rows = connection.execute(text(
            "SELECT id, name, antenna, freq_min_hz, freq_max_hz FROM sources WHERE antenna_id IS NULL "
            "AND (COALESCE(antenna, '') != '' OR freq_min_hz IS NOT NULL OR freq_max_hz IS NOT NULL)"
        )).all()
        made: dict[tuple, int] = {}
        for source_id, source_name, kind, low, high in rows:
            kind = (kind or "").strip()[:60]
            ranges = [[low or 0, high or FREQ_MAX_HZ]] if (low or high) else []
            key = (kind, low, high)
            # Sticks described the same way most likely share one antenna, but only when it has a type.
            antenna_id = made.get(key) if kind else None
            if antenna_id is None:
                antenna_id = connection.execute(Antenna.__table__.insert().values(
                    name=(kind or f"{source_name} antenna")[:120], kind=kind, length=None,
                    length_unit="in", notes="", ranges=ranges,
                )).inserted_primary_key[0]
                made[key] = antenna_id
            connection.execute(
                text("UPDATE sources SET antenna_id = :a, antenna = '', freq_min_hz = NULL, freq_max_hz = NULL WHERE id = :s"),
                {"a": antenna_id, "s": source_id},
            )


def _drop_retired_uses(engine) -> None:
    """Some stick uses were removed from the app. Take them off sticks that had them."""
    from .capabilities import RETIRED_USES
    with engine.begin() as connection:
        for source_id, uses in connection.execute(text("SELECT id, uses FROM sources WHERE uses IS NOT NULL")).all():
            current = json.loads(uses) if isinstance(uses, str) else uses
            if isinstance(current, list) and set(current) & RETIRED_USES:
                kept = [u for u in current if u not in RETIRED_USES]
                connection.execute(text("UPDATE sources SET uses = :u WHERE id = :s"), {"u": json.dumps(kept), "s": source_id})


def _fill_feeder_uuids(engine) -> None:
    """Each receiver gets a stable random ID, which aggregators use to tell feeders apart."""
    import uuid
    with engine.begin() as connection:
        for (source_id,) in connection.execute(text("SELECT id FROM sources WHERE feeder_uuid IS NULL")).all():
            connection.execute(text("UPDATE sources SET feeder_uuid = :u WHERE id = :s"), {"u": str(uuid.uuid4()), "s": source_id})


def make_session_factory(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _record):
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    _retire_lock_passwords(engine)
    _convert_source_antennas(engine)
    _drop_retired_uses(engine)
    _fill_feeder_uuids(engine)
    try:
        db_path.chmod(0o600)
    except OSError:
        pass
    return sessionmaker(engine, expire_on_commit=False)
