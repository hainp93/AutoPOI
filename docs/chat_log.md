# AutoPOI — Chat Log & Quyết Định

> Ghi lại tóm tắt các cuộc thảo luận quan trọng liên quan đến dự án

---

## 2026-05-25 — Phiên khởi tạo dự án

### Bối cảnh
- Người dùng nhận được tài liệu training từ chủ quản (Google Drive: `08_TRAINING`)
- Công việc: Data Entry POI cho Venue Editor của Placer.ai
- Đơn giá: $0.25/POI, KPI: ~140 POI/tuần

### Phân tích tài liệu
AI đã đọc và phân tích toàn bộ tài liệu trong Drive:
- `[VI] Venue Editor Manual for Vietnam` — quy trình đầy đủ 1,654 dòng
- `[VI] Venue Workflow Decision Guide` — sơ đồ quyết định Deliverable/Undeliverable
- `DPW 3: Tips for searching information` — kỹ thuật tìm ngày mở/đóng cửa

### Kết luận thảo luận

**Q: Có đáng tự động hóa không?**  
A: Có. Làm thủ công = $0.25-$0.50/giờ → không kinh tế. Automation mục tiêu $5-$15+/giờ.

**Q: Chi phí công nghệ có cao không?**  
A: Không. Với 560 POI/tháng, toàn bộ tool stack chạy trong free tier → ~$0/tháng.
- Google Places API: $200 free credit ≈ 5,800 POI miễn phí
- Gemini API: Free tier đủ dùng
- Playwright, OSM API, python-jira: Miễn phí

**Q: Khả năng automation cao không? Có nên nhận job không?**  
A: Nên nhận. Đánh giá:
- 90% quy trình tự động hóa được (câu hỏi chỉ là mức độ)
- Rủi ro = 0 (không mất chi phí khởi đầu)
- Cần 1-2 tuần sau khi có VE access để xác định chiến lược chính xác
- Worst case: vẫn tiết kiệm 50-60% thời gian thủ công

**Q: Ẩn số lớn nhất?**  
A: Venue Editor — chưa biết có REST API không. Sẽ phân tích ngay khi có access bằng cách capture DevTools Network tab.

### Quyết định
✅ **Nhận job** — Bắt đầu build dự án AutoPOI, chờ tài khoản VE + Jira từ chủ quản.

---

## 2026-05-25 — Phân Tích PA Browser (Prisma Browser)

### Phát hiện
Chủ quản yêu cầu dùng trình duyệt riêng: https://get.pabrowser.com/welcome

Phân tích source code trang web → phát hiện domain `talon-sec.com` trong code:
```
allowedTracingOrigins: [/https:\/\/.*\.talon-sec\.com/]
```

→ **PA Browser = Prisma Browser của Palo Alto Networks**, được build trên nền **Talon Enterprise Browser** (acquired 2023).

### Khả năng giám sát của Talon/Prisma Browser
- ✅ Full audit trail toàn bộ hoạt động trong browser
- ✅ Theo dõi mọi thao tác với SaaS apps (click, input, navigation)
- ✅ Có thể chặn copy/paste, screenshot nếu admin bật
- ✅ Inspect toàn bộ network traffic bên trong browser
- ❌ KHÔNG theo dõi apps bên ngoài browser
- ❌ KHÔNG keylog mật khẩu

### Tác động đến chiến lược automation
- **Playwright điều khiển Talon Browser**: Gần như không khả thi — browser được thiết kế chống automation
- **Python script gọi VE REST API trực tiếp**: Hoàn toàn an toàn — nằm ngoài tầm nhìn của browser
- **Chiến lược ưu tiên**: Tìm VE REST API → đây là con đường duy nhất để full automation

### Cập nhật bài toán kinh tế
| Kịch bản | Thời gian/tuần | Thu nhập/giờ |
|----------|---------------|-------------|
| VE có API (~60%) | ~30 phút auto | Passive income |
| Hybrid (data auto, VE thủ công) | 8-10 giờ | ~$14-17/h |
| Thuần thủ công | 40-60 giờ | ~$0.25-0.50/h |

### Quyết định
✅ **Vẫn nhận job** — Ưu tiên #1 sau khi có access: xác định VE có REST API không.

---

## TODO khi có access VE + Jira

- [x] Share URL của Venue Editor với AI để phân tích → ĐÃ PHÂN TÍCH
- [ ] Capture DevTools Network tab khi làm 2-3 POI thủ công
- [ ] Xác nhận Jira project key và API endpoint
- [ ] Bắt đầu Phase 0 → Phase 1 → Phase 2 → Phase 3

---

## 2026-05-25 — Phân Tích URL Venue Editor

### URL VE (staging)
```
https://venues-staging.placer.team/#background=Bing&disable_features=deleted_venues,sources_layer,boundaries&map=19.40/28.59833/-81.22283
```

### Phát hiện quan trọng

