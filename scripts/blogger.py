"""
blogger.py — Entry point. Chỉ chứa orchestration (điều phối luồng).
Không chứa business logic — mọi thứ đã được tách vào services/.

Luồng nghiệp vụ:
  1. Parse prompt → topic, platform, schedule
  2. Tìm Google Doc tên = topic, lấy file mới nhất (api.drive.article)
  3. Nếu có doc → Gemini CHỈ convert format caption cho social media, KHÔNG đổi content HTML
  4. Đăng WordPress và/hoặc Buffer tuỳ platform
     - "blog" (mặc định, không đề cập platform) → đăng TẤT CẢ: WP + mọi Buffer channel đã đăng ký
     - "wordpress" → chỉ WP
     - Tên platform cụ thể (facebook, instagram, …) → chỉ Buffer platform đó
"""
import os
import sys
import time
import re as _img_re
import concurrent.futures
from urllib.parse import urlparse as _urlparse

import requests

from config import load_config

from utils.logger import get_logger
from utils.models import PublishResult, BufferPostResult
from utils.parser import parse_request
from services.gemini import GeminiService
from services.wordpress import WordPressService
from services.googledrive import GoogleDriveService, DriveArticle

from services.buffer import BufferClient
from utils.models import ParsedRequest
from services.buffer.social_formatter import build_all as build_social_texts

from utils.selection_cache import (
    PendingSelection, save_pending, load_any_pending,
    delete_pending, delete_all_pending, purge_expired,
)
from utils import buffer_schedule_cache as bsc

logger = get_logger("blogger")

# ── Constants ────────────────────────────────────────────────────────────────

_BUFFER_PLATFORMS = {
    "facebook", "instagram", "tiktok", "threads",
    "twitter", "linkedin", "youtube", "bluesky",
    "pinterest", "mastodon", "google_business",
}
_SERVICE_TO_ATTR = {
    "facebook":       "facebook",
    "instagram":      "instagram",
    "tiktok":         "tiktok",
    "threads":        "threads",
    "twitter":        "twitter",
    "x":              "twitter",
    "linkedin":       "linkedin",
    "youtube":        "youtube",
    "bluesky":        "bluesky",
    "pinterest":      "pinterest",
    "mastodon":       "mastodon",
    "google":         "google_business",
    "google_business":"google_business",
    "googlebusiness": "google_business",
}
_CANCEL_KEYWORDS = {"huỷ", "huy", "cancel", "thôi", "bỏ"}

# ── Helpers ──────────────────────────────────────────────────────────────────

def _save_to_file(content: str, output_dir: str, topic: str, platform: str, schedule: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"travel_blog_{int(time.time())}.md")
    header = f"---\nTopic: {topic}\nSchedule: {schedule}\nPlatform: {platform}\n---\n\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + content)
    return path


def _notify_webhook(webhook_url: str, title: str, file_path: str) -> None:
    try:
        requests.post(
            webhook_url,
            json={"text": f"✅ Bài '{title}' xong!\nFile: {file_path}"},
            timeout=5,
        )
    except Exception as e:
        logger.warning("Webhook error: %s", e)


def _is_selection_reply(prompt: str) -> bool:
    return prompt.strip().isdigit()


def _is_cancel(prompt: str) -> bool:
    return prompt.strip().lower() in _CANCEL_KEYWORDS


# ── Publish workers (chạy song song) ────────────────────────────────────────

def _worker_wordpress(
    wp: WordPressService,
    article_data: dict,
    parsed: ParsedRequest,
) -> dict | None:
    """
    Worker chạy trong thread riêng: đăng WordPress.
    Trả về response dict nếu thành công, None nếu thất bại.
    Ảnh trong HTML được upload lên WP Media để WP tự host.
    """
    return wp.publish(
        article_data   = article_data,
        image_files    = [],             # không có file local — WP tự detect img trong HTML
        schedule_time  = parsed.schedule_time,
        category_names = ["Du Lịch"],
        tag_names      = [parsed.topic.title()],
    )


