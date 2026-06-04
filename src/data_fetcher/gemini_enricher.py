"""
Gemini-powered data enrichment module.
Dùng Gemini API (với Google Search grounding) để tìm:
  - Opening Hours
  - Opening Date + confidence level
  - Closing Date + confidence level
  - Category (loại địa điểm)
  - Trạng thái: còn hoạt động hay đã đóng?
  - Site Plan URL (nếu trong Shopping Center)
  - is_in_shopping_center
"""

import json
import logging
import re
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

    Returns dict với keys:
      opening_hours, opening_hours_source,
      opening_date, opening_date_source, opening_date_confidence,
      is_closed, closing_date, closing_date_source, closing_date_confidence,
      category, status_note,
      is_in_shopping_center, shopping_center_name, site_plan_url,
      grounding_urls (list of real URLs from Google Search)
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

        # Fallback: extract URLs từ response text nếu metadata rỗng
        if not grounding_urls:
            grounding_urls = _extract_urls_from_text(raw_text)
            if grounding_urls:
                logger.debug(f"Fallback: found {len(grounding_urls)} URLs in text")
        else:
            logger.debug(f"Grounding metadata: {len(grounding_urls)} URLs")

        result["grounding_urls"] = grounding_urls

        # Gán source bằng keyword matching trên grounding URLs thực tế
        if grounding_urls:
            result["opening_hours_source"] = _find_source(
                grounding_urls,
                ["hour", "schedule", "time", "location", "store", "menu"]
            )
            result["opening_date_source"] = _find_source(
                grounding_urls,
                ["yelp", "facebook", "grand", "open", "news", "press",
                 "announce", "launch", "first", "review"]
            )
            if result["is_closed"]:
                result["closing_date_source"] = _find_source(
                    grounding_urls,
                    ["clos", "shut", "bankrupt", "perma", "gone"]
                )

        return result

    except Exception as e:
        logger.error(f"Gemini enrichment error: {e}")
        return _empty_result(f"Error: {e}")


# ── URL Extraction ─────────────────────────────────────────────────────────────

def _extract_grounding_urls(response) -> list:
    """
    Lấy URL thực sự từ Google Search grounding metadata.
    Thử nhiều đường dẫn attribute vì SDK version có thể khác nhau.
    """
    urls = []
    try:
        for candidate in response.candidates:
            meta = getattr(candidate, "grounding_metadata", None)
            if not meta:
                continue

            # Path 1: grounding_chunks[].web.uri  (SDK v2.x)
            chunks = getattr(meta, "grounding_chunks", None) or []
            for chunk in chunks:
                web = getattr(chunk, "web", None)
                if web:
                    uri = getattr(web, "uri", None)
                    if uri and uri not in urls:
                        urls.append(uri)

            # Path 2: search_entry_point rendered_content (có thể chứa URLs)
            sep = getattr(meta, "search_entry_point", None)
            if sep:
                content = getattr(sep, "rendered_content", "") or ""
                for u in re.findall(r'https?://[^\s"<>]+', content):
                    if u not in urls:
                        urls.append(u)

            # Log search queries để debug
            queries = getattr(meta, "web_search_queries", []) or []
            if queries:
                logger.debug(f"Gemini search queries: {queries}")

    except Exception as e:
        logger.warning(f"Grounding extraction error: {e}")

    return urls


def _extract_urls_from_text(text: str) -> list:
    """Fallback: tìm URLs xuất hiện trong response text."""
    raw = re.findall(r'https?://[^\s"\'<>)\]]+', text)
    seen = set()
    result = []
    for u in raw:
        u = u.rstrip(".,;:)]")
        if u not in seen and len(u) > 20:
            seen.add(u)
            result.append(u)
    return result


def _find_source(urls: list, keywords: list) -> str:
    """Tìm URL phù hợp nhất theo keywords."""
    for url in urls:
        url_lower = url.lower()
        if any(kw in url_lower for kw in keywords):
            return url
    return urls[0] if urls else ""


