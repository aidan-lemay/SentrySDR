"""The radio engine with fake tools, and a real master/slave link over sockets."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import stat
import sys
import textwrap
import time
import tomllib
import uuid
from array import array
from pathlib import Path

import httpx
import pytest
import uvicorn

from sentrysdr import engine, pipelines
from sentrysdr.__main__ import cmd_list_sdrs, cmd_measure
from sentrysdr.config import Config
from sentrysdr.context import Context
from sentrysdr.engine import PcmFramer, Supervisor
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


async def test_huge_stderr_line_does_not_hang_the_tool(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", "#!/usr/bin/env python3\nimport sys,time\nsys.stderr.write('x'*300000);sys.stderr.flush()\nfor i in range(10):\n  sys.stderr.write('y'*100000);sys.stdout.buffer.write(b'A'*3200);sys.stdout.flush();time.sleep(0.02)\ntime.sleep(60)\n")
    calls = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), calls)
    await sup.apply({"SER1": spec()})
    assert await wait_for(lambda: len(calls) == 1)
    await sup.shutdown()


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


# --- PCM framing -----------------------------------------------------------
def test_framer_never_splits_a_sample():
    framer = PcmFramer()
    assert framer(b"\x01\x02\x03") == b"\x01\x02"
    assert framer(b"\x04") == b"\x03\x04"
    assert framer(b"\x05") == b""


def test_stereo_is_mixed_to_mono_across_split_reads():
    framer = PcmFramer(2)
    frames = array("h", [100, 0, 0, 200, 300, 400]).tobytes()
    assert framer(frames[:5]) + framer(frames[5:]) == array("h", [100, 200, 700]).tobytes()


def test_stereo_mix_clips_instead_of_wrapping():
    framer = PcmFramer(2)
    assert array("h", framer(array("h", [30000, 30000, -30000, -30000]).tobytes())).tolist() == [32767, -32768]


@pytest.mark.parametrize("modulation,channels", [("dmr", 2), ("p25p2", 2), ("p25p1", 1), ("ysf", 1), ("nxdn48", 1), ("dstar", 1), ("nfm", 1)])
def test_channel_count_matches_dsd_fme(modulation, channels):
    assert pipelines.pcm_channels({"modulation": modulation}) == channels


def test_flags_match_the_built_tools(tmp_path):
    cfg = make_config(tmp_path)
    assert pipelines.dsd_args(cfg, {"modulation": "dmr"})[-1] == "-fs"
    args = pipelines.ais_args(cfg, 0, {"gain_db": 28.0, "ppm": 2})
    assert "-q" not in args and args[args.index("-o") + 1] == "5"
    assert args[args.index("-gr") + 1:args.index("-gr") + 3] == ["TUNER", "28"]


def test_tool_env_finds_private_libraries(tmp_path):
    env = make_config(tmp_path, bin_dirs=[tmp_path / "v" / "bin"]).tool_env()
    assert env["LD_LIBRARY_PATH"].split(":")[0] == str((tmp_path / "v" / "lib").resolve())


async def test_dmr_pipeline_records_stereo_as_mono(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", "#!/usr/bin/env python3\nimport sys,time\nfor i in range(20):\n  sys.stdout.buffer.write(b'A'*640);sys.stdout.flush();time.sleep(0.02)\ntime.sleep(60)\n")
    install_tool(bins, "dsd-fme", "#!/usr/bin/env python3\nimport sys\nwhile d:=sys.stdin.buffer.read1(4096):\n  sys.stdout.buffer.write(d);sys.stdout.flush()\n")
    calls = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), calls)
    await sup.apply({"SER1": spec(modulation="dmr")})
    assert await wait_for(lambda: len(calls) == 1)
    await sup.shutdown()
    # 20 x 640 bytes of stereo is 3200 frames, or 0.4 s at 8 kHz. Mono handling would double it.
    assert abs(calls[0][0]["duration"] - 0.4) < 0.01


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


# --- a proxy's size limit must never destroy a recording ---------------------
def spooled_slave(tmp_path, names):
    cfg = Config(role="slave", master_url="https://sentry.example.com", data_dir=tmp_path / "s",
                 recordings_dir=tmp_path / "s" / "rec", log_dir=tmp_path / "s" / "logs")
    slave = Slave(cfg, enumerate_fn=lambda: [])
    for name in names:
        (slave.spool / f"{name}.json").write_text("{}")
        (slave.spool / f"{name}.mp3").write_bytes(b"audio")
    return slave


async def run_upload(slave, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await slave.upload_spool(client, "key")


@pytest.mark.asyncio
async def test_proxy_413_keeps_the_call_and_lets_the_queue_through(tmp_path):
    slave = spooled_slave(tmp_path, ["a-big", "b-small"])

    def handler(request):
        if b'filename="a-big.mp3"' in request.read():
            return httpx.Response(413, headers={"content-type": "text/html"}, text="<html>413</html>")
        return httpx.Response(200, json={"ok": True})

    await run_upload(slave, handler)
    assert (slave.spool / "a-big.mp3").exists() and (slave.spool / "a-big.json").exists()
    assert not (slave.spool / "b-small.mp3").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [413, 422])
async def test_master_rejection_still_discards_the_call(tmp_path, status):
    slave = spooled_slave(tmp_path, ["bad"])
    await run_upload(slave, lambda request: httpx.Response(status, json={"detail": "no"}))
    assert not (slave.spool / "bad.mp3").exists()


@pytest.mark.asyncio
async def test_kept_call_uploads_once_the_proxy_limit_is_raised(tmp_path):
    slave = spooled_slave(tmp_path, ["big"])
    await run_upload(slave, lambda request: httpx.Response(413, text="too big"))
    assert (slave.spool / "big.mp3").exists()
    await run_upload(slave, lambda request: httpx.Response(200, json={"ok": True}))
    assert not (slave.spool / "big.mp3").exists()


# --- a squelch that never closes ----------------------------------------------
# Writes steadily for a while, then goes quiet as if the squelch closed.
FAKE_STUCK_OPEN = """\
#!/usr/bin/env python3
import sys, time
for _ in range(30):
    sys.stdout.buffer.write(b"\\x10\\x00" * 800); sys.stdout.flush(); time.sleep(0.05)
