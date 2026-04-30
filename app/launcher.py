from __future__ import annotations

import asyncio
import contextlib
import getpass
import logging

from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError

from app.auth.service import AuthPrompts, authorize_interactively
from app.bot.runtime import create_bot_client, run_supervised_polling
from app.config.logging import configure_logging
from app.config.process_lock import acquire_runtime_mode_lock
from app.config.runtime import explain_windows_asyncio_failure, prepare_windows_runtime, run_async_entrypoint
from app.config.settings import detect_sync_managed_path, load_settings
from app.db.bootstrap import run_migrations
from app.media.service import MediaPreparationService
from app.services.story_jobs import StoryJobService
from app.telegram.bootstrap import (
    TelegramBootstrapError,
    TelegramSessionState,
    detect_synced_runtime_paths,
    explain_telegram_bootstrap_failure,
    format_synced_runtime_warning,
    raise_for_bootstrap_result,
)
from app.telegram.client import assess_existing_session
from app.telegram.runtime import TelegramRuntime
from app.worker.service import WorkerService

prepare_windows_runtime()

logger = logging.getLogger(__name__)


def _warn_if_sync_managed_runtime_path(settings) -> None:
    risky_label = detect_sync_managed_path(settings.paths.data_dir) or detect_sync_managed_path(settings.paths.project_root)
    if risky_label is None:
        return
    logger.warning(
        "Предупреждение: программа запущена из синхронизируемой папки (%s). Это может мешать SQLite, session-файлам и медиа. "
        "Для надёжной работы перенесите всю папку программы в обычный локальный каталог, например C:\\TGSA.",
        risky_label,
    )


def _prompt_phone() -> str:
    return input("Введите номер телефона в международном формате: ").strip()


def _prompt_code() -> str:
    return input("Введите код подтверждения (пустой ввод = отправить код повторно): ").strip()


def _prompt_password() -> str:
    return getpass.getpass("Введите облачный пароль Telegram: ").strip()


async def _ensure_authorized_session() -> None:
    settings = load_settings()
    synced_paths = detect_synced_runtime_paths(
        (
            settings.session_file,
            settings.runtime_session_string_file,
            settings.paths.data_dir,
        )
    )
    if synced_paths:
        warning = format_synced_runtime_warning(synced_paths)
        logger.warning("Launcher detected synced runtime paths paths=%s", [str(path) for path in synced_paths])
        if warning:
            print(warning)

    bootstrap_result = await assess_existing_session(settings)
    if bootstrap_result.state is TelegramSessionState.AUTHORIZED:
        logger.info(
            "Launcher confirmed reusable Telegram session state=%s stage=%s session=%s runtime=%s",
            bootstrap_result.state.value,
            bootstrap_result.stage.value,
            settings.session_file,
            settings.runtime_session_string_file,
        )
        return
    if (
        bootstrap_result.state is TelegramSessionState.NETWORK_UNAVAILABLE
        and bootstrap_result.reused_runtime_session
    ):
        logger.warning(
            "Launcher could not validate Telegram session because the network is unavailable, but a reusable runtime session artifact exists stage=%s session=%s runtime=%s detail=%s",
            bootstrap_result.stage.value,
            settings.session_file,
            settings.runtime_session_string_file,
            bootstrap_result.detail,
        )
        print(
            "Не удалось подтвердить Telegram-сессию из-за недоступности Telegram. "
            "Программа запускается в ограниченном режиме и продолжит попытки после восстановления сети."
        )
        return
    if bootstrap_result.state is TelegramSessionState.NETWORK_UNAVAILABLE:
        logger.error(
            "Launcher cannot enter degraded startup because no reusable runtime session artifact was confirmed stage=%s session=%s runtime=%s detail=%s",
            bootstrap_result.stage.value,
            settings.session_file,
            settings.runtime_session_string_file,
            bootstrap_result.detail,
        )
        raise_for_bootstrap_result(bootstrap_result)

    logger.info(
        "Launcher requires interactive Telegram auth state=%s stage=%s detail=%s",
        bootstrap_result.state.value,
        bootstrap_result.stage.value,
        bootstrap_result.detail,
    )
    prompts = AuthPrompts(phone=_prompt_phone, code=_prompt_code, password=_prompt_password)
    result = await authorize_interactively(settings, prompts)
    print(
        "Авторизация завершена успешно.\n"
        f"Пользователь: {result.authorized_user_id}\n"
        f"Session: {result.session_file}"
    )


