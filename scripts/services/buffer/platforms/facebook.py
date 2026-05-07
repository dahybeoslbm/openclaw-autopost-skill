from .base import BasePlatform


class FacebookPlatform(BasePlatform):

    def create_post(self, channel_id: str, text: str = "",
                    image_urls=None, video_url=None, scheduled_at=None,
                    link_attachment=None, first_comment=None,
                    save_to_draft=False) -> dict:
        meta = {"type": "post"}
        if link_attachment: meta["linkAttachment"] = link_attachment
        if first_comment:   meta["firstComment"]   = first_comment
        return self._create_post(
            channel_id=channel_id, text=text,
            image_urls=image_urls, video_url=video_url,
            scheduled_at=scheduled_at,
            metadata={"facebook": meta}, save_to_draft=save_to_draft,
        )

    def create_story(self, channel_id: str, image_urls=None, video_url=None,
                     scheduled_at=None, save_to_draft=False) -> dict:
        return self._create_post(
            channel_id=channel_id, text="",
            image_urls=image_urls, video_url=video_url,
            scheduled_at=scheduled_at,
            metadata={"facebook": {"type": "story"}}, save_to_draft=save_to_draft,
        )

    def create_reel(self, channel_id: str, video_url: str, text: str = "",
                    scheduled_at=None, first_comment=None, save_to_draft=False) -> dict:
        if not video_url:
            raise ValueError("video_url là bắt buộc cho Facebook Reel")
        meta = {"type": "reel"}
        if first_comment: meta["firstComment"] = first_comment
        return self._create_post(
            channel_id=channel_id, text=text, video_url=video_url,
            scheduled_at=scheduled_at,
            metadata={"facebook": meta}, save_to_draft=save_to_draft,
        )