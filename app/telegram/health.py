from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import logging
from threading import RLock

logger = logging.getLogger(__name__)


class TelegramConnectivityChannel(StrEnum):
    BOT_API = "bot-api"
    MTPROTO = "mtproto"


class TelegramConnectivityState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"


@dataclass(slots=True)
class ChannelHealth:
    state: TelegramConnectivityState = TelegramConnectivityState.HEALTHY
    consecutive_failures: int = 0
    suppressed_failures: int = 0
    first_failure_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_success_at: datetime | None = None
    last_summary_at: datetime | None = None
    last_error: str | None = None
    operator_hint_emitted: bool = False


class TelegramConnectivityMonitor:
    def __init__(self, *, summary_interval_seconds: int = 60) -> None:
        self._summary_interval_seconds = max(5, summary_interval_seconds)
        self._channels = {
            TelegramConnectivityChannel.BOT_API: ChannelHealth(),
            TelegramConnectivityChannel.MTPROTO: ChannelHealth(),
        }
        self._lock = RLock()

    def _build_bot_api_guidance(self) -> str:
        mtproto_degraded = self._channels[TelegramConnectivityChannel.MTPROTO].state == TelegramConnectivityState.DEGRADED
        if mtproto_degraded:
            return (
                "Bot API Telegram сейчас нестабилен: команды бота и загрузка медиа могут не работать. "
                "Канал личного аккаунта тоже выглядит нестабильным, поэтому публикация по расписанию может задерживаться. "
                "Если это часто происходит в этой сети, пропишите BOT_PROXY_URL или BOT_API_BASE_URL в .env."
            )
        return (
            "Bot API Telegram сейчас нестабилен: команды бота и загрузка медиа могут не работать. "
            "Публикация по расписанию может продолжаться, если MTProto-канал личного аккаунта остаётся доступным. "
            "Если это часто происходит в этой сети, пропишите BOT_PROXY_URL или BOT_API_BASE_URL в .env."
        )

    def _maybe_emit_operator_hint(
        self,
        channel: TelegramConnectivityChannel,
        *,
        health: ChannelHealth,
        current_logger: logging.Logger,
    ) -> None:
        if channel != TelegramConnectivityChannel.BOT_API:
            return
        if health.operator_hint_emitted:
            return
        if health.consecutive_failures < 2:
            return

        current_logger.warning(self._build_bot_api_guidance())
        health.operator_hint_emitted = True

    def configure(self, *, summary_interval_seconds: int) -> None:
        with self._lock:
            self._summary_interval_seconds = max(5, summary_interval_seconds)

    def is_degraded(self, channel: TelegramConnectivityChannel) -> bool:
        with self._lock:
            return self._channels[channel].state == TelegramConnectivityState.DEGRADED

    def report_failure(
        self,
        channel: TelegramConnectivityChannel,
        detail: str,
        *,
        current_logger: logging.Logger | None = None,
    ) -> None:
        active_logger = current_logger or logger
        timestamp = datetime.now(UTC)

        with self._lock:
            health = self._channels[channel]
            normalized_detail = detail.strip()
            health.consecutive_failures += 1
            health.last_failure_at = timestamp
            health.last_error = normalized_detail

            if health.state != TelegramConnectivityState.DEGRADED:
                health.state = TelegramConnectivityState.DEGRADED
                health.first_failure_at = timestamp
                health.last_summary_at = timestamp
                health.suppressed_failures = 0
                active_logger.warning(
                    "Telegram connectivity degraded channel=%s consecutive_failures=%s error=%s",
                    channel.value,
                    health.consecutive_failures,
                    normalized_detail,
                )
                self._maybe_emit_operator_hint(channel, health=health, current_logger=active_logger)
                return

            if health.last_summary_at is None:
                health.last_summary_at = timestamp

            self._maybe_emit_operator_hint(channel, health=health, current_logger=active_logger)
            elapsed = (timestamp - health.last_summary_at).total_seconds()
            if elapsed < self._summary_interval_seconds:
                health.suppressed_failures += 1
                return

            active_logger.warning(
                "Telegram connectivity still degraded channel=%s consecutive_failures=%s suppressed_failures=%s last_error=%s",
                channel.value,
                health.consecutive_failures,
                health.suppressed_failures,
                normalized_detail,
            )
            health.last_summary_at = timestamp
            health.suppressed_failures = 0

    def report_success(
        self,
        channel: TelegramConnectivityChannel,
        *,
        detail: str | None = None,
        current_logger: logging.Logger | None = None,
    ) -> None:
        active_logger = current_logger or logger
        timestamp = datetime.now(UTC)

        with self._lock:
            health = self._channels[channel]
            if health.state != TelegramConnectivityState.DEGRADED:
                health.last_success_at = timestamp
                return

            downtime_seconds = 0
            if health.first_failure_at is not None:
                downtime_seconds = int((timestamp - health.first_failure_at).total_seconds())

            active_logger.info(
                "Telegram connectivity recovered channel=%s downtime_seconds=%s consecutive_failures=%s detail=%s",
                channel.value,
                downtime_seconds,
                health.consecutive_failures,
                detail or "ok",
            )
            health.state = TelegramConnectivityState.HEALTHY
            health.consecutive_failures = 0
            health.suppressed_failures = 0
            health.first_failure_at = None
            health.last_failure_at = None
            health.last_success_at = timestamp
            health.last_summary_at = None
            health.last_error = None
            health.operator_hint_emitted = False


_GLOBAL_MONITOR = TelegramConnectivityMonitor()


def configure_connectivity_monitor(*, summary_interval_seconds: int) -> TelegramConnectivityMonitor:
    _GLOBAL_MONITOR.configure(summary_interval_seconds=summary_interval_seconds)
    return _GLOBAL_MONITOR


def get_connectivity_monitor() -> TelegramConnectivityMonitor:
    return _GLOBAL_MONITOR
