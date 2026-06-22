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
    "google_business":["google business", "google my business", "google map", "google maps"],
    "zalo":           ["zalo"],
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


def _detect_platforms(text: str) -> list[str]:
    found = [
        platform
        for platform, keywords in _PLATFORM_KEYWORDS.items()
        if platform != "blog" and any(kw in text for kw in keywords)
    ]
    return found if found else ["Blog"]


def _detect_topic(text: str, original: str) -> str:
    """
    Strip time → platform → trigger words → connector words.
    Phần còn lại là topic.
    """
    s = text.strip()

    # ── 1. Strip time expressions (phức tạp → đơn giản) ──────────────────────
    _TIME = [
        # "trong 3 phút nữa", "3 phút nữa", "2 tiếng nữa"
        r"\btrong\s+\d+\s*(?:phút|tiếng|giờ)\s*nữa",
        r"\b\d+\s*(?:phút|tiếng|giờ)\s*nữa",
        # "ngày mai lúc 8h30", "ngày mốt lúc 9h"
        r"\bngày\s+(?:mai|mốt|kia)(?:\s+lúc\s+\d{1,2}\s*(?:h|giờ)\s*\d{0,2}\s*(?:phút|rưỡi)?)?",
        # "hôm nay lúc 20h"
        r"\bhôm\s+nay(?:\s+lúc\s+\d{1,2}\s*(?:h|giờ)\s*\d{0,2}\s*(?:phút|rưỡi)?)?",
        # "vào lúc 2 giờ rưỡi", "lúc 8h30", "lúc 20:30"
        r"\b(?:vào\s+)?lúc\s+\d{1,2}\s*(?:h|giờ|:)\s*\d{0,2}\s*(?:phút|rưỡi)?",
        # "ngày 20 tháng 5 năm 2026 lúc 8h"
        r"\bngày\s+\d{1,2}\s+tháng\s+\d{1,2}(?:\s+(?:năm\s+)?\d{4})?(?:\s+lúc\s+\d{1,2}\s*(?:h|giờ)\s*\d{0,2})?",
        # "20/5/2026 20:30", "20/5"
        r"\b\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{4})?(?:\s+\d{1,2}[h:]\d{0,2})?",
        # "8h30", "20h" standalone
        r"\b\d{1,2}h\d{0,2}\b",
        # "ngay lập tức", "bây giờ"
        r"\b(?:ngay\s+lập\s+tức|ngay\s+bây\s+giờ|bây\s+giờ)\b",
        # trailing "vào" còn sót sau khi strip time
        r"\bvào\s*$",
    ]
    for p in _TIME:
        s = re.sub(p, " ", s, flags=re.IGNORECASE)

    # ── 2. Strip platform expressions ─────────────────────────────────────────
    _PLT = (
        r"wordpress|wp|facebook|fb|instagram|ig|tiktok|tik\s+tok|"
        r"threads|twitter|x\.com|linkedin|youtube|yt|bluesky|blue\s+sky|"
        r"pinterest|mastodon|google\s+business|google\s+my\s+business|google\s+map|google\s+maps|zalo"
    )
    # "đăng lên facebook", "post trên instagram"
    s = re.sub(rf"\b(?:đăng|post|lên|trên|qua|lên\s+trang)\s+(?:{_PLT})\b", " ", s, flags=re.IGNORECASE)
    # standalone platform còn sót
    s = re.sub(rf"\b(?:{_PLT})\b", " ", s, flags=re.IGNORECASE)

    # ── 3. Strip trigger / action words ───────────────────────────────────────
    # Đầu câu: "hãy đăng bài", "cho tôi post bài về", "viết bài"
    s = re.sub(
        r"^(?:(?:hãy|giúp\s+tôi|cho\s+tôi)\s+)?(?:đăng|post|viết|tạo|soạn)\s+(?:bài\s+(?:viết\s+)?)?",
        "", s, flags=re.IGNORECASE
    )
    # Giữa câu: "bài viết về", "nội dung về", "review về", "giới thiệu về"
    s = re.sub(
        r"\b(?:bài\s+viết|bài\s+đăng|nội\s+dung|review|giới\s+thiệu)\s+(?:về\s+|tại\s+|ở\s+)?",
        " ", s, flags=re.IGNORECASE
    )

    # ── 4. Strip connector / preposition words ────────────────────────────────
    # "về", "tại", "ở", "du lịch" đứng trước topic
    s = re.sub(r"\b(?:về|tại|ở|du\s+lịch|bài)\s+", " ", s, flags=re.IGNORECASE)
    # trailing connectors còn thừa
    s = re.sub(r"\s+\b(?:vào|trong|lúc|và|hoặc|hay|đăng|post|trên|lên|tại|ở)\s*$", "", s, flags=re.IGNORECASE)

    # ── 5. Cleanup ────────────────────────────────────────────────────────────
    s = re.sub(r"\s+", " ", s).strip().strip(",.!")

    return s if len(s) >= 2 else original[:50]


