"""
services/gemini.py — Tất cả tương tác với Gemini API.
"""
import json
import re as _re
import random
from time import sleep

import requests

from config import GeminiConfig, OllamaConfig
from utils.logger import get_logger

logger = get_logger(__name__)

# Các status code nên chuyển sang Ollama thay vì retry tiếp
_OLLAMA_FALLBACK_STATUSES = {429, 503, 0}


class GeminiService:
    def __init__(self, config: GeminiConfig, ollama_config: OllamaConfig | None = None):
        self._config = config
        self._ollama = ollama_config

    # ── Ollama fallback ───────────────────────────────────────────────────────

    def _generate_via_ollama(self, prompt: str) -> str:
        """Gọi Ollama Cloud API, raise nếu thất bại."""
        if not self._ollama or not self._ollama.is_valid:
            raise RuntimeError("Ollama chưa được cấu hình.")

        payload = {
            "model": self._ollama.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a JSON generator. "
                        "Return ONLY valid JSON with no markdown fences, "
                        "no explanations, no extra text. "
                        "All HTML must be inside JSON string values with properly escaped quotes."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {
                "temperature": 0.85,
                "top_p": 0.9,
                "seed": random.randint(1, 99999),
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

    # ── Core generate ─────────────────────────────────────────────────────────

    def generate(self, prompt: str, max_retries: int = 3) -> str:
        if not self._config.is_valid:
            logger.warning("  → GEMINI_API_KEY chưa được cài đặt.")
            return ""

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "thinkingConfig": {"thinkingBudget": 0}
            },
        }

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
                    break
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

        # Ollama fallback
        logger.warning("  → Thử Ollama fallback...")
        try:
            return self._generate_via_ollama(prompt)
        except Exception as e:
            logger.error("  → Ollama fallback thất bại: %s", e)

        return "Lỗi tạo nội dung."

    # ── JSON parse helper ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """
        Parse JSON từ Gemini output — xử lý markdown fences và garbage prefix.
        Raise ValueError nếu không parse được.
        """
        cleaned = raw.strip()

        # Strip ```json ... ``` fences
        if "```" in cleaned:
            for part in cleaned.split("```"):
                part = part.lstrip("json").strip()
                if part.startswith("{"):
                    cleaned = part
                    break

        # Tìm JSON object đầu tiên
        start = cleaned.find("{")
        end   = cleaned.rfind("}") + 1
        if start != -1 and end > start:
            cleaned = cleaned[start:end]

        return json.loads(cleaned)

    # ── Normalize facebook captions ───────────────────────────────────────────

    def _normalize_result(self, result: dict, facebook_pages: list[dict] | None) -> dict:
        """Đảm bảo facebook value là list có đủ phần tử nếu có nhiều pages."""
        if not facebook_pages or len(facebook_pages) <= 1 or "facebook" not in result:
            return result

        fb = result["facebook"]
        if isinstance(fb, str):
            result["facebook"] = [fb] * len(facebook_pages)
        elif isinstance(fb, list):
            while len(fb) < len(facebook_pages):
                fb.append(fb[-1] if fb else "")
            result["facebook"] = fb[:len(facebook_pages)]
        return result

    # ── Social captions prompt builder ───────────────────────────────────────

    def build_social_captions_prompt(
        self,
        topic: str,
        title: str,
        plain_text: str,
        platforms: list[str] | None = None,
        facebook_pages: list[dict] | None = None,
    ) -> str:
        """Prompt CHỈ sinh social_captions từ plain text. Không format HTML."""
        excerpt = plain_text[:8000]

        all_specs = {
            "facebook": (
                f"JSON array gồm ĐÚNG {len(facebook_pages)} string, "
                f"mỗi string là 1 caption 40-80 ký tự + 2-3 emoji + 2 hashtag, góc độ khác nhau.\n"
                f"VÍ DỤ nếu có 2 pages: [\"caption A 🌟 #tag1\", \"caption B ✈️ #tag2\"]\n"
                f"Pages:\n"
                + "\n".join(f"  {i+1}. {p.get('name','')}" for i, p in enumerate(facebook_pages))
                if facebook_pages and len(facebook_pages) > 1
                else "40-80 ký tự. Câu hấp dẫn, 2-3 emoji, 2 hashtag."
            ),
            "instagram":       "Dòng đầu hook dưới 125 ký tự. Xuống dòng. 2-3 câu storytelling. Xuống dòng. 3-5 hashtag. Kết: 📍 Link in bio",
            "twitter":         "Tối đa 80 ký tự. Punchy, 1 hashtag.",
            "threads":         "Tối đa 500 ký tự. Dòng đầu tiên là tiêu đề/hook ngắn gọn phản ánh chủ đề bài. Tiếp theo liệt kê TẤT CẢ ý chính, mỗi ý 1 dòng (emoji + nội dung cốt lõi). Nếu còn dư ký tự thì thêm chi tiết phụ. Kết bằng 1 câu CTA. KHÔNG hashtag. KHÔNG in nhãn hay tiêu đề phần.",
            "tiktok":          "Hook câu hỏi hoặc fact bất ngờ. 150-250 ký tự. 3-5 hashtag trending.",
            "linkedin":        "Mở bằng insight. 700-1000 ký tự, chia đoạn ngắn. 3-4 hashtag. Kết bằng câu hỏi.",
            "pinterest":       "150-250 ký tự inspirational, bắt đầu bằng động từ. Không hashtag.",
            "bluesky":         "200-270 ký tự. Direct, witty, không hashtag.",
            "mastodon":        "300-400 ký tự. 3-4 hashtag du lịch.",
            "google_business": "300-500 ký tự giới thiệu địa điểm. CTA rõ ràng.",
        }

        if platforms:
            target = {k: v for k, v in all_specs.items() if k in platforms}
        else:
            target = all_specs

        if not target:
            target = all_specs

        specs_json = json.dumps(target, ensure_ascii=False, indent=2)

        return f"""
