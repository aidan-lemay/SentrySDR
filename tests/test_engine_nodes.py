"""The radio engine with fake tools, and a real master/slave link over sockets."""
from __future__ import annotations

import asyncio
import json
import socket
import stat
import textwrap
import time
import uuid

import pytest
import uvicorn

from sentrysdr import pipelines
from sentrysdr.context import Context
from sentrysdr.engine import Supervisor
from sentrysdr.live import LiveHub, silence_needed
from sentrysdr.models import Node, Recording
from sentrysdr.slave import Slave, encode_live_frame, ws_url
from sentrysdr.web import create_app
from sentrysdr.web.routes_nodes import decode_live_frame

from .conftest import ADMIN_EMAIL, add_user, client_for, make_config

# Emits two bursts of PCM separated by a silent gap, like a squelch closing.
FAKE_RTL_FM = """\
#!/usr/bin/env python3
import sys, time
out = sys.stdout.buffer
for burst in range(2):
    for _ in range(8):
        out.write(b"\\x10\\x00" * 800); out.flush(); time.sleep(0.05)
    time.sleep(0.8)
time.sleep(60)
"""


def install_tool(directory, name, source):
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(source)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def spec(**overrides):
    return pipelines.sanitize_spec({
        "recorder_id": 1, "recorder_name": "r", "source_id": 1, "source_name": "s", "target_id": 1,
        "target_name": "K1ABC", "frequency_hz": 444_444_000, "modulation": "nfm", "squelch": 50, **overrides,
    })


async def wait_for(condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return False


def supervisor(cfg, calls, pcm=None, statuses=None, tracks=None, devices=("SER1", "SER2")):
    async def sink(meta, path):
        calls.append((meta, path.read_bytes()[:3], path.stat().st_size))
    return Supervisor(
        cfg, sink, pcm or (lambda *_: None), tracks or (lambda *_: None),
        (lambda serial, status: statuses.setdefault(serial, []).append(status)) if statuses is not None else (lambda *_: None),
        enumerate_fn=lambda: list(devices),
    )


# --- call detection --------------------------------------------------------
async def test_each_transmission_becomes_one_mp3_call(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", FAKE_RTL_FM)
    cfg = make_config(tmp_path, bin_dirs=[bins])
    calls, chunks = [], []
    sup = supervisor(cfg, calls, pcm=lambda serial, data, rate: chunks.append((serial, rate)))
    try:
        await sup.apply({"SER1": spec()})
        assert await wait_for(lambda: len(calls) >= 2)
    finally:
        await sup.shutdown()
    meta, head, size = calls[0]
    assert meta["target_name"] == "K1ABC" and meta["serial"] == "SER1" and 0.3 < meta["duration"] < 1.0
    assert (head == b"ID3" or head[0] == 0xFF) and size > 0
    assert chunks[0] == ("SER1", 16000)
    assert list((cfg.recordings_dir / ".tmp").glob("*.raw")) == []


async def test_call_shorter_than_minimum_discarded(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", "#!/usr/bin/env python3\nimport sys,time\nsys.stdout.buffer.write(b'\\0\\0'*100);sys.stdout.flush();time.sleep(60)\n")
    calls = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins], min_call_seconds=0.5), calls)
    await sup.apply({"SER1": spec()})
    await asyncio.sleep(1.0)
    await sup.shutdown()
    assert calls == []


async def test_retune_saves_call_in_progress(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", "#!/usr/bin/env python3\nimport sys,time\nwhile True:\n  sys.stdout.buffer.write(b'\\1\\0'*1600);sys.stdout.flush();time.sleep(0.05)\n")
    calls = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), calls)
    await sup.apply({"SER1": spec()})
    worker = sup.workers["SER1"]
    assert await wait_for(lambda: worker.in_call)
    await asyncio.sleep(0.5)
    await sup.apply({"SER1": spec(frequency_hz=145_000_000)})
    assert await wait_for(lambda: len(calls) == 1)
    assert calls[0][0]["frequency_hz"] == 444_444_000
    await sup.shutdown()


async def test_long_carrier_is_split(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", "#!/usr/bin/env python3\nimport sys,time\nwhile True:\n  sys.stdout.buffer.write(b'\\1\\0'*1600);sys.stdout.flush();time.sleep(0.05)\n")
    calls = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins], max_call_seconds=1), calls)
    await sup.apply({"SER1": spec()})
    assert await wait_for(lambda: len(calls) >= 2)
    await sup.shutdown()


