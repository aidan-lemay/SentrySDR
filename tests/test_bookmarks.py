"""Web tuner bookmarks: each user's own, and in and out of GQRX and SDR++."""
from __future__ import annotations

import json

import pytest

from sentrysdr import bookmarks

from .conftest import add_user, client_for

GQRX_FILE = """# Tag name          ;  color
Untagged            ; #c0c0c0
Marine              ; #0080ff

# Frequency ; Name                     ; Modulation          ;  Bandwidth; Tags
     156800000; Channel 16               ; Narrow FM           ;      10000; Marine
     162550000; NOAA; weather            ; Narrow FM           ;      10000; Untagged
      98100000; Local FM                 ; WFM (stereo)        ;     160000; Untagged
     not-a-number; Broken                ; AM                  ;      10000; Untagged
"""

SDRPP_FILE = json.dumps({"bookmarks": {"Tower": {"frequency": 119_100_000.0, "bandwidth": 8000.0, "mode": 2},
                                       "Bad": {"frequency": "x", "mode": 0}, "Odd mode": {"frequency": 145_500_000, "mode": 99}}})


def test_crud_is_per_user(ctx, app, operator, admin):
    added = operator.post("/api/me/bookmarks", json={"name": "Repeater", "frequency_hz": 146_940_000, "mode": "nfm", "folder": "Ham"})
    assert added.status_code == 201
    bookmark = added.json()
    assert [b["name"] for b in operator.get("/api/me/bookmarks").json()] == ["Repeater"]
    assert admin.get("/api/me/bookmarks").json() == []  # someone else's
    assert admin.put(f"/api/me/bookmarks/{bookmark['id']}", json={"frequency_hz": 1_000_000}).status_code == 404
    assert admin.delete(f"/api/me/bookmarks/{bookmark['id']}").status_code == 404
    edited = operator.put(f"/api/me/bookmarks/{bookmark['id']}", json={"name": "", "frequency_hz": 147_000_000, "mode": "am"})
    assert edited.json()["name"] == "147.0000 MHz" and edited.json()["mode"] == "am"
    assert operator.post("/api/me/bookmarks", json={"frequency_hz": 1, "mode": "nfm"}).status_code == 422
    assert operator.post("/api/me/bookmarks", json={"frequency_hz": 146_000_000, "mode": "jazz"}).status_code == 422
    assert operator.delete(f"/api/me/bookmarks/{bookmark['id']}").json() == {"ok": True}


def test_bookmarks_need_the_tuner_permission(ctx, app, listener):
    assert listener.get("/api/me/bookmarks").status_code == 403


def test_gqrx_import_and_export(operator):
    reply = operator.post("/api/me/bookmarks/import", json={"format": "gqrx", "text": GQRX_FILE})
    assert reply.json() == {"added": 3, "skipped": 0, "left_out": 0}
    items = {b["name"]: b for b in operator.get("/api/me/bookmarks").json()}
    assert items["Channel 16"]["folder"] == "Marine" and items["Channel 16"]["bandwidth_hz"] == 10_000
    assert items["Local FM"]["mode"] == "wfm_stereo"
    assert "NOAA" in items  # a ';' in a name splits GQRX's columns; the rest of the row still comes in
    again = operator.post("/api/me/bookmarks/import", json={"format": "gqrx", "text": GQRX_FILE})
    assert again.json()["added"] == 0 and again.json()["skipped"] == 3
    out = operator.get("/api/me/bookmarks/export?format=gqrx")
    assert out.headers["content-disposition"] == 'attachment; filename="bookmarks.csv"'
    assert "156800000; Channel 16" in out.text and "Marine              ; #c0c0c0" in out.text
    # GQRX reads its own file back the same.
    assert {(b["frequency_hz"], b["mode"]) for b in bookmarks.parse(out.text, "gqrx")} == \
        {(156_800_000, "nfm"), (162_550_000, "nfm"), (98_100_000, "wfm_stereo")}


def test_sdrpp_import_and_export(operator):
    reply = operator.post("/api/me/bookmarks/import", json={"format": "sdrpp", "text": SDRPP_FILE})
    assert reply.json()["added"] == 2
    items = {b["name"]: b for b in operator.get("/api/me/bookmarks").json()}
    assert items["Tower"]["mode"] == "am" and items["Odd mode"]["mode"] == "nfm"
    exported = json.loads(operator.get("/api/me/bookmarks/export?format=sdrpp").text)
    assert exported["lists"]["General"]["bookmarks"]["Tower"] == {"frequency": 119_100_000.0, "bandwidth": 8000.0, "mode": 2}
    # SDR++'s full config shape, with lists as folders.
    full = json.dumps({"lists": {"Air": {"bookmarks": {"ATIS": {"frequency": 127_650_000, "bandwidth": 8000, "mode": 2}}}}})
    operator.post("/api/me/bookmarks/import", json={"format": "sdrpp", "text": full})
    assert next(b for b in operator.get("/api/me/bookmarks").json() if b["name"] == "ATIS")["folder"] == "Air"


def test_own_format_round_trip_and_replace(operator):
    operator.post("/api/me/bookmarks", json={"name": "Keep", "frequency_hz": 462_562_500, "mode": "nfm", "notes": "FRS 1"})
    text = operator.get("/api/me/bookmarks/export").text
    assert json.loads(text)["format"] == "sentrysdr-bookmarks"
    operator.post("/api/me/bookmarks", json={"name": "Gone", "frequency_hz": 100_000_000, "mode": "wfm"})
    reply = operator.post("/api/me/bookmarks/import", json={"format": "sentrysdr", "text": text, "replace": True})
    assert reply.json()["added"] == 1
    assert [(b["name"], b["notes"]) for b in operator.get("/api/me/bookmarks").json()] == [("Keep", "FRS 1")]


@pytest.mark.parametrize("fmt,text", [("sentrysdr", "{nope"), ("sdrpp", "[]"), ("gqrx", "just words"), ("sentrysdr", '{"bookmarks": 5}')])
def test_bad_files_are_refused(operator, fmt, text):
    assert operator.post("/api/me/bookmarks/import", json={"format": fmt, "text": text}).status_code == 422


def test_import_stops_at_the_limit(ctx, app, monkeypatch):
    monkeypatch.setattr(bookmarks, "MAX_BOOKMARKS", 3)
    add_user(ctx, "op2@example.com", role="operator")
    op = client_for(app, "op2@example.com")
    many = json.dumps([{"name": f"B{i}", "frequency_hz": 146_000_000 + i * 25_000, "mode": "nfm"} for i in range(5)])
    assert op.post("/api/me/bookmarks/import", json={"format": "sentrysdr", "text": many}).json() == {"added": 3, "skipped": 0, "left_out": 0}
    assert op.post("/api/me/bookmarks", json={"frequency_hz": 146_000_000, "mode": "nfm"}).status_code == 422


def test_bookmarks_go_when_the_user_does(ctx, operator):
    operator.post("/api/me/bookmarks", json={"frequency_hz": 146_000_000, "mode": "nfm"})
    from sentrysdr.models import User, UserBookmark
    with ctx.db() as session:
        session.delete(session.query(User).filter_by(email="op@example.com").one())
        session.commit()
        assert session.query(UserBookmark).count() == 0
