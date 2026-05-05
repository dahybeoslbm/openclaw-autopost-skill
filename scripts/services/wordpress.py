"""
services/wordpress.py — Toàn bộ tương tác với WordPress REST API.
Không import gì từ blogger.py để có thể dùng độc lập.
"""
import base64
import os

import requests
import markdown as md

from config import WordPressConfig
from utils.logger import get_logger

logger = get_logger(__name__)

 
class WordPressService:
    def __init__(self, config: WordPressConfig):
        self._config = config
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        """Tạo session với auth header dùng chung cho mọi request."""
        session = requests.Session()
        credentials = f"{self._config.username}:{self._config.app_password}"
        token = base64.b64encode(credentials.encode()).decode("utf-8")
        session.headers.update({
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        })
        return session

    # ── Media ───────────────────────────────────────────────

    def upload_image(self, image_path: str) -> tuple[int, str]  | None:
        """
        Upload ảnh local lên WP Media Library.
        Trả về media ID và URL nếu thành công, None nếu thất bại.
        Bỏ qua ảnh remote URL (http...).
        """
        if not image_path or image_path.startswith("http"):
            return None

        if not os.path.exists(image_path):
            logger.warning("  → File ảnh không tồn tại: %s", image_path)
            return None

        logger.info("  → Upload ảnh: %s", os.path.basename(image_path))
        try:
            filename = os.path.basename(image_path)
            with open(image_path, "rb") as f:
                img_data = f.read()

            # Tạo header riêng cho multipart (không dùng Content-Type: json)
            headers = {
                k: v for k, v in self._session.headers.items()
                if k != "Content-Type"
            }

            resp = requests.post(
                url=f"{self._config.api_base}/media",
                headers=headers,
                files={"file": (filename, img_data, "image/png")},
                data={"title": filename},
                timeout=self._config.timeout,
            )

            if resp.status_code == 201:
                media_id = resp.json()["id"]
                media_url = resp.json()["source_url"]
                logger.info("  → Upload OK, media ID: %d", media_id)
                return media_id, media_url

            logger.warning("  → Upload thất bại: %d %s", resp.status_code, resp.text[:100])

        except Exception as e:
            logger.error("  → Upload error: %s", e)

        return None

    # ── Taxonomy ────────────────────────────────────────────

    def get_or_create_category(self, name: str) -> int | None:
        """Tìm category theo tên, tạo mới nếu chưa có. Trả về ID."""
        try:
            resp = self._session.get(
                url=f"{self._config.api_base}/categories",
                params={"search": name, "_fields": "id,name"},
                timeout=self._config.timeout,
            )
            results = resp.json()
            if results:
                return results[0]["id"]

            # Chưa có → tạo mới
            resp = self._session.post(
                url=f"{self._config.api_base}/categories",
                json={"name": name},
                timeout=self._config.timeout,
            )
            if resp.status_code == 201:
                return resp.json()["id"]

        except Exception as e:
            logger.error("  → get_or_create_category error: %s", e)

        return None

    def get_or_create_tag(self, name: str) -> int | None:
        """Tìm tag theo tên, tạo mới nếu chưa có. Trả về ID."""
        try:
            resp = self._session.get(
                url=f"{self._config.api_base}/tags",
                params={"search": name, "_fields": "id,name"},
                timeout=self._config.timeout,
            )
            results = resp.json()
            if results:
                return results[0]["id"]

            resp = self._session.post(
                url=f"{self._config.api_base}/tags",
                json={"name": name},
                timeout=self._config.timeout,
            )
            if resp.status_code == 201:
                return resp.json()["id"]

        except Exception as e:
            logger.error("  → get_or_create_tag error: %s", e)

        return None

    # ── Duplicate check ─────────────────────────────────────

    def post_exists(self, title: str) -> bool:
        """Kiểm tra bài viết có tiêu đề này đã tồn tại chưa."""
        try:
            resp = self._session.get(
                url=f"{self._config.api_base}/posts",
                params={
                    "search": title,
                    "status": "any",
                    "_fields": "id,title",
                },
                timeout=self._config.timeout,
            )
            for post in resp.json():
                if post["title"]["rendered"].lower() == title.lower():
                    logger.warning("  → Bài đã tồn tại, ID: %d", post["id"])
                    return True
        except Exception as e:
            logger.error("  → post_exists error: %s", e)
        return False

    # ── Publish ─────────────────────────────────────────────

    def publish(
        self,
        article_data: dict,
        image_files: list[str],
        schedule_time: str = "",
        category_names: list[str] | None = None,
        tag_names: list[str] | None = None,
    ) -> dict | None:
        """
        Đăng bài lên WordPress.
        Trả về response dict nếu thành công, None nếu thất bại.
        """
        if not self._config.is_valid:
            logger.warning("  → WP credentials chưa cấu hình. Bỏ qua đăng WP.")
            return None

        logger.info("  → Chuẩn bị đăng lên WordPress...")

        title   = article_data.get("seo_title", "")
        excerpt = article_data.get("excerpt",   "")
        content = article_data.get("content",   "")
        
        # 1. HTML
        html_content = article_data.get("content_html", "")

        # 2. Upload ảnh → lấy featured image ID
        featured_media_id = None
        # Map local path → WP URL để thay thế trong nội dung
        path_to_url: dict[str, str] = {}

        for img_path in image_files:
            result = self.upload_image(img_path)
            if result:
                media_id, media_url = result
                path_to_url[img_path] = media_url
                if not featured_media_id:
                    featured_media_id = media_id

        # Thay thế src ảnh local trong content_html bằng URL thực của WP
        for local_path, wp_url in path_to_url.items():
            # Gemini thường tạo src kiểu "./filename" hoặc "filename"
            filename = os.path.basename(local_path)
            html_content = html_content.replace(f"./{filename}", wp_url)
            html_content = html_content.replace(filename, wp_url)   # fallback không có ./
            html_content = html_content.replace(local_path, wp_url) # fallback đường dẫn đầy đủ

        # 3. Resolve categories & tags
        category_ids = [
            cid for name in (category_names or ["Du Lịch"])
            if (cid := self.get_or_create_category(name))
        ]
        tag_ids = [
            tid for name in (tag_names or [])
            if (tid := self.get_or_create_tag(name))
        ]

        # 4. Xác định status
        wp_status = "draft" if schedule_time and schedule_time != "Hôm nay" else "publish"

        # 5. Build payload
        payload: dict = {
            "title"     : title,
            "content"   : html_content,
            "status"    : wp_status,
            "categories": category_ids,
            "tags"      : tag_ids,
        }
        if featured_media_id:
            payload["featured_media"] = featured_media_id

        # 6. Gửi request
        try:
            resp = self._session.post(
                url=f"{self._config.api_base}/posts",
                json=payload,
                timeout=self._config.timeout,
            )

            if resp.status_code == 201:
                data = resp.json()
                logger.info("  → ✅ Đăng WP thành công! ID=%d | %s", data["id"], data["link"])
                return data

            logger.error(
                "  → ❌ Đăng WP thất bại: %d | %s",
                resp.status_code,
                resp.json().get("message", ""),
            )

        except requests.Timeout:
            logger.error("  → WP request timeout")
        except Exception as e:
            logger.error("  → WP publish error: %s", e)

        return None
