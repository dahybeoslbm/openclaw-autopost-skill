"""
services/openclaw.py — Wrapper cho OpenClaw CLI.
Tách riêng để dễ mock khi test hoặc thay thế về sau.
"""
import json
import subprocess
from urllib.parse import urljoin

from scripts.config import OpenClawConfig
from utils.models import ImageData, ScrapedContent
from utils.logger import get_logger

logger = get_logger(__name__)


class OpenClawService:
    def __init__(self, config: OpenClawConfig):
        self._bin = config.bin_path
        self._timeout = config.timeout

    def _run(self, args: list[str]) -> dict:
        """Chạy OpenClaw CLI và parse JSON output."""
        cmd = [self._bin] + args
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=self._timeout,
        )
        return json.loads(result.stdout)

    def search(self, query: str, limit: int = 1) -> str | None:
        """
        Tìm kiếm web, trả về URL đầu tiên tìm được.
        Trả về None nếu không tìm thấy.
        """
        logger.info("[2/6] Tìm kiếm nguồn: '%s'", query)
        try:
            data = self._run([
                "infer", "web", "search",
                "--query", f"{query} du lịch review",
                "--limit", str(limit),
                "--provider", "gemini",
                "--json",
            ])

            citations = (
                data.get("outputs", [{}])[0]
                    .get("result", {})
                    .get("citations", [])
            )
            if data.get("ok") and citations:
                url = citations[0]["url"]
                logger.info("  → Tìm thấy: %s", url)
                return url

        except subprocess.CalledProcessError as e:
            logger.error("  → OpenClaw search error: %s", e.stderr)
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error("  → OpenClaw search parse error: %s", e)
        except subprocess.TimeoutExpired:
            logger.warning("  → OpenClaw search timeout")
        except Exception as e:
            logger.error("  → OpenClaw search unknown error: %s", e)

        return None

    def fetch(self, url: str) -> ScrapedContent:
        """
        Cào nội dung từ URL, trả về ScrapedContent.
        Luôn trả về object (không raise), fallback về nội dung rỗng.
        """
        logger.info("[3/6] Cào nội dung: %s", url)
        try:
            data = self._run([
                "infer", "web", "fetch",
                "--url", url,
                "--format", "text",
                "--json",
            ])

            result = (
                data.get("outputs", [{}])[0].get("result", {})
                if data.get("ok") else {}
            )

            text = result.get("text_content", "")
            images = self._extract_images(result.get("structured_data"), url)

            if not text:
                logger.warning("  → Không có text content từ URL")

            return ScrapedContent(text=text, images=images, source_url=url)

        except subprocess.CalledProcessError as e:
            logger.error("  → OpenClaw fetch error: %s", e.stderr)
        except subprocess.TimeoutExpired:
            logger.warning("  → OpenClaw fetch timeout")
        except Exception as e:
            logger.error("  → OpenClaw fetch unknown error: %s", e)

        return ScrapedContent(text="Nội dung mặc định.", source_url=url)

    @staticmethod
    def _extract_images(structured_data: dict | None, base_url: str) -> list[ImageData]:
        """Trích xuất danh sách ảnh từ structured_data của OpenClaw."""
        if not isinstance(structured_data, dict):
            return []

        images: list[ImageData] = []
        raw = structured_data.get("image")

        if not raw:
            return []

        items = [raw] if isinstance(raw, str) else raw

        for item in items:
            if isinstance(item, str):
                images.append(ImageData(src=urljoin(base_url, item)))
            elif isinstance(item, dict):
                src = item.get("url", "")
                if src:
                    images.append(ImageData(
                        src=urljoin(base_url, src),
                        alt=item.get("caption", ""),
                    ))

        return images
