from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

from telethon import errors as telethon_errors
from telethon.errors import common as telethon_common_errors

logger = logging.getLogger(__name__)

TRANSIENT_FAILURE_MESSAGE_PREFIX = "Временный сбой Telegram/сети: "
RECOVERED_FAILURE_MESSAGE_PREFIX = "Повторная отправка после временного сбоя: "
AUTO_POST_EXPIRED_MESSAGE_PREFIX = "Автоматическая отправка завершена после окончания дня: "
WEEKLY_ROLLOVER_MESSAGE_PREFIX = "Пропущенная еженедельная отправка перенесена на следующий день по графику: "
MEDIA_REUPLOAD_REQUIRED_MESSAGE_PREFIX = "Медиафайл для сторис требует восстановления: "


@dataclass(slots=True, frozen=True)
class PublishFailureInfo:
    retryable: bool
    reason_code: str
    operator_message: str
    retry_after_seconds: int | None = None


class MediaReuploadRequiredError(RuntimeError):
    """Raised when Telegram rejects media and the asset must be restored or re-uploaded."""


def format_transient_failure_message(message: str) -> str:
    normalized = message.strip()
    if not normalized:
        return TRANSIENT_FAILURE_MESSAGE_PREFIX.rstrip()
    if normalized.startswith(TRANSIENT_FAILURE_MESSAGE_PREFIX) or normalized.startswith(RECOVERED_FAILURE_MESSAGE_PREFIX):
        return normalized
    return f"{TRANSIENT_FAILURE_MESSAGE_PREFIX}{normalized}"


def format_recovered_failure_message(message: str) -> str:
    normalized = message.strip()
    if not normalized:
        return RECOVERED_FAILURE_MESSAGE_PREFIX.rstrip()
    if normalized.startswith(RECOVERED_FAILURE_MESSAGE_PREFIX):
        return normalized
    if normalized.startswith(TRANSIENT_FAILURE_MESSAGE_PREFIX):
        normalized = normalized[len(TRANSIENT_FAILURE_MESSAGE_PREFIX) :]
    return f"{RECOVERED_FAILURE_MESSAGE_PREFIX}{normalized}"


def format_auto_post_expired_message(message: str) -> str:
    normalized = message.strip()
    if not normalized:
        return AUTO_POST_EXPIRED_MESSAGE_PREFIX.rstrip()
    if normalized.startswith(AUTO_POST_EXPIRED_MESSAGE_PREFIX):
        return normalized
    return f"{AUTO_POST_EXPIRED_MESSAGE_PREFIX}{normalized}"


def format_weekly_rollover_message(message: str) -> str:
    normalized = message.strip()
    if not normalized:
        return WEEKLY_ROLLOVER_MESSAGE_PREFIX.rstrip()
    if normalized.startswith(WEEKLY_ROLLOVER_MESSAGE_PREFIX):
        return normalized
    return f"{WEEKLY_ROLLOVER_MESSAGE_PREFIX}{normalized}"


def format_media_reupload_required_message(message: str) -> str:
    normalized = message.strip()
    if not normalized:
        return MEDIA_REUPLOAD_REQUIRED_MESSAGE_PREFIX.rstrip()
    if normalized.startswith(MEDIA_REUPLOAD_REQUIRED_MESSAGE_PREFIX):
        return normalized
    return f"{MEDIA_REUPLOAD_REQUIRED_MESSAGE_PREFIX}{normalized}"


def looks_like_transient_failure_message(message: str | None) -> bool:
    if not message:
        return False

    normalized = message.strip().lower()
    if not normalized:
        return False
    if normalized.startswith(TRANSIENT_FAILURE_MESSAGE_PREFIX.lower()) or normalized.startswith(
        RECOVERED_FAILURE_MESSAGE_PREFIX.lower()
    ):
        return True

    transient_markers = (
        "cannot send requests while disconnected",
        "connectionreseterror",
        "server closed the connection",
        "timed out",
        "timeout",
        "cannot connect to host",
        "networkerror",
        "flood wait",
        "invalidbuffer",
        "ssl:default",
        "temporar",
        "transport",
        "disconnected",
    )
    return any(marker in normalized for marker in transient_markers)


def looks_like_auto_post_expired_message(message: str | None) -> bool:
    if not message:
        return False
    return message.strip().startswith(AUTO_POST_EXPIRED_MESSAGE_PREFIX)


def looks_like_weekly_rollover_message(message: str | None) -> bool:
    if not message:
        return False
    return message.strip().startswith(WEEKLY_ROLLOVER_MESSAGE_PREFIX)


def is_media_file_invalid_error(exc: BaseException) -> bool:
    return isinstance(exc, telethon_errors.RPCError) and "media_file_invalid" in str(exc).lower()


def classify_publish_exception(
    exc: BaseException,
    *,
    max_flood_wait_seconds: int,
) -> PublishFailureInfo:
    logger.debug("Classifying publish exception type=%s value=%s", type(exc).__name__, exc)

    if isinstance(exc, MediaReuploadRequiredError):
        return PublishFailureInfo(
            retryable=False,
            reason_code="media-reupload-required",
            operator_message=str(exc),
        )

    if isinstance(exc, telethon_errors.FloodWaitError):
        retry_after_seconds = max(1, int(getattr(exc, "seconds", 0) or 0))
        return PublishFailureInfo(
            retryable=retry_after_seconds <= max_flood_wait_seconds,
            reason_code="telethon-flood-wait",
            operator_message=f"Flood wait {retry_after_seconds}s",
            retry_after_seconds=retry_after_seconds,
        )

    if isinstance(exc, telethon_errors.ServerError):
        return PublishFailureInfo(
            retryable=True,
            reason_code="telethon-server-error",
            operator_message=str(exc),
        )

    if isinstance(exc, telethon_common_errors.InvalidBufferError):
        return PublishFailureInfo(
            retryable=True,
            reason_code="telethon-invalid-buffer",
            operator_message=str(exc),
        )

    if isinstance(exc, (ConnectionError, asyncio.TimeoutError, TimeoutError, OSError)):
        return PublishFailureInfo(
            retryable=True,
            reason_code="transport-connection-error",
            operator_message=str(exc),
        )

    if isinstance(exc, telethon_errors.RPCError):
        normalized = str(exc).lower()
        if "media_file_invalid" in normalized:
            return PublishFailureInfo(
                retryable=False,
                reason_code="telethon-media-file-invalid",
                operator_message=format_media_reupload_required_message(
                    "Telegram отклонил подготовленный медиафайл. Восстановите исходный файл или загрузите сторис заново."
                ),
            )
        if "no workers running" in normalized or "timeout" in normalized or "temporar" in normalized:
            return PublishFailureInfo(
                retryable=True,
                reason_code="telethon-retryable-rpc-error",
                operator_message=str(exc),
            )
        return PublishFailureInfo(
            retryable=False,
            reason_code="telethon-terminal-rpc-error",
            operator_message=str(exc),
        )

    return PublishFailureInfo(
        retryable=False,
        reason_code="terminal-runtime-error",
        operator_message=str(exc),
    )
