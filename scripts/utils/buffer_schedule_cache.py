"""
utils/buffer_schedule_cache.py — Theo dõi giới hạn 10 scheduled posts/account Buffer free plan.

Logic:
  - Mỗi scheduled post được lưu với Unix timestamp = đúng giờ đăng (scheduled_at).
  - Khi purge() chạy (mỗi lần startup blogger.py), các bản ghi đã qua giờ đăng bị xoá
    → đồng nghĩa Buffer đã đăng post đó, slot trống lại.
  - TTL = chính xác scheduled_at, không có buffer thêm.
  - Check quota theo api_key_tail (8 ký tự cuối) để tránh lưu credential trực tiếp,
    nhưng vẫn phân biệt được nhiều account Buffer khác nhau.

Giới hạn Buffer free: 10 scheduled posts / account (không phải / channel).
"""
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

_BUFFER_FREE_LIMIT = 5


# ── DB setup ─────────────────────────────────────────────────────────────────

def _db_path() -> str:
    base = os.environ.get("OUTPUT_DIR", "/app/output")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "buffer_schedule_cache.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buffer_scheduled_cache (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key_tail TEXT NOT NULL,
            title        TEXT NOT NULL,
            platform     TEXT NOT NULL,
            scheduled_at INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tail(api_key: str) -> str:
    """8 ký tự cuối của api key — đủ để phân biệt account, không lộ credential."""
    return (api_key or "")[-8:]


def _parse_iso(scheduled_at_iso: str) -> int:
    """'2026-05-20T13:00:00Z' → Unix timestamp (UTC)."""
    dt = datetime.strptime(scheduled_at_iso, "%Y-%m-%dT%H:%M:%SZ")
    return int(dt.timestamp())


# ── Public API ────────────────────────────────────────────────────────────────

def purge_expired() -> int:
    """
    Xoá các scheduled posts đã tới giờ đăng (Buffer đã publish).
    Gọi mỗi lần startup blogger.py.
    Trả về số bản ghi đã xoá.
    """
    now = int(time.time())
    with _get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM buffer_scheduled_cache WHERE scheduled_at <= ?", (now,)
        )
        return cursor.rowcount


def count_active(api_key: str) -> int:
    """Đếm scheduled posts chưa tới giờ đăng cho account có api_key này."""
    now  = int(time.time())
    tail = _tail(api_key)
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM buffer_scheduled_cache "
            "WHERE api_key_tail = ? AND scheduled_at > ?",
            (tail, now),
        ).fetchone()
    return row[0] if row else 0


def is_full(api_key: str) -> bool:
    """True nếu account đã đạt 10 scheduled posts (Buffer free limit)."""
    return count_active(api_key) >= _BUFFER_FREE_LIMIT


def save(api_key: str, title: str, platform: str, scheduled_at_iso: str) -> None:
    """
    Lưu một scheduled post vào cache sau khi Buffer xác nhận tạo thành công.
    TTL tự động = đúng scheduled_at, không cộng thêm.
    """
    ts   = _parse_iso(scheduled_at_iso)
    tail = _tail(api_key)
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO buffer_scheduled_cache (api_key_tail, title, platform, scheduled_at) "
            "VALUES (?, ?, ?, ?)",
            (tail, title, platform, ts),
        )

_VN_TZ = timezone(timedelta(hours=7)) 
def list_active(api_key: str) -> list[dict]:
    """
    Liệt kê các scheduled posts còn hiệu lực của account.
    Dùng để log / debug — không phải critical path.
    """
    now  = int(time.time())
    tail = _tail(api_key)
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT title, platform, scheduled_at FROM buffer_scheduled_cache "
            "WHERE api_key_tail = ? AND scheduled_at > ? "
            "ORDER BY scheduled_at ASC",
            (tail, now),
        ).fetchall()
    return [
        {
            "title":        r[0],
            "platform":     r[1],
            "scheduled_at": datetime.fromtimestamp(r[2], tz=_VN_TZ).strftime("%Y-%m-%dT%H:%M:%S+07:00"),
        }
        for r in rows
    ]