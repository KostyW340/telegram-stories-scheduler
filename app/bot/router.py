from __future__ import annotations

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers.create_story import build_create_story_router
from app.bot.handlers.delete_job import build_delete_job_router
from app.bot.handlers.list_jobs import build_list_jobs_router
from app.bot.handlers.manual_send import build_manual_send_router
from app.bot.handlers.start import build_start_router


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(build_start_router())
    dispatcher.include_router(build_create_story_router())
    dispatcher.include_router(build_list_jobs_router())
    dispatcher.include_router(build_manual_send_router())
    dispatcher.include_router(build_delete_job_router())
    return dispatcher
