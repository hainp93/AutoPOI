"""
Browser-based fetcher — Step 2c fallback.

Dùng Chrome thật với profile người dùng (cookies, session, login Google/Yelp/Facebook...)
để đọc nội dung trang web thực sự. Window được đẩy ra ngoài màn hình (x=-3000)
để chạy âm thầm không ảnh hưởng đến công việc của người dùng.

Ưu điểm so với Gemini url_context:
  - Profile thật → bypass 403, bot detection
  - Có cookie/session → đọc được nội dung gated
  - Lấy được URL thực sau redirect (yelp.com/biz/...) để hiển thị đúng nguồn
"""

import re
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Config (set bằng setup_browser()) ────────────────────────────────────────
_cfg = {
    "profile_path": "",
    "profile_dir":  "Default",
    "offscreen_x":  -3000,
    "page_wait":    3,        # giây chờ trang load
}


def setup_browser(profile_path: str, profile_dir: str = "Default",
                  offscreen_x: int = -3000, page_wait: int = 3):
    """Cấu hình browser fetcher từ config.yaml."""
    _cfg.update({
        "profile_path": profile_path,
        "profile_dir":  profile_dir,
        "offscreen_x":  offscreen_x,
        "page_wait":    page_wait,
    })
    logger.info(f"BrowserFetcher configured: {profile_path} / {profile_dir}")


def is_configured() -> bool:
    """Trả True nếu đã cấu hình profile_path."""
    return bool(_cfg.get("profile_path"))


# ── Driver factory ────────────────────────────────────────────────────────────

def _create_driver():
    """
    Tạo Chrome WebDriver với:
    - Profile người dùng thật (cookies, logins)
    - Window đẩy ra ngoài màn hình (offscreen_x, -3000 mặc định)
    - Tắt automation flags để bypass bot detection

    Nếu Chrome đang mở cùng profile → thử lại không có profile
    (vẫn dùng Chrome thật nhưng không có cookies, tốt hơn headless).
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        def _build_opts(with_profile: bool) -> Options:
            opts = Options()
            if with_profile and _cfg["profile_path"]:
                opts.add_argument(f'--user-data-dir={_cfg["profile_path"]}')
                opts.add_argument(f'--profile-directory={_cfg["profile_dir"]}')
            opts.add_argument(f'--window-position={_cfg["offscreen_x"]},0')
            opts.add_argument("--window-size=1280,900")
            opts.add_argument("--no-first-run")
            opts.add_argument("--no-default-browser-check")
            opts.add_argument("--disable-notifications")
            opts.add_argument("--disable-popup-blocking")
            opts.add_argument("--disable-extensions")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            # Bypass bot detection
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            opts.add_experimental_option("useAutomationExtension", False)
            return opts

        # Thử 1: dùng profile thật (có cookies)
        try:
            driver = webdriver.Chrome(options=_build_opts(with_profile=True))
            driver.implicitly_wait(5)
            driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            logger.info(f"Chrome started with profile '{_cfg['profile_dir']}' (off-screen)")
            return driver
        except Exception as e:
            err = str(e).lower()
            if "user data directory is already in use" in err or "already in use" in err:
                logger.warning(
                    f"Chrome profile đang được dùng bởi Chrome đang mở → "
                    f"thử lại không có profile (không có cookies)."
                )
            else:
                raise  # Lỗi khác thì raise luôn

        # Thử 2: không có profile (Chrome đang mở conflict)
        driver = webdriver.Chrome(options=_build_opts(with_profile=False))
        driver.implicitly_wait(5)
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        logger.info("Chrome started WITHOUT profile (fallback, no cookies)")
        return driver

    except ImportError:
        logger.error("selenium chưa được cài. Chạy: pip install selenium")
        return None
    except Exception as e:
        logger.error(f"Không thể khởi động Chrome: {e}")
        return None


# ── Public entry point ────────────────────────────────────────────────────────

def browser_find_opening_date(name: str, address: str,
                               candidate_sources: list) -> dict:
    """
    Dùng Chrome thật để tìm opening date từ các candidate sources.

    Thứ tự ưu tiên:
      1. Yelp  — sort by oldest review → lấy date review đầu tiên
      2. News  — tìm publication date + grand opening keywords
      3. Facebook — tìm grand opening event post

    Args:
        name: tên POI
        address: địa chỉ
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

    try:
        # Phân loại sources theo domain
        yelp_srcs    = [s for s in candidate_sources if _is_domain(s, "yelp")]
        facebook_srcs= [s for s in candidate_sources if _is_domain(s, "facebook")]
        news_srcs    = [s for s in candidate_sources
                        if not _is_domain(s, "yelp") and not _is_domain(s, "facebook")]

        ordered = yelp_srcs + news_srcs + facebook_srcs

        for src in ordered[:5]:
            url    = src.get("url", "")
            domain = src.get("domain", "")
            if not url:
                continue
            try:
                if _is_domain(src, "yelp"):
                    result = _try_yelp(driver, url)
                elif _is_domain(src, "facebook"):
                    result = _try_facebook(driver, url, name)
                else:
                    result = _try_news_article(driver, url, name)

                if result and result.get("opening_date"):
                    logger.info(f"Browser tìm được: {result['opening_date']} từ {domain or url}")
                    return result

            except Exception as e:
                logger.warning(f"Browser lỗi trên {url[:60]}: {e}")
                continue

        logger.info("Browser: không tìm được ngày sau khi thử tất cả sources.")
        return {}

    finally:
        try:
            driver.quit()
            logger.info("Chrome browser closed.")
        except Exception:
            pass


# ── Yelp scraper ──────────────────────────────────────────────────────────────

