from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from app.bot.access import ensure_user_allowed
from app.bot.keyboards.main import main_menu_keyboard
from app.bot.presenters.story_jobs import format_story_jobs_chunks
from app.config.settings import Settings
from app.db.models import StoryJobStatus
from app.services.story_jobs import StoryJobService

logger = logging.getLogger(__name__)


async def show_scheduled_jobs(
    callback: CallbackQuery,
    settings: Settings,
    story_job_service: StoryJobService,
) -> None:
    if not await ensure_user_allowed(callback, settings):
        return
    logger.info("Listing scheduled jobs for user_id=%s", callback.from_user.id if callback.from_user else None)
    jobs = await story_job_service.list_jobs()
    visible_jobs = [job for job in jobs if job.status != StoryJobStatus.CANCELLED]
    chunks = format_story_jobs_chunks(visible_jobs)
    try:
        await callback.message.edit_text(chunks[0], reply_markup=main_menu_keyboard())
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
        logger.debug("Skipping no-op scheduled jobs edit for user_id=%s", callback.from_user.id if callback.from_user else None)
    for chunk in chunks[1:]:
        await callback.message.answer(chunk)
    await callback.answer()


def build_list_jobs_router() -> Router:
    router = Router(name="list_jobs")
    router.callback_query.register(show_scheduled_jobs, F.data == "my_scheduled")
    return router
