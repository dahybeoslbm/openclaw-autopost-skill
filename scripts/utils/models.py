"""
utils/models.py — Các dataclass dùng chung giữa các service.
Định nghĩa rõ cấu trúc dữ liệu thay vì dùng dict thô.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParsedRequest:
    """Kết quả phân tích câu lệnh tự nhiên từ người dùng."""
    topic: str
    platforms: list[str] = field(default_factory=lambda: ["Blog"])
    schedule_time: str = ""
    preselected_pages: str = ""
    preselected_wp_sites: str = ""


@dataclass
class ImageData:
    """Thông tin một ảnh cào được từ trang web."""
    src: str
    alt: str = ""


@dataclass
class ScrapedContent:
    """Nội dung cào được từ một URL."""
    text: str
    images: list[ImageData] = field(default_factory=list)
    source_url: str = ""

@dataclass
class BufferPostResult:
    """Kết quả đăng một channel lên Buffer."""
    platform:     str
    channel_name: str
    channel_id:   str
    status:       str          # "success" | "error"
    post_id:      str  = ""
    error:        str  = ""
    
@dataclass
class FacebookPostResult:
    """Kết quả đăng một Page lên Facebook trực tiếp."""
    page_id:   str
    page_name: str
    status:    str          # "success" | "error"
    post_id:   str = ""
    error:     str = ""

@dataclass
class ZaloPostResult:
    """Kết quả đăng bài viết lên Zalo OA."""
    app_id:        str
    status:        str          # "success" | "error"
    article_token: str = ""
    error:         str = ""

@dataclass
class PublishResult:

    """Kết quả sau khi xuất bản bài viết."""
    file_path: str
    error: str = ""  
    prompt_msg: str = ""
    wp_post_id: Optional[int] = None
    wp_post_url: Optional[str] = None
    wp_status: Optional[str] = None
    buffer_results: list["BufferPostResult"] = field(default_factory=list)
    facebook_results: list["FacebookPostResult"] = field(default_factory=list)
    zalo_results: list["ZaloPostResult"] = field(default_factory=list)

    @property
    def posted_to_wp(self) -> bool:
        return self.wp_post_id is not None
    
    @property
    def posted_to_buffer(self) -> bool:
        return any(r.status == "success" for r in self.buffer_results)
    
    @property
    def has_error(self) -> bool: 
        return bool(self.error)
    
    @property
    def posted_to_facebook(self) -> bool:
        return any(r.status == "success" for r in self.facebook_results)

    @property
    def posted_to_zalo(self) -> bool:
        return any(r.status == "success" for r in self.zalo_results)
