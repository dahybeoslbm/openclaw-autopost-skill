"""
services/facebook.py — Đăng bài trực tiếp lên Facebook Pages qua Meta Graph API.
Hỗ trợ: nhiều Pages song song, photo + caption, video + caption, hẹn giờ.

Cấu hình .env:
  FACEBOOK_PAGES='[{"id":"PAGE_ID","name":"Tên Page","access_token":"PAGE_TOKEN"}]'
"""
import calendar
import concurrent.futures
import datetime
from time import sleep

import requests

from config import FacebookConfig
from utils.logger import get_logger
from utils.models import FacebookPostResult

logger = get_logger(__name__)

FB_API_BASE = "https://graph.facebook.com/v25.0"

_FIXED_FOOTER = """Comment "Tư vấn" hoặc liên hệ Hotline: 1900636060 để nhận báo giá cực sốc!
Bắt đầu hành trình của bạn ngay tại:
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

    # ── Comment footer ────────────────────────────────────────────────────────

    def _post_first_comment(
        self,
        page_id: str,
        page_token: str,
        post_id: str,
    ) -> dict | None:
        """
        Đăng _FIXED_FOOTER làm comment đầu tiên lên post vừa tạo.
        Dùng Page access token — cần pages_manage_engagement permission.
        Không raise khi thất bại để không block luồng chính.
        Bỏ qua với scheduled post (post_id rỗng hoặc chưa published).
        """
        if not post_id:
            return None
        try:
            resp = requests.post(
                f"{FB_API_BASE}/{post_id}/comments",
                data={"message": _FIXED_FOOTER, "access_token": page_token},
                timeout=15,
            )
            if resp.ok:
                comment_id = resp.json().get("id", "")
                logger.info("  → [Facebook] 💬 First comment: %s", comment_id)
                return resp.json()
            logger.warning(
                "  → [Facebook] ⚠️ First comment HTTP %d: %s",
                resp.status_code, resp.text[:200],
            )
            return None
        except Exception as exc:
            logger.warning("  → [Facebook] ⚠️ First comment exception: %s", exc)
            return None

    # ── Post đến 1 page ───────────────────────────────────────────────────────

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
            return self._post_multi_photo(page_id, page_token, image_urls, text, scheduled_at)
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

    def _upload_photo_unpublished(
        self, page_id: str, token: str, image_url: str
    ) -> str | None:
        payload = {"url": image_url, "published": "false", "access_token": token}
        for attempt in range(1, 4):
            resp = requests.post(f"{FB_API_BASE}/{page_id}/photos", data=payload, timeout=30)
            if resp.ok:
                return resp.json().get("id")
            if resp.status_code == 500 and attempt < 3:
                logger.warning(
                    "  → [Facebook] Upload ảnh HTTP 500 (transient), thử lại %d/3...", attempt
                )
                sleep(0.5)
                continue
            logger.warning("  → [Facebook] Upload ảnh thất bại: %s", resp.text[:200])
            return None
        return resp.json().get("id")

    def _post_multi_photo(
        self, page_id: str, token: str, image_urls: list[str],
        caption: str, scheduled_at: str | None
    ) -> dict:
        photo_ids = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            futures = [
                ex.submit(self._upload_photo_unpublished, page_id, token, url)
                for url in image_urls
            ]
            for f in futures:
                pid = f.result()
                if pid:
                    photo_ids.append(pid)

        if not photo_ids:
            logger.warning("  → [Facebook] Không upload được ảnh nào, fallback ảnh đơn")
            return self._post_photo(page_id, token, image_urls[0], caption, scheduled_at)

        payload: dict = {"message": caption, "access_token": token}
        for i, photo_id in enumerate(photo_ids):
            payload[f"attached_media[{i}]"] = f'{{"media_fbid":"{photo_id}"}}'
        if scheduled_at:
            payload["scheduled_publish_time"] = _to_unix(scheduled_at)
            payload["published"] = "false"

        for attempt in range(1, 4):
            resp = requests.post(f"{FB_API_BASE}/{page_id}/feed", data=payload, timeout=30)
            if resp.ok:
                return resp.json()
            if resp.status_code == 500 and attempt < 3:
                logger.warning(
                    "  → [Facebook] Feed post HTTP 500 (transient), thử lại %d/3...", attempt
                )
                sleep(0.5)
                continue
            logger.error("  → [Facebook] Multi-photo thất bại: HTTP %d | %s",
                         resp.status_code, resp.text[:500])
            resp.raise_for_status()
        return resp.json()

    def _post_photo(
        self, page_id: str, token: str, image_url: str,
        caption: str, scheduled_at: str | None
    ) -> dict:
        payload: dict = {"url": image_url, "caption": caption, "access_token": token}
        if scheduled_at:
            payload["scheduled_publish_time"] = _to_unix(scheduled_at)
            payload["published"] = False
        resp = requests.post(f"{FB_API_BASE}/{page_id}/photos", data=payload, timeout=30)
        if not resp.ok:
            logger.error("  → [Facebook] HTTP %d | Body: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    def _post_video(
        self, page_id: str, token: str, video_url: str,
        description: str, scheduled_at: str | None
    ) -> dict:
        payload: dict = {"file_url": video_url, "description": description, "access_token": token}
        if scheduled_at:
            payload["scheduled_publish_time"] = _to_unix(scheduled_at)
            payload["published"] = False
        resp = requests.post(f"{FB_API_BASE}/{page_id}/videos", data=payload, timeout=60)
        if not resp.ok:
            logger.error("  → [Facebook] HTTP %d | Body: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    # ── Post đến nhiều pages song song ───────────────────────────────────────

    def _post_one_page(
        self,
        page: dict,
        text: str,
        image_urls: list[str] | None,
        video_url: str | None,
        scheduled_at: str | None,
        page_texts: dict[str, str] | None,
    ) -> FacebookPostResult:
        """
        Đăng 1 page và tự động thêm footer làm first comment.
        Dùng chung cho post_to_all_pages và post_to_selected_pages.
        """
        pid   = page.get("id", "")
        token = page.get("access_token", "")
        name  = page.get("name", pid)
        try:
            caption = (page_texts or {}).get(pid, text)
            result  = self.post_to_page(pid, token, caption, image_urls, video_url, scheduled_at)
            post_id = result.get("id", "")
            logger.info("  → [Facebook] ✅ %s | post_id=%s", name, post_id)

            # Footer đăng làm comment — chỉ với bài publish ngay (không hẹn giờ)
            if not scheduled_at:
                self._post_first_comment(pid, token, post_id)

            return FacebookPostResult(
                page_id=pid, page_name=name,
                status="success", post_id=post_id,
            )
        except Exception as exc:
            logger.warning("  → [Facebook] ❌ %s — %s", name, exc)
            return FacebookPostResult(
                page_id=pid, page_name=name,
                status="error", error=str(exc),
            )

    def post_to_all_pages(
        self,
        text: str = "",
        image_urls: list[str] | None = None,
        video_url: str | None = None,
        scheduled_at: str | None = None,
        page_texts: dict[str, str] | None = None,
    ) -> list[FacebookPostResult]:
        """Đăng lên tất cả pages trong FACEBOOK_PAGES song song."""
        targets = self._config.pages
        if not targets:
            logger.warning("  → [Facebook] Không tìm thấy page nào trong FACEBOOK_PAGES")
            return []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            return list(ex.map(
                lambda page: self._post_one_page(
                    page, text, image_urls, video_url, scheduled_at, page_texts
                ),
                targets,
            ))

    def post_to_selected_pages(
        self,
        page_ids: list[str],
        text: str = "",
        image_urls: list[str] | None = None,
        video_url: str | None = None,
        scheduled_at: str | None = None,
        page_texts: dict[str, str] | None = None,
    ) -> list[FacebookPostResult]:
        """Đăng lên các pages được chọn theo page_ids."""
        targets = [p for p in self._config.pages if p.get("id") in page_ids]
        if not targets:
            logger.warning("  → [Facebook] Không tìm thấy page nào khớp page_ids: %s", page_ids)
            return []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            return list(ex.map(
                lambda page: self._post_one_page(
                    page, text, image_urls, video_url, scheduled_at, page_texts
                ),
                targets,
            ))