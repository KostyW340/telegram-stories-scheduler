from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from app.config.settings import Settings, load_settings
from app.db.models import MediaType, ScheduleType, StoryJob, StoryJobStatus
from app.db.repositories.story_jobs import StoryJobCreateInput, StoryJobRepository
from app.db.session import session_scope
from app.scheduler.service import SchedulerService
from app.scheduler.rules import compute_next_run_at, encode_weekdays, parse_date_string, parse_time_string, utcnow_naive
from app.services.story_dispatch import StoryDispatchService, StoryDispatchTrigger
from app.telegram.failures import classify_publish_exception
from app.telegram.runtime import TelegramRuntime

logger = logging.getLogger(__name__)


class StoryJobInputError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        earliest_allowed_local: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.earliest_allowed_local = earliest_allowed_local


class DeleteStoryJobAction(StrEnum):
    CANCELLED = "cancelled"
    PURGED = "purged"


class ManualSendStoryJobOutcome(StrEnum):
    PUBLISHED = "published"
    NOT_FOUND = "not_found"
    PROCESSING = "processing"
    ALREADY_SENT = "already_sent"
    CANCELLED = "cancelled"
    PUBLISH_FAILED = "publish_failed"


@dataclass(slots=True, frozen=True)
class CreateStoryJobCommand:
    schedule_type: ScheduleType
    media_type: MediaType
    media_path: str
    prepared_media_path: str | None
    caption: str | None
    scheduled_time: time | str
    timezone: str
    scheduled_date: date | str | None = None
    weekdays: tuple[int, ...] = ()
    legacy_id: int | None = None


@dataclass(slots=True, frozen=True)
class DeleteStoryJobResult:
    job_id: int
    found: bool
    success: bool
    action: DeleteStoryJobAction | None
    status: StoryJobStatus | None


@dataclass(slots=True, frozen=True)
class ManualSendStoryJobResult:
    job_id: int
    outcome: ManualSendStoryJobOutcome
    found: bool
    success: bool
    previous_status: StoryJobStatus | None
    final_status: StoryJobStatus | None
    operator_message: str
    story_id: int | None = None
    update_type: str | None = None