time.sleep(60)
"""


async def run_stuck(tmp_path, monkeypatch, **spec_overrides):
    monkeypatch.setattr(engine, "SQUELCH_STUCK_SECONDS", 0.3)
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", FAKE_STUCK_OPEN)
    calls, statuses = [], {}
    # A short maximum proves the warning spans the splits, which never close the squelch.
    cfg = make_config(tmp_path, bin_dirs=[bins], max_call_seconds=0.2)
    sup = supervisor(cfg, calls, statuses=statuses)
    try:
        await sup.apply({"SER1": spec(**spec_overrides)})
        assert await wait_for(lambda: any(not s["in_call"] and len(calls) >= 2 for s in statuses.get("SER1", [])))
        await wait_for(lambda: statuses["SER1"][-1]["error"] == "", timeout=3)
    finally:
        history = [s["error"] for s in statuses["SER1"]]
        await sup.shutdown()
    return history


async def test_warns_when_squelch_stays_open_then_clears(tmp_path, monkeypatch):
    history = await run_stuck(tmp_path, monkeypatch, squelch=60)
    assert engine.SQUELCH_STUCK_WARNING in history
    assert history[-1] == ""


async def test_no_warning_for_continuous_recording(tmp_path, monkeypatch):
    assert engine.SQUELCH_STUCK_WARNING not in await run_stuck(tmp_path, monkeypatch, squelch=0)


# --- the measure command -------------------------------------------------------
def measure_cfg(tmp_path, tool):
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", tool)
    return make_config(tmp_path, bin_dirs=[bins], serials=["SER1"])


FAKE_RAW = """\
#!/usr/bin/env python3
import sys, time
for _ in range(100):
    sys.stdout.buffer.write(b"\\x40\\x00\\xc0\\xff" * 800); sys.stdout.flush(); time.sleep(0.02)
"""


def test_measure_prints_a_suggestion(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sentrysdr.sdr.enumerate_serials", lambda: ["SER1"])
    cfg = measure_cfg(tmp_path, FAKE_RAW)
    assert cmd_measure(cfg, "SER1", 444.85, "nfm", None, 0, 1.0) == 0
    out = capsys.readouterr().out
    assert "typical 64" in out and "Suggested squelch: 128" in out


def test_measure_refuses_an_unlisted_sdr(tmp_path, capsys):
    assert cmd_measure(measure_cfg(tmp_path, FAKE_RAW), "OTHER", 444.85, "nfm", None, 0, 1.0) == 2
    assert "sdr.serials" in capsys.readouterr().err


def test_measure_explains_a_busy_sdr(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sentrysdr.sdr.enumerate_serials", lambda: ["SER1"])
    cfg = measure_cfg(tmp_path, "#!/bin/sh\necho 'usb_claim_interface error -6' >&2\nexit 1\n")
    assert cmd_measure(cfg, "SER1", 444.85, "nfm", None, 0, 1.0) == 1
    err = capsys.readouterr().err
    assert "clear it or stop the service" in err and "usb_claim_interface" in err


# --- measuring pauses a worker and hands the SDR back ----------------------------
# Raw mode gets constant noise. Anything else stays open, like a squelch on a live call.
FAKE_MODES = """\
#!/usr/bin/env python3
import sys, time
if "raw" in sys.argv:
    for _ in range(200):
        sys.stdout.buffer.write(b"\\x40\\x00\\xc0\\xff" * 800); sys.stdout.flush(); time.sleep(0.02)
    sys.exit(0)
for _ in range(1000):
    sys.stdout.buffer.write(b"\\x10\\x00" * 800); sys.stdout.flush(); time.sleep(0.05)
