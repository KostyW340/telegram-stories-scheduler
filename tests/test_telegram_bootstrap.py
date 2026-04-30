from __future__ import annotations

from pathlib import Path

from app.telegram.bootstrap import (
    TelegramBootstrapResult,
    TelegramBootstrapStage,
    TelegramNetworkUnavailableError,
    TelegramOperatorAction,
    TelegramSessionState,
    format_synced_runtime_warning,
    explain_telegram_bootstrap_failure,
)


def test_explain_telegram_bootstrap_failure_for_network_unavailable() -> None:
    exc = TelegramNetworkUnavailableError(
        TelegramBootstrapResult(
            state=TelegramSessionState.NETWORK_UNAVAILABLE,
            operator_action=TelegramOperatorAction.RESTORE_NETWORK,
            stage=TelegramBootstrapStage.INTERACTIVE_AUTH_BOOTSTRAP,
            detail="Connection to Telegram failed before login code could be requested",
        )
    )

    message = explain_telegram_bootstrap_failure(exc)

    assert message is not None
    assert "Telegram" in message
    assert "не может" in message
    assert "сесси" in message
    assert "Windows asyncio" not in message


def test_format_synced_runtime_warning_mentions_local_folder_move() -> None:
    warning = format_synced_runtime_warning(
        (
            Path(r"C:\Users\user\OneDrive\Projects\Stories\account.session"),
            Path(r"C:\Users\user\OneDrive\Projects\Stories\account.runtime-session.txt"),
        )
    )

    assert warning is not None
    assert "синхронизируемой папки" in warning
    assert "C:\\TGStories" in warning
