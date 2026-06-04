"""
Gemini-powered data enrichment module.
Dùng Gemini API (với Google Search grounding) để tìm:
  - Opening Hours
  - Opening Date
  - Closing Date (nếu đã đóng cửa)
  - Category (loại địa điểm)
  - Trạng thái: còn hoạt động hay đã đóng?
  - Site Plan URL (nếu trong Shopping Center)
  - is_in_shopping_center: có trong SC không
"""

import json
import logging
import re
from typing import Optional
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


def setup_gemini(api_key: str, model: str = "gemini-2.0-flash"):
    """Khởi tạo Gemini client."""
    client = genai.Client(api_key=api_key)
    return client, model


def enrich_poi(model_tuple, name: str, address: str) -> dict:
    """
    Dùng Gemini Search Grounding để tìm toàn bộ thông tin POI.

    Returns:
        dict với keys:
          - opening_hours: str (VE format: "mo 09:00-21:00; tu-sa 09:00-22:00")
          - opening_hours_source: str (URL nguồn)
          - opening_date: str (YYYY-MM-DD hoặc YYYY-MM hoặc YYYY)
          - opening_date_source: str (URL nguồn)
          - is_closed: bool
          - closing_date: str | None
          - closing_date_source: str | None
          - category: str (tên category tiếng Anh)
          - status_note: str (ghi chú thêm)
          - is_in_shopping_center: bool
          - shopping_center_name: str | None
          - site_plan_url: str | None (link PDF/image mặt bằng SC)
    """
    client, model_name = model_tuple
    prompt = _build_prompt(name, address)

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        raw_text = response.text
        logger.debug(f"Gemini raw response:\n{raw_text}")

        result = _parse_response(raw_text)

        # Lấy URL thực tế từ grounding metadata (Google Search)
        grounding_urls = _extract_grounding_urls(response)
        logger.debug(f"Grounding URLs: {grounding_urls}")

        # Gán source URL từ grounding thực tế
        if grounding_urls:
            result["grounding_urls"] = grounding_urls
            # Tìm URL phù hợp cho OH, OD, CD bằng keyword matching
            result["opening_hours_source"] = _find_source(grounding_urls, ["hour", "schedule", "time", "open", "location", "store"])
            result["opening_date_source"]  = _find_source(grounding_urls, ["open", "grand", "new", "launch", "yelp", "facebook", "news"])
            if result["is_closed"]:
                result["closing_date_source"] = _find_source(grounding_urls, ["clos", "shut", "bankrupt", "perma"])
        else:
            result["grounding_urls"] = []

        return result

    except Exception as e:
        logger.error(f"Gemini enrichment error: {e}")
        return _empty_result(f"Error: {e}")


def _extract_grounding_urls(response) -> list:
    """Lấy danh sách URL thực sự từ Google Search grounding metadata."""
    urls = []
    try:
        for candidate in response.candidates:
            meta = getattr(candidate, "grounding_metadata", None)
            if not meta:
                continue
            chunks = getattr(meta, "grounding_chunks", []) or []
            for chunk in chunks:
                web = getattr(chunk, "web", None)
                if web and getattr(web, "uri", None):
                    uri = web.uri
                    if uri not in urls:
                        urls.append(uri)
    except Exception as e:
        logger.warning(f"Could not extract grounding URLs: {e}")
    return urls


def _find_source(urls: list, keywords: list) -> str:
    """Tìm URL phù hợp nhất trong danh sách grounding URLs theo keywords."""
    for url in urls:
        url_lower = url.lower()
        if any(kw in url_lower for kw in keywords):
            return url
    # Fallback: URL đầu tiên nếu không match
    return urls[0] if urls else ""


def _build_prompt(name: str, address: str) -> str:
    return f"""
You are a POI data researcher. Find accurate information about this business:

**Business**: {name}
**Address**: {address}

Search the web and return ONLY a JSON object with exactly these fields:

{{
  "opening_hours": "VE format string, e.g. 'mo 09:00-21:00; tu-fr 09:00-22:00; sa 10:00-20:00; su 11:00-18:00'",
  "is_closed": false,
  "closing_date": null,
  "category": "business category e.g. 'Auto Service', 'Pet Store', 'Home Goods', etc.",
  "status_note": "any important notes about the business status",
  "opening_date": "YYYY-MM-DD or YYYY-MM or YYYY (when this location first opened, NOT the chain)",
  "is_in_shopping_center": false,
  "shopping_center_name": null,
  "site_plan_url": null
}}

Rules:
- opening_hours: Use format "mo" "tu" "we" "th" "fr" "sa" "su". Use ranges like "mo-fr". Time in 24h HH:MM.
- opening_date: Search news, Yelp first reviews, Facebook posts for grand opening of THIS specific location.
- If business is permanently closed, set is_closed=true and provide closing_date.
- is_in_shopping_center: true if this store is inside a mall, strip mall, or shopping center.
- shopping_center_name: name of the shopping center if applicable.
- site_plan_url: Search "[shopping center name] site plan" on LoopNet, CBRE, JLL, CoStar.
  Return a direct link to a PDF or page with the floor plan. null if not found.
- DO NOT invent or construct URLs. Only use data you actually found.
- Return ONLY the JSON, no other text.
""".strip()


def _parse_response(text: str) -> dict:
    """Parse JSON từ Gemini response."""
    # Tìm JSON block trong response
    json_match = re.search(r'\{[\s\S]*\}', text)
    if not json_match:
        logger.warning("Không tìm thấy JSON trong Gemini response")
        return _empty_result("No JSON in response")

    try:
        data = json.loads(json_match.group())
        return {
            "opening_hours": data.get("opening_hours", ""),
            "opening_hours_source": "",   # populated from grounding metadata
            "opening_date": data.get("opening_date", ""),
            "opening_date_source": "",    # populated from grounding metadata
            "is_closed": bool(data.get("is_closed", False)),
            "closing_date": data.get("closing_date"),
            "closing_date_source": "",    # populated from grounding metadata
            "category": data.get("category", ""),
            "status_note": data.get("status_note", ""),
            "is_in_shopping_center": bool(data.get("is_in_shopping_center", False)),
            "shopping_center_name": data.get("shopping_center_name"),
            "site_plan_url": data.get("site_plan_url"),
            "grounding_urls": [],
        }
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}\nText: {text[:500]}")
        return _empty_result(f"JSON parse error: {e}")


def _empty_result(note: str = "") -> dict:
    return {
        "opening_hours": "",
        "opening_hours_source": "",
        "opening_date": "",
        "opening_date_source": "",
        "is_closed": False,
        "closing_date": None,
        "closing_date_source": "",
        "category": "",
        "status_note": note,
        "is_in_shopping_center": False,
        "shopping_center_name": None,
        "site_plan_url": None,
        "grounding_urls": [],
    }


def format_hours_for_display(oh_string: str) -> str:
    """Convert VE format sang dạng dễ đọc hơn."""
    if not oh_string:
        return "(không có dữ liệu)"
    day_map = {
        "mo": "Mon", "tu": "Tue", "we": "Wed",
        "th": "Thu", "fr": "Fri", "sa": "Sat", "su": "Sun"
    }
    parts = [p.strip() for p in oh_string.split(";")]
    result = []
    for part in parts:
        for short, full in day_map.items():
            part = re.sub(rf'\b{short}\b', full, part)
        result.append(part)
    return " | ".join(result)
