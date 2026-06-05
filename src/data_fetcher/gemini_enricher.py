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
import time
from typing import Iterator
from urllib.parse import urlparse
from google import genai
from google.genai import types
from data_fetcher import browser_fetcher

logger = logging.getLogger(__name__)


def setup_gemini(api_key: str, model: str = "gemini-2.0-flash"):
    """Khởi tạo Gemini client từ 1 key (backward compat)."""
    client = genai.Client(api_key=api_key)
    return client, model


def setup_gemini_multi(api_keys: list, model: str = "gemini-2.0-flash"):
    """
    Khởi tạo 3 Gemini client riêng biệt từ danh sách keys.
    Mỗi step sẽ dùng 1 client riêng để phân tải đều ra 3 account free.
    Nếu ít hơn 3 key thì reuse key đầu tiên cho các step thiếu.
    """
    keys = list(api_keys) if api_keys else []
    # Pad lên đúng 3 key
    while len(keys) < 3:
        keys.append(keys[0] if keys else "")
    clients = [genai.Client(api_key=k) for k in keys[:3]]
    return {
        "clients": clients,       # [client_step1, client_step2, client_step3]
        "model": model,
        "keys": keys[:3],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_clients(model_config) -> tuple:
    """
    Trả về (client1, client2, client3, model_name) từ model_config.
    model_config có thể là:
      - tuple (client, model_name)          ← backward compat (1 key)
      - dict {clients, model, keys}         ← multi-key mode
    """
    if isinstance(model_config, dict):
        clients = model_config["clients"]
        model_name = model_config["model"]
        return clients[0], clients[1], clients[2], model_name
    else:
        # Backward compat: tuple (client, model_name)
        client, model_name = model_config
        return client, client, client, model_name


def enrich_poi_stream(model_config, name: str, address: str) -> Iterator[dict]:
    """
    Tìm kiếm POI theo 3 bước, yield từng kết quả trung gian.
    Mỗi bước dùng 1 Gemini client riêng (phân tải đều ra 3 account free).

    Yields dicts:
      {"step": 1, "status": "running", "label": "..."}
      {"step": 1, "status": "done",    "label": "...", "partial": {...}}
      ...
      {"step": "final", "data": {full_result}}
    """
    client1, client2, client3, model_name = _resolve_clients(model_config)
    result = _empty_result()

    # ── Step 1: Hours + Status + Category (dùng key #1) ───────────────────────
    yield {"step": 1, "status": "running",
           "label": "Tìm giờ mở cửa, danh mục và website chính thức..."}
    try:
        s1 = _step1_hours_status_category(client1, model_name, name, address)
        result.update(s1)
        yield {"step": 1, "status": "done",
               "label": "Tìm được giờ mở cửa",
               "partial": {k: result[k] for k in
                           ["opening_hours", "opening_hours_source",
                            "is_closed", "closing_date", "category",
                            "official_website", "grounding_urls",
                            "grounding_sources"]}}
    except Exception as e:
        logger.error(f"Step 1 error: {e}")
        yield {"step": 1, "status": "error", "label": f"Step 1 lỗi: {e}"}

    # ── Step 2: Opening Date / Closing Date (dùng key #2) ────────────────────
    time.sleep(2)  # Delay nhỏ tránh RPM limit giữa bước 1 → 2
    yield {"step": 2, "status": "running",
           "label": "Tìm ngày khai trương và ngày đóng cửa..."}
    try:
        s2 = _step2_dates(client2, model_name, name, address,
                          official_website=result.get("official_website", ""))
        result.update(s2)
        # Merge grounding URLs + sources (tag step, dedup by domain)
        for u in s2.get("grounding_urls", []):
            if u not in result["grounding_urls"]:
                result["grounding_urls"].append(u)
        for s in s2.get("grounding_sources", []):
            s["step"] = s.get("step", 2)   # tag step 2
            if not _source_domain_exists(result["grounding_sources"], s):
                result["grounding_sources"].append(s)
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

    # ── Step 2c: Chrome browser — tự tìm kiếm nếu OD vẫn chưa có ────────────
    _od_missing = (not result.get("opening_date") or
                   result.get("opening_date_confidence") in ("none", "low"))
    if _od_missing and browser_fetcher.is_configured():
        yield {"step": 2, "status": "running",
               "label": "🌐 Chrome browser đang tìm kiếm ngày khai trương..."}
        try:
            r2c = browser_fetcher.browser_find_opening_date(
                name, address, result["grounding_sources"]
            )
            if r2c.get("opening_date"):
                logger.info(f"Step 2c tìm được: {r2c['opening_date']} ({r2c.get('opening_date_confidence')})")
                real_url = r2c.get("opening_date_source", "")
                if real_url:
                    _up = urlparse(real_url)
                    _dom = _up.netloc.replace("www.", "")
                    _src = {
                        "url":         real_url,
                        "title":       r2c.get("opening_date_evidence", _dom),
                        "display_url": real_url,
                        "favicon":     f"https://www.google.com/s2/favicons?domain={_dom}&sz=16",
                        "domain":      _dom,
                        "step":        2,
                    }
                    if not _source_domain_exists(result["grounding_sources"], _src):
                        result["grounding_sources"].append(_src)
                        result["grounding_urls"].append(real_url)
                result.update({k: v for k, v in r2c.items()
                               if k not in ("grounding_urls", "grounding_sources")})
            yield {"step": 2, "status": "done",
                   "label": "🌐 Chrome browser hoàn tất" if r2c.get("opening_date") else "🌐 Chrome: không tìm được ngày",
                   "partial": {k: result[k] for k in
                               ["opening_date", "opening_date_source",
                                "opening_date_confidence", "opening_date_evidence",
                                "grounding_sources", "grounding_urls"]}}
        except Exception as e:
            logger.warning(f"Step 2c lỗi: {e}")
            yield {"step": 2, "status": "error", "label": f"🌐 Chrome lỗi: {e}"}

    # ── Step 3: Shopping Center + Site Plan (dùng key #3) ────────────────────
    time.sleep(2)  # Delay nhỏ tránh RPM limit giữa bước 2 → 3
    yield {"step": 3, "status": "running",
           "label": "Kiểm tra Shopping Center và Site Plan..."}
    try:
        s3 = _step3_shopping_center(client3, model_name, name, address)
        result.update(s3)
        for u in s3.get("grounding_urls", []):
            if u not in result["grounding_urls"]:
                result["grounding_urls"].append(u)
        for s in s3.get("grounding_sources", []):
            s["step"] = s.get("step", 3)   # tag step 3
            if not _source_domain_exists(result["grounding_sources"], s):
                result["grounding_sources"].append(s)
        yield {"step": 3, "status": "done",
               "label": "Hoàn tất",
               "partial": {k: result[k] for k in
                           ["is_in_shopping_center", "shopping_center_name",
                            "site_plan_url"]}}
    except Exception as e:
        logger.error(f"Step 3 error: {e}")
        yield {"step": 3, "status": "error", "label": f"Step 3 lỗi: {e}"}

    yield {"step": "final", "data": result}


def enrich_poi(model_config, name: str, address: str) -> dict:
    """Backward-compat wrapper — chạy tuần tự, trả về result cuối."""
    result = _empty_result()
    for event in enrich_poi_stream(model_config, name, address):
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
    sources = _extract_grounding_sources(response)
    for s in sources:
        s["step"] = 1   # tag step 1 (hours)
    urls = [s["url"] for s in sources] or _extract_urls_from_text(raw)

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
        "grounding_sources":    sources,
    }


