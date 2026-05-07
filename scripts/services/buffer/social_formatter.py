"""
social_formatter.py — Build nội dung post riêng cho từng platform.
"""


def _hashtags(topic: str) -> str:
    tag = topic.replace(" ", "")
    return f"#{tag} #dulich #travel #vietnam"


def for_facebook(title: str, excerpt: str, url: str, topic: str) -> dict:
    text = f"{title}\n\n{excerpt}\n\n{url}" if url else f"{title}\n\n{excerpt}"
    return {"text": text.strip()}


def for_instagram(title: str, excerpt: str, topic: str) -> dict:
    """Instagram: KHÔNG để link trong caption — vô nghĩa."""
    text = f"{title}\n\n{excerpt}\n\n{_hashtags(topic)}\n\n📍 Link in bio"
    return {"text": text.strip()}


def for_twitter(title: str, url: str, topic: str) -> dict:
    """Twitter: tối đa 280 ký tự."""
    base = f"{title} {_hashtags(topic)}"
    if url:
        # URL Twitter tự đếm 23 ký tự bất kể độ dài thật
        max_text = 280 - 23 - 1  # 1 cho space
        base = base[:max_text]
        text = f"{base} {url}"
    else:
        text = base[:280]
    return {"text": text.strip()}


def for_threads(title: str, excerpt: str, url: str) -> dict:
    """Threads: ngắn, conversational."""
    text = f"{title}\n\n{excerpt[:200]}..." if len(excerpt) > 500 else f"{title}\n\n{excerpt}"
    if url:
        text += f"\n\n{url}"
    return {"text": text.strip()}


def for_linkedin(title: str, excerpt: str, url: str, topic: str) -> dict:
    """LinkedIn: tone chuyên nghiệp hơn."""
    text = (
        f"✈️ {title}\n\n"
        f"{excerpt}\n\n"
        f"Khám phá ngay: {url}\n\n"
        f"#DuLich #{topic.replace(' ', '')} #Travel #Vietnam"
    )
    return {"text": text.strip()}


def for_tiktok(title: str, topic: str) -> dict:
    """TikTok: ngắn + hashtag nhiều."""
    text = f"{title}\n\n{_hashtags(topic)} #tiktoktravel #tiktokdulich"
    return {"text": text.strip()}


def build_all(
    topic: str,
    title: str,
    excerpt: str,
    wp_url: str,
) -> dict[str, dict]:
    """
    Trả về dict platform → post options.
    Dùng để truyền vào từng platform.create_post().
    """
    return {
        "facebook":  for_facebook(title, excerpt, wp_url, topic),
        "instagram": for_instagram(title, excerpt, topic),
        "twitter":   for_twitter(title, wp_url, topic),
        "threads":   for_threads(title, excerpt, wp_url),
        "linkedin":  for_linkedin(title, excerpt, wp_url, topic),
        "tiktok":    for_tiktok(title, topic),
        # Các platform còn lại dùng facebook format làm fallback
        "youtube":        for_facebook(title, excerpt, wp_url, topic),
        "bluesky":        for_threads(title, excerpt, wp_url),
        "pinterest":      for_facebook(title, excerpt, wp_url, topic),
        "mastodon":       for_threads(title, excerpt, wp_url),
        "google_business":for_facebook(title, excerpt, wp_url, topic),
    }