"""
MEASURE = {"frequency_hz": 441_000_000, "modulation": "nfm", "gain_db": 19.2, "ppm": 0}


def measuring_sup(tmp_path, monkeypatch, tool=FAKE_MODES):
    monkeypatch.setattr(engine, "MEASURE_SECONDS", 1.2)
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", tool)
    calls, statuses = [], {}
    return supervisor(make_config(tmp_path, bin_dirs=[bins]), calls, statuses=statuses), calls, statuses


async def test_measure_saves_the_call_then_resumes_the_assignment(tmp_path, monkeypatch):
    sup, calls, statuses = measuring_sup(tmp_path, monkeypatch)
    try:
        await sup.apply({"SER1": spec()})
        assert await wait_for(lambda: statuses.get("SER1") and statuses["SER1"][-1]["in_call"])
        await asyncio.sleep(0.8)
        result = await sup.measure("SER1", **MEASURE)
        assert result["typical"] == 64 and result["suggested"] == 128 and result["moved"] is False
        assert "measuring" in [s["state"] for s in statuses["SER1"]]
        assert len(calls) == 1  # the interrupted call was kept
        assert await wait_for(lambda: statuses["SER1"][-1]["state"] == "listening" and statuses["SER1"][-1]["in_call"])
    finally:
        await sup.shutdown()


async def test_measure_leaves_an_unassigned_sdr_idle(tmp_path, monkeypatch):
    sup, _, statuses = measuring_sup(tmp_path, monkeypatch)
    try:
        await sup.measure("SER1", **MEASURE)
        assert statuses["SER1"][-1]["state"] == "idle" and sup.workers["SER1"].task is None
    finally:
        await sup.shutdown()


async def test_assignment_changing_during_a_measurement_waits_for_it(tmp_path, monkeypatch):
    sup, _, statuses = measuring_sup(tmp_path, monkeypatch)
    try:
        running = asyncio.create_task(sup.measure("SER1", **MEASURE))
        assert await wait_for(lambda: sup.workers["SER1"].measuring)
        await sup.apply({"SER1": spec(frequency_hz=444_850_000)})
        assert sup.workers["SER1"].task is None  # nothing else may open the stick yet
        await running
        assert sup.workers["SER1"].spec["frequency_hz"] == 444_850_000
        assert await wait_for(lambda: statuses["SER1"][-1]["state"] == "listening")
    finally:
        await sup.shutdown()


async def test_measuring_twice_at_once_is_refused(tmp_path, monkeypatch):
    sup, _, _ = measuring_sup(tmp_path, monkeypatch)
    try:
        running = asyncio.create_task(sup.measure("SER1", **MEASURE))
        assert await wait_for(lambda: sup.workers["SER1"].measuring)
        with pytest.raises(engine.MeasureError, match="busy"):
            await sup.measure("SER1", **MEASURE)
        await running
    finally:
        await sup.shutdown()


async def test_measure_retries_while_the_stick_is_released(tmp_path, monkeypatch):
    marker = tmp_path / "tried"
    tool = FAKE_MODES.replace('if "raw" in sys.argv:', f"""import os
if not os.path.exists({str(marker)!r}):
    open({str(marker)!r}, "w").close()
    sys.stderr.write("usb_claim_interface error\\n")
    sys.exit(1)
if "raw" in sys.argv:""")
    sup, _, _ = measuring_sup(tmp_path, monkeypatch, tool)
    try:
        assert (await sup.measure("SER1", **MEASURE))["typical"] == 64
    finally:
        await sup.shutdown()


async def test_measure_reports_why_it_failed_and_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "MEASURE_ATTEMPTS", 2)
    tool = "#!/bin/sh\nif echo \"$*\" | grep -q raw; then echo 'usb_claim_interface error -6' >&2; exit 1; fi\nsleep 60\n"
    sup, _, statuses = measuring_sup(tmp_path, monkeypatch, tool)
    try:
        await sup.apply({"SER1": spec()})
        assert await wait_for(lambda: statuses.get("SER1") and statuses["SER1"][-1]["state"] == "listening")
        with pytest.raises(engine.MeasureError, match="usb_claim_interface"):
            await sup.measure("SER1", **MEASURE)
        assert await wait_for(lambda: statuses["SER1"][-1]["state"] == "listening")
    finally:
        await sup.shutdown()


async def test_measure_refuses_a_serial_the_node_does_not_own(tmp_path, monkeypatch):
    sup, _, _ = measuring_sup(tmp_path, monkeypatch)
    with pytest.raises(engine.MeasureError, match="sdr.serials"):
        await sup.measure("NOT-MINE", **MEASURE)


async def test_slave_answers_a_measure_request_and_rejects_bad_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "MEASURE_SECONDS", 1.2)
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", FAKE_MODES)
    cfg = make_config(tmp_path, role="slave", master_url="https://sentry.example.com", bin_dirs=[bins], serials=["FIELD1"])
    slave = Slave(cfg, enumerate_fn=lambda: ["FIELD1"])
    slave.outgoing = asyncio.Queue()
    try:
        await slave.handle_message({"t": "measure", "id": "a1", "serial": "FIELD1", **MEASURE})
        reply = json.loads(await asyncio.wait_for(slave.outgoing.get(), 15))
        assert reply["t"] == "measure_result" and reply["id"] == "a1" and reply["ok"] and reply["result"]["typical"] == 64
        for bad in ({"serial": "OTHER", **MEASURE}, {"serial": "FIELD1", **MEASURE, "modulation": "x; rm"}):
            await slave.handle_message({"t": "measure", "id": "a2", **bad})
            reply = json.loads(await asyncio.wait_for(slave.outgoing.get(), 15))
            assert reply["ok"] is False and reply["error"]
    finally:
        await slave.supervisor.shutdown()


# --- stopping a slave keeps the call in progress ---------------------------------
RUN_SLAVE = """\
import asyncio, sys
from pathlib import Path
from sentrysdr.config import Config
from sentrysdr.__main__ import run_slave
from sentrysdr.slave import Slave

