# 📊 Phân Tích Tài Liệu Training & Đánh Giá Khả Năng Tự Động Hóa

> Nguồn: Google Drive `08_TRAINING` — https://drive.google.com/drive/folders/1La-4pJCzzczCaUQe0TEsuEdthzq-qAJO  
> Ngày phân tích: 2026-05-25

---

## 1. Tổng Quan Công Việc

Đây là công việc **Data Entry** cho hệ thống **Venue Editor của Placer.ai** — nền tảng phân tích foot traffic tại các địa điểm ở Hoa Kỳ.

- **Đơn giá**: $0.25/POI
- **KPI**: ~140 POI/tuần (~$35/tuần nếu thủ công hoàn toàn — không đáng)
- **Mục tiêu của dự án này**: Tự động hóa để đạt ~$140/tuần với effort tối thiểu

---

## 2. Các File Tài Liệu Trong Drive

| File | Loại | Nội dung chính |
|------|------|---------------|
| `[VI] Venue Editor Manual for Vietnam` | Google Doc | Hướng dẫn đầy đủ quy trình làm việc (tiếng Việt) |
| `[VI] Venue Workflow Decision Guide` | Google Doc | Sơ đồ quyết định: Deliverable vs Undeliverable |
| `DPW 3: Tips for searching information` | Slides | Kỹ thuật tìm Opening/Closing Date |
| `Hướng dẫn cài đặt` | Slides | Cài Tampermonkey + Google Maps Helper script |
| `Tài liệu Data entry (slides).pptx` | PowerPoint | Tổng hợp quy trình |
| `Jira Manual.mp4` | Video | Hướng dẫn Jira |
| `Example Videos/` | Subfolder | Video ví dụ minh họa |

---

## 3. Quy Trình Thủ Công Hiện Tại (4 Bước)

### Bước 1: Xác minh địa điểm — Google Maps
- Nhận task từ Jira: format `Tên + Địa chỉ` (US only)
- Mở Google Maps → Satellite View + Street View
- Xác định hình dạng, vị trí thực tế
- Nếu trong Shopping Center: tìm site plan/floor directory

### Bước 2: Thu thập dữ liệu
| Dữ liệu | Nguồn tìm kiếm |
|---------|---------------|
| **Opening Hours** | Google Maps (ưu tiên) + website chính thức (chọn cái dài hơn) |
| **Opening Date** | Google Search, Facebook, Instagram, Yelp, ảnh Google Maps |
| **Closing Date** | Bài báo, mạng xã hội, Google Search |
| **Site Plan** | Website SC + Google Search: "site plan", "leasing", "directory map" |

### Bước 3: Làm việc trong Venue Editor
1. Mở VE tại đúng tọa độ
2. Tìm hoặc tạo POI (Point of Interest)
3. Vẽ Polygon bao quanh tòa nhà
4. Gắn tags bắt buộc:
   - `name`, `address`, `category:pl`
   - `opening_hours:pl` (format: `mo 09:00-21:00; tu-fr 09:00-22:00`)
   - `date_opened:pl` (format: `YYYY-MM-DD`)
   - `manually_reviewed_status:pl` → `success` hoặc `flagged_*`
   - `is_closed:pl`, `date_closed:pl` (nếu đóng cửa)
5. Liên kết POI ↔ Polygon (Shift + Quick Relation)
6. Dọn sạch bán kính 35m quanh POI
7. Lưu (Save mỗi 10-15 edits, dùng Jira ID làm Changeset Comment)
8. Export to Jira

### Bước 4: Update Jira
- Điền fields: Entity ID, Name, Address, Category, Visual Link, VE Link, Analytics Link
- Thêm comment: links nguồn + screenshot
- Chuyển status: `To Do` → `In Progress` → `Resolved`

---

## 4. Phân Loại Địa Điểm (Quyết Định Workflow)

### ✅ Deliverable (xử lý đầy đủ)
- Địa điểm độc lập (standalone)
- Trong Shopping Center tiêu chuẩn (có lối vào riêng từ ngoài)
- Tầng trệt của tòa nhà ≤4 tầng