Bạn là chuyên gia social media. Đọc nội dung bài viết dưới đây và viết caption phù hợp cho từng nền tảng.

TIÊU ĐỀ: {title}
CHỦ ĐỀ: {topic}

NỘI DUNG:
{excerpt}

QUY TẮC BẮT BUỘC:
- Chỉ tóm tắt, trích ý từ nội dung gốc — KHÔNG bịa thêm thông tin, địa điểm, giá cả, hay chi tiết không có trong bài.
- Nếu bài không đề cập giá → không viết giá. Nếu không đề cập địa chỉ cụ thể → không bịa địa chỉ.
- Giữ đúng tông của bài gốc (review thực tế, không thổi phồng)

Trả về JSON hợp lệ, không thêm text hay markdown ngoài JSON.
Chỉ trả về các key sau, không thêm key khác:
{specs_json}""".strip()

    # ── Social captions (single request) ─────────────────────────────────────

    def generate_social_captions(
        self,
        topic: str,
        title: str,
        plain_text: str,
        platforms: list[str] | None = None,
        facebook_pages: list[dict] | None = None,
    ) -> dict:
        """Sinh social_captions từ plain text. Trả về dict (rỗng nếu thất bại)."""
        prompt = self.build_social_captions_prompt(topic, title, plain_text, platforms, facebook_pages)
        raw = self.generate(prompt)
        if not raw or "Lỗi" in raw:
            return {}

        try:
            return self._normalize_result(self._parse_json(raw), facebook_pages)
        except Exception as e:
            logger.warning("  → parse social_captions thất bại: %s", e)
            return {}

    # ── Batch: rewrite N bản + captions trong 1 request ──────────────────────

    def process_content_batch(
        self,
        original_html: str,
        topic: str,
        title: str,
        rewrite_count: int,
        platforms: list[str] | None = None,
        facebook_pages: list[dict] | None = None,
        need_summary: bool = True,
        need_zalo_cta: bool = True,
    ) -> tuple[list[tuple[str, str]], dict, str, str]:
        """
        Gộp rewrite N bản unique + social captions + SEO summary vào 1 Gemini request.

        Args:
            original_html:  HTML gốc từ Google Drive
            topic:          Chủ đề bài viết
            title:          Tiêu đề gốc
            rewrite_count:  Số bản cần viết lại (0 = chỉ gen captions/summary)
            platforms:      Danh sách platform cần gen caption (None = bỏ qua captions)
            facebook_pages: Danh sách FB pages để gen caption riêng từng page
            need_summary:   Có sinh đoạn tóm tắt 260-280 ký tự hay không
            need_zalo_cta:  Có sinh Call to Action cho Zalo OA không

        Returns:
            rewrites: [(html, title), ...] — rewrite_count phần tử (rỗng nếu rewrite_count=0)
            captions: dict social captions
            summary:  chuỗi tóm tắt
            zalo_cta: chuỗi call to action (dưới 50 ký tự)
        """
        plain = _re.sub(r"<[^>]+>", " ", original_html).strip()

        fallback_rewrites = [(original_html, title)] * rewrite_count
        fallback_captions: dict = {}
        fallback_summary = ""
        fallback_zalo_cta = ""

        # ── Build phần rewrite ────────────────────────────────────────────────
        rewrite_section = ""
        if rewrite_count > 0:
            rewrite_section = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NHIỆM VỤ 1 — VIẾT LẠI {rewrite_count} BẢN UNIQUE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NỘI DUNG GỐC:
{plain[:8000]}

Yêu cầu mỗi bản:
- Giữ nguyên cấu trúc thẻ HTML h1/h2/p/ul, copy y hệt thẻ <img>
- Viết lại ít nhất 70% từ ngữ, KHÔNG copy nguyên câu từ bản gốc
- Giữ nguyên thông tin thực tế, tên địa điểm, số liệu
- Mỗi bản có góc nhìn và cách mở đầu khác nhau
- Mỗi bản có tiêu đề <h1> mới khác bản gốc và khác nhau giữa các bản
"""

        # ── Build phần caption ────────────────────────────────────────────────
        caption_section = ""
        if platforms:
            caption_prompt = self.build_social_captions_prompt(
                topic=topic,
                title=title,
                plain_text=plain,
                platforms=platforms,
                facebook_pages=facebook_pages,
            )
            caption_section = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NHIỆM VỤ 2 — SOCIAL CAPTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{caption_prompt}
