from __future__ import annotations

import logging
import logging.config
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.telegram.health import (
    TelegramConnectivityChannel,
    configure_connectivity_monitor,
    get_connectivity_monitor,
)

logger = logging.getLogger(__name__)


class TelegramNetworkNoiseFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._monitor = get_connectivity_monitor()
        self._health_logger = logging.getLogger("app.telegram.health")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()

        if record.name == "aiogram.dispatcher":
            if "Failed to fetch updates - TelegramNetworkError" in message:
                self._monitor.report_failure(
                    TelegramConnectivityChannel.BOT_API,
                    message,
                    current_logger=self._health_logger,
                )
                return False
            if message.startswith("Sleep for ") and "try again" in message:
                return False
            if message.startswith("Connection established"):
                self._monitor.report_success(
                    TelegramConnectivityChannel.BOT_API,
                    detail=message,
                    current_logger=self._health_logger,
                )
                return False
            return True

        if record.name.startswith("telethon.network.connection.connection"):
            if "Server closed the connection" in message or "during disconnect" in message:
                self._monitor.report_failure(
                    TelegramConnectivityChannel.MTPROTO,
                    message,
                    current_logger=self._health_logger,
                )
                return False
            return True

        if record.name.startswith("telethon.network.mtprotosender"):
            noisy_prefixes = (
                "Connecting to ",
                "Connection to ",
                "Connection closed while receiving data",
                "Closing current connection to begin reconnect",
                "Disconnecting from ",
                "Disconnection from ",
            )
            if message.startswith(noisy_prefixes):
                return False

        return True


def configure_logging(
    level: str,
    logs_dir: Path,
    *,
    connectivity_summary_interval_seconds: int = 60,
) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "app.log"
    configure_connectivity_monitor(summary_interval_seconds=connectivity_summary_interval_seconds)

    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "telegram_noise": {
                "()": TelegramNetworkNoiseFilter,
            }
        },
        "formatters": {
            "standard": {
                "format": "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": level,
                "formatter": "standard",
                "filters": ["telegram_noise"],
            },
            "file": {
                "()": RotatingFileHandler,
                "level": level,
                "formatter": "standard",
                "filename": str(log_file),
                "maxBytes": 2 * 1024 * 1024,
                "backupCount": 3,
                "encoding": "utf-8",
                "filters": ["telegram_noise"],
            },
        },
        "root": {
            "level": level,
            "handlers": ["console", "file"],
        },
    }

    logging.config.dictConfig(config)
    logger.info("Logging configured at level %s", level)
    logger.debug("Log file path: %s", log_file)
