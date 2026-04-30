from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient
from telethon.tl import functions, types

from app.config.settings import Settings, load_settings
from app.db.models import MediaType
from app.media.ffmpeg import probe_video

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class PublishedStory:
    story_id: int | None
    update_type: str | None


def _extract_story_id(result: object) -> tuple[int | None, str | None]:
    updates = getattr(result, "updates", None)
    if updates is None:
        return None, None
    for update in updates:
        if isinstance(update, types.UpdateStoryID):
            return update.id, update.__class__.__name__
        if isinstance(update, types.UpdateStory):
            return getattr(update.story, "id", None), update.__class__.__name__
    return None, None


class StoryPublisher:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or load_settings()

    async def _build_input_media(
        self,
        client: TelegramClient,
        media_type: MediaType,
        media_path: Path,
    ) -> types.TypeInputMedia:
        logger.debug("Building raw input media type=%s path=%s", media_type.value, media_path)
        upload = await client.upload_file(str(media_path))
        if media_type == MediaType.PHOTO:
            return types.InputMediaUploadedPhoto(file=upload)

        probe = probe_video(self._settings.media, media_path)
        attributes = [
            types.DocumentAttributeVideo(
                duration=max(probe.duration_seconds, 1.0),
                w=probe.width,
                h=probe.height,
                supports_streaming=True,
                nosound=not probe.has_audio,
            ),
            types.DocumentAttributeFilename(file_name=media_path.name),
        ]
        mime_type = mimetypes.guess_type(media_path.name)[0] or "video/mp4"
        return types.InputMediaUploadedDocument(
            file=upload,
            mime_type=mime_type,
            attributes=attributes,
            nosound_video=True if not probe.has_audio else None,
        )

    async def publish_story(
        self,
        client: TelegramClient,
        *,
        media_type: MediaType,
        media_path: Path,
        caption: str | None,
    ) -> PublishedStory:
        logger.info("Publishing Telegram story media_type=%s path=%s", media_type.value, media_path)
        if not client.is_connected():
            logger.warning("Telethon client is disconnected before story publication path=%s", media_path)
            raise ConnectionError("Cannot send requests while disconnected")

        logger.info("Checking story publishing capability for media_path=%s", media_path)
        await client(functions.stories.CanSendStoryRequest(peer="me"))
        logger.info("Story capability check succeeded for media_path=%s", media_path)
        input_media = await self._build_input_media(client, media_type, media_path)
        logger.info("Sending Telegram story media_type=%s path=%s", media_type.value, media_path)
        result = await client(
            functions.stories.SendStoryRequest(
                peer="me",
                media=input_media,
                privacy_rules=[types.InputPrivacyValueAllowAll()],
                pinned=False,
                noforwards=False,
                caption=caption or None,
                period=86400,
            )
        )
        story_id, update_type = _extract_story_id(result)
        logger.info("Story publish completed story_id=%s update_type=%s", story_id, update_type)
        return PublishedStory(story_id=story_id, update_type=update_type)
