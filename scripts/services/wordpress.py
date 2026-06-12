"""
services/wordpress.py — Toàn bộ tương tác với WordPress REST API.
Không import gì từ blogger.py để có thể dùng độc lập.
"""
import base64
import os

import requests
import markdown as md
from bs4 import BeautifulSoup

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

    # ── Media ────────────────────────────────────────────────────────────────

    def upload_image(self, image_path: str) -> tuple[int, str] | None:
        """
        Upload ảnh lên WP Media Library.
        - Nếu là file local → đọc trực tiếp
        - Nếu là URL http  → download về rồi upload
        Trả về (media_id, media_url) hoặc None nếu thất bại.
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

            import mimetypes
            mime_type, _ = mimetypes.guess_type(image_path)
            if not mime_type:
                mime_type = "image/jpeg"

            headers = {
                k: v for k, v in self._session.headers.items()
                if k != "Content-Type"
            }

            resp = requests.post(
                url=f"{self._config.api_base}/media",
                headers=headers,
                files={"file": (filename, img_data, mime_type)},
                data={"title": filename},
                timeout=self._config.timeout,
            )

            if resp.status_code == 201:
                media_id  = resp.json()["id"]
                media_url = resp.json()["source_url"]
                logger.info("  → Upload OK, media ID: %d", media_id)
                return media_id, media_url

            logger.warning("  → Upload thất bại: %d %s", resp.status_code, resp.text[:100])

        except Exception as e:
            logger.error("  → Upload error: %s", e)

        return None

    def upload_image_from_url(self, url: str) -> tuple[int, str] | None:
        """
        Download ảnh từ URL (VD: localhost:8080/api/...) rồi upload lên WP Media.
        Trả về (media_id, wp_url) nếu thành công, None nếu thất bại.
        """
        import tempfile
        try:
            logger.info("  → Download ảnh từ: %s", url[:60])
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()

            # Kiểm tra định dạng thực tế bằng magic bytes thay vì dựa vào header
            ext = ".jpg"
            header_bytes = resp.content[:12]
            if header_bytes.startswith(b"\xff\xd8\xff"):
                ext = ".jpg"
            elif header_bytes.startswith(b"\x89PNG"):
                ext = ".png"
            elif header_bytes.startswith(b"GIF8"):
                ext = ".gif"
            elif header_bytes.startswith(b"RIFF") and header_bytes[8:12] == b"WEBP":
                ext = ".webp"
            else:
                # Fallback dùng header nếu không nhận diện được magic byte
                content_type = resp.headers.get("Content-Type", "").split(";")[0].lower()
                if content_type == "image/webp":
                    ext = ".webp"
                elif content_type in ("image/jpeg", "image/jpg"):
                    ext = ".jpg"
                elif content_type == "image/png":
                    ext = ".png"
                elif content_type == "image/gif":
                    ext = ".gif"

            fd, tmp_path = tempfile.mkstemp(suffix=ext)
            with os.fdopen(fd, "wb") as f:
                f.write(resp.content)

            result = self.upload_image(tmp_path)

            try:
                os.unlink(tmp_path)
            except OSError:
                pass

            return result

        except Exception as e:
            logger.error("  → upload_image_from_url error: %s", e)
            return None

    def upload_and_replace_html_images(self, html_content: str) -> tuple[str, int | None]:
        """
        Duyệt qua tất cả thẻ <img> trong HTML, upload các link ngoài (Google Drive, v.v.)
        lên WordPress Media và thay thế thuộc tính src.
        Trả về HTML đã cập nhật và ID của ảnh đầu tiên được upload thành công (để làm ảnh bìa).
        """
        if not html_content:
            return html_content, None

        soup = BeautifulSoup(html_content, "html.parser")
        images = soup.find_all("img")
        if not images:
            return html_content, None

        wp_base = self._config.site_url.rstrip("/")
        featured_media_id = None
        imgs_to_upload = []

        for img in images:
            src = img.get("src")
            if not src or src.startswith("data:"):
                continue
            if not src.startswith(wp_base) and src.startswith("http"):
                imgs_to_upload.append((img, src))

        if not imgs_to_upload:
            return html_content, None

        import concurrent.futures
        logger.info("  → Upload %d ảnh trung gian lên WordPress...", len(imgs_to_upload))

        def _upload_one(item: tuple) -> tuple | None:
            img, src = item
            logger.info("  → Tải ảnh: %s", src[:60])
            res = self.upload_image_from_url(src)
            if res:
                return (img, res[0], res[1])
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            for outcome in ex.map(_upload_one, imgs_to_upload):
                if outcome:
                    img, media_id, media_url = outcome
                    img["src"] = media_url
                    logger.info("  → ✅ Ảnh → WP Media: %s", media_url)
                    if not featured_media_id:
                        featured_media_id = media_id

        return str(soup), featured_media_id

    # ── Taxonomy ─────────────────────────────────────────────────────────────

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

    # ── Duplicate check ───────────────────────────────────────────────────────

    def check_duplicate(self, seo_title: str) -> list[dict]:
        """
        Kiểm tra bài viết trùng tiêu đề trên WP (mọi status).
        Trả về list các bài trùng: [{"id": 123, "title": "...", "status": "draft", "edit_url": "..."}]
        """
        params = {
            "search":   seo_title,
            "status":   "publish,future,draft,pending,private",
            "per_page": 20,
            "_fields":  "id,title,status,link",
        }
        try:
            resp = requests.get(
                f"{self._config.api_base}/posts",
                params=params,
                auth=(self._config.username, self._config.app_password),
                timeout=self._config.timeout,
            )
            resp.raise_for_status()
            posts = resp.json()
        except Exception as exc:
            logger.warning("  → [WP] check_duplicate thất bại: %s", exc)
            return []

        title_lower = seo_title.strip().lower()
        matched = []
        for p in posts:
            wp_title = (p.get("title") or {}).get("rendered", "")
            # strip HTML entities nếu có
            import html as _html
            wp_title_clean = _html.unescape(wp_title).strip().lower()
            if wp_title_clean == title_lower:
                edit_url = (
                    f"{self._config.site_url.rstrip('/')}/wp-admin/post.php"
                    f"?post={p['id']}&action=edit"
                )
                matched.append({
                    "id":       p["id"],
                    "title":    wp_title,
                    "status":   p["status"],
                    "edit_url": edit_url,
                })
        return matched

    
    # ── Publish ───────────────────────────────────────────────────────────────

    def publish(
        self,
        article_data: dict,
        image_files: list[str],
        schedule_time: str = "",
        save_as_draft: bool = False,
        category_names: list[str] | None = None,
        tag_names: list[str] | None = None,
    ) -> dict | None:
        """
        Đăng bài lên WordPress.

        Thứ tự ưu tiên status:
          schedule_time → "future"  (hẹn giờ)
          save_as_draft → "draft"   (nháp để human review + thay ảnh)
          default       → "publish"

        Trả về response dict nếu thành công, None nếu thất bại.
        """
        if not self._config.is_valid:
            logger.warning("  → WP credentials chưa cấu hình. Bỏ qua đăng WP.")
            return None

        logger.info("  → Chuẩn bị đăng lên WordPress...")

        title   = article_data.get("seo_title", "")
        excerpt = article_data.get("excerpt", "")
        content = article_data.get("content", "")

        # 1. HTML content
        html_content = article_data.get("content_html", "")

        # 2. Upload ảnh → lấy featured image ID
        featured_media_id = None
        wp_base = self._config.site_url.rstrip("/")

        # 2a. Upload ảnh local (từ AI render — nếu có)
        for img_path in image_files:
            result = self.upload_image(img_path)
            if result:
                media_id, media_url = result
                filename = os.path.basename(img_path)
                html_content = html_content.replace(f"./{filename}", media_url)
                html_content = html_content.replace(filename, media_url)
                html_content = html_content.replace(img_path, media_url)
                if not featured_media_id:
                    featured_media_id = media_id

        # 2b. Upload ảnh nhúng trong HTML (từ Google Docs API — URL ngoài)
        html_content, embedded_featured_id = self.upload_and_replace_html_images(html_content)
        if embedded_featured_id and not featured_media_id:
            featured_media_id = embedded_featured_id

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
        if save_as_draft:
            wp_status     = "draft"
            payload_extra = {"date_gmt": schedule_time} if schedule_time else {}
        elif schedule_time:
            wp_status     = "future"
            payload_extra = {"date_gmt": schedule_time}
        else:
            wp_status     = "publish"
            payload_extra = {}

        # 5. Build payload
        payload: dict = {
            "title":      title,
            "content":    html_content,
            "status":     wp_status,
            "categories": category_ids,
            "tags":       tag_ids,
            **payload_extra,
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
                logger.info("  → ✅ Đăng WP thành công! ID=%d | status=%s | %s",
                            data["id"], data["status"], data.get("link", ""))
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