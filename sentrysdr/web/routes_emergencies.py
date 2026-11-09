"""Aircraft and ship emergencies: the record of them, and each user's choice of which to be told about."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .. import emergencies
from ..models import TrackEmergency, User
from .deps import current_user, get_db, require

router = APIRouter(prefix="/api")


def emergency_view(event: TrackEmergency) -> dict:
    return {
        "id": event.id, "kind": event.kind, "ident": event.ident, "codes": event.codes or [],
        "what": emergencies.describe(event.codes or []),
        "title": " ".join(filter(None, (event.name, event.callsign))) or event.ident.upper(),
        "name": event.name, "callsign": event.callsign, "source_name": event.source_name,
        "first_seen": event.first_seen, "last_seen": event.last_seen, "ended_at": event.ended_at,
        "lat": event.lat, "lon": event.lon, "altitude": event.altitude, "distance_km": event.distance_km,
    }


@router.get("/emergencies")
def list_emergencies(kind: str = Query(default="", pattern=r"^(adsb|ais)?$"),
                     limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                     _user: User = Depends(require("maps")), db=Depends(get_db)):
    """Every one recorded, newest first: the record isn't filtered by anyone's choices."""
    conditions = [TrackEmergency.kind == kind] if kind else []
    total = db.scalar(select(func.count()).select_from(TrackEmergency).where(*conditions))
    rows = db.scalars(select(TrackEmergency).where(*conditions).order_by(TrackEmergency.first_seen.desc())
                      .limit(limit).offset(offset))
    return {"items": [emergency_view(e) for e in rows], "total": total}


@router.get("/me/emergency-alerts")
def get_choices(user: User = Depends(current_user)):
    chosen = emergencies.wanted(user.emergency_codes)
    return {
        "codes": [{"code": code, "kind": kind, "label": label, "meaning": meaning, "chosen": code in chosen}
                  for code, (kind, label, meaning) in emergencies.CODES.items()],
        "km": user.emergency_km, "distances": list(emergencies.DISTANCES_KM), "email": bool(user.email_emergencies),
    }


class ChoicesBody(BaseModel):
    codes: list[str] = Field(max_length=len(emergencies.CODES))
    km: int | None = None
    email: bool = False


@router.put("/me/emergency-alerts")
def set_choices(body: ChoicesBody, user: User = Depends(require("maps")), db=Depends(get_db)):
    unknown = set(body.codes) - set(emergencies.CODES)
    if unknown:
        raise HTTPException(422, f"Unknown codes: {', '.join(sorted(unknown))}")
    if body.km is not None and body.km not in emergencies.DISTANCES_KM:
        raise HTTPException(422, f"Choose {', '.join(map(str, emergencies.DISTANCES_KM))} km, or anywhere")
    user = db.merge(user)
    user.emergency_codes, user.emergency_km, user.email_emergencies = sorted(set(body.codes)), body.km, body.email or None
    db.commit()
    return get_choices(user)
