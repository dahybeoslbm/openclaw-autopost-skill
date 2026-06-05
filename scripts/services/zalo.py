"""
services/zalo.py — Đăng bài viết (Article) lên Zalo OA qua PHP proxy API.

Luồng hoạt động:
  Python → HTTP POST → PHP epZaloPost (SuiteCRM) → Zalo OpenAPI

PHP proxy xử lý:
  - Auth bằng Api-Key header
  - Tự động lấy/refresh access_token từ DB
  - Gọi Zalo OpenAPI v2.0/article/create

Cấu hình .env:
  ZALO_API_URL=http://localhost:8001/index.php?entryPoint=epZaloPost
  ZALO_API_KEY=
  ZALO_APP_ID=
"""
import requests
from bs4 import BeautifulSoup

from config import ZaloConfig
from utils.logger import get_logger
from utils.models import ZaloPostResult

logger = get_logger(__name__)


# ── Body Block builders ──────────────────────────────────────────────────────

def build_text_block(content: str) -> dict:
    """Tạo block text cho body bài viết Zalo."""
    return {"type": "text", "content": content}


def build_image_block(url: str, caption: str = "") -> dict:
    """Tạo block image cho body bài viết Zalo (tuỳ chọn caption)."""
    block: dict = {"type": "image", "url": url}
    if caption:
        block["caption"] = caption
    return block


# ── Service ──────────────────────────────────────────────────────────────────

