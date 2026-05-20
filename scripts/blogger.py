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

from config import FacebookConfig, load_config

from services.facebook import FacebookService
from utils.logger import get_logger
from utils.models import FacebookPostResult, PublishResult, BufferPostResult
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
    PendingPageSelection, save_pending_pages,
    load_pending_pages, delete_pending_pages,
    PendingWPSiteSelection, save_pending_wp_sites,
    load_pending_wp_sites, delete_pending_wp_sites,
)
from utils import buffer_schedule_cache as bsc

logger = get_logger("blogger")

# ── Constants ────────────────────────────────────────────────────────────────

_BUFFER_PLATFORMS = {
    "instagram", "tiktok", "threads",
    "twitter", "linkedin", "youtube", "bluesky",
    "pinterest", "mastodon", "google_business",
}
_SERVICE_TO_ATTR = {
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

_ALL_PAGES_KEYWORDS = {"tất cả", "tat ca", "tatca", "all"}
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

def _is_page_selection(prompt: str) -> bool:
    """
    Nhận diện reply chọn page: "1", "1 3", "tất cả", "all", v.v.
    Phân biệt với _is_selection_reply() (chỉ nhận 1 số).
    """
    p = prompt.strip().lower()
    if p in _ALL_PAGES_KEYWORDS:
        return True
    # "1", "2", "1 3", "1 2 3" — nhiều số cách nhau khoảng trắng
    return bool(p) and all(x.isdigit() for x in p.split())

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
        buffer = BufferClient(api_key=cfg.buffer.api_key)
        return buffer.post_all_channels(
            social_texts = social_texts,
            image_urls   = drive_image_urls,
            scheduled_at = scheduled_at,
            platforms    = buffer_platforms or None,
        )
    except Exception as e:
        logger.warning("  → [Buffer] Lỗi khởi tạo: %s", e)

    return results

def _worker_facebook(
    cfg_facebook: FacebookConfig,
    text: str,
    drive_image_urls: list[str],
    video_url: str | None,
    scheduled_at: str | None,
    selected_page_ids: list[str] | None = None,
    page_texts: dict[str, str] | None = None,
) -> list[FacebookPostResult]:
    """Worker chạy trong thread riêng: đăng tất cả Facebook Pages qua Meta API."""
    fb = FacebookService(cfg_facebook)
    if selected_page_ids is not None:
        # Đăng đúng pages đã chọn
        return fb.post_to_selected_pages(
            page_ids     = selected_page_ids,
            text         = text,
            image_urls   = drive_image_urls or None,
            video_url    = video_url,
            scheduled_at = scheduled_at,
            page_texts=page_texts, 
        )
    else:
        # 1 page hoặc đăng tất cả (selected_page_ids=None chỉ đến đây khi len==1)
        return fb.post_to_all_pages(
            text         = text,
            image_urls   = drive_image_urls or None,
            video_url    = video_url,
            scheduled_at = scheduled_at,
            page_texts   = page_texts, 
        )
    

# ── Main workflow ────────────────────────────────────────────────────────────

def run(user_prompt: str, webhook_url: str | None = None) -> PublishResult:
    cfg = load_config()

    gemini        = GeminiService(cfg.gemini, ollama_config=cfg.ollama)
    drive_service = GoogleDriveService.from_config(cfg.googledrive)


    purge_expired()
    bsc.purge_expired()

    if _is_cancel(user_prompt):
        delete_all_pending()
        print("❌ Đã huỷ yêu cầu chọn bài.")
        return PublishResult(file_path="")

# ── LƯỢT 2b: User reply chọn page ───────────────────────────────────────────
    if _is_page_selection(user_prompt):
        pending_pages = load_pending_pages(cfg.chat_id)

        if pending_pages:
            p = user_prompt.strip().lower()

            if p in _ALL_PAGES_KEYWORDS:
                selected_ids = [pg["id"] for pg in pending_pages.pages]
                selected_names = [pg["name"] for pg in pending_pages.pages]
            else:
                raw_indices = [int(x) - 1 for x in p.split() if x.isdigit()]
                selected = [
                    pending_pages.pages[i]
                    for i in raw_indices
                    if 0 <= i < len(pending_pages.pages)
                ]
                if not selected:
                    print(f"⚠️  Số không hợp lệ. Vui lòng chọn từ 1 đến {len(pending_pages.pages)}.")
                    return PublishResult(file_path="", error="INVALID_PAGE_CHOICE")

                selected_ids   = [pg["id"]   for pg in selected]
                selected_names = [pg["name"] for pg in selected]

            print(f"✅ Đã chọn {len(selected_ids)} page: {', '.join(selected_names)}")
            delete_pending_pages(cfg.chat_id)

            # Fetch lại article theo document_id đã cache
            logger.info("[2/6] Fetch lại doc: %s", pending_pages.article_id)
            try:
                drive_article = drive_service.fetch_article_by_id(
                    pending_pages.article_id, cfg.googledrive.language
                )
            except RuntimeError as exc:
                return PublishResult(file_path="", error=f"❌ Fetch thất bại: {exc}")

            if not drive_article:
                return PublishResult(file_path="", error="❌ Doc không còn tồn tại.")

            # Tái sử dụng _continue_publish với override selected_page_ids
            parsed = ParsedRequest(
                topic         = pending_pages.topic,
                platforms     = pending_pages.platforms, 
                schedule_time = pending_pages.schedule,
            )
            return _continue_publish(
                cfg, gemini, drive_article, parsed, webhook_url,
                selected_page_ids=selected_ids,
                selected_wp_site_urls = pending_pages.selected_wp_site_urls,
            )

        pending_wp = load_pending_wp_sites(cfg.chat_id)
        if pending_wp:
            p = user_prompt.strip().lower()
            if p in _ALL_PAGES_KEYWORDS:
                selected_urls = [s["url"] for s in pending_wp.sites]
            else:
                raw_indices = [int(x) - 1 for x in p.split() if x.isdigit()]
                selected = [pending_wp.sites[i] for i in raw_indices
                            if 0 <= i < len(pending_wp.sites)]
                if not selected:
                    print(f"⚠️  Số không hợp lệ. Vui lòng chọn từ 1 đến {len(pending_wp.sites)}.")
                    return PublishResult(file_path="", error="INVALID_WP_SITE_CHOICE")
                selected_urls = [s["url"] for s in selected]

            print(f"✅ Đã chọn {len(selected_urls)} site: {', '.join(selected_urls)}")
            delete_pending_wp_sites(cfg.chat_id)

            try:
                drive_article = drive_service.fetch_article_by_id(
                    pending_wp.article_id, cfg.googledrive.language
                )
            except RuntimeError as exc:
                return PublishResult(file_path="", error=f"❌ Fetch thất bại: {exc}")
            if not drive_article:
                return PublishResult(file_path="", error="❌ Doc không còn tồn tại.")

            parsed = ParsedRequest(
                topic=pending_wp.topic,
                platforms=pending_wp.platforms,
                schedule_time=pending_wp.schedule,
            )
            return _continue_publish(
                cfg, gemini, drive_article, parsed, webhook_url,
                selected_wp_site_urls=selected_urls,
                selected_page_ids=pending_wp.selected_page_ids,
            )
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

        return _continue_publish(cfg, gemini, drive_article, parsed, webhook_url)

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

        return _continue_publish(cfg, gemini, drive_article, parsed, webhook_url)

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
    drive_article: DriveArticle,
    parsed: ParsedRequest,
    webhook_url: str | None,
    selected_page_ids: list[str] | None = None,
    selected_wp_site_urls: list[str] | None = None,  
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

    should_facebook = cfg.facebook.is_valid and (publish_all or "facebook" in platforms)
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

    # ── Guard: 2+ pages và chưa có lựa chọn → hỏi user ─────────────────
    if should_facebook and selected_page_ids is None and len(cfg.facebook.pages) > 1:
        save_pending_pages(cfg.chat_id, PendingPageSelection(
            pages         = cfg.facebook.pages,
            topic         = parsed.topic,
            platforms     = parsed.platforms,
            schedule      = parsed.schedule_time,
            article_id    = drive_article.document_id,
            article_title = drive_article.title,
            selected_wp_site_urls = selected_wp_site_urls,
        ))
        lines = [f"📄 Tìm thấy {len(cfg.facebook.pages)} Facebook Pages. Chọn page muốn đăng:"]
        for i, pg in enumerate(cfg.facebook.pages):
            lines.append(f"  {i+1}. {pg['name']}")
        lines.append("→ Nhập số thứ tự (vd: '1 3'), hoặc 'tất cả' để đăng hết.")
        lines.append("  (Gõ 'huỷ' để bỏ qua)")
        print("\n".join(lines))
        return PublishResult(file_path="", error="PENDING_PAGE_SELECTION")
    
    valid_wp_sites = [s for s in cfg.wordpress_sites if s.is_valid]
    if should_wp and selected_wp_site_urls is None and len(valid_wp_sites) > 1:
        save_pending_wp_sites(cfg.chat_id, PendingWPSiteSelection(
            sites         = [{"url": s.site_url} for s in valid_wp_sites],
            topic         = parsed.topic,
            platforms     = parsed.platforms,
            schedule      = parsed.schedule_time,
            article_id    = drive_article.document_id,
            article_title = drive_article.title,
            selected_page_ids = selected_page_ids,
        ))
        lines = [f"🌐 Tìm thấy {len(valid_wp_sites)} WordPress sites. Chọn site muốn đăng:"]
        for i, s in enumerate(valid_wp_sites):
            lines.append(f"  {i+1}. {s.site_url}")
        lines.append("→ Nhập số thứ tự (vd: '1 3'), hoặc 'tất cả' để đăng hết.")
        lines.append("  (Gõ 'huỷ' để bỏ qua)")
        print("\n".join(lines))
        return PublishResult(file_path="", error="PENDING_WP_SITE_SELECTION")
    
    # ── Bước 4: Gemini → social captions ────────────────────────────────────
    plain = drive_article.plain_text()

    pages_to_post: list[dict] = []
    if should_facebook:
        if selected_page_ids is not None:
            pages_to_post = [p for p in cfg.facebook.pages if p.get("id") in selected_page_ids]
        else:
            pages_to_post = cfg.facebook.pages
            
    social_texts: dict = {}
    if should_buffer or should_facebook:
        logger.info("[4/6] Gemini tạo social captions...")
        
        gemini_platforms: list[str] | None = None
        if not publish_all:
            gemini_platforms = list(buffer_platforms)  # copy
            if should_facebook:
                gemini_platforms.append("facebook")
            if not gemini_platforms:
                gemini_platforms = None  # thực sự không cần gen gì
                
        social_captions = gemini.generate_social_captions(
            topic      = parsed.topic,
            title      = drive_article.title,
            plain_text = plain,
            platforms = gemini_platforms,
            facebook_pages = pages_to_post if len(pages_to_post) > 1 else None,
        )
        logger.info("  → %d platforms", len(social_captions))
        social_texts = build_social_texts(
            topic           = parsed.topic,
            title           = drive_article.title,
            excerpt         = plain[:300],
            wp_url          = "",          # WP URL chưa có — điền sau khi WP xong
            social_captions = social_captions,
        )
        
        fb_value = social_captions.get("facebook")
        facebook_page_texts: dict[str, str] = {}
        if isinstance(fb_value, list) and len(pages_to_post) > 1:
            facebook_page_texts = {
                p["id"]: v for p, v in zip(pages_to_post, fb_value)
            }
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

    wp_futures: list[tuple] = []
    buffer_future = None
    
    max_workers = len(cfg.wordpress_sites) + 2
    wp_sites_to_publish = (
        [s for s in cfg.wordpress_sites if s.site_url in selected_wp_site_urls]
        if selected_wp_site_urls is not None
        else cfg.wordpress_sites
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        if should_wp:
            for site_cfg in wp_sites_to_publish:
                if site_cfg.is_valid:
                    f = executor.submit(
                        _worker_wordpress, WordPressService(site_cfg), article_data, parsed
                    )
                    wp_futures.append((f, site_cfg.site_url))

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
        facebook_future = None
        if should_facebook:
            fb_text = social_texts.get("facebook", {}).get("text", drive_article.title)
            facebook_future = executor.submit(
                _worker_facebook,
                cfg.facebook,
                fb_text,
                drive_image_urls,
                None,                       # video_url — mở rộng sau nếu cần
                parsed.schedule_time or None,
                selected_page_ids,
                facebook_page_texts or None,
            )

        # ── Thu kết quả WP ───────────────────────────────────────────────────
        for wp_f, site_url in wp_futures:
            try:
                wp_resp = wp_f.result()
                if wp_resp:
                    logger.info("  → ✅ WordPress [%s]: %s", site_url, wp_resp["link"])
                    if result.wp_post_id is None:   # lấy site đầu tiên thành công làm primary
                        result.wp_post_id  = wp_resp["id"]
                        result.wp_post_url = wp_resp["link"]
                        result.wp_status   = wp_resp["status"]
            except Exception as exc:
                logger.error("  → ❌ WordPress [%s] thất bại: %s", site_url, exc)
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
                
        # ── THÊM: Thu kết quả Facebook ────────────────────────────────────────
        if facebook_future is not None:
            try:
                result.facebook_results = facebook_future.result()
                succeeded = sum(1 for r in result.facebook_results if r.status == "success")
                logger.info(
                    "  → Facebook: %d/%d page thành công",
                    succeeded, len(result.facebook_results),
                )
            except Exception as exc:
                logger.warning("  → ❌ Facebook thất bại: %s", exc)

    # ── Log tổng kết ─────────────────────────────────────────────────────────
    if not should_wp and not should_buffer and not should_facebook:
        logger.info("  → 💾 Chỉ lưu file (WP chưa cấu hình + Buffer không hợp lệ + Facebook không hợp lệ)")

    if webhook_url:
        _notify_webhook(webhook_url, title, file_path)

    return result


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    user_input  = sys.argv[1] if len(sys.argv) > 1 else "Du lịch Đà Nẵng"
    webhook_url = sys.argv[2] if len(sys.argv) > 2 else None

    result = run(user_input, webhook_url)

    non_error_states = {
        "PENDING_SELECTION",
        "PENDING_PAGE_SELECTION",
        "PENDING_WP_SITE_SELECTION",
        "NO_PENDING_SELECTION",
        "INVALID_CHOICE",
        "INVALID_PAGE_CHOICE",
        "INVALID_WP_SITE_CHOICE", 
        "",
    }
    if result.error and result.error not in non_error_states:
        logger.error("Lỗi: %s", result.error)
        sys.exit(1)

    if result.posted_to_wp:
        logger.info("WP URL: %s", result.wp_post_url)
    sys.exit(0)


if __name__ == "__main__":
    main()