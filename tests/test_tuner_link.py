"""The web tuner end to end: a fake rtl_tcp, the engine, the master's hub and the browser's websocket."""
from __future__ import annotations

import asyncio
import json
import struct

import pytest

pytest.importorskip("numpy")

from sentrysdr import tuner_session  # noqa: E402
from sentrysdr.tuner_hub import TunerError, TunerHub  # noqa: E402
from sentrysdr.web.routes_nodes import decode_tuner_frame  # noqa: E402

from .conftest import add_user, client_for, make_config  # noqa: E402
from .test_engine_nodes import install_tool, spec, supervisor, wait_for  # noqa: E402
from .test_radio import make_source, make_target  # noqa: E402

ORIGIN = {"origin": "https://sentry.example.com"}

# rtl_tcp's protocol: a 12-byte header, then IQ bytes; commands are 5 bytes each.
# This one sends a carrier 100 kHz above centre and writes down every command it gets.
FAKE_RTL_TCP = r'''#!/usr/bin/env python3
import math, socket, struct, sys, threading, time
args = sys.argv[1:]
port = int(args[args.index("-p") + 1])
log = open(LOG, "a")
log.write("args " + " ".join(args) + "\n"); log.flush()
server = socket.socket(); server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port)); server.listen(1)
conn, _ = server.accept()
conn.sendall(b"RTL0" + struct.pack(">II", 5, 29))
def commands():
    while True:
        data = b""
        while len(data) < 5:
            chunk = conn.recv(5 - len(data))
            if not chunk: return
            data += chunk
        cmd, value = struct.unpack(">BI", data)
        log.write(f"cmd {cmd} {value}\n"); log.flush()
threading.Thread(target=commands, daemon=True).start()
rate = 2048000
n = 102400
phase = 0.0
step = 2 * math.pi * 100000 / rate
block = bytearray(2 * n)
for i in range(n):
    block[2 * i] = int(127.5 + 60 * math.cos(phase + i * step))
    block[2 * i + 1] = int(127.5 + 60 * math.sin(phase + i * step))
try:
    while True:
        conn.sendall(block)
        time.sleep(n / rate)
except OSError:
    pass
'''


def tuner_spec(**overrides):
    return spec(kind="tuner", modulation="tuner", frequency_hz=145_000_000, squelch=0, **overrides)


@pytest.fixture
def fake_rtl_tcp(tmp_path):
    bins, log = tmp_path / "bin", tmp_path / "rtl_tcp.log"
    install_tool(bins, "rtl_tcp", FAKE_RTL_TCP.replace("LOG", repr(str(log)), 1))
    return bins, log


# --- the engine ---------------------------------------------------------------------
async def test_a_tuner_streams_and_takes_commands(tmp_path, fake_rtl_tcp):
    bins, log = fake_rtl_tcp
    out = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), [])
    sup.tuner_out = lambda serial, kind, data: out.append((serial, kind, data))
    try:
        await sup.apply({"SER1": tuner_spec(ppm=3)})
        assert await wait_for(lambda: any(k == "json" and d.get("running") for _, k, d in out))
        args = log.read_text().splitlines()[0]
        assert "-a 127.0.0.1" in args and "-f 145000000" in args and "-P 3" in args
        listen = {"frequency_hz": 145_100_000, "mode": "nfm"}
        assert sup.tuner_command("SER1", {"op": "sync", "watching": True, "listeners": {"7": listen}})
        assert await wait_for(lambda: any(k == "frame" and d[:1] == b"W" for _, k, d in out))
        assert await wait_for(lambda: any(k == "frame" and d[:1] == b"A" for _, k, d in out))
        frame = next(d for _, k, d in out if k == "frame" and d[:1] == b"A")
        listener, rate, channels, level, is_open = struct.unpack_from("<IfBfB", frame, 1)
        assert listener == 7 and 15_000 < rate < 17_000 and channels == 1 and level > -20 and is_open
        row = next(d for _, k, d in out if k == "frame" and d[:1] == b"W")
        center, sample_rate = struct.unpack_from("<QI", row, 1)
        assert center == 145_000_000 and sample_rate == 2_048_000 and len(row) == 13 + 1024
        assert await wait_for(lambda: any(k == "json" and d.get("t") == "rx" and d["listener"] == 7 for _, k, d in out))
        # Steering it reaches rtl_tcp as its own commands.
        sup.tuner_command("SER1", {"op": "center", "hz": 146_000_000})
        sup.tuner_command("SER1", {"op": "gain", "db": 29.7})
        sup.tuner_command("SER1", {"op": "center", "hz": 5})  # out of range: dropped
        assert await wait_for(lambda: "cmd 1 146000000" in log.read_text() and "cmd 4 297" in log.read_text())
        assert "cmd 1 5\n" not in log.read_text()
        assert await wait_for(lambda: sup.workers["SER1"].tuner_saved.get("center_hz") == 146_000_000, timeout=5)
    finally:
        await sup.shutdown()
    assert out[-1][2] == {"t": "state", "running": False}
    assert not sup.tuner_command("SER1", {"op": "center", "hz": 146_000_000})


