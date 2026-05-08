"""
utils/parser.py — Phân tích câu lệnh tự nhiên (NLU) từ người dùng.
Tách riêng để dễ nâng cấp sau (ví dụ: thay regex bằng LLM).
"""
from datetime import datetime, timedelta
import re
from utils.models import ParsedRequest
from utils.logger import get_logger




logger = get_logger(__name__)

# ── Mapping từ khoá → platform ─────────────────────────────
_PLATFORM_KEYWORDS: dict[str, list[str]] = {
    "wordpress":      ["wordpress", "wp"],
    "facebook":       ["facebook", "fb"],
    "instagram":      ["instagram", "ig"],
    "tiktok":         ["tiktok", "tik tok"],
    "threads":        ["threads"],
    "twitter":        ["twitter", "x.com"],
    "linkedin":       ["linkedin"],
    "youtube":        ["youtube", "yt"],
    "bluesky":        ["bluesky", "blue sky"],
    "pinterest":      ["pinterest"],
    "mastodon":       ["mastodon"],
    "google_business":["google business", "google my business"],
    "blog":           [],  # default — không khớp gì hết
}

# ── Mapping từ khoá → schedule ─────────────────────────────
def _detect_schedule(text: str) -> str:
    """
    Trả về ISO 8601 UTC string nếu user nói thời gian,
    trả về "" nếu không nói gì (= đăng ngay).
    """
    now = datetime.utcnow()

    # "30 phút nữa", "2 tiếng nữa"
    m = re.search(r"(\d+)\s*phút\s*nữa", text)
    if m:
        return (now + timedelta(minutes=int(m.group(1)))).strftime("%Y-%m-%dT%H:%M:%SZ")

    m = re.search(r"(\d+)\s*tiếng\s*nữa", text)
    if m:
        return (now + timedelta(hours=int(m.group(1)))).strftime("%Y-%m-%dT%H:%M:%SZ")

    # "ngày mai" = +24h cùng giờ
    if "ngày mai" in text:
        return (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # "hôm nay" hoặc không nói gì → đăng ngay
    return ""


def _detect_platform(text: str) -> str:
    for platform, keywords in _PLATFORM_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return platform
    return "Blog"


def _detect_topic(text: str, original: str) -> str:
    match = re.search(r"(về|tại|ở|du lịch|bài)\s+([^,.\n]+)", text)
    if not match:
        return original[:30]
    
    raw = match.group(2).strip()
    
    # Cắt bỏ phần "đăng <platform>" nếu có
    raw = re.sub(
        r"\s+(đăng|post|lên)\s+(facebook|instagram|tiktok|threads|twitter|"
        r"linkedin|youtube|bluesky|pinterest|mastodon|wordpress|wp|fb|ig).*$",
        "", raw
    ).strip()
    
    return raw


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
