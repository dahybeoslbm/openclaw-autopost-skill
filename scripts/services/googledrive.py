"""
services/googledrive.py — Client gọi api.drive.article để lấy nội dung Google Docs.

Workflow:
    service = GoogleDriveService.from_config(cfg.googledrive)
    article = service.fetch_article("Du lịch Đà Nẵng")
    if article:
        # article.content      → HTML đã sanitize
        # article.content_blocks → list block có cấu trúc
        # article.title        → tên doc
        # article.keywords     → keywords tự động trích xuất
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json

import requests

from utils.logger import get_logger

logger = get_logger(__name__)


# ── Response model ───────────────────────────────────────────────────────────

@dataclass
class DriveArticle:
    """Kết quả trả về từ api.drive.article."""
    document_id:    str
    document_url:   str
    title:          str
    content:        str           # HTML đã sanitize
    content_blocks: list          # block có cấu trúc (heading, paragraph, image…)
    keywords:       list[str] = field(default_factory=list)
    modified_date:  str = ""

    # ── Helpers ──────────────────────────────────────────────────────────────

    def plain_text(self) -> str:
        import re
        return re.sub(r"<[^>]+>", " ", self.content).strip()

    def get_images(self) -> list[dict]:
        return [b for b in self.content_blocks if b.get("type") == "image"]

    def has_images(self) -> bool:
        return any(b.get("type") == "image" for b in self.content_blocks)

    def image_count(self) -> int:
        return sum(1 for b in self.content_blocks if b.get("type") == "image")


# ── Service ──────────────────────────────────────────────────────────────────

class GoogleDriveService:
    """
    Wrapper gọi api.drive.article.

    Args:
        api_url:  Base URL của PHP API, ví dụ http://localhost:8080
        timeout:  Giây chờ tối đa cho mỗi request
    """

    def __init__(self, api_url: str, timeout: int = 30):
        self._endpoint = api_url.rstrip("/") + "/api/v1/get-latest-article"
        self._timeout  = timeout

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, cfg) -> "GoogleDriveService":
        """Khởi tạo từ GoogleDriveAPIConfig."""
        return cls(api_url=cfg.api_url, timeout=cfg.timeout)

    # ── Public API ───────────────────────────────────────────────────────────

    def fetch_article(self, topic: str, language: str = "vi") -> DriveArticle | None:
        """
        Tìm và trả về Google Doc khớp topic.

        Returns:
            DriveArticle nếu tìm thấy, None nếu không có doc nào.

        Raises:
            RuntimeError: lỗi kết nối hoặc HTTP không phục hồi được.
        """
        logger.info("  → [Drive] Tìm doc cho topic: %s", topic)

        try:
            resp = requests.post(
                self._endpoint,
                json={"topic": topic, "language": language},
                headers={"Content-Type": "application/json"},
                timeout=self._timeout,
            )
            logger.info("  → [Drive] HTTP %d | URL: %s | body: %s",
            resp.status_code, self._endpoint,
            resp.text[:200])
        except requests.ConnectionError as exc:
            raise RuntimeError(
                f"Không thể kết nối api.drive.article ({self._endpoint}): {exc}"
            ) from exc
        except requests.Timeout:
            raise RuntimeError(
                f"api.drive.article timeout sau {self._timeout}s cho topic '{topic}'"
            )

        # 404 → không tìm thấy doc (không phải lỗi nghiêm trọng)
        if resp.status_code == 404:
            logger.warning("  → [Drive] Không tìm thấy doc nào cho '%s'", topic)
            return None

        # Lỗi HTTP khác
        if not resp.ok:
            raise RuntimeError(
                f"api.drive.article HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            raw_text = resp.text
            # Strip PHP deprecated warnings (HTML trước JSON)
            json_start = raw_text.find("{")
            if json_start > 0:
                raw_text = raw_text[json_start:]
            data = json.loads(raw_text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("api.drive.article trả về response không phải JSON") from exc
        
        if "error" in data:
            raise RuntimeError(f"api.drive.article error: {data['error']}")

        metadata = data.get("metadata") or {}
        article = DriveArticle(
            document_id    = data.get("document_id", ""),
            document_url   = data.get("document_url", ""),
            title          = data.get("title", topic),
            content        = data.get("content", ""),
            content_blocks = data.get("content_blocks") or [],
            keywords       = metadata.get("keywords") or [],
            modified_date  = metadata.get("modified_date", ""),
        )

        logger.info(
            "  → [Drive] Tìm thấy: \"%s\" (%s)",
            article.title, article.document_id
        )
        return article