"""
blogger.py — Entry point. Chỉ chứa orchestration (điều phối luồng).
Không chứa business logic — mọi thứ đã được tách vào services/.

Luồng nghiệp vụ:
  1. Parse prompt → topic, platform, schedule
  2. Tìm Google Doc tên = topic, lấy file mới nhất (api.drive.article)
  3. Nếu có doc → Gemini CHỈ convert format (Docs JSON → SEO HTML)
                   Nội dung giữ nguyên 100% — không viết thêm
     Nếu không  → fallback: OpenClaw scrape → Gemini viết bài
  4. Đăng WordPress và/hoặc Buffer tuỳ platform
"""
import os
import sys
import time

import requests

from config import load_config

from utils.logger import get_logger
from utils.models import PublishResult
from utils.parser import parse_request
from services.gemini import GeminiService
from services.wordpress import WordPressService
from services.googledrive import GoogleDriveService, DriveArticle

from services.buffer import BufferClient
from utils.models    import BufferPostResult, ScrapedContent
from services.buffer.social_formatter import build_all as build_social_texts

from datetime import datetime, timezone
logger = get_logger("blogger")


# ── Helpers ─────────────────────────────────────────────────



def _build_images_markdown(image_files: list[str]) -> str:
    lines = []
    for i, img in enumerate(image_files):
        if img.startswith("http"):
            lines.append(f"![Hình minh họa {i+1}]({img})")
        else:
            lines.append(f"![Hình minh họa {i+1}](./{img})")
    return "\n".join(lines)


def _save_to_file(content: str, output_dir: str, topic: str, platform: str, schedule: str) -> str:
    """Lưu bài viết thành file .md, trả về đường dẫn."""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"travel_blog_{int(time.time())}.md")
    header = f"---\nTopic: {topic}\nSchedule: {schedule}\nPlatform: {platform}\n---\n\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + content)
    return path


def _notify_webhook(webhook_url: str, title: str, file_path: str) -> None:
    """Gửi thông báo qua webhook (Telegram, v.v.)."""
    try:
        requests.post(
            webhook_url,
            json={"text": f"✅ Bài '{title}' xong!\nFile: {file_path}"},
            timeout=5,
        )
    except Exception as e:
        logger.warning("Webhook error: %s", e)

# Danh sách platform thuộc Buffer (social)
_BUFFER_PLATFORMS = {
    "facebook", "instagram", "tiktok", "threads",
    "twitter", "linkedin", "youtube", "bluesky",
    "pinterest", "mastodon", "google_business",
}
_SERVICE_TO_ATTR = {
    "facebook": "facebook", "instagram": "instagram",
    "tiktok": "tiktok", "threads": "threads",
    "twitter": "twitter", "x": "twitter",
    "linkedin": "linkedin", "youtube": "youtube",
    "bluesky": "bluesky", "pinterest": "pinterest",
    "mastodon": "mastodon",
    "google": "google_business",
    "google_business": "google_business",
    "googlebusiness": "google_business",
}

# ── Source resolver ───────────────────────────────────────────────────────────
 
def _resolve_source(
    topic: str,
    cfg,
) ->  DriveArticle | None:
    """
    Tìm Google Doc theo topic.
    Trả về DriveArticle nếu tìm thấy, None nếu không có.
    Không fallback về OpenClaw — nội dung phải do người viết.
    """
    if not cfg.googledrive.is_valid:
        logger.error("  → [Drive] GDRIVE_API_URL chưa cấu hình trong .env")
        return None

    try:
        drive_service = GoogleDriveService.from_config(cfg.googledrive)
        drive_article = drive_service.fetch_article(
            topic=topic,
            language=cfg.googledrive.language,
        )
        if drive_article:
            logger.info("  → [Drive] ✅ Tìm thấy: \"%s\"", drive_article.title)
            return drive_article

        logger.warning("  → [Drive] Không tìm thấy doc nào cho topic: \"%s\"", topic)
        return None

    except RuntimeError as exc:
        logger.error("  → [Drive] Lỗi kết nối: %s", exc)
        return None
 
 

# ── Main workflow ────────────────────────────────────────────

