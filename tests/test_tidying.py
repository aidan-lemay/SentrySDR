"""Small conveniences: each user's own Sources page layout, removing revoked nodes, and ACARS by source."""
from __future__ import annotations

from sqlalchemy import select

from sentrysdr.models import Node

from .test_radio import add_message


def test_source_layout_is_each_users_own(admin, listener):
    assert listener.get("/api/me/source-layout").json() == {"items": []}
    layout = {"items": [{"source": 3}, {"group": " North ", "sources": [1, 2], "collapsed": True}, {"group": "Empty"}]}
    saved = listener.put("/api/me/source-layout", json=layout)
    assert saved.status_code == 200
    assert saved.json() == {"items": [{"source": 3}, {"group": "North", "sources": [1, 2], "collapsed": True},
                                      {"group": "Empty", "sources": [], "collapsed": False}]}
    assert listener.get("/api/me/source-layout").json() == saved.json()
    assert admin.get("/api/me/source-layout").json() == {"items": []}  # nobody else's changes


def test_source_layout_is_checked(listener):
    bad = [
        {"items": [{"source": 1}, {"group": "A", "sources": [1]}]},  # a source in two places
        {"items": [{"group": "A"}, {"group": "a"}]},  # the same group twice
        {"items": [{"group": "  "}]},
        {"items": [{"source": 1, "group": "A"}]},  # both at once
        {"items": [{}]},
        {"items": [{"group": "x" * 61}]},
        {"items": [{"group": f"G{i}"} for i in range(51)]},
    ]
    for layout in bad:
        assert listener.put("/api/me/source-layout", json=layout).status_code == 422, layout


def test_a_revoked_node_can_be_removed_from_the_list(ctx, admin, operator):
    with ctx.db() as session:
        session.add(Node(name="Field", status="active", serials=["FIELD1"]))
        session.commit()
        field = session.scalar(select(Node).where(Node.name == "Field")).id
    assert admin.post(f"/api/nodes/{field}/hide").status_code == 409  # revoke it first
    assert admin.post(f"/api/nodes/{ctx.local_node_id}/hide").status_code == 404
    admin.delete(f"/api/nodes/{field}").raise_for_status()
    assert operator.post(f"/api/nodes/{field}/hide").status_code == 403
    assert admin.post(f"/api/nodes/{field}/hide").status_code == 200
    assert field not in [n["id"] for n in admin.get("/api/nodes").json()]
    with ctx.db() as session:
        assert session.get(Node, field).status == "revoked"  # still there for what refers to it
    assert admin.post(f"/api/nodes/{field}/hide").status_code == 404


def test_acars_messages_by_source(ctx, listener):
    add_message(ctx, node_name="Master", serial="A1", source_name="Roof", received_at=1000)
    add_message(ctx, node_name="Master", serial="A1", source_name="Roof ACARS", received_at=2000)  # renamed since
    add_message(ctx, node_name="Field", serial="B1", source_name="Hangar", received_at=1500)
    add_message(ctx, node_name="Field", serial="B2", source_name="", received_at=1600)  # an SDR since removed
    assert listener.get("/api/messages/sources").json() == [
        {"node": "Field", "serial": "B2", "name": "B2", "count": 1},
        {"node": "Field", "serial": "B1", "name": "Hangar", "count": 1},
        {"node": "Master", "serial": "A1", "name": "Roof ACARS", "count": 2},
    ]
    only = listener.get("/api/messages", params={"node": "Master", "serial": "A1"}).json()
    assert only["total"] == 2 and {m["source_name"] for m in only["items"]} == {"Roof", "Roof ACARS"}
    assert listener.get("/api/messages", params={"node": "Field", "serial": "A1"}).json()["total"] == 0
