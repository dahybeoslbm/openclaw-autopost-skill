"""
services/gemini.py — Tất cả tương tác với Gemini API.
"""
import requests
from scripts.config import GeminiConfig
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
Hãy đóng vai một Travel Blogger chuyên nghiệp.
Viết một bài review du lịch về chủ đề: "{topic}".
Nền tảng xuất bản: {platform}.
Trích dẫn nguồn: "{source_url}".
Định dạng: Markdown.
{image_instruction}

Nội dung tham khảo:
{text_content[:2000]}
""".strip()

    def build_image_prompts(self, alts: list[str], topic: str) -> str:
        lines = "\n".join(f"- {alt}" for alt in alts[:3])
        return f"""
Viết lại mỗi mô tả (alt text) về {topic} thành MỘT prompt tiếng Anh ngắn gọn để vẽ ảnh AI.
Mỗi prompt một dòng. Thêm: photorealistic, 4k, travel photography.

{lines}
""".strip()