async def test_nobody_watching_means_no_processing(tmp_path, fake_rtl_tcp):
    bins, _ = fake_rtl_tcp
    out = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), [])
    sup.tuner_out = lambda serial, kind, data: out.append((kind, data))
    try:
        await sup.apply({"SER1": tuner_spec()})
        assert await wait_for(lambda: any(k == "json" and d.get("running") for k, d in out))
        await asyncio.sleep(0.5)
        assert not any(k == "frame" for k, _ in out)
    finally:
        await sup.shutdown()


async def test_a_bad_command_doesnt_stop_the_tuner(tmp_path, fake_rtl_tcp):
    bins, _ = fake_rtl_tcp
    out = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), [])
    sup.tuner_out = lambda serial, kind, data: out.append((kind, data))
    try:
        await sup.apply({"SER1": tuner_spec()})
        assert await wait_for(lambda: any(k == "json" and d.get("running") for k, d in out))
        for bad in ({"op": "listen", "listener": "x"}, {"op": "listen", "listener": 1, "frequency_hz": "lots"},
                    {"op": "sync", "watching": True, "listeners": {"1": {"frequency_hz": 145e6, "mode": "jazz"}}},
                    {"op": "rate", "hz": 3}, {"op": None}, {"op": "sync", "listeners": "nope"}):
            sup.tuner_command("SER1", bad)
        sup.tuner_command("SER1", {"op": "sync", "watching": True, "listeners": {"2": {"frequency_hz": 145_100_000, "mode": "am"}}})
        assert await wait_for(lambda: any(k == "frame" and d[:1] == b"A" for k, d in out))
        assert sup.workers["SER1"].tuner is not None
    finally:
        await sup.shutdown()


# --- the hub ------------------------------------------------------------------------------
def test_hub_routes_frames_and_ownership():
    sent = []
    hub = TunerHub(lambda node, serial, command: sent.append(command) or True)
    key = (2, "SER9")
    first = hub.join(key, 1, "Ada", is_admin=False, can_own=True)
    second = hub.join(key, 2, "Bea", is_admin=False, can_own=True)
    third = hub.join(key, 3, "Cy", is_admin=False, can_own=False)  # Listen live only
    assert sent[-1]["op"] == "sync" and sent[-1]["watching"]
    assert hub.rooms[key].controller is None  # nobody owns it until they take it
    hub.command(key, second, {"t": "listen", "frequency_hz": 145_100_000, "mode": "nfm", "evil": 1})
    assert sent[-1] == {"op": "listen", "listener": second.id, "frequency_hz": 145_100_000, "mode": "nfm", "deemphasis_us": 75}
    hub.command(key, third, {"t": "listen", "frequency_hz": 145_200_000, "mode": "am"})  # anyone picks their own channel
    with pytest.raises(TunerError, match="owner"):
        hub.command(key, first, {"t": "center", "hz": 146_000_000})
    with pytest.raises(TunerError, match="permission"):
        hub.command(key, third, {"t": "claim"})
    hub.command(key, first, {"t": "claim"})
    with pytest.raises(TunerError, match="Ada has this tuner"):
        hub.command(key, second, {"t": "claim"})
    hub.command(key, first, {"t": "center", "hz": 146_000_000})
    assert sent[-1] == {"op": "center", "hz": 146_000_000}
    with pytest.raises(TunerError):
        hub.command(key, first, {"t": "rate", "hz": 3_000_000})
    # The owner sees who it can go to; handing to someone without permission is refused.
    state = json.loads([item for item in list(first.queue._queue) if item[0] == "text"][-1][1])
    assert state["you_control"] and [v["name"] for v in state["handover"]] == ["Bea"]
    with pytest.raises(TunerError):
        hub.command(key, first, {"t": "handover", "to": third.id})
    hub.command(key, first, {"t": "handover", "to": second.id})
    assert hub.rooms[key].controller == second.id
    with pytest.raises(TunerError, match="don't have"):
        hub.command(key, first, {"t": "release"})
    # Waterfall rows go to everyone, audio only to its listener.
    for queue in (first.queue, second.queue):
        while not queue.empty():
            queue.get_nowait()
    hub.from_node(2, "SER9", "frame", tuner_session.waterfall_frame(146_000_000, 2_048_000, b"\x01" * 4))
    hub.from_node(2, "SER9", "frame", tuner_session.audio_frame(second.id, 16000.0, 1, -30.0, True, b"\x80" * 8))
    hub.from_node(3, "SER9", "frame", tuner_session.audio_frame(first.id, 16000.0, 1, -30.0, True, b"\x80" * 8))  # another node
    kinds = lambda q: [q.get_nowait()[1][:1] for _ in range(q.qsize())]  # noqa: E731
    assert kinds(first.queue) == [b"W"] and kinds(second.queue) == [b"W", b"A"]
    # The owner leaving frees it; nobody gets it by themselves.
    hub.leave(key, second)
    assert hub.rooms[key].controller is None and sent[-1] == {"op": "leave", "listener": second.id}
    hub.command(key, first, {"t": "claim"})
    hub.command(key, first, {"t": "release"})
    assert hub.rooms[key].controller is None
    hub.leave(key, first)
    hub.leave(key, third)
    assert key not in hub.rooms and sent[-1] == {"op": "sync", "watching": False, "listeners": {}}