def _worker_buffer(
    cfg,
    social_texts: dict,
    drive_image_urls: list[str],
    scheduled_at: str | None,
    buffer_platforms: list[str],         # [] = tất cả channels đã đăng ký
    article_title: str = "",
) -> list[BufferPostResult]:
    """
    Worker chạy trong thread riêng: đăng tất cả Buffer channels.
    Ảnh lấy trực tiếp từ Drive API URL — không qua WP Media.
    """
    results: list[BufferPostResult] = []
    try:
        buffer  = BufferClient(api_key=cfg.buffer.api_key)
        targets = buffer.get_channels_from_env(buffer_platforms or None)

        if not targets:
            logger.warning("  → [Buffer] Không tìm thấy channel nào trong .env")
            return results

        for ch in targets:
            service      = (ch.get("service") or "").lower()
            post_opts    = social_texts.get(service) or social_texts.get("facebook", {})
            attr         = _SERVICE_TO_ATTR.get(service)
            platform_obj = getattr(buffer, attr, None) if attr else None

            if not platform_obj:
                logger.warning("  → [Buffer] '%s' chưa hỗ trợ, bỏ qua", service)
                continue

            # ── Guard: Buffer free plan giới hạn 10 scheduled posts/account ──
            api_key_for_ch = (
                buffer._channel_api_key_map.get(ch["id"]) or cfg.buffer.api_key
            )
            if scheduled_at and bsc.is_full(api_key_for_ch):
                tail    = api_key_for_ch[-4:]
                pending = bsc.list_active(api_key_for_ch)

                # Format list scheduled posts
                lines = [f"  {i+1}. [{r['platform'].upper()}] {r['title']}\n     ⏰ {r['scheduled_at'].replace('T', ' ').replace('Z', ' UTC')}"
                        for i, r in enumerate(pending)]
                
                msg = (
                    f"⚠️ Buffer account ...{tail} đã đạt giới hạn 10 scheduled posts.\n\n"
                    f"📋 Danh sách bài đang chờ đăng:\n"
                    + "\n".join(lines) +
                    f"\n\nChờ bài cũ được đăng xong rồi thử lại."
                )
                logger.warning("  → [Buffer] ❌ BUFFER_SCHEDULE_LIMIT:\n%s", msg)
                results.append(BufferPostResult(
                    platform     = service,
                    channel_name = ch.get("name", ""),
                    channel_id   = ch["id"],
                    status       = "error",
                    error        = msg,
                ))
                continue


            try:
                post = platform_obj.create_post(
                    ch["id"],
                    text         = post_opts.get("text", ""),
                    image_urls   = drive_image_urls or None,
                    scheduled_at = scheduled_at,
                )
                results.append(BufferPostResult(
                    platform     = service,
                    channel_name = ch.get("name", ""),
                    channel_id   = ch["id"],
                    status       = "success",
                    post_id      = post.get("id", ""),
                ))
                logger.info("  → [Buffer] ✅ [%s] %s", service.upper(), ch.get("name"))
            except Exception as e:
                results.append(BufferPostResult(
                    platform     = service,
                    channel_name = ch.get("name", ""),
                    channel_id   = ch["id"],
                    status       = "error",
                    error        = str(e),
                ))
                logger.warning("  → [Buffer] ❌ [%s] %s — %s", service.upper(), ch.get("name"), e)

    except Exception as e:
        logger.warning("  → [Buffer] Lỗi khởi tạo: %s", e)

    return results


# ── Main workflow ────────────────────────────────────────────────────────────

