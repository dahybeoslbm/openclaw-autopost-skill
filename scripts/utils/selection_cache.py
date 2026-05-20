"""
utils/selection_cache.py — Lưu trữ danh sách candidates chờ user chọn.

Key   : SHA256(chat_id + ":" + topic)
Value : { candidates, platform, schedule, topic }
TTL   : 24 giờ

SQLite lưu tại OUTPUT_DIR/selection_cache.db
Persist qua Docker restart — user mất mạng 23h sau vẫn dùng được.
"""
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass

_TTL_SECONDS = 24 * 60 * 60  # 24 giờ


def _db_path() -> str:
    base = os.environ.get("OUTPUT_DIR", "/app/output")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "selection_cache.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute("""
        CREATE TABLE IF NOT EXISTS selection_cache (
            cache_key  TEXT PRIMARY KEY,
            payload    TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn


def _make_key(chat_id: str, topic: str) -> str:
    raw = f"{chat_id}:{topic.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class PendingSelection:
    candidates: list[dict]
    platform:   list[str]
    schedule:   str
    topic:      str


def save_pending(chat_id: str, topic: str, pending: PendingSelection) -> None:
    """Lưu candidates vào cache, TTL 24h."""
    key     = _make_key(chat_id, topic)
    payload = json.dumps({
        "candidates": pending.candidates,
        "platform":   pending.platform,
        "schedule":   pending.schedule,
        "topic":      pending.topic,
    }, ensure_ascii=False)
    expires_at = int(time.time()) + _TTL_SECONDS

    with _get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO selection_cache (cache_key, payload, expires_at)
            VALUES (?, ?, ?)
        """, (key, payload, expires_at))


def load_any_pending(chat_id: str) -> PendingSelection | None:
    """
    Tìm bất kỳ pending selection còn hạn nào.
    Trả về cái được tạo gần nhất (expires_at lớn nhất).
    Dùng khi user gõ "1" mà không rõ topic.
    """
    now = int(time.time())
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT payload, expires_at FROM selection_cache WHERE expires_at > ? ORDER BY expires_at DESC LIMIT 1",
            (now,)
        ).fetchall()

    if not rows:
        return None

    data = json.loads(rows[0][0])
    return PendingSelection(
        candidates = data["candidates"],
        platform   = data["platform"],
        schedule   = data["schedule"],
        topic      = data["topic"],
    )


def delete_pending(chat_id: str, topic: str) -> None:
    """Xoá cache sau khi user đã chọn xong."""
    key = _make_key(chat_id, topic)
    with _get_conn() as conn:
        conn.execute("DELETE FROM selection_cache WHERE cache_key = ?", (key,))


def delete_all_pending() -> None:
    """Xoá toàn bộ cache — dùng khi user gõ 'huỷ'."""
    with _get_conn() as conn:
        conn.execute("DELETE FROM selection_cache")


def purge_expired() -> int:
    """Dọn bản ghi hết hạn. Gọi mỗi lần startup."""
    with _get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM selection_cache WHERE expires_at <= ?",
            (int(time.time()),)
        )
        return cursor.rowcount
    

@dataclass
class PendingPageSelection:
    pages:         list[dict]   # list pages từ FACEBOOK_PAGES env
    topic:         str
    platforms:     list[str]
    schedule:      str          # ISO 8601 hoặc ""
    article_id:    str          # document_id đã fetch
    article_title: str
    article_data:  dict | None = None 
    selected_wp_site_urls: list[str] | None = None
    
# Key prefix riêng để tránh xung đột với selection_cache của bài viết
_PAGE_KEY_PREFIX = "page_sel:"

def _make_page_key(chat_id: str) -> str:
    """Một chat_id chỉ có 1 pending page selection tại một thời điểm."""
    raw = f"{_PAGE_KEY_PREFIX}{chat_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def save_pending_pages(chat_id: str, pending: PendingPageSelection) -> None:
    """Lưu page selection vào cache, TTL 24h (dùng chung bảng selection_cache)."""
    key     = _make_page_key(chat_id)
    payload = json.dumps({
        "pages":         pending.pages,
        "topic":         pending.topic,
        "platforms":     pending.platforms,
        "schedule":      pending.schedule,
        "article_id":    pending.article_id,
        "article_title": pending.article_title,
        "article_data":  pending.article_data,
        "selected_wp_site_urls": pending.selected_wp_site_urls,
        "_type":         "page_selection",   # phân biệt với article selection
    }, ensure_ascii=False)
    expires_at = int(time.time()) + _TTL_SECONDS

    with _get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO selection_cache (cache_key, payload, expires_at)
            VALUES (?, ?, ?)
        """, (key, payload, expires_at))


