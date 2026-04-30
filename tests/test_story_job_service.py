from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.db.repositories.story_jobs import StoryJobRepository
from app.db.session import session_scope
from app.db.bootstrap import run_migrations
from app.db.models import MediaType, ScheduleType, StoryJobStatus
from app.scheduler.rules import utcnow_naive
from app.services.story_jobs import (
    CreateStoryJobCommand,
    DeleteStoryJobAction,
    ManualSendStoryJobOutcome,
    StoryJobService,
)
from app.scheduler.service import SchedulerService
from app.services.story_dispatch import StoryDispatchResult, StoryDispatchTrigger
from app.telegram.failures import MediaReuploadRequiredError
from app.telegram.story_publisher import PublishedStory


class SuccessfulDispatchService:
    def __init__(self) -> None:
        self.calls: list[tuple[int, StoryDispatchTrigger]] = []

    async def dispatch_claimed_job(self, _session, job, *, trigger):
        self.calls.append((job.id, trigger))
        return StoryDispatchResult(
            trigger=trigger,
            media_path=Path("prepared/manual-send.mp4"),
            published_story=PublishedStory(story_id=321, update_type="UpdateStoryID"),
        )


class FailingDispatchService:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def dispatch_claimed_job(self, _session, _job, *, trigger):
        raise self.exc


@pytest.mark.asyncio
async def test_delete_job_purges_sent_job(isolated_settings) -> None:
    run_migrations(isolated_settings)
    service = StoryJobService(isolated_settings)
    created = await service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.ONCE,
            media_type=MediaType.PHOTO,
            media_path="photos/demo.jpg",
            prepared_media_path="prepared/photos/demo.jpg",
            caption="caption",
            scheduled_time=time(9, 0),
            scheduled_date=date(2036, 3, 20),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        job.status = StoryJobStatus.SENT
        await repository.save(job)

    result = await service.delete_job(created.id)

    assert result.found is True
    assert result.success is True
    assert result.action == DeleteStoryJobAction.PURGED
    assert result.status == StoryJobStatus.SENT

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        assert await repository.get(created.id) is None


@pytest.mark.asyncio
async def test_delete_job_reports_missing_id(isolated_settings) -> None:
    run_migrations(isolated_settings)
    service = StoryJobService(isolated_settings)

    result = await service.delete_job(999)

    assert result.found is False
    assert result.success is False
    assert result.status is None


@pytest.mark.asyncio
async def test_delete_job_cancels_active_weekly_job(isolated_settings) -> None:
    run_migrations(isolated_settings)
    service = StoryJobService(isolated_settings)
    created = await service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.WEEKLY,
            media_type=MediaType.PHOTO,
            media_path="photos/demo.jpg",
            prepared_media_path="prepared/photos/demo.jpg",
            caption="caption",
            scheduled_time=time(9, 0),
            weekdays=(0, 2),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    result = await service.delete_job(created.id)

    assert result.found is True
    assert result.success is True
    assert result.action == DeleteStoryJobAction.CANCELLED
    assert result.status == StoryJobStatus.CANCELLED

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        assert job.status == StoryJobStatus.CANCELLED
        assert job.next_run_at is None


@pytest.mark.asyncio
async def test_delete_job_rejects_processing_job(isolated_settings) -> None:
    run_migrations(isolated_settings)
    service = StoryJobService(isolated_settings)
    created = await service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.ONCE,
            media_type=MediaType.PHOTO,
            media_path="photos/demo.jpg",
            prepared_media_path="prepared/photos/demo.jpg",
            caption="caption",
            scheduled_time=time(9, 0),
            scheduled_date=date(2036, 3, 20),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        job.status = StoryJobStatus.PROCESSING
        await repository.save(job)

    result = await service.delete_job(created.id)

    assert result.found is True
    assert result.success is False
    assert result.action is None
    assert result.status == StoryJobStatus.PROCESSING


@pytest.mark.asyncio
async def test_delete_job_cancels_failed_job(isolated_settings) -> None:
    run_migrations(isolated_settings)
    service = StoryJobService(isolated_settings)
    created = await service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.WEEKLY,
            media_type=MediaType.PHOTO,
            media_path="photos/demo.jpg",
            prepared_media_path="prepared/photos/demo.jpg",
            caption="caption",
            scheduled_time=time(9, 0),
            weekdays=(0, 2),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        job.status = StoryJobStatus.FAILED
        job.last_error = "failed"
        await repository.save(job)

    result = await service.delete_job(created.id)

    assert result.found is True
    assert result.success is True
    assert result.action == DeleteStoryJobAction.CANCELLED
    assert result.status == StoryJobStatus.CANCELLED


