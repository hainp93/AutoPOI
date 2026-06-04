"""
Gemini-powered data enrichment module — Multi-Step Search.

Thay vì 1 prompt lớn, dùng 3 bước tìm kiếm riêng biệt:
  Step 1: Official website + Opening Hours + Status + Category
  Step 2: Opening Date / Closing Date (grand opening news, Yelp)
  Step 3: Shopping Center + Site Plan
"""

import json
import logging
import re
from typing import Iterator
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


def setup_gemini(api_key: str, model: str = "gemini-2.0-flash"):
    """Khởi tạo Gemini client."""
    client = genai.Client(api_key=api_key)
    return client, model


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def enrich_poi_stream(model_tuple, name: str, address: str) -> Iterator[dict]:
    """
    Tìm kiếm POI theo 3 bước, yield từng kết quả trung gian.

    Yields dicts:
      {"step": 1, "status": "running", "label": "..."}
      {"step": 1, "status": "done",    "label": "...", "partial": {...}}
      ...
      {"step": "final", "data": {full_result}}
    """
    client, model_name = model_tuple
    result = _empty_result()

    # ── Step 1: Hours + Status + Category ─────────────────────────────────────
    yield {"step": 1, "status": "running",
           "label": "Tìm giờ mở cửa, danh mục và website chính thức..."}
    try:
        s1 = _step1_hours_status_category(client, model_name, name, address)
        result.update(s1)
        yield {"step": 1, "status": "done",
               "label": "Tìm được giờ mở cửa",
               "partial": {k: result[k] for k in
                           ["opening_hours", "opening_hours_source",
                            "is_closed", "closing_date", "category",
                            "official_website", "grounding_urls"]}}
    except Exception as e:
        logger.error(f"Step 1 error: {e}")
        yield {"step": 1, "status": "error", "label": f"Step 1 lỗi: {e}"}

    # ── Step 2: Opening Date / Closing Date ───────────────────────────────────
    yield {"step": 2, "status": "running",
           "label": "Tìm ngày khai trương và ngày đóng cửa..."}
    try:
        s2 = _step2_dates(client, model_name, name, address,
                          official_website=result.get("official_website", ""))
        result.update(s2)
        # Merge grounding URLs
        for u in s2.get("grounding_urls", []):
            if u not in result["grounding_urls"]:
                result["grounding_urls"].append(u)
        yield {"step": 2, "status": "done",
               "label": "Tìm được ngày",
               "partial": {k: result[k] for k in
                           ["opening_date", "opening_date_source",
                            "opening_date_confidence", "closing_date",
                            "closing_date_source", "closing_date_confidence",
                            "suggested_searches"]}}
    except Exception as e:
        logger.error(f"Step 2 error: {e}")
        yield {"step": 2, "status": "error", "label": f"Step 2 lỗi: {e}"}

    # ── Step 3: Shopping Center + Site Plan ───────────────────────────────────
    yield {"step": 3, "status": "running",
           "label": "Kiểm tra Shopping Center và Site Plan..."}
    try:
        s3 = _step3_shopping_center(client, model_name, name, address)
        result.update(s3)
        for u in s3.get("grounding_urls", []):
            if u not in result["grounding_urls"]:
                result["grounding_urls"].append(u)
        yield {"step": 3, "status": "done",
               "label": "Hoàn tất",
               "partial": {k: result[k] for k in
                           ["is_in_shopping_center", "shopping_center_name",
                            "site_plan_url"]}}
    except Exception as e:
        logger.error(f"Step 3 error: {e}")
        yield {"step": 3, "status": "error", "label": f"Step 3 lỗi: {e}"}

    yield {"step": "final", "data": result}


def enrich_poi(model_tuple, name: str, address: str) -> dict:
    """Backward-compat wrapper — chạy tuần tự, trả về result cuối."""
    result = _empty_result()
    for event in enrich_poi_stream(model_tuple, name, address):
        if event.get("step") == "final":
            result = event["data"]
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Step Implementations
# ─────────────────────────────────────────────────────────────────────────────

