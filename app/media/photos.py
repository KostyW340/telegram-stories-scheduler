from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

STORY_WIDTH = 1080
STORY_HEIGHT = 1920


def _fingerprinted_name(source_path: Path, suffix: str) -> str:
    stat = source_path.stat()
    fingerprint = hashlib.sha1(
        f"{source_path.resolve()}::{stat.st_size}::{stat.st_mtime_ns}".encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:12]
    return f"{source_path.stem}_{fingerprint}{suffix}"


def prepare_story_photo(source_path: Path, output_dir: Path) -> Path:
    logger.info("Preparing photo story asset from %s", source_path)
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"Photo file not found: {source_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / _fingerprinted_name(source_path, ".jpg")

    with Image.open(source_path) as opened:
        image = ImageOps.exif_transpose(opened)
        logger.debug("Loaded photo size=%s mode=%s", image.size, image.mode)

        if image.mode != "RGB":
            image = image.convert("RGB")

        if image.size == (STORY_WIDTH, STORY_HEIGHT) and source_path.suffix.lower() in {".jpg", ".jpeg"}:
            shutil.copy2(source_path, output_path)
            logger.info("Photo already matches story frame; copied without re-encoding")
            return output_path

        image.thumbnail((STORY_WIDTH, STORY_HEIGHT), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (STORY_WIDTH, STORY_HEIGHT), (0, 0, 0))
        offset = ((STORY_WIDTH - image.width) // 2, (STORY_HEIGHT - image.height) // 2)
        canvas.paste(image, offset)
        canvas.save(output_path, quality=95, optimize=True)

    logger.info("Prepared photo story asset at %s", output_path)
    return output_path
