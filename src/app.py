"""
AutoPOI Web Server — FastAPI backend
Chạy: python src/app.py
Mở:  http://localhost:8765
"""

import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src.data_fetcher.geocoder import geocode, build_ve_url, build_gm_url, build_gm_search_url
from src.data_fetcher.gemini_enricher import setup_gemini, enrich_poi

logging.basicConfig(level=logging.WARNING)

# ── Load config ───────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"

def load_config():
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Không tìm thấy config: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

config = load_config()
gemini_key = config.get("gemini", {}).get("api_key", "")
gemini_model_name = config.get("gemini", {}).get("model", "gemini-2.0-flash")

if not gemini_key or gemini_key == "YOUR_GEMINI_API_KEY":
    print("[ERROR] Chua dien Gemini API key vao config/config.yaml!")
    sys.exit(1)

gemini_model = setup_gemini(gemini_key, gemini_model_name)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="AutoPOI", version="1.0.0")

# Serve static files (frontend)
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class POIRequest(BaseModel):
    name: str
    address: str


@app.get("/")
async def index():
    """Serve the main UI."""
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="index.html không tìm thấy")
    return FileResponse(str(html_path))


@app.post("/api/lookup")
async def lookup_poi(req: POIRequest):
    """Tra cứu thông tin POI."""
    name = req.name.strip()
    address = req.address.strip()

    if not name or not address:
        raise HTTPException(status_code=400, detail="Cần nhập cả tên và địa chỉ")

    # Geocode
    geo = geocode(name, address)

    # Enrich với Gemini
    enriched = enrich_poi(gemini_model, name, address)

    # Build URLs
    if geo:
        ve_url = build_ve_url(geo["lat"], geo["lon"])
        gm_url = build_gm_url(geo["lat"], geo["lon"])
        lat = geo["lat"]
        lon = geo["lon"]
    else:
        ve_url = None
        gm_url = build_gm_search_url(name, address)
        lat = None
        lon = None

    return JSONResponse({
        "name": name,
        "address": address,
        "lat": lat,
        "lon": lon,
        "ve_url": ve_url,
        "gm_url": gm_url,
        **enriched,
    })


@app.get("/api/health")
async def health():
    return {"status": "ok", "model": gemini_model_name}


if __name__ == "__main__":
    print("\n🚀 AutoPOI Web UI đang chạy tại: http://localhost:8765\n")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
