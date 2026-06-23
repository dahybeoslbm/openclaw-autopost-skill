import os
import sys
import time
import argparse
from utils.parser import _detect_schedule, _detect_preselection, parse_request

from config import load_config
from utils.logger import get_logger
from utils.models import ParsedRequest, PublishResult
from services.gemini import GeminiService
from services.googledrive import GoogleDriveService
from utils.selection_cache import (
    PendingSelection, save_pending, load_any_pending,
    delete_pending, delete_all_pending, purge_expired,
    load_pending_pages, delete_pending_pages,
    load_pending_wp_sites, delete_pending_wp_sites,
)
from utils import buffer_schedule_cache as bsc

from use_cases.publish_article import _continue_publish, _load_drive_article, _cleanup_old_backups

logger = get_logger("cli_controller")

_CANCEL_KEYWORDS = {"huỷ", "huy", "cancel", "thôi", "bỏ"}
_ALL_PAGES_KEYWORDS = {"tất cả", "tat ca", "tatca", "all"}
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