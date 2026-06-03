"""
config.py — Tập trung toàn bộ cấu hình từ environment variables.
Không hardcode bất kỳ credential nào ở đây.
"""
import os
import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str
    model: str = "gemini-3.5-flash"
    timeout: int = 90
    api_url: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(
            self, "api_url",
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        )

    @property
    def is_valid(self) -> bool:
        return bool(self.api_key) and self.api_key != "NHAP_API_KEY_GEMINI_CUA_BAN_VAO_DAY"


@dataclass(frozen=True)
class WordPressConfig:
    site_url: str
    username: str
    app_password: str
    timeout: int = 15

    @property
    def is_valid(self) -> bool:
        return all([self.site_url, self.username, self.app_password])

    @property
    def api_base(self) -> str:
        return f"{self.site_url.rstrip('/')}/wp-json/wp/v2"

def _load_wp_sites(timeout: int) -> list[WordPressConfig]:
    raw = os.environ.get("WP_SITES")
    if not raw:
        return []
    try:
        return [
            WordPressConfig(
                site_url=s["url"],
                username=s["username"],
                app_password=s["app_password"],
                timeout=timeout,
            )
            for s in json.loads(raw)
            if s.get("url") and s.get("username") and s.get("app_password")
        ]
    except (json.JSONDecodeError, KeyError):
        return []

@dataclass(frozen=True)
class OpenClawConfig:
    bin_path: str
    timeout: int = 30
    

@dataclass(frozen=True)
class BufferConfig:
    api_key: str
    # Không cần parse channels ở đây — BufferClient tự đọc từ os.environ

    @property
    def is_valid(self) -> bool:
        return bool(self.api_key)
    
@dataclass(frozen=True)
class OllamaConfig:
    api_key: str
    model: str = "gpt-oss:20b"
    host: str = "https://ollama.com"
    timeout: int = 120

    @property
    def is_valid(self) -> bool:
        return bool(self.api_key)

    @property
    def api_url(self) -> str:
        return f"{self.host}/api/chat"

@dataclass(frozen=True)
class GoogleDriveAPIConfig:
    """
    Config để gọi api.drive.article (PHP service lấy nội dung Google Docs). 
    """
    api_url:  str
    timeout:  int = 30
    language: str = "vi"
 
    @property
    def is_valid(self) -> bool:
        return bool(self.api_url)
@dataclass(frozen=True)
class FacebookConfig:
    pages: list[dict] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return bool(self.pages)
    
def _load_fb_pages() -> list[dict]:
    raw = os.environ.get("FACEBOOK_PAGES", "")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []

@dataclass(frozen=True)
class ZaloConfig:
    """
    Config để gọi PHP proxy epZaloPost (đăng bài lên Zalo OA).
    PHP proxy tự xử lý access_token — Python chỉ cần api_key + app_id.
    """
    api_url: str
    api_key: str
    app_id:  str
    timeout: int = 30

    @property
    def is_valid(self) -> bool:
        return all([self.api_url, self.api_key, self.app_id])

@dataclass(frozen=True)
class AppConfig:
    output_dir: str
    chat_id:    str
    openclaw: OpenClawConfig
    gemini: GeminiConfig
    ollama: OllamaConfig
    wordpress: WordPressConfig
    wordpress_sites: list[WordPressConfig]
    buffer: BufferConfig
    googledrive: GoogleDriveAPIConfig
    facebook: FacebookConfig
    zalo: ZaloConfig

def load_config() -> AppConfig:
    """
    Load và validate toàn bộ config từ environment.
    Gọi 1 lần duy nhất ở đầu chương trình.
    """
    wp_timeout = int(os.environ.get("WP_TIMEOUT", "15")) 
    wp_sites   = _load_wp_sites(wp_timeout)
    return AppConfig(
        output_dir=os.environ.get("OUTPUT_DIR", "/app/output"),
        # Ưu tiên CHAT_ID nếu có, fallback lấy ID đầu tiên từ CHAT_IDS
        chat_id=(
            os.environ.get("CHAT_ID", "").strip()
            or os.environ.get("CHAT_IDS", "").split(",")[0].strip()
        ),
        openclaw=OpenClawConfig(
            bin_path=os.environ.get(
                "OPENCLAW_BIN",
                "/root/.openclaw/tmp/node-compile-cache/openclaw"
            ),
            timeout=int(os.environ.get("OPENCLAW_TIMEOUT", "30")),
        ),
        gemini=GeminiConfig(
            api_key=os.environ.get("GEMINI_API_KEY", ""),
            model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
            timeout=int(os.environ.get("GEMINI_TIMEOUT", "90")),
        ),
        ollama=OllamaConfig(
            api_key=os.environ.get("OLLAMA_API_KEY", ""),
            model=os.environ.get("OLLAMA_MODEL", "gpt-oss:20b"),
            timeout=int(os.environ.get("OLLAMA_TIMEOUT", "120")),
        ),
        wordpress=wp_sites[0] if wp_sites else WordPressConfig(
            site_url="", username="", app_password=""
        ),
        wordpress_sites=wp_sites,
        buffer=BufferConfig(
            api_key=os.environ.get("BUFFER_API_KEY", ""),
        ),
        googledrive=GoogleDriveAPIConfig(
            api_url=os.environ.get("GDRIVE_API_URL", ""),
            timeout=int(os.environ.get("GDRIVE_API_TIMEOUT", "30")),
            language=os.environ.get("GDRIVE_LANGUAGE", "vi"),
        ),
        facebook=FacebookConfig(pages=_load_fb_pages()),
        zalo=ZaloConfig(
            api_url=os.environ.get("ZALO_API_URL", ""),
            api_key=os.environ.get("ZALO_API_KEY", ""),
            app_id=os.environ.get("ZALO_APP_ID", ""),
            timeout=int(os.environ.get("ZALO_TIMEOUT", "30")),
        ),
    )