# ── Prompt ─────────────────────────────────────────────────────────────────────

def _build_prompt(name: str, address: str) -> str:
    return f"""
You are a POI data researcher. Find accurate information about this specific business location:

**Business**: {name}
**Address**: {address}

Search the web and return ONLY a JSON object with exactly these fields:

{{
  "opening_hours": "VE format e.g. 'mo 09:00-21:00; tu-fr 09:00-22:00; sa 10:00-20:00; su 11:00-18:00'",
  "is_closed": false,
  "closing_date": null,
  "closing_date_confidence": "none",
  "category": "business category e.g. 'Auto Service', 'Pet Store', 'Home Goods'",
  "status_note": "any important notes about business status",
  "opening_date": "YYYY-MM-DD or YYYY-MM or YYYY",
  "opening_date_confidence": "none",
  "is_in_shopping_center": false,
  "shopping_center_name": null,
  "site_plan_url": null
}}

Rules:
- opening_hours: days as "mo" "tu" "we" "th" "fr" "sa" "su", ranges like "mo-fr", 24h time HH:MM.
- opening_date: date THIS specific location opened (NOT the brand/chain founding date).
  Search: local news, Yelp first reviews, Facebook grand opening posts, permit records.
- opening_date_confidence:
    "high"   = found explicit news article or press release with exact grand opening date
    "medium" = inferred from first Yelp/Google review, social post, or indirect article
    "low"    = estimated from indirect evidence (chain expansion, permit records)
    "none"   = no evidence found, leave opening_date as null
- closing_date_confidence: same scale.
- is_in_shopping_center: true if inside a mall, strip mall, or shopping center.
- site_plan_url: search "[shopping center name] site plan" or "[shopping center name] leasing map"
  on LoopNet, CBRE, JLL, CoStar. Return direct link to PDF/image floor plan. null if not found.
- DO NOT construct or invent URLs. Only include data you actually found via search.
- Return ONLY the JSON object, no markdown, no explanation.
""".strip()


# ── Parsing ────────────────────────────────────────────────────────────────────

def _parse_response(text: str) -> dict:
    """Parse JSON từ Gemini response."""
    json_match = re.search(r'\{[\s\S]*\}', text)
    if not json_match:
        logger.warning("Không tìm thấy JSON trong Gemini response")
        return _empty_result("No JSON in response")

    try:
        data = json.loads(json_match.group())
        return {
            "opening_hours":            data.get("opening_hours", ""),
            "opening_hours_source":     "",   # filled from grounding metadata
            "opening_date":             data.get("opening_date", ""),
            "opening_date_source":      "",   # filled from grounding metadata
            "opening_date_confidence":  data.get("opening_date_confidence", "none"),
            "is_closed":                bool(data.get("is_closed", False)),
            "closing_date":             data.get("closing_date"),
            "closing_date_source":      "",   # filled from grounding metadata
            "closing_date_confidence":  data.get("closing_date_confidence", "none"),
            "category":                 data.get("category", ""),
            "status_note":              data.get("status_note", ""),
            "is_in_shopping_center":    bool(data.get("is_in_shopping_center", False)),
            "shopping_center_name":     data.get("shopping_center_name"),
            "site_plan_url":            data.get("site_plan_url"),
            "grounding_urls":           [],
        }
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}\nText: {text[:500]}")
        return _empty_result(f"JSON parse error: {e}")


def _empty_result(note: str = "") -> dict:
    return {
        "opening_hours":            "",
        "opening_hours_source":     "",
        "opening_date":             "",
        "opening_date_source":      "",
        "opening_date_confidence":  "none",
        "is_closed":                False,
        "closing_date":             None,
        "closing_date_source":      "",
        "closing_date_confidence":  "none",
        "category":                 "",
        "status_note":              note,
        "is_in_shopping_center":    False,
        "shopping_center_name":     None,
        "site_plan_url":            None,
        "grounding_urls":           [],
    }


# ── Display helpers ────────────────────────────────────────────────────────────

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
