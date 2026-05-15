"""
buffer/client.py — Orchestrator chính cho Buffer GraphQL API.

Hỗ trợ multi-account (free tier 3 channels/account):
  Mỗi platform lưu channels JSON trong .env (BUFFER_FACEBOOK_CHANNELS, v.v.)
  Mỗi channel mang apiKey của account sở hữu nó.

Cấu trúc .env:
  BUFFER_API_KEY=...                 ← fallback nếu channel không có apiKey
  BUFFER_FACEBOOK_CHANNELS='[...]'
  BUFFER_INSTAGRAM_CHANNELS='[...]'
  ...
"""
import os
import json
import concurrent.futures
from typing import Callable

import requests

from utils.logger import get_logger
from .platforms.facebook       import FacebookPlatform
from .platforms.instagram      import InstagramPlatform
# from .platforms.tiktok         import TikTokPlatform
from .platforms.threads        import ThreadsPlatform
# from .platforms.twitter        import TwitterPlatform
# from .platforms.linkedin       import LinkedInPlatform
# from .platforms.youtube        import YouTubePlatform
# from .platforms.bluesky        import BlueskyPlatform
# from .platforms.pinterest      import PinterestPlatform
# from .platforms.mastodon       import MastodonPlatform
# from .platforms.google_business import GoogleBusinessPlatform

logger = get_logger(__name__)

BUFFER_API_URL = "https://api.buffer.com/graphql"

# service name từ Buffer API → tên attribute trên BufferClient
_SERVICE_TO_ATTR = {
    "facebook":       "facebook",
    "instagram":      "instagram",
    "tiktok":         "tiktok",
    "threads":        "threads",
    "twitter":        "twitter",
    "x":              "twitter",
    "linkedin":       "linkedin",
    "youtube":        "youtube",
    "bluesky":        "bluesky",
    "pinterest":      "pinterest",
    "mastodon":       "mastodon",
    "google":         "google_business",
    "google_business":"google_business",
    "googlebusiness": "google_business",
}

# canonical platform name → tên biến môi trường
_PLATFORM_ENV_KEYS = {
    "facebook":        "BUFFER_FACEBOOK_CHANNELS",
    "instagram":       "BUFFER_INSTAGRAM_CHANNELS",
    "tiktok":          "BUFFER_TIKTOK_CHANNELS",
    "threads":         "BUFFER_THREADS_CHANNELS",
    "twitter":         "BUFFER_TWITTER_CHANNELS",
    "linkedin":        "BUFFER_LINKEDIN_CHANNELS",
    "youtube":         "BUFFER_YOUTUBE_CHANNELS",
    "bluesky":         "BUFFER_BLUESKY_CHANNELS",
    "pinterest":       "BUFFER_PINTEREST_CHANNELS",
    "mastodon":        "BUFFER_MASTODON_CHANNELS",
    "google_business": "BUFFER_GOOGLE_BUSINESS_CHANNELS",
}

# service name (kể cả alias) → canonical
_SERVICE_TO_CANONICAL = {
    "facebook": "facebook", "instagram": "instagram",
    "tiktok":   "tiktok",   "threads":   "threads",
    "twitter":  "twitter",  "x":         "twitter",
    "linkedin": "linkedin", "youtube":   "youtube",
    "bluesky":  "bluesky",  "pinterest": "pinterest",
    "mastodon": "mastodon",
    "google":         "google_business",
    "google_business":"google_business",
    "googlebusiness": "google_business",
}


