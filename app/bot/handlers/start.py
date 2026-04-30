from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.access import ensure_user_allowed
from app.bot.keyboards.main import main_menu_keyboard
from app.config.settings import Settings

logger = logging.getLogger(__name__)


async def command_start(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(message, settings):
        return
    await state.clear()
    logger.info("Handling /start for user_id=%s", message.from_user.id if message.from_user else None)
    await message.answer(
        "Добро пожаловать в планировщик сторис.\n\n"
        "Выберите действие из меню ниже.",
        reply_markup=main_menu_keyboard(),
    )


async def cancel_action(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(callback, settings):
        return
    await state.clear()
    logger.info("Cancelling current bot flow for user_id=%s", callback.from_user.id if callback.from_user else None)
    await callback.message.edit_text("❌ Действие отменено.", reply_markup=main_menu_keyboard())
    await callback.answer()


def build_start_router() -> Router:
    router = Router(name="start")
    router.message.register(command_start, CommandStart())
    router.callback_query.register(cancel_action, F.data == "cancel")
    return router
