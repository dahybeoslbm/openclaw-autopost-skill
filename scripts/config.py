"""
config.py — Tập trung toàn bộ cấu hình từ environment variables.
Không hardcode bất kỳ credential nào ở đây.
"""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str
    model: str = "gemini-2.5-flash"
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
    model: str = "gpt-oss:120b"
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
class AppConfig:
    output_dir: str
    openclaw: OpenClawConfig
    gemini: GeminiConfig
    ollama: OllamaConfig
    wordpress: WordPressConfig
    buffer: BufferConfig
    googledrive: GoogleDriveAPIConfig

def load_config() -> AppConfig:
    """
    Load và validate toàn bộ config từ environment.
    Gọi 1 lần duy nhất ở đầu chương trình.
    """
    return AppConfig(
        output_dir=os.environ.get("OUTPUT_DIR", "/app/output"),
        openclaw=OpenClawConfig(
            bin_path=os.environ.get(
                "OPENCLAW_BIN",
                "/root/.openclaw/tmp/node-compile-cache/openclaw"
            ),
            timeout=int(os.environ.get("OPENCLAW_TIMEOUT", "30")),
        ),
        gemini=GeminiConfig(
            api_key=os.environ.get("GEMINI_API_KEY", ""),
            model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            timeout=int(os.environ.get("GEMINI_TIMEOUT", "90")),
        ),
        ollama=OllamaConfig(
            api_key=os.environ.get("OLLAMA_API_KEY", ""),
            model=os.environ.get("OLLAMA_MODEL", "gpt-oss:120b"),
            timeout=int(os.environ.get("OLLAMA_TIMEOUT", "120")),
        ),
        wordpress=WordPressConfig(
            site_url=os.environ.get("WP_SITE_URL", ""),
            username=os.environ.get("WP_USERNAME", ""),
            app_password=os.environ.get("WP_APP_PASSWORD", ""),
            timeout=int(os.environ.get("WP_TIMEOUT", "15")),
        ),
        buffer=BufferConfig(
            api_key=os.environ.get("BUFFER_API_KEY", ""),
        ),
        
        googledrive=GoogleDriveAPIConfig(
            api_url=os.environ.get("GDRIVE_API_URL", ""),
            timeout=int(os.environ.get("GDRIVE_API_TIMEOUT", "30")),
            language=os.environ.get("GDRIVE_LANGUAGE", "vi"),
        ),
    )
