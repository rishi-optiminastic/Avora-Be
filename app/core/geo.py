"""Geo helpers for location-restricted clock-in — pure functions, no I/O."""

from __future__ import annotations

import math

_EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/long points, in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def is_valid_coord(lat: float | None, lon: float | None) -> bool:
    """A usable WGS-84 coordinate (rejects None and out-of-range/spoofed values)."""
    return (
        lat is not None
        and lon is not None
        and -90.0 <= lat <= 90.0
        and -180.0 <= lon <= 180.0
        and not (lat == 0.0 and lon == 0.0)  # null island — almost always a bad fix
    )
