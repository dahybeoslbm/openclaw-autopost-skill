"""
utils/parser.py — Phân tích câu lệnh tự nhiên (NLU) từ người dùng.
Tách riêng để dễ nâng cấp sau (ví dụ: thay regex bằng LLM).
"""
from datetime import datetime, timedelta, timezone
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

    Hỗ trợ:
      - Không đề cập / "bây giờ" / "ngay"       → "" (đăng ngay)
      - "30 phút nữa"                            → now + 30m
      - "2 tiếng nữa" / "2 giờ nữa"             → now + 2h
      - "hôm nay lúc 20h" / "hôm nay 20:30"     → hôm nay giờ đó
      - "ngày mai" / "ngày mai lúc 8h"           → ngày mai [giờ đó]
      - "ngày mốt" / "ngày mốt lúc 9h30"        → ngày kia [giờ đó]
      - "20/5" / "20/5 lúc 8h"
      - "ngày 20 tháng 5 lúc 8h"
      - "20/5/2026 20:30"

    Giờ input theo múi VN (UTC+7), tự convert sang UTC khi trả về.
    """
    VN_OFFSET = timedelta(hours=7)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    now_vn  = now_utc + VN_OFFSET

    # ── Đăng ngay ────────────────────────────────────────────────────────────
    if re.search(r"(ngay bây giờ|bây giờ|ngay lập tức|ngay|liền)", text):
        return ""

    # ── Helper: trích giờ:phút từ text ───────────────────────────────────────
    def _extract_hm(t: str):
        """Trả về (h, m) nếu tìm thấy giờ cụ thể, else None."""
        # "lúc 8h30", "lúc 20:30", "lúc 8 giờ 30", "8h", "20h00"
        m = re.search(
            r"(?:lúc\s*)?(\d{1,2})\s*(?:h|giờ)\s*(\d{2})?(?:\s*phút)?",
            t,
        )
        if m:
            return int(m.group(1)), int(m.group(2) or 0)
        # "20:30"
        m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
        if m:
            return int(m.group(1)), int(m.group(2))
        return None

    def _apply_hm(dt_vn: datetime, hm) -> datetime:
        """Gán giờ:phút vào datetime (VN), trả về UTC."""
        if hm:
            h, mi = hm
            dt_vn = dt_vn.replace(hour=h, minute=mi, second=0, microsecond=0)
        else:
            dt_vn = dt_vn.replace(second=0, microsecond=0)
        return dt_vn - VN_OFFSET  # → UTC

    hm = _extract_hm(text)

    # ── X phút nữa ───────────────────────────────────────────────────────────
    m = re.search(r"(\d+)\s*phút\s*nữa", text)
    if m:
        return (now_utc + timedelta(minutes=int(m.group(1)))).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── X tiếng / giờ nữa ────────────────────────────────────────────────────
    m = re.search(r"(\d+)\s*(?:tiếng|giờ)\s*nữa", text)
    if m:
        return (now_utc + timedelta(hours=int(m.group(1)))).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Ngày mốt ─────────────────────────────────────────────────────────────
    if re.search(r"ngày\s*(mốt|kia)", text):
        target_vn = now_vn + timedelta(days=2)
        return _apply_hm(target_vn, hm).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Ngày mai ─────────────────────────────────────────────────────────────
    if "ngày mai" in text:
        target_vn = now_vn + timedelta(days=1)
        return _apply_hm(target_vn, hm).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Hôm nay + giờ cụ thể ─────────────────────────────────────────────────
    if "hôm nay" in text and hm:
        target_utc = _apply_hm(now_vn, hm)
        return target_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Ngày cụ thể: "ngày 20 tháng 5 [năm 2026] [lúc 8h]" ──────────────────
    m = re.search(
        r"ngày\s*(\d{1,2})\s*tháng\s*(\d{1,2})(?:\s*(?:năm\s*)?(\d{4}))?",
        text,
    )
    if not m:
        # "20/5[/2026]" hoặc "20-5[-2026]"
        m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{4}))?\b", text)

    if m:
        day   = int(m.group(1))
        month = int(m.group(2))
        year  = int(m.group(3)) if m.group(3) else now_vn.year
        try:
            target_vn  = now_vn.replace(year=year, month=month, day=day,
                                        second=0, microsecond=0)
            target_utc = _apply_hm(target_vn, hm)
            # Ngày đã qua & không chỉ định năm → tự đẩy sang năm sau
            if target_utc < now_utc and not m.group(3):
                target_vn  = target_vn.replace(year=year + 1)
                target_utc = _apply_hm(target_vn, hm)
            return target_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass  # ngày không hợp lệ (vd 31/2) → bỏ qua

    # ── Chỉ có giờ, không có ngày ("lúc 20h", "8h30") ────────────────────────
    if hm:
        target_utc = _apply_hm(now_vn, hm)
        # Giờ đã qua hôm nay → tự chuyển sang ngày mai
        if target_utc <= now_utc:
            target_utc += timedelta(days=1)
        return target_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Không đề cập thời gian → đăng ngay ──────────────────────────────────
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
