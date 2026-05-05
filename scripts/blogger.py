"""
blogger.py — Entry point. Chỉ chứa orchestration (điều phối luồng).
Không chứa business logic — mọi thứ đã được tách vào services/.
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
from services.openclaw import OpenClawService
from services.image import render_images_parallel, parse_image_prompts
from services.wordpress import WordPressService

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


# ── Main workflow ────────────────────────────────────────────

def run(user_prompt: str, webhook_url: str | None = None) -> PublishResult:
    """
    Chạy toàn bộ workflow từ câu lệnh tự nhiên đến xuất bản.
    Trả về PublishResult chứa kết quả.
    """
    cfg = load_config()

    # Khởi tạo services
    gemini   = GeminiService(cfg.gemini)
    openclaw = OpenClawService(cfg.openclaw)
    wp       = WordPressService(cfg.wordpress)

    # ── Bước 1: Parse yêu cầu ───────────────────────────────
    parsed = parse_request(user_prompt)

    # ── Bước 2: Tìm kiếm nguồn ──────────────────────────────
    source_url = openclaw.search(parsed.topic) or "https://vnexpress.net/du-lich"

    # ── Bước 3: Cào nội dung ────────────────────────────────
    scraped = openclaw.fetch(source_url)

    # ── Bước 4: Sinh image prompts ──────────────────────────
    logger.info("[4/6] Chuyển đổi ý tưởng ảnh...")
    alts = [img.alt for img in scraped.images if img.alt] or [parsed.topic] * 3
    raw_prompts = gemini.generate(
        gemini.build_image_prompts(alts[:3], parsed.topic)
    )
    prompts = parse_image_prompts(raw_prompts, parsed.topic, count=len(alts[:3]))

    # ── Bước 4.5: Vẽ ảnh song song ──────────────────────────
    image_files = render_images_parallel(prompts, scraped.images)

    # ── Bước 5: Viết bài ────────────────────────────────────
    logger.info("[5/6] Chấp bút bài viết (Gemini)...")
    article_data = gemini.generate_article(
        gemini.build_article_prompt(
            topic=parsed.topic,
            platform=parsed.platform,
            source_url=source_url,
            text_content=scraped.text,
            images_markdown=_build_images_markdown(image_files),
        )
    )

    # ── Bước 6: Xuất bản ────────────────────────────────────
    logger.info("[6/6] Xuất bản bài viết...")

    # Luôn lưu file .md (backup)
    file_path = _save_to_file(
        content=article_data.get("content_html", ""),
        output_dir=cfg.output_dir,
        topic=parsed.topic,
        platform=parsed.platform,
        schedule=parsed.schedule_time,
    )
    logger.info("  → 💾 Lưu file: %s", file_path)

    result = PublishResult(file_path=file_path)

    # Đăng WordPress nếu platform phù hợp
    if parsed.platform.lower() == "wordpress":
        wp_result = wp.publish(
            article_data=article_data,
            image_files=image_files,
            schedule_time=parsed.schedule_time,
            category_names=["Du Lịch"],
            tag_names=[parsed.topic.title()],
        )
        if wp_result:
            result.wp_post_id  = wp_result["id"]
            result.wp_post_url = wp_result["link"]
            result.wp_status   = wp_result["status"]

    # Webhook notification
    if webhook_url:
        _notify_webhook(webhook_url, title, file_path)

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
