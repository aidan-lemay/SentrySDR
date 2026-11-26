"""ClearCut and RDIO systems, set up like Broadcastify's: one server address in settings,
then systems with their own ID and API key, which each target picks with its talkgroup."""
from __future__ import annotations

import re
import sqlite3

import httpx
import pytest
from sqlalchemy import select

from sentrysdr import uploaders
from sentrysdr.models import Recording, Setting, Target, UploadJob, UploadSystem, make_session_factory
from sentrysdr.security import SecretBox

from .test_radio import make_target, run_uploads, store, upload_system


def field(body: str, name: str) -> str | None:
    found = re.search(rf'name="{name}"\r\n\r\n([^\r]*)\r\n', body)
    return found.group(1) if found else None


async def test_each_rdio_system_sends_its_own_id_and_key(ctx, tmp_path, admin):
    admin.patch("/api/admin/settings", json={"rdio_server": "https://rdio.example.com"})
    one = make_target(admin, "A", rdio_enabled=True, rdio_system_ref=upload_system(admin, "rdio", 11, "key-a"), rdio_talkgroup=100)
    two = make_target(admin, "B", hz=445_000_000, rdio_enabled=True, rdio_system_ref=upload_system(admin, "rdio", 22, "key-b"), rdio_talkgroup=200)
    store(ctx, tmp_path, target_id=one)
    store(ctx, tmp_path, target_id=two)
    seen = []

    def handler(request):
        seen.append(request.read().decode(errors="replace"))
        return httpx.Response(200, text="Call imported successfully")

    assert await run_uploads(ctx, handler) == 2
    sent = sorted((field(b, "system"), field(b, "key"), field(b, "talkgroup")) for b in seen)
    assert sent == [("11", "key-a", "100"), ("22", "key-b", "200")]


async def test_clearcut_uses_the_systems_key_and_remembers_its_id_for_transcripts(ctx, tmp_path, admin, monkeypatch):
    admin.patch("/api/admin/settings", json={"clearcut_server": "https://cc.example.com"})
    system = upload_system(admin, "clearcut", "us-ny-monroe", "cc-key")
    target = make_target(admin, clearcut_enabled=True, clearcut_system_ref=system, clearcut_id=1077)
    call = store(ctx, tmp_path, target_id=target)
    with ctx.db() as session:
        assert session.get(Recording, call).clearcut_system == "us-ny-monroe"
    sent = []

    async def fake_send(cfg, client, server, api_key, recording, target, audio):
        sent.append((server, api_key, target.clearcut_id))

    monkeypatch.setattr(uploaders, "send_clearcut", fake_send)
    assert await run_uploads(ctx, lambda request: httpx.Response(200)) == 1
    assert sent == [("https://cc.example.com", "cc-key", 1077)]
    with ctx.db() as session:
        assert session.scalar(select(UploadJob)).status == "done"


def test_a_target_must_pick_a_system_of_the_right_service(admin):
    bcfy = admin.post("/api/admin/upload-systems", json={"name": "B", "system_id": 5, "api_key": "k"}).json()["id"]
    rdio = upload_system(admin, "rdio", 5)
    body = {"name": "x", "frequency_hz": 444_000_000, "rdio_enabled": True, "rdio_talkgroup": 1}
    assert admin.post("/api/targets", json={**body, "rdio_system_ref": bcfy}).status_code == 422
    assert admin.post("/api/targets", json={**body, "rdio_system_ref": rdio}).status_code == 201
    assert admin.post("/api/targets", json={**body, "kind": "adsb", "modulation": "adsb", "rdio_system_ref": rdio,
                                            "frequency_hz": 1_090_000_000}).status_code == 422


def test_system_ids_are_checked_per_service(admin):
    post = lambda **b: admin.post("/api/admin/upload-systems", json={"name": "s", "api_key": "k", **b}).status_code  # noqa: E731
    assert post(service="rdio", system_id="abc") == 422
    assert post(service="rdio", system_id=0) == 422
    assert post(service="rdio", system_id="9" * 80) == 422
    assert post(service="broadcastify", system_id="us-ny") == 422
    assert post(service="clearcut", system_id="has space") == 422
    assert post(service="clearcut", system_id="us-ny-monroe") == 201
    assert post(service="clearcut", system_id="us-ny-monroe") == 409  # already added
    assert post(service="rdio", system_id=7) == 201
    assert post(service="bogus", system_id=7) == 422
    listed = {(s["service"], s["system_id"]) for s in admin.get("/api/admin/upload-systems").json()}
    assert listed == {("clearcut", "us-ny-monroe"), ("rdio", 7)}


