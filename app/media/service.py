from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from app.config.settings import Settings, load_settings
from app.db.models import MediaType
from app.media.photos import prepare_story_photo
from app.media.videos import prepare_story_video

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class PreparedMedia:
    media_type: MediaType
    original_path: Path
    prepared_path: Path


class MediaPreparationService:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or load_settings()

    async def prepare(
        self,
        media_type: MediaType,
        source_path: Path,
        *,
        force_video_normalization: bool = False,
    ) -> PreparedMedia:
        logger.info(
            "Preparing media asset type=%s path=%s force_video_normalization=%s",
            media_type.value,
            source_path,
            force_video_normalization,
        )
        if media_type == MediaType.PHOTO:
            prepared_path = await asyncio.to_thread(
                prepare_story_photo,
                source_path,
                self._settings.paths.prepared_photos_dir,
            )
        elif media_type == MediaType.VIDEO:
            prepared_path = await asyncio.to_thread(
                prepare_story_video,
                self._settings.media,
                source_path,
                self._settings.paths.prepared_videos_dir,
                force_normalize=force_video_normalization,
            )
        else:
            raise ValueError(f"Unsupported media type: {media_type}")

        logger.info("Prepared media ready at %s", prepared_path)
        return PreparedMedia(
            media_type=media_type,
            original_path=source_path,
            prepared_path=prepared_path,
        )
