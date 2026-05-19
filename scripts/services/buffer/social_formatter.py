"""
social_formatter.py — Build nội dung post riêng cho từng platform.
"""

def _hashtags(topic: str) -> str:
    tag = topic.title().replace(" ", "")
    return f"#{tag} #DuLich #Travel #Vietnam"


def for_facebook(caption: str, url: str) -> dict:
    text = f"{caption}\n\n{url}" if url else caption
    return {"text": text.strip()}


def for_instagram(caption: str) -> dict:
    """Link KHÔNG đặt trong caption — vô nghĩa vì không clickable."""
    return {"text": caption.strip()}


def for_twitter(caption: str, url: str) -> dict:
    """
    Tổng = caption + space + url.
    URL luôn = 23 ký tự trên Twitter dù dài bao nhiêu.
    Caption tối đa = 280 - 23 - 1 = 256 ký tự.
    """
    max_caption = 256
    caption = caption[:max_caption].strip()
    text = f"{caption} {url}" if url else caption
    return {"text": text.strip()}


def for_threads(caption: str, url: str) -> dict:
    suffix = f"\n\n{url}" if url else ""
    limit = 470
    max_caption = limit - len(suffix)
    text = f"{caption[:max_caption]}{suffix}".strip()
    # Hard cap — Threads API reject nếu > 500
    if len(text) > 500:
        text = text[:497] + "..."
    return {"text": text}


def for_tiktok(caption: str) -> dict:
    """Limit 4,000 ký tự — Gemini đã format sẵn."""
    return {"text": caption.strip()}


def for_linkedin(caption: str, url: str) -> dict:
    """Limit 3,000 ký tự. Thêm link ở cuối."""
    suffix = f"\n\n🔗 {url}" if url else ""
    max_caption = 3000 - len(suffix)
    text = f"{caption[:max_caption]}{suffix}"
    return {"text": text.strip()}


def for_pinterest(caption: str, url: str) -> dict:
    """Limit 500 ký tự. Keyword-based, không hashtag."""
    suffix = f" {url}" if url else ""
    max_caption = 500 - len(suffix)
    text = f"{caption[:max_caption]}{suffix}"
    return {"text": text.strip()}


def for_bluesky(caption: str) -> dict:
    """
    300 ký tự. Link thêm dưới dạng card riêng (Buffer tự xử lý).
    → KHÔNG nhét link vào text để tiết kiệm ký tự.
    """
    return {"text": caption[:300].strip()}


def for_mastodon(caption: str, url: str) -> dict:
    """500 ký tự. Hashtag giúp discovery."""
    suffix = f"\n\n{url}" if url else ""
    max_caption = 500 - len(suffix)
    text = f"{caption[:max_caption]}{suffix}"
    return {"text": text.strip()}


def for_google_business(caption: str, url: str) -> dict:
    """1,500 ký tự. Local SEO + CTA."""
    suffix = f"\n\n👉 {url}" if url else ""
    max_caption = 1500 - len(suffix)
    text = f"{caption[:max_caption]}{suffix}"
    return {"text": text.strip()}


def build_all(
    topic: str,
    title: str,
    excerpt: str,
    wp_url: str,
    social_captions: dict | None = None,  # từ Gemini
) -> dict[str, dict]:
    """
    Ưu tiên dùng social_captions từ Gemini (đã được tối ưu per-platform).
    Fallback về excerpt nếu Gemini không trả về.
    """
    sc = social_captions or {}
    fallback = excerpt or title

    return {
        "facebook": for_facebook(
            sc.get("facebook", fallback) if not isinstance(sc.get("facebook"), list)
            else (sc["facebook"][0] if sc["facebook"] else fallback),
            wp_url
        ),
        "instagram":      for_instagram(sc.get("instagram", fallback)),
        "twitter":        for_twitter(sc.get("twitter", title), wp_url),
        "threads":        for_threads(sc.get("threads", fallback), wp_url),
        "tiktok":         for_tiktok(sc.get("tiktok", fallback)),
        "linkedin":       for_linkedin(sc.get("linkedin", fallback), wp_url),
        "pinterest":      for_pinterest(sc.get("pinterest", fallback), wp_url),
        "bluesky":        for_bluesky(sc.get("bluesky", title)),
        "mastodon":       for_mastodon(sc.get("mastodon", fallback), wp_url),
        "google_business":for_google_business(sc.get("google_business", fallback), wp_url),
    }