### ❌ Undeliverable (chỉ gắn tag, không dọn 35m)
| Tag | Khi nào dùng |
|-----|-------------|
| `flagged_indoor_mall` | Nằm trong mall, ≥2 tường giáp không gian trong nhà |
| `flagged_multistory_building` | Có tầng trên (dù trống hay có người) |
| `flagged_dense_area` | Khu vực quá đông đúc (Manhattan, downtown Chicago...) |
| `flagged_combo_store` | 2 thương hiệu chia sẻ cùng mặt bằng không có vách ngăn |
| `flagged_cant_geofence` | Không có đủ ảnh/SV để xác định vị trí chính xác |
| `flagged_no_imagery` | Không có satellite imagery cập nhật |

### ⚠️ Extended Polygon (6 danh mục nhạy cảm)
Các danh mục sau cần vẽ thêm Extended Polygon:
- Abortion Clinic, Fertility Clinic, Addiction Treatment Center
- Alcohol/Drug Addiction Treatment Center, Vulnerable Populations Shelters

---

## 5. Phân Tích Điểm Đau (Để Xác Định Ưu Tiên Automation)

| Bước | Thời gian thủ công | Automation potential |
|------|-------------------|---------------------|
| Đọc Jira ticket | 1 phút | 99% — Jira API |
| Verify địa điểm Google Maps | 3-5 phút | 95% — Places API |
| Lấy Opening Hours | 3-8 phút | 90% — Places API + format |
| Tìm Opening/Closing Date | 5-15 phút | 60-70% — Search + LLM |
| Mở VE tại tọa độ | 1-2 phút | 95% — URL parameter |
| Tạo/tìm POI trong VE | 2-5 phút | 70-80% — UI automation |
| Vẽ Polygon | 5-15 phút | 50-70% — OSM footprint |
| Gắn tags | 3-5 phút | 80-90% — UI automation |
| Dọn 35m radius | 5-20 phút | 40-60% — phức tạp |
| Export + Jira update | 3-5 phút | 95% — API |

**Ước tính overall: 70-80% quy trình có thể tự động hóa**

---

## 6. Đánh Giá Khả Năng Automation (3 Kịch Bản)

### 🟢 Kịch bản A — VE có REST API (~60% xác suất)
- Bằng chứng: VE tích hợp "Export to Jira" → ngụ ý có API layer
- Kết quả: ~95% tự động, 140 POI/tuần chạy trong 30-60 phút
- **Rất đáng làm**

### 🟡 Kịch bản B — VE là web UI, automation được (~30% xác suất)
- Dùng Playwright để điều khiển browser
- Kết quả: ~70% tự động, 140 POI/tuần trong 2-3 giờ
- **Vẫn đáng làm**

### 🔴 Kịch bản C — VE chống bot tốt (~10% xác suất)
- Chỉ automate phần data gathering
- Kết quả: ~40% tự động, tiết kiệm 50-60% thời gian
- Thu nhập: ~$8/giờ — không lý tưởng nhưng vẫn chấp nhận được

---

## 7. Chi Phí Thực Tế

**Với quy mô 140 POI/tuần (~560/tháng):**

| Công nghệ | Giải pháp | Chi phí |
|-----------|-----------|---------|
| Google Places API | $200 free credit/tháng (đủ cho ~5,800 POI) | **$0** |
| LLM (Gemini Flash) | Free tier (1M tokens/ngày) | **$0** |
| Web search/scraping | Scrape trực tiếp | **$0** |
| OpenStreetMap API | Hoàn toàn miễn phí | **$0** |
| Playwright | Open source | **$0** |
| Hosting | Chạy local trên máy tính | **$0** |
| **TỔNG** | | **~$0/tháng** |

**→ $140/tuần doanh thu, chi phí gần 0**

---

## 8. Kết Luận

**Nên nhận job vì:**
- Không có chi phí khởi đầu
- Rủi ro thấp (chỉ tốn thời gian phân tích VE)
- Ngay cả kịch bản tệ nhất vẫn có lời
- Cơ hội học reverse-engineering hệ thống thực tế

**Điều kiện để thành công:**
- Dành 1-2 tuần đầu phân tích VE sau khi có access
- Làm thủ công 5-10 POI đầu để hiểu hệ thống
- Tôi (AI) phân tích network traffic của VE ngay khi có tài khoản