def test_a_system_keeps_its_service_and_key_when_edited(admin, ctx):
    system = upload_system(admin, "clearcut", "old", "secret")
    assert admin.put(f"/api/admin/upload-systems/{system}", json={"service": "rdio", "name": "n", "system_id": 3}).status_code == 422
    assert admin.put(f"/api/admin/upload-systems/{system}", json={"service": "clearcut", "name": "n", "system_id": "new"}).status_code == 200
    with ctx.db() as session:
        saved = session.get(UploadSystem, system)
        assert (saved.code, ctx.secrets.open(saved.api_key_sealed)) == ("new", "secret")


def test_a_system_in_use_cannot_be_removed(admin):
    system = upload_system(admin, "clearcut", "sys")
    target = make_target(admin, clearcut_enabled=True, clearcut_system_ref=system, clearcut_id=5)
    reply = admin.delete(f"/api/admin/upload-systems/{system}")
    assert reply.status_code == 409 and "K1ABC" in reply.json()["detail"]
    admin.delete(f"/api/targets/{target}").raise_for_status()
    assert admin.delete(f"/api/admin/upload-systems/{system}").status_code == 200


def test_only_broadcastify_has_a_credentials_check(admin):
    system = upload_system(admin, "rdio", 3)
    assert admin.post(f"/api/admin/upload-systems/{system}/test").status_code == 409


def test_settings_no_longer_take_upload_keys(admin):
    admin.patch("/api/admin/settings", json={"rdio_api_key": "ignored"})
    settings = admin.get("/api/admin/settings").json()
    assert "rdio_api_key_set" not in settings and "clearcut_api_key_set" not in settings


@pytest.fixture
def old_database(tmp_path):
    """A database from before: one key each in settings, and system IDs typed on targets."""
    db = tmp_path / "old.db"
    make_session_factory(db)
    box = SecretBox(tmp_path / "secret.key")
    connection = sqlite3.connect(db)
    insert = ("INSERT INTO targets (name, kind, frequency_hz, modulation, squelch, clearcut_enabled, clearcut_system, clearcut_id, "
              "rdio_enabled, rdio_system, rdio_talkgroup) VALUES (?, 'audio', 441000000, 'nfm', 0, ?, ?, ?, ?, ?, ?)")
    connection.executemany(insert, [
        ("A", 1, "us-ny-monroe", 10, 1, 11, 100),
        ("B", 1, "us-ny-monroe", 20, 1, 22, 200),
        ("C", 0, None, None, 1, 11, 300),
    ])
    connection.executemany("INSERT INTO settings (key, value) VALUES (?, ?)",
                           [("rdio_api_key", box.seal("rdio-key")), ("clearcut_api_key", box.seal("cc-key"))])
    connection.execute("INSERT INTO broadcastify_systems (name, system_id, api_key_sealed) VALUES ('Home', 4242, 'x')")
    connection.commit()
    connection.close()
    return db, box


def test_upgrade_turns_the_old_keys_into_systems(old_database):
    db, box = old_database
    factory = make_session_factory(db)
    make_session_factory(db)  # twice: the second run must change nothing
    with factory() as session:
        systems = {(s.kind, s.label): s for s in session.scalars(select(UploadSystem))}
        assert set(systems) == {("broadcastify", 4242), ("clearcut", "us-ny-monroe"), ("rdio", 11), ("rdio", 22)}
        assert box.open(systems[("rdio", 11)].api_key_sealed) == "rdio-key"
        assert box.open(systems[("clearcut", "us-ny-monroe")].api_key_sealed) == "cc-key"
        targets = {t.name: t for t in session.scalars(select(Target))}
        assert targets["A"].clearcut_system_ref == targets["B"].clearcut_system_ref == systems[("clearcut", "us-ny-monroe")].id
        assert targets["A"].rdio_system_ref == targets["C"].rdio_system_ref == systems[("rdio", 11)].id
        assert targets["B"].rdio_system_ref == systems[("rdio", 22)].id
        assert (targets["A"].clearcut_id, targets["B"].rdio_talkgroup) == (10, 200)
        assert all(t.clearcut_system is None and t.rdio_system is None for t in targets.values())
        assert session.scalar(select(Setting).where(Setting.key.in_(["rdio_api_key", "clearcut_api_key"]))) is None


def test_upgrade_without_saved_keys_makes_systems_that_say_so(tmp_path):
    db = tmp_path / "nokey.db"
    make_session_factory(db)
    connection = sqlite3.connect(db)
    connection.execute("INSERT INTO targets (name, kind, frequency_hz, modulation, squelch, clearcut_enabled, rdio_enabled, rdio_system, rdio_talkgroup) "
                       "VALUES ('A', 'audio', 441000000, 'nfm', 0, 0, 1, 5, 9)")
    connection.commit()
    connection.close()
    with make_session_factory(db)() as session:
        system = session.scalar(select(UploadSystem))
        assert (system.kind, system.label, system.api_key_sealed) == ("rdio", 5, "")
