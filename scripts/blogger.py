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
from bs4 import BeautifulSoup

import argparse
from utils.parser import _detect_schedule, _detect_preselection

from config import FacebookConfig, ZaloConfig, load_config
from services.facebook import FacebookService
from services.zalo import ZaloService
from utils.logger import get_logger
from utils.models import FacebookPostResult, ZaloPostResult, PublishResult, BufferPostResult
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
    "NO_PENDING_SELECTION",
    "INVALID_CHOICE",
    "INVALID_PAGE_CHOICE",
    "INVALID_WP_SITE_CHOICE",
    "DUPLICATE_POST",
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
        
# ── Content helpers ───────────────────────────────────────────────────────────

def _extract_first_paragraph(html: str) -> str:
    """Extracts the first paragraph text from HTML, skipping headings."""
    soup = BeautifulSoup(html, "html.parser")
    for p in soup.find_all('p'):
        text = p.get_text(strip=True)
        if text:
            return text
    for heading in soup.find_all(['h1', 'h2', 'h3']):
        heading.decompose()
    return soup.get_text(strip=True)

def _strip_leading_h1(html: str) -> str:
    """Strip the first <h1> block from content HTML.

    WordPress renders the post title (seo_title) itself, so keeping <h1>
    inside content_html causes a duplicate heading on the page.
    The Google Doc legitimately contains an <h1> — we strip it only at the
    WP publish boundary.
    """
    return _img_re.sub(
        r'^\s*<h1[^>]*>.*?</h1>\s*',
        '',
        html,
        count=1,
        flags=_img_re.DOTALL | _img_re.IGNORECASE,
    )

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


