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
from importlib import metadata
from importlib.metadata import metadata
import json

import requests

import re as _re
from urllib.parse import urlparse, parse_qs, unquote
from utils.logger import get_logger

logger = get_logger(__name__)

# CSS properties có ý nghĩa hiển thị thực — giữ lại
_KEEP_CSS = {
    "text-align",
    "color",
    "font-size",
    "font-weight",
    "font-style",
    "font-family",       # giữ nhưng lọc default + font không có trên web
    "text-decoration",
    "vertical-align",    # superscript/subscript trong bảng
    "width",             # chiều rộng ảnh/bảng
    "height",
}

# Giá trị default của WordPress theme — giữ lại cũng vô nghĩa
_SKIP_VALUES = {
    "text-align":      {"left", "start"},
    "color":           {"#000000", "#000", "black", "rgb(0,0,0)", "rgb(0, 0, 0)"},
    "font-size":       {"11pt", "12pt", "10pt"},   # body text default
    "font-weight":     {"400", "normal"},
    "font-style":      {"normal"},
    "text-decoration": {"none"},
    # font-family: bỏ qua Arial (Google Docs default) và các font hệ thống phổ biến
    # vì chúng không có ý nghĩa đặc biệt và thường xung đột với theme
    "font-family": {
        '"arial"', "arial",
        '"helvetica"', "helvetica",
        '"times new roman"', "times new roman",
        '"georgia"', "georgia",           # bỏ ghi chú dòng này nếu muốn giữ Georgia
        "sans-serif", "serif", "monospace",
        '"arial",sans-serif',
        '"helvetica neue",helvetica,sans-serif',
    },
}


def _filter_style(style_value: str) -> str:
    """
    Lọc style attribute: chỉ giữ lại properties có nghĩa thực.
    Trả về chuỗi style mới (rỗng nếu không còn gì).

    Ví dụ:
        input:  "color:#000000;text-align:center;orphans:2;border-top:0pt"
        output: "text-align:center"
    """
    kept = []
    for part in style_value.split(";"):
        part = part.strip()
        if ":" not in part:
            continue
        prop, _, val = part.partition(":")
        prop = prop.strip().lower()
        val  = val.strip().lower().replace(" ", "")

        if prop not in _KEEP_CSS:
            continue

        # Bỏ giá trị default của theme
        skip_set = _SKIP_VALUES.get(prop, set())
        if val in skip_set:
            continue

        kept.append(f"{prop}:{val}")

    return ";".join(kept)

def _replace_style_attr(m: _re.Match) -> str:
    """Callback cho re.sub: lọc style attribute trong một thẻ HTML."""
    quote   = m.group(1)   # " hoặc '
    content = m.group(2)
    new_val = _filter_style(content)
    if not new_val:
        return ""          # xóa hẳn attribute
    return f' style={quote}{new_val}{quote}'

