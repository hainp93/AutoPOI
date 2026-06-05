"""
Browser-based fetcher — Step 2c fallback.

Dùng Chrome thật với profile người dùng (cookies, session, login Google/Yelp/Facebook...)
để tự TÌM KIẾM và ĐỌC nội dung trang web thực sự.

Không phụ thuộc vào URLs từ Gemini — tự search Google, Yelp, Facebook.
Window đẩy ra ngoài màn hình (x=-3000) để chạy âm thầm.

Chiến lược tìm kiếm:
  1. Yelp search trực tiếp → sort by oldest review → lấy date đầu tiên
  2. Google search "{name} {city} grand opening" → đọc snippets + click results
  3. Yelp redirect URL từ Gemini grounding (nếu có)
  4. News/Facebook URLs từ Gemini grounding
"""

import re
import sys
import time
import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urlparse

logger = logging.getLogger(__name__)

# ── Config (set bằng setup_browser()) ────────────────────────────────────────
_cfg = {
    "profile_path": "",
    "profile_dir":  "Default",
    "offscreen_x":  -3000,
    "page_wait":    4,        # giây chờ trang load (tăng lên 4 để chính xác hơn)
}


def setup_browser(profile_path: str, profile_dir: str = "Default",
                  offscreen_x: int = -3000, page_wait: int = 4):
    """
    Cấu hình browser fetcher từ config.yaml.
    profile_path có thể là:
      - "auto"  → tự detect theo HOME của máy hiện tại (Windows/Mac/Linux)
      - "đường dẫn đầy đủ"  → dùng nguyên
      - "" / None  → không dùng browser (Step 2c bị tắt)
    """
    resolved = _resolve_profile_path(profile_path)
    _cfg.update({
        "profile_path": resolved,
        "profile_dir":  profile_dir,
        "offscreen_x":  offscreen_x,
        "page_wait":    page_wait,
    })
    if resolved:
        logger.info(f"BrowserFetcher: profile='{resolved}' / dir='{profile_dir}'")
        print(f"[BrowserFetcher] Configured: {Path(resolved).name} / {profile_dir}")
    else:
        logger.info("BrowserFetcher: không có profile, Step 2c sẽ bị bỏ qua.")


def _resolve_profile_path(profile_path: str) -> str:
    """
    Chuyển 'auto' thành đường dẫn thực theo OS và username hiện tại.
    Windows: C:\\Users\\<username>\\AppData\\Local\\Google\\Chrome\\User Data
    Mac:     ~/Library/Application Support/Google/Chrome
    Linux:   ~/.config/google-chrome
    """
    if not profile_path or profile_path.strip().lower() in ("", "none", "off", "false"):
        return ""
    if profile_path.strip().lower() != "auto":
        return profile_path.strip()   # Dùng nguyên giá trị trong config

    # Auto-detect theo OS
    home = Path.home()
    if sys.platform == "win32":
        candidate = home / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    elif sys.platform == "darwin":
        candidate = home / "Library" / "Application Support" / "Google" / "Chrome"
    else:  # Linux
        candidate = home / ".config" / "google-chrome"

    if candidate.exists():
        logger.info(f"BrowserFetcher auto-detect: {candidate}")
        return str(candidate)
    else:
        logger.warning(f"BrowserFetcher: không tìm thấy Chrome profile tại {candidate}")
        return ""


def is_configured() -> bool:
    """Trả True nếu đã cấu hình profile_path."""
    return bool(_cfg.get("profile_path"))


# ── Driver factory ────────────────────────────────────────────────────────────