def test_an_admin_can_take_the_tuner():
    hub = TunerHub(lambda *_: True)
    key = (1, "S")
    owner = hub.join(key, 1, "Ada", is_admin=False, can_own=True)
    admin = hub.join(key, 2, "Root", is_admin=True, can_own=False)  # an admin can always own
    hub.command(key, owner, {"t": "claim"})
    hub.command(key, admin, {"t": "claim"})
    assert hub.rooms[key].controller == admin.id


def test_hub_limits_viewers_and_commands():
    hub = TunerHub(lambda *_: True)
    key = (1, "S")
    viewers = [hub.join(key, i, f"U{i}", False) for i in range(8)]
    with pytest.raises(TunerError, match="already has"):
        hub.join(key, 99, "Late", False)
    hub.command(key, viewers[0], {"t": "claim"})
    for _ in range(100):
        hub.command(key, viewers[0], {"t": "center", "hz": 146_000_000})
    assert viewers[0].allowance < 1


def test_tuner_frames_from_a_slave_are_checked():
    frame = b"\x00\x04SER1" + tuner_session.waterfall_frame(1, 2, b"x")
    assert decode_tuner_frame(frame) == ("SER1", tuner_session.waterfall_frame(1, 2, b"x"))
    assert decode_tuner_frame(b"\x00\x00") is None and decode_tuner_frame(b"\x00\x09SER1") is None
    assert decode_tuner_frame(b"\x00\x04S R1xxxx") is None  # not a serial
    assert decode_tuner_frame(b"\x00\x04SER1" + b"x" * 600_000) is None


# --- the browser's websocket --------------------------------------------------------------------
def assign_tuner(admin):
    source = make_source(admin)
    target = make_target(admin, "Tuner", 145_000_000, kind="tuner", modulation="tuner")
    assert admin.post(f"/api/sources/{source}/assign", json={"target_id": target}).status_code == 200
    return source


def test_websocket_needs_origin_signin_and_permission(ctx, app, admin, listener):
    source = assign_tuner(admin)
    with pytest.raises(Exception):
        with admin.websocket_connect(f"wss://sentry.example.com/api/tuner/{source}/ws"):  # no Origin: refused before anything
            pass
    with pytest.raises(Exception):
        with admin.websocket_connect(f"wss://sentry.example.com/api/tuner/{source}/ws", headers={"origin": "https://evil.example"}):
            pass
    anonymous = client_for(app)
    with anonymous.websocket_connect(f"wss://sentry.example.com/api/tuner/{source}/ws", headers=ORIGIN) as socket:
        assert "Sign in" in json.loads(socket.receive_text())["detail"]
    me = listener.get("/api/me").json()
    admin.patch(f"/api/admin/users/{me['id']}", json={"perm_overrides": {"listen_live": False}}).raise_for_status()
    with listener.websocket_connect(f"wss://sentry.example.com/api/tuner/{source}/ws", headers=ORIGIN) as socket:
        reply = json.loads(socket.receive_text())
        assert reply["fatal"] and "permission" in reply["detail"]
    assert listener.get("/api/tuners").status_code == 403


