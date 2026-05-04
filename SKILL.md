---
name: auto-travel-blogger
description: Tự động viết bài review du lịch, đăng bài, vẽ ảnh theo yêu cầu qua lệnh tự nhiên. Dùng khi người dùng yêu cầu viết bài travel blog, cào dữ liệu URL, vẽ ảnh tham khảo, hoặc hỏi về review du lịch.
---

# Auto Travel Blogger

Kỹ năng này tự động hóa toàn bộ quy trình tạo nội dung du lịch, cào bài, và vẽ ảnh từ **Nano Banana 2 Skill**.

## Dành cho AI Agent (Gemini CLI)
Khi người dùng yêu cầu viết một bài travel blog thông qua chat (ví dụ như qua Telegram: "Viết bài review về Đà Lạt..."), **bạn (AI Agent) HÃY THỰC HIỆN CÁC BƯỚC SAU BẰNG CÔNG CỤ `run_shell_command`**:

1. Di chuyển vào thư mục `/Users/itdev/auto-travel-blogger`.
2. Chạy lệnh Docker Compose với nguyên văn câu prompt của người dùng:
   ```bash
   cd /Users/itdev/auto-travel-blogger && docker-compose run --rm auto-travel-blogger "<NGUYÊN VĂN CÂU YÊU CẦU CỦA NGƯỜI DÙNG>"
   ```
   *(Ví dụ: `docker-compose run --rm auto-travel-blogger "Viết bài review về Vịnh Hạ Long, đăng lúc 9h sáng mai lên trang WordPress"`)*
3. Đợi lệnh chạy xong. Kết quả bài viết sẽ được xuất ra một file `.md` trong thư mục `/Users/itdev/auto-travel-blogger/output/`.
4. Bạn có thể đọc nội dung file Markdown vừa tạo ra trong thư mục `output` và tóm tắt kết quả lại cho người dùng vào trong đoạn chat Telegram, báo cho họ biết là bài đã được lưu/đẩy thành công kèm theo đường dẫn file.

## Dành cho Người Dùng Cuối (Cài đặt thủ công)
- Đã cài đặt Docker và Docker Compose.
- **Quan trọng:** Cần cài đặt kỹ năng vẽ ảnh trên máy tính thông qua Clawhub:
  ```bash
  clawhub install xixihhhh/nano-banana-2-skill
  clawhub config set ATLAS_CLOUD_API_KEY [YourKey]
  ```
- Thư mục làm việc: `/Users/itdev/auto-travel-blogger/`.