class BufferClient:
    def __init__(self, api_key: str | None = None):
        self._default_api_key    = api_key
        self._channel_api_key_map: dict[str, str] = {}
        self._load_channel_api_keys()
        req = self._build_request_fn()

        self.facebook        = FacebookPlatform(req)
        self.instagram       = InstagramPlatform(req)
        # self.tiktok          = TikTokPlatform(req)
        self.threads         = ThreadsPlatform(req)
        # self.twitter         = TwitterPlatform(req)
        # self.linkedin        = LinkedInPlatform(req)
        # self.youtube         = YouTubePlatform(req)
        # self.bluesky         = BlueskyPlatform(req)
        # self.pinterest       = PinterestPlatform(req)
        # self.mastodon        = MastodonPlatform(req)
        # self.google_business = GoogleBusinessPlatform(req)

    @classmethod
    def from_env(cls) -> "BufferClient":
        return cls(api_key=os.environ.get("BUFFER_API_KEY") or None)

    # ── INTERNAL: API KEY RESOLUTION ────────────────────────

    def _load_channel_api_keys(self):
        """Đọc tất cả BUFFER_*_CHANNELS và xây map channelId → apiKey."""
        for env_key in _PLATFORM_ENV_KEYS.values():
            raw = os.environ.get(env_key)
            if not raw:
                continue
            try:
                for ch in json.loads(raw):
                    if ch.get("id") and ch.get("apiKey"):
                        self._channel_api_key_map[ch["id"]] = ch["apiKey"]
            except json.JSONDecodeError:
                pass  # lỗi parse xử lý lazy trong get_channels_from_env

    def _build_request_fn(self) -> Callable:
        """Trả về hàm request tự resolve đúng API key cho mỗi channel_id."""
        def request(query: str, variables: dict, channel_id: str | None = None):
            api_key = (
                self._channel_api_key_map.get(channel_id)
                if channel_id else None
            ) or self._default_api_key

            if not api_key:
                if channel_id:
                    raise ValueError(
                        f'Không tìm thấy API key cho channel "{channel_id}". '
                        f'Kiểm tra BUFFER_*_CHANNELS trong .env hoặc đặt BUFFER_API_KEY.'
                    )
                raise ValueError("Thiếu BUFFER_API_KEY trong .env")

            return self._graphql_request(query, variables, api_key)

        return request

    # ── CORE: GRAPHQL REQUEST ────────────────────────────────

    def _graphql_request(self, query: str, variables: dict = None,
                         api_key: str = None) -> dict:
        key = api_key or self._default_api_key
        if not key:
            raise ValueError("Thiếu API key để gọi Buffer API")

        try:
            resp = requests.post(
                url=BUFFER_API_URL,
                json={"query": query, "variables": variables or {}},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
                timeout=30,
            )
        except requests.ConnectionError as e:
            raise RuntimeError(f"Không thể kết nối đến Buffer API: {e}") from e

        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError("Buffer API trả về response không hợp lệ (không phải JSON)")

        if data.get("errors"):
            msgs = "; ".join(e["message"] for e in data["errors"])
            raise RuntimeError(f"GraphQL Error: {msgs}")

        return data.get("data", {})

    # ── QUERY: ORGANIZATIONS & CHANNELS ─────────────────────

    def get_organizations(self) -> list[dict]:
        """Lấy danh sách tổ chức. Yêu cầu BUFFER_API_KEY."""
        data = self._graphql_request(
            "query { account { organizations { id name } } }"
        )
        return data["account"]["organizations"]

    def get_channels(self, organization_id: str) -> list[dict]:
        """Lấy danh sách channels của một tổ chức qua API."""
        if not organization_id:
            raise ValueError("organization_id là bắt buộc")
        data = self._graphql_request(
            """query GetChannels($orgId: OrganizationId!) {
                 channels(input: { organizationId: $orgId }) { id name service }
               }""",
            {"orgId": organization_id},
        )
        return data["channels"]

    def get_channels_from_env(self, platforms: list[str] | None = None) -> list[dict]:
        """
        Đọc channels từ BUFFER_*_CHANNELS trong .env. Không gọi API.
        platforms=["facebook","instagram"] để lọc, None/[] → tất cả.
        """
        canonical_filter = {
            _SERVICE_TO_CANONICAL.get(p.lower(), p.lower())
            for p in (platforms or [])
        }

        all_channels = []
        for canonical, env_key in _PLATFORM_ENV_KEYS.items():
            if canonical_filter and canonical not in canonical_filter:
                continue
            raw = os.environ.get(env_key)
            if not raw:
                continue
            try:
                all_channels.extend(json.loads(raw))
            except json.JSONDecodeError:
                raise ValueError(
                    f"{env_key} trong .env không hợp lệ JSON. "
                    f"Hãy chạy lại setup (node --env-file=.env setup.js)"
                )

        return all_channels

    # ── MULTI-CHANNEL: ĐĂNG LÊN NHIỀU NỀN TẢNG ─────────────

    def post_to_channels(
        self,
        platforms: list[str] | None = None,
        text: str = "",
        image_urls: list[str] | None = None,
        video_url: str | None = None,
        scheduled_at: str | None = None,
        save_to_draft: bool = False,
    ) -> dict:
        """
        Đăng bài lên nhiều channel cùng lúc (song song).
        Lỗi ở một channel không làm gián đoạn các channel khác.

        Returns:
            {"summary": {"total", "succeeded", "failed"}, "results": [...]}
        """
        targets = self.get_channels_from_env(platforms)

        if not targets:
            hint = f"platforms: [{', '.join(platforms)}]" if platforms else "bất kỳ platform nào"
            raise ValueError(
                f"Không tìm thấy channel nào cho {hint}. "
                f"Kiểm tra BUFFER_*_CHANNELS trong .env."
            )

        post_options = {
            "text":         text,
            "image_urls":   image_urls,
            "video_url":    video_url,
            "scheduled_at": scheduled_at,
            "save_to_draft":save_to_draft,
        }

        def _post_to_channel(ch: dict) -> dict:
            attr = _SERVICE_TO_ATTR.get((ch.get("service") or "").lower())
            platform = getattr(self, attr, None) if attr else None

            if not platform:
                return {
                    "platform": ch.get("service"), "channel_name": ch.get("name"),
                    "channel_id": ch.get("id"), "status": "error",
                    "error": f"Platform \"{ch.get('service')}\" chưa được hỗ trợ",
                }
            try:
                post = platform.create_post(ch["id"], **post_options)
                return {
                    "platform": ch.get("service"), "channel_name": ch.get("name"),
                    "channel_id": ch.get("id"), "status": "success", "post": post,
                }
            except Exception as e:
                return {
                    "platform": ch.get("service"), "channel_name": ch.get("name"),
                    "channel_id": ch.get("id"), "status": "error", "error": str(e),
                }

        # Song song, giống Promise.all
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            results = list(ex.map(_post_to_channel, targets))

        succeeded = sum(1 for r in results if r["status"] == "success")
        return {
            "summary": {
                "total":     len(results),
                "succeeded": succeeded,
                "failed":    len(results) - succeeded,
            },
            "results": results,
        }