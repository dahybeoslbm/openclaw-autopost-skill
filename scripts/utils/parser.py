"""
utils/parser.py — Phân tích câu lệnh tự nhiên (NLU) từ người dùng.
Tách riêng để dễ nâng cấp sau (ví dụ: thay regex bằng LLM).
"""
import re
from utils.models import ParsedRequest
from utils.logger import get_logger

logger = get_logger(__name__)

# ── Mapping từ khoá → platform ─────────────────────────────
_PLATFORM_KEYWORDS: dict[str, list[str]] = {
    "Facebook" : ["facebook", "fb"],
    "Wordpress": ["wordpress", "wp"],
    "Instagram": ["instagram", "ig"],
    "Blog"     : [],  # default
}

# ── Mapping từ khoá → schedule ─────────────────────────────
def _detect_schedule(text: str) -> str:
    if "ngày mai" in text:
        return "Ngày mai"
    if "hôm nay" in text:
        return "Hôm nay"
    m = re.search(r"(\d+)\s*phút nữa", text)
    if m:
        return f"Sau {m.group(1)} phút"
    return ""


def _detect_platform(text: str) -> str:
    for platform, keywords in _PLATFORM_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return platform
    return "Blog"


def _detect_topic(text: str, original: str) -> str:
    match = re.search(r"(về|tại|ở|du lịch)\s+([^,.\n]+)", text)
    return match.group(2).strip() if match else original[:30]


def parse_request(user_prompt: str) -> ParsedRequest:
    """
    Phân tích câu lệnh tự nhiên thành ParsedRequest.
    Input : "Viết bài review về Đà Lạt, đăng lên WordPress lúc ngày mai"
    Output: ParsedRequest(topic="đà lạt", platform="Wordpress", schedule_time="Ngày mai")
    """
    logger.info("[1/6] Phân tích yêu cầu (NLU)...")
    lower = user_prompt.lower()

    result = ParsedRequest(
        topic=_detect_topic(lower, user_prompt),
        platform=_detect_platform(lower),
        schedule_time=_detect_schedule(lower),
    )

    logger.info(
        "  → Topic='%s' | Platform='%s' | Schedule='%s'",
        result.topic, result.platform, result.schedule_time
    )
    return result
