"""Activity over a period: calls per day and hour, per channel and per SDR, and upload results."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from ..models import AcarsMessage, Recording, Source, UploadJob, User
from .deps import ctx_of, effective_permissions, get_db, require

router = APIRouter(prefix="/api")

PERIODS = (7, 30, 90)


@router.get("/stats")
def stats(request: Request, days: int = Query(default=30), user: User = Depends(require("listen_recordings")), db=Depends(get_db)):
    """Counted in the master's time zone, so 'busiest hour' matches the schedules."""
    days = days if days in PERIODS else 30
    ctx = ctx_of(request)
    tz = ctx.cfg.tz
    now = time.time()
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    first_day = today - timedelta(days=days - 1)
    start = first_day.timestamp()

    per_day = {(first_day + timedelta(days=i)).date().isoformat(): [0, 0.0] for i in range(days)}
    heat = [[0] * 24 for _ in range(7)]  # Monday first
    channels: dict[str, list] = {}
    sources: dict[str, float] = {}
    rows = db.execute(select(Recording.started_at, Recording.duration, Recording.target_name, Recording.source_name)
                      .where(Recording.started_at >= start)).all()
    for started, duration, target, source in rows:
        local = datetime.fromtimestamp(started, tz)
        day = per_day.get(local.date().isoformat())
        if day is not None:
            day[0] += 1
            day[1] += duration or 0
        heat[local.weekday()][local.hour] += 1
        channel = channels.setdefault(target or "Unknown target", [0, 0.0])
        channel[0] += 1
        channel[1] += duration or 0
        sources[source or "Unknown SDR"] = sources.get(source or "Unknown SDR", 0.0) + (duration or 0)

    period_seconds = max(1.0, now - start)
    known = {s.name for s in db.scalars(select(Source))}
    result = {
        "days": days, "timezone": ctx.cfg.timezone,
        "per_day": [{"day": d, "calls": c, "seconds": round(s)} for d, (c, s) in per_day.items()],
        "by_hour": heat,
        "channels": sorted(({"name": n, "calls": c, "seconds": round(s)} for n, (c, s) in channels.items()),
                           key=lambda x: x["calls"], reverse=True),
        # How much of the period each SDR spent in a call. Over 100% can't happen for one SDR.
        "sdrs": sorted(({"name": n, "seconds": round(s), "busy": min(1.0, s / period_seconds), "current": n in known}
                        for n, s in sources.items()), key=lambda x: x["busy"], reverse=True),
        "totals": {"calls": len(rows), "seconds": round(sum(r[1] or 0 for r in rows)),
                   "today": per_day[today.date().isoformat()][0],
                   "messages": db.scalar(select(func.count()).select_from(AcarsMessage).where(AcarsMessage.received_at >= start)) or 0},
    }
    if user.role in ("admin", "operator") or "assign" in effective_permissions(user):
        uploads: dict[str, dict] = {}
        for destination, status, detail, count in db.execute(
            select(UploadJob.destination, UploadJob.status, UploadJob.last_error, func.count())
            .join(Recording).where(Recording.started_at >= start)
            .group_by(UploadJob.destination, UploadJob.status, UploadJob.last_error)
        ).all():
            row = uploads.setdefault(destination, {"destination": destination, "sent": 0, "skipped": 0, "failed": 0, "waiting": 0})
            if status == "done":
                row["skipped" if (detail or "").startswith("Skipped") else "sent"] += count
            elif status == "failed":
                row["failed"] += count
            else:
                row["waiting"] += count
        result["uploads"] = sorted(uploads.values(), key=lambda u: u["destination"])
    return result
