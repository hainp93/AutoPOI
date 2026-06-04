# 🤖 AutoPOI — Automated POI Data Entry for Placer.ai Venue Editor

> **Mục tiêu**: Tự động hóa hoàn toàn quy trình Data Entry POI cho hệ thống Venue Editor của Placer.ai  
> **Doanh thu**: $0.25/POI | Mục tiêu: 140 POI/tuần = ~$140/tuần passive income  
> **Khởi tạo**: 2026-05-25

---

## 📁 Cấu Trúc Dự Án

```
AutoPOI/
├── README.md                    # File này
├── docs/
│   ├── research_notes.md        # Phân tích tài liệu training + đánh giá automation
│   ├── implementation_plan.md   # Kế hoạch kỹ thuật chi tiết
│   └── training/                # Tài liệu từ phía chủ quản (copy từ Drive)
├── src/
│   ├── pipeline/                # Core automation pipeline
│   ├── data_fetcher/            # Google Places, web scraping, LLM
│   ├── venue_editor/            # VE automation (Playwright hoặc API)
│   └── jira/                    # Jira API integration
├── config/
│   └── config.example.yaml      # Template cấu hình (keys, URLs, ...)
└── logs/                        # Runtime logs
```

---

## 🚦 Trạng Thái Hiện Tại

- [x] Phân tích tài liệu training từ Google Drive
- [x] Đánh giá khả năng tự động hóa
- [x] Lên kế hoạch kỹ thuật
- [ ] **Chờ**: Nhận tài khoản Venue Editor + Jira từ chủ quản
- [ ] Phase 0: Phân tích VE API / network traffic
- [ ] Phase 1: Build data pipeline
- [ ] Phase 2: Build VE automation
- [ ] Phase 3: End-to-end integration

---

## ⚡ Quick Start (sau khi có access)

```bash
# 1. Clone / mở project
cd e:\Tool\AutoPOI

# 2. Cài dependencies
pip install -r requirements.txt

# 3. Cấu hình
copy config\config.example.yaml config\config.yaml
# Điền API keys, URLs vào config.yaml

# 4. Chạy pipeline
python src/main.py
```

---

## 📞 Liên Hệ & Ghi Chú

- Job nhận từ: [chủ quản — chờ cập nhật tên/contact]
- Drive tài liệu training: https://drive.google.com/drive/folders/1La-4pJCzzczCaUQe0TEsuEdthzq-qAJO
- Venue Editor URL: [chờ cấp tài khoản]
- Jira URL: [chờ cấp tài khoản]
