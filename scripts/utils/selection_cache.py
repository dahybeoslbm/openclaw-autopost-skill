"""
utils/selection_cache.py — Lưu trữ danh sách candidates chờ user chọn.

Key   : SHA256(prefix + chat_id [+ topic])
Value : JSON payload với _type để phân biệt loại pending
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


# ── DB helpers ───────────────────────────────────────────────────────────────

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


def _make_key(prefix: str, *parts: str) -> str:
    """Tạo cache key từ prefix + các phần, hash SHA256."""
    raw = prefix + ":".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def _save(key: str, payload: dict) -> None:
    """Lưu payload vào cache với TTL chuẩn."""
    expires_at = int(time.time()) + _TTL_SECONDS
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO selection_cache (cache_key, payload, expires_at) VALUES (?, ?, ?)",
            (key, json.dumps(payload, ensure_ascii=False), expires_at),
        )


def _load(key: str) -> dict | None:
    """Load payload còn hạn, trả về None nếu không tìm thấy."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM selection_cache WHERE cache_key = ? AND expires_at > ?",
            (key, int(time.time())),
        ).fetchone()
    return json.loads(row[0]) if row else None


def _delete(key: str) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM selection_cache WHERE cache_key = ?", (key,))


# ── Article selection ────────────────────────────────────────────────────────

@dataclass
class PendingSelection:
    candidates: list[dict]
    platform:   list[str]
    schedule:   str
    topic:      str


def save_pending(chat_id: str, topic: str, pending: PendingSelection) -> None:
    key = _make_key(chat_id + ":", topic.strip().lower())
    _save(key, {
        "candidates": pending.candidates,
        "platform":   pending.platform,
        "schedule":   pending.schedule,
        "topic":      pending.topic,
    })


def load_any_pending(chat_id: str) -> PendingSelection | None:
    """Tìm bất kỳ pending selection còn hạn, ưu tiên cái mới nhất."""
    now = int(time.time())
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT payload FROM selection_cache WHERE expires_at > ? ORDER BY expires_at DESC LIMIT 1",
            (now,),
        ).fetchall()
    if not rows:
        return None
    data = json.loads(rows[0][0])
    # Chỉ trả về nếu đây là article selection (không có _type)
    if "_type" in data:
        return None
    return PendingSelection(
        candidates=data["candidates"],
        platform=data["platform"],
        schedule=data["schedule"],
        topic=data["topic"],
    )


def delete_pending(chat_id: str, topic: str) -> None:
    _delete(_make_key(chat_id + ":", topic.strip().lower()))


def delete_all_pending() -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM selection_cache")


def purge_expired() -> int:
    with _get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM selection_cache WHERE expires_at <= ?",
            (int(time.time()),),
        )
        return cursor.rowcount


# ── Facebook page selection ──────────────────────────────────────────────────

@dataclass
class PendingPageSelection:
    pages:         list[dict]
    topic:         str
    platforms:     list[str]
    schedule:      str
    article_id:    str
    article_title: str
    article_data:  dict | None = None
    selected_wp_site_urls: list[str] | None = None


_PAGE_KEY_PREFIX = "page_sel:"


def save_pending_pages(chat_id: str, pending: PendingPageSelection) -> None:
    key = _make_key(_PAGE_KEY_PREFIX, chat_id)
    _save(key, {
        "pages":         pending.pages,
        "topic":         pending.topic,
        "platforms":     pending.platforms,
        "schedule":      pending.schedule,
        "article_id":    pending.article_id,
        "article_title": pending.article_title,
        "article_data":  pending.article_data,
        "selected_wp_site_urls": pending.selected_wp_site_urls,
        "_type": "page_selection",
    })


