"""
services/gemini.py — Tất cả tương tác với Gemini API.
"""
import requests
import json
from config import GeminiConfig
from utils.logger import get_logger

logger = get_logger(__name__)

_FALLBACK_ARTICLE = """### Review (Bài viết mẫu — thiếu GEMINI_API_KEY)

Đây là bài viết mẫu được tạo tự động khi chưa cài đặt API Key."""


class GeminiService:
    def __init__(self, config: GeminiConfig):
        self._config = config

    def generate(self, prompt: str) -> str:
        """Gọi Gemini API, trả về text. Fallback về mẫu nếu lỗi."""
        if not self._config.is_valid:
            logger.warning("  → GEMINI_API_KEY chưa được cài đặt. Trả về bài mẫu.")
            return _FALLBACK_ARTICLE

        payload = {"contents": [{"parts": [{"text": prompt}]}]}

        try:
            resp = requests.post(
                url=f"{self._config.api_url}?key={self._config.api_key}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self._config.timeout,
            )
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

        except requests.Timeout:
            logger.error("  → Gemini API timeout sau %ds", self._config.timeout)
        except requests.HTTPError as e:
            logger.error("  → Gemini HTTP error: %s", e)
        except (KeyError, IndexError) as e:
            logger.error("  → Gemini response parse error: %s", e)
        except Exception as e:
            logger.error("  → Gemini unknown error: %s", e)

        return "Lỗi tạo nội dung."

    def generate_article(self, prompt: str) -> dict:
        raw = self.generate(prompt)
        if not raw:
            return {"seo_title": "", "meta_description": "",
                "focus_keyword": "", "excerpt": "", "content": ""}
        try:
            cleaned = raw.strip()
            if "```" in cleaned:
                for part in cleaned.split("```"):
                    part = part.lstrip("json").strip()
                    try:
                        return json.loads(part)
                    except json.JSONDecodeError:
                        continue
            return json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            return {"seo_title": "", "meta_description": "",
                "focus_keyword": "", "excerpt": "", "content": raw}

    def build_article_prompt(
        self,
        topic: str,
        platform: str,
        source_url: str,
        text_content: str,
        images_markdown: str,
    ) -> str:
        image_instruction = (
            f"Chèn các ảnh sau vào bài viết ở vị trí phù hợp:\n{images_markdown}"
            if images_markdown
            else "KHÔNG tự ý chèn ảnh vào bài viết vì hiện không có ảnh."
        )
        return f"""
        Bạn là Travel Blogger SEO chuyên nghiệp.
        Viết bài review du lịch về: "{topic}". Nền tảng: {platform}.

        YÊU CẦU HTML (bắt buộc tuân theo):
        - KHÔNG viết thẻ <html>, <head>, <body> — chỉ viết phần content bên trong
        - KHÔNG dùng <h1> (WordPress tự tạo từ seo_title)
        - Dùng <h2> cho section chính, <h3> cho mục con
        - Dùng <p> cho đoạn văn, KHÔNG để text trần không có thẻ
        - Dùng <strong> cho từ khoá quan trọng
        - Dùng <blockquote> cho tips/lưu ý nổi bật
        - Dùng <ul><li> hoặc <ol><li> cho danh sách
        - {image_instruction}

        CẤU TRÚC BÀI (theo đúng thứ tự):
        <p>Đoạn mở đầu hấp dẫn, có từ khoá chính...</p>
        <h2>Tổng quan về {topic}</h2>
        <h2>Những điểm đến không thể bỏ qua</h2>
        <h2>Ẩm thực & Đặc sản địa phương</h2>
        <h2>Kinh nghiệm di chuyển & Lưu trú</h2>
        <h2>Lịch trình gợi ý</h2>
        <h2>Kết luận</h2>

        Nguồn tham khảo: {source_url}
        Nội dung tham khảo: {text_content[:2000]}

        Trả về JSON hợp lệ, không thêm text ngoài JSON:
        {{
        "seo_title": "50-60 ký tự, có từ khoá",
        "meta_description": "150-160 ký tự, có call-to-action",
        "focus_keyword": "từ khoá chính VD: du lịch {topic}",
        "excerpt": "2-3 câu tóm tắt hiển thị ngoài trang chủ",
        "content_html": "<p>Toàn bộ nội dung HTML ở đây</p>"
        }}
        """.strip()

    def build_image_prompts(self, alts: list[str], topic: str) -> str:
        lines = "\n".join(f"- {alt}" for alt in alts[:3])
        return f"""
Viết lại mỗi mô tả (alt text) về {topic} thành MỘT prompt tiếng Anh ngắn gọn để vẽ ảnh AI.
Mỗi prompt một dòng. Thêm: photorealistic, 4k, travel photography.

{lines}
""".strip()