def load_pending_pages(chat_id: str) -> PendingPageSelection | None:
    """Load pending page selection còn hạn."""
    key = _make_page_key(chat_id)
    now = int(time.time())
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM selection_cache WHERE cache_key = ? AND expires_at > ?",
            (key, now)
        ).fetchone()

    if not row:
        return None

    data = json.loads(row[0])
    if data.get("_type") != "page_selection":
        return None

    return PendingPageSelection(
        pages         = data["pages"],
        topic         = data["topic"],
        platforms     = data.get("platforms") or [data.get("platform", "facebook")],  # backward-compat
        schedule      = data["schedule"],
        article_id    = data["article_id"],
        article_title = data["article_title"],
        article_data  = data.get("article_data"), 
        selected_wp_site_urls = data.get("selected_wp_site_urls"),
    )


def delete_pending_pages(chat_id: str) -> None:
    """Xoá page selection sau khi user đã chọn xong."""
    key = _make_page_key(chat_id)
    with _get_conn() as conn:
        conn.execute("DELETE FROM selection_cache WHERE cache_key = ?", (key,))
        

@dataclass
class PendingWPSiteSelection:
    sites:         list[dict]   # [{"url": site_url}, ...]
    topic:         str
    platforms:     list[str]
    schedule:      str
    article_id:    str
    article_title: str
    article_data:  dict | None = None 
    selected_page_ids: list[str] | None = None

_WP_SITE_KEY_PREFIX = "wp_site_sel:"

def _make_wp_site_key(chat_id: str) -> str:
    raw = f"{_WP_SITE_KEY_PREFIX}{chat_id}"
    return hashlib.sha256(raw.encode()).hexdigest()

def save_pending_wp_sites(chat_id: str, pending: PendingWPSiteSelection) -> None:
    key = _make_wp_site_key(chat_id)
    payload = json.dumps({
        "sites": pending.sites, "topic": pending.topic,
        "platforms": pending.platforms, "schedule": pending.schedule,
        "article_id": pending.article_id, "article_title": pending.article_title,
        "selected_page_ids": pending.selected_page_ids,
        "article_data": pending.article_data,
        "_type": "wp_site_selection",
    }, ensure_ascii=False)
    expires_at = int(time.time()) + _TTL_SECONDS
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO selection_cache (cache_key, payload, expires_at) VALUES (?, ?, ?)",
            (key, payload, expires_at)
        )

def load_pending_wp_sites(chat_id: str) -> "PendingWPSiteSelection | None":
    key = _make_wp_site_key(chat_id)
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM selection_cache WHERE cache_key = ? AND expires_at > ?",
            (key, int(time.time()))
        ).fetchone()
    if not row:
        return None
    data = json.loads(row[0])
    if data.get("_type") != "wp_site_selection":
        return None
    return PendingWPSiteSelection(
        sites=data["sites"], topic=data["topic"], platforms=data["platforms"],
        schedule=data["schedule"], article_id=data["article_id"],
        article_title=data["article_title"],
        selected_page_ids=data.get("selected_page_ids"),
        article_data=data.get("article_data"),  
    )

def delete_pending_wp_sites(chat_id: str) -> None:
    key = _make_wp_site_key(chat_id)
    with _get_conn() as conn:
        conn.execute("DELETE FROM selection_cache WHERE cache_key = ?", (key,))