def _detect_preselection(text: str) -> tuple[str, str, str, str]:
    """
    Extract inline selections for facebook pages and wp sites.
    Returns (pages_sel, wp_sel, fb_full_match, wp_full_match)
    """
    pages_sel = ""
    wp_sel = ""
    fb_full = ""
    wp_full = ""
    
    # Split the text by connector words to isolate clauses
    clauses = re.split(r'\b(?:và|hoặc|hay)\b', text)
    
    for clause in clauses:
        fb_m = re.search(r"\b(tất cả(?: các)?\s+)?(?:page(?:s)?\s+)?(?:facebook|fb)\s*(?:page(?:s)?)?(?:\s+(.+?))?$", clause, re.IGNORECASE)
        if fb_m:
            prefix = (fb_m.group(1) or "").strip()
            suffix = (fb_m.group(2) or "").strip()
            if "tất cả" in prefix.lower() or "tất cả" in suffix.lower():
                pages_sel = "tất cả"
            else:
                pages_sel = suffix
            pages_sel = re.sub(r'\b(?:lên|trên)$', '', pages_sel).strip()
            if not fb_full:
                fb_full = fb_m.group(0).strip()
            
        wp_m = re.search(r"\b(tất cả(?: các)?\s+)?(?:site(?:s)?\s+)?(?:wordpress|wp)\s*(?:site(?:s)?)?(?:\s+(.+?))?$", clause, re.IGNORECASE)
        if wp_m:
            prefix = (wp_m.group(1) or "").strip()
            suffix = (wp_m.group(2) or "").strip()
            if "tất cả" in prefix.lower() or "tất cả" in suffix.lower():
                wp_sel = "tất cả"
            else:
                wp_sel = suffix
            wp_sel = re.sub(r'\b(?:lên|trên)$', '', wp_sel).strip()
            if not wp_full:
                wp_full = wp_m.group(0).strip()

    return pages_sel, wp_sel, fb_full, wp_full

def parse_request(user_prompt: str) -> ParsedRequest:
    """
    Phân tích câu lệnh tự nhiên thành ParsedRequest.
    """
    logger.info("[1/6] Phân tích yêu cầu (NLU)...")
    lower = user_prompt.lower()

    pages_sel, wp_sel, fb_full, wp_full = _detect_preselection(lower)
    
    # Remove the preselection chunks so they don't pollute the topic
    topic_cleaner = lower
    if fb_full:
        topic_cleaner = topic_cleaner.replace(fb_full, " facebook ")
    if wp_full:
        topic_cleaner = topic_cleaner.replace(wp_full, " wordpress ")

    result = ParsedRequest(
        topic=_detect_topic(topic_cleaner, user_prompt),
        platforms=_detect_platforms(lower),
        schedule_time=_detect_schedule(lower),
        preselected_pages=pages_sel,
        preselected_wp_sites=wp_sel,
    )

    logger.info(
        "  → Topic='%s' | Platform='%s' | Schedule='%s' | FB_Pages='%s' | WP_Sites='%s'",
        result.topic, result.platforms, result.schedule_time, result.preselected_pages, result.preselected_wp_sites
    )
    return result
