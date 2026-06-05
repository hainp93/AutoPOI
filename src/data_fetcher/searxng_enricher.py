"""
AutoPOI — Open-Source Enricher (DuckDuckGo + Playwright + Ollama)
==================================================================
Stack 100% self-hosted, không giới hạn, không cần Docker:
  - DuckDuckGo Search : tìm kiếm web, không cần API key, chạy như thư viện Python
                        (pip install duckduckgo-search)
  - Playwright        : tải nội dung trang web (hỗ trợ JS, lazy-load)
  - Ollama            : phân tích text, trả về JSON có cấu trúc (Llama3, Mistral, ...)

Nếu muốn dùng SearXNG (self-hosted) thay cho DuckDuckGo:
  đặt search_backend: "searxng" trong config.yaml

Interface giống hệt gemini_enricher để dễ thay thế:
  - enrich_poi_stream_searxng(cfg, name, address) → generator[dict]
"""

from __future__ import annotations
import json
import logging
import re
import time
from typing import Generator
from urllib.parse import urlparse, urlencode, quote_plus

import requests

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Default config
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CFG = {
    "search_backend":  "duckduckgo",      # "duckduckgo" (không cần cài) | "searxng" (self-hosted)
    "searxng_url":     "http://localhost:8888",  # chỉ dùng khi backend=searxng
    "ollama_url":      "http://localhost:11434",
    "ollama_model":    "llama3.1:8b",
    "ollama_timeout":  120,
    "max_results":     5,
    "page_timeout":    15,   # giây chờ Playwright tải trang
}

# Domains bị chặn (aggregators trả về 403 hoặc không có thông tin hữu ích)
_BLOCKLIST = (
    "carfax.com", "yellowpages.com", "foursquare.com",
    "mapquest.com", "whitepages.com", "bbb.org", "manta.com",
    "bizapedia.com", "dnb.com", "opencorporates.com",
)

# Domains ưu tiên cao nhất (official / social)
_PRIORITY_DOMAINS = (
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "yelp.com",
    ".gov", ".edu",
)


# ─────────────────────────────────────────────────────────────────────────────
# Setup helper
# ─────────────────────────────────────────────────────────────────────────────

def build_config(raw: dict) -> dict:
    """Merge raw config với DEFAULT_CFG."""
    cfg = dict(DEFAULT_CFG)
    cfg.update({k: v for k, v in raw.items() if v is not None and v != ""})
    return cfg


def check_services(cfg: dict) -> dict:
    """Kiểm tra Ollama và tùy chọn SearXNG có đang chạy không."""
    backend = cfg.get("search_backend", "duckduckgo")
    status = {"searxng": True, "ollama": False, "errors": [], "search_backend": backend}

    if backend == "duckduckgo":
        # Kiểm tra thư viện duckduckgo-search có được cài không
        try:
            from duckduckgo_search import DDGS  # noqa
            status["searxng"] = True
        except ImportError:
            status["searxng"] = False
            status["errors"].append("DuckDuckGo: chưa cài thư viện. Chạy: pip install duckduckgo-search")
    else:
        # SearXNG: kiểm tra HTTP
        try:
            r = requests.get(f"{cfg['searxng_url']}/", timeout=3)
            status["searxng"] = r.status_code < 500
        except Exception as e:
            status["searxng"] = False
            status["errors"].append(f"SearXNG: {e}")

    try:
        r = requests.get(f"{cfg['ollama_url']}/api/tags", timeout=3)
        status["ollama"] = r.status_code == 200
        if status["ollama"]:
            tags = r.json().get("models", [])
            status["ollama_models"] = [m["name"] for m in tags]
    except Exception as e:
        status["errors"].append(f"Ollama: {e}")

    return status


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Search (DuckDuckGo by default, SearXNG optional)
# ─────────────────────────────────────────────────────────────────────────────

def _search(query: str, cfg: dict) -> list[dict]:
    """
    Tìm kiếm web và trả về list URLs thật.
    Backend: DuckDuckGo (mặc định, không cần server) hoặc SearXNG (self-hosted).
    """
    backend = cfg.get("search_backend", "duckduckgo")
    if backend == "searxng":
        return _search_searxng(query, cfg)
    return _search_duckduckgo(query, cfg)


