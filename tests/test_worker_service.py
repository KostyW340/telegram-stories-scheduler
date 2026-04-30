from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.db.bootstrap import run_migrations
from app.db.models import MediaType, ScheduleType, StoryJobStatus
from app.db.repositories.story_jobs import StoryJobRepository
from app.db.session import session_scope
from app.scheduler.rules import utcnow_naive
from app.services.story_jobs import CreateStoryJobCommand, StoryJobService
from app.telegram.failures import MediaReuploadRequiredError
from app.telegram.health import TelegramConnectivityChannel, TelegramConnectivityMonitor
from app.telegram.runtime import TelegramRuntimeRole
from app.worker.service import WorkerService


class FakeTelegramRuntime:
    def __init__(self, *, ready: bool = True) -> None:
        self.start_roles: list[TelegramRuntimeRole] = []
        self.invalidations: list[tuple[TelegramRuntimeRole, str]] = []
        self.ready = ready
        self.ensure_role_ready_calls: list[TelegramRuntimeRole] = []

    async def start_role(self, role: TelegramRuntimeRole = TelegramRuntimeRole.PUBLISHER):
        self.start_roles.append(role)
        return SimpleNamespace()

    async def ensure_role_ready(self, role: TelegramRuntimeRole = TelegramRuntimeRole.PUBLISHER) -> bool:
        self.ensure_role_ready_calls.append(role)
        return self.ready

    async def stop(self) -> None:
        return None

    async def invalidate_role(self, role: TelegramRuntimeRole = TelegramRuntimeRole.PUBLISHER, *, reason: str) -> None:
        self.invalidations.append((role, reason))

    @asynccontextmanager
    async def client_context(self, role: TelegramRuntimeRole = TelegramRuntimeRole.PUBLISHER):
        yield SimpleNamespace(role=role, connected=True)


class FailingStoryPublisher:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def publish_story(self, *_args, **_kwargs):
        raise self._exc


class SuccessfulStoryPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []

    async def publish_story(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class FailingDispatchService:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def dispatch_claimed_job(self, _session, _job, *, trigger):
        raise self.exc


def _write_prepared_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"prepared-media")


@pytest.mark.asyncio
async def test_worker_service_schedules_transient_retry_for_connection_error(isolated_settings) -> None:
    run_migrations(isolated_settings)
    prepared_path = isolated_settings.paths.prepared_videos_dir / "retry.mp4"
    _write_prepared_file(prepared_path)

    service = StoryJobService(isolated_settings)
    created = await service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.WEEKLY,
            media_type=MediaType.VIDEO,
            media_path=isolated_settings.to_relative_runtime_path(prepared_path),
            prepared_media_path=isolated_settings.to_relative_runtime_path(prepared_path),
            caption="caption",
            scheduled_time=utcnow_naive().time().replace(second=0, microsecond=0),
            weekdays=(utcnow_naive().weekday(),),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        job.next_run_at = utcnow_naive() - timedelta(seconds=1)
        await repository.save(job)

    runtime = FakeTelegramRuntime()
    worker = WorkerService(
        isolated_settings,
        story_publisher=FailingStoryPublisher(ConnectionError("Cannot send requests while disconnected")),
        telegram_runtime=runtime,
    )

    processed = await worker.run_once()

    assert processed == 1
    assert runtime.invalidations == [(TelegramRuntimeRole.PUBLISHER, "publish-failure:transport-connection-error")]

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        assert job.status == StoryJobStatus.PENDING
        assert job.retry_count == 1
        assert job.next_run_at is not None
        assert job.next_run_at > utcnow_naive()
        assert "disconnected" in (job.last_error or "")


@pytest.mark.asyncio
async def test_worker_service_marks_weekly_job_failed_after_retry_budget_exhausted(isolated_settings) -> None:
    run_migrations(isolated_settings)
    prepared_path = isolated_settings.paths.prepared_videos_dir / "failed.mp4"
    _write_prepared_file(prepared_path)

    service = StoryJobService(isolated_settings)
    created = await service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.WEEKLY,
            media_type=MediaType.VIDEO,
            media_path=isolated_settings.to_relative_runtime_path(prepared_path),
            prepared_media_path=isolated_settings.to_relative_runtime_path(prepared_path),
            caption="caption",
            scheduled_time=utcnow_naive().time().replace(second=0, microsecond=0),
            weekdays=(utcnow_naive().weekday(),),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        job.next_run_at = utcnow_naive() - timedelta(seconds=1)
        job.retry_count = isolated_settings.runtime.publish_retry_max_attempts
        job.retry_window_started_at = utcnow_naive() - timedelta(minutes=2)
        await repository.save(job)

    runtime = FakeTelegramRuntime()
    worker = WorkerService(
        isolated_settings,
        story_publisher=FailingStoryPublisher(ConnectionError("Cannot send requests while disconnected")),
        telegram_runtime=runtime,
    )

    processed = await worker.run_once()

    assert processed == 1

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        assert job.status == StoryJobStatus.PENDING
        assert job.retry_count == isolated_settings.runtime.publish_retry_max_attempts + 1
        assert job.next_run_at is not None
        assert job.next_run_at > utcnow_naive()
        assert "disconnected" in (job.last_error or "")


