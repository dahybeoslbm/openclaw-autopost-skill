"""
services/image.py — Sinh ảnh AI và download ảnh.
Tách riêng khỏi blogger.py để dễ swap engine sau này.
"""
import os
import re
import time
import tempfile
import subprocess
import concurrent.futures

import requests

from utils.models import ImageData
from utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_PROMPT_SUFFIX = "photorealistic, 4k, travel photography"


def download_image(url: str) -> str | None:
    """Download ảnh từ URL về file tạm, trả về đường dẫn file."""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        fd, path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(resp.content)
        return path
    except Exception as e:
        logger.warning("  → Không download được ảnh %s: %s", url[:40], e)
        return None


def _render_single(prompt: str, ref_image: ImageData | None, index: int) -> str | None:
    """
    Vẽ 1 ảnh bằng nano-banana CLI.
    Trả về đường dẫn file ảnh, hoặc URL gốc nếu thất bại.
    """
    ref_url = ref_image.src if ref_image else None
    logger.info("  → Luồng %d: Bắt đầu vẽ '%s...'", index + 1, prompt[:40])

    ref_path = download_image(ref_url) if ref_url else None

    cmd = ["gemini", "nano-banana-2-skill", prompt]
    if ref_path:
        cmd.append(ref_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
        output = result.stdout.strip()

        # Tìm URL ảnh trong output
        url_match = re.search(
            r"https?://[^\s\"'>]+\.(?:png|jpg|jpeg|webp)", output
        )
        file_match = re.search(r"(/[\w.\-]+)+\.(png|jpg|jpeg|webp)", output)

        if url_match:
            gen_url = url_match.group(0)
            out_path = f"travel_blog_img_{int(time.time())}_{index}.png"
            resp = requests.get(gen_url, timeout=15)
            with open(out_path, "wb") as f:
                f.write(resp.content)
            logger.info("  → Luồng %d: Vẽ xong → %s", index + 1, out_path)
            return out_path

        if file_match:
            logger.info("  → Luồng %d: Tìm thấy file local %s", index + 1, file_match.group(0))
            return file_match.group(0)

        logger.warning("  → Luồng %d: Không tìm thấy output. Dùng ảnh gốc.", index + 1)
        return ref_url

    except subprocess.TimeoutExpired:
        logger.warning("  → Luồng %d: Timeout. Dùng ảnh gốc.", index + 1)
        return ref_url
    except Exception as e:
        logger.error("  → Luồng %d: Lỗi %s. Dùng ảnh gốc.", index + 1, e)
        return ref_url
    finally:
        # Dọn file tạm
        if ref_path and os.path.exists(ref_path):
            os.remove(ref_path)


def render_images_parallel(
    prompts: list[str],
    source_images: list[ImageData],
    max_workers: int = 3,
) -> list[str]:
    """
    Vẽ nhiều ảnh song song, trả về list đường dẫn file hoặc URL.
    """
    logger.info("[4.5/6] Vẽ ảnh đa luồng (%d ảnh)...", len(prompts))
    results: list[str | None] = [None] * len(prompts)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(
                _render_single,
                prompt,
                source_images[i] if i < len(source_images) else None,
                i,
            ): i
            for i, prompt in enumerate(prompts)
        }

        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                logger.error("  → Luồng %d thất bại: %s", idx + 1, e)

    return [r for r in results if r]


def parse_image_prompts(response_text: str, topic: str, count: int) -> list[str]:
    """Parse danh sách prompt từ Gemini response."""
    fallback = [f"{_DEFAULT_PROMPT_SUFFIX}, {topic}"] * count

    if not response_text or "Lỗi" in response_text:
        return fallback

    prompts = [
        line.strip().lstrip("-*•").strip()
        for line in response_text.strip().splitlines()
        if line.strip()
    ]
    return prompts[:count] if prompts else fallback
