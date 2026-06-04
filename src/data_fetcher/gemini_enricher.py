"""
Gemini-powered data enrichment module.
Dùng Gemini API (với Google Search grounding) để tìm:
  - Opening Hours
  - Opening Date
  - Closing Date (nếu đã đóng cửa)
  - Category (loại địa điểm)
  - Trạng thái: còn hoạt động hay đã đóng?
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
        return _parse_response(raw_text)

    except Exception as e:
        logger.error(f"Gemini enrichment error: {e}")
        return _empty_result(f"Error: {e}")


def _build_prompt(name: str, address: str) -> str:
    return f"""
You are a POI data researcher. Find accurate information about this business:

**Business**: {name}
**Address**: {address}

Search the web and return ONLY a JSON object with exactly these fields:

{{
  "opening_hours": "VE format string, e.g. 'mo 09:00-21:00; tu-fr 09:00-22:00; sa 10:00-20:00; su 11:00-18:00'",
  "opening_hours_source": "URL where you found the hours",
  "opening_date": "YYYY-MM-DD or YYYY-MM or YYYY (when this location first opened)",
  "opening_date_source": "URL where you found the opening date",
  "is_closed": false,
  "closing_date": null,
  "closing_date_source": null,
  "category": "business category e.g. 'Auto Service', 'Pet Store', 'Home Goods', etc.",
  "status_note": "any important notes about the business status"
}}

Rules:
- opening_hours: Use format "mo" "tu" "we" "th" "fr" "sa" "su". Use ranges like "mo-fr". Time in 24h HH:MM.
- opening_date: Search news, Yelp reviews, Facebook posts for grand opening. Use earliest evidence.
- If business is permanently closed, set is_closed=true and provide closing_date.
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
        # Validate và normalize
        return {
            "opening_hours": data.get("opening_hours", ""),
            "opening_hours_source": data.get("opening_hours_source", ""),
            "opening_date": data.get("opening_date", ""),
            "opening_date_source": data.get("opening_date_source", ""),
            "is_closed": bool(data.get("is_closed", False)),
            "closing_date": data.get("closing_date"),
            "closing_date_source": data.get("closing_date_source"),
            "category": data.get("category", ""),
            "status_note": data.get("status_note", ""),
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
        "closing_date_source": None,
        "category": "",
        "status_note": note,
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
