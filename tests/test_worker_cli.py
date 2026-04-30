from __future__ import annotations

import pytest

from app.telegram.bootstrap import (
    TelegramBootstrapResult,
    TelegramBootstrapStage,
    TelegramNetworkUnavailableError,
    TelegramOperatorAction,
    TelegramSessionState,
)
from app.worker import cli as worker_cli


def test_worker_main_reports_telegram_bootstrap_failure_without_windows_asyncio_label(
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

    monkeypatch.setattr(worker_cli, "run_worker_cli", raise_bootstrap_error)

    assert worker_cli.main() == 1
    captured = capsys.readouterr()
    assert "Telegram" in captured.out
    assert "Windows asyncio" not in captured.out