"""

        # ── Build phần summary ────────────────────────────────────────────────
        summary_section = ""
        if need_summary:
            summary_section = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NHIỆM VỤ 3 — SEO SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Viết 1 đoạn tóm tắt SEO dài từ 260-280 ký tự, mượt mà, bao quát toàn bộ nội dung gốc để làm subtitle. LƯU Ý: Đây là bài viết của đại lý vé máy bay "Tìm Chuyến Bay", hãy khéo léo lồng ghép tên thương hiệu "Tìm Chuyến Bay" vào tóm tắt thay vì dùng các từ chung chung như "đại lý uy tín". KHÔNG dùng markdown.
"""

        # ── Build phần CTA ────────────────────────────────────────────────────
        cta_section = ""
        if need_zalo_cta:
            cta_section = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NHIỆM VỤ 4 — CALL TO ACTION (ZALO)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Viết 1 câu Call To Action (Kêu gọi hành động) siêu ngắn gọn, hấp dẫn, liên quan trực tiếp đến bài viết. Yêu cầu: BẮT BUỘC dưới 50 ký tự, mang tính thúc giục người dùng bấm vào link để săn vé/đặt vé. KHÔNG dùng markdown.
"""

        # ── Build JSON schema ─────────────────────────────────────────────────
        rewrites_schema = ""
        if rewrite_count > 0:
            rewrites_schema = """
  "rewrites": [
    {"title": "Tiêu đề bản 1", "html": "<h1>...</h1><p>...</p>"},
    {"title": "Tiêu đề bản 2", "html": "<h1>...</h1><p>...</p>"}
  ]"""

        captions_schema = ""
        if platforms:
            prefix = "," if rewrite_count > 0 else ""
            captions_schema = f"""{prefix}
  "captions": {{
    "facebook": "...",
    "instagram": "..."
  }}"""

        summary_schema = ""
        if need_summary:
            prefix = "," if (rewrite_count > 0 or platforms) else ""
            summary_schema = f"""{prefix}
  "summary": "Đoạn tóm tắt 260-280 ký tự..."
"""

        cta_schema = ""
        if need_zalo_cta:
            prefix = "," if (rewrite_count > 0 or platforms or need_summary) else ""
            cta_schema = f"""{prefix}
  "zalo_cta": "Câu kêu gọi hành động dưới 50 ký tự..."
"""

        prompt = f"""
Bạn là chuyên gia SEO content. CHỦ ĐỀ: {topic} | TIÊU ĐỀ GỐC: {title}
{rewrite_section}
{caption_section}
{summary_section}
{cta_section}
Trả về JSON hợp lệ duy nhất, không markdown, không giải thích:
{{{rewrites_schema}{captions_schema}{summary_schema}{cta_schema}}}
""".strip()

        raw = self.generate(prompt, max_retries=2)

        try:
            data = self._parse_json(raw)

            # Parse rewrites
            rewrites: list[tuple[str, str]] = []
            if rewrite_count > 0:
                for v in data.get("rewrites", [])[:rewrite_count]:
                    rewrites.append((
                        v.get("html", original_html),
                        v.get("title", title),
                    ))
                # Pad với bản gốc nếu Gemini trả thiếu
                while len(rewrites) < rewrite_count:
                    logger.warning("  → Gemini trả thiếu bản rewrite, dùng bản gốc")
                    rewrites.append((original_html, title))

            # Parse captions
            captions: dict = {}
            if platforms:
                captions = self._normalize_result(
                    data.get("captions", {}), facebook_pages
                )

            # Parse summary
            summary = data.get("summary", "") if need_summary else ""

            # Parse cta
            zalo_cta = data.get("zalo_cta", "") if need_zalo_cta else ""

            return rewrites, captions, summary, zalo_cta

        except Exception as e:
            logger.warning("  → Parse batch thất bại: %s — dùng fallback", e)
            return fallback_rewrites, fallback_captions, fallback_summary, fallback_zalo_cta