root = Path(sys.argv[1])
cfg = Config(role="slave", master_url="http://127.0.0.1:9", enrollment_token="unused", serials=["FIELD1"],
             data_dir=root / "data", recordings_dir=root / "rec", log_dir=root / "logs", bin_dirs=[root / "bin"])
slave = Slave(cfg, enumerate_fn=lambda: ["FIELD1"])
try:
    asyncio.run(run_slave(slave))
except asyncio.CancelledError:
    pass
"""
FAKE_ON_AIR = """\
#!/usr/bin/env python3
import os, sys, time
open(sys.argv[0] + ".pid", "w").write(str(os.getpid()))
while True:
    sys.stdout.buffer.write(b"\\x10\\x00" * 800); sys.stdout.flush(); time.sleep(0.05)
"""


async def test_sigterm_spools_the_call_in_progress_and_stops_the_tool(tmp_path):
    install_tool(tmp_path / "bin", "rtl_fm", FAKE_ON_AIR)
    data = tmp_path / "data"
    data.mkdir()
    (data / "assignments.json").write_text(json.dumps({"FIELD1": spec()}))
    env = {**os.environ, "PYTHONPATH": str(Path(engine.__file__).parent.parent)}
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", RUN_SLAVE, str(tmp_path), env=env)
    pid_file = tmp_path / "bin" / "rtl_fm.pid"
    try:
        assert await wait_for(pid_file.exists, timeout=20)
        await asyncio.sleep(1.5)
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), 30) == 0
    finally:
        if process.returncode is None:
            process.kill()
    spooled = list((data / "spool").glob("*.json"))
    assert len(spooled) == 1 and spooled[0].with_suffix(".mp3").stat().st_size > 0
    assert json.loads(spooled[0].read_text())["duration"] > 0.5
    tool = int(pid_file.read_text())
    assert await wait_for(lambda: not Path(f"/proc/{tool}").exists())


async def test_stopping_a_slave_cancels_a_measurement_without_reopening_the_stick(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "MEASURE_SECONDS", 30)
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", FAKE_MODES)
    cfg = make_config(tmp_path, role="slave", master_url="http://127.0.0.1:9", enrollment_token="unused",
                      bin_dirs=[bins], serials=["FIELD1"])
    slave = Slave(cfg, enumerate_fn=lambda: ["FIELD1"])
    slave.save_assignments({"FIELD1": spec()})
    running = asyncio.create_task(slave.run())
    worker = slave.supervisor.workers["FIELD1"]
    assert await wait_for(lambda: worker.task is not None)
    await slave.handle_message({"t": "measure", "id": "a", "serial": "FIELD1", **MEASURE})
    assert await wait_for(lambda: worker.measuring)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not worker.measuring and worker.task is None


# --- changing volume keeps the same radio session ---------------------------------
async def test_changing_volume_applies_without_retuning(tmp_path):
    install_tool(tmp_path / "bin", "rtl_fm", FAKE_ON_AIR)
    chunks, calls, statuses = [], [], {}
    cfg = make_config(tmp_path, bin_dirs=[tmp_path / "bin"])
    sup = supervisor(cfg, calls, pcm=lambda serial, data, rate: chunks.append(data), statuses=statuses)
    try:
        await sup.apply({"SER1": spec()})
        assert await wait_for(lambda: len(chunks) > 2)
        pid = (tmp_path / "bin" / "rtl_fm.pid").read_text()
        assert chunks[-1][:2] == b"\x10\x00"
        await sup.apply({"SER1": spec(volume_db=12)})
        chunks.clear()
        assert await wait_for(lambda: len(chunks) > 40)
        assert chunks[-1][:2] != b"\x10\x00"  # DC is removed once boosted
        assert (tmp_path / "bin" / "rtl_fm.pid").read_text() == pid
    finally:
        await sup.shutdown()


# --- list-sdrs ---------------------------------------------------------------------
def listed_serials(output):
    line = next(l for l in output.splitlines() if l.startswith("serials = "))
    return tomllib.loads(line)["serials"]


def test_list_sdrs_prints_a_line_to_paste_into_the_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sentrysdr.sdr.enumerate_serials", lambda: ["00000001", "18150304"])
    assert cmd_list_sdrs(make_config(tmp_path, serials=["18150304"])) == 0
    out = capsys.readouterr().out
    assert "0: 00000001  [not configured, will not be used]" in out
    assert "1: 18150304  [configured]" in out
    assert "To add all of them" in out and "[sdr]" in out
    assert listed_serials(out) == ["00000001", "18150304"]


def test_list_sdrs_leaves_out_serials_the_app_would_refuse(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sentrysdr.sdr.enumerate_serials", lambda: ["DUP", "DUP", "", "GOOD"])
    cmd_list_sdrs(make_config(tmp_path))
    out = capsys.readouterr().out
    assert listed_serials(out) == ["GOOD"] and "rtl_eeprom -s" in out


def test_list_sdrs_with_nothing_usable_prints_no_line(tmp_path, monkeypatch, capsys):
    for devices in ([], ["DUP", "DUP"]):
        monkeypatch.setattr("sentrysdr.sdr.enumerate_serials", lambda: devices)
        cmd_list_sdrs(make_config(tmp_path))
        assert "serials = " not in capsys.readouterr().out


# --- a stick that didn't open cleanly ---------------------------------------
@pytest.mark.parametrize("line,flagged", [
    ("rtlsdr_write_reg failed with -7", True), ("Resetting device...", True),
    ("WARNING: Failed to set sample rate.", True), ("Found Rafael Micro R828D tuner", False),
])
async def test_usb_fault_lines(line, flagged):
    stream = asyncio.StreamReader()
    stream.feed_data(f"{line}\n".encode()); stream.feed_eof()
    seen = []
    await engine._log_stderr("S", stream, [], lambda: seen.append(1))
    assert bool(seen) is flagged


async def test_errors_while_exiting_are_ignored():
    stream = asyncio.StreamReader()
    stream.feed_data(b"Signal caught, exiting!\nUser cancel, exiting...\nrtlsdr_demod_write_reg failed with -9\nrtlsdr_write_reg failed with -7\n")
    stream.feed_eof()
    seen = []
    await engine._log_stderr("S", stream, [], lambda: seen.append(1))
    assert seen == []

def resetting_tool(starts, resets_on):
    """A fake rtl_fm that prints librtlsdr's reset message on the given starts (1-based)."""
    return f"""#!/usr/bin/env python3
import sys, time
with open({str(starts)!r}, "a") as f: f.write("x")
n = len(open({str(starts)!r}).read())
if n in {sorted(resets_on)!r}:
    sys.stderr.write("rtlsdr_write_reg failed with -7\\nResetting device...\\n"); sys.stderr.flush()
time.sleep(60)
"""