def _try_yelp(driver, redirect_url: str) -> dict:
    """
    Mở Yelp qua redirect URL, sort reviews by oldest first,
    lấy date của review đầu tiên → xấp xỉ ngày khai trương.
    """
    logger.debug(f"Yelp: navigating to {redirect_url[:70]}...")
    driver.get(redirect_url)
    time.sleep(_cfg["page_wait"])

    actual_url = driver.current_url
    if "yelp.com" not in actual_url:
        logger.debug(f"Yelp: redirect không dẫn đến yelp.com (got {actual_url[:60]})")
        return {}

    # Sort by oldest (date_asc)
    yelp_base   = actual_url.split("?")[0]
    oldest_url  = f"{yelp_base}?sort_by=date_asc"
    driver.get(oldest_url)
    time.sleep(_cfg["page_wait"])

    page_source = driver.page_source
    final_url   = driver.current_url

    # ── Pattern 1: <time datetime="YYYY-MM-DD"> ──
    dates_iso = re.findall(r'datetime=["\'](\d{4}-\d{2}-\d{2})["\']', page_source)
    if dates_iso:
        earliest = min(dates_iso)
        return {
            "opening_date":            earliest,
            "opening_date_source":     final_url,
            "opening_date_confidence": "medium",
            "opening_date_evidence":   f"Yelp oldest review date: {earliest} (sorted by date ascending)",
        }

    # ── Pattern 2: text "Month DD, YYYY" trong reviews ──
    body_text = _get_body_text(driver)
    date_match = _extract_earliest_date_from_text(body_text)
    if date_match:
        return {
            "opening_date":            date_match["iso"],
            "opening_date_source":     final_url,
            "opening_date_confidence": "medium",
            "opening_date_evidence":   f"Yelp oldest review: {date_match['text']}",
        }

    return {}


# ── News article scraper ──────────────────────────────────────────────────────

def _try_news_article(driver, redirect_url: str, poi_name: str) -> dict:
    """
    Mở news article qua redirect URL, tìm:
    1. Publication date trong meta tags
    2. "Grand opening" / "now open" + date trong body text
    """
    logger.debug(f"News: navigating to {redirect_url[:70]}...")
    driver.get(redirect_url)
    time.sleep(_cfg["page_wait"])

    actual_url  = driver.current_url
    page_source = driver.page_source

    # ── Meta tag dates ──
    # <meta property="article:published_time" content="2019-07-25T10:00:00Z"/>
    # <meta name="pubdate" content="2019-07-25"/>
    meta_patterns = [
        r'(?:published_time|datePublished|pubdate|article:published)["\s:=]+["\']?(\d{4}-\d{2}-\d{2})',
        r'<time[^>]+datetime=["\'](\d{4}-\d{2}-\d{2})',
    ]
    for pat in meta_patterns:
        m = re.search(pat, page_source, re.IGNORECASE)
        if m:
            date_iso = m.group(1)
            # Chỉ trả về nếu article có mention POI name hoặc opening keywords
            body_text = _get_body_text(driver)
            poi_words = poi_name.lower().split()[:2]
            if any(w in body_text.lower() for w in poi_words):
                return {
                    "opening_date":            date_iso,
                    "opening_date_source":     actual_url,
                    "opening_date_confidence": "high",
                    "opening_date_evidence":   f"News article published: {date_iso}",
                }

    # ── "Grand opening" mention trong body ──
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
        return {
            "opening_date":            iso or found_date,
            "opening_date_source":     actual_url,
            "opening_date_confidence": "high" if iso else "medium",
            "opening_date_evidence":   f"News article: '{body_text[max(0,m.start()-30):m.end()+30].strip()}'",
        }

    return {}


# ── Facebook scraper ──────────────────────────────────────────────────────────

def _try_facebook(driver, redirect_url: str, poi_name: str) -> dict:
    """
    Mở Facebook page qua redirect URL, tìm grand opening post.
    (Yêu cầu user đã login Facebook trong Chrome profile)
    """
    logger.debug(f"Facebook: navigating to {redirect_url[:70]}...")
    driver.get(redirect_url)
    time.sleep(_cfg["page_wait"] + 1)  # FB cần thêm thời gian load

    actual_url = driver.current_url
    if "facebook.com" not in actual_url:
        return {}

    body_text = _get_body_text(driver)

    # Tìm grand opening post
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

def _is_domain(source: dict, keyword: str) -> bool:
    domain = (source.get("domain") or "").lower()
    title  = (source.get("title")  or "").lower()
    return keyword in domain or keyword in title


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
    # YYYY-MM-DD
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', text)
    if m:
        return text[:10]
    # Month DD, YYYY hoặc Month YYYY
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
    # Month YYYY only
    m = re.match(
        r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
        r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
        r'\s+(\d{4})', text, re.IGNORECASE
    )
    if m:
        mon = _MONTH_MAP.get(m.group(1)[:3].lower(), "01")
        return f"{m.group(2)}-{mon}"
    # Just year
    m = re.match(r'(\d{4})$', text)
    if m:
        return m.group(1)
    return None


def _extract_earliest_date_from_text(text: str) -> Optional[dict]:
    """
    Tìm date sớm nhất trong text theo dạng 'Month DD, YYYY'.
    Trả {iso, text} hoặc None.
    """
    pattern = (
        r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
        r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
        r'\s+(\d{1,2}),?\s+(\d{4})'
    )
    found = []
    for m in re.finditer(pattern, text, re.IGNORECASE):
        full_text = m.group(0)
        iso = _parse_text_date(full_text)
        if iso:
            found.append({"iso": iso, "text": full_text})

    if not found:
        return None
    # Trả về date sớm nhất
    return min(found, key=lambda x: x["iso"])
