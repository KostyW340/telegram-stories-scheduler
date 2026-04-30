from __future__ import annotations

import logging

from app.bot.runtime import create_bot_client, run_supervised_polling
from app.config.process_lock import acquire_runtime_mode_lock
from app.config.runtime import explain_windows_asyncio_failure, prepare_windows_runtime, run_async_entrypoint
prepare_windows_runtime()

from app.bot.router import build_dispatcher
from app.config.logging import configure_logging
from app.config.settings import detect_sync_managed_path, load_settings
from app.db.bootstrap import run_migrations
from app.media.service import MediaPreparationService
from app.services.story_jobs import StoryJobService
from app.telegram.bootstrap import TelegramBootstrapError, explain_telegram_bootstrap_failure
from app.telegram.runtime import TelegramRuntime

logger = logging.getLogger(__name__)


def _warn_if_sync_managed_runtime_path(settings) -> None:
    risky_label = detect_sync_managed_path(settings.paths.data_dir) or detect_sync_managed_path(settings.paths.project_root)
    if risky_label is None:
        return
    logger.warning(
        "Предупреждение: режим бота запущен из синхронизируемой папки (%s). Это может мешать SQLite, session-файлам и медиа. "
        "Для надёжной работы перенесите программу в обычный локальный каталог.",
        risky_label,
    )


async def run_bot_cli() -> int:
    settings = load_settings()
    configure_logging(
        settings.runtime.log_level,
        settings.paths.logs_dir,
        connectivity_summary_interval_seconds=settings.runtime.connectivity_summary_interval_seconds,
    )
    _warn_if_sync_managed_runtime_path(settings)
    with acquire_runtime_mode_lock("bot", settings.paths.data_dir, logger):
        run_migrations(settings)
        configure_logging(
            settings.runtime.log_level,
            settings.paths.logs_dir,
            connectivity_summary_interval_seconds=settings.runtime.connectivity_summary_interval_seconds,
        )
        telegram_runtime = TelegramRuntime(settings)
        story_job_service = StoryJobService(settings, telegram_runtime=telegram_runtime)
        media_service = MediaPreparationService(settings)

        async def poll_once() -> None:
            logger.info("Starting bot-only control-plane polling attempt with a fresh dispatcher")
            bot = create_bot_client(settings)
            dispatcher = build_dispatcher()
            try:
                await dispatcher.start_polling(
                    bot,
                    settings=settings,
                    story_job_service=story_job_service,
                    media_service=media_service,
                    handle_signals=False,
                    close_bot_session=False,
                )
            finally:
                logger.info("Closing bot HTTP session")
                await bot.session.close()

        try:
            await run_supervised_polling(poll_once, settings=settings, current_logger=logger)
        finally:
            logger.info("Stopping Telegram runtime for bot-only mode")
            await telegram_runtime.stop()
    return 0


def main() -> int:
    try:
        prepare_windows_runtime(logger)
        return run_async_entrypoint(run_bot_cli, logger)
    except KeyboardInterrupt:
        logger.warning("Bot runtime interrupted by operator")
        return 130
    except TelegramBootstrapError as exc:
        logger.error(
            "Bot runtime Telegram bootstrap failure stage=%s state=%s detail=%s",
            exc.result.stage.value,
            exc.result.state.value,
            exc.result.detail,
        )
        print(explain_telegram_bootstrap_failure(exc) or f"Ошибка запуска бота: {exc}")
        return 1
    except ConnectionError as exc:
        logger.exception("Windows asyncio startup failure in bot runtime")
        message = explain_windows_asyncio_failure(exc) or "Ошибка запуска бота: сбой инициализации сетевого рантайма Windows."
        print(message)
        return 1
    except RuntimeError as exc:
        logger.error("Bot runtime configuration/runtime error: %s", exc)
        print(f"Ошибка запуска бота: {exc}")
        return 1
    except Exception:
        logger.exception("Unhandled bot runtime failure")
        print("Ошибка запуска бота: непредвиденная ошибка. Проверьте логи.")
        return 1