@pytest.fixture
def fast_reset(monkeypatch):
    monkeypatch.setattr(engine, "RESET_SETTLE_SECONDS", 0.2)


async def test_reset_on_open_restarts_once_and_notes_it(tmp_path, fast_reset):
    bins, starts = tmp_path / "bin", tmp_path / "starts"
    install_tool(bins, "rtl_fm", resetting_tool(starts, {1}))
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), [])
    worker = sup.workers["SER1"]
    try:
        await sup.apply({"SER1": spec()})
        assert await wait_for(lambda: starts.exists() and len(starts.read_text()) == 2)
        assert await wait_for(lambda: worker.state == "listening")
        await asyncio.sleep(1.5)
        assert len(starts.read_text()) == 2  # one restart, then left alone
        assert worker.error == "" and "so it was restarted" in worker.note and "USB errors" in worker.note
        assert worker.snapshot()["note"] == worker.note
    finally:
        await sup.shutdown()


async def test_reset_every_open_doesnt_loop(tmp_path, fast_reset):
    bins, starts = tmp_path / "bin", tmp_path / "starts"
    install_tool(bins, "rtl_fm", resetting_tool(starts, {1, 2, 3, 4, 5}))
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), [])
    worker = sup.workers["SER1"]
    try:
        await sup.apply({"SER1": spec()})
        assert await wait_for(lambda: "twice in a row" in worker.note)
        await asyncio.sleep(2.0)
        assert len(starts.read_text()) == 2 and worker.state == "listening"
    finally:
        await sup.shutdown()


async def test_new_assignment_clears_the_note(tmp_path, fast_reset):
    bins, starts = tmp_path / "bin", tmp_path / "starts"
    install_tool(bins, "rtl_fm", resetting_tool(starts, {1}))
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), [])
    worker = sup.workers["SER1"]
    try:
        await sup.apply({"SER1": spec()})
        assert await wait_for(lambda: worker.note)
        await sup.apply({"SER1": spec(frequency_hz=145_000_000)})
        assert await wait_for(lambda: worker.state == "listening" and worker.note == "")
    finally:
        await sup.shutdown()


async def test_reset_note_reaches_the_sources_page(ctx, admin):
    admin.post("/api/sources", json={"serial": "SER1", "name": "S"}).raise_for_status()
    ctx.status[(ctx.local_node_id, "SER1")] = {"state": "listening", "error": "", "note": "Reset itself on start"}
    assert admin.get("/api/sources").json()[0]["note"] == "Reset itself on start"