class StoryJobService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        dispatch_service: StoryDispatchService | None = None,
        telegram_runtime: TelegramRuntime | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        self._dispatch_service = dispatch_service or StoryDispatchService(
            self._settings,
            telegram_runtime=telegram_runtime,
        )

    async def create_job(self, command: CreateStoryJobCommand) -> StoryJob:
        scheduled_time = (
            parse_time_string(command.scheduled_time)
            if isinstance(command.scheduled_time, str)
            else command.scheduled_time
        )
        scheduled_date = (
            parse_date_string(command.scheduled_date)
            if isinstance(command.scheduled_date, str)
            else command.scheduled_date
        )
        encoded_days = encode_weekdays(command.weekdays) if command.weekdays else None
        next_run_at = compute_next_run_at(
            command.schedule_type,
            scheduled_time,
            command.timezone,
            scheduled_date=scheduled_date,
            weekdays=command.weekdays,
        )
        if next_run_at is None:
            raise StoryJobInputError(
                "Указанные дата и время уже прошли. Укажите время позже.",
                code="schedule_in_past",
            )
        if command.schedule_type == ScheduleType.ONCE:
            min_lead = timedelta(seconds=self._settings.runtime.one_time_min_lead_seconds)
            time_until_run = next_run_at - utcnow_naive()
            if time_until_run < min_lead:
                earliest_allowed_local = self.minimum_allowed_one_time_local_datetime(command.timezone)
                logger.warning(
                    "Rejecting one-time story too close to now scheduled_date=%s scheduled_time=%s next_run_at=%s min_lead_seconds=%s earliest_allowed_local=%s",
                    scheduled_date,
                    scheduled_time,
                    next_run_at,
                    self._settings.runtime.one_time_min_lead_seconds,
                    earliest_allowed_local,
                )
                min_lead_minutes = max(1, self._settings.runtime.one_time_min_lead_seconds // 60)
                raise StoryJobInputError(
                    f"Время выбрано слишком близко. Укажите публикацию минимум через {min_lead_minutes} мин.",
                    code="schedule_too_close",
                    earliest_allowed_local=earliest_allowed_local,
                )
        logger.info(
            "Creating story job schedule_type=%s media_type=%s next_run_at=%s",
            command.schedule_type.value,
            command.media_type.value,
            next_run_at,
        )

        async with session_scope(self._settings) as session:
            repository = StoryJobRepository(session)
            return await repository.create(
                StoryJobCreateInput(
                    schedule_type=command.schedule_type,
                    scheduled_time=scheduled_time,
                    timezone=command.timezone,
                    media_type=command.media_type,
                    media_path=command.media_path,
                    prepared_media_path=command.prepared_media_path,
                    caption=command.caption,
                    scheduled_date=scheduled_date,
                    days=encoded_days,
                    next_run_at=next_run_at,
                    legacy_id=command.legacy_id,
                )
            )

    def minimum_allowed_one_time_local_datetime(self, timezone_name: str) -> datetime:
        zone = ZoneInfo(timezone_name)
        current_local = datetime.now(zone)
        earliest_local = current_local + timedelta(seconds=self._settings.runtime.one_time_min_lead_seconds)
        if earliest_local.second or earliest_local.microsecond:
            earliest_local = earliest_local.replace(second=0, microsecond=0) + timedelta(minutes=1)
        logger.debug(
            "Computed minimum allowed one-time schedule timezone=%s earliest_allowed_local=%s",
            timezone_name,
            earliest_local,
        )
        return earliest_local

    async def list_jobs(
        self,
        limit: int = 100,
        statuses: tuple[StoryJobStatus, ...] | None = None,
    ) -> list[StoryJob]:
        async with session_scope(self._settings) as session:
            repository = StoryJobRepository(session)
            jobs = await repository.list_jobs(limit=limit, statuses=statuses)
            logger.debug("Loaded %s jobs for listing", len(jobs))
            return jobs

    async def delete_pending(self, job_id: int) -> bool:
        async with session_scope(self._settings) as session:
            repository = StoryJobRepository(session)
            return await repository.delete_pending(job_id)

    async def delete_job(self, job_id: int) -> DeleteStoryJobResult:
        async with session_scope(self._settings) as session:
            repository = StoryJobRepository(session)
            job = await repository.get(job_id)
            if job is None:
                logger.warning("Delete requested for unknown job id=%s", job_id)
                return DeleteStoryJobResult(job_id=job_id, found=False, success=False, action=None, status=None)

            logger.debug("Delete decision for job_id=%s current_status=%s", job_id, job.status.value)
            if job.status == StoryJobStatus.PROCESSING:
                logger.warning("Delete requested for processing job id=%s", job_id)
                return DeleteStoryJobResult(job_id=job_id, found=True, success=False, action=None, status=job.status)

            if job.status in (StoryJobStatus.PENDING, StoryJobStatus.FAILED):
                cancelled_job = await repository.cancel(job)
                logger.info("Cancelled active job id=%s previous_status=%s", job_id, job.status.value)
                return DeleteStoryJobResult(
                    job_id=job_id,
                    found=True,
                    success=True,
                    action=DeleteStoryJobAction.CANCELLED,
                    status=cancelled_job.status,
                )

            if job.status in (StoryJobStatus.SENT, StoryJobStatus.CANCELLED):
                previous_status = job.status
                await repository.purge(job)
                logger.info("Purged historical job id=%s previous_status=%s", job_id, previous_status.value)
                return DeleteStoryJobResult(
                    job_id=job_id,
                    found=True,
                    success=True,
                    action=DeleteStoryJobAction.PURGED,
                    status=previous_status,
                )

            logger.warning("Delete requested for unsupported job state id=%s status=%s", job_id, job.status.value)
            return DeleteStoryJobResult(job_id=job_id, found=True, success=False, action=None, status=job.status)

    async def manual_send_job(self, job_id: int, *, operator_user_id: int) -> ManualSendStoryJobResult:
        async with session_scope(self._settings) as session:
            repository = StoryJobRepository(session)
            scheduler = SchedulerService(session, self._settings)
            job = await repository.get(job_id)
            if job is None:
                logger.warning(
                    "Manual send requested for unknown job_id=%s operator_user_id=%s",
                    job_id,
                    operator_user_id,
                )
                return ManualSendStoryJobResult(
                    job_id=job_id,
                    outcome=ManualSendStoryJobOutcome.NOT_FOUND,
                    found=False,
                    success=False,
                    previous_status=None,
                    final_status=None,
                    operator_message="Задача с таким ID не найдена.",
                )

            logger.info(
                "Evaluating manual send request job_id=%s operator_user_id=%s current_status=%s schedule_type=%s next_run_at=%s",
                job.id,
                operator_user_id,
                job.status.value,
                job.schedule_type.value,
                job.next_run_at,
            )
            if job.status == StoryJobStatus.PROCESSING:
                logger.warning(
                    "Manual send rejected because job is already processing job_id=%s operator_user_id=%s",
                    job.id,
                    operator_user_id,
                )
                return ManualSendStoryJobResult(
                    job_id=job.id,
                    outcome=ManualSendStoryJobOutcome.PROCESSING,
                    found=True,
                    success=False,
                    previous_status=job.status,
                    final_status=job.status,
                    operator_message="Задача уже отправляется. Дождитесь завершения текущей попытки.",
                )

            if job.status == StoryJobStatus.SENT:
                logger.warning(
                    "Manual send rejected because job is already sent job_id=%s operator_user_id=%s",
                    job.id,
                    operator_user_id,
                )
                return ManualSendStoryJobResult(
                    job_id=job.id,
                    outcome=ManualSendStoryJobOutcome.ALREADY_SENT,
                    found=True,
                    success=False,
                    previous_status=job.status,
                    final_status=job.status,
                    operator_message="Задача уже отправлена и не может быть отправлена повторно.",
                )

            if job.status == StoryJobStatus.CANCELLED:
                logger.warning(
                    "Manual send rejected because job is cancelled job_id=%s operator_user_id=%s",
                    job.id,
                    operator_user_id,
                )
                return ManualSendStoryJobResult(
                    job_id=job.id,
                    outcome=ManualSendStoryJobOutcome.CANCELLED,
                    found=True,
                    success=False,
                    previous_status=job.status,
                    final_status=job.status,
                    operator_message="Задача отменена и не может быть отправлена принудительно.",
                )

            if job.status not in (StoryJobStatus.PENDING, StoryJobStatus.FAILED):
                logger.warning(
                    "Manual send rejected because job state is unsupported job_id=%s operator_user_id=%s status=%s",
                    job.id,
                    operator_user_id,
                    job.status.value,
                )
                return ManualSendStoryJobResult(
                    job_id=job.id,
                    outcome=ManualSendStoryJobOutcome.PUBLISH_FAILED,
                    found=True,
                    success=False,
                    previous_status=job.status,
                    final_status=job.status,
                    operator_message="Задача сейчас нельзя отправить принудительно.",
                )

            claimed_at = utcnow_naive()
            lock_token = uuid.uuid4().hex
            claimed_job = await repository.claim_for_dispatch(
                job.id,
                lock_token=lock_token,
                claimed_at=claimed_at,
                stale_after=timedelta(seconds=self._settings.runtime.job_lock_ttl_seconds),
            )
            if claimed_job is None:
                current_job = await repository.get(job.id)
                logger.warning(
                    "Manual send claim was rejected job_id=%s operator_user_id=%s current_status=%s",
                    job.id,
                    operator_user_id,
                    None if current_job is None else current_job.status.value,
                )
                current_status = None if current_job is None else current_job.status
                outcome = (
                    ManualSendStoryJobOutcome.NOT_FOUND
                    if current_job is None
                    else ManualSendStoryJobOutcome.PROCESSING
                    if current_status == StoryJobStatus.PROCESSING
                    else ManualSendStoryJobOutcome.ALREADY_SENT
                    if current_status == StoryJobStatus.SENT
                    else ManualSendStoryJobOutcome.CANCELLED
                    if current_status == StoryJobStatus.CANCELLED
                    else ManualSendStoryJobOutcome.PUBLISH_FAILED
                )
                operator_message = (
                    "Задача с таким ID не найдена."
                    if outcome == ManualSendStoryJobOutcome.NOT_FOUND
                    else "Задача уже отправляется. Дождитесь завершения текущей попытки."
                    if outcome == ManualSendStoryJobOutcome.PROCESSING
                    else "Задача уже отправлена и не может быть отправлена повторно."
                    if outcome == ManualSendStoryJobOutcome.ALREADY_SENT
                    else "Задача отменена и не может быть отправлена принудительно."
                    if outcome == ManualSendStoryJobOutcome.CANCELLED
                    else "Не удалось захватить задачу для принудительной отправки."
                )
                return ManualSendStoryJobResult(
                    job_id=job.id,
                    outcome=outcome,
                    found=current_job is not None,
                    success=False,
                    previous_status=job.status,
                    final_status=current_status,
                    operator_message=operator_message,
                )

            logger.info(
                "Manual send accepted and claimed job_id=%s operator_user_id=%s previous_status=%s lock_token=%s",
                claimed_job.id,
                operator_user_id,
                job.status.value,
                lock_token,
            )
            try:
                dispatch_result = await self._dispatch_service.dispatch_claimed_job(
                    session,
                    claimed_job,
                    trigger=StoryDispatchTrigger.MANUAL_SEND,
                )
            except Exception as exc:
                logger.exception(
                    "Manual send dispatch failed job_id=%s operator_user_id=%s",
                    claimed_job.id,
                    operator_user_id,
                )
                failure = classify_publish_exception(
                    exc,
                    max_flood_wait_seconds=self._settings.runtime.publish_retry_max_flood_wait_seconds,
                )
                failed_job = await scheduler.mark_failed(claimed_job, failure.operator_message)
                logger.warning(
                    "Manual send finished with failure job_id=%s operator_user_id=%s final_status=%s reason=%s",
                    failed_job.id,
                    operator_user_id,
                    failed_job.status.value,
                    failure.reason_code,
                )
                return ManualSendStoryJobResult(
                    job_id=failed_job.id,
                    outcome=ManualSendStoryJobOutcome.PUBLISH_FAILED,
                    found=True,
                    success=False,
                    previous_status=job.status,
                    final_status=failed_job.status,
                    operator_message=f"Не удалось отправить сторис принудительно: {failure.operator_message}",
                )

            sent_job = await scheduler.mark_sent(claimed_job)
            logger.info(
                "Manual send completed successfully job_id=%s operator_user_id=%s final_status=%s story_id=%s update_type=%s",
                sent_job.id,
                operator_user_id,
                sent_job.status.value,
                dispatch_result.published_story.story_id,
                dispatch_result.published_story.update_type,
            )
            return ManualSendStoryJobResult(
                job_id=sent_job.id,
                outcome=ManualSendStoryJobOutcome.PUBLISHED,
                found=True,
                success=True,
                previous_status=job.status,
                final_status=sent_job.status,
                operator_message=f"Сторис по задаче {sent_job.id} отправлена принудительно.",
                story_id=dispatch_result.published_story.story_id,
                update_type=dispatch_result.published_story.update_type,
            )
