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


import re


def _clean_name(name: str) -> str:
    """Xóa ký tự đặc biệt gây lỗi khi query."""
    return re.sub(r"['\"\u2019\u2018]", "", name).strip()


def _strip_suite(address: str) -> str:
    """
    Xóa suite/apartment/unit number khỏi địa chỉ.
    VD: '409 Commack Rd Ste 2, Deer Park, NY' → '409 Commack Rd, Deer Park, NY'
    """
    # Remove Ste/Suite/Apt/Unit/#/Fl patterns
    cleaned = re.sub(
        r'\b(ste|suite|apt|apartment|unit|fl|floor|#)\s*[\w\-]+\b',
        '', address, flags=re.IGNORECASE
    ).strip().strip(',').strip()
    # Collapse multiple commas/spaces
    cleaned = re.sub(r',\s*,', ',', cleaned)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned)
    return cleaned


def _extract_city_state_zip(address: str) -> str:
    """Lấy phần city, state, zip từ địa chỉ."""
    parts = [p.strip() for p in address.split(',')]
    # Thường là phần cuối: City, ST ZIP
    return ', '.join(parts[-2:]) if len(parts) >= 2 else address


def geocode(name: str, address: str) -> Optional[dict]:
    """
    Tìm lat/lng cho một POI với nhiều chiến lược fallback.

    Thứ tự thử:
    1. Tên sạch + địa chỉ đầy đủ
    2. Tên sạch + địa chỉ đã bỏ suite number
    3. Chỉ địa chỉ đã bỏ suite number
    4. Tên + City/State
    """
    clean_name = _clean_name(name)
    clean_addr = _strip_suite(address)

    strategies = [
        (f"{clean_name}, {address}",    "name + full address"),
        (f"{clean_name}, {clean_addr}", "name + stripped address"),
        (clean_addr,                    "stripped address only"),
        (f"{clean_name}, {_extract_city_state_zip(address)}", "name + city/state"),
    ]

    for query, label in strategies:
        result = _search(query)
        if result:
            if label != "name + full address":
                logger.debug(f"Geocoded via: {label}")
            return result

    logger.warning(f"Khong the geocode: {name} | {address}")
    return None


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