def _create_driver(visible: bool = True):
    """
    Tạo Chrome WebDriver — luôn mở ở góc màn hình (100, 100) cửa sổ nhỏ.
    User thấy Chrome đang làm việc và dễ debug.

    Fallback chain:
      1. visible + profile thật (cookies đầy đủ)
      2. visible + không có profile (nếu profile bị lock bởi Chrome đang mở)
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        def _build_opts(with_profile: bool) -> Options:
            opts = Options()
            if with_profile and _cfg["profile_path"]:
                opts.add_argument(f'--user-data-dir={_cfg["profile_path"]}')
                opts.add_argument(f'--profile-directory={_cfg["profile_dir"]}')
            # Cửa sổ nhỏ ở góc trên-trái — nhìn thấy được, không chiếm màn hình
            opts.add_argument("--window-position=100,100")
            opts.add_argument("--window-size=900,600")
            opts.add_argument("--no-first-run")
            opts.add_argument("--no-default-browser-check")
            opts.add_argument("--disable-notifications")
            opts.add_argument("--disable-popup-blocking")
            opts.add_argument("--disable-extensions")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-gpu")
            opts.add_argument("--remote-debugging-port=0")
            # Tắt "Restore pages?" popup (Chrome didn't shut down correctly)
            opts.add_argument("--disable-restore-session-state")
            opts.add_argument("--disable-session-crashed-bubble")
            # Bypass bot detection
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            opts.add_experimental_option("useAutomationExtension", False)
            return opts

        def _patch(driver):
            driver.implicitly_wait(5)
            # Dismiss "Restore pages?" popup bằng cách navigate về blank
            # Nếu Chrome đang ở trang restore/crash, điều này sẽ bypass nó
            try:
                import time as _t
                _t.sleep(0.8)  # chờ Chrome ổn định
                current = driver.current_url or ""
                # Nếu đang ở trang new-tab hoặc chrome internal → force navigate
                if not current or "chrome://" in current or current == "data:," or current.endswith("newtab"):
                    driver.get("about:blank")
                    _t.sleep(0.3)
            except Exception:
                pass
            try:
                driver.execute_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
            except Exception:
                pass
            return driver

        # ── Thử 1: visible + profile thật (có cookies) ──
        try:
            driver = webdriver.Chrome(options=_build_opts(with_profile=True))
            logger.info(f"Chrome started at (100,100) with profile '{_cfg['profile_dir']}'")
            print(f"[BrowserFetcher] Chrome opened at (100,100) - profile={_cfg['profile_dir']}")
            return _patch(driver)
        except Exception as e:
            err = str(e).lower()
            is_profile_conflict = (
                "user data directory is already in use" in err
                or "already in use" in err
                or "devtoolsactiveport" in err
                or "chrome instance exited" in err
            )
            if is_profile_conflict:
                logger.warning("Chrome profile bị lock bởi Chrome đang mở -> thử không có profile")
                print("[BrowserFetcher] Profile locked -> trying without profile")
            else:
                logger.error(f"Chrome lỗi: {e}")
                print(f"[BrowserFetcher] Chrome error: {e}")
                raise

        # ── Thử 2: visible + không có profile ──
        driver = webdriver.Chrome(options=_build_opts(with_profile=False))
        logger.info("Chrome started at (100,100) WITHOUT profile (no cookies)")
        print("[BrowserFetcher] Chrome opened (no profile - Chrome was already running)")
        return _patch(driver)

    except ImportError:
        logger.error("selenium chưa được cài. Chạy: pip install selenium")
        return None
    except Exception as e:
        logger.error(f"Không thể khởi động Chrome: {e}")
        return None


# ── Public entry point ────────────────────────────────────────────────────────

def browser_find_opening_date(name: str, address: str,
                               candidate_sources: list = None) -> dict:
    """
    Dùng Chrome thật để tìm opening date — tự search Google + Yelp.

    Chiến lược (theo thứ tự):
      1. Tìm kiếm Yelp trực tiếp: yelp.com/search → mở trang → sort by oldest
      2. Tìm kiếm Google: "{name} {city} grand opening" → đọc snippets + click
      3. Visit Yelp redirect URLs từ Gemini sources
      4. Visit News/Facebook redirect URLs từ Gemini sources

    Args:
        name: tên POI (vd: "Valvoline Instant Oil Change")
        address: địa chỉ (vd: "1867 College Ave, Elmira, NY 14901")
        candidate_sources: list dict {url, title, domain} từ Gemini grounding

    Returns:
        dict với opening_date, opening_date_source, opening_date_confidence,
        opening_date_evidence; hoặc {} nếu không tìm được.
    """
    if not is_configured():
        logger.info("BrowserFetcher: chưa cấu hình, bỏ qua.")
        return {}

    driver = _create_driver()
    if not driver:
        return {}

    city_state = _extract_city_state(address)
    print(f"[BrowserFetcher] Searching for: '{name}' in '{city_state}'")

    try:
        # ── Chiến lược 1: Truy cập thẳng các link do Gemini cung cấp ──
        # Gemini đã search Google API và có sẵn URL chính xác -> ưu tiên cao nhất
        print("[BrowserFetcher] Strategy 1: Visit Gemini provided URLs...")
        sources = candidate_sources or []
        yelp_srcs = [s for s in sources if _is_domain(s, "yelp")]
        news_srcs = [s for s in sources
                     if not _is_domain(s, "yelp") and not _is_domain(s, "google")]
        fb_srcs   = [s for s in sources if _is_domain(s, "facebook")]
        other_ordered = yelp_srcs + news_srcs + fb_srcs

        for src in other_ordered[:5]:
            url = src.get("url", "")
            if not url:
                continue
            try:
                if _is_domain(src, "yelp"):
                    print(f"[BrowserFetcher]   -> Yelp: {url[:60]}...")
                    result = _try_yelp(driver, url)
                elif _is_domain(src, "facebook"):
                    print(f"[BrowserFetcher]   -> Facebook: {url[:60]}...")
                    result = _try_facebook(driver, url, name)
                else:
                    print(f"[BrowserFetcher]   -> News: {url[:60]}...")
                    result = _try_news_article(driver, url, name)

                if result and result.get("opening_date"):
                    domain = src.get("domain", "")
                    print(f"[BrowserFetcher] [OK] Found via {domain}: {result['opening_date']}")
                    return result
            except Exception as e:
                logger.warning(f"Browser lỗi trên {url[:60]}: {e}")
                continue

        # ── Chiến lược 2: Google search "grand opening" nếu links Gemini fail ──
        print("[BrowserFetcher] Strategy 2: Google 'grand opening' search...")
        result = _google_grand_opening_search(driver, name, city_state)
        if result and result.get("opening_date"):
            print(f"[BrowserFetcher] [OK] Found via Google search: {result['opening_date']}")
            return result

        print("[BrowserFetcher] [FAIL] Did not find opening date after trying all strategies.")
        return {}

    finally:
        try:
            driver.quit()
            print("[BrowserFetcher] Chrome closed.")
        except Exception:
            pass


# ── Strategy 1: Yelp direct search ───────────────────────────────────────────

def _yelp_direct_search(driver, name: str, city_state: str) -> dict:
    """
    Tìm trên Yelp.com bằng search form → mở trang business → sort oldest reviews.
    """
    try:
        search_url = (
            f"https://www.yelp.com/search?"
            f"find_desc={quote_plus(name)}&find_loc={quote_plus(city_state)}"
        )
        logger.debug(f"Yelp search: {search_url}")
        driver.get(search_url)
        time.sleep(_cfg["page_wait"])

        # Tìm link đến business page đầu tiên
        biz_url = _extract_first_yelp_biz_link(driver)
        if not biz_url:
            logger.debug("Yelp search: không tìm thấy business link")
            return {}

        # Sort by oldest reviews
        biz_base = biz_url.split("?")[0]
        oldest_url = f"{biz_base}?sort_by=date_asc"
        driver.get(oldest_url)
        time.sleep(_cfg["page_wait"])

        return _parse_yelp_oldest_review(driver, oldest_url)
    except Exception as e:
        logger.warning(f"Yelp direct search lỗi: {e}")
        return {}


def _extract_first_yelp_biz_link(driver) -> str:
    """Tìm link trang business Yelp đầu tiên trong kết quả search."""
    try:
        page_source = driver.page_source
        # Pattern: /biz/business-name trong search results
        matches = re.findall(r'href="(https://www\.yelp\.com/biz/[^"?]+)', page_source)
        if matches:
            return matches[0]
        # Fallback: relative /biz/ links
        matches = re.findall(r'href="(/biz/[^"?]+)"', page_source)
        if matches:
            return f"https://www.yelp.com{matches[0]}"
    except Exception:
        pass
    return ""


# ── Strategy 2: Google search ─────────────────────────────────────────────────

def _google_grand_opening_search(driver, name: str, city_state: str) -> dict:
    """
    Tìm kiếm Google: "{name} {city_state} grand opening"
    1. Đọc date từ search result snippets
    2. Click vào kết quả Yelp nếu có → đọc trang
    3. Click vào kết quả news nếu có → đọc trang
    """
    try:
        query = f'"{name}" {city_state} grand opening opening date'
        search_url = f"https://www.google.com/search?q={quote_plus(query)}"
        logger.debug(f"Google search: {search_url}")
        driver.get(search_url)
        time.sleep(_cfg["page_wait"])

        # ── Đọc date từ search snippets (không cần click) ──
        result = _extract_date_from_google_snippets(driver, name)
        if result and result.get("opening_date"):
            return result

        # ── Tìm và click các link ưu tiên (Facebook, Instagram, Yelp, News) ──
        # Mở rộng chiến lược: tìm các nền tảng mạng xã hội thường đăng lễ khai trương
        for platform in ["facebook.com", "instagram.com", "twitter.com", "yelp.com"]:
            platform_link = _find_google_result_link(driver, platform)
            if platform_link:
                logger.debug(f"Google -> {platform}: {platform_link[:70]}")
                if "facebook" in platform:
                    result = _try_facebook(driver, platform_link, name)
                elif "yelp" in platform:
                    result = _try_yelp(driver, platform_link)
                else:
                    # Các mạng xã hội khác dùng hàm đọc news/text chung vì chúng ta đọc text
                    result = _try_news_article(driver, platform_link, name)
                
                if result and result.get("opening_date"):
                    return result

        # ── Click vào kết quả news đầu tiên ──
        news_link = _find_google_news_link(driver)
        if news_link:
            logger.debug(f"Google → News: {news_link[:70]}")
            result = _try_news_article(driver, news_link, name)
            if result and result.get("opening_date"):
                return result

        # ── Thử search thêm: "{name} {city} opened YYYY" ──
        query2 = f'"{name}" {city_state} opened site:yelp.com OR site:yellowpages.com'
        driver.get(f"https://www.google.com/search?q={quote_plus(query2)}")
        time.sleep(_cfg["page_wait"])
        yelp_link2 = _find_google_result_link(driver, "yelp.com")
        if yelp_link2:
            result = _try_yelp(driver, yelp_link2)
            if result and result.get("opening_date"):
                return result

    except Exception as e:
        logger.warning(f"Google search lỗi: {e}")

    return {}


def _extract_date_from_google_snippets(driver, poi_name: str) -> dict:
    """
    Đọc date từ các đoạn text trong Google search results (snippets).
    Google thường hiển thị "Opened: March 2019" hay "Grand opening July 15, 2019".
    """
    try:
        body_text = driver.find_element("tag name", "body").text
        current_url = driver.current_url

        # Pattern: "opened" / "grand opening" + date
        patterns = [
            r'(?:grand\s+opening|opened?|now\s+open|first\s+open(?:ed)?)\s*:?\s*'
            r'((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
            r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
            r'\s+\d{1,2},?\s+\d{4})',
            r'(?:grand\s+opening|opened?)\s*:?\s*(\d{4})',
            r'(?:opened|established|founded)\s+in\s+(\d{4})',
        ]
        for pat in patterns:
            m = re.search(pat, body_text, re.IGNORECASE)
            if m:
                found = m.group(1).strip()
                iso = _parse_text_date(found)
                if iso:
                    snippet = body_text[max(0, m.start()-50):m.end()+50].strip()
                    return {
                        "opening_date":            iso,
                        "opening_date_source":     current_url,
                        "opening_date_confidence": "medium",
                        "opening_date_evidence":   f"Google snippet: '{snippet}'",
                    }
    except Exception:
        pass
    return {}


def _find_google_result_link(driver, domain_keyword: str) -> str:
    """Tìm link đến domain cụ thể trong Google results."""
    try:
        page_source = driver.page_source
        # Tìm href có chứa domain trong kết quả
        pattern = rf'href="(https?://(?:www\.)?{re.escape(domain_keyword)}[^"]*)"'
        matches = re.findall(pattern, page_source)
        # Lọc bỏ Google's own redirect links
        for m in matches:
            if "google.com" not in m:
                return m
    except Exception:
        pass
    return ""


def _find_google_news_link(driver) -> str:
    """Tìm link news article trong Google results (không phải Yelp, không phải Google)."""
    _skip = ("google.com", "yelp.com", "facebook.com", "twitter.com",
             "instagram.com", "wikipedia.org", "carfax.com")
    try:
        page_source = driver.page_source
        matches = re.findall(r'href="(https?://[^"]+)"', page_source)
        for m in matches:
            parsed = urlparse(m)
            domain = parsed.netloc.lower().replace("www.", "")
            if not any(skip in domain for skip in _skip):
                # Là news link nếu path có dạng /2019/... hay /article/...
                if (re.search(r'/20\d{2}/', m) or
                        re.search(r'/(article|news|story|post|press)/', m, re.I)):
                    return m
    except Exception:
        pass
    return ""


# ── Yelp page reader ──────────────────────────────────────────────────────────

def _try_yelp(driver, url: str) -> dict:
    """
    Mở trang Yelp (redirect URL hoặc direct URL), sort by oldest, lấy date đầu tiên.
    """
    logger.debug(f"Yelp: navigating to {url[:70]}...")
    driver.get(url)
    time.sleep(_cfg["page_wait"])

    actual_url = driver.current_url
    if "yelp.com" not in actual_url:
        logger.debug(f"Yelp: redirect không dẫn đến yelp.com (got {actual_url[:60]})")
        return {}

    return _parse_yelp_oldest_review(driver, actual_url)


def _parse_yelp_oldest_review(driver, current_url: str) -> dict:
    """Sort Yelp reviews by oldest, parse date của review đầu tiên."""
    # Sort by oldest (date_asc)
    biz_base  = current_url.split("?")[0]
    oldest_url = f"{biz_base}?sort_by=date_asc"
    if driver.current_url != oldest_url:
        driver.get(oldest_url)
        time.sleep(_cfg["page_wait"])

    page_source = driver.page_source
    final_url   = driver.current_url

    # ── Pattern 1: <time datetime="YYYY-MM-DD"> ──
    import datetime
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    
    dates_iso = re.findall(r'datetime=["\'](\d{4}-\d{2}-\d{2})["\']', page_source)
    if dates_iso:
        # Lọc bỏ các ngày trong tương lai (rác do CAPTCHA/bảo mật, vd: 2026-07-04)
        valid_dates = [d for d in dates_iso if d <= today_str]
        if valid_dates:
            earliest = min(valid_dates)
            return {
                "opening_date":            earliest,
                "opening_date_source":     final_url,
                "opening_date_confidence": "medium",
                "opening_date_evidence":   f"Yelp oldest review: {earliest} (sorted by date_asc)",
            }

    # ── Pattern 2: text "Month DD, YYYY" trong review section ──
    body_text  = _get_body_text(driver)
    date_match = _extract_earliest_date_from_text(body_text)
    if date_match:
        return {
            "opening_date":            date_match["iso"],
            "opening_date_source":     final_url,
            "opening_date_confidence": "medium",
            "opening_date_evidence":   f"Yelp oldest review: {date_match['text']}",
        }

    return {}


# ── News article reader ───────────────────────────────────────────────────────

def _try_news_article(driver, url: str, poi_name: str) -> dict:
    """
    Mở news article, tìm publication date + grand opening keywords.
    """
    logger.debug(f"News: navigating to {url[:70]}...")
    driver.get(url)
    time.sleep(_cfg["page_wait"])

    actual_url  = driver.current_url
    page_source = driver.page_source

    # ── Meta tag dates ──
    meta_patterns = [
        r'(?:published_time|datePublished|pubdate|article:published)["\s:=]+["\']?(\d{4}-\d{2}-\d{2})',
        r'<time[^>]+datetime=["\'](\d{4}-\d{2}-\d{2})',
    ]
    for pat in meta_patterns:
        m = re.search(pat, page_source, re.IGNORECASE)
        if m:
            date_iso  = m.group(1)
            body_text = _get_body_text(driver)
            poi_words = [w for w in poi_name.lower().split() if len(w) > 3][:3]
            if any(w in body_text.lower() for w in poi_words):
                return {
                    "opening_date":            date_iso,
                    "opening_date_source":     actual_url,
                    "opening_date_confidence": "high",
                    "opening_date_evidence":   f"News article published: {date_iso}",
                }

    # ── Grand opening mention trong body ──
    body_text = _get_body_text(driver)
    pattern = (
        r'(?:grand\s+opening|now\s+open(?:ed)?|opened?\s+(?:its\s+doors\s+)?(?:on|in)?'
        r'|ribbon[\s-]?cutting)\s+(?:on\s+)?'
        r'((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
        r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
        r'\s+\d{1,2},?\s+\d{4}|\d{4})'
    )
    m = re.search(pattern, body_text, re.IGNORECASE)
    if m:
        found_date = m.group(1).strip()
        iso = _parse_text_date(found_date)
        snippet = body_text[max(0, m.start()-30):m.end()+50].strip()
        return {
            "opening_date":            iso or found_date,
            "opening_date_source":     actual_url,
            "opening_date_confidence": "high" if iso else "medium",
            "opening_date_evidence":   f"'{snippet}'",
        }

    return {}


# ── Facebook reader ───────────────────────────────────────────────────────────

def _try_facebook(driver, url: str, poi_name: str) -> dict:
    """Mở Facebook page, tìm grand opening post."""
    logger.debug(f"Facebook: navigating to {url[:70]}...")
    driver.get(url)
    time.sleep(_cfg["page_wait"] + 2)  # FB cần thêm thời gian

    actual_url = driver.current_url
    if "facebook.com" not in actual_url:
        return {}

    body_text = _get_body_text(driver)
    pattern = (
        r'(?:grand\s+opening|now\s+open|we\s+are\s+open|doors\s+open)'
        r'.*?'
        r'((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
        r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
        r'\s+\d{1,2},?\s+\d{4}|\d{4})'
    )
    m = re.search(pattern, body_text, re.IGNORECASE | re.DOTALL)
    if m:
        found_date = m.group(1).strip()
        iso = _parse_text_date(found_date)
        return {
            "opening_date":            iso or found_date,
            "opening_date_source":     actual_url,
            "opening_date_confidence": "high" if iso else "medium",
            "opening_date_evidence":   f"Facebook grand opening post: '{found_date}'",
        }
    return {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_city_state(address: str) -> str:
    """Trích xuất 'City, STATE' từ địa chỉ US đầy đủ."""
    # "1867 College Ave, Elmira, NY 14901" → "Elmira, NY"
    parts = [p.strip() for p in address.split(",")]
    if len(parts) >= 3:
        city = parts[-3] if len(parts) > 3 else parts[-2]
        # State là parts[-2] (có thể kèm zip)
        state_zip = parts[-2].strip() if len(parts) >= 2 else ""
        state = re.match(r'([A-Z]{2})', state_zip)
        state = state.group(1) if state else state_zip
        return f"{city}, {state}"
    elif len(parts) == 2:
        return address.split(",", 1)[1].strip()
    return address


def _is_domain(source: dict, keyword: str) -> bool:
    domain = (source.get("domain") or "").lower()
    title  = (source.get("title")  or "").lower()
    url    = (source.get("url")    or "").lower()
    return keyword in domain or keyword in title or keyword in url


def _get_body_text(driver) -> str:
    try:
        return driver.find_element("tag name", "body").text
    except Exception:
        return ""


_MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _parse_text_date(text: str) -> Optional[str]:
    """Chuyển 'July 25, 2019' → '2019-07-25'. Trả None nếu không parse được."""
    text = text.strip()
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', text)
    if m:
        return text[:10]
    m = re.match(
        r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
        r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
        r'\s+(\d{1,2}),?\s+(\d{4})', text, re.IGNORECASE
    )
    if m:
        mon = _MONTH_MAP.get(m.group(1)[:3].lower(), "01")
        day = m.group(2).zfill(2)
        yr  = m.group(3)
        return f"{yr}-{mon}-{day}"
    m = re.match(
        r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
        r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
        r'\s+(\d{4})', text, re.IGNORECASE
    )
    if m:
        mon = _MONTH_MAP.get(m.group(1)[:3].lower(), "01")
        return f"{m.group(2)}-{mon}"
    m = re.match(r'(\d{4})$', text)
    if m:
        return m.group(1)
    return None


def _extract_earliest_date_from_text(text: str) -> Optional[dict]:
    """Tìm date sớm nhất trong text theo dạng 'Month DD, YYYY'."""
    pattern = (
        r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
        r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
        r'\s+(\d{1,2}),?\s+(\d{4})'
    )
    import datetime
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")

    found = []
    for m in re.finditer(pattern, text, re.IGNORECASE):
        full_text = m.group(0)
        iso = _parse_text_date(full_text)
        if iso and iso <= today_str:
            found.append({"iso": iso, "text": full_text})
    if not found:
        return None
    return min(found, key=lambda x: x["iso"])