def _search_duckduckgo(query: str, cfg: dict) -> list[dict]:
    """
    Dùng thư viện duckduckgo-search (pip install duckduckgo-search).
    Không cần API key, không cần server, chạy như thư viện Python đơn thuần.
    """
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        logger.error("Thư viện 'duckduckgo-search' chưa được cài. Chạy: pip install duckduckgo-search")
        return []

    try:
        results = []
        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=cfg.get("max_results", 5) + 3))
        for r in hits:
            u = r.get("href", "")
            if not u or _is_blocked(u):
                continue
            results.append({
                "url":     u,
                "title":   r.get("title", ""),
                "content": r.get("body",  ""),
                "domain":  urlparse(u).netloc.replace("www.", ""),
                "score":   _priority_score(u),
            })
        results.sort(key=lambda x: -x["score"])
        return results[:cfg.get("max_results", 5)]
    except Exception as e:
        logger.error(f"DuckDuckGo search lỗi: {e}")
        return []


def _search_searxng(query: str, cfg: dict) -> list[dict]:
    """
    Gọi SearXNG JSON API để tìm kiếm (fallback nếu muốn self-hosted).
    Cần dựng SearXNG trước: docker run -d -p 8888:8080 searxng/searxng
    """
    try:
        params = {
            "q":          query,
            "format":     "json",
            "categories": "general",
            "language":   "en-US",
        }
        url = f"{cfg['searxng_url']}/search?{urlencode(params)}"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "AutoPOI/1.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"SearXNG error: {e}")
        return []

    results = []
    for r in data.get("results", []):
        u = r.get("url", "")
        if not u or _is_blocked(u):
            continue
        results.append({
            "url":     u,
            "title":   r.get("title", ""),
            "content": r.get("content", ""),
            "domain":  urlparse(u).netloc.replace("www.", ""),
            "score":   _priority_score(u),
        })
    results.sort(key=lambda x: -x["score"])
    return results[:cfg.get("max_results", 5)]


# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Playwright page fetch
# ─────────────────────────────────────────────────────────────────────────────

def fetch_page_text(url: str, timeout_s: int = 15) -> str:
    """
    Dùng Playwright (headless Chromium) để tải trang và trích xuất text sạch.
    Trả về chuỗi text, giới hạn 8000 ký tự để không vượt context Ollama.
    Nếu Playwright chưa cài, fallback sang requests + regex strip HTML.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(url, timeout=timeout_s * 1000,
                          wait_until="domcontentloaded")
                page.wait_for_timeout(1500)  # đợi lazy load
                text = page.inner_text("body")
            except PWTimeout:
                text = page.inner_text("body") if page else ""
            finally:
                browser.close()
        return _clean_text(text)[:8000]
    except ImportError:
        logger.warning("Playwright chưa cài — fallback sang requests")
        return _fetch_requests_fallback(url)
    except Exception as e:
        logger.warning(f"Playwright lỗi [{url}]: {e}")
        return _fetch_requests_fallback(url)


def _fetch_requests_fallback(url: str) -> str:
    """Fallback: tải bằng requests + strip HTML tag."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
            )
        }
        resp = requests.get(url, timeout=10, headers=headers)
        html = resp.text
        # Xóa script, style
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.I)
        # Xóa tag HTML
        text = re.sub(r"<[^>]+>", " ", html)
        return _clean_text(text)[:8000]
    except Exception as e:
        logger.warning(f"requests fallback lỗi [{url}]: {e}")
        return ""


def _clean_text(text: str) -> str:
    """Làm sạch text: xóa khoảng trắng thừa, ký tự điều khiển."""
    text = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {3,}", " ", text)
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Ollama extraction
# ─────────────────────────────────────────────────────────────────────────────

