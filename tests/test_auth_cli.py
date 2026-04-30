from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.auth import cli as auth_cli
from app.auth.service import AuthResult
from app.telegram.bootstrap import (
    TelegramBootstrapResult,
    TelegramBootstrapStage,
    TelegramNetworkUnavailableError,
    TelegramOperatorAction,
    TelegramSessionState,
)


def test_auth_main_returns_error_for_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def raise_runtime_error() -> int:
        raise RuntimeError("missing credentials")

    monkeypatch.setattr(auth_cli, "run_auth_cli", raise_runtime_error)

    assert auth_cli.main() == 1
    captured = capsys.readouterr()
    assert "Ошибка запуска авторизации: missing credentials" in captured.out


def test_auth_main_reports_telegram_bootstrap_failure_without_windows_asyncio_label(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def raise_bootstrap_error(*_args, **_kwargs) -> int:
        raise TelegramNetworkUnavailableError(
            TelegramBootstrapResult(
                state=TelegramSessionState.NETWORK_UNAVAILABLE,
                operator_action=TelegramOperatorAction.RESTORE_NETWORK,
                stage=TelegramBootstrapStage.INTERACTIVE_AUTH_BOOTSTRAP,
                detail="Connection to Telegram failed before login code could be requested",
            )
        )

    monkeypatch.setattr(auth_cli, "run_async_entrypoint", raise_bootstrap_error)
    monkeypatch.setattr(auth_cli, "prepare_windows_runtime", lambda *_args, **_kwargs: None)

    assert auth_cli.main() == 1
    captured = capsys.readouterr()
    assert "Telegram" in captured.out
    assert "Windows asyncio" not in captured.out


def test_run_auth_cli_requests_forced_reauth(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    captured: dict[str, object] = {}

    async def fake_authorize(settings, prompts, *, force_reauth: bool = False):
        captured["settings"] = settings
        captured["force_reauth"] = force_reauth
        return AuthResult(
            session_file=Path("account.session"),
            authorized_user_id=123,
            username="client",
            phone="+1000",
            reused_existing_session=False,
        )

    monkeypatch.setattr(auth_cli, "load_settings", lambda: isolated_settings)
    monkeypatch.setattr(auth_cli, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_cli, "authorize_interactively", fake_authorize)

    assert asyncio.run(auth_cli.run_auth_cli()) == 0
    assert captured["settings"] is isolated_settings
    assert captured["force_reauth"] is True