# --- detecting a stick's tuner --------------------------------------------------
FAKE_RTL_TEST = """#!/usr/bin/env python3
import sys
sys.stderr.write("Found 1 device(s):\\nUsing device 0: Generic RTL2832U OEM\\nFound Rafael Micro R828D tuner\\n")
sys.stderr.write("Supported gain values (3): 0.0 19.7 49.6 \\nNo E4000 tuner found, aborting.\\n")
"""


async def test_detect_pauses_reads_and_resumes(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_test", FAKE_RTL_TEST)
    install_tool(bins, "rtl_fm", "#!/bin/sh\nsleep 60\n")
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), [])
    worker = sup.workers["SER1"]
    try:
        await sup.apply({"SER1": spec()})
        assert await wait_for(lambda: worker.state == "listening")
        found = await sup.measure("SER1", op="detect")
        assert found == {"tuner": "R828D", "gain_steps": [0.0, 19.7, 49.6], "freq_min_hz": 24_000_000, "freq_max_hz": 1_766_000_000}
        assert await wait_for(lambda: worker.state == "listening")
    finally:
        await sup.shutdown()


async def test_detect_with_no_tuner_found_is_an_error(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_test", "#!/bin/sh\necho 'usb_claim_interface error -6'\n")
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), [])
    try:
        with pytest.raises(engine.MeasureError, match="usb_claim_interface"):
            await sup.measure("SER1", op="detect")
    finally:
        await sup.shutdown()


async def test_slave_answers_a_detect_request(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_test", FAKE_RTL_TEST)
    cfg = make_config(tmp_path, role="slave", master_url="https://sentry.example.com", bin_dirs=[bins], serials=["FIELD1"])
    slave = Slave(cfg, enumerate_fn=lambda: ["FIELD1"])
    slave.outgoing = asyncio.Queue()
    try:
        await slave.handle_message({"t": "measure", "id": "d1", "serial": "FIELD1", "op": "detect"})
        reply = json.loads(await asyncio.wait_for(slave.outgoing.get(), 15))
        assert reply["ok"] and reply["result"]["tuner"] == "R828D"
    finally:
        await slave.supervisor.shutdown()


# --- ACARS ------------------------------------------------------------------------
FAKE_ACARSDEC = """#!/usr/bin/env python3
import json, sys, time
open(sys.argv[0] + ".args", "w").write(" ".join(sys.argv[1:]))
print("not json")
print("x" * 70000)
print(json.dumps({"freq": 131.55, "tail": ".N123UA", "text": "HELLO", "label": "H1"}))
sys.stdout.flush()
time.sleep(60)
"""


async def test_acars_worker_passes_messages_on(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "acarsdec", FAKE_ACARSDEC)
    got = []
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), [], tracks=lambda kind, serial, data: got.append((kind, serial, data)))
    try:
        await sup.apply({"SER1": spec(kind="acars", modulation="acars", frequency_hz=130_025_000, squelch=0,
                                      channels=[130_025_000, 131_550_000], gain_db=40.2)})
        assert await wait_for(lambda: got)
        await asyncio.sleep(0.3)
    finally:
        await sup.shutdown()
    assert len(got) == 1  # the junk lines are skipped
    kind, serial, data = got[0]
    assert kind == "acars" and serial == "SER1" and data["target_id"] == 1 and data["message"]["tail"] == ".N123UA"
    args = (bins / "acarsdec.args").read_text()
    assert args == "--output json:file:path=- --rtlsdr 0 -g 40.2 130.025 131.550"


# --- feed connection checks -------------------------------------------------------
def test_established_connections_are_found_and_sorted(tmp_path):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    accepted, _ = server.accept()
    try:
        cfg = make_config(tmp_path)
        sup = supervisor(cfg, [])
        worker = sup.workers["SER1"]
        # In this process the client looks like an outbound feed, and the accepted side like a local reader.
        worker._check_feeds(os.getpid(), {"adsbx": {"127.0.0.1"}, "adsb_fi": {"192.0.2.1"}}, port)
        assert worker.feed_status["aggregators"] == {"adsbx": True, "adsb_fi": False}
        assert worker.feed_status["local_clients"] == 1
        assert worker.snapshot()["feeds"] == worker.feed_status
    finally:
        for s in (client, accepted, server):
            s.close()



# --- what slaves send is checked ---------------------------------------------------
def test_aircraft_data_is_cleaned_and_capped(ctx):
    from sentrysdr import context as context_module
    hostile = {"hex": "a00001", "flight": "<img src=x onerror=alert(1)>", "lat": 43.1, "lon": float("nan"),
               "alt_baro": 1e300, "bad key!": 1, "nested": {"deep": {"deeper": {"x": 1}}}, "list": list(range(100))}
    ctx.update_tracks("adsb", 7, "S", {"aircraft": [hostile] + [{"hex": f"{i:06x}"} for i in range(context_module.MAX_TRACK_ITEMS + 50)]})
    items = ctx.current_tracks("adsb")
    assert len(items) <= context_module.MAX_TRACK_ITEMS
    clean = next(i for i in items if i.get("hex") == "a00001")
    assert "<" not in clean["flight"] and ">" not in clean["flight"]
    assert clean["lon"] is None and clean["alt_baro"] is None and "bad key!" not in clean
    assert clean["nested"] == {"deep": None}  # only two levels kept; readsb's own lists are one level down
    assert len(clean["list"]) == 32


