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
    added = operator.post("/api/me/bookmarks", json={"name": "Repeater", "frequency_hz": 146_940_000, "mode": "nfm", "folders": ["Ham"]})
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


def test_bookmarks_need_the_tuner_or_listen_live(ctx, app, admin, listener):
    assert listener.get("/api/me/bookmarks").status_code == 200
    me = listener.get("/api/me").json()
    admin.patch(f"/api/admin/users/{me['id']}", json={"perm_overrides": {"listen_live": False}}).raise_for_status()
    assert listener.get("/api/me/bookmarks").status_code == 403


def test_gqrx_import_and_export(operator):
    reply = operator.post("/api/me/bookmarks/import", json={"format": "gqrx", "text": GQRX_FILE})
    assert reply.json() == {"added": 3, "skipped": 0, "left_out": 0}
    items = {b["name"]: b for b in operator.get("/api/me/bookmarks").json()}
    assert items["Channel 16"]["folders"] == ["Marine"] and items["Channel 16"]["bandwidth_hz"] == 10_000
    assert items["Local FM"]["folders"] == []  # GQRX's "Untagged" means no folder
    marine = next(f for f in operator.get("/api/me/bookmark-folders").json() if f["name"] == "Marine")
    assert marine["color"] == "#0080ff" and marine["count"] == 1  # the file's own tag color
    assert items["Local FM"]["mode"] == "wfm_stereo"
    assert "NOAA" in items  # a ';' in a name splits GQRX's columns; the rest of the row still comes in
    again = operator.post("/api/me/bookmarks/import", json={"format": "gqrx", "text": GQRX_FILE})
    assert again.json()["added"] == 0 and again.json()["skipped"] == 3
    out = operator.get("/api/me/bookmarks/export?format=gqrx")
    assert out.headers["content-disposition"] == 'attachment; filename="bookmarks.csv"'
    assert "156800000; Channel 16" in out.text and "Marine              ; #0080ff" in out.text
    # GQRX reads its own file back the same.
    assert {(b["frequency_hz"], b["mode"]) for b in bookmarks.parse(out.text, "gqrx")[0]} == \
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
    assert next(b for b in operator.get("/api/me/bookmarks").json() if b["name"] == "ATIS")["folders"] == ["Air"]


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


def test_folders_are_tags_with_colors(operator):
    one = operator.post("/api/me/bookmarks", json={"name": "Tower", "frequency_hz": 119_100_000, "mode": "am", "folders": ["Air", "Local", "Air", " "]}).json()
    assert one["folders"] == ["Air", "Local"]
    operator.post("/api/me/bookmarks", json={"name": "ATIS", "frequency_hz": 127_650_000, "mode": "am", "folders": ["Air"]})
    folders = {f["name"]: f for f in operator.get("/api/me/bookmark-folders").json()}
    assert folders["Air"]["count"] == 2 and folders["Local"]["count"] == 1
    assert folders["Air"]["color"] != folders["Local"]["color"]  # each new folder takes the next color
    assert operator.put("/api/me/bookmark-folders/Air", json={"color": "#123ABC"}).json()["color"] == "#123abc"
    assert operator.put("/api/me/bookmark-folders/Air", json={"color": "red"}).status_code == 422
    # Renaming moves every bookmark; a name already taken is refused.
    assert operator.put("/api/me/bookmark-folders/Air", json={"name": "Local"}).status_code == 422
    operator.put("/api/me/bookmark-folders/Air", json={"name": "Airband/VHF"}).raise_for_status()
    names = {b["name"]: b["folders"] for b in operator.get("/api/me/bookmarks").json()}
    assert names == {"Tower": ["Airband/VHF", "Local"], "ATIS": ["Airband/VHF"]}
    assert next(f for f in operator.get("/api/me/bookmark-folders").json() if f["name"] == "Airband/VHF")["color"] == "#123abc"
    # Removing a folder keeps its bookmarks.
    operator.delete("/api/me/bookmark-folders/Airband%2FVHF").raise_for_status()
    names = {b["name"]: b["folders"] for b in operator.get("/api/me/bookmarks").json()}
    assert names == {"Tower": ["Local"], "ATIS": []}
    assert operator.put("/api/me/bookmark-folders/Nope", json={"color": "#000000"}).status_code == 404


def test_bookmarks_saved_with_one_folder_still_read(ctx, operator):
    from sentrysdr.models import User, UserBookmark
    with ctx.db() as session:
        me = session.query(User).filter_by(email="op@example.com").one()
        session.add(UserBookmark(user_id=me.id, name="Old", frequency_hz=146_000_000, mode="nfm", folder="Ham"))
        session.commit()
    assert operator.get("/api/me/bookmarks").json()[0]["folders"] == ["Ham"]
    assert operator.get("/api/me/bookmark-folders").json()[0]["name"] == "Ham"


def test_gqrx_round_trip_keeps_several_tags(operator):
    operator.post("/api/me/bookmarks", json={"name": "Both", "frequency_hz": 146_520_000, "mode": "nfm", "folders": ["Ham", "Simplex"]})
    out = operator.get("/api/me/bookmarks/export?format=gqrx").text
    assert out.rstrip().endswith("Ham,Simplex")
    found, colors = bookmarks.parse(out, "gqrx")
    assert found[0]["folders"] == ["Ham", "Simplex"] and set(colors) >= {"Ham", "Simplex"}
    own = json.loads(operator.get("/api/me/bookmarks/export").text)
    assert own["version"] == 2 and set(own["folders"]) == {"Ham", "Simplex"}
