from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StoryJob, StoryJobStatus

logger = logging.getLogger(__name__)


class DueJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_due_jobs(
        self,
        *,
        now_utc: datetime,
        limit: int,
        lock_token: str,
        stale_after: timedelta = timedelta(minutes=15),
    ) -> list[StoryJob]:
        stale_before = now_utc - stale_after
        eligible_statuses = (StoryJobStatus.PENDING, StoryJobStatus.FAILED)
        logger.debug(
            "Claiming due jobs now_utc=%s limit=%s lock_token=%s stale_before=%s",
            now_utc,
            limit,
            lock_token,
            stale_before,
        )
        eligible_ids = (
            select(StoryJob.id)
            .where(StoryJob.next_run_at.is_not(None))
            .where(StoryJob.next_run_at <= now_utc)
            .where(StoryJob.status.in_(eligible_statuses))
            .where(
                or_(
                    StoryJob.lock_token.is_(None),
                    StoryJob.locked_at.is_(None),
                    StoryJob.locked_at < stale_before,
                )
            )
            .order_by(StoryJob.next_run_at.asc(), StoryJob.id.asc())
            .limit(limit)
            .subquery()
        )
        statement = (
            update(StoryJob)
            .where(StoryJob.id.in_(select(eligible_ids.c.id)))
            .values(
                status=StoryJobStatus.PROCESSING,
                lock_token=lock_token,
                locked_at=now_utc,
                updated_at=now_utc,
            )
            .returning(StoryJob)
        )
        result = await self._session.scalars(statement)
        jobs = list(result.all())
        logger.debug("Claimed %s due jobs", len(jobs))
        return jobs
