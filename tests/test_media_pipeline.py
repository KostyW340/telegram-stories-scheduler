from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image

from app.media.ffmpeg import probe_video
from app.media.photos import prepare_story_photo
from app.media.videos import prepare_story_video


def test_prepare_story_photo_outputs_story_frame(isolated_settings, tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    Image.new("RGB", (400, 300), color=(255, 0, 0)).save(source)

    prepared = prepare_story_photo(source, isolated_settings.paths.prepared_photos_dir)
    with Image.open(prepared) as image:
        assert image.size == (1080, 1920)


def test_prepare_story_video_outputs_story_friendly_mp4(isolated_settings, tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            isolated_settings.media.ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=640x360:d=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    prepared = prepare_story_video(isolated_settings.media, source, isolated_settings.paths.prepared_videos_dir)
    probe = probe_video(isolated_settings.media, prepared)
    assert probe.width == 1080
    assert probe.height == 1920
    assert probe.video_codec == "h264"
    assert probe.pixel_format == "yuv420p"


def test_prepare_story_video_normalizes_yuv444_h264_input(isolated_settings, tmp_path: Path) -> None:
    source = tmp_path / "source-yuv444.mp4"
    subprocess.run(
        [
            isolated_settings.media.ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=1080x1920:d=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv444p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    source_probe = probe_video(isolated_settings.media, source)
    assert source_probe.video_codec == "h264"
    assert source_probe.pixel_format == "yuv444p"

    prepared = prepare_story_video(isolated_settings.media, source, isolated_settings.paths.prepared_videos_dir)
    prepared_probe = probe_video(isolated_settings.media, prepared)

    assert prepared_probe.pixel_format == "yuv420p"
    assert prepared_probe.width == 1080
    assert prepared_probe.height == 1920
