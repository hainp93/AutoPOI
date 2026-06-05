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
from . import browser_fetcher

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

    # ── Step 1: Unified Gemini Search (All-in-one) ──────────────────────────────
    yield {"step": 1, "status": "running",
           "label": "Gemini đang tổng hợp dữ liệu (All-in-one)..."}
    try:
        s1 = _step1_gemini_unified(client1, model_name, name, address)
        result.update(s1)
        yield {"step": 1, "status": "done",
               "label": "Gemini hoàn tất tổng hợp",
               "partial": {k: result[k] for k in
                           ["opening_hours", "opening_hours_source",
                            "is_closed", "closing_date", "category",
                            "official_website", "opening_date", "opening_date_source",
                            "is_in_shopping_center", "site_plan_url"]}}
    except Exception as e:
        logger.error(f"Step 1 error: {e}")
        yield {"step": 1, "status": "error", "label": f"Step 1 lỗi: {e}"}

    # ── Step 2: Chrome browser (ưu tiên tìm Opening Date chính xác) ──────────────
    if browser_fetcher.is_configured():
        yield {"step": 2, "status": "running",
               "label": "🌐 Chrome browser đang kiểm tra ngày khai trương..."}
        try:
            r2c = browser_fetcher.browser_find_opening_date(
                name, address, result.get("grounding_sources", [])
            )
            if r2c.get("opening_date"):
                logger.info(f"Chrome tìm được: {r2c['opening_date']} ({r2c.get('opening_date_confidence')})")
                real_url = r2c.get("opening_date_source", "")
                if real_url:
                    _up  = urlparse(real_url)
                    _dom = _up.netloc.replace("www.", "")
                    _src = {
                        "url":         real_url,
                        "title":       r2c.get("opening_date_evidence", _dom),
                        "display_url": real_url,
                        "favicon":     f"https://www.google.com/s2/favicons?domain={_dom}&sz=16",
                        "domain":      _dom,
                        "step":        2,
                    }
                    if not _source_domain_exists(result.get("grounding_sources", []), _src):
                        result.setdefault("grounding_sources", []).append(_src)
                        result.setdefault("grounding_urls", []).append(real_url)
                
                result.update({k: v for k, v in r2c.items()
                               if k not in ("grounding_urls", "grounding_sources")})
                
                yield {"step": 2, "status": "done",
                       "label": "🌐 Chrome hoàn tất: tìm được ngày",
                       "partial": {k: result.get(k) for k in
                                   ["opening_date", "opening_date_source",
                                    "opening_date_confidence", "opening_date_evidence"]}}
            else:
                yield {"step": 2, "status": "done",
                       "label": "🌐 Chrome không tìm thêm được ngày", "partial": {}}
        except Exception as e:
            logger.warning(f"Chrome lỗi: {e}")
            yield {"step": 2, "status": "error", "label": f"🌐 Chrome lỗi: {e}"}
    else:
        cfg_path = browser_fetcher._cfg.get("profile_path", "")
        if not cfg_path:
            logger.info("Chrome bỏ qua: chrome.profile_path chưa cấu hình")
        else:
            logger.warning(f"Chrome bỏ qua: profile_path='{cfg_path}' nhưng is_configured()=False")

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

