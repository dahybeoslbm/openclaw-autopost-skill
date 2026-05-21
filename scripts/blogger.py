"""
blogger.py — Entry point. Chỉ chứa orchestration (điều phối luồng).
Không chứa business logic — mọi thứ đã được tách vào services/.

Luồng nghiệp vụ:
  1. Parse prompt → topic, platform, schedule
  2. Tìm Google Doc tên = topic, lấy file mới nhất (api.drive.article)
  3. Nếu có doc → Gemini viết lại (nếu nhiều WP sites) + gen social captions
  4. Đăng WordPress (draft nếu nhiều sites) và/hoặc Buffer/Facebook
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
    PendingWPRewriteMode, save_pending_wp_rewrite_mode,
    load_pending_wp_rewrite_mode, delete_pending_wp_rewrite_mode,
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
    "instagram":       "instagram",
    "tiktok":          "tiktok",
    "threads":         "threads",
    "twitter":         "twitter",
    "x":               "twitter",
    "linkedin":        "linkedin",
    "youtube":         "youtube",
    "bluesky":         "bluesky",
    "pinterest":       "pinterest",
    "mastodon":        "mastodon",
    "google":          "google_business",
    "google_business": "google_business",
    "googlebusiness":  "google_business",
}
_CANCEL_KEYWORDS    = {"huỷ", "huy", "cancel", "thôi", "bỏ"}
_ALL_PAGES_KEYWORDS = {"tất cả", "tat ca", "tatca", "all"}
_BACKUP_TTL_SECONDS = 7 * 86400  # 7 ngày

# Các error code không phải lỗi thật — không exit(1)
_NON_ERROR_STATES = {
    "PENDING_SELECTION",
    "PENDING_PAGE_SELECTION",
    "PENDING_WP_SITE_SELECTION",
    "PENDING_WP_REWRITE_MODE",
    "NO_PENDING_SELECTION",
    "INVALID_CHOICE",
    "INVALID_PAGE_CHOICE",
    "INVALID_WP_SITE_CHOICE",
    "",
}

# ── File helpers ─────────────────────────────────────────────────────────────

def _cleanup_old_backups(output_dir: str) -> None:
    cutoff = time.time() - _BACKUP_TTL_SECONDS
    for fname in os.listdir(output_dir):
        if not (fname.startswith("travel_blog_") and fname.endswith(".md")):
            continue
        fpath = os.path.join(output_dir, fname)
        try:
            if os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                logger.info("  → 🗑️  Xoá backup hết TTL: %s", fname)
        except OSError as e:
            logger.warning("  → Không xoá được %s: %s", fname, e)


def _save_to_file(content: str, output_dir: str, topic: str, platform: str, schedule: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"travel_blog_{int(time.time())}.md")
    header = f"---\nTopic: {topic}\nSchedule: {schedule}\nPlatform: {platform}\n---\n\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + content)
    _cleanup_old_backups(output_dir)
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

# ── Input classifiers ─────────────────────────────────────────────────────────

def _is_cancel(prompt: str) -> bool:
    return prompt.strip().lower() in _CANCEL_KEYWORDS


def _is_selection_reply(prompt: str) -> bool:
    """Chỉ 1 chữ số → chọn bài từ danh sách."""
    return prompt.strip().isdigit()


def _is_page_selection(prompt: str) -> bool:
    """
    Nhận diện reply chọn page/site: "1", "1 3", "tất cả", "all", v.v.
    Khác _is_selection_reply ở chỗ chấp nhận nhiều số.
    """
    p = prompt.strip().lower()
    if p in _ALL_PAGES_KEYWORDS:
        return True
    return bool(p) and all(x.isdigit() for x in p.split())


def _is_rewrite_mode_reply(prompt: str) -> bool:
    """Nhận diện user chọn rewrite mode: chỉ chấp nhận '1' hoặc '2'."""
    return prompt.strip() in {"1", "2"}

# ── Publish workers ──────────────────────────────────────────────────────────

def _worker_wordpress(
    wp: WordPressService,
    article_data: dict,
    parsed: ParsedRequest,
    save_as_draft: bool = False,
) -> dict | None:
    return wp.publish(
        article_data   = article_data,
        image_files    = [],
        schedule_time  = parsed.schedule_time,
        save_as_draft  = save_as_draft,
        category_names = ["Du Lịch"],
        tag_names      = [parsed.topic.title()],
    )


def _worker_buffer(
    cfg,
    social_texts: dict,
    drive_image_urls: list[str],
    scheduled_at: str | None,
    buffer_platforms: list[str],
    article_title: str = "",
) -> list[BufferPostResult]:
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
    return []


def _worker_facebook(
    cfg_facebook: FacebookConfig,
    text: str,
    drive_image_urls: list[str],
    video_url: str | None,
    scheduled_at: str | None,
    selected_page_ids: list[str] | None = None,
    page_texts: dict[str, str] | None = None,
) -> list[FacebookPostResult]:
    fb = FacebookService(cfg_facebook)
    if selected_page_ids is not None:
        return fb.post_to_selected_pages(
            page_ids     = selected_page_ids,
            text         = text,
            image_urls   = drive_image_urls or None,
            video_url    = video_url,
            scheduled_at = scheduled_at,
            page_texts   = page_texts,
        )
    return fb.post_to_all_pages(
        text         = text,
        image_urls   = drive_image_urls or None,
        video_url    = video_url,
        scheduled_at = scheduled_at,
        page_texts   = page_texts,
    )

# ── Drive article loader helper ───────────────────────────────────────────────

def _load_drive_article(
    drive_service: GoogleDriveService,
    article_id: str,
    language: str,
    article_data: dict | None,
) -> DriveArticle | PublishResult:
    """
    Trả về DriveArticle từ cache dict (nếu có) hoặc fetch lại từ Drive.
    Nếu thất bại trả về PublishResult chứa error.
    """
    if article_data:
        return DriveArticle(**article_data)
    try:
        article = drive_service.fetch_article_by_id(article_id, language)
    except RuntimeError as exc:
        return PublishResult(file_path="", error=f"❌ Fetch thất bại: {exc}")
    if not article:
        return PublishResult(file_path="", error="❌ Doc không còn tồn tại.")
    return article

# ── Main run loop ─────────────────────────────────────────────────────────────

def run(user_prompt: str, webhook_url: str | None = None) -> PublishResult:
    cfg           = load_config()
    gemini        = GeminiService(cfg.gemini, ollama_config=cfg.ollama)
    drive_service = GoogleDriveService.from_config(cfg.googledrive)

    purge_expired()
    bsc.purge_expired()
    _cleanup_old_backups(cfg.output_dir)

    # ── Huỷ ──────────────────────────────────────────────────────────────────
    if _is_cancel(user_prompt):
        delete_all_pending()
        print("❌ Đã huỷ yêu cầu chọn bài.")
        return PublishResult(file_path="")

    # ── LƯỢT 2c: User reply chọn rewrite mode ────────────────────────────────
    # Phải check TRƯỚC _is_page_selection vì "1" / "2" cũng match page selection
    if _is_rewrite_mode_reply(user_prompt):
        pending_rewrite = load_pending_wp_rewrite_mode(cfg.chat_id)
        if pending_rewrite:
            rewrite_mode = (user_prompt.strip() == "1")
            delete_pending_wp_rewrite_mode(cfg.chat_id)
            print(
                "✅ Sẽ viết lại nội dung riêng cho từng site."
                if rewrite_mode else
                "⚠️  Dùng cùng nội dung gốc cho tất cả sites."
            )

            result = _load_drive_article(
                drive_service, pending_rewrite.article_id,
                cfg.googledrive.language, pending_rewrite.article_data,
            )
            if isinstance(result, PublishResult):
                return result
            drive_article = result

            parsed = ParsedRequest(
                topic         = pending_rewrite.topic,
                platforms     = pending_rewrite.platforms,
                schedule_time = pending_rewrite.schedule,
            )
            return _continue_publish(
                cfg, gemini, drive_article, parsed, webhook_url,
                selected_page_ids     = pending_rewrite.selected_page_ids,
                selected_wp_site_urls = pending_rewrite.selected_wp_site_urls,
                rewrite_mode          = rewrite_mode,
            )

    # ── LƯỢT 2b: User reply chọn page / WP site ──────────────────────────────
    if _is_page_selection(user_prompt):

        # -- Facebook pages --
        pending_pages = load_pending_pages(cfg.chat_id)
        if pending_pages:
            p = user_prompt.strip().lower()
            if p in _ALL_PAGES_KEYWORDS:
                selected_ids = [pg["id"] for pg in pending_pages.pages]
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
                selected_ids = [pg["id"] for pg in selected]

            print(f"✅ Đã chọn {len(selected_ids)} page: {', '.join(pg['name'] for pg in pending_pages.pages if pg['id'] in selected_ids)}")
            delete_pending_pages(cfg.chat_id)

            result = _load_drive_article(
                drive_service, pending_pages.article_id,
                cfg.googledrive.language, pending_pages.article_data,
            )
            if isinstance(result, PublishResult):
                return result

            parsed = ParsedRequest(
                topic         = pending_pages.topic,
                platforms     = pending_pages.platforms,
                schedule_time = pending_pages.schedule,
            )
            return _continue_publish(
                cfg, gemini, result, parsed, webhook_url,
                selected_page_ids     = selected_ids,
                selected_wp_site_urls = pending_pages.selected_wp_site_urls,
            )

        # -- WordPress sites --
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

            result = _load_drive_article(
                drive_service, pending_wp.article_id,
                cfg.googledrive.language, pending_wp.article_data,
            )
            if isinstance(result, PublishResult):
                return result

            parsed = ParsedRequest(
                topic         = pending_wp.topic,
                platforms     = pending_wp.platforms,
                schedule_time = pending_wp.schedule,
            )
            return _continue_publish(
                cfg, gemini, result, parsed, webhook_url,
                selected_wp_site_urls = selected_urls,
                selected_page_ids     = pending_wp.selected_page_ids,
            )

    # ── LƯỢT 2a: User reply số thứ tự bài ────────────────────────────────────
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
        result = _load_drive_article(drive_service, document_id, cfg.googledrive.language, None)
        if isinstance(result, PublishResult):
            return result

        return _continue_publish(cfg, gemini, result, parsed, webhook_url)

    # ── LƯỢT 1: Parse prompt mới ──────────────────────────────────────────────
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
        result = _load_drive_article(
            drive_service, candidates[0]["document_id"], cfg.googledrive.language, None
        )
        if isinstance(result, PublishResult):
            return result
        return _continue_publish(cfg, gemini, result, parsed, webhook_url)

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


# ── Core publish pipeline ─────────────────────────────────────────────────────

def _continue_publish(
    cfg,
    gemini: GeminiService,
    drive_article: DriveArticle,
    parsed: ParsedRequest,
    webhook_url: str | None,
    selected_page_ids: list[str] | None = None,
    selected_wp_site_urls: list[str] | None = None,
    rewrite_mode: bool | None = None,   # None=chưa hỏi, True/False=đã chọn
) -> PublishResult:
    """
    Bước 3→6: xử lý nội dung, lưu file, đăng bài song song.

    Platform logic:
      "blog"  (default) → WP + TẤT CẢ Buffer channels
      "wordpress"       → chỉ WP
      Tên platform cụ thể → chỉ platform đó
    """
    platforms     = [p.lower() for p in parsed.platforms]
    publish_all   = platforms == ["blog"]
    should_wp     = publish_all or "wordpress" in platforms
    buffer_list   = [p for p in platforms if p in _BUFFER_PLATFORMS]
    should_buffer = cfg.buffer.is_valid and (publish_all or bool(buffer_list))
    buffer_platforms = [] if publish_all else buffer_list
    should_facebook  = cfg.facebook.is_valid and (publish_all or "facebook" in platforms)

    # ── Bước 3: Thu thập Drive image URLs (cho Buffer/Facebook) ──────────────
    logger.info("[3/6] Lấy ảnh từ Google Docs (%d ảnh)", drive_article.image_count())
    _api_parsed  = _urlparse(cfg.googledrive.api_url)
    _actual_base = f"{_api_parsed.scheme}://{_api_parsed.netloc}"

    drive_image_urls: list[str] = []
    for block in drive_article.content_blocks:
        if block.get("type") == "image" and block.get("url"):
            url = _img_re.sub(r'https?://localhost(:\d+)?', _actual_base, block["url"])
            drive_image_urls.append(url)

    # ── Guard: chọn Facebook pages ────────────────────────────────────────────
    if should_facebook and selected_page_ids is None and len(cfg.facebook.pages) > 1:
        save_pending_pages(cfg.chat_id, PendingPageSelection(
            pages                = cfg.facebook.pages,
            topic                = parsed.topic,
            platforms            = parsed.platforms,
            schedule             = parsed.schedule_time,
            article_id           = drive_article.document_id,
            article_title        = drive_article.title,
            article_data         = drive_article.to_dict(),
            selected_wp_site_urls = selected_wp_site_urls,
        ))
        lines = [f"📄 Tìm thấy {len(cfg.facebook.pages)} Facebook Pages. Chọn page muốn đăng:"]
        for i, pg in enumerate(cfg.facebook.pages):
            lines.append(f"  {i+1}. {pg['name']}")
        lines.append("→ Nhập số thứ tự (vd: '1 3'), hoặc 'tất cả' để đăng hết.")
        lines.append("  (Gõ 'huỷ' để bỏ qua)")
        print("\n".join(lines))
        return PublishResult(file_path="", error="PENDING_PAGE_SELECTION")

    # ── Guard: chọn WordPress sites ───────────────────────────────────────────
    valid_wp_sites = [s for s in cfg.wordpress_sites if s.is_valid]
    if should_wp and selected_wp_site_urls is None and len(valid_wp_sites) > 1:
        save_pending_wp_sites(cfg.chat_id, PendingWPSiteSelection(
            sites              = [{"url": s.site_url} for s in valid_wp_sites],
            topic              = parsed.topic,
            platforms          = parsed.platforms,
            schedule           = parsed.schedule_time,
            article_id         = drive_article.document_id,
            article_title      = drive_article.title,
            article_data       = drive_article.to_dict(),
            selected_page_ids  = selected_page_ids,
        ))
        lines = [f"🌐 Tìm thấy {len(valid_wp_sites)} WordPress sites. Chọn site muốn đăng:"]
        for i, s in enumerate(valid_wp_sites):
            lines.append(f"  {i+1}. {s.site_url}")
        lines.append("→ Nhập số thứ tự (vd: '1 3'), hoặc 'tất cả' để đăng hết.")
        lines.append("  (Gõ 'huỷ' để bỏ qua)")
        print("\n".join(lines))
        return PublishResult(file_path="", error="PENDING_WP_SITE_SELECTION")

    # ── Guard: hỏi rewrite mode (chỉ khi chọn 2+ WP sites) ──────────────────
    if (
        should_wp
        and selected_wp_site_urls is not None
        and len(selected_wp_site_urls) > 1
        and rewrite_mode is None
    ):
        save_pending_wp_rewrite_mode(cfg.chat_id, PendingWPRewriteMode(
            selected_wp_site_urls = selected_wp_site_urls,
            topic                 = parsed.topic,
            platforms             = parsed.platforms,
            schedule              = parsed.schedule_time,
            article_id            = drive_article.document_id,
            article_title         = drive_article.title,
            article_data          = drive_article.to_dict(),
            selected_page_ids     = selected_page_ids,
        ))
        print(
            "⚠️  Đăng cùng nội dung lên nhiều site sẽ bị Google đánh spam.\n\n"
            "📝 Chọn cách xử lý nội dung:\n"
            "  1. Viết lại riêng cho từng site (khuyến nghị ✅)\n"
            "  2. Dùng cùng nội dung gốc cho tất cả (không khuyến nghị ⚠️)\n"
            "  (Gõ 'huỷ' để bỏ qua)"
        )
        return PublishResult(file_path="", error="PENDING_WP_REWRITE_MODE")

    # ── Xác định sites thực tế sẽ publish ────────────────────────────────────
    wp_sites_to_publish = (
        [s for s in cfg.wordpress_sites if s.site_url in selected_wp_site_urls]
        if selected_wp_site_urls is not None
        else valid_wp_sites
    )
    wp_sites_to_publish = [s for s in wp_sites_to_publish if s.is_valid]

    # Draft khi: nhiều sites HOẶC có rewrite
    save_as_draft = len(wp_sites_to_publish) > 1 or bool(rewrite_mode)

    # Số bản cần viết lại (site đầu giữ bản gốc, site 2+ nhận bản rewrite)
    rewrite_count = (len(wp_sites_to_publish) - 1) if rewrite_mode else 0

    # ── Bước 4: Gemini — rewrite + captions trong 1 request ──────────────────
    plain = drive_article.plain_text()

    pages_to_post: list[dict] = []
    if should_facebook:
        pages_to_post = (
            [p for p in cfg.facebook.pages if p.get("id") in selected_page_ids]
            if selected_page_ids is not None
            else cfg.facebook.pages
        )

    # Platforms cần gen caption
    gemini_platforms: list[str] | None = None
    if not publish_all:
        gemini_platforms = list(buffer_platforms)
        if should_facebook:
            gemini_platforms.append("facebook")
        if not gemini_platforms:
            gemini_platforms = None

    need_captions = should_buffer or should_facebook
    need_rewrite  = rewrite_count > 0

    social_texts: dict                        = {}
    social_captions: dict                     = {}
    rewritten_versions: list[tuple[str, str]] = []

    if need_rewrite or need_captions:
        logger.info(
            "[4/6] Gemini: rewrite=%d bản | captions=%s",
            rewrite_count, gemini_platforms or "all",
        )
        print(
            "  ⏳ Gemini đang xử lý"
            + (f" {rewrite_count} bản rewrite" if need_rewrite else "")
            + (" captions" if need_captions else "")
            + "..."
        )
        if need_rewrite:
            # Gộp rewrite + caption trong 1 request (giữ nguyên flow cũ)
            rewritten_versions, social_captions = gemini.rewrite_and_caption_batch(
                original_html  = drive_article.content,
                topic          = parsed.topic,
                title          = drive_article.title,
                rewrite_count  = rewrite_count,
                platforms      = gemini_platforms if need_captions else None,
                facebook_pages = pages_to_post if len(pages_to_post) > 1 else None,
            )
        elif need_captions:
            # Chỉ caption (Facebook/Buffer/...) — dùng standalone prompt
            # tránh conflict JSON schema trong rewrite_and_caption_batch
            social_captions = gemini.generate_social_captions(
                topic          = parsed.topic,
                title          = drive_article.title,
                plain_text     = plain,
                platforms      = gemini_platforms,
                facebook_pages = pages_to_post if len(pages_to_post) > 1 else None,
            )
        print("  ✅ Xong")
        logger.info("  → %d bản rewrite | %d platforms caption",
                    len(rewritten_versions), len(social_captions))
    else:
        logger.info("[4/6] Bỏ qua Gemini (chỉ đăng WP đơn, không social)")

    if need_captions:
        social_texts = build_social_texts(
            topic           = parsed.topic,
            title           = drive_article.title,
            excerpt         = plain[:300],
            wp_url          = "",
            social_captions = social_captions,
        )

    fb_value = social_captions.get("facebook")
    facebook_page_texts: dict[str, str] = {}
    if isinstance(fb_value, list) and len(pages_to_post) > 1:
        facebook_page_texts = {
            p["id"]: v for p, v in zip(pages_to_post, fb_value)
        }

    # ── Build (site, article_data) pairs ─────────────────────────────────────
    # Site 0 = bản gốc; site 1+ = bản rewrite tương ứng (nếu có)
    base_article_data = {
        "seo_title":        drive_article.title,
        "meta_description": plain[:160],
        "focus_keyword":    parsed.topic,
        "excerpt":          plain[:300],
        "content_html":     drive_article.content,
        "social_captions":  {},
    }

    site_article_pairs: list[tuple] = []
    for i, site_cfg in enumerate(wp_sites_to_publish):
        if i == 0 or not rewrite_count:
            site_article_pairs.append((site_cfg, base_article_data))
        else:
            idx = i - 1
            new_html, new_title = (
                rewritten_versions[idx]
                if idx < len(rewritten_versions)
                else (drive_article.content, drive_article.title)
            )
            site_article_pairs.append((site_cfg, {
                **base_article_data,
                "content_html": new_html,
                "seo_title":    new_title,
            }))

    # ── Bước 5: Lưu file backup ───────────────────────────────────────────────
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

    # ── Bước 6: Publish song song ─────────────────────────────────────────────
    logger.info("[6/6] Xuất bản song song (WP=%s | Buffer=%s | FB=%s)...",
                should_wp, should_buffer, should_facebook)

    wp_futures: list[tuple]  = []
    buffer_future            = None
    facebook_future          = None
    max_workers              = len(wp_sites_to_publish) + 2

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:

        if should_wp:
            for site_cfg, art_data in site_article_pairs:
                f = executor.submit(
                    _worker_wordpress,
                    WordPressService(site_cfg),
                    art_data,
                    parsed,
                    save_as_draft,
                )
                wp_futures.append((f, site_cfg.site_url))

        if should_buffer:
            buffer_future = executor.submit(
                _worker_buffer,
                cfg, social_texts, drive_image_urls,
                parsed.schedule_time or None,
                buffer_platforms,
                drive_article.title,
            )

        if should_facebook:
            fb_text = social_texts.get("facebook", {}).get("text", drive_article.title)
            facebook_future = executor.submit(
                _worker_facebook,
                cfg.facebook,
                fb_text,
                drive_image_urls,
                None,
                parsed.schedule_time or None,
                selected_page_ids,
                facebook_page_texts or None,
            )

        # ── Thu kết quả WP ────────────────────────────────────────────────────
        for wp_f, site_url in wp_futures:
            try:
                wp_resp = wp_f.result()
                if wp_resp:
                    if wp_resp.get("status") == "draft":
                        edit_url = (
                            f"{site_url.rstrip('/')}/wp-admin/post.php"
                            f"?post={wp_resp['id']}&action=edit"
                        )
                        print(f"✅ [{site_url}] Bản nháp đã tạo")
                        print(f"   └─ Thay ảnh rồi Publish tại: {edit_url}")
                    else:
                        logger.info("  → ✅ WordPress [%s]: %s", site_url, wp_resp["link"])
                        print(f"✅ [{site_url}] Đã đăng: {wp_resp['link']}")

                    if result.wp_post_id is None:
                        result.wp_post_id  = wp_resp["id"]
                        result.wp_post_url = wp_resp.get("link", "")
                        result.wp_status   = wp_resp["status"]
            except Exception as exc:
                logger.error("  → ❌ WordPress [%s] thất bại: %s", site_url, exc)
                result.error = str(exc)

        # ── Thu kết quả Buffer ────────────────────────────────────────────────
        if buffer_future is not None:
            try:
                result.buffer_results = buffer_future.result()
                succeeded = sum(1 for r in result.buffer_results if r.status == "success")
                logger.info("  → Buffer: %d/%d channel thành công",
                            succeeded, len(result.buffer_results))
            except Exception as exc:
                logger.warning("  → ❌ Buffer thất bại: %s", exc)

        # ── Thu kết quả Facebook ──────────────────────────────────────────────
        if facebook_future is not None:
            try:
                result.facebook_results = facebook_future.result()
                succeeded = sum(1 for r in result.facebook_results if r.status == "success")
                logger.info("  → Facebook: %d/%d page thành công",
                            succeeded, len(result.facebook_results))
            except Exception as exc:
                logger.warning("  → ❌ Facebook thất bại: %s", exc)

    if not should_wp and not should_buffer and not should_facebook:
        logger.info("  → 💾 Chỉ lưu file (WP/Buffer/Facebook chưa cấu hình)")

    if webhook_url:
        _notify_webhook(webhook_url, drive_article.title, file_path)

    return result


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    user_input  = sys.argv[1] if len(sys.argv) > 1 else "Du lịch Đà Nẵng"
    webhook_url = sys.argv[2] if len(sys.argv) > 2 else None

    result = run(user_input, webhook_url)

    if result.error and result.error not in _NON_ERROR_STATES:
        logger.error("Lỗi: %s", result.error)
        sys.exit(1)

    if result.posted_to_wp:
        logger.info("WP URL: %s", result.wp_post_url)
    sys.exit(0)


if __name__ == "__main__":
    main()