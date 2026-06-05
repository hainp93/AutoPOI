"""
AutoPOI Web Server — FastAPI backend
Chạy: python src/app.py
Mở:  http://localhost:8765
"""

import sys
import json
import logging
import asyncio
import queue
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from src.data_fetcher.geocoder import geocode, build_ve_url, build_gm_url, build_gm_search_url
from src.data_fetcher.gemini_enricher import (
    setup_gemini, setup_gemini_multi, enrich_poi, enrich_poi_stream
)
from src.data_fetcher import browser_fetcher
from src.data_fetcher import searxng_enricher

logging.basicConfig(level=logging.WARNING)

# ── Load config ───────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"


def load_config():
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Khong tim thay config: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


config = load_config()
gemini_cfg = config.get("gemini", {})
gemini_model_name = gemini_cfg.get("model", "gemini-2.0-flash")

# Ư u tiên dùng api_keys (multi-key) nếu có, fallback về api_key (single)
gemini_api_keys = gemini_cfg.get("api_keys", [])
gemini_api_key  = gemini_cfg.get("api_key", "")

# ── Detect active engine ─────────────────────────────────────────────────────
active_engine = config.get("engine", "gemini").lower().strip()

gemini_model = None

if active_engine == "gemini" or active_engine not in ("searxng",):
    active_engine = "gemini"
    if gemini_api_keys and any(k and not k.startswith("YOUR_") for k in gemini_api_keys):
        valid_keys = [k for k in gemini_api_keys if k and not k.startswith("YOUR_")]
        gemini_model = setup_gemini_multi(valid_keys, gemini_model_name)
        print(f"[AutoPOI] 🧠 Brain: Gemini | Multi-key mode: {len(valid_keys)} key(s)")
    elif gemini_api_key and gemini_api_key != "YOUR_GEMINI_API_KEY":
        gemini_model = setup_gemini(gemini_api_key, gemini_model_name)
        print("[AutoPOI] 🧠 Brain: Gemini | Single-key mode")
    else:
        print("[ERROR] engine=gemini nhưng chưa điền Gemini API key!")
        sys.exit(1)

# ── Luôn khởi tạo searxng_cfg (cho phép UI switch sang bất cứ lúc nào) ───────
_raw_sx = config.get("searxng", {})
_raw_ol = config.get("ollama",  {})
searxng_cfg = searxng_enricher.build_config({
    "search_backend":  _raw_sx.get("search_backend", "duckduckgo"),
    "searxng_url":    _raw_sx.get("url",   "http://localhost:8888"),
    "max_results":    _raw_sx.get("max_results", 5),
    "ollama_url":     _raw_ol.get("url",   "http://localhost:11434"),
    "ollama_model":   _raw_ol.get("model", "llama3.1:8b"),
    "ollama_timeout": _raw_ol.get("timeout", 120),
})

if active_engine == "searxng":
    # In trạng thái dịch vụ khi dùng engine searxng
    svc = searxng_enricher.check_services(searxng_cfg)
    sx_ok = "✓" if svc["searxng"] else "✗"
    ol_ok = "✓" if svc["ollama"]  else "✗"
    models = svc.get("ollama_models", [])
    backend_label = "DuckDuckGo" if searxng_cfg["search_backend"] == "duckduckgo" else "SearXNG"
    print(f"[AutoPOI] 🧊 Brain: {backend_label} + Ollama")
    print(f"[AutoPOI]   Search   {sx_ok} {backend_label}")
    print(f"[AutoPOI]   Ollama   {ol_ok} {searxng_cfg['ollama_url']} | model: {searxng_cfg['ollama_model']}")
    if models:
        print(f"[AutoPOI]   Models available: {', '.join(models[:5])}")
    if svc["errors"]:
        for err in svc["errors"]:
            print(f"[AutoPOI] ⚠️  {err}")
    if not svc["searxng"] or not svc["ollama"]:
        print("[AutoPOI] ⚠️  Một số dịch vụ chưa sẵn sàng. Khởi động vẫn tiếp tục nhưng có thể lỗi khi tra cứu.")

# ── Setup Chrome browser (Step 2c — primary method for Opening Date) ──────────
chrome_cfg   = config.get("chrome", {})
# Nếu không có section chrome → tự động dùng "auto"
chrome_path  = chrome_cfg.get("profile_path", "auto")
if chrome_path is None:
    chrome_path = "auto"
