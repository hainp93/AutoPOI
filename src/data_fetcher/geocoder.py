"""
Geocoder module — dùng Nominatim (OSM, hoàn toàn miễn phí)
để lấy lat/lng từ tên + địa chỉ POI.
"""

import time
import requests
from typing import Optional
import logging

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {
    "User-Agent": "AutoPOI/1.0 (POI data entry automation; contact: autopoi@local)"
}


def geocode(name: str, address: str) -> Optional[dict]:
    """
    Tìm lat/lng cho một POI.

    Args:
        name: Tên địa điểm, ví dụ "Valvoline Instant Oil Change"
        address: Địa chỉ đầy đủ, ví dụ "1867 College Ave, Elmira, NY 14901"

    Returns:
        dict với keys: lat, lon, display_name, importance
        hoặc None nếu không tìm thấy
    """
    # Thử search cả tên + địa chỉ trước
    query = f"{name}, {address}"
    result = _search(query)

    if result:
        return result

    # Fallback: chỉ search địa chỉ
    logger.warning(f"Không tìm thấy '{query}', thử chỉ địa chỉ...")
    result = _search(address)
    return result


def _search(query: str) -> Optional[dict]:
    """Gọi Nominatim API."""
    params = {
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "us",  # chỉ US
        "addressdetails": 1,
    }

    try:
        time.sleep(1.1)  # Nominatim rate limit: max 1 req/giây
        resp = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            return None

        r = data[0]
        return {
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "display_name": r.get("display_name", ""),
            "importance": r.get("importance", 0),
        }

    except Exception as e:
        logger.error(f"Geocoding error: {e}")
        return None


def build_ve_url(lat: float, lon: float, zoom: int = 19) -> str:
    """
    Tạo URL mở Venue Editor tại đúng tọa độ.
    Format: https://venues-prod.placer.team/#map={zoom}/{lat}/{lon}
    """
    return f"https://venues-prod.placer.team/#map={zoom}/{lat:.6f}/{lon:.6f}"


def build_gm_url(lat: float, lon: float) -> str:
    """Tạo link Google Maps."""
    return f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}"


def build_gm_search_url(name: str, address: str) -> str:
    """Tạo link Google Maps search."""
    query = f"{name} {address}".replace(" ", "+")
    return f"https://www.google.com/maps/search/{query}"