def _sanitize_gdoc_html(html: str) -> str:
    """
    Dọn dẹp HTML thô từ Google Docs API.

    Bước 1 — Fix backslash-escaped quotes (lỗi PHP serialize)
    Bước 2 — Unwrap Google redirect URLs
    Bước 3 — Smart style filter (giữ lại text-align, color lạ, v.v.)
    Bước 4 — Xóa id dạng h.xxx / kix.xxx (Google internal anchor)
    Bước 5 — Xóa dir="ltr/rtl"
    """

    # 1. Fix \" ───────────────────────────────────────────────────────────────
    html = html.replace('\\"', '"')

    # 2. Unwrap Google redirect URLs ──────────────────────────────────────────
    def _unwrap(m: _re.Match) -> str:
        try:
            qs = parse_qs(urlparse(m.group(0)).query)
            return unquote(qs.get("q", [m.group(0)])[0])
        except Exception:
            return m.group(0)

    html = _re.sub(
        r'https?://(?:www\.)?google\.com/url\?[^"\'>\s]+',
        _unwrap,
        html,
    )

    # 3. Smart style filter ───────────────────────────────────────────────────
    #    Xử lý cả style="..." và style='...'
    html = _re.sub(r'\s+style=(["\'])(.*?)\1', _replace_style_attr, html, flags=_re.DOTALL)

    # 4. Xóa Google Docs internal id ─────────────────────────────────────────
    html = _re.sub(r'\s+id="(?:h\.|kix\.)[^"]*"', "", html)

    # 5. Xóa dir attribute ────────────────────────────────────────────────────
    html = _re.sub(r'\s+dir="(?:ltr|rtl)"', "", html)
    return html


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
        raw_content = data.get("content", "")

        # Rewrite URL ảnh localhost → host thực của GDRIVE_API_URL
        # VD: http://localhost:8080 → http://host.docker.internal:8080
        from urllib.parse import urlparse
        api_base = urlparse(self._endpoint)
        actual_base = f"{api_base.scheme}://{api_base.netloc}"  # http://host.docker.internal:8080
        raw_content = raw_content.replace("http://localhost:", f"{api_base.scheme}://localhost:") # no-op nếu không match
        # Replace mọi biến thể localhost với actual_base
        import re as _re
        raw_content = _re.sub(
            r'https?://localhost(:\d+)?',
            actual_base,
            raw_content,
        )

        article = DriveArticle(
            document_id    = data.get("document_id", ""),
            document_url   = data.get("document_url", ""),
            title          = data.get("title", topic),
            content        = _sanitize_gdoc_html(raw_content),
            content_blocks = data.get("content_blocks") or [],
            keywords       = metadata.get("keywords") or [],
            modified_date  = metadata.get("modified_date", ""),
        )

        logger.info(
            "  → [Drive] Tìm thấy: \"%s\" (%s)",
            article.title, article.document_id
        )
        return article
    
    def list_articles(
        self, topic: str, language: str = "vi", limit: int = 10
    ) -> list[dict]:
            """
            Tìm danh sách doc theo topic — chỉ metadata, không export HTML.
            Trả về list candidates (rỗng nếu không tìm thấy).
            """
            list_endpoint = self._endpoint.replace("get-latest-article", "list-articles")
            logger.info("  → [Drive] list_articles topic='%s'", topic)

            try:
                resp = requests.post(
                    list_endpoint,
                    json={"topic": topic, "language": language, "limit": limit},
                    headers={"Content-Type": "application/json"},
                    timeout=self._timeout,
                )
            except requests.ConnectionError as exc:
                raise RuntimeError(f"Không kết nối được api.drive.article: {exc}") from exc
            except requests.Timeout:
                raise RuntimeError(f"api.drive.article timeout sau {self._timeout}s")

            if resp.status_code == 404:
                return []
            if not resp.ok:
                raise RuntimeError(f"api.drive.article HTTP {resp.status_code}: {resp.text[:300]}")

            try:
                raw = resp.text
                start = raw.find("{")
                if start > 0:
                    raw = raw[start:]
                data = json.loads(raw)
            except (ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("Response không phải JSON") from exc

            return data.get("candidates", [])


    def fetch_article_by_id(
        self, document_id: str, language: str = "vi"
    ) -> DriveArticle | None:
        """
        Fetch full content bằng document_id cụ thể.
        Bỏ qua bước search — dùng sau khi user đã chọn từ list.
        """
        logger.info("  → [Drive] fetch_article_by_id: %s", document_id)

        try:
            resp = requests.post(
                self._endpoint,
                json={"document_id": document_id, "language": language},
                headers={"Content-Type": "application/json"},
                timeout=self._timeout,
            )
        except requests.ConnectionError as exc:
            raise RuntimeError(f"Không kết nối được api.drive.article: {exc}") from exc
        except requests.Timeout:
            raise RuntimeError(f"api.drive.article timeout sau {self._timeout}s")

        if resp.status_code == 404:
            return None
        if not resp.ok:
            raise RuntimeError(f"api.drive.article HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            raw = resp.text
            start = raw.find("{")
            if start > 0:
                raw = raw[start:]
            data = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Response không phải JSON") from exc

        metadata    = data.get("metadata") or {}
        raw_content = data.get("content", "")

        import re as _re
        from urllib.parse import urlparse as _up
        api_base    = _up(self._endpoint)
        actual_base = f"{api_base.scheme}://{api_base.netloc}"
        raw_content = _re.sub(r'https?://localhost(:\d+)?', actual_base, raw_content)

        return DriveArticle(
            document_id    = data.get("document_id", document_id),
            document_url   = data.get("document_url", ""),
            title          = data.get("title", ""),
            content        = _sanitize_gdoc_html(raw_content),
            content_blocks = data.get("content_blocks") or [],
            keywords       = metadata.get("keywords") or [],
            modified_date  = metadata.get("modified_date", ""),
        )