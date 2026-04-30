from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class TelegramSessionState(str, Enum):
    MISSING = "missing"
    AUTHORIZED = "authorized"
    UNAUTHORIZED = "unauthorized"
    REVOKED = "revoked"
    NETWORK_UNAVAILABLE = "network_unavailable"
    INVALID = "invalid"


class TelegramOperatorAction(str, Enum):
    NONE = "none"
    RUN_AUTH = "run_auth"
    RESTORE_NETWORK = "restore_network"
    DELETE_STALE_RUNTIME = "delete_stale_runtime"
    MOVE_OUT_OF_SYNC_FOLDER = "move_out_of_sync_folder"


class TelegramBootstrapStage(str, Enum):
    FILE_SESSION_VALIDATION = "file_session_validation"
    RUNTIME_SESSION_REFRESH = "runtime_session_refresh"
    RUNTIME_SESSION_CONNECT = "runtime_session_connect"
    INTERACTIVE_AUTH_BOOTSTRAP = "interactive_auth_bootstrap"


@dataclass(slots=True, frozen=True)
class TelegramBootstrapResult:
    state: TelegramSessionState
    operator_action: TelegramOperatorAction
    stage: TelegramBootstrapStage
    detail: str
    authorized_user_id: int | None = None
    reused_runtime_session: bool = False


class TelegramBootstrapError(RuntimeError):
    def __init__(self, result: TelegramBootstrapResult) -> None:
        super().__init__(result.detail)
        self.result = result


class TelegramSessionMissingError(TelegramBootstrapError):
    pass


class TelegramSessionUnauthorizedError(TelegramBootstrapError):
    pass


class TelegramSessionRevokedError(TelegramBootstrapError):
    pass


class TelegramNetworkUnavailableError(TelegramBootstrapError):
    pass


class TelegramSessionInvalidError(TelegramBootstrapError):
    pass


SYNC_FOLDER_MARKERS = (
    "yandex.disk",
    "yandexdisk",
    "onedrive",
    "dropbox",
    "google drive",
    "googledrive",
    "icloud",
)


def raise_for_bootstrap_result(result: TelegramBootstrapResult) -> None:
    if result.state is TelegramSessionState.MISSING:
        raise TelegramSessionMissingError(result)
    if result.state is TelegramSessionState.UNAUTHORIZED:
        raise TelegramSessionUnauthorizedError(result)
    if result.state is TelegramSessionState.REVOKED:
        raise TelegramSessionRevokedError(result)
    if result.state is TelegramSessionState.NETWORK_UNAVAILABLE:
        raise TelegramNetworkUnavailableError(result)
    if result.state is TelegramSessionState.INVALID:
        raise TelegramSessionInvalidError(result)


def explain_telegram_bootstrap_failure(exc: BaseException) -> str | None:
    if not isinstance(exc, TelegramBootstrapError):
        return None

    result = exc.result
    if isinstance(exc, TelegramNetworkUnavailableError):
        if result.stage is TelegramBootstrapStage.INTERACTIVE_AUTH_BOOTSTRAP:
            return (
                "Не удалось подключиться к Telegram, поэтому программа сейчас не может создать новую сессию. "
                "Проверьте интернет, VPN или прокси и повторите попытку."
            )
        return (
            "Не удалось подключиться к Telegram, поэтому программа сейчас не может проверить существующую сессию. "
            "Проверьте интернет, VPN или прокси и повторите попытку."
        )

    if isinstance(exc, TelegramSessionMissingError):
        return "Файл Telegram-сессии не найден. Запустите авторизацию заново."

    if isinstance(exc, TelegramSessionRevokedError):
        return "Текущая Telegram-сессия недействительна или была завершена. Пройдите авторизацию заново."

    if isinstance(exc, TelegramSessionUnauthorizedError):
        return "Найденный файл Telegram-сессии не авторизован. Пройдите авторизацию заново."

    if isinstance(exc, TelegramSessionInvalidError):
        return (
            "Файлы Telegram-сессии повреждены или непригодны для работы. "
            "Удалите старые session-файлы и пройдите авторизацию заново."
        )

    return result.detail


def detect_synced_runtime_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    detected: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        normalized = str(path).replace("\\", "/").casefold()
        if normalized in seen:
            continue
        if any(marker in normalized for marker in SYNC_FOLDER_MARKERS):
            detected.append(path)
            seen.add(normalized)
    return tuple(detected)


def format_synced_runtime_warning(paths: Iterable[Path]) -> str | None:
    detected = detect_synced_runtime_paths(paths)
    if not detected:
        return None
    joined = ", ".join(str(path) for path in detected)
    return (
        "Программа запущена из синхронизируемой папки. Session-файлы и runtime-файлы в таких местах "
        f"могут повреждаться или откатываться во время работы: {joined}. "
        "Для стабильной авторизации и публикации перенесите папку программы в обычную локальную директорию "
        "вроде C:\\TGStories и только потом проходите авторизацию заново."
    )


def log_telegram_bootstrap_result(
    current_logger: logging.Logger,
    *,
    prefix: str,
    result: TelegramBootstrapResult,
) -> None:
    message = (
        "%s stage=%s state=%s action=%s reused_runtime_session=%s user_id=%s detail=%s"
    )
    args = (
        prefix,
        result.stage.value,
        result.state.value,
        result.operator_action.value,
        result.reused_runtime_session,
        result.authorized_user_id,
        result.detail,
    )
    if result.state is TelegramSessionState.AUTHORIZED:
        current_logger.info(message, *args)
        return
    if result.state is TelegramSessionState.NETWORK_UNAVAILABLE:
        current_logger.warning(message, *args)
        return
    current_logger.error(message, *args)