def _step2_dates(client, model_name: str, name: str, address: str,
                 official_website: str = "") -> dict:
    """
    Step 2: Tìm opening date từ news, Yelp, Facebook.
    Phase 2a: Google Search để tìm candidate URLs (snippets).
    Phase 2b: Nếu không tìm được, dùng url_context để đọc chi tiết trang web.
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
    sources = _extract_grounding_sources(response)
    urls = [s["url"] for s in sources] or _extract_urls_from_text(raw)

    result = {
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
        "grounding_sources":        sources,
    }

    # ── Phase 2b: Đọc chi tiết trang nếu chưa tìm được ngày ─────────────────
    if not result["opening_date"] or result["opening_date_confidence"] == "none":
        logger.info("Step 2a không tìm được OD — chuyển sang Phase 2b (url_context)...")
        readable_urls = _filter_readable_urls(urls)
        suggestions   = result["suggested_searches"]
        if readable_urls or suggestions:
            try:
                r2b = _step2b_read_urls(client, model_name, name, address,
                                        readable_urls, suggestions)
                if r2b.get("opening_date"):
                    logger.info(f"Phase 2b tìm được: {r2b['opening_date']} ({r2b['opening_date_confidence']})")
                    for u in r2b.get("grounding_urls", []):
                        if u not in result["grounding_urls"]:
                            result["grounding_urls"].append(u)
                    for s in r2b.get("grounding_sources", []):
                        if not any(x["url"] == s["url"] for x in result["grounding_sources"]):
                            result["grounding_sources"].append(s)
                    result.update({k: v for k, v in r2b.items()
                                   if k not in ("grounding_urls", "grounding_sources")})
            except Exception as e:
                logger.warning(f"Phase 2b lỗi: {e}")

    return result


def _filter_readable_urls(urls: list) -> list:
    """
    Giữ lại tất cả grounding URLs (kể cả redirect Google) vì chúng hoạt động
    khi Gemini đọc qua url_context tool. Loại bỏ chỉ các CDN/font vô nghĩa.
    """
    return [u for u in urls if _is_valid_source_url(u)][:6]


def _step2b_read_urls(client, model_name: str, name: str, address: str,
                      urls: list, suggested_searches: list) -> dict:
    """
    Phase 2b: Dùng url_context tool để Gemini THỰC SỰ FETCH và đọc nội dung
    các trang Yelp/news/Facebook — không chỉ đọc snippet từ Google Search.
    """
    url_lines    = "\n".join(f"- {u}" for u in urls) if urls else "(không có URL cụ thể)"
    search_lines = "\n".join(f'- Search: "{s}"' for s in suggested_searches[:3]) \
                   if suggested_searches else ""

    prompt = f"""