def test_a_listener_listens_but_cant_own(ctx, app, admin, listener):
    source = assign_tuner(admin)
    ctx.tuners.send = lambda *_: True
    assert listener.get("/api/tuners").status_code == 200
    with listener.websocket_connect(f"wss://sentry.example.com/api/tuner/{source}/ws", headers=ORIGIN) as socket:
        assert json.loads(socket.receive_text())["t"] == "hello"
        state = json.loads(socket.receive_text())
        assert not state["you_control"] and not state["can_claim"]
        socket.send_text(json.dumps({"t": "claim"}))
        assert "permission to tune" in json.loads(socket.receive_text())["detail"]


def test_websocket_joins_and_takes_commands(ctx, app, admin, operator):
    source = assign_tuner(admin)
    sent = []
    ctx.tuners.send = lambda node, serial, command: sent.append(command) or True
    assert [t["name"] for t in operator.get("/api/tuners").json()] == ["Source 1"]
    with operator.websocket_connect(f"wss://sentry.example.com/api/tuner/{source}/ws", headers=ORIGIN) as socket:
        assert json.loads(socket.receive_text())["t"] == "hello"
        state = json.loads(socket.receive_text())
        assert state["t"] == "state" and not state["you_control"] and state["can_claim"] and state["viewers"] == 1
        socket.send_text(json.dumps({"t": "claim"}))
        assert json.loads(socket.receive_text())["you_control"]
        socket.send_text(json.dumps({"t": "center", "hz": 146_000_000}))
        socket.send_text(json.dumps({"t": "center", "hz": 1}))
        assert "centre frequency" in json.loads(socket.receive_text())["detail"]
        assert {"op": "center", "hz": 146_000_000} in sent
        assert operator.get("/api/tuners").json()[0]["viewers"] == 1
    assert sent[-1] == {"op": "sync", "watching": False, "listeners": {}}
    assert any(row["action"] == "tuner.opened" for row in admin.get("/api/admin/audit").json())


def test_websocket_refuses_an_sdr_that_isnt_a_tuner(ctx, admin):
    source = make_source(admin)
    with admin.websocket_connect(f"wss://sentry.example.com/api/tuner/{source}/ws", headers=ORIGIN) as socket:
        assert "isn't set up as a tuner" in json.loads(socket.receive_text())["detail"]


def test_tuner_permission_can_be_granted(ctx, app, admin):
    add_user(ctx, "lee@example.com")
    lee = client_for(app, "lee@example.com")
    me = lee.get("/api/me").json()
    admin.patch(f"/api/admin/users/{me['id']}", json={"perm_overrides": {"tuner": True}}).raise_for_status()
    assert lee.get("/api/tuners").status_code == 200


# --- a slave's side ------------------------------------------------------------------------------
async def test_slave_passes_tuner_commands_and_output(tmp_path):
    from sentrysdr.slave import Slave
    cfg = make_config(tmp_path, role="slave", master_url="https://m.example.com", serials=["FIELD1"])
    slave = Slave(cfg, enumerate_fn=lambda: [])
    got = []
    slave.supervisor.tuner_command = lambda serial, command: got.append((serial, command)) or True
    await slave.handle_message({"t": "tuner", "serial": "FIELD1", "command": {"op": "center", "hz": 146_000_000}})
    await slave.handle_message({"t": "tuner", "serial": "OTHER", "command": {"op": "center", "hz": 1}})
    await slave.handle_message({"t": "tuner", "serial": "FIELD1", "command": "nope"})
    assert got == [("FIELD1", {"op": "center", "hz": 146_000_000})]
    slave.outgoing = asyncio.Queue(10)
    row = tuner_session.waterfall_frame(146_000_000, 2_048_000, b"\x05" * 8)
    slave._on_tuner("FIELD1", "frame", row)
    slave._on_tuner("FIELD1", "json", {"t": "rx", "listener": 3})
    frame, text = slave.outgoing.get_nowait(), slave.outgoing.get_nowait()
    assert decode_tuner_frame(frame) == ("FIELD1", row)
    assert json.loads(text) == {"t": "tuner_out", "serial": "FIELD1", "data": {"t": "rx", "listener": 3}}