@pytest.mark.asyncio
async def test_worker_service_skips_due_job_claim_when_mtproto_runtime_is_unavailable(
    isolated_settings,
) -> None:
    run_migrations(isolated_settings)
    runtime = FakeTelegramRuntime(ready=False)
    worker = WorkerService(
        isolated_settings,
        telegram_runtime=runtime,
    )

    processed = await worker.run_once()

    assert processed == 0
    assert runtime.ensure_role_ready_calls == [TelegramRuntimeRole.PUBLISHER]


@pytest.mark.asyncio
async def test_worker_service_repairs_transient_failed_one_time_job_on_startup(isolated_settings) -> None:
    run_migrations(isolated_settings)
    prepared_path = isolated_settings.paths.prepared_videos_dir / "repair-once.mp4"
    _write_prepared_file(prepared_path)
    future_local = datetime.now(ZoneInfo(isolated_settings.runtime.timezone)) + timedelta(minutes=5)

    service = StoryJobService(isolated_settings)
    created = await service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.ONCE,
            media_type=MediaType.VIDEO,
            media_path=isolated_settings.to_relative_runtime_path(prepared_path),
            prepared_media_path=isolated_settings.to_relative_runtime_path(prepared_path),
            caption="caption",
            scheduled_time=future_local.time().replace(second=0, microsecond=0),
            scheduled_date=future_local.date(),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        job.status = StoryJobStatus.FAILED
        job.next_run_at = None
        job.last_error = "Cannot send requests while disconnected"
        await repository.save(job)

    runtime = FakeTelegramRuntime(ready=False)
    worker = WorkerService(
        isolated_settings,
        telegram_runtime=runtime,
    )

    processed = await worker.run_once()

    assert processed == 0

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        assert job.status == StoryJobStatus.PENDING
        assert job.next_run_at is not None
        assert job.next_run_at > utcnow_naive()
        assert "временного сбоя" in (job.last_error or "").lower()


@pytest.mark.asyncio
async def test_worker_service_marks_media_invalid_reupload_failure_as_terminal(isolated_settings) -> None:
    run_migrations(isolated_settings)
    prepared_path = isolated_settings.paths.prepared_videos_dir / "invalid-media.mp4"
    _write_prepared_file(prepared_path)

    service = StoryJobService(isolated_settings)
    created = await service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.WEEKLY,
            media_type=MediaType.VIDEO,
            media_path=isolated_settings.to_relative_runtime_path(prepared_path),
            prepared_media_path=isolated_settings.to_relative_runtime_path(prepared_path),
            caption="caption",
            scheduled_time=utcnow_naive().time().replace(second=0, microsecond=0),
            weekdays=(utcnow_naive().weekday(),),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        job.next_run_at = utcnow_naive() - timedelta(seconds=1)
        await repository.save(job)

    runtime = FakeTelegramRuntime()
    worker = WorkerService(
        isolated_settings,
        telegram_runtime=runtime,
        dispatch_service=FailingDispatchService(
            MediaReuploadRequiredError("Медиафайл для сторис требует восстановления: загрузите сторис заново.")
        ),
    )

    processed = await worker.run_once()

    assert processed == 1
    assert runtime.invalidations == []

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        assert job.status == StoryJobStatus.FAILED
        assert "медиафайл для сторис требует восстановления" in (job.last_error or "").lower()


