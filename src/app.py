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

if gemini_api_keys and any(k and not k.startswith("YOUR_") for k in gemini_api_keys):
    # Multi-key mode: mỗi step dùng 1 key riêng
    valid_keys = [k for k in gemini_api_keys if k and not k.startswith("YOUR_")]
    gemini_model = setup_gemini_multi(valid_keys, gemini_model_name)
    print(f"[AutoPOI] Multi-key mode: {len(valid_keys)} Gemini API key(s) — mỗi step dùng 1 key riêng")
elif gemini_api_key and gemini_api_key != "YOUR_GEMINI_API_KEY":
    # Single-key mode (backward compat)
    gemini_model = setup_gemini(gemini_api_key, gemini_model_name)
    print("[AutoPOI] Single-key mode: 1 Gemini API key dùng cho cả 3 step")
else:
    print("[ERROR] Chưa điền Gemini API key vào config/config.yaml!")
    print("        Điền api_key (1 key) hoặc api_keys (list 3 key) trong section [gemini]")
    sys.exit(1)

# ── Setup Chrome browser (Step 2c — primary method for Opening Date) ──────────
chrome_cfg  = config.get("chrome", {})
chrome_path = chrome_cfg.get("profile_path", "")   # "auto" → tự detect, "" → tắt
print(f"[AutoPOI] Chrome config: profile_path='{chrome_path}', dir='{chrome_cfg.get('profile_dir','Default')}'")
if chrome_path:
    browser_fetcher.setup_browser(
        profile_path=chrome_path,
        profile_dir=chrome_cfg.get("profile_dir", "Default"),
        offscreen_x=chrome_cfg.get("offscreen_x", -3000),
        page_wait=chrome_cfg.get("page_wait", 4),
    )
    if browser_fetcher.is_configured():
        resolved = browser_fetcher._cfg["profile_path"]
        print(f"[AutoPOI] ✓ Chrome browser READY: '{resolved}' / '{chrome_cfg.get('profile_dir','Default')}'")
        print(f"[AutoPOI]   Step 2c sẽ tự động search Google + Yelp cho mỗi POI")
    else:
        print(f"[AutoPOI] ✗ Chrome browser DISABLED: profile_path='{chrome_path}' không tồn tại")
        print(f"[AutoPOI]   Kiểm tra lại đường dẫn Chrome profile trong config.yaml")
else:
    print("[AutoPOI]   Chrome browser: không cấu hình (bỏ qua Step 2c)")


app = FastAPI(title="AutoPOI", version="2.0.0")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class POIRequest(BaseModel):
    name: str
    address: str


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


@app.get("/")
async def index():
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="index.html khong tim thay")
    return FileResponse(str(html_path))


# ── SSE Streaming endpoint (new) ──────────────────────────────────────────────
@app.get("/api/lookup/stream")
async def lookup_stream(name: str, address: str):
    """
    Server-Sent Events endpoint — stream kết quả từng bước.
    Frontend dùng EventSource để nhận updates realtime.
    """
    name = name.strip()
    address = address.strip()

    if not name or not address:
        raise HTTPException(status_code=400, detail="Can nhap ca ten va dia chi")

    async def generate():
        def send(data: dict) -> str:
            return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        # ── Step 0: Geocoding ─────────────────────────────────────────────────
        yield send({"step": 0, "status": "running",
                    "label": "Geocoding địa chỉ..."})
        geo = await asyncio.get_event_loop().run_in_executor(
            None, geocode, name, address
        )
        geo_info = _build_geo_info(name, address, geo)
        yield send({"step": 0, "status": "done",
                    "label": "Geocoding xong",
                    "partial": geo_info})

        # ── Steps 1-3: Gemini enrichment — stream real-time qua queue ─────────
        full_result = {"name": name, "address": address, **geo_info}
        _SENTINEL = object()
        q: queue.Queue = queue.Queue()

        def _producer():
            """Chạy trong thread riêng, đẩy từng event vào queue ngay khi có."""
            try:
                for event in enrich_poi_stream(gemini_model, name, address):
                    q.put(event)
            finally:
                q.put(_SENTINEL)  # báo hiệu kết thúc

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


@app.get("/api/health")
async def health():
    mode = "multi-key" if isinstance(gemini_model, dict) else "single-key"
    key_count = len(gemini_model["keys"]) if isinstance(gemini_model, dict) else 1
    return {"status": "ok", "model": gemini_model_name,
            "mode": mode, "keys": key_count, "version": "2.1.0"}


if __name__ == "__main__":
    print("\nAutoPOI Web UI dang chay tai: http://localhost:8765\n")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