class ZaloService:
    """
    Client gọi PHP proxy để đăng bài viết lên Zalo OA.

    Không gọi trực tiếp Zalo OpenAPI — mọi xác thực (access_token)
    do PHP proxy quản lý. Python chỉ cần truyền app_id và nội dung bài.
    """

    def __init__(self, config: ZaloConfig):
        self._config = config
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        """Tạo session với Api-Key header dùng chung cho mọi request."""
        session = requests.Session()
        session.headers.update({
            "Api-Key": self._config.api_key,
            "Content-Type": "application/json",
        })
        return session

    # ── Core: gọi PHP proxy ──────────────────────────────────────────────────

    def _call_proxy(self, method: str, params: dict) -> dict:
        """
        Gửi request tới PHP epZaloPost endpoint.

        Args:
            method: Tên method PHP xử lý (createArticle, uploadVideoArticle, ...)
            params: Tham số truyền vào — không cần truyền app_id (tự thêm).

        Returns:
            dict response từ PHP proxy: {"status": 0|1, "message": "...", "data": ...}
        """
        payload = {
            "method": method,
            "params": {
                "app_id": self._config.app_id,
                **params,
            },
        }

        try:
            resp = self._session.post(
                self._config.api_url,
                json=payload,
                timeout=self._config.timeout,
            )
            resp.raise_for_status()
            return resp.json()

        except requests.Timeout:
            logger.error("  → [Zalo] Request timeout (%ds)", self._config.timeout)
            return {"status": 0, "message": "Request timeout"}
        except requests.RequestException as exc:
            logger.error("  → [Zalo] HTTP error: %s", exc)
            return {"status": 0, "message": f"HTTP error: {exc}"}
        except ValueError:
            logger.error("  → [Zalo] Response không phải JSON: %s", resp.text[:200])
            return {"status": 0, "message": "Invalid JSON response"}

    # ── Public API ───────────────────────────────────────────────────────────

    def create_article(
        self,
        title: str,
        body: list[dict],
        *,
        article_type: str = "normal",
        author: str = "",
        description: str = "",
        cover_photo_url: str = "",
        cover_status: str = "show",
        status: str = "show",
        comment: str = "show",
        action_link: str = "",
        action_text: str = "",
    ) -> ZaloPostResult:
        """
        Tạo bài viết (article) trên Zalo OA.

        Args:
            title:           Tiêu đề bài viết (bắt buộc).
            body:            Danh sách body blocks (dùng build_text_block / build_image_block).
            article_type:    Loại bài: "normal" (mặc định) hoặc "video".
            author:          Tên tác giả hiển thị.
            description:     Mô tả ngắn cho bài viết.
            cover_photo_url: URL ảnh cover (dùng cho type=normal).
            cover_status:    Hiện/ẩn cover: "show" | "hide".
            status:          Hiện/ẩn bài viết: "show" | "hide".
            comment:         Cho phép comment: "show" | "hide".
            action_link:     URL khi người dùng bấm vào CTA.
            action_text:     Text hiển thị trên nút CTA (dưới 50 ký tự).

        Returns:
            ZaloPostResult chứa trạng thái và thông tin bài viết.
        """
        if not title:
            return ZaloPostResult(
                app_id=self._config.app_id,
                status="error",
                error="Thiếu tiêu đề bài viết (title)",
            )

        if not body:
            return ZaloPostResult(
                app_id=self._config.app_id,
                status="error",
                error="Thiếu nội dung bài viết (body)",
            )

        params: dict = {
            "type": article_type,
            "title": title,
            "body": body,
            "status": status,
            "comment": comment,
        }

        if author:
            params["author"] = author
        if description:
            params["description"] = description

        # Cover (chỉ dùng cho type=normal)
        if cover_photo_url and article_type == "normal":
            params["cover"] = {
                "cover_type": "photo",
                "photo_url": cover_photo_url,
                "status": cover_status,
            }

        if action_link and action_text:
            body.append({
                "type": "text",
                "content": f"\n\n👉 {action_text}: {action_link}"
            })

        response = self._call_proxy("createArticle", params)

        if response.get("status") == 1:
            token = (response.get("data") or {}).get("token", "")
            logger.info(
                "  → [Zalo] ✅ Tạo bài viết thành công | token=%s",
                token or "(none)",
            )
            return ZaloPostResult(
                app_id=self._config.app_id,
                status="success",
                article_token=token,
            )

        error_msg = response.get("message", "Unknown error")
        logger.warning("  → [Zalo] ❌ Tạo bài viết thất bại: %s", error_msg)
        return ZaloPostResult(
            app_id=self._config.app_id,
            status="error",
            error=error_msg,
        )

    # ── Convenience: đăng bài từ article_data dict ───────────────────────────

    def publish_from_article_data(
        self,
        article_data: dict,
        cover_photo_url: str = "",
        author: str = "Hệ thống Booking",
    ) -> ZaloPostResult:
        """
        Đăng bài lên Zalo từ article_data dict (cùng format với WP publish).

        Tự động chuyển content_html → body blocks dạng text.
        Ưu tiên lấy ảnh đầu tiên từ nội dung làm cover nếu không truyền.

        Args:
            article_data: Dict chứa seo_title, content_html, meta_description, ...
            cover_photo_url: URL ảnh cover (tuỳ chọn).
            author: Tên tác giả.

        Returns:
            ZaloPostResult
        """
        title = article_data.get("seo_title", "")
        description = article_data.get("meta_description", "")
        content = article_data.get("content_html", "")

        # Chuyển HTML content thành body blocks (text thuần)
        body = self._html_to_body_blocks(content)

        return self.create_article(
            title=title,
            body=body,
            author=author,
            description=description,
            cover_photo_url=cover_photo_url,
            action_text=article_data.get("zalo_cta_text", ""),
            action_link=article_data.get("zalo_cta_link", ""),
            status="hide",       # Mặc định ẩn, duyệt thủ công trên Zalo OA
            comment="show",
        )

    # ── Private helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _html_to_body_blocks(html_content: str) -> list[dict]:
        """
        Chuyển HTML content thành danh sách body blocks cho Zalo.
        """

        if not html_content:
            return []

        blocks: list[dict] = []
        soup = BeautifulSoup(html_content, "html.parser")

        for element in soup.descendants:
            if getattr(element, 'name', None) == 'img':
                src = element.get('src')
                if src:
                    blocks.append(build_image_block(src, element.get('alt', '')))
            elif isinstance(element, str):
                text = element.strip()
                if text:
                    # Gộp text vào block cuối cùng nếu nó là text block
                    if blocks and blocks[-1].get("type") == "text":
                        blocks[-1]["content"] += " " + text
                    else:
                        blocks.append(build_text_block(text))

        return blocks or [build_text_block("(Nội dung trống)")]
