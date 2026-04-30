from __future__ import annotations

from datetime import date, datetime, time

import pytest
from sqlalchemy import delete, select

from app.db.bootstrap import run_migrations
from app.db.models import MediaType, ScheduleType, StoryJob, StoryJobStatus
from app.db.repositories.due_jobs import DueJobRepository
from app.db.repositories.story_jobs import StoryJobCreateInput, StoryJobRepository
from app.db.session import session_scope


@pytest.mark.asyncio
async def test_story_repository_create_list_delete(isolated_settings) -> None:
    run_migrations(isolated_settings)
    async with session_scope(isolated_settings) as session:
        await session.execute(delete(StoryJob))
        repository = StoryJobRepository(session)
        job = await repository.create(
            StoryJobCreateInput(
                schedule_type=ScheduleType.ONCE,
                scheduled_time=time(hour=9, minute=0),
                timezone=isolated_settings.runtime.timezone,
                media_type=MediaType.PHOTO,
                media_path="photos/demo.jpg",
                prepared_media_path="prepared/photos/demo.jpg",
                caption="hello",
                next_run_at=datetime(2036, 3, 20, 6, 0),
            )
        )
        jobs = await repository.list_jobs()
        assert job.id == 1
        assert len(jobs) == 1
        assert await repository.delete_pending(job.id) is True


@pytest.mark.asyncio
async def test_story_repository_delete_pending_weekly_job(isolated_settings) -> None:
    run_migrations(isolated_settings)
    async with session_scope(isolated_settings) as session:
        await session.execute(delete(StoryJob))
        repository = StoryJobRepository(session)
        job = await repository.create(
            StoryJobCreateInput(
                schedule_type=ScheduleType.WEEKLY,
                scheduled_time=time(hour=9, minute=0),
                scheduled_date=None,
                timezone=isolated_settings.runtime.timezone,
                media_type=MediaType.PHOTO,
                media_path="photos/demo.jpg",
                prepared_media_path="prepared/photos/demo.jpg",
                caption="weekly",
                days="mon,wed",
                next_run_at=datetime(2036, 3, 24, 6, 0),
            )
        )

        assert job.id == 1
        assert await repository.delete_pending(job.id) is True


@pytest.mark.asyncio
async def test_story_repository_delete_pending_one_time_job(isolated_settings) -> None:
    run_migrations(isolated_settings)
    async with session_scope(isolated_settings) as session:
        await session.execute(delete(StoryJob))
        repository = StoryJobRepository(session)
        job = await repository.create(
            StoryJobCreateInput(
                schedule_type=ScheduleType.ONCE,
                scheduled_time=time(hour=9, minute=0),
                scheduled_date=date(2036, 3, 20),
                timezone=isolated_settings.runtime.timezone,
                media_type=MediaType.PHOTO,
                media_path="photos/demo.jpg",
                prepared_media_path="prepared/photos/demo.jpg",
                caption="once",
                next_run_at=datetime(2036, 3, 20, 6, 0),
            )
        )

        assert job.id == 1
        assert await repository.delete_pending(job.id) is True


@pytest.mark.asyncio
async def test_story_repository_cancel_marks_job_as_cancelled(isolated_settings) -> None:
    run_migrations(isolated_settings)
    async with session_scope(isolated_settings) as session:
        await session.execute(delete(StoryJob))
        repository = StoryJobRepository(session)
        job = await repository.create(
            StoryJobCreateInput(
                schedule_type=ScheduleType.WEEKLY,
                scheduled_time=time(hour=9, minute=0),
                timezone=isolated_settings.runtime.timezone,
                media_type=MediaType.PHOTO,
                media_path="photos/demo.jpg",
                prepared_media_path="prepared/photos/demo.jpg",
                caption="hello",
                days="mon,wed",
                next_run_at=datetime(2036, 3, 24, 6, 0),
            )
        )

        cancelled = await repository.cancel(job)

        assert cancelled.status == StoryJobStatus.CANCELLED
        assert cancelled.next_run_at is None


@pytest.mark.asyncio
async def test_story_repository_purge_deletes_row(isolated_settings) -> None:
    run_migrations(isolated_settings)
    async with session_scope(isolated_settings) as session:
        await session.execute(delete(StoryJob))
        repository = StoryJobRepository(session)
        job = await repository.create(
            StoryJobCreateInput(
                schedule_type=ScheduleType.ONCE,
                scheduled_time=time(hour=9, minute=0),
                timezone=isolated_settings.runtime.timezone,
                media_type=MediaType.PHOTO,
                media_path="photos/demo.jpg",
                prepared_media_path="prepared/photos/demo.jpg",
                caption="hello",
                next_run_at=datetime(2036, 3, 20, 6, 0),
            )
        )
        await repository.purge(job)
        statement = select(StoryJob).where(StoryJob.id == job.id)
        assert (await session.scalars(statement)).one_or_none() is None


@pytest.mark.asyncio
async def test_due_job_repository_claims_pending_rows(isolated_settings) -> None:
    run_migrations(isolated_settings)
    async with session_scope(isolated_settings) as session:
        await session.execute(delete(StoryJob))
        repository = StoryJobRepository(session)
        await repository.create(
            StoryJobCreateInput(
                schedule_type=ScheduleType.ONCE,
                scheduled_time=time(hour=9, minute=0),
                timezone=isolated_settings.runtime.timezone,
                media_type=MediaType.PHOTO,
                media_path="photos/demo.jpg",
                prepared_media_path="prepared/photos/demo.jpg",
                caption="hello",
                next_run_at=datetime(2036, 3, 20, 6, 0),
            )
        )
        due_repository = DueJobRepository(session)
        claimed = await due_repository.claim_due_jobs(
            now_utc=datetime(2036, 3, 20, 6, 0),
            limit=10,
            lock_token="test-lock",
        )
        assert len(claimed) == 1
        assert claimed[0].status == StoryJobStatus.PROCESSING
