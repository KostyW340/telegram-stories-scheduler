from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, load_settings
from app.db.models import StoryJob
from app.media.service import MediaPreparationService
from app.telegram.failures import (
    MediaReuploadRequiredError,
    format_media_reupload_required_message,
    is_media_file_invalid_error,
)
from app.telegram.runtime import TelegramRuntime, TelegramRuntimeRole
from app.telegram.story_publisher import PublishedStory, StoryPublisher

logger = logging.getLogger(__name__)


class StoryDispatchTrigger(StrEnum):
    WORKER = "worker"
    MANUAL_SEND = "manual_send"


class StoryDispatchUnavailableError(ConnectionError):
    """Raised when the MTProto publishing runtime is not ready."""


@dataclass(slots=True, frozen=True)
class StoryDispatchResult:
    trigger: StoryDispatchTrigger
    media_path: Path
    published_story: PublishedStory


class StoryDispatchService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        media_service: MediaPreparationService | None = None,
        story_publisher: StoryPublisher | None = None,
        telegram_runtime: TelegramRuntime | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        self._media_service = media_service or MediaPreparationService(self._settings)
        self._story_publisher = story_publisher or StoryPublisher(self._settings)
        self._telegram_runtime = telegram_runtime or TelegramRuntime(self._settings)

    async def _ensure_media_path(
        self,
        session: AsyncSession,
        job: StoryJob,
        *,
        trigger: StoryDispatchTrigger,
        force_rebuild: bool = False,
    ) -> Path:
        original_path = self._settings.resolve_runtime_path(job.media_path)
        prepared_path = (
            self._settings.resolve_runtime_path(job.prepared_media_path) if job.prepared_media_path else None
        )

        if not force_rebuild and prepared_path and prepared_path.exists():
            logger.debug(
                "Using existing prepared media for dispatch job_id=%s trigger=%s prepared_media_path=%s",
                job.id,
                trigger.value,
                prepared_path,
            )
            return prepared_path

        if force_rebuild and prepared_path and prepared_path.exists():
            logger.warning(
                "Invalidating prepared media before forced rebuild job_id=%s trigger=%s prepared_media_path=%s",
                job.id,
                trigger.value,
                prepared_path,
            )
            prepared_path.unlink(missing_ok=True)

        if not original_path.exists() or not original_path.is_file():
            logger.warning(
                "Prepared media rebuild skipped because original media is unavailable job_id=%s trigger=%s original_media_path=%s",
                job.id,
                trigger.value,
                original_path,
            )
            raise MediaReuploadRequiredError(
                format_media_reupload_required_message(
                    "Исходный файл не найден. Восстановите медиафайл или загрузите сторис заново."
                )
            )

        if force_rebuild:
            logger.info(
                "Starting forced prepared media rebuild job_id=%s trigger=%s original_media_path=%s",
                job.id,
                trigger.value,
                original_path,
            )
        else:
            logger.warning(
                "Prepared media missing before dispatch job_id=%s trigger=%s original_media_path=%s",
                job.id,
                trigger.value,
                original_path,
            )

        prepared = await self._media_service.prepare(
            job.media_type,
            original_path,
            force_video_normalization=force_rebuild,
        )
        job.prepared_media_path = self._settings.to_relative_runtime_path(prepared.prepared_path)
        session.add(job)
        await session.flush()
        logger.info(
            "Prepared media rebuilt for dispatch job_id=%s trigger=%s prepared_media_path=%s force_rebuild=%s",
            job.id,
            trigger.value,
            prepared.prepared_path,
            force_rebuild,
        )
        return prepared.prepared_path

    async def dispatch_claimed_job(
        self,
        session: AsyncSession,
        job: StoryJob,
        *,
        trigger: StoryDispatchTrigger,
    ) -> StoryDispatchResult:
        logger.info(
            "Dispatching claimed story job job_id=%s trigger=%s status=%s media_type=%s",
            job.id,
            trigger.value,
            job.status.value,
            job.media_type.value,
        )
        runtime_ready = await self._telegram_runtime.ensure_role_ready(TelegramRuntimeRole.PUBLISHER)
        logger.info(
            "Checked MTProto publisher readiness job_id=%s trigger=%s role=%s ready=%s",
            job.id,
            trigger.value,
            TelegramRuntimeRole.PUBLISHER.value,
            runtime_ready,
        )
        if not runtime_ready:
            raise StoryDispatchUnavailableError("Telegram MTProto runtime is currently unavailable")

        media_path = await self._ensure_media_path(session, job, trigger=trigger)
        logger.info(
            "Starting MTProto story dispatch job_id=%s trigger=%s media_path=%s",
            job.id,
            trigger.value,
            media_path,
        )
        async with self._telegram_runtime.client_context(TelegramRuntimeRole.PUBLISHER) as client:
            try:
                published_story = await self._publish_story_with_timeout(
                    job,
                    client,
                    media_path=media_path,
                    trigger=trigger,
                )
            except Exception as exc:
                if not is_media_file_invalid_error(exc):
                    raise

                logger.warning(
                    "Telegram rejected prepared media as invalid job_id=%s trigger=%s original_media_path=%s prepared_media_path=%s",
                    job.id,
                    trigger.value,
                    self._settings.resolve_runtime_path(job.media_path),
                    media_path,
                )
                rebuilt_media_path = await self._ensure_media_path(
                    session,
                    job,
                    trigger=trigger,
                    force_rebuild=True,
                )
                logger.info(
                    "Retrying story dispatch after forced media rebuild job_id=%s trigger=%s rebuilt_media_path=%s",
                    job.id,
                    trigger.value,
                    rebuilt_media_path,
                )
                try:
                    published_story = await self._publish_story_with_timeout(
                        job,
                        client,
                        media_path=rebuilt_media_path,
                        trigger=trigger,
                    )
                except Exception as retry_exc:
                    logger.warning(
                        "Forced media rebuild retry failed job_id=%s trigger=%s rebuilt_media_path=%s error=%s",
                        job.id,
                        trigger.value,
                        rebuilt_media_path,
                        retry_exc,
                    )
                    if is_media_file_invalid_error(retry_exc):
                        raise MediaReuploadRequiredError(
                            format_media_reupload_required_message(
                                "Telegram снова отклонил медиафайл после повторной подготовки. Загрузите сторис заново."
                            )
                        ) from retry_exc
                    raise
                media_path = rebuilt_media_path
        logger.info(
            "Story dispatch completed job_id=%s trigger=%s story_id=%s update_type=%s",
            job.id,
            trigger.value,
            published_story.story_id,
            published_story.update_type,
        )
        return StoryDispatchResult(
            trigger=trigger,
            media_path=media_path,
            published_story=published_story,
        )

    async def _publish_story_with_timeout(
        self,
        job: StoryJob,
        client: object,
        *,
        media_path: Path,
        trigger: StoryDispatchTrigger,
    ) -> PublishedStory:
        timeout_seconds = self._settings.runtime.mtproto_publish_timeout_seconds
        try:
            return await asyncio.wait_for(
                self._story_publisher.publish_story(
                    client,
                    media_type=job.media_type,
                    media_path=media_path,
                    caption=job.caption,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            raise TimeoutError(
                f"publish-story job_id={job.id} trigger={trigger.value} "
                f"timed out after {timeout_seconds:.1f}s"
            ) from exc