@pytest.mark.asyncio
async def test_create_job_rejects_one_time_story_too_close_to_now(isolated_settings) -> None:
    run_migrations(isolated_settings)
    service = StoryJobService(isolated_settings)
    now_local = datetime.now(ZoneInfo(isolated_settings.runtime.timezone))
    scheduled_local = now_local + timedelta(minutes=1)

    with pytest.raises(ValueError, match="минимум через"):
        await service.create_job(
            CreateStoryJobCommand(
                schedule_type=ScheduleType.ONCE,
                media_type=MediaType.PHOTO,
                media_path="photos/demo.jpg",
                prepared_media_path="prepared/photos/demo.jpg",
                caption="caption",
                scheduled_time=scheduled_local.time().replace(second=0, microsecond=0),
                scheduled_date=scheduled_local.date(),
                timezone=isolated_settings.runtime.timezone,
            )
        )


@pytest.mark.asyncio
async def test_scheduler_keeps_previous_day_one_time_job_failed_after_startup_repair(isolated_settings) -> None:
    run_migrations(isolated_settings)
    service = StoryJobService(isolated_settings)
    created = await service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.ONCE,
            media_type=MediaType.PHOTO,
            media_path="photos/demo.jpg",
            prepared_media_path="prepared/photos/demo.jpg",
            caption="caption",
            scheduled_time=time(9, 0),
            scheduled_date=date(2036, 3, 20),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        job.status = StoryJobStatus.FAILED
        job.scheduled_date = utcnow_naive().date() - timedelta(days=1)
        job.next_run_at = None
        job.last_error = "Cannot send requests while disconnected"
        await repository.save(job)

    async with session_scope(isolated_settings) as session:
        scheduler = SchedulerService(session, isolated_settings)
        repaired = await scheduler.repair_recoverable_failed_jobs()

    assert repaired == 0

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        assert job.status == StoryJobStatus.FAILED
        assert job.next_run_at is None
        assert "окончания дня" in (job.last_error or "").lower()


@pytest.mark.asyncio
async def test_scheduler_rolls_weekly_job_forward_after_missed_day(isolated_settings) -> None:
    run_migrations(isolated_settings)
    service = StoryJobService(isolated_settings)
    created = await service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.WEEKLY,
            media_type=MediaType.PHOTO,
            media_path="photos/demo.jpg",
            prepared_media_path="prepared/photos/demo.jpg",
            caption="caption",
            scheduled_time=time(9, 0),
            weekdays=((utcnow_naive().date() - timedelta(days=1)).weekday(),),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    missed_occurrence = datetime.combine(utcnow_naive().date() - timedelta(days=1), time(9, 0))
    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        job.status = StoryJobStatus.FAILED
        job.next_run_at = missed_occurrence
        job.last_error = "Cannot send requests while disconnected"
        await repository.save(job)

    async with session_scope(isolated_settings) as session:
        scheduler = SchedulerService(session, isolated_settings)
        repaired = await scheduler.repair_recoverable_failed_jobs()

    assert repaired == 0

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        assert job.status == StoryJobStatus.FAILED
        assert job.next_run_at is not None
        assert job.next_run_at > utcnow_naive()
        assert "перенесена на следующий день по графику" in (job.last_error or "").lower()


@pytest.mark.asyncio
async def test_scheduler_mark_sent_resumes_weekly_cadence_after_catch_up_delivery(isolated_settings) -> None:
    run_migrations(isolated_settings)
    service = StoryJobService(isolated_settings)
    created = await service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.WEEKLY,
            media_type=MediaType.PHOTO,
            media_path="photos/demo.jpg",
            prepared_media_path="prepared/photos/demo.jpg",
            caption="caption",
            scheduled_time=time(9, 0),
            weekdays=(0,),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    catch_up_sent_at = datetime(2036, 3, 24, 9, 5)
    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        scheduler = SchedulerService(session, isolated_settings)
        job = await repository.get(created.id)
        assert job is not None
        job.status = StoryJobStatus.PENDING
        job.next_run_at = datetime(2036, 3, 24, 6, 0)
        job.last_error = "Временный сбой Telegram/сети: Cannot connect to host api.telegram.org:443"
        await repository.save(job)

        await scheduler.mark_sent(job, sent_at_utc=catch_up_sent_at)

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        assert job.status == StoryJobStatus.PENDING
        assert job.last_error is None
        assert job.last_sent_at == catch_up_sent_at
        assert job.last_sent_date == date(2036, 3, 24)
        assert job.next_run_at == datetime(2036, 3, 31, 6, 0)


