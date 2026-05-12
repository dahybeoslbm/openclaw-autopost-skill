"""
platforms/base.py — Lớp nền cho tất cả platform Buffer.
"""
from datetime import datetime, timedelta, timezone
from typing import Callable
from utils.media import resolve_assets

_CREATE_POST_MUTATION = """
  mutation CreatePost($input: CreatePostInput!) {
    createPost(input: $input) {
      ... on PostActionSuccess {
        post { id text dueAt status assets { id mimeType } }
      }
      ... on MutationError { message }
    }
  }
"""


class BasePlatform:
    def __init__(self, request_fn: Callable):
        self._request = request_fn  # (query, variables, channel_id) → dict

    def _create_post(
        self,
        channel_id: str,
        text: str = "",
        scheduled_at: str | None = None,
        image_urls: list[str] | None = None,
        video_url: str | None = None,
        metadata: dict | None = None,
        save_to_draft: bool = False,
    ) -> dict:
        if not channel_id:
            raise ValueError("channel_id là bắt buộc")

        if scheduled_at:
                mode   = "customScheduled"
                due_at = scheduled_at
        else:
                # Không có mode "now" — dùng customScheduled với thời gian hiện tại
                mode   = "customScheduled"
                due_at = (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        payload: dict = {
            "channelId":      channel_id,
            "text":           text,
            "schedulingType": "automatic",
            "mode":           mode,
            "dueAt":          due_at,
        }
        if scheduled_at:
            payload["dueAt"] = scheduled_at
        if save_to_draft:
            payload["saveToDraft"] = True

        assets = resolve_assets(image_urls, video_url)
        if assets:
            payload["assets"] = assets
        if metadata:
            payload["metadata"] = metadata

        data   = self._request(_CREATE_POST_MUTATION, {"input": payload}, channel_id)
        result = (data or {}).get("createPost")

        if not result:
            raise RuntimeError("Buffer API trả về kết quả không hợp lệ")
        if "message" in result:
            raise RuntimeError(f"Buffer từ chối tạo post: {result['message']}")
        if "post" not in result:
            raise RuntimeError("Buffer không trả về post sau khi tạo")

        return result["post"]