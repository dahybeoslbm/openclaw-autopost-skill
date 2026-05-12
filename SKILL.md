---
name: auto-travel-blogger
description: >
  Tự động đăng bài du lịch từ Google Docs lên WordPress và/hoặc mạng xã hội
  (Facebook, Instagram, Threads, v.v.) thông qua Buffer. Kích hoạt khi người
  dùng yêu cầu viết bài, đăng bài, post bài về một địa điểm/chủ đề du lịch,
  hoặc muốn publish nội dung từ Google Drive lên blog/social media. Dùng skill
  này ngay cả khi người dùng chỉ nói "đăng bài về Đà Lạt" hay "post bài Hội An
  lên Facebook" — không cần yêu cầu chi tiết hơn.
---

# Auto Travel Blogger

Pipeline tự động: **Google Drive → Gemini (social captions) → WordPress / Buffer**.

> **Quan trọng:** Skill này KHÔNG cào web, KHÔNG sinh nội dung từ đầu.
> Toàn bộ nội dung bài viết phải có sẵn trong Google Drive (qua `GDRIVE_API_URL`).
> Gemini chỉ dùng để tạo caption cho mạng xã hội.

---

## Cách chạy

```bash
cd /Users/itdev/.openclaw/workspace/skills/auto-travel-blogger
docker-compose run --rm auto-travel-blogger "<câu yêu cầu tự nhiên>"
```

**Ví dụ:**
```bash
docker-compose run --rm auto-travel-blogger "Đăng bài về Hội An lên WordPress"
docker-compose run --rm auto-travel-blogger "Post bài Đà Lạt lên Facebook lúc 8h sáng mai"
docker-compose run --rm auto-travel-blogger "Đăng bài Phú Quốc lên Instagram ngày mai 9h"
```

---

## Luồng xử lý (6 bước)

```
[1] Parse NLU     → topic / platform / schedule_time
[2] List Drive    → Tìm Google Docs khớp topic (list-articles API)
    ├─ 0 kết quả  → Báo lỗi, dừng
    ├─ 1 kết quả  → Tự động chọn, tiếp tục
    └─ 2+ kết quả → In danh sách, CHỜ user chọn số thứ tự (two-turn)
[3] Fetch Drive   → Tải HTML + ảnh từ Google Doc đã chọn
[4] Gemini        → Tạo social captions (Facebook/Instagram/Threads/…)
[5] Lưu backup    → /app/output/travel_blog_<timestamp>.md
[6] Xuất bản      → WordPress (REST API) và/hoặc Buffer (GraphQL)
```

---

## Two-Turn Interaction (chọn bài)

Khi Drive trả về **2+ tài liệu**, pipeline in danh sách rồi dừng chờ:

```
Tìm thấy 3 tài liệu về 'đà lạt':
  1. Kinh nghiệm du lịch Đà Lạt tháng 12 (sửa: 2025-11-20)
  2. Review khách sạn Đà Lạt 2025 (sửa: 2025-10-15)
  3. Đà Lạt mùa hoa dã quỳ (sửa: 2025-09-01)
→ Trả lời số thứ tự để chọn bài muốn đăng.
  (Gõ 'huỷ' để bỏ qua)
```

Agent cần **hiển thị danh sách này cho user** và chạy lại lệnh với đúng số họ chọn:

```bash
docker-compose run --rm auto-travel-blogger "2"
```

Để huỷ:
```bash
docker-compose run --rm auto-travel-blogger "huỷ"
```

---

## Cú pháp câu lệnh tự nhiên

| Thành phần | Ví dụ | Mặc định |
|---|---|---|
| Topic | "Đà Lạt", "Hội An", "Phú Quốc" | Bắt buộc |
| Platform | "WordPress", "Facebook", "Instagram", "Threads" | Blog |
| Thời gian | "lúc 8h", "ngày mai 9h", "30 phút nữa", "20/5" | Ngay lập tức |

