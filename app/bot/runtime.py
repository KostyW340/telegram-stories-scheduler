from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError, TelegramUnauthorizedError
from aiogram.utils.backoff import BackoffConfig

from app.config.settings import Settings, load_settings

logger = logging.getLogger(__name__)


def build_polling_backoff_config(settings: Settings | None = None) -> BackoffConfig:
    current_settings = settings or load_settings()
    config = BackoffConfig(
        min_delay=current_settings.runtime.bot_polling_backoff_min_delay_seconds,
        max_delay=current_settings.runtime.bot_polling_backoff_max_delay_seconds,
        factor=current_settings.runtime.bot_polling_backoff_factor,
        jitter=current_settings.runtime.bot_polling_backoff_jitter,
    )
    logger.info(
        "Configured aiogram polling backoff min=%s max=%s factor=%s jitter=%s",
        config.min_delay,
        config.max_delay,
        config.factor,
        config.jitter,
    )
    return config


def create_bot_client(settings: Settings | None = None) -> Bot:
    current_settings = settings or load_settings()
    token = current_settings.bot.require_token()
    session_kwargs = {}
    if current_settings.bot.proxy_url:
        logger.info("Using Telegram Bot API proxy")
        session_kwargs["proxy"] = current_settings.bot.proxy_url
    if current_settings.bot.api_base_url:
        logger.info("Using custom Telegram Bot API base URL %s", current_settings.bot.api_base_url)
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(current_settings.bot.api_base_url),
            **session_kwargs,
        )
        return Bot(token=token, session=session)
    if session_kwargs:
        session = AiohttpSession(**session_kwargs)
        return Bot(token=token, session=session)
    return Bot(token=token)


def _compute_supervisor_delay(config: BackoffConfig, restart_attempt: int) -> float:
    if restart_attempt <= 1:
        return float(config.min_delay)
    delay = float(config.min_delay) * (float(config.factor) ** float(restart_attempt - 1))
    return min(float(config.max_delay), delay)


def _is_retryable_polling_exception(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            TelegramNetworkError,
            TelegramServerError,
            TelegramRetryAfter,
            ConnectionError,
            OSError,
            TimeoutError,
            asyncio.TimeoutError,
        ),
    )


def _is_fatal_polling_exception(exc: BaseException) -> bool:
    return isinstance(exc, (TelegramConflictError, TelegramUnauthorizedError))


def _supervisor_monotonic() -> float:
    return time.monotonic()


def _should_reset_backoff(run_duration_seconds: float, settings: Settings) -> bool:
    return run_duration_seconds >= float(settings.runtime.bot_polling_backoff_reset_after_seconds)


async def run_supervised_polling(
    run_once: Callable[[], Awaitable[None]],
    *,
    settings: Settings | None = None,
    current_logger: logging.Logger | None = None,
) -> None:
    current_settings = settings or load_settings()
    active_logger = current_logger or logger
    backoff_config = build_polling_backoff_config(current_settings)
    restart_attempt = 0

    while True:
        run_started_at = _supervisor_monotonic()
        try:
            await run_once()
        except asyncio.CancelledError:
            active_logger.info("Bot polling supervisor cancelled")
            raise
        except Exception as exc:
            run_duration_seconds = _supervisor_monotonic() - run_started_at
            if restart_attempt > 0 and _should_reset_backoff(run_duration_seconds, current_settings):
                active_logger.info(
                    "Resetting bot polling backoff after healthy runtime run_duration_seconds=%.3f previous_restart_attempt=%s",
                    run_duration_seconds,
                    restart_attempt,
                )
                restart_attempt = 0
            if _is_fatal_polling_exception(exc):
                active_logger.exception("Bot polling failed with a fatal error; supervisor will stop")
                raise
            if not _is_retryable_polling_exception(exc):
                active_logger.exception("Bot polling failed with a non-retryable error; supervisor will stop")
                raise
            restart_attempt += 1
            delay = _compute_supervisor_delay(backoff_config, restart_attempt)
            active_logger.warning(
                "Bot polling crashed with a retryable error and will restart restart_attempt=%s delay_seconds=%.3f error=%s",
                restart_attempt,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
            continue

        run_duration_seconds = _supervisor_monotonic() - run_started_at
        if restart_attempt > 0 and _should_reset_backoff(run_duration_seconds, current_settings):
            active_logger.info(
                "Resetting bot polling backoff after healthy runtime run_duration_seconds=%.3f previous_restart_attempt=%s",
                run_duration_seconds,
                restart_attempt,
            )
            restart_attempt = 0
        restart_attempt += 1
        delay = _compute_supervisor_delay(backoff_config, restart_attempt)
        active_logger.warning(
            "Bot polling stopped unexpectedly without an error; scheduling restart restart_attempt=%s delay_seconds=%.3f",
            restart_attempt,
            delay,
        )
        await asyncio.sleep(delay)
