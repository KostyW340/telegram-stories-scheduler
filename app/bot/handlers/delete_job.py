from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.access import ensure_user_allowed
from app.bot.fsm.create_story import DeleteStoryStates
from app.bot.keyboards.main import cancel_keyboard, main_menu_keyboard
from app.config.settings import Settings
from app.db.models import StoryJobStatus
from app.services.story_jobs import DeleteStoryJobAction, StoryJobService

logger = logging.getLogger(__name__)


def render_delete_result(
    job_id: int,
    *,
    found: bool,
    success: bool,
    action: DeleteStoryJobAction | None,
    status: StoryJobStatus | None,
) -> str:
    if success and action == DeleteStoryJobAction.CANCELLED:
        return f"✅ Задача {job_id} отменена и больше не будет отправлена."
    if success and action == DeleteStoryJobAction.PURGED:
        return f"✅ Задача {job_id} удалена из списка."
    if not found:
        return f"❌ Задача с ID {job_id} не найдена."
    if status == StoryJobStatus.PROCESSING:
        return f"❌ Задача {job_id} сейчас обрабатывается. Попробуйте удалить её позже."
    if status == StoryJobStatus.SENT:
        return f"❌ Задача {job_id} уже отправлена. Повторите попытку с новой сборкой, если она всё ещё не удаляется."
    if status == StoryJobStatus.CANCELLED:
        return f"❌ Задача {job_id} уже отменена."
    if status == StoryJobStatus.FAILED:
        return f"❌ Задача {job_id} завершилась с ошибкой и не была удалена."
    return f"❌ Задача {job_id} недоступна для удаления."


async def delete_task_start(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(callback, settings):
        return
    logger.info("Starting delete flow for user_id=%s", callback.from_user.id if callback.from_user else None)
    await callback.message.answer(
        "Введите ID задачи для удаления.\nАктивная задача будет отменена, а уже отправленная — удалена из списка.",
        reply_markup=cancel_keyboard(),
    )
    await state.set_state(DeleteStoryStates.waiting_job_id)
    await callback.answer()


async def delete_task_by_id(
    message: Message,
    state: FSMContext,
    settings: Settings,
    story_job_service: StoryJobService,
) -> None:
    if not await ensure_user_allowed(message, settings):
        return
    raw_id = (message.text or "").strip()
    if not raw_id.isdigit():
        await message.answer("Введите корректный числовой ID.", reply_markup=main_menu_keyboard())
        return

    job_id = int(raw_id)
    result = await story_job_service.delete_job(job_id)
    await state.clear()
    if result.success:
        logger.info("Delete flow completed job_id=%s action=%s", job_id, result.action.value if result.action else None)
        await message.answer(
            render_delete_result(job_id, found=True, success=True, action=result.action, status=result.status),
            reply_markup=main_menu_keyboard(),
        )
    else:
        logger.warning("Delete request could not remove job id=%s found=%s status=%s", job_id, result.found, result.status.value if result.status else None)
        await message.answer(
            render_delete_result(job_id, found=result.found, success=False, action=result.action, status=result.status),
            reply_markup=main_menu_keyboard(),
        )


def build_delete_job_router() -> Router:
    router = Router(name="delete_job")
    router.callback_query.register(delete_task_start, F.data == "delete_task")
    router.message.register(delete_task_by_id, DeleteStoryStates.waiting_job_id)
    return router