@pytest.mark.asyncio
async def test_manual_send_job_publishes_pending_job_and_clears_retry_state(isolated_settings) -> None:
    run_migrations(isolated_settings)
    create_service = StoryJobService(isolated_settings)
    created = await create_service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.ONCE,
            media_type=MediaType.PHOTO,
            media_path="photos/demo.jpg",
            prepared_media_path="prepared/photos/demo.jpg",
            caption="caption",
            scheduled_time=time(9, 0),
            scheduled_date=date(2036, 3, 20),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        job.retry_count = 2
        job.retry_window_started_at = utcnow_naive() - timedelta(minutes=5)
        job.last_error = "Временный сбой Telegram/сети: Cannot connect to host"
        await repository.save(job)

    dispatch = SuccessfulDispatchService()
    service = StoryJobService(isolated_settings, dispatch_service=dispatch)
    result = await service.manual_send_job(created.id, operator_user_id=100002)

    assert result.success is True
    assert result.story_id == 321
    assert dispatch.calls == [(created.id, StoryDispatchTrigger.MANUAL_SEND)]

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        assert job.status == StoryJobStatus.SENT
        assert job.retry_count == 0
        assert job.retry_window_started_at is None
        assert job.last_error is None
        assert job.lock_token is None


@pytest.mark.asyncio
async def test_manual_send_job_allows_failed_one_time_story_after_auto_window_expiry(isolated_settings) -> None:
    run_migrations(isolated_settings)
    create_service = StoryJobService(isolated_settings)
    created = await create_service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.ONCE,
            media_type=MediaType.PHOTO,
            media_path="photos/demo.jpg",
            prepared_media_path="prepared/photos/demo.jpg",
            caption="caption",
            scheduled_time=time(9, 0),
            scheduled_date=date(2036, 3, 20),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        job.status = StoryJobStatus.FAILED
        job.scheduled_date = utcnow_naive().date() - timedelta(days=1)
        job.last_error = "Автоматическая отправка завершена после окончания дня: наступили следующие сутки"
        await repository.save(job)

    dispatch = SuccessfulDispatchService()
    service = StoryJobService(isolated_settings, dispatch_service=dispatch)
    result = await service.manual_send_job(created.id, operator_user_id=100002)

    assert result.success is True
    assert result.outcome.value == "published"
    assert dispatch.calls == [(created.id, StoryDispatchTrigger.MANUAL_SEND)]


@pytest.mark.asyncio
async def test_manual_send_job_rejects_already_sent_job(isolated_settings) -> None:
    run_migrations(isolated_settings)
    create_service = StoryJobService(isolated_settings)
    created = await create_service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.ONCE,
            media_type=MediaType.PHOTO,
            media_path="photos/demo.jpg",
            prepared_media_path="prepared/photos/demo.jpg",
            caption="caption",
            scheduled_time=time(9, 0),
            scheduled_date=date(2036, 3, 20),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        job.status = StoryJobStatus.SENT
        await repository.save(job)

    service = StoryJobService(isolated_settings, dispatch_service=SuccessfulDispatchService())
    result = await service.manual_send_job(created.id, operator_user_id=100002)

    assert result.success is False
    assert result.outcome.value == "already_sent"


@pytest.mark.asyncio
async def test_manual_send_job_reports_restore_or_reupload_message_for_invalid_media(isolated_settings) -> None:
    run_migrations(isolated_settings)
    create_service = StoryJobService(isolated_settings)
    created = await create_service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.WEEKLY,
            media_type=MediaType.VIDEO,
            media_path="videos/demo.mp4",
            prepared_media_path="prepared/videos/demo.mp4",
            caption="caption",
            scheduled_time=time(9, 0),
            weekdays=(0,),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    service = StoryJobService(
        isolated_settings,
        dispatch_service=FailingDispatchService(
            MediaReuploadRequiredError("Медиафайл для сторис требует восстановления: загрузите сторис заново.")
        ),
    )
    result = await service.manual_send_job(created.id, operator_user_id=100002)

    assert result.success is False
    assert result.outcome == ManualSendStoryJobOutcome.PUBLISH_FAILED
    assert "загрузите сторис заново" in result.operator_message.lower()

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        assert job.status == StoryJobStatus.FAILED
        assert "медиафайл для сторис требует восстановления" in (job.last_error or "").lower()
