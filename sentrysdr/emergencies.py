"""Aircraft and ship emergencies, spotted in the ADS-B and AIS data every receiver already reports.

ADS-B: the 7500, 7600 and 7700 squawks, and the separate emergency status newer transponders send.
A squawk is read from replies that can be decoded wrongly, so an aircraft must show it on
CONFIRM_UPDATES updates over CONFIRM_SECONDS before it counts.

AIS: search-and-rescue beacons (SART, man overboard, EPIRB) by their MMSI, and the aground and
not-under-command navigation statuses. AIS messages carry a checksum and these values are
deliberate, so they count at once.

One aircraft or ship in an emergency is one event, however many receivers hear it, until the
emergency hasn't been seen for END_SECONDS.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from .models import Source, TrackEmergency

log = logging.getLogger("sentrysdr.emergencies")

CODES = {
    # code: (kind, short label, what it means)
    "squawk_7700": ("adsb", "Squawk 7700", "general emergency"),
    "squawk_7600": ("adsb", "Squawk 7600", "radio failure"),
    "squawk_7500": ("adsb", "Squawk 7500", "unlawful interference (hijack), often a mis-set transponder"),
    "adsb_general": ("adsb", "Emergency", "general emergency, from the transponder's emergency status"),
    "adsb_lifeguard": ("adsb", "Medical", "lifeguard or medical flight in an emergency"),
    "adsb_minfuel": ("adsb", "Minimum fuel", "minimum fuel"),
    "adsb_nordo": ("adsb", "No radio", "no radio contact"),
    "adsb_unlawful": ("adsb", "Unlawful interference", "unlawful interference, from the emergency status"),
    "adsb_downed": ("adsb", "Downed aircraft", "downed aircraft"),
    "ais_sart": ("ais", "AIS-SART", "search-and-rescue transmitter, from a life raft or vessel in distress"),
    "ais_mob": ("ais", "Man overboard", "man-overboard beacon"),
    "ais_epirb": ("ais", "EPIRB", "emergency position-indicating radio beacon"),
    "ais_aground": ("ais", "Aground", "vessel reports being aground"),
    "ais_nuc": ("ais", "Not under command", "vessel reports not being under command; often left set by mistake"),
}
# What a user gets until they choose: everything except the two AIS statuses that are often left on by mistake.
DEFAULT_CODES = [code for code in CODES if code not in ("ais_nuc", "ais_aground")]
DISTANCES_KM = (25, 50, 100, 200)

CONFIRM_UPDATES = 3
CONFIRM_SECONDS = 10
END_SECONDS = 300
REUSE_SECONDS = 600  # after a restart, an event seen this recently carries on rather than alerting again
SAVE_EVERY = 30
ADSB_EMERGENCY = {"general", "lifeguard", "minfuel", "nordo", "unlawful", "downed"}


def codes_for(kind: str, item: dict) -> set[str]:
    found = set()
    if kind == "adsb":
        squawk = str(item.get("squawk") or "")
        if squawk in ("7500", "7600", "7700"):
            found.add(f"squawk_{squawk}")
        status = str(item.get("emergency") or "").lower()
        if status in ADSB_EMERGENCY:
            found.add(f"adsb_{status}")
    else:
        mmsi = str(item.get("mmsi") or "")
        prefix = {"970": "ais_sart", "972": "ais_mob", "974": "ais_epirb"}.get(mmsi[:3]) if len(mmsi) == 9 else None
        if prefix:
            found.add(prefix)
        status = item.get("status")
        if status == 14:
            found.add("ais_sart")  # "AIS-SART active"
        elif status == 6:
            found.add("ais_aground")
        elif status == 2:
            found.add("ais_nuc")
    return found


def ident_for(kind: str, item: dict) -> str:
    return str(item.get("hex") if kind == "adsb" else item.get("mmsi") or "").lower()[:16]


SAME_AS = {"adsb_general": "squawk_7700", "adsb_unlawful": "squawk_7500", "adsb_nordo": "squawk_7600"}


def describe(codes) -> str:
    """Squawks first; a status that says the same as a squawk is left out."""
    codes = set(codes)
    shown = [c for c in CODES if c in codes and SAME_AS.get(c) not in codes]
    def one(code: str) -> str:
        label, meaning = CODES[code][1], CODES[code][2].split(",")[0]
        return label if label.lower() == meaning.lower() else f"{label} ({meaning})"
    return ", ".join(one(c) for c in shown)


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _number(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


@dataclass
class Watch:
    """One aircraft or ship showing an emergency code, before and after it counts."""
    codes: set[str] = field(default_factory=set)
    updates: int = 0
    first_at: float = 0.0
    last_at: float = 0.0
    event_id: int | None = None
    saved_at: float = 0.0


class Emergencies:
    def __init__(self, db):
        self.db = db
        self.watching: dict[tuple[str, str], Watch] = {}

    def active(self, kind: str) -> dict[str, str]:
        """Aircraft or ships in a confirmed emergency now: ident -> what it is, for the map."""
        return {ident: describe(w.codes) for (k, ident), w in self.watching.items() if k == kind and w.event_id}

    def check(self, kind: str, node_id: int, serial: str, items: list[dict], now: float | None = None) -> None:
        now = now or time.time()
        seen = set()
        for item in items:
            ident = ident_for(kind, item)
            codes = codes_for(kind, item)
            if not ident or not codes:
                continue
            seen.add(ident)
            key = (kind, ident)
            watch = self.watching.get(key)
            if watch is None or now - watch.last_at > END_SECONDS:
                watch = self.watching[key] = Watch(first_at=now)
            if now - watch.last_at >= 1 or not watch.updates:
                watch.updates += 1  # several receivers hearing it in the same second count once
            watch.codes |= codes
            watch.last_at = now
            confirmed = kind == "ais" or (watch.updates >= CONFIRM_UPDATES and now - watch.first_at >= CONFIRM_SECONDS)
            try:
                if confirmed and watch.event_id is None:
                    watch.event_id = self._open(kind, ident, item, watch, node_id, serial, now)
                    watch.saved_at = now
                elif watch.event_id is not None and now - watch.saved_at >= SAVE_EVERY:
                    self._update(watch, item, now)
                    watch.saved_at = now
            except Exception:
                log.exception("couldn't save the emergency for %s %s", kind, ident)
        self._expire(kind, seen, now)

    def _expire(self, kind: str, seen: set[str], now: float) -> None:
        for key, watch in list(self.watching.items()):
            if key[0] != kind or key[1] in seen or now - watch.last_at <= END_SECONDS:
                continue
            del self.watching[key]
            if watch.event_id is not None:
                try:
                    with self.db() as session:
                        event = session.get(TrackEmergency, watch.event_id)
                        if event is not None:
                            event.ended_at, event.last_seen = watch.last_at, watch.last_at
                            session.commit()
                except Exception:
                    log.exception("couldn't close emergency %s", watch.event_id)

    def _open(self, kind, ident, item, watch, node_id, serial, now) -> int:
        with self.db() as session:
            recent = session.scalar(select(TrackEmergency).where(
                TrackEmergency.kind == kind, TrackEmergency.ident == ident, TrackEmergency.ended_at.is_(None),
                TrackEmergency.last_seen > now - REUSE_SECONDS).order_by(TrackEmergency.id.desc()))
            if recent is not None:  # the same emergency, from before a restart: no new alert
                recent.codes = sorted(set(recent.codes or []) | watch.codes)
                recent.last_seen = now
                session.commit()
                return recent.id
            source = session.scalar(select(Source).where(Source.node_id == node_id, Source.serial == serial))
            lat, lon = _number(item.get("lat")), _number(item.get("lon"))
            away = distance_km((source.lat, source.lon), (lat, lon)) \
                if source and source.lat is not None and source.lon is not None and lat is not None and lon is not None else None
            name = (item.get("flight") or "").strip() if kind == "adsb" else (item.get("shipname") or "").strip()
            event = TrackEmergency(
                kind=kind, ident=ident, codes=sorted(watch.codes), name=str(name)[:40],
                callsign=str(item.get("registration") or item.get("callsign") or "")[:20],
                node_id=node_id, serial=serial, source_name=source.name if source else serial,
                first_seen=watch.first_at, last_seen=now, lat=lat, lon=lon,
                altitude=_number(item.get("alt_baro")) if kind == "adsb" else None,
                distance_km=round(away, 1) if away is not None else None,
            )
            session.add(event)
            session.commit()
            log.warning("%s emergency: %s %s, %s", kind, ident, name, describe(watch.codes))
            return event.id

    def _update(self, watch: Watch, item: dict, now: float) -> None:
        with self.db() as session:
            event = session.get(TrackEmergency, watch.event_id)
            if event is None:
                return
            event.last_seen = now
            event.codes = sorted(set(event.codes or []) | watch.codes)
            lat, lon = _number(item.get("lat")), _number(item.get("lon"))
            if lat is not None and lon is not None:
                event.lat, event.lon = lat, lon
            if _number(item.get("alt_baro")) is not None:
                event.altitude = item["alt_baro"]
            session.commit()


def wanted(user_codes: list[str] | None) -> set[str]:
    return set(DEFAULT_CODES if user_codes is None else user_codes) & set(CODES)


def matches_user(event: TrackEmergency, codes: set[str], max_km: int | None) -> bool:
    """A user's choice of codes, and how far away they care about. An SDR with no location set can't be measured, so it counts."""
    if not codes & set(event.codes or []):
        return False
    return max_km is None or event.distance_km is None or event.distance_km <= max_km