You are a POI researcher. Open and READ the following web pages to find the GRAND OPENING DATE
of this specific business location:

Business: {name}
Address: {address}

Pages to open and read:
{url_lines}

{f"If pages above don't help, also try:{chr(10)}{search_lines}" if search_lines else ""}

Instructions:
- Actually OPEN each URL and read the page content
- On Yelp: look at the date of the FIRST (oldest) review — that's the earliest known open date
- On news sites: look for "grand opening", "now open", "opened" with a specific date
- On Facebook: look for grand opening event posts
- On the business website: look for "established", "founded", "since YYYY"

Return ONLY a JSON object:
{{
  "opening_date": "YYYY-MM-DD or YYYY-MM or YYYY or null",
  "opening_date_confidence": "high|medium|low|none",
  "opening_date_source": "exact URL where you found the date",
  "opening_date_evidence": "what you found, e.g. 'Yelp first review by John D. dated March 15, 2019'",
  "closing_date": null,
  "closing_date_confidence": "none",
  "closing_date_source": ""
}}

Confidence: high=grand opening article, medium=first Yelp/review date, low=indirect, none=not found
Return ONLY JSON, no markdown.
""".strip()

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[
                    types.Tool(url_context=types.UrlContext()),
                    types.Tool(google_search=types.GoogleSearch()),
                ],
            ),
        )
    except Exception as e:
        # url_context có thể không hỗ trợ trên model fallback — dùng chỉ google_search
        if "url_context" in str(e).lower() or "unsupported" in str(e).lower() \
                or "invalid" in str(e).lower():
            logger.warning(f"url_context không hỗ trợ trên {model_name}, dùng google_search: {e}")
            response = _call_gemini(client, model_name, prompt)
        else:
            raise

    raw = response.text
    data = _parse_json(raw)
    sources = _extract_grounding_sources(response)
    found_urls = [s["url"] for s in sources] or _extract_urls_from_text(raw)

    return {
        "opening_date":            data.get("opening_date", "") or "",
        "opening_date_source":     _pick_source(found_urls, data.get("opening_date_source", ""),
                                                ["yelp", "facebook", "news", "open", "grand"]),
        "opening_date_confidence": data.get("opening_date_confidence", "none"),
        "opening_date_evidence":   data.get("opening_date_evidence", "") or "",
        "closing_date":            data.get("closing_date"),
        "closing_date_source":     _pick_source(found_urls, data.get("closing_date_source", ""),
                                                ["clos", "shut", "bankrupt"]),
        "closing_date_confidence": data.get("closing_date_confidence", "none"),
        "grounding_urls":          found_urls,
        "grounding_sources":       sources,
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
    sources = _extract_grounding_sources(response)
    urls = [s["url"] for s in sources] or _extract_urls_from_text(raw)

    return {
        "is_in_shopping_center": bool(data.get("is_in_shopping_center", False)),
        "shopping_center_name":  data.get("shopping_center_name"),
        "site_plan_url":         data.get("site_plan_url"),
        "grounding_urls":        urls,
        "grounding_sources":     sources,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

# Model chính và model fallback khi gặp 429
_FALLBACK_MODEL = "gemini-2.5-flash-lite"
# Delay retry (giây) nếu cả fallback cũng bị 429: lần 1, lần 2
_RETRY_DELAYS = [20, 60]


def _is_rate_limit(e: Exception) -> bool:
    s = str(e).lower()
    return "429" in s or "resource_exhausted" in s or "rate" in s


def _call_gemini(client, model_name: str, prompt: str):
    """
    Gọi Gemini với Google Search grounding.
    Khi gặp 429:
      1. Lập tức thử lại bằng gemini-2.5-flash-lite (không chờ)
      2. Nếu lite cũng 429 → chờ 20s rồi thử lại model gốc
      3. Nếu vẫn 429 → chờ 60s rồi thử lần cuối
    """
    def _do_call(model: str):
        return client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )

    # Attempt 0: model chính
    try:
        return _do_call(model_name)
    except Exception as e:
        if not _is_rate_limit(e):
            raise
        logger.warning(f"429 trên {model_name} — thử fallback {_FALLBACK_MODEL}...")

    # Attempt 1: fallback ngay (không chờ)
    try:
        resp = _do_call(_FALLBACK_MODEL)
        logger.info(f"Fallback thành công với {_FALLBACK_MODEL}")
        return resp
    except Exception as e:
        if not _is_rate_limit(e):
            raise
        logger.warning(f"429 trên {_FALLBACK_MODEL} — bắt đầu retry với backoff...")

    # Attempt 2+: retry với backoff, luân phiên model chính và lite
    last_exc = None
    for i, wait in enumerate(_RETRY_DELAYS):
        logger.warning(f"Chờ {wait}s rồi thử lại (lần {i + 1}/{len(_RETRY_DELAYS)})...")
        time.sleep(wait)
        # Luân phiên: lần lẻ dùng model gốc, lần chẵn dùng lite
        retry_model = model_name if i % 2 == 0 else _FALLBACK_MODEL
        try:
            return _do_call(retry_model)
        except Exception as e:
            if not _is_rate_limit(e):
                raise
            last_exc = e
            logger.warning(f"429 lại trên {retry_model} (lần {i + 1}): {e}")

    raise last_exc


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


def _source_domain_exists(sources: list, new_source: dict) -> bool:
    """
    Kiểm tra xem đã có source cùng domain chưa.
    Dùng để deduplicate: không hiển thị 2 link cùng trang (vioc.com × 2).
    Ngoại lệ: cho phép cùng domain nếu step khác nhau (hours vs date source).
    """
    new_domain = new_source.get("domain", "")
    new_step   = new_source.get("step", 0)
    new_url    = new_source.get("url", "")

    for s in sources:
        if s.get("url") == new_url:
            return True   # Exact URL match → dedup
        if new_domain and s.get("domain") == new_domain and s.get("step") == new_step:
            return True   # Same domain + same step → dedup
    return False


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
    sources = _extract_grounding_sources(response)
    return [s["url"] for s in sources]


def _extract_grounding_sources(response) -> list:
    """
    Trích xuất thông tin nguồn từ grounding_chunks[].web — CHỈ dùng Path này.

    Lý do chỉ dùng grounding_chunks:
    - URI là Google redirect (vertexaisearch.cloud.google.com/...) — luôn hoạt động khi user click
    - title là tên trang thực do Gemini cung cấp (Yelp, Carfax, Facebook...)
    - Không dùng search_entry_point.rendered_content vì cho URL thẳng (carfax.com/...)
      hay bị 403 khi click trực tiếp vì thiếu referrer/cookie/session.
    """
    seen_urls = set()
    sources = []
    try:
        for candidate in response.candidates:
            meta = getattr(candidate, "grounding_metadata", None)
            if not meta:
                continue
            for chunk in (getattr(meta, "grounding_chunks", None) or []):
                web = getattr(chunk, "web", None)
                if not web:
                    continue
                uri   = getattr(web, "uri",   None) or ""
                title = getattr(web, "title", None) or ""
                if uri and uri not in seen_urls and _is_valid_source_url(uri):
                    seen_urls.add(uri)
                    domain  = _infer_domain_from_title(title) or _domain_from_uri(uri)
                    favicon = f"https://www.google.com/s2/favicons?domain={domain}&sz=16" if domain else ""
                    sources.append({
                        "url":         uri,
                        "title":       title,
                        "display_url": uri,
                        "favicon":     favicon,
                        "domain":      domain,
                    })
            queries = getattr(meta, "web_search_queries", []) or []
            if queries:
                logger.debug(f"Search queries: {queries}")
    except Exception as e:
        logger.warning(f"Grounding extraction error: {e}")
    return sources


# Map keyword trong title → domain thực của nguồn
_TITLE_DOMAIN_MAP = [
    ("yelp",              "yelp.com"),
    ("facebook",          "facebook.com"),
    ("tripadvisor",       "tripadvisor.com"),
    ("yellow pages",      "yellowpages.com"),
    ("yp.com",            "yp.com"),
    ("foursquare",        "foursquare.com"),
    ("google maps",       "maps.google.com"),
    ("google business",   "business.google.com"),
    ("bbb",               "bbb.org"),
    ("better business",   "bbb.org"),
    ("mapquest",          "mapquest.com"),
    ("indeed",            "indeed.com"),
    ("linkedin",          "linkedin.com"),
    ("instagram",         "instagram.com"),
    ("twitter",           "twitter.com"),
    ("yelp",              "yelp.com"),
    ("angi",              "angi.com"),
    ("homeadvisor",       "homeadvisor.com"),
    ("nextdoor",          "nextdoor.com"),
    ("citysearch",        "citysearch.com"),
    ("superpages",        "superpages.com"),
    ("manta",             "manta.com"),
    ("chamberofcommerce", "chamberofcommerce.com"),
]


def _infer_domain_from_title(title: str) -> str:
    """
    Infer domain thực từ title của trang (ví dụ: 'Valvoline - Yelp' → 'yelp.com').
    Nhờ Gemini metadata thường chứa tên trang ở cuối title sau dấu ' - ' hoặc ' | '.
    """
    if not title:
        return ""
    tl = title.lower()
    # Thử map trực tiếp
    for keyword, domain in _TITLE_DOMAIN_MAP:
        if keyword in tl:
            return domain
    # Thử lấy phần sau ' - ' hoặc ' | ' cười title như domain hint
    for sep in (" - ", " | ", " – "):
        if sep in title:
            suffix = title.split(sep)[-1].strip().lower()
            # Nếu suffix trông giống domain (có dấu chấm), trả luôn
            if "." in suffix and " " not in suffix and len(suffix) < 30:
                return suffix
    return ""


def _domain_from_uri(uri: str) -> str:
    """Lấy domain từ URI (không phải Google redirect)."""
    skip = ("vertexaisearch", "googleapis", "google.com")
    try:
        parsed = urlparse(uri)
        host = parsed.netloc.lower()
        if not any(s in host for s in skip):
            return host.replace("www.", "")
    except Exception:
        pass
    return ""


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
        "grounding_sources":        [],   # [{title, url, display_url, favicon}]
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