def _step1_gemini_unified(client, model_name: str, name: str, address: str) -> dict:
    """
    Step 1: Unified Gemini Search (All-in-one).
    Tìm tất cả các trường dữ liệu bằng 1 lệnh gọi Gemini Grounding duy nhất.
    """
    prompt = f"""
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
  "status_note": "any important notes about the business status",
  "is_in_shopping_center": false,
  "shopping_center_name": null,
  "site_plan_url": null,
  "official_website": "URL of the official business website or location page, or null"
}}

Rules:
- opening_hours: Use format "mo" "tu" "we" "th" "fr" "sa" "su". Use ranges like "mo-fr". Time in 24h HH:MM.
- STRICT RULE: Do NOT use aggregator or review sites (e.g., carfax.com, yelp.com, yellowpages.com, foursquare.com, mapquest.com) as the `opening_hours_source` because QA cannot access them (403 Forbidden).
- If you find the hours on an aggregator, you MUST find the official website or Facebook page and return that as the source instead. If no official site exists, return the Google Maps link.
- opening_date: Search news, Yelp reviews, Facebook posts for grand opening. Use earliest evidence.
- If business is permanently closed, set is_closed=true and provide closing_date.
- is_in_shopping_center: true if this store is inside a mall, strip mall, or shopping center.
- shopping_center_name: name of the shopping center if applicable (e.g. "Deer Park Town Center").
- site_plan_url: Search for a site plan / leasing map / floor directory of the shopping center.
  Search: "[shopping center name] site plan", "[shopping center name] leasing map",
  "[shopping center name] floor directory", "[address] site plan".
  Look on: the SC official website, LoopNet, CBRE, JLL, CoStar, retailsitesusa.com.
  Return a direct URL to a PDF, image, or webpage showing the floor plan. null if not found.
- Return ONLY the JSON, no other text.
""".strip()

    response = _call_gemini(client, model_name, prompt)
    raw = response.text
    data = _parse_json(raw)
    sources = _extract_grounding_sources(response)
    for s in sources:
        s["step"] = 1   # tag step 1 (unified)
    
    # Chỉ lấy URL có thật từ Google Search Grounding. Tuyệt đối không trích xuất URL hallucinate từ raw text.
    urls = [s["url"] for s in sources]

    return {
        "opening_hours":        data.get("opening_hours", "") or "",
        "opening_hours_source": _pick_source(urls, data.get("opening_hours_source", ""),
                                             ["hour", "schedule", "time", "store", "location"], 
                                             block_aggregators=True,
                                             official_website=data.get("official_website", "")),
        "opening_date":             data.get("opening_date", "") or "",
        "opening_date_source":      _pick_source(urls, data.get("opening_date_source", ""),
                                                 ["yelp", "facebook", "news", "patch", "article", "grand"]),
        "opening_date_confidence":  "medium" if data.get("opening_date") else "none",
        "is_closed":            bool(data.get("is_closed", False)),
        "closing_date":         data.get("closing_date"),
        "closing_date_source":  _pick_source(urls, data.get("closing_date_source", ""), ["close", "news", "article"]),
        "closing_date_confidence": "medium" if data.get("closing_date") else "none",
        "category":             data.get("category", "") or "",
        "official_website":     data.get("official_website", "") or "",
        "is_in_shopping_center": bool(data.get("is_in_shopping_center", False)),
        "shopping_center_name": data.get("shopping_center_name", "") or "",
        "site_plan_url":        _pick_source(urls, data.get("site_plan_url", ""),
                                             ["plan", "map", "directory", "lease", "property"]),
        "grounding_urls":       urls,
        "grounding_sources":    sources,
    }


_FALLBACK_MODEL = "gemini-2.5-flash-lite"
_RETRY_DELAYS = [20, 60]