# --- only configured SDRs are touched --------------------------------------
async def test_unconfigured_serial_never_started(tmp_path):
    bins = tmp_path / "bin"
    marker = tmp_path / "started"
    install_tool(bins, "rtl_fm", f"#!/bin/sh\ntouch {marker}\nsleep 60\n")
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins], serials=["SER1"]), [], devices=("SER1", "OTHER"))
    await sup.apply({"OTHER": spec()})
    await asyncio.sleep(0.3)
    await sup.shutdown()
    assert not marker.exists()


async def test_correct_device_index_used(tmp_path):
    bins = tmp_path / "bin"
    out = tmp_path / "args"
    install_tool(bins, "rtl_fm", f"#!/bin/sh\necho \"$@\" > {out}\nsleep 60\n")
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins], serials=["00000001"]), [], devices=("OTHER", "SPARE", "00000001"))
    await sup.apply({"00000001": spec()})
    assert await wait_for(out.exists)
    await sup.shutdown()
    assert out.read_text().startswith("-d 2 ")


async def test_missing_sdr_and_missing_tool_reported_not_fatal(tmp_path):
    statuses = {}
    sup = supervisor(make_config(tmp_path), [], statuses=statuses, devices=())
    await sup.apply({"SER1": spec()})
    assert await wait_for(lambda: any(s["error"] == "SDR not connected" for s in statuses.get("SER1", [])))
    await sup.shutdown()

    statuses = {}
    cfg = make_config(tmp_path, bin_dirs=[tmp_path / "none"])
    cfg.binary = lambda name: f"/nonexistent/{name}"
    sup = supervisor(cfg, [], statuses=statuses)
    await sup.apply({"SER1": spec()})
    assert await wait_for(lambda: any("not installed" in s["error"] for s in statuses.get("SER1", [])))
    await sup.shutdown()


async def test_digital_pipeline_pipes_tuner_into_decoder(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", "#!/usr/bin/env python3\nimport sys,time\nfor i in range(10):\n  sys.stdout.buffer.write(b'A'*400);sys.stdout.flush();time.sleep(0.02)\ntime.sleep(60)\n")
    # A stand-in decoder: proves stdin is the tuner, and emits its input as audio.
    install_tool(bins, "dsd-fme", "#!/usr/bin/env python3\nimport sys\nwhile d:=sys.stdin.buffer.read1(4096):\n  sys.stdout.buffer.write(d);sys.stdout.flush()\n")
    calls = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), calls)
    await sup.apply({"SER1": spec(modulation="p25p1")})
    assert await wait_for(lambda: len(calls) == 1)
    await sup.shutdown()
    assert calls[0][0]["modulation"] == "p25p1"


async def test_ais_pipeline_publishes_vessels(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "AIS-catcher", "#!/usr/bin/env python3\nimport time\nfor i in range(30):\n  print('{\"mmsi\":123,\"lat\":1.0,\"lon\":2.0}',flush=True);time.sleep(0.1)\ntime.sleep(60)\n")
    tracks = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), [], tracks=lambda *a: tracks.append(a))
    await sup.apply({"SER1": spec(kind="ais", modulation="ais", frequency_hz=162_000_000)})
    assert await wait_for(lambda: tracks)
    await sup.shutdown()
    assert tracks[0][0] == "ais" and tracks[0][2]["vessels"][0]["mmsi"] == "123"


