from __future__ import annotations

import pytest

from app.bot import cli as bot_cli
from app.telegram.bootstrap import (
    TelegramBootstrapResult,
    TelegramBootstrapStage,
    TelegramNetworkUnavailableError,
    TelegramOperatorAction,
    TelegramSessionState,
)


def test_bot_main_returns_error_for_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def raise_runtime_error() -> int:
        raise RuntimeError("Режим 'bot' уже запущен из этой папки. Закройте старое окно и попробуйте снова.")

    monkeypatch.setattr(bot_cli, "run_bot_cli", raise_runtime_error)

    assert bot_cli.main() == 1
    captured = capsys.readouterr()
    assert "Режим 'bot' уже запущен из этой папки" in captured.out


def test_bot_main_reports_telegram_bootstrap_failure_without_windows_asyncio_label(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def raise_bootstrap_error() -> int:
        raise TelegramNetworkUnavailableError(
            TelegramBootstrapResult(
                state=TelegramSessionState.NETWORK_UNAVAILABLE,
                operator_action=TelegramOperatorAction.RESTORE_NETWORK,
                stage=TelegramBootstrapStage.RUNTIME_SESSION_CONNECT,
                detail="Connection to Telegram failed while connecting the runtime StringSession",
            )
        )

    monkeypatch.setattr(bot_cli, "run_bot_cli", raise_bootstrap_error)

    assert bot_cli.main() == 1
    captured = capsys.readouterr()
    assert "Telegram" in captured.out
    assert "Windows asyncio" not in captured.out