chrome_dir   = chrome_cfg.get("profile_dir",  "Default")
offscreen_x  = chrome_cfg.get("offscreen_x",  -3000)
page_wait    = chrome_cfg.get("page_wait",    5)

print(f"[AutoPOI] Chrome config: profile_path='{chrome_path}', dir='{chrome_dir}'")
browser_fetcher.setup_browser(
    profile_path=chrome_path,
    profile_dir=chrome_dir,
    offscreen_x=offscreen_x,
    page_wait=page_wait,
)
if browser_fetcher.is_configured():
    resolved = browser_fetcher._cfg["profile_path"]
    print(f"[AutoPOI] ✓ Chrome browser READY: '{resolved}' / '{chrome_dir}'")
    print(f"[AutoPOI]   Step 2c sẽ tự động search Google + Yelp cho mỗi POI")
else:
    print(f"[AutoPOI] ✗ Chrome browser DISABLED: không tìm thấy Chrome profile")
    print(f"[AutoPOI]   Kiểm tra Chrome đã được cài chưa, hoặc đặt chrome.profile_path trong config.yaml")


app = FastAPI(title="AutoPOI", version="3.0.0")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class POIRequest(BaseModel):
    name: str
    address: str


class POIStreamRequest(BaseModel):
    name: str
    address: str
    engine: str = ""   # override engine from config


def _build_geo_info(name: str, address: str, geo: dict | None) -> dict:
    """Build geo-related fields từ geocoding result."""
    if geo:
        return {
            "lat": geo["lat"],
            "lon": geo["lon"],
            "ve_url": build_ve_url(geo["lat"], geo["lon"]),
            "gm_url": build_gm_url(geo["lat"], geo["lon"]),
        }
    return {
        "lat": None,
        "lon": None,
        "ve_url": None,
        "gm_url": build_gm_search_url(name, address),
    }


@app.get("/debug/chrome-status")
async def chrome_status():
    """Kiểm tra trạng thái Chrome browser config cho Step 2c."""
    from pathlib import Path as _Path
    cfg = browser_fetcher._cfg
    profile_path = cfg.get("profile_path", "")
    return {
        "configured":    browser_fetcher.is_configured(),
        "profile_path":  profile_path,
        "profile_dir":   cfg.get("profile_dir", ""),
        "profile_exists": _Path(profile_path).exists() if profile_path else False,
        "offscreen_x":   cfg.get("offscreen_x", -3000),
        "page_wait":     cfg.get("page_wait", 4),
        "message": (
            "✓ Chrome sẵn sàng — Step 2c sẽ chạy với mỗi POI" if browser_fetcher.is_configured()
            else f"✗ Chrome chưa ready — kiểm tra profile_path trong config.yaml"
        )
    }


@app.get("/debug/test-chrome")
async def test_chrome():
    """
    Mở Chrome ở vị trí NHÌN THẤY ĐƯỢC (x=100, y=100) để test.
    Navigate sang Yelp trong 5 giây rồi tự đóng.
    Dùng để xác nhận Chrome hoạt động trước khi chạy thật.
    """
    import threading
    import time as _time

    if not browser_fetcher.is_configured():
        return {
            "status": "error",
            "message": "Chrome chưa configured — xem /debug/chrome-status để debug"
        }

    result = {}

    def _test():
        import time as _t
        try:
            # visible=True → Chrome hiện ở (100,100) để user thấy
            driver = browser_fetcher._create_driver(visible=True)
            if not driver:
                result["status"]  = "error"
                result["message"] = "Không tạo được Chrome driver — xem console log"
                return
            driver.get("https://www.yelp.com/search?find_desc=Valvoline&find_loc=New+York")
            _t.sleep(5)
            title = driver.title
            driver.quit()
            result["status"]  = "success"
            result["title"]   = title
            result["message"] = f"✓ Chrome hoạt động! Title: '{title}'"
        except Exception as e:
            result["status"]  = "error"
            result["message"] = str(e)

    t = threading.Thread(target=_test, daemon=True)
    t.start()
    t.join(timeout=30)
    return result


@app.get("/")
async def index():
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="index.html khong tim thay")
    return FileResponse(str(html_path))


