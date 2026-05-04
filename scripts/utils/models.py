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
    platform: str = "Blog"
    schedule_time: str = ""


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
class PublishResult:
    """Kết quả sau khi xuất bản bài viết."""
    file_path: str
    wp_post_id: Optional[int] = None
    wp_post_url: Optional[str] = None
    wp_status: Optional[str] = None

    @property
    def posted_to_wp(self) -> bool:
        return self.wp_post_id is not None
