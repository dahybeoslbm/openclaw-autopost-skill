from .base import BasePlatform

class ThreadsPlatform(BasePlatform):
    
#    * Đăng một bài đơn lên Threads.
#    * @param {string} channelId
#    * @param {object} options
#    * @param {string}   options.text
#    * @param {string[]} [options.imageUrls]
#    * @param {string}   [options.videoUrl]
#    * @param {string}   [options.scheduledAt]
#    * @param {object}   [options.linkAttachment]  - { url: string } — loại trừ lẫn nhau với videoUrl
#    * @param {string}   [options.topic]
#    * @param {string}   [options.locationId]
#    * @param {string}   [options.locationName]
#    * @param {boolean}  [options.saveToDraft]
    def create_post(self, channel_id, text, image_urls=None, video_url=None, scheduled_at=None,
                    link_attachment=None, topic=None, location_id=None, location_name=None, save_to_draft=False) -> dict:
        if(not text and not image_urls and not video_url):
            raise ValueError("Threads post cần ít nhất text hoặc media")
        
        meta = {"type": "post"}
        if link_attachment: meta["linkAttachment"] = link_attachment
        if topic:           meta["topic"]          = topic
        if location_id:     meta["locationId"]     = location_id
        if location_name:   meta["locationName"]   = location_name
        
        return self._create_post(
            channel_id=channel_id, text=text,
            image_urls=image_urls, video_url=video_url,
            scheduled_at=scheduled_at,
            metadata={"threads": meta}, save_to_draft=save_to_draft,
        )
        

#    * Đăng chuỗi thread nhiều bài (long-form thread).
#    *
#    * posts[0] là bài mở đầu (opener), posts[1..n] là các reply nối tiếp nhau.
#    * Drive URLs trong từng bài được auto-convert.
#    *
#    * @param {string} channelId
#    * @param {object} options
#    * @param {Array<{ text?: string, imageUrls?: string[], videoUrl?: string }>} options.posts
#    *   Tối thiểu 2 bài, tối đa 10 bài.
#    * @param {string}  [options.scheduledAt]
#    * @param {string}  [options.topic]
#    * @param {string}  [options.locationId]
#    * @param {string}  [options.locationName]
#    * @param {boolean} [options.saveToDraft]
    def create_thread(self, channel_id, posts, scheduled_at=None, topic=None, location_id=None, location_name=None, save_to_draft=False) -> dict:
        if len(posts) < 2:
            raise ValueError("create_thread cần ít nhất 2 bài. Dùng create_post() cho bài đơn.")
        if len(posts) > 10:
            raise ValueError("Threads chỉ hỗ trợ tối đa 10 bài trong một chuỗi.")
        
        opener, continuations = posts[0], posts[1:]
        if not opener.get("text") and not opener.get("videoUrl") and not opener.get("imageUrls"):
            raise ValueError("Bài mở đầu (posts[0]) phải có text hoặc media.")
        
        meta = {"type": "post", "thread": []}
        for item in continuations:
            thread_item = {}
            if item.get("text"): thread_item["text"] = item["text"]
            if item.get("imageUrls") or item.get("videoUrl"):
                thread_item["assets"] = self._resolve_assets(image_urls=item.get("imageUrls"), video_url=item.get("videoUrl"))
            meta["thread"].append(thread_item)
        
        if topic:           meta["topic"]        = topic
        if location_id:     meta["locationId"]   = location_id
        if location_name:   meta["locationName"] = location_name
        
        return self._create_post(
            channel_id=channel_id, text=opener.get("text", ""),
            image_urls=opener.get("imageUrls"), video_url=opener.get("videoUrl"),
            scheduled_at=scheduled_at,
            metadata={"threads": meta}, save_to_draft=save_to_draft,
        )
    