def run(user_prompt: str, webhook_url: str | None = None) -> PublishResult:
    cfg = load_config()

    gemini        = GeminiService(cfg.gemini, ollama_config=cfg.ollama)
    wp            = WordPressService(cfg.wordpress)
    drive_service = GoogleDriveService.from_config(cfg.googledrive)


    purge_expired()
    bsc.purge_expired()

    if _is_cancel(user_prompt):
        delete_all_pending()
        print("❌ Đã huỷ yêu cầu chọn bài.")
        return PublishResult(file_path="")

    # ── LƯỢT 2: User reply số thứ tự ────────────────────────────────────────
    if _is_selection_reply(user_prompt):
        choice  = int(user_prompt.strip())
        pending = load_any_pending(cfg.chat_id)

        if not pending:
            print(
                "⚠️  Không tìm thấy phiên chọn bài nào còn hạn (TTL 24h).\n"
                "Vui lòng gõ lại yêu cầu đăng bài từ đầu."
            )
            return PublishResult(file_path="", error="NO_PENDING_SELECTION")

        if choice < 1 or choice > len(pending.candidates):
            print(f"⚠️  Số không hợp lệ. Vui lòng chọn từ 1 đến {len(pending.candidates)}.")
            return PublishResult(file_path="", error="INVALID_CHOICE")

        selected    = pending.candidates[choice - 1]
        document_id = selected["document_id"]
        parsed      = ParsedRequest(
            topic         = pending.topic,
            platforms     = pending.platform,
            schedule_time = pending.schedule,
        )

        print(f"✅ Đã chọn: {selected['title']}")
        delete_pending(cfg.chat_id, pending.topic)

        logger.info("[2/6] Fetch doc đã chọn: %s", document_id)
        try:
            drive_article = drive_service.fetch_article_by_id(
                document_id, cfg.googledrive.language
            )
        except RuntimeError as exc:
            return PublishResult(file_path="", error=f"❌ Fetch thất bại: {exc}")

        if not drive_article:
            return PublishResult(file_path="", error=f"❌ Doc {document_id} không tồn tại.")

        return _continue_publish(cfg, gemini, wp, drive_article, parsed, webhook_url)

    # ── LƯỢT 1: Parse prompt mới ─────────────────────────────────────────────
    parsed = parse_request(user_prompt)

    if not cfg.googledrive.is_valid:
        return PublishResult(file_path="", error="❌ GDRIVE_API_URL chưa cấu hình.")

    logger.info("[2/6] List tài liệu Google Drive...")
    try:
        candidates = drive_service.list_articles(
            topic    = parsed.topic,
            language = cfg.googledrive.language,
        )
    except RuntimeError as exc:
        return PublishResult(file_path="", error=f"❌ Lỗi Drive API: {exc}")

    if not candidates:
        msg = f"❌ Không tìm thấy tài liệu nào cho topic: \"{parsed.topic}\""
        print(msg)
        return PublishResult(file_path="", error=msg)

    if len(candidates) == 1:
        logger.info("  → 1 kết quả, tự động chọn: %s", candidates[0]["title"])
        try:
            drive_article = drive_service.fetch_article_by_id(
                candidates[0]["document_id"], cfg.googledrive.language
            )
        except RuntimeError as exc:
            return PublishResult(file_path="", error=f"❌ Fetch thất bại: {exc}")

        return _continue_publish(cfg, gemini, wp, drive_article, parsed, webhook_url)

    # 2+ kết quả → lưu cache, in list, dừng chờ
    save_pending(cfg.chat_id, parsed.topic, PendingSelection(
        candidates = candidates,
        platform   = parsed.platforms,
        schedule   = parsed.schedule_time,
        topic      = parsed.topic,
    ))

    lines = [f"Tìm thấy {len(candidates)} tài liệu về '{parsed.topic}':"]
    for i, doc in enumerate(candidates):
        date  = doc.get("modified_date", "")[:10]
        title = doc.get("title", f"Tài liệu {i+1}")
        lines.append(f"  {i+1}. {title} (sửa: {date})")
    lines.append("→ Trả lời số thứ tự để chọn bài muốn đăng.")
    lines.append("  (Gõ 'huỷ' để bỏ qua)")

    print("\n".join(lines))
    return PublishResult(file_path="", error="PENDING_SELECTION")


# ── Core publish pipeline ────────────────────────────────────────────────────

