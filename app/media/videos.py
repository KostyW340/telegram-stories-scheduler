from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

from app.config.settings import MediaSettings
from app.media.ffmpeg import MediaProcessingError, VideoProbe, probe_video, transcode_video_to_story_format

logger = logging.getLogger(__name__)

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
TARGET_MAX_FPS = 30.0
SUPPORTED_H264_PROFILES = {
    "Baseline",
    "Constrained Baseline",
    "Main",
    "High",
}


def _fingerprinted_name(source_path: Path) -> str:
    stat = source_path.stat()
    fingerprint = hashlib.sha1(
        f"{source_path.resolve()}::{stat.st_size}::{stat.st_mtime_ns}".encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:12]
    return f"{source_path.stem}_{fingerprint}.mp4"


def _is_story_compatible(probe: VideoProbe, max_duration_seconds: int) -> bool:
    format_names = {part.strip() for part in (probe.format_name or "").split(",") if part.strip()}
    return (
        probe.video_codec == "h264"
        and probe.video_profile in SUPPORTED_H264_PROFILES
        and probe.pixel_format == "yuv420p"
        and probe.duration_seconds <= max_duration_seconds
        and probe.width == TARGET_WIDTH
        and probe.height == TARGET_HEIGHT
        and probe.fps <= TARGET_MAX_FPS
        and "mp4" in format_names
        and (probe.audio_codec in {None, "aac"})
    )


def prepare_story_video(
    settings: MediaSettings,
    source_path: Path,
    output_dir: Path,
    *,
    force_normalize: bool = False,
) -> Path:
    logger.info("Preparing video story asset from %s", source_path)
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"Video file not found: {source_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    probe = probe_video(settings, source_path)
    if probe.duration_seconds <= 0:
        raise MediaProcessingError(f"Video duration could not be determined for {source_path}")
    if probe.duration_seconds > settings.max_video_duration_seconds:
        raise MediaProcessingError(
            f"Video duration {probe.duration_seconds:.2f}s exceeds the configured limit of "
            f"{settings.max_video_duration_seconds}s"
        )
    if probe.width <= 0 or probe.height <= 0:
        raise MediaProcessingError(f"Invalid video frame size for {source_path}")

    output_path = output_dir / _fingerprinted_name(source_path)
    if not force_normalize and _is_story_compatible(probe, settings.max_video_duration_seconds):
        shutil.copy2(source_path, output_path)
        logger.info(
            "Video already matches target profile; copied without re-encoding profile=%s pix_fmt=%s format=%s",
            probe.video_profile,
            probe.pixel_format,
            probe.format_name,
        )
        return output_path

    logger.warning(
        "Video requires normalization codec=%s profile=%s pix_fmt=%s audio=%s size=%sx%s fps=%s duration=%s force_normalize=%s",
        probe.video_codec,
        probe.video_profile,
        probe.pixel_format,
        probe.audio_codec,
        probe.width,
        probe.height,
        probe.fps,
        probe.duration_seconds,
        force_normalize,
    )
    return transcode_video_to_story_format(settings, source_path, output_path)
