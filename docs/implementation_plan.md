# 🛠️ Kế Hoạch Kỹ Thuật — AutoPOI

> Cập nhật lần cuối: 2026-05-25  
> Trạng thái: Chờ access Venue Editor + Jira

---

## Kiến Trúc Pipeline

```
┌─────────────────────────────────────────────────────────┐
│                    AUTOPOI PIPELINE                     │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  [Jira Board]                                           │
│      ↓ đọc tickets mới (To Do)                         │
│  [Orchestrator] ── quản lý queue, parallel jobs        │
│      ↓                                                  │
│  [Step 1: Venue Verification]                           │
│      → Google Places API: xác minh tồn tại             │
│      → Lấy: place_id, lat/lng, tên, địa chỉ, status   │
│      ↓                                                  │
│  [Step 2: Data Enrichment]                              │
│      → Opening Hours: Places API → format VE           │
│      → Opening Date: Web search + LLM extract          │
│      → Closing Date: Web search + LLM extract          │
│      → Building footprint: OpenStreetMap Overpass API  │
│      → Classify: Deliverable / Undeliverable?          │
│      ↓                                                  │
│  [Step 3: Venue Editor Submit]                          │
│      → Kịch bản A: Direct API calls                   │
│      → Kịch bản B: Playwright browser automation      │
│      → Tạo/tìm POI, vẽ polygon, gắn tags              │
│      → Export to Jira                                  │
│      ↓                                                  │
│  [Step 4: Jira Update]                                  │
│      → Điền fields, thêm comment, Resolve ticket       │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

---

## Tech Stack

```yaml
language: Python 3.11+
browser_automation: Playwright + playwright-stealth
http_client: httpx (async)
llm: google-generativeai (Gemini 2.0 Flash — free tier)
maps: googlemaps (Google Places API)
osm: overpy (OpenStreetMap Overpass API)
jira: jira (python-jira library)
database: SQLite (tracking state, retry logic)
scheduler: APScheduler hoặc cron
config: PyYAML
logging: loguru
```

---

## Cấu Trúc Code

```
src/
├── main.py                      # Entry point, orchestrator
├── config.py                    # Load config từ YAML
├── pipeline/
│   ├── __init__.py
│   ├── orchestrator.py          # Queue management, parallel processing
│   └── job.py                   # POI job state machine
├── data_fetcher/
│   ├── __init__.py
│   ├── google_places.py         # Places API: verify + hours
│   ├── date_finder.py           # Search + LLM: opening/closing date
│   ├── osm_footprint.py         # OSM building polygons
│   └── classifier.py            # Deliverable vs Undeliverable logic
├── venue_editor/
│   ├── __init__.py
│   ├── ve_client.py             # Interface (API hoặc Playwright)
│   ├── ve_api.py                # Nếu VE có REST API
│   ├── ve_playwright.py         # Nếu cần browser automation
│   └── polygon_utils.py         # Convert OSM coords → VE format
└── jira/
    ├── __init__.py
    └── jira_client.py           # Read tickets, update fields, comments
