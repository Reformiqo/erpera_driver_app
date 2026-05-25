import math

from erpera_driver_app.utils.exceptions import InvalidCoordinatesError


def haversine_m(lat1, lon1, lat2, lon2):
    """Return distance in metres between two WGS-84 coordinates."""
    R = 6_371_000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def validate_coords(lat, lon):
    """Raise InvalidCoordinatesError if lat/lon are out of range."""
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        raise InvalidCoordinatesError()
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise InvalidCoordinatesError()
    return lat, lon
