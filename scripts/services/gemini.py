"""
services/gemini.py — Tất cả tương tác với Gemini API.
"""
from time import sleep

import requests
import json
from config import GeminiConfig, OllamaConfig
from utils.logger import get_logger

logger = get_logger(__name__)

_FALLBACK_ARTICLE = """### Review (Bài viết mẫu — thiếu GEMINI_API_KEY)
Đây là bài viết mẫu được tạo tự động khi chưa cài đặt API Key."""

# Các status code nên chuyển sang Ollama thay vì retry tiếp
_OLLAMA_FALLBACK_STATUSES = {429, 503}
class GeminiService:
    def __init__(self, config: GeminiConfig, ollama_config: OllamaConfig | None = None):
        self._config = config
        self._ollama = ollama_config
        
 # ── Ollama fallback ──────────────────────────────────────────────────────
    def _generate_via_ollama(self, prompt: str) -> str:
        """Gọi Ollama Cloud API, raise nếu thất bại."""
        if not self._ollama or not self._ollama.is_valid:
            raise RuntimeError("Ollama chưa được cấu hình.")

        system_msg = (
            "You are a JSON generator. "
            "Return ONLY valid JSON with no markdown fences, "
            "no explanations, no extra text. "
            "All HTML must be inside JSON string values with properly escaped quotes."
        )

        payload = {
            "model": self._ollama.model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt}
                ],
            "stream": False,
            "options": {                                    
                "temperature": 0.1,
                "top_p": 0.9,
            },
        }
        resp = requests.post(
            url=self._ollama.api_url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._ollama.api_key}",
            },
            timeout=self._ollama.timeout,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]
    
    
    def generate(self, prompt: str, max_retries: int = 3) -> str:
        if not self._config.is_valid:
            logger.warning("  → GEMINI_API_KEY chưa được cài đặt.")
            return _FALLBACK_ARTICLE

        payload = {"contents": [{"parts": [{"text": prompt}]}]}

        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(
                    url=f"{self._config.api_url}?key={self._config.api_key}",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=self._config.timeout,
                )
                resp.raise_for_status()
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

            except requests.HTTPError as e:
                status = e.response.status_code if e.response else 0

                if status in _OLLAMA_FALLBACK_STATUSES:
                    logger.warning("  → Gemini %d, chuyển sang Ollama fallback...", status)
                    break  # ← thoát loop, xuống xử lý Ollama bên dưới

                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning("  → Gemini HTTP %d, thử lại %d/%d sau %ds...",
                                status, attempt, max_retries, wait)
                    sleep(wait)
                else:
                    logger.error("  → Gemini HTTP error: %s", e)
                    break

            except requests.Timeout:
                if attempt < max_retries:
                    logger.warning("  → Gemini timeout, thử lại %d/%d...", attempt, max_retries)
                    sleep(2 ** attempt)
                else:
                    logger.error("  → Gemini timeout sau %d lần thử", max_retries)
                    break

            except (KeyError, IndexError) as e:
                logger.error("  → Gemini parse error: %s", e)
                break
            except Exception as e:
                logger.error("  → Gemini unknown error: %s", e)
                break

        # ── Ollama fallback — luôn thử sau khi Gemini thất bại ──────────────
        logger.warning("  → Thử Ollama fallback...")
        try:
            return self._generate_via_ollama(prompt)
        except Exception as e:
            logger.error("  → Ollama fallback thất bại: %s", e)

        return "Lỗi tạo nội dung."

    def generate_article(self, prompt: str) -> dict:
            raw = self.generate(prompt)
            
            _EMPTY = {"seo_title": "", "meta_description": "",
                    "focus_keyword": "", "excerpt": "", "content_html": ""}
            if not raw:
                return _EMPTY
            
            result = None
            
            try:
                cleaned = raw.strip()

                # Bóc fence ```json ... ``` hoặc ``` ... ```
                if "```" in cleaned:
                    for part in cleaned.split("```"):
                        part = part.lstrip("json").strip()
                        if not part:
                            continue
                        try:
                            return json.loads(part)
                            break
                        except json.JSONDecodeError:
                            continue

                if result is None:
                    try:
                        result = json.loads(cleaned)
                    except json.JSONDecodeError:
                        pass
                    
                if result is None:
                    start = cleaned.find("{")
                    end   = cleaned.rfind("}") + 1
                    if start != -1 and end > start:
                        try:
                            result = json.loads(cleaned[start:end])
                        except json.JSONDecodeError:
                            pass
                        
            except (ValueError, TypeError):
                pass

            # Fallback: trả content thô
            if result is None:
                result = {**_EMPTY, "content_html": f"<p>{raw}</p>"}
                
            # Chuẩn hoá key: một số model trả "content" thay vì "content_html"
            if "content_html" not in result and "content" in result:
                result["content_html"] = result.pop("content")
                
            # Post-process: fix lỗi Ollama re-escape
            html = result.get("content_html", "")
            if html:
                html = html.replace('\\"', '"')
                html = html.replace('\\n', '\n')
                result["content_html"] = html

            return result


    def build_article_prompt(
        self,
        topic: str,
        platform: str,
        source_url: str,
        text_content: str,
        images_markdown: str,
        *,
        source_is_html: bool = False,
        doc_title: str = "",
        doc_keywords: list[str] | None = None,
    ) -> str:
        if source_is_html:
            # Ảnh đã có sẵn trong HTML gốc từ Google Docs — giữ nguyên, không xóa
            image_instruction = (
                "Giữ NGUYÊN tất cả thẻ <img> trong nội dung gốc, "
                "bao gồm src, style, alt. KHÔNG xóa, KHÔNG thay đổi bất kỳ thẻ <img> nào."
            )
        elif images_markdown:
            image_instruction = f"Chèn các ảnh sau vào bài viết ở vị trí phù hợp:\n{images_markdown}"
        else:
            image_instruction = "KHÔNG tự ý chèn ảnh vào bài viết vì hiện không có ảnh."
        
        keyword_hint = ""
        if doc_keywords:
            keyword_hint = f"\nCác từ khoá nổi bật: {', '.join(doc_keywords[:10])}"
        
        source_section = f"""NGUỒN NỘI DUNG: Google Docs{f' — "{doc_title}"' if doc_title else ''}
URL: {source_url}{keyword_hint}

--- NỘI DUNG GOOGLE DOC (HTML) ---
{text_content[:3000]}
--- KẾT THÚC ---""" if source_is_html else f"""NGUỒN THAM KHẢO: {source_url}
{text_content[:2000]}"""


        # ── Khi nguồn là Google Docs: CHỈ chuyển format, không viết thêm ────
        if source_is_html:
            return f"""
Bạn là chuyên gia định dạng nội dung SEO. Nhiệm vụ: chuyển đổi nội dung Google Docs sang HTML chuẩn SEO.

{source_section}

QUY TẮC BẮT BUỘC:
- GIỮ NGUYÊN 100% nội dung gốc — KHÔNG thêm, KHÔNG bớt, KHÔNG viết lại bất kỳ thông tin nào
- KHÔNG sáng tác, KHÔNG suy luận thêm — chỉ chuyển đổi định dạng
- Nội dung do con người viết, AI chỉ có nhiệm vụ format lại

YÊU CẦU HTML:
- KHÔNG viết thẻ <html>, <head>, <body>
- KHÔNG dùng <h1> (WordPress tự tạo từ seo_title)
- Dùng <h2> / <h3> theo đúng cấu trúc heading gốc trong doc
- Dùng <p> cho đoạn văn
- Dùng <strong> cho text in đậm trong doc gốc
- Dùng <blockquote> cho phần được highlight/callout trong doc
- Dùng <ul><li> / <ol><li> cho danh sách gốc
- {image_instruction}

Trả về JSON hợp lệ, không thêm text ngoài JSON:
{{
  "seo_title": "Lấy từ tiêu đề doc hoặc heading đầu tiên, 50-60 ký tự",
  "meta_description": "Tóm tắt từ đoạn mở đầu của doc, 150-160 ký tự, có call-to-action",
  "focus_keyword": "Từ khoá chính rút ra từ nội dung doc",
  "excerpt": "2-3 câu đầu của bài viết gốc",
  "content_html": "<p>Toàn bộ nội dung HTML đã format ở đây — giữ nguyên nội dung gốc</p>",
  "social_captions": {{
    "facebook":  "Trích dẫn câu hấp dẫn nhất từ bài, 40-80 ký tự, 2-3 emoji, 2 hashtag.",
    "instagram": "Dòng đầu hook từ nội dung gốc (dưới 125 ký tự). Xuống dòng. 2-3 câu từ bài viết. Xuống dòng. 3-5 hashtag. Kết: 📍 Link in bio",
    "twitter":   "Tối đa 80 ký tự, trích ý nổi bật nhất, 1 hashtag.",
    "threads":   "Tối đa 450 ký tự. 3-4 điểm nổi bật từ bài, mỗi điểm 1 dòng với emoji bullet. CTA ngắn. KHÔNG hashtag.",
    "tiktok":    "Hook từ fact/điểm thú vị trong bài. 150-250 ký tự. 3-5 hashtag trending.",
    "linkedin":  "Mở bằng insight từ bài viết. 700-1000 ký tự. 3-4 hashtag. Kết bằng câu hỏi.",
    "pinterest": "150-250 ký tự inspirational từ nội dung, bắt đầu bằng động từ. Không hashtag.",
    "bluesky":   "200-270 ký tự. Ý hay nhất từ bài, witty, không hashtag.",
    "mastodon":  "300-400 ký tự từ nội dung gốc. 3-4 hashtag.",
    "google_business": "300-500 ký tự giới thiệu địa điểm từ bài viết. CTA rõ ràng."
  }}
}}
""".strip()
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
        "content_html": "<p>Toàn bộ nội dung HTML ở đây</p>",
        "social_captions": {{
            "facebook":  "40-80 ký tự. Câu hấp dẫn về {topic}, kèm 2-3 emoji, 2 hashtag. Không cần link (sẽ thêm sau).",
            "instagram": "Dòng đầu hook mạnh (dưới 125 ký tự, người đọc thấy ngay không cần bấm more). Xuống dòng. 2-3 câu storytelling ngắn. Xuống dòng. 3-5 hashtag liên quan. Kết bằng: 📍 Link in bio",
            "twitter":   "Tối đa 80 ký tự plain text (link sẽ thêm riêng +23 ký tự). Punchy, có 1 hashtag.",
            "threads": "Tối đa 450 ký tự. Tóm tắt 3-4 điểm nổi bật NHẤT về {topic} (điểm đến, ẩm thực, trải nghiệm). Mỗi điểm 1 dòng, dùng emoji bullet (✨🍜🏔️). Kết bằng 1 câu CTA ngắn. KHÔNG kể chuyện dài, KHÔNG dùng hashtag.",
            "tiktok":    "Dòng đầu hook câu hỏi hoặc fact bất ngờ về {topic}. 150-250 ký tự tổng. 3-5 hashtag trending du lịch Việt Nam. Tone trẻ trung casual.",
            "linkedin":  "Mở đầu bằng insight hoặc câu hỏi chuyên sâu về du lịch {topic}. 700-1000 ký tự. Chia đoạn ngắn. Personal story angle. 3-4 hashtag chuyên nghiệp. Kết bằng câu hỏi mời thảo luận.",
            "pinterest": "150-250 ký tự mô tả hình ảnh và trải nghiệm tại {topic}. Ngôn ngữ inspirational, giàu keyword tự nhiên (không dùng hashtag). Bắt đầu bằng động từ hành động.",
            "bluesky":   "200-270 ký tự (link card sẽ thêm riêng). Direct, witty, không hashtag. Tone authentic.",
            "mastodon":  "300-400 ký tự. Mô tả {topic} chi tiết. 3-4 hashtag du lịch Việt Nam để discovery.",
            "google_business": "300-500 ký tự. Giới thiệu {topic} như local business recommendation. Có CTA rõ ràng. Dùng từ khoá địa điểm tự nhiên."
        }}
        }}
        """.strip()

    def build_image_prompts(self, alts: list[str], topic: str) -> str:
        lines = "\n".join(f"- {alt}" for alt in alts[:3])
        return f"""
Viết lại mỗi mô tả (alt text) về {topic} thành MỘT prompt tiếng Anh ngắn gọn để vẽ ảnh AI.
Mỗi prompt một dòng. Thêm: photorealistic, 4k, travel photography.

{lines}
""".strip()
