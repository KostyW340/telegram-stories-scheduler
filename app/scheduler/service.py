from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, load_settings
from app.db.models import ScheduleType, StoryJob, StoryJobStatus
from app.db.repositories.due_jobs import DueJobRepository
from app.db.repositories.story_jobs import StoryJobRepository
from app.scheduler.rules import compute_next_run_at, decode_weekdays, local_schedule_date, localize_utc_naive, utcnow_naive
from app.telegram.failures import (
    format_auto_post_expired_message,
    format_recovered_failure_message,
    format_transient_failure_message,
    format_weekly_rollover_message,
    looks_like_transient_failure_message,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ClaimedJobsBatch:
    lock_token: str
    jobs: tuple[StoryJob, ...]


@dataclass(slots=True, frozen=True)
class TransientRetryPlan:
    should_retry: bool
    retry_count: int
    window_started_at: datetime
    next_run_at: datetime | None
    delay_seconds: int
    exhausted_reason: str | None = None
    terminal_error_message: str | None = None


class SchedulerService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or load_settings()
        self._due_jobs = DueJobRepository(session)
        self._story_jobs = StoryJobRepository(session)

    async def claim_due_jobs(self, limit: int | None = None) -> ClaimedJobsBatch:
        now_utc = utcnow_naive()
        batch_limit = limit or self._settings.runtime.worker_batch_size
        lock_token = uuid.uuid4().hex
        jobs = await self._due_jobs.claim_due_jobs(
            now_utc=now_utc,
            limit=batch_limit,
            lock_token=lock_token,
            stale_after=timedelta(seconds=self._settings.runtime.job_lock_ttl_seconds),
        )
        logger.info("Due jobs batch claimed size=%s token=%s", len(jobs), lock_token)
        return ClaimedJobsBatch(lock_token=lock_token, jobs=tuple(jobs))

    async def mark_sent(self, job: StoryJob, sent_at_utc: datetime | None = None) -> StoryJob:
        timestamp = sent_at_utc or utcnow_naive()
        logger.info("Marking job as sent job_id=%s schedule_type=%s", job.id, job.schedule_type.value)
        job.attempt_count += 1
        job.retry_count = 0
        job.retry_window_started_at = None
        job.last_error = None
        job.last_sent_at = timestamp
        job.last_sent_date = local_schedule_date(timestamp, job.timezone)
        job.lock_token = None
        job.locked_at = None
        job.updated_at = timestamp

        if job.schedule_type == ScheduleType.ONCE:
            job.status = StoryJobStatus.SENT
            job.next_run_at = None
        else:
            next_run = compute_next_run_at(
                job.schedule_type,
                job.scheduled_time,
                job.timezone,
                weekdays=decode_weekdays(job.days),
                scheduled_date=job.scheduled_date,
                now_utc=timestamp + timedelta(seconds=1),
                last_sent_date=job.last_sent_date,
            )
            job.status = StoryJobStatus.PENDING
            job.next_run_at = next_run
            logger.debug("Recurring job next_run_at recalculated to %s", next_run)

        return await self._story_jobs.save(job)

    def _automatic_delivery_local_date(self, job: StoryJob) -> date | None:
        if job.schedule_type == ScheduleType.ONCE:
            return job.scheduled_date
        if job.next_run_at is not None:
            return local_schedule_date(job.next_run_at, job.timezone)
        if job.retry_window_started_at is not None:
            return local_schedule_date(job.retry_window_started_at, job.timezone)
        return None

    def plan_transient_retry(
        self,
        job: StoryJob,
        *,
        failed_at_utc: datetime | None = None,
        retry_after_seconds: int | None = None,
    ) -> TransientRetryPlan:
        timestamp = failed_at_utc or utcnow_naive()
        window_started_at = job.retry_window_started_at or job.next_run_at or timestamp
        retry_count = job.retry_count + 1
        scheduled_local_date = self._automatic_delivery_local_date(job)
        current_local_date = localize_utc_naive(timestamp, job.timezone).date()

        computed_delay = min(
            self._settings.runtime.publish_retry_base_delay_seconds * (2 ** max(job.retry_count, 0)),
            self._settings.runtime.publish_retry_max_delay_seconds,
        )
        if retry_after_seconds is not None:
            computed_delay = max(computed_delay, retry_after_seconds)

        next_run_at = timestamp + timedelta(seconds=computed_delay)
        next_retry_local_date = localize_utc_naive(next_run_at, job.timezone).date()
        if scheduled_local_date is not None and current_local_date > scheduled_local_date:
            terminal_error_message = (
                format_auto_post_expired_message("наступили следующие сутки")
                if job.schedule_type == ScheduleType.ONCE
                else format_weekly_rollover_message("пропущенный день завершён, задача перенесена на следующий слот")
            )
            logger.warning(
                "Refusing transient retry because automatic delivery window already closed job_id=%s schedule_type=%s scheduled_local_date=%s current_local_date=%s exhausted_reason=%s",
                job.id,
                job.schedule_type.value,
                scheduled_local_date,
                current_local_date,
                "automatic-window-closed",
            )
            return TransientRetryPlan(
                should_retry=False,
                retry_count=retry_count,
                window_started_at=window_started_at,
                next_run_at=None,
                delay_seconds=computed_delay,
                exhausted_reason="automatic-window-closed",
                terminal_error_message=terminal_error_message,
            )
        if scheduled_local_date is not None and next_retry_local_date > scheduled_local_date:
            terminal_error_message = (
                format_auto_post_expired_message("повторная попытка уже вышла бы за пределы текущего дня")
                if job.schedule_type == ScheduleType.ONCE
                else format_weekly_rollover_message("повторная попытка уже вышла бы за пределы текущего дня")
            )
            logger.warning(
                "Refusing transient retry because proposed retry crosses local day boundary job_id=%s schedule_type=%s scheduled_local_date=%s next_retry_local_date=%s exhausted_reason=%s",
                job.id,
                job.schedule_type.value,
                scheduled_local_date,
                next_retry_local_date,
                "retry-crosses-local-midnight",
            )
            return TransientRetryPlan(
                should_retry=False,
                retry_count=retry_count,
                window_started_at=window_started_at,
                next_run_at=None,
                delay_seconds=computed_delay,
                exhausted_reason="retry-crosses-local-midnight",
                terminal_error_message=terminal_error_message,
            )
        logger.info(
            "Transient catch-up retry planned job_id=%s retry_count=%s delay_seconds=%s next_run_at=%s window_started_at=%s",
            job.id,
            retry_count,
            computed_delay,
            next_run_at,
            window_started_at,
        )
        return TransientRetryPlan(
            should_retry=True,
            retry_count=retry_count,
            window_started_at=window_started_at,
            next_run_at=next_run_at,
            delay_seconds=computed_delay,
        )

    async def mark_retry(
        self,
        job: StoryJob,
        error_message: str,
        retry_plan: TransientRetryPlan,
        *,
        failed_at_utc: datetime | None = None,
    ) -> StoryJob:
        timestamp = failed_at_utc or utcnow_naive()
        logger.warning(
            "Marking job for transient retry job_id=%s retry_count=%s next_run_at=%s error=%s",
            job.id,
            retry_plan.retry_count,
            retry_plan.next_run_at,
            error_message,
        )
        job.attempt_count += 1
        job.retry_count = retry_plan.retry_count
        job.retry_window_started_at = retry_plan.window_started_at
        job.last_error = format_transient_failure_message(error_message)
        job.lock_token = None
        job.locked_at = None
        job.updated_at = timestamp
        job.status = StoryJobStatus.PENDING
        job.next_run_at = retry_plan.next_run_at
        return await self._story_jobs.save(job)

    async def mark_failed(
        self,
        job: StoryJob,
        error_message: str,
        failed_at_utc: datetime | None = None,
        *,
        increment_attempt: bool = True,
    ) -> StoryJob:
        timestamp = failed_at_utc or utcnow_naive()
        logger.warning("Marking job as failed job_id=%s error=%s", job.id, error_message)
        if increment_attempt:
            job.attempt_count += 1
        job.retry_count = 0
        job.retry_window_started_at = None
        job.last_error = error_message
        job.lock_token = None
        job.locked_at = None
        job.updated_at = timestamp

        if job.schedule_type == ScheduleType.WEEKLY:
            job.status = StoryJobStatus.FAILED
            job.next_run_at = compute_next_run_at(
                job.schedule_type,
                job.scheduled_time,
                job.timezone,
                weekdays=decode_weekdays(job.days),
                scheduled_date=job.scheduled_date,
                now_utc=timestamp + timedelta(seconds=1),
                last_sent_date=job.last_sent_date,
            )
            logger.debug("Recurring failed job next_run_at recalculated to %s", job.next_run_at)
        else:
            job.status = StoryJobStatus.FAILED
            job.next_run_at = None

        return await self._story_jobs.save(job)

    async def repair_recoverable_failed_jobs(self, *, limit: int = 500) -> int:
        timestamp = utcnow_naive()
        retry_delay = self._settings.runtime.publish_retry_base_delay_seconds
        next_retry_at = timestamp + timedelta(seconds=retry_delay)
        repaired = 0

        failed_jobs = await self._story_jobs.list_jobs(statuses=(StoryJobStatus.FAILED,), limit=limit)
        logger.info("Checking failed jobs for transient catch-up repair count=%s", len(failed_jobs))

        for job in failed_jobs:
            if not looks_like_transient_failure_message(job.last_error):
                logger.warning(
                    "Leaving failed job untouched because it does not look transient job_id=%s error=%s",
                    job.id,
                    job.last_error,
                )
                continue

            retry_plan = self.plan_transient_retry(
                job,
                failed_at_utc=timestamp,
                retry_after_seconds=retry_delay,
            )
            if not retry_plan.should_retry:
                logger.info(
                    "Leaving transient-failed job in terminal state after repair window check job_id=%s exhausted_reason=%s",
                    job.id,
                    retry_plan.exhausted_reason,
                )
                await self.mark_failed(
                    job,
                    retry_plan.terminal_error_message or (job.last_error or ""),
                    failed_at_utc=timestamp,
                    increment_attempt=False,
                )
                continue

            logger.info(
                "Requeueing transient-failed job for catch-up delivery job_id=%s previous_next_run_at=%s",
                job.id,
                job.next_run_at,
            )
            job.status = StoryJobStatus.PENDING
            job.retry_count = 0
            job.retry_window_started_at = None
            job.last_error = format_recovered_failure_message(job.last_error or "")
            job.next_run_at = retry_plan.next_run_at or next_retry_at
            job.lock_token = None
            job.locked_at = None
            job.updated_at = timestamp
            await self._story_jobs.save(job)
            repaired += 1

        if repaired:
            logger.info("Requeued transient-failed jobs for catch-up delivery repaired=%s", repaired)
        else:
            logger.debug("No transient-failed jobs required catch-up repair")
        return repaired

    async def cancel_pending(self, job_id: int) -> bool:
        logger.info("Cancelling pending job id=%s", job_id)
        return await self._story_jobs.delete_pending(job_id)