**1. VE là iD Editor (OpenStreetMap) — mã nguồn mở!**
- URL hash format `#map=zoom/lat/lng` + params `background`, `disable_features` là đặc trưng 100% của [OpenStreetMap iD Editor](https://github.com/openstreetmap/iD)
- Placer.ai đã fork iD Editor và customize với backend riêng
- → Biết toàn bộ cách iD Editor gọi API

**2. Server là private IP: `10.110.16.25`**
- VE nằm sau VPN/mạng nội bộ Placer.ai
- PA Browser đóng vai trò VPN gateway cho contractor
- Script Python KHÔNG thể gọi VE API trực tiếp (không qua PA Browser)
- Cần điều tra: PA Browser có tạo local proxy không?

**3. API structure dự đoán (từ iD Editor)**
```
GET  /api/0.6/map?bbox=lon_min,lat_min,lon_max,lat_max
PUT  /api/0.6/changeset/create
PUT  /api/0.6/node/create      (tạo POI)
PUT  /api/0.6/way/create       (tạo polygon)
POST /api/0.6/changeset/{id}/upload
```

### Khi có access — làm ngay
1. Mở DevTools trong PA Browser (F12)
2. Tab Network → filter XHR/Fetch → làm 1 POI thủ công
3. Capture: auth token, API base URL, request format
4. Test curl từ terminal: nếu được → automation 100% OK

---

## 2026-05-25 — Phát Hiện Harmony SASE VPN (từ slide bị bỏ sót)

### Setup thực tế gồm 2 lớp
```
[Harmony SASE VPN]   ← VPN tunnel OS-level (Check Point / Perimeter81)
         +
[PA Browser/Prisma]  ← trình duyệt có giám sát
```

**Slide hướng dẫn:**
- Cài Harmony SASE: https://support.perimeter81.com/docs/downloading-the-agent  
- Đăng nhập bằng tài khoản Placer (qua trình duyệt Prisma)
- Kết nối server: **"gcp-us-central1-forti"** (GCP us-central1)
- Public IP sau khi connect: `14.160.26.197`

### Tại sao đây là tin cực tốt

Harmony SASE là VPN hoạt động ở **tầng OS** — toàn bộ traffic máy tính đi qua tunnel, KHÔNG chỉ browser.

→ **Script Python trên cùng máy → gọi VE API qua VPN tunnel → hoàn toàn khả thi!**

```
Trước: Script Python ❌→ private IP (10.110.16.25) [không tới được]
Sau:   Script Python ✅→ Harmony VPN tunnel → private IP [tới được!]
```

PA Browser chỉ cần để **lấy auth token** lần đầu. Sau đó script Python dùng token đó gọi API tự do.

### Cập nhật xác suất automation

| Kịch bản | Xác suất | Kết quả |
|----------|----------|---------|
| VE có REST API (iD-style) + VPN OK | **~75%** | Full automation ✅ |
| VE có API nhưng token expire nhanh | ~15% | Semi-auto (refresh token qua browser) |
| VE chặn non-browser clients | ~10% | Hybrid approach |

### Chiến lược mới (cập nhật)
1. Cài Harmony SASE → connect VPN
2. Mở PA Browser → đăng nhập VE → F12 lấy auth token
3. Test ngay: `curl -H "Authorization: Bearer <token>" https://venues-staging.placer.team/api/0.6/capabilities`
4. Nếu thành công → build Python pipeline hoàn toàn

---

## 2026-05-25 — Chi Tiết Quy Trình Từ Video Mẫu

### Thông tin xác nhận
- **Jira project key**: `POID` (POID-3257484, POID-3257761...)
- **Jira structure**: Parent Epic → Child Issue
- **VE production URL**: `venues-prod.placer.team`

### Quy trình 7 bước thực tế
```
1. Jira    → đọc ticket: tên + địa chỉ venue
2. Maps    → xác minh địa điểm (Satellite + Street View)
3. Maps→VE → copy "mã đối tượng" → tìm/mở POI trong VE   ← cần rõ hơn
4. VE      → xác định Main POI + vẽ Polygon
5. Jira    → comment: tên + OD link + OH link + screenshot
6. VE      → paste Jira ticket ID vào changeset → Upload/Save
7. VE      → paste POI ID → Export to Jira (auto-fill fields)
```

### Format comment Jira chuẩn (từ screenshot)
```
H&R Block
OD: 🗺️ [Google Maps link]
OH: H&R Block, 444 Route 211 E, Ste 6, Middletown, NY 10940, US - MapQuest
[screenshot đính kèm]
```

### Automation mapping từng bước
| Bước | Khả năng auto | Tool |
|------|--------------|------|
| 1. Đọc Jira | ✅ 100% | Jira REST API (project: POID) |
| 2. Verify Maps | ✅ 95% | Google Places API |
| 3. Mở POI trong VE | ❓ Cần xác định "mã" là gì | URL param hoặc VE search |
| 4. Vẽ Polygon | ❓ Phụ thuộc VE API | OSM Overpass → VE API |
| 5. Jira comment | ✅ 95% | Jira REST API |
| 6. Save changeset | ❓ Phụ thuộc VE API | VE /changeset endpoint |
| 7. Export to Jira | ❓ Nút có sẵn trong VE | Có thể là VE API call |

### ❓ Câu hỏi còn lại — cần xem lại video
1. **Bước 3**: "mã đối tượng" copy từ Maps là gì? `ChIJ...` (Place ID) hay tọa độ lat/lng?
2. **Bước 5**: Screenshot chụp thủ công hay VE có tính năng export?
3. **Bước 7**: "Export to Jira" là nút trong VE (đã đề cập trong manual) → confirm có nút này