def run(user_prompt: str, webhook_url: str | None = None) -> PublishResult:
    """
    Chạy toàn bộ workflow từ câu lệnh tự nhiên đến xuất bản.
    Trả về PublishResult chứa kết quả.
    """
    cfg = load_config()

    # Khởi tạo services
    gemini   = GeminiService(cfg.gemini, ollama_config=cfg.ollama)
    wp       = WordPressService(cfg.wordpress)

    # ── Bước 1: Parse yêu cầu ───────────────────────────────
    parsed = parse_request(user_prompt)

    # ── Bước 2: Tìm tài liệu Google Drive ───────────────────────────────────
    logger.info("[2/6] Tìm tài liệu Google Drive...")
    drive_article = _resolve_source(topic=parsed.topic, cfg=cfg)

    if not drive_article:
        msg = (
            f"❌ Không tìm thấy tài liệu nào cho topic: \"{parsed.topic}\".\n"
            f"Vui lòng tạo Google Doc với tên khớp topic rồi thử lại."
        )
        logger.error(msg)
        # Webhook thông báo thất bại nếu có
        if webhook_url:
            try:
                requests.post(webhook_url, json={"text": msg}, timeout=5)
            except Exception:
                pass
        return PublishResult(file_path="", error=msg)

    # ── Bước 3: Ảnh từ Drive (không render AI) ───────────────────────────────
    logger.info("[3/6] Dùng %d ảnh từ Google Docs", drive_article.image_count())
    image_files: list[str] = []   # ảnh đã nhúng trong HTML, không cần file riêng

    # ── Bước 4: Tạo nội dung HTML ────────────────────────────────────────────
    logger.info("[4/6] Chuyển đổi nội dung (Gemini)...")
    plain = drive_article.plain_text()
    social_captions = gemini.generate_social_captions(
        topic      = parsed.topic,
        title      = drive_article.title,
        plain_text = plain,
    )
    logger.info("  → social_captions: %d platforms", len(social_captions))

    article_data = {
        "seo_title":        drive_article.title,
        "meta_description": plain[:160],
        "focus_keyword":    parsed.topic,
        "excerpt":          plain[:300],
        "content_html":     drive_article.content,   # Drive, không thay đổi
        "social_captions":  social_captions,
    }

    # ── Bước 5: Lưu file backup ──────────────────────────────────────────────
    file_path = _save_to_file(
        content    = article_data.get("content_html", ""),
        output_dir = cfg.output_dir,
        topic      = parsed.topic,
        platform   = parsed.platform,
        schedule   = parsed.schedule_time,
    )
    logger.info("[5/6] 💾 Lưu file: %s", file_path)
 
    result = PublishResult(file_path=file_path)

    # ── Bước 6: Xuất bản ────────────────────────────────────
    logger.info("[6/6] Xuất bản bài viết...")
    platform_lower = parsed.platform.lower()
    
    # WordPress
    if platform_lower == "wordpress":
        wp_result = wp.publish(
            article_data   = article_data,
            image_files    = image_files,
            schedule_time  = parsed.schedule_time,
            category_names = ["Du Lịch"],
            tag_names      = [parsed.topic.title()],
        )
        if wp_result:
            result.wp_post_id  = wp_result["id"]
            result.wp_post_url = wp_result["link"]
            result.wp_status   = wp_result["status"]
            logger.info("  → ✅ WordPress: %s", result.wp_post_url)

    # Webhook notification
    title = article_data.get("seo_title", parsed.topic)
    if webhook_url:
        _notify_webhook(webhook_url, title, file_path)
    
    # Buffer (mạng xã hội)
    should_run_buffer = cfg.buffer.is_valid and platform_lower != "wordpress"
 
    if should_run_buffer:
        buffer_platforms = (
            [platform_lower] if platform_lower in _BUFFER_PLATFORMS else []
        )
        logger.info(
            "[7/7] Buffer: %s",
            ", ".join(buffer_platforms) if buffer_platforms else "tất cả platforms",
        )
 
        # Ảnh đính kèm post: nếu có image_files dùng file, không thì bỏ qua
        social_image_urls = [f for f in image_files if f.startswith("http")][:1]
 
        social_texts = build_social_texts(
            topic          = parsed.topic,
            title          = title,
            excerpt        = article_data.get("excerpt", ""),
            wp_url         = result.wp_post_url or "",
            social_captions= article_data.get("social_captions"),
        )
        scheduled_at = parsed.schedule_time or None
 
        try:
            buffer  = BufferClient(api_key=cfg.buffer.api_key)
            targets = buffer.get_channels_from_env(buffer_platforms)
 
            if not targets:
                logger.warning("  → Không tìm thấy channel nào trong .env")
 
            for ch in targets:
                service      = (ch.get("service") or "").lower()
                post_opts    = social_texts.get(service) or social_texts["facebook"]
                attr         = _SERVICE_TO_ATTR.get(service)
                platform_obj = getattr(buffer, attr, None) if attr else None
 
                if not platform_obj:
                    logger.warning("  → '%s' chưa hỗ trợ, bỏ qua", service)
                    continue
 
                try:
                    post = platform_obj.create_post(
                        ch["id"],
                        text         = post_opts["text"],
                        image_urls   = social_image_urls or None,
                        scheduled_at = scheduled_at,
                    )
                    br = BufferPostResult(
                        platform=service, channel_name=ch.get("name", ""),
                        channel_id=ch["id"], status="success",
                        post_id=post.get("id", ""),
                    )
                    logger.info("  → ✅ [%s] %s", service.upper(), ch.get("name"))
                except Exception as e:
                    br = BufferPostResult(
                        platform=service, channel_name=ch.get("name", ""),
                        channel_id=ch["id"], status="error", error=str(e),
                    )
                    logger.warning("  → ❌ [%s] %s — %s", service.upper(), ch.get("name"), e)
 
                result.buffer_results.append(br)
 
            succeeded = sum(1 for r in result.buffer_results if r.status == "success")
            logger.info("  → Buffer: %d/%d channel thành công",
                        succeeded, len(result.buffer_results))
        except Exception as e:
            logger.warning("  → Buffer thất bại: %s", e)
 
    elif platform_lower == "wordpress":
        logger.info("[7/7] Bỏ qua Buffer (chỉ đăng WordPress)")
    else:
        logger.info("[7/7] Bỏ qua Buffer (chưa cấu hình BUFFER_API_KEY)")
 
    return result

# ── Entry point ──────────────────────────────────────────────

def main():
    user_input  = sys.argv[1] if len(sys.argv) > 1 else "Du lịch Đà Nẵng"
    webhook_url = sys.argv[2] if len(sys.argv) > 2 else None

    result = run(user_input, webhook_url)

    logger.info("\n[+] Hoàn thành! Kết quả:")
    logger.info("    File     : %s", result.file_path)
    if result.posted_to_wp:
        logger.info("    WP ID    : %d", result.wp_post_id)
        logger.info("    WP URL   : %s", result.wp_post_url)
        logger.info("    WP Status: %s", result.wp_status)


if __name__ == "__main__":
    main()
