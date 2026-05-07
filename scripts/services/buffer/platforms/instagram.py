from .base import BasePlatform


class InstagramPlatform(BasePlatform):
    
#    * Đăng bài thông thường lên Instagram (ảnh hoặc video).
#    * @param {string} channelId
#    * @param {object} options
#    * @param {string}   options.text
#    * @param {string[]} [options.imageUrls]
#    * @param {string}   [options.videoUrl]
#    * @param {string}   [options.scheduledAt]
#    * @param {boolean}  [options.shouldShareToFeed]  - Mặc định: true
#    * @param {string}   [options.firstComment]
#    * @param {string}   [options.link]               - Shop Grid link
#    * @param {object}   [options.geolocation]        - { id?, text? }
#    * @param {boolean}  [options.saveToDraft]
    def create_post(self, channel_id, text, image_urls=None, video_url=None, scheduled_at=None,
                    link_attachment=None, first_comment=None, geolocation=None, save_to_draft=False) -> dict:
        meta = {"type": "post"}
        if link_attachment: meta["linkAttachment"] = link_attachment
        if first_comment:   meta["firstComment"]   = first_comment
        if geolocation:   meta["geolocation"]    = geolocation
        
        return self._create_post(
            channel_id=channel_id, text=text,
            image_urls=image_urls, video_url=video_url,
            scheduled_at=scheduled_at,
            metadata={"instagram": meta}, save_to_draft=save_to_draft,
        )
        
#    * Đăng Story lên Instagram.
#    * @param {string} channelId
#    * @param {object} options
#    * @param {string[]} [options.imageUrls]
#    * @param {string}   [options.videoUrl]
#    * @param {string}   [options.scheduledAt]
#    * @param {object}   [options.stickerFields]  - { text?, music?, products?, topics?, other? }
#    * @param {boolean}  [options.saveToDraft]
    def create_story(self, channel_id, image_urls=None, video_url=None, scheduled_at=None,
                     sticker_fields=None, save_to_draft=False) -> dict:
        meta = {"type": "story", "shouldShareToFeed": False}
        if sticker_fields: meta["stickerFields"] = sticker_fields
        
        return self._create_post(
            channel_id=channel_id, text="",
            image_urls=image_urls, video_url=video_url,
            scheduled_at=scheduled_at,
            metadata={"instagram": meta}, save_to_draft=save_to_draft,
        )
    
#    * Đăng Reel lên Instagram.
#    * @param {string} channelId
#    * @param {object} options
#    * @param {string}   options.videoUrl              - Bắt buộc (≤ 100 MB, 3–90 giây)
#    * @param {string}   [options.text]
#    * @param {string}   [options.scheduledAt]
#    * @param {boolean}  [options.shouldShareToFeed]   - Mặc định: true
#    * @param {string}   [options.firstComment]
#    * @param {boolean}  [options.saveToDraft]
#    */
    def create_reel(self, channel_id, video_url, text="", scheduled_at=None,
                    should_share_to_feed=True, first_comment=None, save_to_draft=False) -> dict:
        if not video_url:
            raise ValueError("video_url là bắt buộc cho Instagram Reel")
        
        meta = {"type": "reel", "shouldShareToFeed": should_share_to_feed}
        if first_comment: meta["firstComment"] = first_comment
        
        return self._create_post(
            channel_id=channel_id, text=text, video_url=video_url,
            scheduled_at=scheduled_at,
            metadata={"instagram": meta}, save_to_draft=save_to_draft,
        )
        

        