def _is_rate_limit(e: Exception) -> bool:
    err = str(e).lower()
    return "429" in err or "quota" in err or "rate limit" in err

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
        # 1. Trích xuất tất cả URL thực từ rendered_content (tránh link redirect bị 403)
        real_urls = []
        for candidate in response.candidates:
            meta = getattr(candidate, "grounding_metadata", None)
            if meta and getattr(meta, "search_entry_point", None):
                html = getattr(meta.search_entry_point, "rendered_content", "")
                if html:
                    real_urls.extend(re.findall(r'href="(https?://[^"]+)"', html))

        # 2. Xử lý từng chunk
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
                    
                    # Cố gắng tìm URL thực tương ứng với domain này để thay thế link redirect
                    display_url = uri
                    if domain and "vertexaisearch" in uri:
                        for ru in real_urls:
                            if domain in ru:
                                display_url = ru
                                break

                    favicon = f"https://www.google.com/s2/favicons?domain={domain}&sz=16" if domain else ""
                    sources.append({
                        "url":         display_url,
                        "title":       title,
                        "display_url": display_url,
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
    # Review / listing sites
    ("yelp",               "yelp.com"),
    ("facebook",           "facebook.com"),
    ("tripadvisor",        "tripadvisor.com"),
    ("yellow pages",       "yellowpages.com"),
    ("yp.com",             "yp.com"),
    ("foursquare",         "foursquare.com"),
    ("google maps",        "maps.google.com"),
    ("google business",    "business.google.com"),
    ("bbb",                "bbb.org"),
    ("better business",    "bbb.org"),
    ("mapquest",           "mapquest.com"),
    ("indeed",             "indeed.com"),
    ("linkedin",           "linkedin.com"),
    ("instagram",          "instagram.com"),
    ("twitter",            "twitter.com"),
    ("angi",               "angi.com"),
    ("homeadvisor",        "homeadvisor.com"),
    ("nextdoor",           "nextdoor.com"),
    ("citysearch",         "citysearch.com"),
    ("superpages",         "superpages.com"),
    ("manta",              "manta.com"),
    ("chamberofcommerce",  "chamberofcommerce.com"),
    ("carfax",             "carfax.com"),
    ("cars.com",           "cars.com"),
    ("autotrader",         "autotrader.com"),
    # Official brand sites (auto service chains)
    ("valvoline",          "valvoline.com"),
    ("vioc",               "vioc.com"),
    ("jiffy lube",         "jiffylube.com"),
    ("firestone",          "firestonecompleteautocare.com"),
    ("pep boys",           "pepboys.com"),
    ("midas",              "midas.com"),
    ("meineke",            "meineke.com"),
    ("mavis",              "mavisnortheast.com"),
    ("sears auto",         "searsnorthamerica.com"),
    ("goodyear",           "goodyear.com"),
    ("ntb",                "ntb.com"),
    # Retail / food chains
    ("walmart",            "walmart.com"),
    ("target",             "target.com"),
    ("starbucks",          "starbucks.com"),
    ("mcdonald",           "mcdonalds.com"),
    ("chick-fil-a",        "chick-fil-a.com"),
    ("dunkin",             "dunkindonuts.com"),
    ("dollar tree",        "dollartree.com"),
    ("dollar general",     "dollargeneral.com"),
    ("cvs",                "cvs.com"),
    ("walgreens",          "walgreens.com"),
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
                 keywords: list, block_aggregators: bool = False,
                 official_website: str = "") -> str:
    """
    Chọn URL nguồn tốt nhất:
    1. Tìm trong grounding URLs theo keyword
    2. Nếu có official_website từ Gemini, ưu tiên dùng (thường chính xác hơn link Maps ngẫu nhiên)
    3. Nếu không có, lấy URL hợp lệ đầu tiên do Google trả về (tránh link Maps nếu có thể)
    4. Cuối cùng mới dùng URL Gemini suggest (để tránh hallucination)
    """
    aggregator_domains = ("carfax.com", "yelp.com", "yellowpages.com", "foursquare.com", "mapquest.com", "tripadvisor.com")
    
    def _is_allowed(u):
        if not _is_valid_source_url(u):
            return False
        if block_aggregators and any(agg in u.lower() for agg in aggregator_domains):
            return False
        return True

    valid = [u for u in grounding_urls if _is_allowed(u)]
    # Ưu tiên grounding URL match keyword
    for url in valid:
        if any(kw in url.lower() for kw in keywords):
            return url
            
    # Ưu tiên trang web chính thức do Gemini tìm được (ví dụ store.vioc.com) 
    # thay vì lấy link Google Maps ngẫu nhiên của chi nhánh khác
    if official_website and _is_allowed(official_website) and "google.com" not in official_website:
        return official_website

    # Nếu không match keyword nhưng có URL hợp lệ từ Google Search, lấy cái đầu tiên
    # Google Search kết quả đầu tiên thường là website chính thức
    if valid:
        # Cố gắng tránh Google Maps nếu có link khác hợp lệ hơn
        for v in valid:
            if "google.com/maps" not in v:
                return v
        return valid[0]
        
    # Dùng URL Gemini suggest nếu nó hợp lệ (fallback cuối cùng)
    if gemini_suggested and _is_allowed(gemini_suggested):
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