def _worker_zalo(
    cfg_zalo: ZaloConfig,
    article_data: dict,
    cover_photo_url: str = "",
    cfg_wp = None,
) -> ZaloPostResult:
    wp_svc = None
    if cfg_wp and cfg_wp.is_valid:
        from services.wordpress import WordPressService
        wp_svc = WordPressService(cfg_wp)

    if cover_photo_url and ("localhost" in cover_photo_url or "host.docker.internal" in cover_photo_url or "drive.google.com" in cover_photo_url):
        if wp_svc:
            try:
                logger.info("  → [Zalo] Upload ảnh cover trung gian lên WordPress...")
                upload_res = wp_svc.upload_image_from_url(cover_photo_url)
                if upload_res:
                    cover_photo_url = upload_res[1]
                else:
                    logger.warning("  → [Zalo] Upload ảnh cover trung gian thất bại.")
            except Exception as e:
                logger.error("  → [Zalo] Lỗi upload ảnh cover trung gian: %s", e)

    # Parse and replace body images
    content = article_data.get("content_html", "")
    if content and wp_svc:
        new_content, _ = wp_svc.upload_and_replace_html_images(content)
        article_data["content_html"] = new_content

    zalo = ZaloService(cfg_zalo)
    return zalo.publish_from_article_data(
        article_data    = article_data,
        cover_photo_url = cover_photo_url,
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

def run(user_prompt: str, webhook_url: str | None = None, parsed_override: ParsedRequest | None = None) -> PublishResult:
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
                    msg = f"⚠️  Số không hợp lệ. Vui lòng chọn từ 1 đến {len(pending_pages.pages)}."
                    print(msg)
                    return PublishResult(file_path="", error="INVALID_PAGE_CHOICE", prompt_msg=msg)
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
                    msg = f"⚠️  Số không hợp lệ. Vui lòng chọn từ 1 đến {len(pending_wp.sites)}."
                    print(msg)
                    return PublishResult(file_path="", error="INVALID_WP_SITE_CHOICE", prompt_msg=msg)
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
            msg = "⚠️  Không tìm thấy phiên chọn bài nào còn hạn (TTL 24h).\nVui lòng gõ lại yêu cầu đăng bài từ đầu."
            print(msg)
            return PublishResult(file_path="", error="NO_PENDING_SELECTION", prompt_msg=msg)

        if choice < 1 or choice > len(pending.candidates):
            msg = f"⚠️  Số không hợp lệ. Vui lòng chọn từ 1 đến {len(pending.candidates)}."
            print(msg)
            return PublishResult(file_path="", error="INVALID_CHOICE", prompt_msg=msg)

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
    parsed = parsed_override if parsed_override else parse_request(user_prompt)

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

    msg = "\n".join(lines)
    print(msg)
    return PublishResult(file_path="", error="PENDING_SELECTION", prompt_msg=msg)


# ── Core publish pipeline ─────────────────────────────────────────────────────

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
      "blog"  (default) → WP + TẤT CẢ Buffer channels
      "wordpress"       → chỉ WP
      Tên platform cụ thể → chỉ platform đó

    WordPress luôn lưu draft để con người kiểm duyệt trước khi publish.
    Khi đăng nhiều WP sites, Gemini tự động viết lại từng bản để tránh
    duplicate content (không cần hỏi người dùng).
    """
    platforms     = [p.lower() for p in parsed.platforms]
    publish_all   = platforms == ["blog"]
    should_wp     = publish_all or "wordpress" in platforms
    should_facebook  = cfg.facebook.is_valid and (publish_all or "facebook" in platforms)
    should_zalo      = cfg.zalo.is_valid and (publish_all or "zalo" in platforms)

    if publish_all:
        buffer_platforms = [
            p for p in BufferClient.get_configured_platforms()
            if p != "facebook"
        ]
    else:
        buffer_platforms = [p for p in platforms if p in _BUFFER_PLATFORMS]

    should_buffer = cfg.buffer.is_valid and bool(buffer_platforms)

    # ── Bước 3: Thu thập Drive image URLs (cho Buffer/Facebook) ──────────────
    logger.info("[3/6] Lấy ảnh từ Google Docs (%d ảnh)", drive_article.image_count())
    _api_parsed  = _urlparse(cfg.googledrive.api_url)
    _actual_base = f"{_api_parsed.scheme}://{_api_parsed.netloc}"

    drive_image_urls: list[str] = []
    for block in drive_article.content_blocks:
        if block.get("type") == "image" and block.get("url"):
            url = _img_re.sub(r'https?://localhost(:\d+)?', _actual_base, block["url"])
            drive_image_urls.append(url)

    import re
    def normalize_name(name: str) -> str:
        return re.sub(r'[^a-z0-9]', '', name.lower())

    # ── Tự động resolve pre-selection từ NLU ─────────────────────────────────
    if should_facebook and selected_page_ids is None and parsed.preselected_pages:
        p_pages = parsed.preselected_pages.strip().lower()
        if p_pages in _ALL_PAGES_KEYWORDS:
            selected_page_ids = [pg["id"] for pg in cfg.facebook.pages]
            print(f"✅ Đã tự động chọn {len(selected_page_ids)} page: {', '.join(pg['name'] for pg in cfg.facebook.pages)}")
        else:
            if all(x.isdigit() for x in p_pages.split()):
                # Resolve by indices
                raw_indices = [int(x) - 1 for x in p_pages.split()]
                selected = [cfg.facebook.pages[i] for i in raw_indices if 0 <= i < len(cfg.facebook.pages)]
            else:
                # Resolve by string matching
                norm_p = normalize_name(p_pages)
                selected = [pg for pg in cfg.facebook.pages if norm_p in normalize_name(pg['name'])]
                
            if len(selected) == 1 or (len(selected) > 1 and all(x.isdigit() for x in p_pages.split())):
                selected_page_ids = [pg["id"] for pg in selected]
                print(f"✅ Đã tự động chọn {len(selected_page_ids)} page: {', '.join(pg['name'] for pg in selected)}")
            elif len(selected) > 1:
                print(f"⚠️ Từ khóa '{p_pages}' khớp với {len(selected)} pages. Vui lòng chọn thủ công để tránh nhầm lẫn.")
            else:
                print(f"⚠️ Không tìm thấy page nào khớp với '{p_pages}'. Vui lòng chọn thủ công.")

    valid_wp_sites = [s for s in cfg.wordpress_sites if s.is_valid]
    if should_wp and selected_wp_site_urls is None and parsed.preselected_wp_sites:
        p_sites = parsed.preselected_wp_sites.strip().lower()
        if p_sites in _ALL_PAGES_KEYWORDS:
            selected_wp_site_urls = [s.site_url for s in valid_wp_sites]
            print(f"✅ Đã tự động chọn {len(selected_wp_site_urls)} site: {', '.join(selected_wp_site_urls)}")
        else:
            if all(x.isdigit() for x in p_sites.split()):
                # Resolve by indices
                raw_indices = [int(x) - 1 for x in p_sites.split()]
                selected = [valid_wp_sites[i] for i in raw_indices if 0 <= i < len(valid_wp_sites)]
            else:
                # Resolve by string matching
                norm_s = normalize_name(p_sites)
                selected = [s for s in valid_wp_sites if norm_s in normalize_name(s.site_url)]
                
            if len(selected) == 1 or (len(selected) > 1 and all(x.isdigit() for x in p_sites.split())):
                selected_wp_site_urls = [s.site_url for s in selected]
                print(f"✅ Đã tự động chọn {len(selected_wp_site_urls)} site: {', '.join(selected_wp_site_urls)}")
            elif len(selected) > 1:
                print(f"⚠️ Từ khóa '{p_sites}' khớp với {len(selected)} sites. Vui lòng chọn thủ công để tránh nhầm lẫn.")
            else:
                print(f"⚠️ Không tìm thấy site nào khớp với '{p_sites}'. Vui lòng chọn thủ công.")

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
        msg = "\n".join(lines)
        print(msg)
        return PublishResult(file_path="", error="PENDING_PAGE_SELECTION", prompt_msg=msg)

    # ── Guard: chọn WordPress sites ───────────────────────────────────────────
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
        lines.append("→ Nhập số thứ tự (vd: '1 2') hoặc 'tất cả'.")
        lines.append("  (Gõ 'huỷ' để bỏ qua)")
        msg = "\n".join(lines)
        print(msg)
        return PublishResult(file_path="", error="PENDING_WP_SITE_SELECTION", prompt_msg=msg)

    # ── Xác định sites thực tế sẽ publish ────────────────────────────────────
    wp_sites_to_publish = (
        [s for s in cfg.wordpress_sites if s.site_url in selected_wp_site_urls]
        if selected_wp_site_urls is not None
        else valid_wp_sites
    )
    wp_sites_to_publish = [s for s in wp_sites_to_publish if s.is_valid]

    # WordPress luôn lưu draft để kiểm duyệt trước khi publish
    save_as_draft = True

    if should_wp:
        _STATUS_LABELS = {
            "publish": "Đã publish",
            "draft":   "Bản nháp",
            "pending": "Chờ duyệt",
            "future":  "Đã lên lịch",
            "private": "Riêng tư",
        }
        all_duplicates: list[str] = []
        filtered_wp_sites = []

        for site_cfg in wp_sites_to_publish:
            wp_svc     = WordPressService(site_cfg)
            duplicates = wp_svc.check_duplicate(drive_article.title)
            if duplicates:
                for dup in duplicates:
                    label = _STATUS_LABELS.get(dup["status"], dup["status"])
                    all_duplicates.append(
                        f"  ⚠️  [{site_cfg.site_url}] Bài \"{dup['title']}\" "
                        f"đã tồn tại ({label})\n"
                        f"     └─ {dup['edit_url']}"
                    )
            else:
                filtered_wp_sites.append(site_cfg)

        if all_duplicates:
            msg = "\n".join(["⛔ Đã bỏ qua các site có bài viết trùng lặp:"] + all_duplicates)
            print(msg)
            
        wp_sites_to_publish = filtered_wp_sites
        if not wp_sites_to_publish:
            msg = "⛔ Tất cả các site đều đã tồn tại bài viết này. Dừng tiến trình."
            print(msg)
            return PublishResult(file_path="", error="DUPLICATE_POST", prompt_msg=msg)

    # Khi đăng nhiều sites → tự động rewrite để tránh duplicate content
    # Site đầu tiên dùng bản gốc, các site sau nhận bản rewrite riêng
    rewrite_count = max(0, len(wp_sites_to_publish) - 1) if should_wp else 0
    
    # ── Bước 4: Gemini — rewrite + captions trong 1 request ──────────────────
    plain = drive_article.plain_text()

    pages_to_post: list[dict] = []
    if should_facebook:
        pages_to_post = (
            [p for p in cfg.facebook.pages if p.get("id") in selected_page_ids]
            if selected_page_ids is not None
            else cfg.facebook.pages
        )

    _active_for_gemini = list(buffer_platforms)
    if should_facebook:
        _active_for_gemini.append("facebook")
    gemini_platforms: list[str] | None = _active_for_gemini or None

    need_captions = should_buffer or should_facebook
    need_rewrite  = rewrite_count > 0
    need_summary  = True  # Luôn gọi Gemini để lấy SEO Summary

    social_texts: dict                        = {}
    social_captions: dict                     = {}
    rewritten_versions: list[tuple[str, str]] = []
    seo_summary: str                          = ""
    zalo_cta_text: str                        = ""

    if need_rewrite or need_captions or need_summary or should_zalo:
        logger.info(
            "[4/6] Gemini: rewrite=%d bản | captions=%s | summary=%s | cta=%s",
            rewrite_count, gemini_platforms or "None", need_summary, should_zalo,
        )
        print("  ⏳ Gemini đang xử lý (Batch Request)...")
        
        rewritten_versions, social_captions, seo_summary, zalo_cta_text = gemini.process_content_batch(
            original_html  = drive_article.content,
            topic          = parsed.topic,
            title          = drive_article.title,
            rewrite_count  = rewrite_count,
            platforms      = gemini_platforms if need_captions else None,
            facebook_pages = pages_to_post if len(pages_to_post) > 1 else None,
            need_summary   = need_summary,
            need_zalo_cta  = should_zalo,
        )

        print("  ✅ Xong")
        logger.info("  → %d bản rewrite | %d platforms caption | summary: %d chars | cta: %d chars",
                    len(rewritten_versions), len(social_captions), len(seo_summary), len(zalo_cta_text))
    else:
        logger.info("[4/6] Bỏ qua Gemini")

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
    final_summary = seo_summary if seo_summary else _extract_first_paragraph(drive_article.content)[:300]
    
    base_article_data = {
        "seo_title":        drive_article.title,
        "meta_description": final_summary,
        "focus_keyword":    parsed.topic,
        "excerpt":          final_summary,
        "content_html":     _strip_leading_h1(drive_article.content),
        "social_captions":  {},
        "zalo_cta_text":    zalo_cta_text,
        "zalo_cta_link":    "https://timchuyenbay.vn/",
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
                "content_html": _strip_leading_h1(new_html),
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
    logger.info("[6/6] Xuất bản song song (WP=%s | Buffer=%s | FB=%s | Zalo=%s)...",
                should_wp, should_buffer, should_facebook, should_zalo)

    wp_futures: list[tuple]  = []
    buffer_future            = None
    facebook_future          = None
    zalo_future              = None
    max_workers              = len(wp_sites_to_publish) + 3

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

        if should_zalo:
            zalo_cover = drive_image_urls[0] if drive_image_urls else ""
            zalo_future = executor.submit(
                _worker_zalo,
                cfg.zalo,
                base_article_data,
                zalo_cover,
                cfg.wordpress,
            )

        # ── Thu kết quả WP ────────────────────────────────────────────────────
        for wp_f, site_url in wp_futures:
            try:
                wp_resp = wp_f.result()
                if wp_resp:
                    edit_url = (
                        f"{site_url.rstrip('/')}/wp-admin/post.php"
                        f"?post={wp_resp['id']}&action=edit"
                    )
                    print(f"✅ [{site_url}] Bản nháp đã tạo")
                    print(f"   └─ Kiểm duyệt rồi Publish tại: {edit_url}")

                    if "link" in wp_resp:
                        if not hasattr(result, "wp_urls"):
                            result.wp_urls = []
                        result.wp_urls.append(wp_resp["link"])

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

        # ── Thu kết quả Zalo ──────────────────────────────────────────────────
        if zalo_future is not None:
            try:
                zalo_result = zalo_future.result()
                result.zalo_results = [zalo_result]
                if zalo_result.status == "success":
                    logger.info("  → Zalo: ✅ Đăng bài thành công | token=%s",
                                zalo_result.article_token)
                else:
                    logger.warning("  → Zalo: ❌ %s", zalo_result.error)
            except Exception as exc:
                logger.warning("  → ❌ Zalo thất bại: %s", exc)

    if not should_wp and not should_buffer and not should_facebook and not should_zalo:
        logger.info("  → 💾 Chỉ lưu file (WP/Buffer/Facebook/Zalo chưa cấu hình)")

    if webhook_url:
        _notify_webhook(webhook_url, drive_article.title, file_path)

    return result


# ── Entry point ───────────────────────────────────────────────────────────────

def main():

    
    parser = argparse.ArgumentParser()
    parser.add_argument('user_prompt', nargs='*', default=[])
    parser.add_argument('--topic', type=str, default="")
    parser.add_argument('--platform', type=str, default="")
    parser.add_argument('--time', type=str, default="")
    # Tham số preselection rõ ràng — Agent truyền thẳng không qua Regex
    parser.add_argument('--pages', type=str, default="",
                        help="Preselect Facebook pages: 'all', '1', '1 2', or page name")
    parser.add_argument('--wp-site', type=str, default="",
                        help="Preselect WP site URL or 'all'")
    args, unknown = parser.parse_known_args()

    user_input = " ".join(args.user_prompt) if args.user_prompt else ""
    if not user_input and not args.topic:
        user_input = "Du lịch Đà Nẵng"

    parsed_override = None
    if args.topic:
        # Ưu tiên tham số rõ ràng (--pages, --wp-site), fallback sang Regex từ prompt gốc
        original_prompt = " ".join(args.user_prompt) if args.user_prompt else ""
        pages_sel_regex, wp_sel_regex, _, _ = _detect_preselection(original_prompt) if original_prompt else ("", "", "", "")
        pages_sel = args.pages or pages_sel_regex
        wp_sel = args.wp_site or wp_sel_regex
        
        user_input = args.topic
        plts = [x.strip().lower() for x in args.platform.split(',')] if args.platform else ["blog"]
        
        schedule = ""
        if args.time:
            import re
            # Nếu truyền sẵn chuẩn ISO thì dùng luôn, khỏi parse
            if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", args.time):
                schedule = args.time
            else:
                schedule = _detect_schedule(args.time)

        parsed_override = ParsedRequest(
            topic=args.topic,
            platforms=plts,
            schedule_time=schedule,
            preselected_pages=pages_sel,
            preselected_wp_sites=wp_sel
        )

    webhook_url = None

    cfg = load_config()
    chat_id = cfg.chat_id or "default"
    output_dir = os.environ.get("OUTPUT_DIR", "/app/output")
    os.makedirs(output_dir, exist_ok=True)
    start_time_file = os.path.join(output_dir, f".start_time_{chat_id}.txt")
    
    p = user_input.strip().lower()
    is_continuation = p.isdigit() or all(x.isdigit() for x in p.split()) or p in ["tất cả", "tat ca", "tatca", "all", "huỷ", "huy", "cancel", "thôi", "bỏ"]

    # ── Luôn detect preselection từ câu gốc nếu parsed_override chưa có ────────
    # Áp dụng kể cả khi Agent không truyền --pages/--wp-site
    if not is_continuation:
        full_text = " ".join(args.user_prompt) if args.user_prompt else user_input
        pages_from_prompt, wp_from_prompt, _, _ = _detect_preselection(full_text.lower())
        if parsed_override is None:
            # Không có --topic flag: tạo ParsedRequest tối thiểu chỉ với preselection
            parsed_override = ParsedRequest(
                topic=user_input,
                preselected_pages=args.pages or pages_from_prompt,
                preselected_wp_sites=args.wp_site or wp_from_prompt,
            )
        else:
            # Có --topic flag nhưng Agent quên --pages: bổ sung từ regex
            if not parsed_override.preselected_pages:
                parsed_override.preselected_pages = args.pages or pages_from_prompt
            if not parsed_override.preselected_wp_sites:
                parsed_override.preselected_wp_sites = args.wp_site or wp_from_prompt
    
    if not is_continuation or not os.path.exists(start_time_file):
        start_time = time.time()
        try:
            with open(start_time_file, "w") as f:
                f.write(str(start_time))
        except Exception:
            pass
    else:
        try:
            with open(start_time_file, "r") as f:
                start_time = float(f.read().strip())
        except Exception:
            start_time = time.time()

    result = run(user_input, webhook_url, parsed_override=parsed_override)

    elapsed = time.time() - start_time
    time_str = f"⏱ Thời gian xử lý: {elapsed:.1f} giây"
    print(f"\n[AGENT_INSTRUCTION: YOU MUST INCLUDE THIS EXACT TEXT IN YOUR FINAL REPLY: '{time_str}']", flush=True)
    
    if hasattr(result, 'file_path') and result.file_path:
        time_file = result.file_path.replace(".md", ".time")
        try:
            with open(time_file, "w", encoding="utf-8") as f:
                f.write(time_str)
        except Exception:
            pass

    if result.error and result.error not in _NON_ERROR_STATES:
        logger.error("Lỗi: %s", result.error)
        
        tele_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if tele_token and chat_id and chat_id != "default":
            import requests
            msg = f"❌ Lỗi xử lý bài '{user_input}':\n{result.error}"
            
            # Thêm debug info để kiểm tra NLU
            if parsed_override:
                msg += "\n\n🛠 **[NLU Debug - Trích xuất từ OpenClaw]**"
                msg += f"\n- Topic: {parsed_override.topic}"
                msg += f"\n- Platform: {', '.join(parsed_override.platforms)}"
                msg += f"\n- Time (ISO): {parsed_override.schedule_time if parsed_override.schedule_time else 'Đăng ngay'}"

            try:
                requests.post(
                    f"https://api.telegram.org/bot{tele_token}/sendMessage",
                    json={"chat_id": chat_id, "text": msg},
                    timeout=5
                )
            except Exception:
                pass
        sys.exit(1)

    if result.posted_to_wp:
        logger.info("WP URL: %s", result.wp_post_url)
        
    # Gửi thông báo Telegram (vì chạy ngầm)
    tele_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if tele_token and chat_id and chat_id != "default":
        import requests
        
        if result.prompt_msg:
            # Nếu có thông báo nhắc (Prompt) -> gửi thẳng prompt cho user
            msg = result.prompt_msg
        elif is_continuation and not result.posted_to_wp and not result.posted_to_facebook and not result.posted_to_buffer and not result.zalo_results:
            # Chưa đăng được đâu (huỷ hoặc lựa chọn trung gian)
            if "huỷ" in p or "huy" in p or "cancel" in p or "thôi" in p:
                msg = f"❌ Đã huỷ yêu cầu.\n{time_str}"
            else:
                msg = f"⏳ Đã xử lý lựa chọn: {user_input}\n{time_str}"
        else:
            msg = f"✅ Đã chạy lệnh: {user_input}\n"
            
            if getattr(result, "wp_urls", []):
                for url in result.wp_urls:
                    if url:
                        msg += f"🌐 WP: {url}\n"
            elif getattr(result, "wp_post_url", None):
                msg += f"🌐 WP: {result.wp_post_url}\n"
                
            if getattr(result, "facebook_results", []):
                ok = [r.page_name for r in result.facebook_results if r.status == "success"]
                errs = [f"{r.page_name}: {r.error}" for r in result.facebook_results if r.status == "error"]
                if ok: msg += f"👤 FB OK: {', '.join(ok)}\n"
                if errs: msg += f"❌ FB Lỗi: {'; '.join(errs)}\n"
                
            if getattr(result, "zalo_results", []):
                ok = [r.app_id for r in result.zalo_results if r.status == "success"]
                errs = [r.error for r in result.zalo_results if r.status == "error"]
                if ok: msg += f"💬 Zalo: OK\n"
                if errs: msg += f"❌ Zalo Lỗi: {errs[0]}\n"
                
            if getattr(result, "buffer_results", []):
                ok = [f"{r.platform} ({r.channel_name})" for r in result.buffer_results if r.status == "success"]
                errs = [f"{r.platform} ({r.channel_name}): {r.error}" for r in result.buffer_results if r.status == "error"]
                if ok: msg += f"🔗 Buffer OK: {', '.join(ok)}\n"
                if errs: msg += f"❌ Buffer Lỗi: {'; '.join(errs)}\n"
                
            msg += time_str
            
        try:
            requests.post(
                f"https://api.telegram.org/bot{tele_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg},
                timeout=5
            )
        except Exception as e:
            logger.warning("Không thể gửi Telegram webhook: %s", e)

    sys.exit(0)


if __name__ == "__main__":
    main()