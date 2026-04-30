from __future__ import annotations

import logging

from app.config.logging import configure_logging
from app.config.process_lock import acquire_runtime_mode_lock
from app.config.runtime import explain_windows_asyncio_failure, prepare_windows_runtime, run_async_entrypoint
from app.config.settings import load_settings
from app.db.bootstrap import run_migrations
from app.telegram.bootstrap import TelegramBootstrapError, explain_telegram_bootstrap_failure

logger = logging.getLogger(__name__)

prepare_windows_runtime()


async def run_worker_cli() -> int:
    from app.worker.service import WorkerService

    settings = load_settings()
    configure_logging(
        settings.runtime.log_level,
        settings.paths.logs_dir,
        connectivity_summary_interval_seconds=settings.runtime.connectivity_summary_interval_seconds,
    )
    with acquire_runtime_mode_lock("worker", settings.paths.data_dir, logger):
        run_migrations(settings)
        configure_logging(
            settings.runtime.log_level,
            settings.paths.logs_dir,
            connectivity_summary_interval_seconds=settings.runtime.connectivity_summary_interval_seconds,
        )
        worker = WorkerService(settings)
        await worker.run_forever()
    return 0


def main() -> int:
    try:
        prepare_windows_runtime(logger)
        return run_async_entrypoint(run_worker_cli, logger)
    except KeyboardInterrupt:
        logger.warning("Worker runtime interrupted by operator")
        return 130
    except TelegramBootstrapError as exc:
        logger.error(
            "Worker runtime Telegram bootstrap failure stage=%s state=%s detail=%s",
            exc.result.stage.value,
            exc.result.state.value,
            exc.result.detail,
        )
        print(explain_telegram_bootstrap_failure(exc) or f"Ошибка запуска worker: {exc}")
        return 1
    except ConnectionError as exc:
        logger.exception("Windows asyncio startup failure in worker runtime")
        message = explain_windows_asyncio_failure(exc) or "Ошибка запуска worker: сбой инициализации сетевого рантайма Windows."
        print(message)
        return 1
    except RuntimeError as exc:
        logger.error("Worker runtime configuration/runtime error: %s", exc)
        print(f"Ошибка запуска worker: {exc}")
        return 1
    except Exception:
        logger.exception("Unhandled worker runtime failure")
        print("Ошибка запуска worker: непредвиденная ошибка. Проверьте логи.")
        return 1