def _step1_hours_status_category(client, model_name: str,
                                  name: str, address: str) -> dict:
    """
    Step 1: Tìm opening hours, trạng thái, category từ website chính thức.
    Focus: Tìm trang web của location cụ thể này, không phải chain tổng.
    """
    prompt = f"""
You are a POI researcher. Find accurate business information for this specific location:

Business: {name}
Address: {address}

Search for: "{name} {address} hours" and "{name} official website location"

Return ONLY a JSON object:
{{
  "opening_hours": "VE format: 'mo 09:00-21:00; tu-fr 09:00-22:00; sa 10:00-20:00; su 11:00-18:00' or null",
  "opening_hours_source": "exact URL of the page where you found the hours (must be a real URL you visited)",
  "is_closed": false,
  "closing_date": null,
  "category": "specific category e.g. 'Auto Service', 'Swimming Pool Supplies', 'Pet Store'",
  "official_website": "URL of the official business website or location page, or null"
}}

Rules:
- opening_hours format: days as mo/tu/we/th/fr/sa/su, ranges like mo-fr, 24h time HH:MM.
- Prefer the official website or Google Business listing over aggregators.
- If this specific location is permanently closed, set is_closed=true.
- official_website: prefer the location-specific page (e.g. chain.com/stores/city-state) over homepage.
- Return ONLY JSON, no markdown.
""".strip()

    response = _call_gemini(client, model_name, prompt)
    raw = response.text
    data = _parse_json(raw)
    urls = _extract_grounding_urls(response) or _extract_urls_from_text(raw)

    return {
        "opening_hours":        data.get("opening_hours", "") or "",
        "opening_hours_source": _pick_source(urls, data.get("opening_hours_source", ""),
                                             ["hour", "schedule", "time", "store", "location"]),
        "is_closed":            bool(data.get("is_closed", False)),
        "closing_date":         data.get("closing_date"),
        "closing_date_source":  "",
        "closing_date_confidence": "none",
        "category":             data.get("category", "") or "",
        "official_website":     data.get("official_website", "") or "",
        "grounding_urls":       urls,
    }


def _step2_dates(client, model_name: str, name: str, address: str,
                 official_website: str = "") -> dict:
    """
    Step 2: Tìm opening date từ news, Yelp, Facebook.
    Dùng official_website từ Step 1 nếu có để narrow down search.
    """
    city_state = _extract_city_state(address)
    extra = f"\nThe official website is: {official_website}" if official_website else ""
    prompt = f"""
You are a POI researcher. Find when this specific location FIRST OPENED (grand opening date):

Business: {name}
Address: {address}
City/State: {city_state}{extra}

Search for:
1. "{name} {city_state} grand opening" or "{name} {city_state} opened"
2. Yelp page for this location - check the first review date
3. Local news articles about this location opening
4. Facebook posts about the grand opening

Return ONLY a JSON object:
{{
  "opening_date": "YYYY-MM-DD or YYYY-MM or YYYY or null",
  "opening_date_confidence": "high|medium|low|none",
  "opening_date_source": "exact URL of news article, Yelp page, or Facebook post you found",
  "opening_date_evidence": "brief description of what you found (e.g. 'Yelp first review dated 2019-03-15')",
  "closing_date": null,
  "closing_date_confidence": "none",
  "closing_date_source": "",
  "suggested_searches": ["search query 1 if not found", "search query 2"]
}}

Confidence rules:
- "high": explicit grand opening news article or press release with exact date
- "medium": first Yelp/Google review date, Facebook grand opening post
- "low": indirect evidence (chain expansion article, permit record)
- "none": could not find — fill suggested_searches with useful Google queries

Return ONLY JSON, no markdown.
""".strip()

    response = _call_gemini(client, model_name, prompt)
    raw = response.text
    data = _parse_json(raw)
    urls = _extract_grounding_urls(response) or _extract_urls_from_text(raw)

    return {
        "opening_date":             data.get("opening_date", "") or "",
        "opening_date_source":      _pick_source(urls, data.get("opening_date_source", ""),
                                                 ["yelp", "facebook", "news", "open", "grand"]),
        "opening_date_confidence":  data.get("opening_date_confidence", "none"),
        "opening_date_evidence":    data.get("opening_date_evidence", "") or "",
        "closing_date":             data.get("closing_date"),
        "closing_date_source":      _pick_source(urls, data.get("closing_date_source", ""),
                                                 ["clos", "shut", "bankrupt"]),
        "closing_date_confidence":  data.get("closing_date_confidence", "none"),
        "suggested_searches":       data.get("suggested_searches", []) or [],
        "grounding_urls":           urls,
    }


def _step3_shopping_center(client, model_name: str, name: str, address: str) -> dict:
    """
    Step 3: Xác định Shopping Center và tìm Site Plan / Leasing Map.
    """
    prompt = f"""
You are a POI researcher. Determine if this business is inside a shopping center:

Business: {name}
Address: {address}

Search for:
1. Is "{name}" at "{address}" inside a mall, strip mall, or shopping center?
2. If yes, what is the shopping center name?
3. Search: "[shopping center name] site plan" or "[shopping center name] leasing map" on
   LoopNet, CBRE, JLL, CoStar, retailsitesusa.com, or the SC's official website.

Return ONLY a JSON object:
{{
  "is_in_shopping_center": false,
  "shopping_center_name": null,
  "site_plan_url": "direct URL to a PDF or webpage with the floor plan/site plan, or null"
}}

Rules:
- is_in_shopping_center = true for: mall, strip mall, power center, lifestyle center, outlet center.
- is_in_shopping_center = false for: standalone building, gas station, office park.
- site_plan_url: must be a real URL you actually found. null if not found.
- Return ONLY JSON, no markdown.
""".strip()

    response = _call_gemini(client, model_name, prompt)
    raw = response.text
    data = _parse_json(raw)
    urls = _extract_grounding_urls(response) or _extract_urls_from_text(raw)

    return {
        "is_in_shopping_center": bool(data.get("is_in_shopping_center", False)),
        "shopping_center_name":  data.get("shopping_center_name"),
        "site_plan_url":         data.get("site_plan_url"),
        "grounding_urls":        urls,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _call_gemini(client, model_name: str, prompt: str):
    """Gọi Gemini với Google Search grounding."""
    return client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )


def _parse_json(text: str) -> dict:
    """Extract và parse JSON từ Gemini response text."""
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        logger.warning("No JSON found in Gemini response")
        return {}
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        return {}


# URL blocklist — loại bỏ namespace/CDN URLs vô nghĩa
_URL_BLOCKLIST = (
    "w3.org", "schema.org", "xmlns", "json-ld",
    "openstreetmap.org", "leafletjs.com", "unpkg.com",
    "googleapis.com/fonts", "cdnjs.cloudflare", "html2canvas",
)


def _is_valid_source_url(url: str) -> bool:
    u = url.lower()
    if any(b in u for b in _URL_BLOCKLIST):
        return False
    return len(url) >= 25


def _extract_grounding_urls(response) -> list:
    """Lấy URL thực từ Google Search grounding metadata."""
    urls = []
    try:
        for candidate in response.candidates:
            meta = getattr(candidate, "grounding_metadata", None)
            if not meta:
                continue
            # Path 1: grounding_chunks[].web.uri
            for chunk in (getattr(meta, "grounding_chunks", None) or []):
                web = getattr(chunk, "web", None)
                if web:
                    uri = getattr(web, "uri", None)
                    if uri and uri not in urls and _is_valid_source_url(uri):
                        urls.append(uri)
            # Path 2: search_entry_point rendered_content
            sep = getattr(meta, "search_entry_point", None)
            if sep:
                content = getattr(sep, "rendered_content", "") or ""
                for u in re.findall(r'https?://[^\s"<>]+', content):
                    if u not in urls and _is_valid_source_url(u):
                        urls.append(u)
            # Log queries for debug
            queries = getattr(meta, "web_search_queries", []) or []
            if queries:
                logger.debug(f"Search queries: {queries}")
    except Exception as e:
        logger.warning(f"Grounding extraction error: {e}")
    return urls


def _extract_urls_from_text(text: str) -> list:
    """Fallback: tìm URLs trong response text."""
    seen, result = set(), []
    for u in re.findall(r'https?://[^\s"\'<>)\]]+', text):
        u = u.rstrip(".,;:)]")
        if u not in seen and _is_valid_source_url(u):
            seen.add(u)
            result.append(u)
    return result


def _pick_source(grounding_urls: list, gemini_suggested: str,
                 keywords: list) -> str:
    """
    Chọn URL nguồn tốt nhất:
    1. Tìm trong grounding URLs theo keyword
    2. Nếu không có, kiểm tra URL Gemini suggested (nếu valid)
    3. Trả về rỗng
    """
    valid = [u for u in grounding_urls if _is_valid_source_url(u)]
    # Ưu tiên grounding URL match keyword
    for url in valid:
        if any(kw in url.lower() for kw in keywords):
            return url
    # Dùng URL Gemini suggest nếu nó có trong grounding list
    if gemini_suggested and gemini_suggested in grounding_urls:
        return gemini_suggested
    return ""


def _extract_city_state(address: str) -> str:
    """Lấy City, State từ địa chỉ."""
    parts = [p.strip() for p in address.split(",")]
    return ", ".join(parts[-2:]) if len(parts) >= 2 else address


def _empty_result() -> dict:
    return {
        "opening_hours":            "",
        "opening_hours_source":     "",
        "opening_date":             "",
        "opening_date_source":      "",
        "opening_date_confidence":  "none",
        "opening_date_evidence":    "",
        "is_closed":                False,
        "closing_date":             None,
        "closing_date_source":      "",
        "closing_date_confidence":  "none",
        "category":                 "",
        "status_note":              "",
        "official_website":         "",
        "is_in_shopping_center":    False,
        "shopping_center_name":     None,
        "site_plan_url":            None,
        "suggested_searches":       [],
        "grounding_urls":           [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────────────────

def format_hours_for_display(oh_string: str) -> str:
    if not oh_string:
        return "(không có dữ liệu)"
    day_map = {"mo": "Mon", "tu": "Tue", "we": "Wed",
               "th": "Thu", "fr": "Fri", "sa": "Sat", "su": "Sun"}
    parts = [p.strip() for p in oh_string.split(";")]
    result = []
    for part in parts:
        for short, full in day_map.items():
            part = re.sub(rf'\b{short}\b', full, part)
        result.append(part)
    return " | ".join(result)