async def test_adsb_pipeline_reads_written_json(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "readsb", textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys, time
        out = sys.argv[sys.argv.index("--write-json") + 1]
        json.dump({"aircraft": [{"hex": "abc123", "lat": 1, "lon": 2}]}, open(out + "/aircraft.json", "w"))
        time.sleep(60)
        """))
    tracks = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), [], tracks=lambda *a: tracks.append(a))
    await sup.apply({"SER1": spec(kind="adsb", modulation="adsb", frequency_hz=1_090_000_000)})
    assert await wait_for(lambda: tracks)
    await sup.shutdown()
    assert tracks[0][2]["aircraft"][0]["hex"] == "abc123"


# --- live pacing -----------------------------------------------------------
def test_no_silence_inserted_between_bursts_of_speech():
    assert silence_needed(elapsed=1.0, written=2 * 16000 - 4096, rate=16000) == 0


def test_silence_fills_a_closed_squelch_to_real_time():
    assert silence_needed(elapsed=2.0, written=0, rate=16000) == 64000


def test_silence_is_whole_samples():
    assert silence_needed(elapsed=1.23457, written=1, rate=16000) % 2 == 0


async def test_burst_arrival_keeps_stream_at_real_time(tmp_path):
    """PCM arriving every 128 ms must not gain inserted silence."""
    hub = LiveHub(make_config(tmp_path))
    queue = await hub.subscribe("k")
    stream = hub.streams["k"]
    written = []
    write = stream.proc.stdin.write
    stream.proc.stdin.write = lambda data: (written.append(len(data)), write(data))[1]
    started = time.monotonic()
    while time.monotonic() - started < 1.5:
        hub.feed("k", b"\1\0" * 2048, 16000)
        await asyncio.sleep(0.128)
    elapsed = time.monotonic() - started
    stop_started = time.monotonic()
    await hub.unsubscribe("k", queue)
    assert time.monotonic() - stop_started < 1.0, "stopping a stream must be prompt"
    assert abs(sum(written) - elapsed * 32000) < 32000 * 0.35


# --- live frame format -----------------------------------------------------
def test_live_frame_round_trip():
    assert decode_live_frame(encode_live_frame("SER1", 16000, b"\1\2")) == ("SER1", 16000, b"\1\2")


@pytest.mark.parametrize("frame", [b"", b"\x00abc", b"\xff" * 10, b"\x04a;b;\x00\x00\x3e\x80xx", b"\x04SER1\x00\x00\x00\x01xx"])
def test_malformed_live_frames_rejected(frame):
    assert decode_live_frame(frame) is None


def test_ws_url_scheme():
    assert ws_url("https://m.example.com/sdr/") == "wss://m.example.com/sdr/api/nodes/agent/ws"
    assert ws_url("http://100.64.0.2:8642") == "ws://100.64.0.2:8642/api/nodes/agent/ws"


# --- a real master and slave talking over a socket -------------------------
@pytest.fixture
def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def start_master(tmp_path, port, bins):
    cfg = make_config(
        tmp_path / "master", public_url=f"http://127.0.0.1:{port}", serials=[], bin_dirs=[bins],
        extra_origins=["https://sentry.example.com"],
    )
    (tmp_path / "master").mkdir(exist_ok=True)
    ctx = Context(cfg, enumerate_fn=lambda: [])
    app = create_app(ctx, run_background=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    assert await wait_for(lambda: server.started)
    return ctx, app, server, task


async def test_slave_enrols_records_and_uploads_to_master(tmp_path, free_port):
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", FAKE_RTL_FM)
    ctx, app, server, master_task = await start_master(tmp_path, free_port, bins)
    url = f"http://127.0.0.1:{free_port}"
    slave_task = None
    try:
        add_user(ctx, ADMIN_EMAIL, role="admin")
        admin = client_for(app, ADMIN_EMAIL)
        # TestClient addresses the app in-process, so this shares the running ctx.
        response = await asyncio.to_thread(admin.post, "/api/nodes", json={"name": "field-pi"})
        token = response.json()["enrollment_token"]

        (tmp_path / "slave").mkdir()
        cfg = make_config(tmp_path / "slave", role="slave", master_url=url, enrollment_token=token,
                          serials=["FIELD1"], bin_dirs=[bins])
        slave = Slave(cfg, enumerate_fn=lambda: ["FIELD1"])
        slave_task = asyncio.create_task(slave.run())
        assert await wait_for(lambda: ctx.node_links)
        node_id = next(iter(ctx.node_links))
        assert (cfg.data_dir / "node.key").stat().st_mode & 0o077 == 0
        with ctx.db() as session:
            assert session.get(Node, node_id).serials == ["FIELD1"]

        # The enrolment token can't be used a second time.
        again = await asyncio.to_thread(admin.post, "/api/nodes/agent/enroll", json={"token": token})
        assert again.status_code == 403

        # The master's UI configures the slave's SDR, and calls arrive at the master.
        source = (await asyncio.to_thread(admin.post, "/api/sources", json={"node_id": node_id, "serial": "FIELD1", "name": "Airboss"})).json()["id"]
        target = (await asyncio.to_thread(admin.post, "/api/targets", json={"name": "Air Boss", "frequency_hz": 120_000_000, "modulation": "am"})).json()["id"]
        await asyncio.to_thread(admin.post, f"/api/sources/{source}/assign", json={"target_id": target})

        def received():
            with ctx.db() as session:
                return session.query(Recording).count() >= 1
        assert await wait_for(received, 20)
        with ctx.db() as session:
            call = session.query(Recording).first()
            assert (call.node_name, call.target_name, call.source_id) == ("field-pi", "Air Boss", source)
        assert await wait_for(lambda: not list(slave.spool.glob("*.mp3")))
        rows = (await asyncio.to_thread(admin.get, "/api/sources")).json()
        assert rows[0]["node_online"] is True

        # The slave remembers assignments across restarts.
        assert slave.load_assignments()["FIELD1"]["target_name"] == "Air Boss"

        # Revoking cuts the link and the key stops working.
        await asyncio.to_thread(admin.delete, f"/api/nodes/{node_id}")
        assert await wait_for(lambda: not ctx.node_links)
    finally:
        if slave_task:
            slave_task.cancel()
            await asyncio.gather(slave_task, return_exceptions=True)
        server.should_exit = True
        await master_task


def fake_node(ctx, serials=("FIELD1",)):
    from sentrysdr import security
    key = security.new_token()
    with ctx.db() as session:
        node = Node(name="n", status="active", key_hash=security.hash_token(key), serials=list(serials))
        session.add(node)
        session.commit()
    return key


def call_meta(**overrides):
    return json.dumps({
        "id": str(uuid.uuid4()), "serial": "FIELD1", "started_at": 1.0, "ended_at": 2.0, "duration": 1.0, **overrides,
    })


def upload(client, key, meta, audio=b"ID3audio"):
    return client.post("/api/nodes/agent/calls", headers={"Authorization": f"Bearer {key}"},
                       data={"meta": meta}, files={"audio": ("c.mp3", audio, "audio/mpeg")})


def test_call_upload_validation(ctx, app):
    key = fake_node(ctx)
    client = client_for(app)
    assert upload(client, "wrong-key", call_meta()).status_code == 401
    assert upload(client, key, call_meta()).status_code == 200
    assert upload(client, key, call_meta(serial="NOT-MINE")).status_code == 403
    assert upload(client, key, call_meta(id="../../etc/passwd")).status_code == 422
    assert upload(client, key, call_meta(), audio=b"<script>alert(1)</script>").status_code == 422
    assert upload(client, key, "not json").status_code == 422


def test_call_upload_is_idempotent(ctx, app):
    key = fake_node(ctx)
    client = client_for(app)
    meta = call_meta()
    assert upload(client, key, meta).json()["stored"] is True
    assert upload(client, key, meta).json()["stored"] is False
    with ctx.db() as session:
        assert session.query(Recording).count() == 1


def test_expired_enrolment_token_refused(ctx, app):
    from sentrysdr import security
    with ctx.db() as session:
        session.add(Node(name="n", status="pending", enroll_token_hash=security.hash_token("tok"), enroll_expires_at=time.time() - 1))
        session.commit()
    assert client_for(app).post("/api/nodes/agent/enroll", json={"token": "tok"}).status_code == 403


def test_enrolment_guessing_rate_limited(app):
    client = client_for(app)
    codes = [client.post("/api/nodes/agent/enroll", json={"token": f"g{i}"}).status_code for i in range(11)]
    assert codes[:10] == [403] * 10 and codes[10] == 429


def test_slave_ignores_assignment_for_unconfigured_serial(tmp_path):
    cfg = make_config(tmp_path, role="slave", master_url="https://m.example.com", serials=["FIELD1"])
    slave = Slave(cfg, enumerate_fn=lambda: [])
    clean = slave.clean_assignments({"OTHER": spec(), "FIELD1": spec(), "BAD": {"modulation": "x"}})
    assert list(clean) == ["FIELD1"]


def test_slave_spool_limit_drops_oldest(tmp_path):
    cfg = make_config(tmp_path, role="slave", master_url="https://m.example.com", serials=["F"], spool_max_mb=1)
    slave = Slave(cfg, enumerate_fn=lambda: [])
    for index in range(3):
        (slave.spool / f"{index}.mp3").write_bytes(b"x" * 600_000)
        (slave.spool / f"{index}.json").write_text("{}")
    old = slave.spool / "0.mp3"
    import os
    os.utime(old, (1, 1))
    slave._enforce_spool_limit()
    assert not old.exists() and (slave.spool / "2.mp3").exists()
