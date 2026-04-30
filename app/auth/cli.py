from __future__ import annotations

import getpass
import logging

from app.config.runtime import explain_windows_asyncio_failure, prepare_windows_runtime, run_async_entrypoint
prepare_windows_runtime()

from app.auth.service import AuthPrompts, authorize_interactively
from app.config.logging import configure_logging
from app.config.settings import load_settings
from app.telegram.bootstrap import TelegramBootstrapError, explain_telegram_bootstrap_failure

logger = logging.getLogger(__name__)


def _prompt_phone() -> str:
    return input("Введите номер телефона в международном формате: ").strip()


def _prompt_code() -> str:
    return input("Введите код подтверждения (пустой ввод = отправить код повторно): ").strip()


def _prompt_password() -> str:
    return getpass.getpass("Введите облачный пароль Telegram: ").strip()


async def run_auth_cli() -> int:
    settings = load_settings()
    configure_logging(
        settings.runtime.log_level,
        settings.paths.logs_dir,
        connectivity_summary_interval_seconds=settings.runtime.connectivity_summary_interval_seconds,
    )

    logger.info("Starting auth CLI")
    prompts = AuthPrompts(phone=_prompt_phone, code=_prompt_code, password=_prompt_password)
    result = await authorize_interactively(settings, prompts, force_reauth=True)

    if result.reused_existing_session:
        print(
            "Сессия уже была авторизована.\n"
            f"Пользователь: {result.authorized_user_id}\n"
            f"Session: {result.session_file}"
        )
    else:
        print(
            "Авторизация завершена успешно.\n"
            f"Пользователь: {result.authorized_user_id}\n"
            f"Session: {result.session_file}"
        )
    return 0


def main() -> int:
    try:
        prepare_windows_runtime(logger)
        return run_async_entrypoint(run_auth_cli, logger)
    except KeyboardInterrupt:
        logger.warning("Auth CLI interrupted by operator")
        print("Авторизация прервана.")
        return 130
    except TelegramBootstrapError as exc:
        logger.error(
            "Auth CLI Telegram bootstrap failure stage=%s state=%s detail=%s",
            exc.result.stage.value,
            exc.result.state.value,
            exc.result.detail,
        )
        print(explain_telegram_bootstrap_failure(exc) or f"Ошибка запуска авторизации: {exc}")
        return 1
    except ConnectionError as exc:
        logger.exception("Windows asyncio startup failure in auth CLI")
        message = explain_windows_asyncio_failure(exc) or "Ошибка запуска авторизации: сбой инициализации сетевого рантайма Windows."
        print(message)
        return 1
    except RuntimeError as exc:
        logger.error("Auth CLI configuration/runtime error: %s", exc)
        print(f"Ошибка запуска авторизации: {exc}")
        return 1
    except EOFError:
        logger.error("Auth CLI could not read interactive input")
        print("Ошибка запуска авторизации: не удалось прочитать ввод оператора.")
        return 1
    except Exception:
        logger.exception("Unhandled auth CLI failure")
        print("Ошибка запуска авторизации: непредвиденная ошибка. Проверьте логи.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
