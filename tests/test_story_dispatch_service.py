from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, time
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import errors as telethon_errors

from app.db.bootstrap import run_migrations
from app.db.models import MediaType, ScheduleType
from app.db.repositories.story_jobs import StoryJobCreateInput, StoryJobRepository
from app.db.session import session_scope
from app.media.service import PreparedMedia
from app.scheduler.rules import compute_next_run_at, encode_weekdays
from app.services.story_dispatch import StoryDispatchService, StoryDispatchTrigger
from app.telegram.failures import MediaReuploadRequiredError
from app.telegram.runtime import TelegramRuntimeRole
from app.telegram.story_publisher import PublishedStory


class FakeMediaInvalidError(telethon_errors.RPCError):
    pass


class FakeTelegramRuntime:
    async def ensure_role_ready(self, _role: TelegramRuntimeRole = TelegramRuntimeRole.PUBLISHER) -> bool:
        return True

    @asynccontextmanager
    async def client_context(self, _role: TelegramRuntimeRole = TelegramRuntimeRole.PUBLISHER):
        yield SimpleNamespace()


class FakeMediaPreparationService:
    def __init__(self, rebuilt_path: Path) -> None:
        self.rebuilt_path = rebuilt_path
        self.calls: list[tuple[MediaType, Path, bool]] = []

    async def prepare(
        self,
        media_type: MediaType,
        source_path: Path,
        *,
        force_video_normalization: bool = False,
    ) -> PreparedMedia:
        self.calls.append((media_type, source_path, force_video_normalization))
        self.rebuilt_path.parent.mkdir(parents=True, exist_ok=True)
        self.rebuilt_path.write_bytes(b"rebuilt-media")
        return PreparedMedia(
            media_type=media_type,
            original_path=source_path,
            prepared_path=self.rebuilt_path,
        )