def test_ship_names_keep_apostrophes(ctx):
    ctx.update_tracks("ais", 7, "S", {"vessels": [{"mmsi": 1, "shipname": "O'BRIEN\x00 & SONS"}]})
    assert ctx.current_tracks("ais")[0]["shipname"] == "O'BRIEN & SONS"


def test_acars_floods_are_capped(ctx, monkeypatch):
    from sentrysdr import context as context_module
    monkeypatch.setattr(context_module, "ACARS_LIMIT_PER_MINUTE", 5)
    stored = []
    monkeypatch.setattr(ctx, "ingest_acars", lambda *args: stored.append(args))
    for i in range(20):
        ctx.update_tracks("acars", 7, "S", {"message": {"text": str(i)}})
    assert len(stored) == 5


def test_slave_times_are_checked(ctx):
    from sentrysdr.web.routes_nodes import handle_agent_message, _epoch
    handle_agent_message(ctx, 7, ["S"], {"t": "status", "sources": {"S": {"state": "x", "last_call_at": "nan", "call_started": "<b>"}}})
    assert ctx.status[(7, "S")]["last_call_at"] is None and ctx.status[(7, "S")]["call_started"] is None
    assert _epoch(1.7e9) == 1.7e9 and _epoch(float("inf")) is None and _epoch(-5) is None


async def test_call_being_split_survives_a_shutdown(tmp_path, caplog):
    """A split call was saved twice when a retune or shutdown landed mid-save, and lost."""
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", "#!/usr/bin/env python3\nimport sys,time\nwhile True:\n  sys.stdout.buffer.write(b'\\1\\0'*1600);sys.stdout.flush();time.sleep(0.05)\n")
    calls = []
    cfg = make_config(tmp_path, bin_dirs=[bins], max_call_seconds=1)
    sup = supervisor(cfg, calls)
    await sup.apply({"SER1": spec()})
    assert await wait_for(lambda: len(calls) >= 2)
    before = len(calls)
    await sup.shutdown()
    assert len(calls) == before + 1  # the call in progress was saved too
    assert "encoding failed" not in caplog.text and "failed to save" not in caplog.text
    assert list((cfg.recordings_dir / ".tmp").glob("*.raw")) == []


def test_app_never_uses_asyncio_wait_for():
    """On Python 3.11 (Raspberry Pi OS) it can lose a cancellation, so a stop or retune hangs. Use aio.wait_for."""
    root = Path(__file__).resolve().parent.parent / "sentrysdr"
    offenders = [str(p) for p in root.rglob("*.py") if p.name != "aio.py" and "asyncio.wait_for(" in p.read_text()]
    assert offenders == []


# --- rtl_tcp ------------------------------------------------------------------------------
FAKE_RTL_TCP = """#!/usr/bin/env python3
import socket, sys, time
open(sys.argv[0] + ".args", "w").write(" ".join(sys.argv[1:]))
host, port = sys.argv[sys.argv.index("-a") + 1], int(sys.argv[sys.argv.index("-p") + 1])
server = socket.socket(); server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); server.bind((host, port)); server.listen(1)
while True:
    conn, _ = server.accept()
    while conn.recv(1024):
        pass
    conn.close()
"""


async def test_rtl_tcp_worker_reports_clients(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "RTL_TCP_CHECK_SECONDS", 0.2)
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_tcp", FAKE_RTL_TCP)
    probe = socket.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()
    cfg = make_config(tmp_path, bin_dirs=[bins], rtl_tcp_bind="127.0.0.1")
    sup = supervisor(cfg, [])
    worker = sup.workers["SER1"]
    try:
        await sup.apply({"SER1": spec(kind="rtltcp", modulation="rtltcp", frequency_hz=162_550_000, squelch=0, rtl_tcp_port=port)})
        assert await wait_for(lambda: worker.state == "listening")
        assert worker.rtl_tcp == {"address": f"127.0.0.1:{port}", "client": False}
        await asyncio.sleep(0.5)
        client = socket.create_connection(("127.0.0.1", port))
        assert await wait_for(lambda: worker.rtl_tcp.get("client") is True, 5)
        assert worker.snapshot()["rtl_tcp"]["client"] is True
        client.close()
        assert await wait_for(lambda: worker.rtl_tcp.get("client") is False, 5)
        assert (bins / "rtl_tcp.args").read_text() == f"-a 127.0.0.1 -p {port} -d 0 -f 162550000"
    finally:
        await sup.shutdown()


