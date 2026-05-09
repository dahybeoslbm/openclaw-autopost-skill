"""
services/gemini.py — Tất cả tương tác với Gemini API.
"""
from time import sleep

import requests
import json
from config import GeminiConfig, OllamaConfig
from utils.logger import get_logger

logger = get_logger(__name__)

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


    def build_social_captions_prompt(self, topic: str, title: str, plain_text: str) -> str:
        """Prompt nhẹ — CHỈ sinh social_captions từ plain text. Không format HTML."""
        excerpt = plain_text[:8000]
        return f"""
Bạn là chuyên gia social media. Đọc nội dung bài viết dưới đây và viết caption phù hợp cho từng nền tảng.

TIÊU ĐỀ: {title}
CHỦ ĐỀ: {topic}

NỘI DUNG:
{excerpt}

QUY TẮC: Chỉ tóm tắt, trích ý từ nội dung gốc — KHÔNG bịa thêm thông tin.

Trả về JSON hợp lệ, không thêm text ngoài JSON:
{{
  "facebook":        "40-80 ký tự. Câu hấp dẫn, 2-3 emoji, 2 hashtag.",
  "instagram":       "Dòng đầu hook dưới 125 ký tự. Xuống dòng. 2-3 câu storytelling. Xuống dòng. 3-5 hashtag. Kết: 📍 Link in bio",
  "twitter":         "Tối đa 80 ký tự. Punchy, 1 hashtag.",
  "threads":         "Tối đa 500 ký tự. Dòng đầu tiên là tiêu đề/hook ngắn gọn phản ánh chủ đề bài. Tiếp theo liệt kê TẤT CẢ ý chính, mỗi ý 1 dòng (emoji + nội dung cốt lõi). Nếu còn dư ký tự thì thêm chi tiết phụ. Kết bằng 1 câu CTA. KHÔNG hashtag. KHÔNG in nhãn hay tiêu đề phần.",
  "tiktok":          "Hook câu hỏi hoặc fact bất ngờ. 150-250 ký tự. 3-5 hashtag trending.",
  "linkedin":        "Mở bằng insight. 700-1000 ký tự, chia đoạn ngắn. 3-4 hashtag. Kết bằng câu hỏi.",
  "pinterest":       "150-250 ký tự inspirational, bắt đầu bằng động từ. Không hashtag.",
  "bluesky":         "200-270 ký tự. Direct, witty, không hashtag.",
  "mastodon":        "300-400 ký tự. 3-4 hashtag du lịch.",
  "google_business": "300-500 ký tự giới thiệu địa điểm. CTA rõ ràng."
}}
""".strip()

    def generate_social_captions(self, topic: str, title: str, plain_text: str) -> dict:
        """Sinh social_captions từ plain text. Trả về dict (rỗng nếu thất bại)."""
        prompt = self.build_social_captions_prompt(topic, title, plain_text)
        raw = self.generate(prompt)
        if not raw or "Lỗi" in raw:
            return {}

        try:
            cleaned = raw.strip()
            if "```" in cleaned:
                for part in cleaned.split("```"):
                    part = part.lstrip("json").strip()
                    if not part:
                        continue
                    try:
                        return json.loads(part)
                    except json.JSONDecodeError:
                        continue

            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass

            start = cleaned.find("{")
            end   = cleaned.rfind("}") + 1
            if start != -1 and end > start:
                try:
                    return json.loads(cleaned[start:end])
                except json.JSONDecodeError:
                    pass

        except Exception as e:
            logger.warning("  → parse social_captions thất bại: %s", e)

        return {}