class SequenceStoryPublisher:
    def __init__(self, outcomes: list[BaseException | PublishedStory]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[Path] = []

    async def publish_story(self, _client, *, media_type, media_path: Path, caption):
        assert media_type == MediaType.VIDEO
        assert caption == "caption"
        self.calls.append(media_path)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class StalledStoryPublisher:
    def __init__(self) -> None:
        self.calls = 0

    async def publish_story(self, _client, *, media_type, media_path: Path, caption):
        self.calls += 1
        await asyncio.Event().wait()


async def _create_video_job(isolated_settings, original_path: Path, prepared_path: Path) -> int:
    scheduled_time = time(11, 0)
    weekdays = (6,)
    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        created = await repository.create(
            StoryJobCreateInput(
                schedule_type=ScheduleType.WEEKLY,
                media_type=MediaType.VIDEO,
                media_path=isolated_settings.to_relative_runtime_path(original_path),
                prepared_media_path=isolated_settings.to_relative_runtime_path(prepared_path),
                caption="caption",
                scheduled_time=scheduled_time,
                days=encode_weekdays(weekdays),
                timezone=isolated_settings.runtime.timezone,
                next_run_at=compute_next_run_at(
                    ScheduleType.WEEKLY,
                    scheduled_time,
                    isolated_settings.runtime.timezone,
                    weekdays=weekdays,
                ),
            )
        )
        return created.id


@pytest.mark.asyncio
async def test_dispatch_service_rebuilds_and_retries_after_media_file_invalid(isolated_settings) -> None:
    run_migrations(isolated_settings)
    original_path = isolated_settings.paths.videos_dir / "source.mp4"
    stale_prepared_path = isolated_settings.paths.prepared_videos_dir / "stale.mp4"
    rebuilt_path = isolated_settings.paths.prepared_videos_dir / "rebuilt.mp4"
    original_path.parent.mkdir(parents=True, exist_ok=True)
    original_path.write_bytes(b"original-video")
    stale_prepared_path.parent.mkdir(parents=True, exist_ok=True)
    stale_prepared_path.write_bytes(b"stale-prepared")
    job_id = await _create_video_job(isolated_settings, original_path, stale_prepared_path)

    dispatch = StoryDispatchService(
        isolated_settings,
        media_service=FakeMediaPreparationService(rebuilt_path),
        story_publisher=SequenceStoryPublisher(
            [
                FakeMediaInvalidError(None, "MEDIA_FILE_INVALID", 400),
                PublishedStory(story_id=77, update_type="UpdateStoryID"),
            ]
        ),
        telegram_runtime=FakeTelegramRuntime(),
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(job_id)
        assert job is not None
        result = await dispatch.dispatch_claimed_job(session, job, trigger=StoryDispatchTrigger.WORKER)
        await session.commit()

    assert result.media_path == rebuilt_path
    assert stale_prepared_path.exists() is False
    assert dispatch._media_service.calls == [(MediaType.VIDEO, original_path, True)]
    assert dispatch._story_publisher.calls == [stale_prepared_path, rebuilt_path]

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(job_id)
        assert job is not None
        assert job.prepared_media_path == isolated_settings.to_relative_runtime_path(rebuilt_path)


@pytest.mark.asyncio
async def test_dispatch_service_reports_restore_message_when_original_media_is_missing(isolated_settings) -> None:
    run_migrations(isolated_settings)
    original_path = isolated_settings.paths.videos_dir / "missing-source.mp4"
    stale_prepared_path = isolated_settings.paths.prepared_videos_dir / "missing-stale.mp4"
    rebuilt_path = isolated_settings.paths.prepared_videos_dir / "unused.mp4"
    stale_prepared_path.parent.mkdir(parents=True, exist_ok=True)
    stale_prepared_path.write_bytes(b"stale-prepared")
    job_id = await _create_video_job(isolated_settings, original_path, stale_prepared_path)

    dispatch = StoryDispatchService(
        isolated_settings,
        media_service=FakeMediaPreparationService(rebuilt_path),
        story_publisher=SequenceStoryPublisher([FakeMediaInvalidError(None, "MEDIA_FILE_INVALID", 400)]),
        telegram_runtime=FakeTelegramRuntime(),
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(job_id)
        assert job is not None
        with pytest.raises(MediaReuploadRequiredError, match="Восстановите медиафайл"):
            await dispatch.dispatch_claimed_job(session, job, trigger=StoryDispatchTrigger.MANUAL_SEND)

    assert dispatch._media_service.calls == []


@pytest.mark.asyncio
async def test_dispatch_service_reports_reupload_when_rebuilt_media_is_still_invalid(isolated_settings) -> None:
    run_migrations(isolated_settings)
    original_path = isolated_settings.paths.videos_dir / "source-again.mp4"
    stale_prepared_path = isolated_settings.paths.prepared_videos_dir / "stale-again.mp4"
    rebuilt_path = isolated_settings.paths.prepared_videos_dir / "rebuilt-again.mp4"
    original_path.parent.mkdir(parents=True, exist_ok=True)
    original_path.write_bytes(b"original-video")
    stale_prepared_path.parent.mkdir(parents=True, exist_ok=True)
    stale_prepared_path.write_bytes(b"stale-prepared")
    job_id = await _create_video_job(isolated_settings, original_path, stale_prepared_path)

    dispatch = StoryDispatchService(
        isolated_settings,
        media_service=FakeMediaPreparationService(rebuilt_path),
        story_publisher=SequenceStoryPublisher(
            [
                FakeMediaInvalidError(None, "MEDIA_FILE_INVALID", 400),
                FakeMediaInvalidError(None, "MEDIA_FILE_INVALID", 400),
            ]
        ),
        telegram_runtime=FakeTelegramRuntime(),
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(job_id)
        assert job is not None
        with pytest.raises(MediaReuploadRequiredError, match="снова отклонил"):
            await dispatch.dispatch_claimed_job(session, job, trigger=StoryDispatchTrigger.WORKER)

    assert dispatch._media_service.calls == [(MediaType.VIDEO, original_path, True)]


@pytest.mark.asyncio
async def test_dispatch_service_times_out_stalled_publish(isolated_settings) -> None:
    run_migrations(isolated_settings)
    runtime_settings = replace(
        isolated_settings.runtime,
        mtproto_publish_timeout_seconds=0.01,
    )
    settings = replace(isolated_settings, runtime=runtime_settings)
    original_path = settings.paths.videos_dir / "stalled-source.mp4"
    prepared_path = settings.paths.prepared_videos_dir / "stalled-prepared.mp4"
    original_path.parent.mkdir(parents=True, exist_ok=True)
    original_path.write_bytes(b"original-video")
    prepared_path.parent.mkdir(parents=True, exist_ok=True)
    prepared_path.write_bytes(b"prepared-video")
    job_id = await _create_video_job(settings, original_path, prepared_path)
    publisher = StalledStoryPublisher()
    dispatch = StoryDispatchService(
        settings,
        story_publisher=publisher,
        telegram_runtime=FakeTelegramRuntime(),
    )

    async with session_scope(settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(job_id)
        assert job is not None
        with pytest.raises(TimeoutError, match="publish-story"):
            await dispatch.dispatch_claimed_job(session, job, trigger=StoryDispatchTrigger.WORKER)

    assert publisher.calls == 1
