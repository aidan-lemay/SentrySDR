"""Slave nodes: admin enrolment, plus the agent endpoints that slaves dial into.

Agent routes authenticate with a per-node bearer key, never a browser cookie.
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import feeds, security
from ..config import SERIAL_RE
from ..context import Context
from ..models import Node, Source, Target, User
from .deps import admin_user, ctx_of, get_db, ip_of

log = logging.getLogger("sentrysdr.nodes")
router = APIRouter(prefix="/api/nodes")

ENROLL_TOKEN_SECONDS = 86400
ENROLL_LIMIT = 10
ENROLL_WINDOW = 3600
MAX_CALL_BYTES = 256 * 1024 * 1024
MAX_FRAME_SERIAL = 64


def _view(ctx: Context, node: Node) -> dict:
    return {
        "id": node.id, "name": node.name, "is_local": node.is_local, "status": node.status,
        "online": ctx.node_online(node) and node.status == "active",
        "serials": node.serials or [], "last_seen_at": node.last_seen_at, "last_ip": node.last_ip,
        "enroll_expires_at": node.enroll_expires_at,
    }


@router.get("")
def list_nodes(request: Request, _admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    return [_view(ctx, n) for n in db.scalars(select(Node).order_by(Node.id))]


class NodeBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.post("", status_code=201)
def create_enrollment(body: NodeBody, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    """Create a slave entry and a one-time enrolment token, shown only here."""
    ctx = ctx_of(request)
    token = security.new_token()
    node = Node(
        name=body.name, status="pending", enroll_token_hash=security.hash_token(token),
        enroll_expires_at=time.time() + ENROLL_TOKEN_SECONDS,
    )
    db.add(node)
    db.commit()
    ctx.audit("node.enrollment_created", user=admin, ip=ip_of(request), target=node.name)
    return {**_view(ctx, node), "enrollment_token": token}


@router.delete("/{node_id}")
def revoke_node(node_id: int, request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    ctx = ctx_of(request)
    node = db.get(Node, node_id)
    if node is None or node.is_local:
        raise HTTPException(404, "Node not found")
    node.status, node.key_hash, node.enroll_token_hash = "revoked", None, None
    db.commit()
    link = ctx.node_links.get(node.id)
    if link is not None and ctx.loop:
        ctx.loop.call_soon_threadsafe(link.disconnect)
    ctx.audit("node.revoked", user=admin, ip=ip_of(request), target=node.name)
    return {"detail": "Revoked"}


# --- agent endpoints -------------------------------------------------------
class EnrollBody(BaseModel):
    token: str = Field(max_length=128)
    name: str | None = Field(default=None, max_length=120)


@router.post("/agent/enroll")
def enroll(body: EnrollBody, request: Request, db=Depends(get_db)):
    ctx = ctx_of(request)
    ip = ip_of(request)
    key = f"enroll:{ip}"
    if ctx.limiter.blocked(key, ENROLL_LIMIT, ENROLL_WINDOW):
        raise HTTPException(429, "Too many attempts")
    node = db.scalar(select(Node).where(Node.enroll_token_hash == security.hash_token(body.token)))
    if node is None or node.status != "pending" or (node.enroll_expires_at or 0) < time.time():
        ctx.limiter.hit(key, ENROLL_WINDOW)
        ctx.audit("node.enroll_failed", ip=ip)
        raise HTTPException(403, "Invalid or expired enrolment token")
    device_key = security.new_token()
    node.key_hash = security.hash_token(device_key)
    node.enroll_token_hash, node.enroll_expires_at = None, None  # single use
    node.status, node.last_ip = "active", ip
    db.commit()
    ctx.audit("node.enrolled", ip=ip, target=node.name)
    return {"node_id": node.id, "name": node.name, "key": device_key}


def _authenticate(ctx: Context, db, authorization: str | None) -> Node:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing device key")
    node = db.scalar(select(Node).where(Node.key_hash == security.hash_token(authorization[7:].strip())))
    if node is None or node.status != "active":
        raise HTTPException(401, "Invalid device key")
    return node


def clean_meta(raw: dict, node: Node, db) -> dict:
    """Validate a slave's call metadata and tie it to that node's own source."""
    try:
        call_id = str(uuid.UUID(str(raw["id"])))
        serial = str(raw["serial"])
        meta = {
            "id": call_id, "serial": serial,
            "target_id": int(raw["target_id"]) if raw.get("target_id") is not None else None,
            "source_name": str(raw.get("source_name", ""))[:120],
            "target_name": str(raw.get("target_name", ""))[:120],
            "recorder_name": str(raw.get("recorder_name", ""))[:120],
            "frequency_hz": int(raw.get("frequency_hz", 0)),
            "modulation": str(raw.get("modulation", ""))[:12],
            "started_at": float(raw["started_at"]),
            "ended_at": float(raw["ended_at"]),
            "duration": float(raw["duration"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, "Invalid call metadata") from exc
    if serial not in (node.serials or []):
        raise HTTPException(403, "That serial doesn't belong to this node")
    source = db.scalar(select(Source).where(Source.node_id == node.id, Source.serial == serial))
    meta["source_id"] = source.id if source else None
    # An enrolled node is trusted to name a target; a revoked key can't reach here.
    if meta["target_id"] is not None and db.get(Target, meta["target_id"]) is None:
        meta["target_id"] = None
    return meta


def _looks_like_mp3(path: Path) -> bool:
    with path.open("rb") as handle:
        head = handle.read(3)
    return head == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)


@router.post("/agent/calls")
async def receive_call(
    request: Request, meta: str = Form(max_length=8192), audio: UploadFile = File(),
    authorization: str | None = Header(default=None), db=Depends(get_db),
):
    ctx = ctx_of(request)
    node = _authenticate(ctx, db, authorization)
    try:
        clean = clean_meta(json.loads(meta), node, db)
    except ValueError as exc:
        raise HTTPException(422, "Invalid call metadata") from exc
    with tempfile.TemporaryDirectory(dir=ctx.cfg.recordings_dir) as tmp:
        path = Path(tmp) / "call.mp3"
        size = 0
        with path.open("wb") as handle:
            while chunk := await audio.read(1 << 20):
                size += len(chunk)
                if size > MAX_CALL_BYTES:
                    raise HTTPException(413, "Call is too large")
                handle.write(chunk)
        if not _looks_like_mp3(path):
            raise HTTPException(422, "Audio is not MP3")
        created = await asyncio.to_thread(ctx.store_call, clean, path, node.name)
    return {"stored": created, "id": clean["id"]}


class NodeLink:
    """The master's end of one slave's websocket."""

    def __init__(self, ctx: Context, node_id: int, websocket: WebSocket):
        self.ctx, self.node_id, self.ws = ctx, node_id, websocket
        self._last_desired: str | None = None
        self.outbox: asyncio.Queue = asyncio.Queue(256)

    def request_live(self, serial: str, on: bool) -> None:
        self._queue({"t": "live", "serial": serial, "on": on})

    def request_measure(self, request_id: str, serial: str, request: dict) -> None:
        self._queue({"t": "measure", "id": request_id, "serial": serial, **request})

    def _queue(self, message: dict) -> None:
        try:
            self.outbox.put_nowait(json.dumps(message))
        except asyncio.QueueFull:
            log.warning("node %s outbox full; dropping a message", self.node_id)

    async def send_desired(self, assignments: dict) -> None:
        payload = json.dumps(assignments, sort_keys=True)
        if payload != self._last_desired:
            self._last_desired = payload
            self._queue({"t": "desired", "assignments": assignments})

    def disconnect(self) -> None:
        self._queue({"t": "revoked"})
        asyncio.ensure_future(self.ws.close(code=4403))

    async def writer(self) -> None:
        while True:
            await self.ws.send_text(await self.outbox.get())


def decode_live_frame(frame: bytes) -> tuple[str, int, bytes] | None:
    """Frame layout: 1 byte serial length, serial, 4 byte big-endian rate, PCM."""
    if len(frame) < 6 or frame[0] == 0 or frame[0] > MAX_FRAME_SERIAL or len(frame) < 5 + frame[0]:
        return None
    length = frame[0]
    try:
        serial = frame[1:1 + length].decode()
    except UnicodeDecodeError:
        return None
    if not SERIAL_RE.match(serial):
        return None
    rate = int.from_bytes(frame[1 + length:5 + length], "big")
    if rate not in (8000, 16000):
        return None
    return serial, rate, frame[5 + length:]


def clean_feed_status(raw) -> dict:
    """A slave's report of its feeds, keeping only what the page shows."""
    if not isinstance(raw, dict):
        return {}
    clean = {}
    if isinstance(raw.get("aggregators"), dict):
        clean["aggregators"] = {k: bool(v) for k, v in raw["aggregators"].items() if k in feeds.ADSB_AGGREGATORS}
    if isinstance(raw.get("local_clients"), int) and 0 <= raw["local_clients"] < 1000:
        clean["local_clients"] = raw["local_clients"]
    if isinstance(raw.get("sending"), list):
        clean["sending"] = [s for s in raw["sending"] if s in feeds.AIS_SERVICES or s == "community"][:10]
    return clean


