from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib
import logging

import pytest

from app.telegram.health import TelegramConnectivityChannel, TelegramConnectivityMonitor

app_logging = importlib.import_module("app.config.logging")


def _record(name: str, level: int, message: str) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, (), None)


def test_connectivity_monitor_coalesces_failures_and_logs_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    monitor = TelegramConnectivityMonitor(summary_interval_seconds=60)
    health_logger = logging.getLogger("test.health")

    with caplog.at_level(logging.INFO, logger="test.health"):
        monitor.report_failure(
            TelegramConnectivityChannel.MTPROTO,
            "ConnectionResetError: boom",
            current_logger=health_logger,
        )
        monitor.report_failure(
            TelegramConnectivityChannel.MTPROTO,
            "ConnectionResetError: boom-again",
            current_logger=health_logger,
        )
        monitor._channels[TelegramConnectivityChannel.MTPROTO].last_summary_at = datetime.now(UTC) - timedelta(
            seconds=120
        )
        monitor.report_failure(
            TelegramConnectivityChannel.MTPROTO,
            "ConnectionResetError: still-bad",
            current_logger=health_logger,
        )
        monitor.report_success(
            TelegramConnectivityChannel.MTPROTO,
            detail="probe-ok:publisher",
            current_logger=health_logger,
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("Telegram connectivity degraded channel=mtproto" in message for message in messages)
    assert any("Telegram connectivity still degraded channel=mtproto" in message for message in messages)
    assert any("Telegram connectivity recovered channel=mtproto" in message for message in messages)


def test_network_noise_filter_tracks_aiogram_failure_and_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = TelegramConnectivityMonitor(summary_interval_seconds=60)
    monkeypatch.setattr(app_logging, "get_connectivity_monitor", lambda: monitor)

    log_filter = app_logging.TelegramNetworkNoiseFilter()

    assert log_filter.filter(
        _record(
            "aiogram.dispatcher",
            logging.ERROR,
            "Failed to fetch updates - TelegramNetworkError: Cannot connect to host api.telegram.org:443 ssl:default [None]",
        )
    ) is False
    assert monitor.is_degraded(TelegramConnectivityChannel.BOT_API) is True

    assert log_filter.filter(
        _record(
            "aiogram.dispatcher",
            logging.WARNING,
            "Sleep for 60.000000 seconds and try again... (tryings = 1, bot id = 100001)",
        )
    ) is False

    assert log_filter.filter(
        _record(
            "aiogram.dispatcher",
            logging.INFO,
            "Connection established (tryings = 7, bot id = 100001)",
        )
    ) is False
    assert monitor.is_degraded(TelegramConnectivityChannel.BOT_API) is False


def test_connectivity_monitor_logs_bot_api_guidance_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    monitor = TelegramConnectivityMonitor(summary_interval_seconds=60)
    health_logger = logging.getLogger("test.health")

    with caplog.at_level(logging.WARNING, logger="test.health"):
        monitor.report_failure(
            TelegramConnectivityChannel.BOT_API,
            "Failed to fetch updates - TelegramNetworkError: Request timeout error",
            current_logger=health_logger,
        )
        monitor.report_failure(
            TelegramConnectivityChannel.BOT_API,
            "Failed to fetch updates - TelegramNetworkError: Request timeout error",
            current_logger=health_logger,
        )
        monitor.report_failure(
            TelegramConnectivityChannel.BOT_API,
            "Failed to fetch updates - TelegramNetworkError: Request timeout error",
            current_logger=health_logger,
        )

    messages = [record.getMessage() for record in caplog.records]
    guidance_messages = [message for message in messages if "BOT_PROXY_URL" in message]
    assert len(guidance_messages) == 1
    assert "публикация по расписанию может продолжаться" in guidance_messages[0].lower()