def _continue_publish(
    cfg,
    gemini: GeminiService,
    wp: WordPressService,
    drive_article: DriveArticle,
    parsed: ParsedRequest,
    webhook_url: str | None,
) -> PublishResult:
    """
    Bước 3→6: xử lý nội dung, lưu file, đăng bài song song.

    Platform logic:
      "blog"  (default, không đề cập) → WP + TẤT CẢ Buffer channels
      "wordpress"                      → chỉ WP
      Tên platform cụ thể              → chỉ Buffer platform đó
    """
    platforms        = [p.lower() for p in parsed.platforms]

    # Quyết định publish đâu
    # "blog" = không đề cập platform → publish ALL
    publish_all      = platforms == ["blog"]
    should_wp        = publish_all or "wordpress" in platforms
    buffer_list      = [p for p in platforms if p in _BUFFER_PLATFORMS]
    should_buffer    = cfg.buffer.is_valid and (publish_all or bool(buffer_list))
    # Danh sách platform filter cho Buffer ([] = tất cả channels đã đăng ký)
    buffer_platforms = [] if publish_all else buffer_list

    # ── Bước 3: Thu thập Drive image URLs (cho Buffer) ───────────────────────
    # Buffer dùng URL thẳng từ Drive API, KHÔNG upload lên WP Media
    logger.info("[3/6] Lấy ảnh từ Google Docs (%d ảnh)", drive_article.image_count())
    _api_parsed  = _urlparse(cfg.googledrive.api_url)
    _actual_base = f"{_api_parsed.scheme}://{_api_parsed.netloc}"

    drive_image_urls: list[str] = []
    for block in drive_article.content_blocks:
        if block.get("type") == "image" and block.get("url"):
            url = _img_re.sub(r'https?://localhost(:\d+)?', _actual_base, block["url"])
            drive_image_urls.append(url)

    # ── Bước 4: Gemini → social captions ────────────────────────────────────
    plain = drive_article.plain_text()

    social_texts: dict = {}
    if should_buffer:
        logger.info("[4/6] Gemini tạo social captions...")
        social_captions = gemini.generate_social_captions(
            topic      = parsed.topic,
            title      = drive_article.title,
            plain_text = plain,
            platforms = buffer_platforms or None,  # None = gen cho tất cả platforms, [] = gen cho 0 platform → không gen gì
        )
        logger.info("  → %d platforms", len(social_captions))
        social_texts = build_social_texts(
            topic           = parsed.topic,
            title           = drive_article.title,
            excerpt         = plain[:300],
            wp_url          = "",          # WP URL chưa có — điền sau khi WP xong
            social_captions = social_captions,
        )
    else:
        logger.info("[4/6] Bỏ qua Gemini (không đăng social)")

    # ── Bước 5: Lưu file backup ──────────────────────────────────────────────
    platform_label = "all" if publish_all else ", ".join(platforms)
    file_path = _save_to_file(
        content    = drive_article.content,
        output_dir = cfg.output_dir,
        topic      = parsed.topic,
        platform   = platform_label,
        schedule   = parsed.schedule_time,
    )
    logger.info("[5/6] 💾 Lưu file: %s", file_path)

    result = PublishResult(file_path=file_path)

    # ── Bước 6: Publish song song WP + Buffer ───────────────────────────────
    logger.info("[6/6] Xuất bản song song (WP=%s | Buffer=%s)...", should_wp, should_buffer)

    article_data = {
        "seo_title":       drive_article.title,
        "meta_description": plain[:160],
        "focus_keyword":   parsed.topic,
        "excerpt":         plain[:300],
        "content_html":    drive_article.content,
        "social_captions": {},
    }
    title = drive_article.title

    wp_future     = None
    buffer_future = None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        if should_wp and cfg.wordpress.is_valid:
            wp_future = executor.submit(
                _worker_wordpress, wp, article_data, parsed
            )

        if should_buffer:
            buffer_future = executor.submit(
                _worker_buffer,
                cfg,
                social_texts,
                drive_image_urls,
                parsed.schedule_time or None,
                buffer_platforms,
                drive_article.title,
            )

        # ── Thu kết quả WP ───────────────────────────────────────────────────
        if wp_future is not None:
            try:
                wp_resp = wp_future.result()
                if wp_resp:
                    result.wp_post_id  = wp_resp["id"]
                    result.wp_post_url = wp_resp["link"]
                    result.wp_status   = wp_resp["status"]
                    logger.info("  → ✅ WordPress: %s", result.wp_post_url)

                    # Nếu publish_all: cập nhật wp_url vào social_texts và re-post
                    # Không cần: wp_url trong caption là bonus, không ảnh hưởng chất lượng.
                    # Buffer đã chạy song song rồi — không wait để inject url.
                    # Trade-off chấp nhận được: tốc độ > có link WP trong caption.
            except Exception as exc:
                logger.error("  → ❌ WordPress thất bại: %s", exc)
                result.error = str(exc)

        # ── Thu kết quả Buffer ───────────────────────────────────────────────
        if buffer_future is not None:
            try:
                result.buffer_results = buffer_future.result()
                succeeded = sum(1 for r in result.buffer_results if r.status == "success")
                logger.info(
                    "  → Buffer: %d/%d channel thành công",
                    succeeded, len(result.buffer_results),
                )
            except Exception as exc:
                logger.warning("  → ❌ Buffer thất bại: %s", exc)

    # ── Log tổng kết ─────────────────────────────────────────────────────────
    if not should_wp and not should_buffer:
        logger.info("  → 💾 Chỉ lưu file (WP chưa cấu hình + Buffer không hợp lệ)")

    if webhook_url:
        _notify_webhook(webhook_url, title, file_path)

    return result


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    user_input  = sys.argv[1] if len(sys.argv) > 1 else "Du lịch Đà Nẵng"
    webhook_url = sys.argv[2] if len(sys.argv) > 2 else None

    result = run(user_input, webhook_url)

    non_error_states = {"PENDING_SELECTION", "NO_PENDING_SELECTION", "INVALID_CHOICE", ""}
    if result.error and result.error not in non_error_states:
        logger.error("Lỗi: %s", result.error)
        sys.exit(1)

    if result.posted_to_wp:
        logger.info("WP URL: %s", result.wp_post_url)
    sys.exit(0)


if __name__ == "__main__":
    main()