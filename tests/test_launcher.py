from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import launcher
from app.telegram.bootstrap import (
    TelegramBootstrapResult,
    TelegramBootstrapStage,
    TelegramNetworkUnavailableError,
    TelegramOperatorAction,
    TelegramSessionState,
)


def test_launcher_main_reports_telegram_bootstrap_failure_without_windows_asyncio_label(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def raise_bootstrap_error(*_args, **_kwargs) -> int:
        raise TelegramNetworkUnavailableError(
            TelegramBootstrapResult(
                state=TelegramSessionState.NETWORK_UNAVAILABLE,
                operator_action=TelegramOperatorAction.RESTORE_NETWORK,
                stage=TelegramBootstrapStage.FILE_SESSION_VALIDATION,
                detail="Connection to Telegram failed while validating an existing session",
            )
        )

    monkeypatch.setattr(launcher, "run_async_entrypoint", raise_bootstrap_error)
    monkeypatch.setattr(launcher, "prepare_windows_runtime", lambda *_args, **_kwargs: None)

    assert launcher.main() == 1
    captured = capsys.readouterr()
    assert "Telegram" in captured.out
    assert "Windows asyncio" not in captured.out


def test_ensure_authorized_session_uses_degraded_mode_only_with_runtime_artifact(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = SimpleNamespace(
        session_file="account.session",
        runtime_session_string_file="account.runtime-session.txt",
        paths=SimpleNamespace(data_dir="data"),
    )

    async def fake_assess_existing_session(_settings):
        return TelegramBootstrapResult(
            state=TelegramSessionState.NETWORK_UNAVAILABLE,
            operator_action=TelegramOperatorAction.RESTORE_NETWORK,
            stage=TelegramBootstrapStage.FILE_SESSION_VALIDATION,
            detail="Connection to Telegram failed while validating the existing file session",
            reused_runtime_session=False,
        )

    monkeypatch.setattr(launcher, "load_settings", lambda: settings)
    monkeypatch.setattr(launcher, "assess_existing_session", fake_assess_existing_session)

    with pytest.raises(TelegramNetworkUnavailableError):
        asyncio.run(launcher._ensure_authorized_session())

    captured = capsys.readouterr()
    assert "ограниченном режиме" not in captured.out


def test_ensure_authorized_session_allows_degraded_mode_with_runtime_artifact(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = SimpleNamespace(
        session_file="account.session",
        runtime_session_string_file="account.runtime-session.txt",
        paths=SimpleNamespace(data_dir="data"),
    )

    async def fake_assess_existing_session(_settings):
        return TelegramBootstrapResult(
            state=TelegramSessionState.NETWORK_UNAVAILABLE,
            operator_action=TelegramOperatorAction.RESTORE_NETWORK,
            stage=TelegramBootstrapStage.FILE_SESSION_VALIDATION,
            detail="Connection to Telegram failed while validating the existing file session",
            reused_runtime_session=True,
        )

    monkeypatch.setattr(launcher, "load_settings", lambda: settings)
    monkeypatch.setattr(launcher, "assess_existing_session", fake_assess_existing_session)

    asyncio.run(launcher._ensure_authorized_session())

    captured = capsys.readouterr()
    assert "ограниченном режиме" in captured.out