async def run_client_launcher() -> int:
    from app.bot.router import build_dispatcher

    settings = load_settings()
    configure_logging(
        settings.runtime.log_level,
        settings.paths.logs_dir,
        connectivity_summary_interval_seconds=settings.runtime.connectivity_summary_interval_seconds,
    )
    _warn_if_sync_managed_runtime_path(settings)

    with acquire_runtime_mode_lock("client", settings.paths.data_dir, logger):
        with acquire_runtime_mode_lock("bot", settings.paths.data_dir, logger):
            with acquire_runtime_mode_lock("worker", settings.paths.data_dir, logger):
                await _ensure_authorized_session()
                run_migrations(settings)
                configure_logging(
                    settings.runtime.log_level,
                    settings.paths.logs_dir,
                    connectivity_summary_interval_seconds=settings.runtime.connectivity_summary_interval_seconds,
                )

                telegram_runtime = TelegramRuntime(settings)
                worker = WorkerService(settings, telegram_runtime=telegram_runtime)
                worker_task = asyncio.create_task(worker.run_forever(), name="stories-worker")
                story_job_service = StoryJobService(settings, telegram_runtime=telegram_runtime)
                media_service = MediaPreparationService(settings)
                logger.info("Starting unified client runtime")
                bot_task: asyncio.Task[None] | None = None
                try:
                    async def poll_once() -> None:
                        logger.info("Starting unified client bot control-plane polling attempt with a fresh dispatcher")
                        bot = create_bot_client(settings)
                        dispatcher = build_dispatcher()
                        try:
                            await dispatcher.start_polling(
                                bot,
                                settings=settings,
                                story_job_service=story_job_service,
                                media_service=media_service,
                                telegram_runtime=telegram_runtime,
                                handle_signals=False,
                                close_bot_session=False,
                            )
                        finally:
                            logger.info("Closing bot HTTP session for unified client runtime")
                            await bot.session.close()

                    async def run_bot_control_plane() -> None:
                        try:
                            await run_supervised_polling(poll_once, settings=settings, current_logger=logger)
                        except asyncio.CancelledError:
                            logger.info("Unified client bot control plane cancelled")
                            raise
                        except TelegramConflictError:
                            logger.exception(
                                "Bot control plane stopped due to Telegram polling conflict; scheduled publishing will continue, but bot commands are unavailable until the conflict is removed"
                            )
                        except TelegramUnauthorizedError:
                            logger.exception(
                                "Bot control plane stopped due to Telegram bot authorization failure; scheduled publishing will continue, but bot commands are unavailable until BOT_TOKEN is fixed"
                            )
                        except Exception:
                            logger.exception(
                                "Bot control plane stopped due to an internal or non-retryable error; scheduled publishing will continue while the worker stays alive"
                            )
                            logger.error(
                                "Внутренняя ошибка бота остановила только контур команд. Публикация по расписанию продолжит работать, если worker и MTProto-канал доступны. Проверьте data/logs/app.log."
                            )
                        else:
                            logger.warning(
                                "Bot control plane exited without an exception; scheduled publishing will continue while the worker stays alive"
                            )

                    bot_task = asyncio.create_task(run_bot_control_plane(), name="stories-bot")
                    await asyncio.gather(worker_task, bot_task)
                finally:
                    logger.info("Stopping unified client runtime")
                    if bot_task is not None and not bot_task.done():
                        bot_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await bot_task
                    if not worker_task.done():
                        worker_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await worker_task
                    await telegram_runtime.stop()
    return 0


def main() -> int:
    try:
        prepare_windows_runtime(logger)
        return run_async_entrypoint(run_client_launcher, logger)
    except KeyboardInterrupt:
        logger.warning("Unified client runtime interrupted by operator")
        return 130
    except TelegramBootstrapError as exc:
        logger.error(
            "Unified client runtime Telegram bootstrap failure stage=%s state=%s detail=%s",
            exc.result.stage.value,
            exc.result.state.value,
            exc.result.detail,
        )
        print(explain_telegram_bootstrap_failure(exc) or f"Ошибка запуска программы: {exc}")
        return 1
    except ConnectionError as exc:
        logger.exception("Windows asyncio startup failure in unified client runtime")
        message = explain_windows_asyncio_failure(exc) or "Ошибка запуска программы: сбой инициализации сетевого рантайма Windows."
        print(message)
        return 1
    except RuntimeError as exc:
        logger.error("Unified client runtime configuration/runtime error: %s", exc)
        print(f"Ошибка запуска программы: {exc}")
        return 1
    except EOFError:
        logger.error("Unified client runtime could not read interactive input")
        print("Ошибка запуска программы: не удалось прочитать ввод оператора.")
        return 1
    except Exception:
        logger.exception("Unhandled unified client runtime failure")
        print("Ошибка запуска программы: непредвиденная ошибка. Проверьте логи.")
        return 1
