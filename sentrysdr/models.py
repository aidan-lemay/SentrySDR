"""Database schema. Timestamps are UTC epoch seconds."""
from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import JSON, ForeignKey, Index, String, create_engine, event, inspect, text
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
    antenna: Mapped[str] = mapped_column(String(120), default="")
    freq_min_hz: Mapped[int | None] = mapped_column(nullable=True)
    freq_max_hz: Mapped[int | None] = mapped_column(nullable=True)
    gain_db: Mapped[float | None] = mapped_column(nullable=True)  # None = tuner AGC
    ppm: Mapped[int] = mapped_column(default=0)
    __table_args__ = (Index("uq_source_node_serial", "node_id", "serial", unique=True),)


class Target(Base):
    __tablename__ = "targets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(8), default="audio")  # audio|adsb|ais
    frequency_hz: Mapped[int] = mapped_column()
    modulation: Mapped[str] = mapped_column(String(12), default="nfm")
    squelch: Mapped[int] = mapped_column(default=50)  # 0 = continuous recording
    clearcut_enabled: Mapped[bool] = mapped_column(default=False)
    clearcut_system: Mapped[str | None] = mapped_column(String(64), nullable=True)
    clearcut_id: Mapped[int | None] = mapped_column(nullable=True)  # talkgroup number
    rdio_enabled: Mapped[bool] = mapped_column(default=False)
    rdio_system: Mapped[int | None] = mapped_column(nullable=True)
    rdio_talkgroup: Mapped[int | None] = mapped_column(nullable=True)


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
    lock_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    try:
        db_path.chmod(0o600)
    except OSError:
        pass
    return sessionmaker(engine, expire_on_commit=False)
