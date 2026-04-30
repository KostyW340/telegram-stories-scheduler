from __future__ import annotations

import logging

from aiogram.types import CallbackQuery, Message

from app.config.settings import Settings

logger = logging.getLogger(__name__)


async def ensure_user_allowed(event: Message | CallbackQuery, settings: Settings) -> bool:
    allowed = set(settings.bot.allowed_user_ids)
    if not allowed:
        return True

    user = event.from_user
    user_id = user.id if user else None
    if user_id in allowed:
        return True

    logger.warning("Rejected unauthorized bot user_id=%s", user_id)
    if isinstance(event, CallbackQuery):
        await event.answer("У вас нет доступа к этому боту.", show_alert=True)
    else:
        await event.answer("У вас нет доступа к этому боту.")
    return False