**Từ khoá platform được nhận diện:**
- `wordpress` / `wp` → WordPress
- `facebook` / `fb` → Facebook
- `instagram` / `ig` → Instagram
- `threads` → Threads
- `twitter` / `x.com` → Twitter
- `linkedin` / `youtube` / `tiktok` / `bluesky` / `pinterest` / `mastodon`
- Không đề cập → Blog (lưu file, không đăng)

---

## Cấu hình môi trường (.env)

File `.env` nằm tại `~/.openclaw/workspace/.env` (mount vào container).

### Bắt buộc

| Biến | Mô tả |
|---|---|
| `GDRIVE_API_URL` | URL của api.drive.article (VD: `http://host.docker.internal:8080`) |
| `GEMINI_API_KEY` | Google Gemini API key |

### WordPress (nếu đăng WP)

| Biến | Mô tả |
|---|---|
| `WP_SITE_URL` | URL trang WordPress (VD: `https://myblog.com`) |
| `WP_USERNAME` | Tên đăng nhập WP |
| `WP_APP_PASSWORD` | Application Password của WP |

### Buffer (nếu đăng social)

| Biến | Mô tả |
|---|---|
| `BUFFER_API_KEY` | Buffer API key (fallback) |
| `BUFFER_FACEBOOK_CHANNELS` | JSON array channels Facebook |
| `BUFFER_INSTAGRAM_CHANNELS` | JSON array channels Instagram |
| `BUFFER_THREADS_CHANNELS` | JSON array channels Threads |

**Cấu trúc channel JSON:**
```json
[{"id": "channel_id_here", "name": "Tên page", "service": "facebook", "apiKey": "key_của_account"}]
```

### Tùy chọn

| Biến | Mặc định | Mô tả |
|---|---|---|
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model Gemini |
| `GDRIVE_LANGUAGE` | `vi` | Ngôn ngữ tài liệu |
| `CHAT_ID` | `""` | ID phiên để lưu selection cache (TTL 24h) |
| `OUTPUT_DIR` | `/app/output` | Thư mục lưu file backup |
| `OLLAMA_API_KEY` | `""` | Fallback khi Gemini lỗi 429/503 |

---

## Output

### File backup
Mỗi lần chạy tạo file: `/app/output/travel_blog_<timestamp>.md`  
(mount ra `./output/` trên host)

### WordPress thành công
```
✅ WordPress: https://myblog.com/?p=123
```

### Buffer thành công
```
✅ [FACEBOOK] Tên Page
✅ [INSTAGRAM] Tên Profile
```

### Lỗi thường gặp

| Lỗi | Nguyên nhân | Xử lý |
|---|---|---|
| `GDRIVE_API_URL chưa cấu hình` | Thiếu biến env | Thêm vào `.env` |
| `Không tìm thấy tài liệu` | Drive không có doc khớp topic | Kiểm tra tên doc trên Drive |
| `PENDING_SELECTION` | 2+ tài liệu, chờ chọn | Hiển thị list, hỏi user |
| `Không tìm thấy channel nào` | Thiếu `BUFFER_*_CHANNELS` | Thêm channel JSON vào `.env` |

---

## Cấu trúc project

```
auto-travel-blogger/
├── docker-compose.yml
├── Dockerfile
├── SKILL.md
├── GEMINI.md          ← Hướng dẫn cho OpenClaw agent
├── .env.example
├── output/            ← File .md backup (mount từ container)
└── scripts/
    ├── blogger.py     ← Entry point / orchestrator
    ├── config.py      ← Load env config
    ├── services/
    │   ├── gemini.py        ← Gemini API + Ollama fallback
    │   ├── googledrive.py   ← Google Drive API client
    │   ├── wordpress.py     ← WordPress REST API
    │   └── buffer/          ← Buffer GraphQL (Facebook/Instagram/Threads)
    └── utils/
        ├── parser.py        ← NLU: topic/platform/schedule
        ├── models.py        ← Dataclasses
        ├── media.py         ← Drive URL resolver
        ├── logger.py
        └── selection_cache.py  ← SQLite cache cho two-turn flow
```