# ── SSE Streaming endpoint (new) ──────────────────────────────────────────────
@app.get("/api/lookup/stream")
async def lookup_stream(name: str, address: str, engine: str = ""):
    """
    Server-Sent Events endpoint — stream kết quả từng bước.
    Frontend dùng EventSource để nhận updates realtime.
    """
    name = name.strip()
    address = address.strip()
    use_engine = (engine or active_engine).lower()

    if not name or not address:
        raise HTTPException(status_code=400, detail="Can nhap ca ten va dia chi")

    async def generate():
        def send(data: dict) -> str:
            return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        # ── Step 0: Geocoding ─────────────────────────────────────────────────────
        yield send({"step": 0, "status": "running",
                    "label": "Geocoding địa chỉ..."})
        geo = await asyncio.get_event_loop().run_in_executor(
            None, geocode, name, address
        )
        geo_info = _build_geo_info(name, address, geo)
        yield send({"step": 0, "status": "done",
                    "label": "Geocoding xong",
                    "partial": geo_info})

        # ── Steps 1-3: Enrichment — route đúng engine ──────────────────────
        full_result = {"name": name, "address": address, **geo_info}
        full_result["engine_used"] = use_engine
        _SENTINEL = object()
        q: queue.Queue = queue.Queue()

        def _producer():
            """Chạy trong thread riêng, đẩy từng event vào queue ngay khi có."""
            try:
                if use_engine == "searxng" and searxng_cfg:
                    for event in searxng_enricher.enrich_poi_stream_searxng(
                        searxng_cfg, name, address
                    ):
                        q.put(event)
                else:
                    for event in enrich_poi_stream(gemini_model, name, address):
                        q.put(event)
            except Exception as e:
                q.put({"step": "error", "status": "error", "label": str(e)})
            finally:
                q.put(_SENTINEL)

        threading.Thread(target=_producer, daemon=True).start()

        # Đọc từ queue và yield ra SSE ngay lập tức
        loop = asyncio.get_event_loop()
        while True:
            # Chờ event tiếp theo mà không block event loop
            event = await loop.run_in_executor(None, q.get)
            if event is _SENTINEL:
                break
            if event.get("step") == "final":
                full_result.update(event["data"])
            else:
                yield send(event)

        # ── Final result ──────────────────────────────────────────────────────
        yield send({"step": "final", "data": full_result})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Legacy POST endpoint (backward compat) ────────────────────────────────────
@app.post("/api/lookup")
async def lookup_poi(req: POIRequest):
    """Tra cứu thông tin POI (sync, legacy)."""
    name = req.name.strip()
    address = req.address.strip()

    if not name or not address:
        raise HTTPException(status_code=400, detail="Can nhap ca ten va dia chi")

    geo = geocode(name, address)
    enriched = enrich_poi(gemini_model, name, address)
    geo_info = _build_geo_info(name, address, geo)

    return JSONResponse({
        "name": name,
        "address": address,
        **geo_info,
        **enriched,
    })


@app.get("/api/engines")
async def engines_status():
    """Trả về danh sách engines và trạng thái dịch vụ."""
    result = {
        "current": active_engine,
        "gemini": {
            "available": gemini_model is not None,
            "model": gemini_model_name if gemini_model else None,
        },
        "searxng": {
            "available": searxng_cfg is not None,
        }
    }
    if searxng_cfg:
        svc = searxng_enricher.check_services(searxng_cfg)
        result["searxng"].update({
            "searxng_ok":  svc["searxng"],
            "ollama_ok":   svc["ollama"],
            "ollama_model": searxng_cfg.get("ollama_model"),
            "ollama_models": svc.get("ollama_models", []),
            "errors": svc.get("errors", []),
        })
    return result


@app.get("/api/health")
async def health():
    mode = "multi-key" if isinstance(gemini_model, dict) else "single-key"
    key_count = len(gemini_model["keys"]) if isinstance(gemini_model, dict) else (1 if gemini_model else 0)
    return {"status": "ok", "engine": active_engine, "model": gemini_model_name,
            "mode": mode, "keys": key_count, "version": "3.0.0"}


if __name__ == "__main__":
    print("\nAutoPOI Web UI dang chay tai: http://localhost:8765\n")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
