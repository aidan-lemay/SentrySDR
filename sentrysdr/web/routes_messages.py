"""ACARS messages: the searchable feed, and which aircraft on the map have sent some."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select

from .. import keywords
from ..acars import Registrations, normalize_tail
from ..models import AcarsMessage, User, now
from .deps import ctx_of, get_db, require
from .routes_alerts import important_condition, user_words

router = APIRouter(prefix="/api")

MAP_WINDOW_SECONDS = 3600


def _view(message: AcarsMessage, words: list[str] | None = None) -> dict:
    view = {c.name: getattr(message, c.name) for c in AcarsMessage.__table__.columns}
    view["important"] = bool(message.important)
    view["matches"] = keywords.matches(words or [], message.text)
    return view


@router.get("/messages")
def list_messages(
    user: User = Depends(require("maps")), db=Depends(get_db),
    search: str = Query(default="", max_length=200), tail: str = Query(default="", max_length=16),
    node: str | None = Query(default=None, max_length=120), serial: str | None = Query(default=None, max_length=64),
    show_empty: bool = False, important: bool = False, id: int | None = None,  # one message, from an alert email
    start: float | None = None, end: float | None = None, order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
):
    words = user_words(db, user, "messages")
    conditions = []
    if important:
        conditions.append(important_condition(AcarsMessage.text, AcarsMessage.important, words))
    if id is not None:
        conditions.append(AcarsMessage.id == id)
    elif not show_empty:
        conditions.append(AcarsMessage.has_text.is_(True))
    if tail:
        conditions.append(AcarsMessage.tail == normalize_tail(tail))
    if node is not None:
        conditions.append(AcarsMessage.node_name == node)
    if serial is not None:
        conditions.append(AcarsMessage.serial == serial)
    if start is not None:
        conditions.append(AcarsMessage.received_at >= start)
    if end is not None:
        conditions.append(AcarsMessage.received_at <= end)
    if search:
        # Typed % and _ are literal, as in the recordings search.
        def pattern(text: str) -> str:
            return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        like, tail_like = pattern(search.strip()), pattern(normalize_tail(search))
        conditions.append(or_(
            AcarsMessage.flight.ilike(like, escape="\\"), AcarsMessage.tail.ilike(tail_like, escape="\\"),
            AcarsMessage.text.ilike(like, escape="\\"), AcarsMessage.label.ilike(like, escape="\\"),
            AcarsMessage.target_name.ilike(like, escape="\\"),
        ))
    total = db.scalar(select(func.count()).select_from(AcarsMessage).where(*conditions))
    rows = db.scalars(select(AcarsMessage).where(*conditions)
                      .order_by(*((AcarsMessage.received_at.asc(), AcarsMessage.id.asc()) if order == "asc"
                                  else (AcarsMessage.received_at.desc(), AcarsMessage.id.desc())))
                      .limit(limit).offset(offset))
    return {"items": [_view(m, words) for m in rows], "total": total}


@router.get("/messages/sources")
def message_sources(_user: User = Depends(require("maps")), db=Depends(get_db)):
    """Every SDR messages have come from, to filter by: one per stick on a node, under its latest name."""
    rows = db.execute(select(AcarsMessage.node_name, AcarsMessage.serial, AcarsMessage.source_name,
                             func.max(AcarsMessage.received_at), func.count())
                      .group_by(AcarsMessage.node_name, AcarsMessage.serial, AcarsMessage.source_name))
    found: dict[tuple[str, str], dict] = {}
    for node_name, serial, source_name, last, count in rows:
        item = found.setdefault((node_name, serial), {"node": node_name, "serial": serial, "name": "", "last": 0, "count": 0})
        item["count"] += count
        if last >= item["last"]:  # a renamed SDR shows as it's called now
            item["last"], item["name"] = last, source_name or item["name"]
    items = sorted(found.values(), key=lambda i: ((i["name"] or i["serial"]).lower(), i["node"].lower()))
    return [{"node": i["node"], "serial": i["serial"], "name": i["name"] or i["serial"], "count": i["count"]} for i in items]


def registrations(request: Request) -> Registrations | None:
    state = request.app.state
    directory = getattr(state, "tar1090_dir", None)
    if directory is None:
        return None
    if getattr(state, "registrations", None) is None:
        state.registrations = Registrations(Path(directory) / "db2")
    return state.registrations


def annotate_aircraft(request: Request, db, aircraft: list[dict]) -> list[dict]:
    """Add each aircraft's registration and how many messages it sent in the last hour."""
    lookup = registrations(request)
    if lookup is None or not aircraft:
        return aircraft
    counts = dict(db.execute(
        select(AcarsMessage.tail, func.count()).where(
            AcarsMessage.received_at >= now() - MAP_WINDOW_SECONDS, AcarsMessage.has_text.is_(True), AcarsMessage.tail != "",
        ).group_by(AcarsMessage.tail)
    ).all())
    out = []
    for item in aircraft:
        registration = lookup.lookup(item.get("hex"))
        extra = {"registration": registration} if registration else {}
        count = counts.get(normalize_tail(registration)) if registration else None
        if count:
            extra["acars_count"], extra["acars_tail"] = count, normalize_tail(registration)
        out.append({**item, **extra})
    return out