def handle_agent_message(ctx: Context, node_id: int, allowed: list[str], message: dict) -> None:
    kind = message.get("t")
    if kind == "status":
        for serial, status in (message.get("sources") or {}).items():
            if serial in allowed and isinstance(status, dict):
                ctx.set_status(node_id, serial, {
                    "state": str(status.get("state", ""))[:16], "error": str(status.get("error", ""))[:300],
                    "in_call": bool(status.get("in_call")), "call_started": status.get("call_started"),
                    "last_call_at": status.get("last_call_at"),
                    "note": str(status.get("note") or "")[:300], "feeds": clean_feed_status(status.get("feeds")),
                })
    elif kind == "measure_result":
        ctx.finish_measure(node_id, message)
    elif kind == "tracks":
        serial, track_kind = message.get("serial"), message.get("kind")
        if serial in allowed and track_kind in ("adsb", "ais", "acars") and isinstance(message.get("data"), dict):
            ctx.update_tracks(track_kind, node_id, serial, message["data"])


@router.websocket("/agent/ws")
async def agent_socket(websocket: WebSocket):
    ctx: Context = websocket.app.state.ctx
    with ctx.db() as db:
        try:
            node = _authenticate(ctx, db, websocket.headers.get("authorization"))
        except HTTPException:
            await websocket.close(code=4401)
            return
        node_id, node_name = node.id, node.name

    await websocket.accept()
    ip = security.client_ip(ctx.cfg, websocket.client.host if websocket.client else None, websocket.headers)
    link = NodeLink(ctx, node_id, websocket)
    allowed: list[str] = []
    writer = None
    try:
        hello = json.loads(await asyncio.wait_for(websocket.receive_text(), 15))
        allowed = [s for s in hello.get("serials", []) if isinstance(s, str) and SERIAL_RE.match(s)][:64]
        with ctx.db() as db:
            node = db.get(Node, node_id)
            node.serials, node.last_seen_at, node.last_ip = allowed, time.time(), ip
            db.commit()
        previous = ctx.node_links.get(node_id)
        if previous is not None:
            await previous.ws.close(code=4409)
        ctx.node_links[node_id] = link
        writer = asyncio.create_task(link.writer())
        ctx.audit("node.connected", ip=ip, target=node_name)
        ctx.kick()
        # A listener may already be waiting (for example, after a reconnect).
        for key in ctx.live.active_keys():
            owner, _, serial = key.partition(":")
            if owner == str(node_id):
                link.request_live(serial, True)
        last_seen_written = time.monotonic()
        while True:
            packet = await websocket.receive()
            if packet["type"] == "websocket.disconnect":
                break
            if packet.get("bytes") is not None:
                decoded = decode_live_frame(packet["bytes"])
                if decoded and decoded[0] in allowed:
                    ctx.live.feed(ctx.live_key(node_id, decoded[0]), decoded[2], decoded[1])
            elif packet.get("text"):
                try:
                    handle_agent_message(ctx, node_id, allowed, json.loads(packet["text"]))
                except ValueError:
                    pass
            if time.monotonic() - last_seen_written > 15:
                last_seen_written = time.monotonic()
                with ctx.db() as db:
                    db.get(Node, node_id).last_seen_at = time.time()
                    db.commit()
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, AttributeError):
        pass
    finally:
        if writer:
            writer.cancel()
        if ctx.node_links.get(node_id) is link:
            del ctx.node_links[node_id]
            for serial in allowed:
                ctx.status.pop((node_id, serial), None)
            ctx.audit("node.disconnected", ip=ip, target=node_name)
