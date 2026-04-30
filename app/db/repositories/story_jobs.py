from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import Select, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MediaType, ScheduleType, StoryJob, StoryJobStatus
from app.db.models import utcnow

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class StoryJobCreateInput:
    schedule_type: ScheduleType
    scheduled_time: time
    timezone: str
    media_type: MediaType
    media_path: str
    prepared_media_path: str | None
    caption: str | None
    scheduled_date: date | None = None
    days: str | None = None
    status: StoryJobStatus = StoryJobStatus.PENDING
    next_run_at: datetime | None = None
    legacy_id: int | None = None


class StoryJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, payload: StoryJobCreateInput) -> StoryJob:
        logger.info(
            "Creating story job schedule_type=%s media_type=%s next_run_at=%s",
            payload.schedule_type.value,
            payload.media_type.value,
            payload.next_run_at,
        )
        job = StoryJob(
            id=payload.legacy_id,
            photo_path=payload.media_path,
            media_path=payload.media_path,
            prepared_media_path=payload.prepared_media_path,
            caption=payload.caption,
            scheduled_time=payload.scheduled_time,
            scheduled_date=payload.scheduled_date,
            status=payload.status,
            schedule_type=payload.schedule_type,
            days=payload.days,
            timezone=payload.timezone,
            media_type=payload.media_type,
            next_run_at=payload.next_run_at,
        )
        self._session.add(job)
        await self._session.flush()
        logger.debug("Created story job row %s", job.to_log_context())
        return job

    async def get(self, job_id: int) -> StoryJob | None:
        logger.debug("Loading story job by id=%s", job_id)
        return await self._session.get(StoryJob, job_id)

    async def claim_for_dispatch(
        self,
        job_id: int,
        *,
        lock_token: str,
        claimed_at: datetime,
        stale_after: timedelta,
    ) -> StoryJob | None:
        stale_before = claimed_at - stale_after
        logger.info(
            "Claiming story job for dispatch job_id=%s lock_token=%s claimed_at=%s stale_before=%s",
            job_id,
            lock_token,
            claimed_at,
            stale_before,
        )
        statement = (
            update(StoryJob)
            .where(StoryJob.id == job_id)
            .where(StoryJob.status.in_((StoryJobStatus.PENDING, StoryJobStatus.FAILED)))
            .where(
                or_(
                    StoryJob.lock_token.is_(None),
                    StoryJob.locked_at.is_(None),
                    StoryJob.locked_at < stale_before,
                )
            )
            .values(
                status=StoryJobStatus.PROCESSING,
                lock_token=lock_token,
                locked_at=claimed_at,
                updated_at=claimed_at,
            )
            .returning(StoryJob)
        )
        claimed_job = await self._session.scalar(statement)
        if claimed_job is None:
            logger.warning("Story job dispatch claim rejected job_id=%s lock_token=%s", job_id, lock_token)
            return None
        logger.info(
            "Story job dispatch claim succeeded job_id=%s lock_token=%s status=%s",
            claimed_job.id,
            lock_token,
            claimed_job.status.value,
        )
        return claimed_job

    async def list_jobs(
        self,
        statuses: tuple[StoryJobStatus, ...] | None = None,
        limit: int = 100,
    ) -> list[StoryJob]:
        statement: Select[tuple[StoryJob]] = select(StoryJob).order_by(StoryJob.created_at.desc()).limit(limit)
        if statuses:
            statement = statement.where(StoryJob.status.in_(statuses))
        logger.debug("Listing story jobs with statuses=%s limit=%s", statuses, limit)
        result = await self._session.scalars(statement)
        return list(result.all())

    async def delete_pending(self, job_id: int) -> bool:
        logger.info("Deleting pending job id=%s", job_id)
        statement = delete(StoryJob).where(
            StoryJob.id == job_id,
            StoryJob.status == StoryJobStatus.PENDING,
        )
        result = await self._session.execute(statement)
        deleted = bool(result.rowcount)
        logger.debug("Delete pending result for id=%s -> %s", job_id, deleted)
        return deleted

    async def cancel(self, job: StoryJob) -> StoryJob:
        logger.info("Cancelling story job id=%s previous_status=%s", job.id, job.status.value)
        job.status = StoryJobStatus.CANCELLED
        job.next_run_at = None
        job.lock_token = None
        job.locked_at = None
        job.updated_at = utcnow()
        self._session.add(job)
        await self._session.flush()
        logger.debug("Cancelled story job id=%s", job.id)
        return job

    async def purge(self, job: StoryJob) -> None:
        logger.info("Purging story job id=%s previous_status=%s", job.id, job.status.value)
        await self._session.delete(job)
        await self._session.flush()
        logger.debug("Purged story job id=%s", job.id)

    async def save(self, job: StoryJob) -> StoryJob:
        self._session.add(job)
        await self._session.flush()
        logger.debug("Saved story job %s", job.to_log_context())
        return job