async def test_going_idle_clears_old_feed_and_rtl_tcp_status(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_tcp", FAKE_RTL_TCP)
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), [])
    worker = sup.workers["SER1"]
    try:
        await sup.apply({"SER1": spec(kind="rtltcp", modulation="rtltcp", frequency_hz=162_550_000, squelch=0, rtl_tcp_port=31251)})
        assert await wait_for(lambda: worker.rtl_tcp)
        worker.feed_status = {"aggregators": {"adsbx": False}}
        await sup.apply({"SER1": None})
        assert worker.state == "idle" and worker.snapshot()["rtl_tcp"] == {} and worker.snapshot()["feeds"] == {}
    finally:
        await sup.shutdown()


# --- an unplugged stick, and restarting by hand ---------------------------------------------
HANGING_TOOL = """#!/usr/bin/env python3
import os, sys, time
open(sys.argv[0] + ".pids", "a").write(str(os.getpid()) + "\\n")
time.sleep(100000)  # like a decoder that hangs instead of exiting when its stick is pulled
"""


async def test_unplugged_stick_stops_a_hung_tool_and_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "PRESENCE_SECONDS", 0.2)
    monkeypatch.setattr(engine, "RESTART_MIN", 0.3)
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", HANGING_TOOL)
    plugged = {"SER1"}
    cfg = make_config(tmp_path, bin_dirs=[bins])
    sup = Supervisor(cfg, lambda *a: None, lambda *a: None, lambda *a: None, lambda *a: None,
                     enumerate_fn=lambda: sorted(plugged), present_fn=lambda: set(plugged))
    worker = sup.workers["SER1"]
    pids = lambda: (bins / "rtl_fm.pids").read_text().split() if (bins / "rtl_fm.pids").exists() else []
    try:
        await sup.apply({"SER1": spec()})
        assert await wait_for(lambda: worker.state == "listening" and len(pids()) == 1)
        plugged.clear()  # pulled out
        assert await wait_for(lambda: worker.state == "error" and worker.error in engine.UNPLUGGED, 5)
        first = int(pids()[0])
        assert await wait_for(lambda: not os.path.exists(f"/proc/{first}") or open(f"/proc/{first}/stat").read().split()[2] == "Z", 5)
        plugged.add("SER1")  # plugged back in
        assert await wait_for(lambda: worker.state == "listening" and len(pids()) == 2, 5)
        assert worker.error == ""
    finally:
        await sup.shutdown()


async def test_presence_unknown_never_counts_as_unplugged(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "PRESENCE_SECONDS", 0.1)
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", HANGING_TOOL)
    sup = Supervisor(make_config(tmp_path, bin_dirs=[bins]), lambda *a: None, lambda *a: None, lambda *a: None, lambda *a: None,
                     enumerate_fn=lambda: ["SER1"], present_fn=lambda: None)  # not Linux: can't tell
    worker = sup.workers["SER1"]
    try:
        await sup.apply({"SER1": spec()})
        assert await wait_for(lambda: worker.state == "listening")
        await asyncio.sleep(0.6)
        assert worker.state == "listening"
    finally:
        await sup.shutdown()


async def test_restart_starts_the_tool_again(tmp_path):
    bins = tmp_path / "bin"
    install_tool(bins, "rtl_fm", HANGING_TOOL)
    sup = supervisor(make_config(tmp_path, bin_dirs=[bins]), [])
    worker = sup.workers["SER1"]
    pids = lambda: (bins / "rtl_fm.pids").read_text().split() if (bins / "rtl_fm.pids").exists() else []
    try:
        await sup.apply({"SER1": spec()})
        assert await wait_for(lambda: len(pids()) == 1)
        await sup.restart("SER1")
        assert await wait_for(lambda: len(pids()) == 2 and worker.state == "listening")
        with pytest.raises(engine.MeasureError):
            await sup.restart("NOT-MINE")
    finally:
        await sup.shutdown()


async def test_real_sysfs_reading(tmp_path):
    from sentrysdr import sdr
    for name, vendor, product, serial in (("1-1", "0bda", "2838", "AAA"), ("1-2", "0bda", "2832", "BBB"), ("1-3", "046d", "c52b", "MOUSE"), ("1-4", "0bda", "2838", None)):
        d = tmp_path / name; d.mkdir()
        (d / "idVendor").write_text(vendor + "\n"); (d / "idProduct").write_text(product + "\n")
        if serial:
            (d / "serial").write_text(serial + "\n")
    assert sdr.present_serials(str(tmp_path)) == {"AAA", "BBB"}
    assert sdr.present_serials(str(tmp_path / "missing")) is None


async def test_slave_obeys_restart_messages(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, role="slave", master_url="https://sentry.example.com", serials=["FIELD1"])
    slave = Slave(cfg, enumerate_fn=lambda: ["FIELD1"])
    restarted, killed = [], []
    async def fake_restart(serial):
        restarted.append(serial)
    monkeypatch.setattr(slave.supervisor, "restart", fake_restart)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(sig))
    await slave.handle_message({"t": "restart", "serial": "FIELD1"})
    await slave.handle_message({"t": "restart", "serial": "NOT-MINE"})
    await slave.handle_message({"t": "restart_service"})
    await asyncio.sleep(1.3)
    assert restarted == ["FIELD1"] and killed == [signal.SIGTERM]
    await slave.supervisor.shutdown()