```

---

## Lộ Trình (Tasks)

### Phase 0 — Khảo Sát (sau khi có access) — Ưu tiên #1
- [ ] Làm thủ công 3-5 POI đầu
- [ ] Mở DevTools → Network tab → capture toàn bộ API calls của VE
- [ ] Xác định: VE có REST API không? Endpoint nào?
- [ ] Xác định VE URL structure (tham số lat/lng?)
- [ ] Ghi lại Jira project key, API endpoint

**→ Kết quả Phase 0 xác định toàn bộ chiến lược Phase 2**

---

### Phase 1 — Data Pipeline (3-5 ngày, làm ngay không cần đợi VE)
- [ ] `jira_client.py`: Đọc tickets `To Do`, extract tên + địa chỉ
- [ ] `google_places.py`: Text Search → Place Details (hours, status, coords)
- [ ] Format Opening Hours: `[{day, open_time, close_time}]` → `mo 09:00-21:00; tu-fr...`
- [ ] `date_finder.py`: Web search (Serper/scrape) + Gemini extract ngày
- [ ] `osm_footprint.py`: Query Overpass API → building polygon tại coords
- [ ] `classifier.py`: Logic phân loại Deliverable/Undeliverable
- [ ] Unit tests + test với 20 địa chỉ thực tế

---

### Phase 2 — Venue Editor Automation (5-10 ngày, sau Phase 0)
#### Nhánh A: VE có REST API
- [ ] Reverse-engineer API endpoints từ network captures
- [ ] `ve_api.py`: POST create POI, POST create polygon, POST add tags
- [ ] Test với 5 POI thực tế, kiểm tra kết quả

#### Nhánh B: Playwright (nếu không có API)
- [ ] `ve_playwright.py`: Login, navigate to coordinates
- [ ] Automate: tạo POI, fill name/address
- [ ] Automate: draw polygon (inject JS coordinates vào map canvas)
- [ ] Automate: fill tags, save changeset
- [ ] Stealth mode để tránh detection
- [ ] Test với 5 POI thực tế

---

### Phase 3 — Integration & Optimization (3-5 ngày)
- [ ] Kết nối end-to-end toàn bộ pipeline
- [ ] Error handling: retry logic, fallback cho từng bước
- [ ] Parallel processing: chạy 3-5 jobs đồng thời
- [ ] Logging dashboard: theo dõi progress, lỗi, thống kê
- [ ] Test batch 50 POI liên tục
- [ ] Đo tốc độ thực tế (POI/giờ)

---

## Config Template

```yaml
# config/config.yaml (không commit lên git)

google:
  places_api_key: "YOUR_KEY_HERE"
  # Free tier: $200 credit/tháng ≈ 5,800 POI miễn phí

gemini:
  api_key: "YOUR_KEY_HERE"
  model: "gemini-2.0-flash"
  # Free tier: đủ dùng

jira:
  url: "https://YOUR_COMPANY.atlassian.net"
  username: "your@email.com"
  api_token: "YOUR_JIRA_TOKEN"
  project_key: "POID"  # cập nhật sau khi có access

venue_editor:
  url: "https://..."  # cập nhật sau khi có access
  username: "your@email.com"
  password: "YOUR_PASSWORD"
  # Hoặc API key nếu VE có API

pipeline:
  parallel_jobs: 3          # Số job chạy song song
  delay_between_jobs: 2.5   # Giây, để tránh rate limit
  max_retries: 3
  
osm:
  overpass_url: "https://overpass-api.de/api/interpreter"  # Free
```

---

## Ghi Chú Quan Trọng

> [!WARNING]
> **Về ToS của Venue Editor**: Cần kiểm tra Terms of Service trước khi deploy automation.
> Nếu bị ban tài khoản → mất toàn bộ doanh thu.
> Chiến lược an toàn: rate limit thấp, human-like delays, tránh chạy 24/7 liên tục.

> [!TIP]
> **Thứ tự ưu tiên khi gặp lỗi tìm Opening Date**:
> 1. Google Search news articles
> 2. Facebook page của venue (search "grand opening", "soft opening")  
> 3. Instagram page
> 4. Yelp reviews (earliest review date)
> 5. Google Maps photos (earliest photo date)
> 6. Street View historical images
> 7. Default fallback: `2017-01-01` (nếu mở trước 2017)

> [!NOTE]  
> **Format Opening Hours trong VE**:
> - Chuẩn: `mo 09:00-21:00; tu-fr 09:00-22:00; sa 10:00-20:00; su 11:00-18:00`
> - 24/7: `mo-su 00:00-24:00`
> - Closed một ngày: bỏ ngày đó khỏi string
> - Multiple shifts: `mo 11:30-14:00,17:00-21:00`

---

## Câu Hỏi Cần Làm Rõ (khi có access)

1. **Venue Editor URL** là gì? Có tham số lat/lng trong URL không?
2. **VE có REST API** không? → Capture DevTools Network tab khi làm thủ công
3. **Jira project key** là gì? (`POID-XXXXXX` → key là `POID`?)
4. **Jira API** có bật không? Cần token loại nào?
5. **Rate limits** của VE? Bao nhiêu edits/phút được chấp nhận?
6. **Polygon có bắt buộc** cho mọi POI không, hay chỉ Deliverable?
7. **Có cần làm 35m cleanup** cho tất cả hay chỉ một số loại?
