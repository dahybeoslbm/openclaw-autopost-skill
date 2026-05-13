"""
utils/media.py — Tiện ích xử lý URL media: Google Drive, Dropbox.
"""
import re
from typing import Optional


def is_google_drive_url(url: str) -> bool:
    return isinstance(url, str) and "drive.google.com" in url


def drive_to_direct_url(url: str, media_type: str = "image") -> str:
    match = re.search(r'/d/([a-zA-Z0-9_-]+)', url) or \
            re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
    if not match:
        raise ValueError(
            f'URL Google Drive không hợp lệ: "{url}". '
            f'Định dạng đúng: https://drive.google.com/file/d/FILE_ID/view'
        )
    file_id = match.group(1)
    if media_type == "video":
        return f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}"
    return f"https://drive.google.com/uc?export=view&id={file_id}"


def resolve_media_url(url: str, media_type: str = "image") -> str:
    """Auto-convert nếu là Drive URL, giữ nguyên nếu không phải."""
    return drive_to_direct_url(url, media_type) if is_google_drive_url(url) else url


def resolve_assets(
    image_urls: list[str] | None = None,
    video_url: str | None = None,
) -> dict | None:
    """
    Build AssetsInput dict cho Buffer GraphQL API.
    Video được ưu tiên hơn images nếu cả hai cùng có mặt.
    """
    image_urls = image_urls or []
    resolved_video  = resolve_media_url(video_url, "video") if video_url else None
    resolved_images = [resolve_media_url(u, "image") for u in image_urls]

    if resolved_video:
        return {"videos": [{"url": resolved_video}]}
    if resolved_images:
        return [{"image": {"url": u}} for u in resolved_images]
    return None