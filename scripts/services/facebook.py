"""
services/facebook.py — Đăng bài trực tiếp lên Facebook Pages qua Meta Graph API.
Hỗ trợ: nhiều Pages song song, photo + caption, video + caption, hẹn giờ.

Cấu hình .env:
  FACEBOOK_PAGES='[{"id":"PAGE_ID","name":"Tên Page","access_token":"PAGE_TOKEN"}]'
"""
import calendar
import concurrent.futures
import datetime
import json
import os

import requests

from config import FacebookConfig
from utils.logger import get_logger
from utils.models import FacebookPostResult

logger = get_logger(__name__)

FB_API_BASE = "https://graph.facebook.com/v25.0"

_FIXED_FOOTER = """Comment "Tư vấn" hoặc liên hệ Hotline: 1900636060 để nhận báo giá cực sốc!
Bắt đầu hành trình của bạn ngay tại:
Website: https://timchuyenbay.vn/
Cần tư vấn chặng bay êm ái, giá mềm? Nhắn ngay cho chúng mình:
Zalo: https://zalo.me/2941581384627345950
Ghé thăm chúng mình tại: https://maps.app.goo.gl/uMhLZedph1ki9rgd7?g_st=ic"""


def _to_unix(iso_str: str) -> int:
    """Chuyển ISO 8601 UTC ('2026-05-20T13:00:00Z') → Unix timestamp."""
    dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return int(calendar.timegm(dt.utctimetuple()))


class FacebookService:

    def __init__(self, config: FacebookConfig):
        self._config = config

    def _build_caption(self, text: str) -> str:
        return f"{text}\n\n{_FIXED_FOOTER}"
    
    # ── Post đến 1 page ──────────────────────────────────────────────────────
    def post_to_page(
        self,
        page_id: str,
        page_token: str,
        text: str = "",
        image_urls: list[str] | None = None,
        video_url: str | None = None,
        scheduled_at: str | None = None,
    ) -> dict:
        """
        Đăng 1 bài lên 1 Page.
        Ưu tiên: video > ảnh (lấy URL đầu tiên) > text thuần.
        """
        if video_url:
            return self._post_video(page_id, page_token, video_url, text, scheduled_at)
        if image_urls:
            return self._post_photo(page_id, page_token, image_urls[0], text, scheduled_at)
        return self._post_text(page_id, page_token, text, scheduled_at)

    def _post_text(
        self, page_id: str, token: str, message: str, scheduled_at: str | None
    ) -> dict:
        payload: dict = {"message": message, "access_token": token}
        if scheduled_at:
            payload["scheduled_publish_time"] = _to_unix(scheduled_at)
            payload["published"] = False
        resp = requests.post(f"{FB_API_BASE}/{page_id}/feed", data=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post_photo(
        self, page_id: str, token: str, image_url: str,
        caption: str, scheduled_at: str | None
    ) -> dict:
        payload: dict = {
            "url": image_url,
            "caption": caption,
            "access_token": token,
        }
        if scheduled_at:
            payload["scheduled_publish_time"] = _to_unix(scheduled_at)
            payload["published"] = False
        resp = requests.post(f"{FB_API_BASE}/{page_id}/photos", data=payload, timeout=30)
        if not resp.ok:
            logger.error(
            "  → [Facebook] HTTP %d | Body: %s", 
            resp.status_code, 
            resp.text[:500]   # in tối đa 500 ký tự
        )
        resp.raise_for_status()
        return resp.json()

    def _post_video(
        self, page_id: str, token: str, video_url: str,
        description: str, scheduled_at: str | None
    ) -> dict:
        payload: dict = {
            "file_url": video_url,
            "description": description,
            "access_token": token,
        }
        if scheduled_at:
            payload["scheduled_publish_time"] = _to_unix(scheduled_at)
            payload["published"] = False
        resp = requests.post(f"{FB_API_BASE}/{page_id}/videos", data=payload, timeout=60)
        
        if not resp.ok:
            logger.error(
                "  → [Facebook] HTTP %d | Body: %s", 
                resp.status_code, 
                resp.text[:500]   # in tối đa 500 ký tự
        )
        resp.raise_for_status()
        return resp.json()

    # ── Post đến nhiều pages song song ───────────────────────────────────────

    def post_to_all_pages(
        self,
        text: str = "",
        image_urls: list[str] | None = None,
        video_url: str | None = None,
        scheduled_at: str | None = None,
        page_texts: dict[str, str] | None = None
    ) -> list[FacebookPostResult]:
        """Đăng lên tất cả pages trong FACEBOOK_PAGES song song."""
        targets = self._config.pages
        if not targets:
            logger.warning("  → [Facebook] Không tìm thấy page nào trong FACEBOOK_PAGES")
            return []

        def _post_one(page: dict) -> FacebookPostResult:
            pid   = page.get("id", "")
            token = page.get("access_token", "")
            name  = page.get("name", pid)
            try:
                caption = self._build_caption((page_texts or {}).get(pid, text))
                result = self.post_to_page(
                    pid, token, caption, image_urls, video_url, scheduled_at
                )
                logger.info("  → [Facebook] ✅ %s | post_id=%s", name, result.get("id", ""))
                return FacebookPostResult(
                    page_id=pid, page_name=name,
                    status="success", post_id=result.get("id", ""),
                )
            except Exception as exc:
                logger.warning("  → [Facebook] ❌ %s — %s", name, exc)
                return FacebookPostResult(
                    page_id=pid, page_name=name,
                    status="error", error=str(exc),
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            return list(ex.map(_post_one, targets))
    
    def post_to_selected_pages(
        self,
        page_ids: list[str],
        text: str = "",
        image_urls: list[str] | None = None,
        video_url: str | None = None,
        scheduled_at: str | None = None,
        page_texts: dict[str, str] | None = None
    ) -> list[FacebookPostResult]:
        """
        Đăng lên các pages được chọn theo page_ids.
        Lọc từ self._config.pages — chỉ đăng đúng pages trong danh sách.
        """
        targets = [p for p in self._config.pages if p.get("id") in page_ids]

        if not targets:
            logger.warning("  → [Facebook] Không tìm thấy page nào khớp page_ids: %s", page_ids)
            return []

        # Tái sử dụng logic song song của post_to_all_pages
        def _post_one(page: dict) -> FacebookPostResult:
            pid   = page.get("id", "")
            token = page.get("access_token", "")
            name  = page.get("name", pid)
            try:
                caption = self._build_caption((page_texts or {}).get(pid, text))
                result = self.post_to_page(pid, token, caption, image_urls, video_url, scheduled_at)
                logger.info("  → [Facebook] ✅ %s | post_id=%s", name, result.get("id", ""))
                return FacebookPostResult(page_id=pid, page_name=name,
                                        status="success", post_id=result.get("id", ""))
            except Exception as exc:
                logger.warning("  → [Facebook] ❌ %s — %s", name, exc)
                return FacebookPostResult(page_id=pid, page_name=name,
                                        status="error", error=str(exc))

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            return list(ex.map(_post_one, targets))