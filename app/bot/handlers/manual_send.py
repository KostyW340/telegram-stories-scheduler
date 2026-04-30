from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.access import ensure_user_allowed
from app.bot.fsm.create_story import ManualSendStoryStates
from app.bot.keyboards.main import cancel_keyboard, main_menu_keyboard
from app.config.settings import Settings
from app.services.story_jobs import ManualSendStoryJobOutcome, ManualSendStoryJobResult, StoryJobService

logger = logging.getLogger(__name__)


def render_manual_send_result(result: ManualSendStoryJobResult) -> str:
    logger.debug(
        "Rendering manual-send result job_id=%s outcome=%s success=%s previous_status=%s final_status=%s",
        result.job_id,
        result.outcome.value,
        result.success,
        None if result.previous_status is None else result.previous_status.value,
        None if result.final_status is None else result.final_status.value,
    )
    if result.outcome == ManualSendStoryJobOutcome.PUBLISHED:
        if result.story_id is not None:
            return f"✅ Задача {result.job_id} отправлена принудительно.\nStory ID: {result.story_id}"
        return f"✅ Задача {result.job_id} отправлена принудительно."
    if result.outcome == ManualSendStoryJobOutcome.NOT_FOUND:
        return "❌ Задача с таким ID не найдена."
    if result.outcome == ManualSendStoryJobOutcome.PROCESSING:
        return "⚙️ Эта задача уже отправляется. Дождитесь завершения текущей попытки."
    if result.outcome == ManualSendStoryJobOutcome.ALREADY_SENT:
        return "✅ Эта задача уже была отправлена. Повторная принудительная отправка отключена."
    if result.outcome == ManualSendStoryJobOutcome.CANCELLED:
        return "🚫 Эта задача отменена и не может быть отправлена принудительно."
    return f"❌ {result.operator_message}"


async def manual_send_start(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(callback, settings):
        return
    logger.info("Starting manual-send flow for user_id=%s", callback.from_user.id if callback.from_user else None)
    await callback.message.answer(
        "Введите ID задачи для принудительной отправки.\n"
        "Разрешены только задачи в статусах pending и failed.",
        reply_markup=cancel_keyboard(),
    )
    await state.set_state(ManualSendStoryStates.waiting_job_id)
    await callback.answer()


async def manual_send_by_id(
    message: Message,
    state: FSMContext,
    settings: Settings,
    story_job_service: StoryJobService,
) -> None:
    if not await ensure_user_allowed(message, settings):
        return
    raw_id = (message.text or "").strip()
    if not raw_id.isdigit():
        logger.warning(
            "Manual-send flow received invalid job ID user_id=%s raw_id=%s",
            message.from_user.id if message.from_user else None,
            raw_id,
        )
        await message.answer("Введите корректный числовой ID.", reply_markup=cancel_keyboard())
        return

    job_id = int(raw_id)
    operator_user_id = message.from_user.id if message.from_user else 0
    logger.info("Submitting manual-send request job_id=%s operator_user_id=%s", job_id, operator_user_id)
    result = await story_job_service.manual_send_job(job_id, operator_user_id=operator_user_id)
    await state.clear()
    await message.answer(render_manual_send_result(result), reply_markup=main_menu_keyboard())


def build_manual_send_router() -> Router:
    router = Router(name="manual_send")
    router.callback_query.register(manual_send_start, F.data == "manual_send_task")
    router.message.register(manual_send_by_id, ManualSendStoryStates.waiting_job_id)
    return router