_EXTRACTION_PROMPT = """You are a POI data researcher. Extract structured business information from the web content below.

Business: {name}
Address: {address}

Web content from {num_sources} sources:
{content}

Return ONLY a JSON object with exactly these fields:
{{
  "opening_hours": "VE format string, e.g. 'mo 09:00-21:00; tu-fr 09:00-22:00; sa 10:00-20:00; su 11:00-18:00' or null",
  "opening_hours_source": "URL where you found the hours (must be from the sources above)",
  "opening_date": "YYYY-MM-DD or YYYY-MM or YYYY (when this location first opened) or null",
  "opening_date_source": "URL where you found the opening date (must be from the sources above)",
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
- ONLY use URLs from the sources provided above for source fields. Do NOT invent URLs.
- opening_date: Use earliest evidence (first Yelp review date, news article about grand opening, etc.)
- If business is permanently closed, set is_closed=true and provide closing_date.
- is_in_shopping_center: true if this store is inside a mall, strip mall, or shopping center.
- Return ONLY the JSON object, no explanation, no markdown code block."""


def extract_with_ollama(content_blocks: list[dict], name: str, address: str, cfg: dict) -> dict:
    """
    Gọi Ollama local để trích xuất thông tin POI từ nội dung trang web.
    content_blocks: list[{url, text}]
    """
    # Gộp nội dung từ tất cả sources
    combined = ""
    for i, block in enumerate(content_blocks, 1):
        if block.get("text"):
            combined += f"\n\n--- SOURCE {i}: {block['url']} ---\n{block['text'][:3000]}"

    if not combined.strip():
        logger.warning("Không có nội dung để phân tích")
        return {}

    prompt = _EXTRACTION_PROMPT.format(
        name=name,
        address=address,
        num_sources=len(content_blocks),
        content=combined[:12000],  # Giới hạn context
    )

    try:
        payload = {
            "model":  cfg["ollama_model"],
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,   # Thấp = ít ảo giác hơn
                "num_predict": 800,
            }
        }
        resp = requests.post(
            f"{cfg['ollama_url']}/api/generate",
            json=payload,
            timeout=cfg.get("ollama_timeout", 120),
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
        return _parse_json(raw)
    except Exception as e:
        logger.error(f"Ollama error: {e}")
        return {}


def _parse_json(text: str) -> dict:
    """Trích xuất JSON từ response text (xử lý markdown code block nếu có)."""
    if not text:
        return {}
    # Thử parse thẳng
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    # Tìm JSON block trong text
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Build grounding sources từ search results
# ─────────────────────────────────────────────────────────────────────────────

def _build_source(result: dict, step: int = 1) -> dict:
    url = result.get("url", "")
    domain = result.get("domain", urlparse(url).netloc.replace("www.", ""))
    return {
        "url":         url,
        "title":       result.get("title", domain),
        "display_url": url,
        "favicon":     f"https://www.google.com/s2/favicons?domain={domain}&sz=16",
        "domain":      domain,
        "step":        step,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main streaming entry point (mirrors gemini_enricher interface)
# ─────────────────────────────────────────────────────────────────────────────

def enrich_poi_stream_searxng(cfg: dict, name: str, address: str) -> Generator[dict, None, None]:
    """
    Stream SSE events: mỗi bước xử lý yield 1 dict.
    Interface giống hệt gemini_enricher.enrich_poi_stream.
    """
    city_state = _extract_city_state(address)

    # Khởi tạo kết quả rỗng
    result = _empty_result()
    all_sources: list[dict] = []
    all_urls: list[str] = []

    # ── Step 1: Web Search ───────────────────────────────────────────────────
    backend = cfg.get("search_backend", "duckduckgo")
    backend_label = "DuckDuckGo" if backend == "duckduckgo" else "SearXNG"
    yield {"step": 1, "status": "running",
           "label": f"🔍 {backend_label} đang tìm kiếm trên web..."}

    search_results = []
    queries = [
        f"{name} {city_state} hours official website",
        f"{name} {address} grand opening date",
        f"{name} {city_state} facebook",
    ]
    seen_domains: set[str] = set()
    for q in queries:
        hits = _search(q, cfg)
        for h in hits:
            dom = h.get("domain", "")
            if dom not in seen_domains:
                seen_domains.add(dom)
                search_results.append(h)
        if len(search_results) >= cfg["max_results"] * 2:
            break

    if not search_results:
        yield {"step": 1, "status": "error",
               "label": f"❌ {backend_label} không tìm được kết quả nào"}
        yield {"step": "final", "data": result}
        return

    # Build grounding sources từ search results
    for sr in search_results:
        src = _build_source(sr, step=1)
        all_sources.append(src)
        all_urls.append(sr["url"])

    yield {"step": 1, "status": "done",
           "label": f"🔍 Tìm được {len(search_results)} nguồn ({backend_label})",
           "partial": {"grounding_urls": all_urls, "grounding_sources": all_sources}}

    # ── Step 2: Playwright fetch pages ─────────────────────────────────────
    yield {"step": 2, "status": "running",
           "label": f"🌐 Đang tải {len(search_results[:5])} trang web..."}

    content_blocks = []
    for sr in search_results[:5]:
        url = sr["url"]
        logger.info(f"Đang tải: {url}")
        text = fetch_page_text(url, timeout_s=cfg.get("page_timeout", 15))
        content_blocks.append({"url": url, "text": text})
        if text:
            logger.info(f"  → {len(text)} ký tự")

    fetched = sum(1 for b in content_blocks if b.get("text"))
    yield {"step": 2, "status": "done",
           "label": f"🌐 Đã tải {fetched}/{len(content_blocks)} trang thành công"}

    # ── Step 3: Ollama extraction ───────────────────────────────────────────
    yield {"step": 3, "status": "running",
           "label": f"🦙 Ollama ({cfg['ollama_model']}) đang phân tích dữ liệu..."}

    data = extract_with_ollama(content_blocks, name, address, cfg)

    if not data:
        yield {"step": 3, "status": "error",
               "label": "❌ Ollama không trả về dữ liệu — kiểm tra model và kết nối"}
    else:
        # Merge vào result
        def _pick_url(suggested: str) -> str:
            """Chỉ chấp nhận URL có trong danh sách thật, tránh hallucination."""
            if suggested and suggested in all_urls:
                return suggested
            # Tìm URL khớp domain
            if suggested:
                try:
                    s_host = urlparse(suggested).netloc
                    for u in all_urls:
                        if urlparse(u).netloc == s_host:
                            return u
                except Exception:
                    pass
            return ""

        result["opening_hours"]        = data.get("opening_hours") or ""
        result["opening_hours_source"] = _pick_url(data.get("opening_hours_source", "")) or (all_urls[0] if all_urls else "")
        result["opening_date"]         = data.get("opening_date") or ""
        result["opening_date_source"]  = _pick_url(data.get("opening_date_source", ""))
        result["opening_date_confidence"] = "medium" if result["opening_date"] else "none"
        result["is_closed"]            = bool(data.get("is_closed", False))
        result["closing_date"]         = data.get("closing_date")
        result["closing_date_source"]  = _pick_url(data.get("closing_date_source", ""))
        result["closing_date_confidence"] = "medium" if result["closing_date"] else "none"
        result["category"]             = data.get("category") or ""
        result["status_note"]          = data.get("status_note") or ""
        result["official_website"]     = data.get("official_website") or ""
        result["is_in_shopping_center"] = bool(data.get("is_in_shopping_center", False))
        result["shopping_center_name"] = data.get("shopping_center_name") or ""
        result["site_plan_url"]        = _pick_url(data.get("site_plan_url", ""))
        result["grounding_urls"]       = all_urls
        result["grounding_sources"]    = all_sources

        yield {"step": 3, "status": "done",
               "label": f"🦙 Ollama hoàn tất — tìm được dữ liệu",
               "partial": {k: result[k] for k in [
                   "opening_hours", "opening_hours_source",
                   "opening_date", "opening_date_source",
                   "category", "is_closed", "official_website",
                   "is_in_shopping_center",
               ]}}

    yield {"step": "final", "data": result}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_city_state(address: str) -> str:
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
        "grounding_sources":        [],
    }
