from __future__ import annotations

import asyncio
import logging

from app.config.settings import Settings, load_settings
from app.db.session import get_session_factory
from app.media.service import MediaPreparationService
from app.scheduler.service import SchedulerService
from app.services.story_dispatch import StoryDispatchService, StoryDispatchTrigger
from app.telegram.failures import classify_publish_exception
from app.telegram.health import TelegramConnectivityChannel, get_connectivity_monitor
from app.telegram.runtime import TelegramRuntime, TelegramRuntimeRole
from app.telegram.story_publisher import StoryPublisher

logger = logging.getLogger(__name__)


class WorkerService:
    def __init__(
        self,
        settings: Settings | None = None,
        media_service: MediaPreparationService | None = None,
        story_publisher: StoryPublisher | None = None,
        telegram_runtime: TelegramRuntime | None = None,
        dispatch_service: StoryDispatchService | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        self._telegram_runtime = telegram_runtime or TelegramRuntime(self._settings)
        self._dispatch_service = dispatch_service or StoryDispatchService(
            self._settings,
            media_service=media_service,
            story_publisher=story_publisher,
            telegram_runtime=self._telegram_runtime,
        )
        self._owns_telegram_runtime = telegram_runtime is None
        self._connectivity_monitor = get_connectivity_monitor()
        self._publisher_outage_logged = False
        self._bot_api_degraded_logged = False
        self._both_channels_degraded_logged = False
        self._repair_checked = False

    async def _handle_publish_failure(
        self,
        scheduler: SchedulerService,
        job: StoryJob,
        exc: BaseException,
    ) -> None:
        failure = classify_publish_exception(
            exc,
            max_flood_wait_seconds=self._settings.runtime.publish_retry_max_flood_wait_seconds,
        )
        logger.warning(
            "Classified publish failure job_id=%s retryable=%s reason=%s retry_after_seconds=%s",
            job.id,
            failure.retryable,
            failure.reason_code,
            failure.retry_after_seconds,
        )

        if failure.retryable:
            await self._telegram_runtime.invalidate_role(
                TelegramRuntimeRole.PUBLISHER,
                reason=f"publish-failure:{failure.reason_code}",
            )
            retry_plan = scheduler.plan_transient_retry(
                job,
                retry_after_seconds=failure.retry_after_seconds,
            )
            if retry_plan.should_retry:
                await scheduler.mark_retry(job, failure.operator_message, retry_plan)
                return

            logger.error(
                "Transient publish failure exhausted retry budget job_id=%s reason=%s exhausted_reason=%s",
                job.id,
                failure.reason_code,
                retry_plan.exhausted_reason,
            )
            await scheduler.mark_failed(
                job,
                retry_plan.terminal_error_message or failure.operator_message,
            )
            return

        await scheduler.mark_failed(job, failure.operator_message)

    async def run_once(self) -> int:
        if not self._repair_checked:
            session_factory = get_session_factory(self._settings)
            async with session_factory() as session:
                scheduler = SchedulerService(session, self._settings)
                repaired = await scheduler.repair_recoverable_failed_jobs()
                await session.commit()
            logger.info("Completed transient-failure repair check repaired=%s", repaired)
            self._repair_checked = True

        bot_api_degraded = self._connectivity_monitor.is_degraded(TelegramConnectivityChannel.BOT_API)
        if not await self._telegram_runtime.ensure_role_ready(TelegramRuntimeRole.PUBLISHER):
            if bot_api_degraded and not self._both_channels_degraded_logged:
                logger.warning(
                    "Both Telegram channels are currently degraded: bot commands/media upload are unavailable and scheduled publishing is delayed until the MTProto channel recovers"
                )
                self._both_channels_degraded_logged = True
            if not self._publisher_outage_logged:
                logger.info("Skipping due-job claim because Telegram MTProto runtime is currently unavailable")
                self._publisher_outage_logged = True
            else:
                logger.debug("Skipping due-job claim because Telegram MTProto runtime is still unavailable")
            return 0

        if self._both_channels_degraded_logged:
            logger.info("Telegram MTProto runtime recovered; scheduled publishing can resume")
            self._both_channels_degraded_logged = False
        if self._publisher_outage_logged:
            logger.info("Telegram MTProto runtime is available again; resuming due-job claims")
            self._publisher_outage_logged = False
        if bot_api_degraded:
            if not self._bot_api_degraded_logged:
                logger.info(
                    "Telegram Bot API is currently degraded, but scheduled publishing continues through the MTProto channel"
                )
                self._bot_api_degraded_logged = True
        elif self._bot_api_degraded_logged:
            logger.info("Telegram Bot API connectivity is available again; bot commands and media uploads should recover")
            self._bot_api_degraded_logged = False

        session_factory = get_session_factory(self._settings)
        async with session_factory() as session:
            scheduler = SchedulerService(session, self._settings)
            batch = await scheduler.claim_due_jobs()
            await session.commit()

            if not batch.jobs:
                logger.debug("Worker cycle found no due jobs")
                return 0

            logger.info("Worker cycle claimed %s due jobs", len(batch.jobs))
            processed = 0
            for job in batch.jobs:
                try:
                    await self._dispatch_service.dispatch_claimed_job(
                        session,
                        job,
                        trigger=StoryDispatchTrigger.WORKER,
                    )
                except Exception as exc:
                    logger.exception("Story publication failed for job_id=%s", job.id)
                    await self._handle_publish_failure(scheduler, job, exc)
                else:
                    await scheduler.mark_sent(job)
                finally:
                    await session.commit()
                    processed += 1

            return processed

    async def run_forever(self) -> None:
        poll_interval = self._settings.runtime.scheduler_poll_interval_seconds
        cycle_timeout = self._settings.runtime.worker_cycle_timeout_seconds
        logger.info("Starting worker loop poll_interval=%ss cycle_timeout=%ss", poll_interval, cycle_timeout)
        try:
            while True:
                try:
                    await asyncio.wait_for(self.run_once(), timeout=cycle_timeout)
                except TimeoutError:
                    logger.exception(
                        "Worker cycle timed out after %ss; invalidating Telegram publisher runtime",
                        cycle_timeout,
                    )
                    try:
                        await self._telegram_runtime.invalidate_role(
                            TelegramRuntimeRole.PUBLISHER,
                            reason="worker-cycle-timeout",
                        )
                    except Exception:
                        logger.exception("Failed to invalidate Telegram publisher runtime after worker cycle timeout")
                except Exception:
                    logger.exception("Worker cycle crashed")
                await asyncio.sleep(poll_interval)
        finally:
            if self._owns_telegram_runtime:
                await self._telegram_runtime.stop()