@pytest.mark.asyncio
async def test_worker_service_does_not_requeue_previous_day_one_time_job_on_startup(isolated_settings) -> None:
    run_migrations(isolated_settings)
    prepared_path = isolated_settings.paths.prepared_videos_dir / "repair-once-expired.mp4"
    _write_prepared_file(prepared_path)
    service = StoryJobService(isolated_settings)
    created = await service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.ONCE,
            media_type=MediaType.VIDEO,
            media_path=isolated_settings.to_relative_runtime_path(prepared_path),
            prepared_media_path=isolated_settings.to_relative_runtime_path(prepared_path),
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

    runtime = FakeTelegramRuntime(ready=False)
    worker = WorkerService(
        isolated_settings,
        telegram_runtime=runtime,
    )

    processed = await worker.run_once()

    assert processed == 0

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        assert job.status == StoryJobStatus.FAILED
        assert job.next_run_at is None
        assert "окончания дня" in (job.last_error or "").lower()


@pytest.mark.asyncio
async def test_worker_service_logs_that_publishing_continues_while_bot_api_is_degraded(
    isolated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_migrations(isolated_settings)
    prepared_path = isolated_settings.paths.prepared_photos_dir / "bot-api-degraded.jpg"
    _write_prepared_file(prepared_path)
    future_local = datetime.now(ZoneInfo(isolated_settings.runtime.timezone)) + timedelta(minutes=5)

    service = StoryJobService(isolated_settings)
    created = await service.create_job(
        CreateStoryJobCommand(
            schedule_type=ScheduleType.ONCE,
            media_type=MediaType.PHOTO,
            media_path=isolated_settings.to_relative_runtime_path(prepared_path),
            prepared_media_path=isolated_settings.to_relative_runtime_path(prepared_path),
            caption="caption",
            scheduled_time=future_local.time().replace(second=0, microsecond=0),
            scheduled_date=future_local.date(),
            timezone=isolated_settings.runtime.timezone,
        )
    )

    async with session_scope(isolated_settings) as session:
        repository = StoryJobRepository(session)
        job = await repository.get(created.id)
        assert job is not None
        job.next_run_at = utcnow_naive() - timedelta(seconds=1)
        await repository.save(job)

    monitor = TelegramConnectivityMonitor(summary_interval_seconds=60)
    monitor.report_failure(TelegramConnectivityChannel.BOT_API, "Bot API timeout")
    monkeypatch.setattr("app.worker.service.get_connectivity_monitor", lambda: monitor)
    publisher = SuccessfulStoryPublisher()
    worker = WorkerService(
        isolated_settings,
        story_publisher=publisher,
        telegram_runtime=FakeTelegramRuntime(),
    )
    info_messages: list[str] = []

    def fake_info(message: str, *args, **kwargs) -> None:
        info_messages.append(message % args if args else message)

    monkeypatch.setattr("app.worker.service.logger.info", fake_info)
    processed = await worker.run_once()

    assert processed == 1
    assert publisher.calls
    assert any("scheduled publishing continues through the MTProto channel" in message for message in info_messages)


@pytest.mark.asyncio
async def test_worker_service_warns_when_bot_api_and_mtproto_are_both_degraded(
    isolated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_migrations(isolated_settings)
    monitor = TelegramConnectivityMonitor(summary_interval_seconds=60)
    monitor.report_failure(TelegramConnectivityChannel.BOT_API, "Bot API timeout")
    monkeypatch.setattr("app.worker.service.get_connectivity_monitor", lambda: monitor)
    worker = WorkerService(
        isolated_settings,
        telegram_runtime=FakeTelegramRuntime(ready=False),
    )
    warning_messages: list[str] = []

    def fake_warning(message: str, *args, **kwargs) -> None:
        warning_messages.append(message % args if args else message)

    monkeypatch.setattr("app.worker.service.logger.warning", fake_warning)
    processed = await worker.run_once()

    assert processed == 0
    assert any("Both Telegram channels are currently degraded" in message for message in warning_messages)


@pytest.mark.asyncio
async def test_worker_service_times_out_stalled_cycle_and_invalidates_publisher(
    isolated_settings,
) -> None:
    runtime_settings = replace(
        isolated_settings.runtime,
        worker_cycle_timeout_seconds=0.01,
    )
    settings = replace(isolated_settings, runtime=runtime_settings)
    runtime = FakeTelegramRuntime()
    worker = WorkerService(
        settings,
        telegram_runtime=runtime,
    )

    async def stalled_run_once() -> int:
        await asyncio.Event().wait()
        return 0

    worker.run_once = stalled_run_once  # type: ignore[method-assign]
    task = asyncio.create_task(worker.run_forever())
    try:
        for _ in range(50):
            if runtime.invalidations:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert runtime.invalidations == [(TelegramRuntimeRole.PUBLISHER, "worker-cycle-timeout")]
