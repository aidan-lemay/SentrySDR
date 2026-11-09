"""Aircraft trails, and how far each ADS-B receiver reaches in each direction."""
from __future__ import annotations

import math
import time
from collections import deque

TRAIL_SECONDS = 600
TRAIL_POINTS = 120
SECTORS = 36  # 10 degrees each
MAX_RANGE_KM = 450  # past this a position is a bad decode, not coverage
EARTH_KM = 6371.0


def distance_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Great-circle distance in km, and the bearing from the first point, in degrees."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    distance = 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    return distance, (math.degrees(math.atan2(y, x)) + 360) % 360


def destination(lat: float, lon: float, bearing: float, km: float) -> tuple[float, float]:
    p1, l1, b, d = math.radians(lat), math.radians(lon), math.radians(bearing), km / EARTH_KM
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return round(math.degrees(p2), 5), round((math.degrees(l2) + 540) % 360 - 180, 5)


def _position(item: dict) -> tuple[float, float] | None:
    lat, lon = item.get("lat"), item.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and -90 <= lat <= 90 and -180 <= lon <= 180:
        return float(lat), float(lon)
    return None


class Trails:
    """Recent positions per aircraft, from every receiver."""

    def __init__(self):
        self.points: dict[str, deque] = {}

    def add(self, aircraft: list[dict], now: float | None = None) -> None:
        now = now or time.time()
        for item in aircraft:
            position = _position(item)
            key = str(item.get("hex") or "")
            if not position or not key:
                continue
            trail = self.points.setdefault(key, deque(maxlen=TRAIL_POINTS))
            if not trail or trail[-1][:2] != position:
                trail.append((*position, now))
        cutoff = now - TRAIL_SECONDS
        for key in [k for k, t in self.points.items() if not t or t[-1][2] < cutoff]:
            del self.points[key]  # gone quiet: forget it

    def get(self, key: str, now: float | None = None) -> list[list[float]]:
        cutoff = (now or time.time()) - TRAIL_SECONDS
        return [[lat, lon] for lat, lon, at in self.points.get(str(key), ()) if at >= cutoff]


def update_sectors(sectors: list[float], home: tuple[float, float], aircraft: list[dict]) -> bool:
    """Widen a receiver's reach with these aircraft. Returns whether anything grew."""
    grew = False
    for item in aircraft:
        position = _position(item)
        if not position:
            continue
        km, bearing = distance_bearing(*home, *position)
        if km > MAX_RANGE_KM:
            continue
        slot = int(bearing // (360 / SECTORS)) % SECTORS
        if km > sectors[slot]:
            sectors[slot], grew = round(km, 1), True
    return grew


def outline(home: tuple[float, float], sectors: list[float]) -> list[list[float]]:
    """The coverage shape: the farthest point seen in each direction heard so far.

    Directions with nothing heard are left out rather than drawn at the receiver,
    which would spike the outline back to the centre (tar1090 draws it this way too).
    """
    step = 360 / SECTORS
    points = [list(destination(*home, i * step + step / 2, km)) for i, km in enumerate(sectors) if km > 0]
    return points if len(points) >= 3 else []
