from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config.settings import MediaSettings

logger = logging.getLogger(__name__)


class MediaProcessingError(RuntimeError):
    """Raised when media inspection or conversion fails."""


@dataclass(slots=True, frozen=True)
class VideoProbe:
    path: Path
    format_name: str | None
    duration_seconds: float
    size_bytes: int
    video_codec: str | None
    video_profile: str | None
    pixel_format: str | None
    width: int
    height: int
    fps: float
    audio_codec: str | None
    has_audio: bool


def _run_command(command: list[str], error_message: str) -> subprocess.CompletedProcess[str]:
    logger.debug("Running external media command: %s", command)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        logger.error("Required media binary is unavailable: %s", command[0])
        raise MediaProcessingError(f"Required media binary is unavailable: {command[0]}") from exc
    if result.returncode != 0:
        logger.error("%s stderr=%s", error_message, result.stderr.strip())
        raise MediaProcessingError(error_message)
    return result


def probe_video(settings: MediaSettings, input_path: Path) -> VideoProbe:
    logger.info("Probing video input %s", input_path)
    command = [
        settings.ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_name,codec_type,profile,pix_fmt,width,height,r_frame_rate,duration:format=format_name,duration,size",
        "-of",
        "json",
        str(input_path),
    ]
    result = _run_command(command, f"ffprobe failed for {input_path}")
    payload = json.loads(result.stdout)

    streams = payload.get("streams", [])
    format_info = payload.get("format", {})
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if video_stream is None:
        raise MediaProcessingError(f"No video stream found in {input_path}")

    frame_rate = video_stream.get("r_frame_rate", "0/0")
    numerator, _, denominator = frame_rate.partition("/")
    fps = 0.0
    if numerator.isdigit() and denominator.isdigit() and int(denominator) != 0:
        fps = int(numerator) / int(denominator)

    probe = VideoProbe(
        path=input_path,
        format_name=format_info.get("format_name"),
        duration_seconds=float(format_info.get("duration") or 0.0),
        size_bytes=int(format_info.get("size") or 0),
        video_codec=video_stream.get("codec_name"),
        video_profile=video_stream.get("profile"),
        pixel_format=video_stream.get("pix_fmt"),
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        fps=fps,
        audio_codec=audio_stream.get("codec_name") if audio_stream else None,
        has_audio=audio_stream is not None,
    )
    logger.debug(
        "Video probe result path=%s codec=%s profile=%s pix_fmt=%s audio=%s size=%sx%s fps=%s duration=%s format=%s",
        probe.path,
        probe.video_codec,
        probe.video_profile,
        probe.pixel_format,
        probe.audio_codec,
        probe.width,
        probe.height,
        probe.fps,
        probe.duration_seconds,
        probe.format_name,
    )
    return probe


def transcode_video_to_story_format(
    settings: MediaSettings,
    input_path: Path,
    output_path: Path,
) -> Path:
    logger.info("Converting video to story-friendly format: %s -> %s", input_path, output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filter_graph = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
        "setsar=1,fps=30"
    )
    command = [
        settings.ffmpeg_bin,
        "-y",
        "-i",
        str(input_path),
        "-vf",
        filter_graph,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-profile:v",
        "high",
        "-level:v",
        "4.1",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-crf",
        "20",
        "-r",
        "30",
        "-t",
        str(settings.max_video_duration_seconds),
    ]

    probe = probe_video(settings, input_path)
    if probe.has_audio:
        command.extend(["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"])
    else:
        command.append("-an")

    command.append(str(output_path))
    _run_command(command, f"ffmpeg conversion failed for {input_path}")
    logger.info("Video conversion completed successfully: %s", output_path)
    return output_path