def load_pending_pages(chat_id: str) -> PendingPageSelection | None:
    data = _load(_make_key(_PAGE_KEY_PREFIX, chat_id))
    if not data or data.get("_type") != "page_selection":
        return None
    return PendingPageSelection(
        pages         = data["pages"],
        topic         = data["topic"],
        platforms     = data.get("platforms") or [data.get("platform", "facebook")],
        schedule      = data["schedule"],
        article_id    = data["article_id"],
        article_title = data["article_title"],
        article_data  = data.get("article_data"),
        selected_wp_site_urls = data.get("selected_wp_site_urls"),
    )


def delete_pending_pages(chat_id: str) -> None:
    _delete(_make_key(_PAGE_KEY_PREFIX, chat_id))


# ── WordPress site selection ─────────────────────────────────────────────────

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
    rewrite_mode:  bool | None = None


_WP_SITE_KEY_PREFIX = "wp_site_sel:"


def save_pending_wp_sites(chat_id: str, pending: PendingWPSiteSelection) -> None:
    key = _make_key(_WP_SITE_KEY_PREFIX, chat_id)
    _save(key, {
        "sites":           pending.sites,
        "topic":           pending.topic,
        "platforms":       pending.platforms,
        "schedule":        pending.schedule,
        "article_id":      pending.article_id,
        "article_title":   pending.article_title,
        "article_data":    pending.article_data,
        "selected_page_ids": pending.selected_page_ids,
        "_type": "wp_site_selection",
    })


def load_pending_wp_sites(chat_id: str) -> PendingWPSiteSelection | None:
    data = _load(_make_key(_WP_SITE_KEY_PREFIX, chat_id))
    if not data or data.get("_type") != "wp_site_selection":
        return None
    return PendingWPSiteSelection(
        sites          = data["sites"],
        topic          = data["topic"],
        platforms      = data["platforms"],
        schedule       = data["schedule"],
        article_id     = data["article_id"],
        article_title  = data["article_title"],
        article_data   = data.get("article_data"),
        selected_page_ids = data.get("selected_page_ids"),
    )


def delete_pending_wp_sites(chat_id: str) -> None:
    _delete(_make_key(_WP_SITE_KEY_PREFIX, chat_id))


# ── WordPress rewrite mode ───────────────────────────────────────────────────

@dataclass
class PendingWPRewriteMode:
    selected_wp_site_urls: list[str]
    topic:                 str
    platforms:             list[str]
    schedule:              str
    article_id:            str
    article_title:         str
    article_data:          dict | None = None
    selected_page_ids:     list[str] | None = None


_WP_REWRITE_KEY_PREFIX = "wp_rewrite_sel:"


def save_pending_wp_rewrite_mode(chat_id: str, pending: PendingWPRewriteMode) -> None:
    key = _make_key(_WP_REWRITE_KEY_PREFIX, chat_id)
    _save(key, {
        "selected_wp_site_urls": pending.selected_wp_site_urls,
        "topic":                 pending.topic,
        "platforms":             pending.platforms,
        "schedule":              pending.schedule,
        "article_id":            pending.article_id,
        "article_title":         pending.article_title,
        "article_data":          pending.article_data,
        "selected_page_ids":     pending.selected_page_ids,
        "_type": "wp_rewrite_mode",
    })


def load_pending_wp_rewrite_mode(chat_id: str) -> PendingWPRewriteMode | None:
    data = _load(_make_key(_WP_REWRITE_KEY_PREFIX, chat_id))
    if not data or data.get("_type") != "wp_rewrite_mode":
        return None
    return PendingWPRewriteMode(
        selected_wp_site_urls = data["selected_wp_site_urls"],
        topic                 = data["topic"],
        platforms             = data["platforms"],
        schedule              = data["schedule"],
        article_id            = data["article_id"],
        article_title         = data["article_title"],
        article_data          = data.get("article_data"),
        selected_page_ids     = data.get("selected_page_ids"),
    )


def delete_pending_wp_rewrite_mode(chat_id: str) -> None:
    _delete(_make_key(_WP_REWRITE_KEY_PREFIX, chat_id))