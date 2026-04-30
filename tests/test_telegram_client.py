from __future__ import annotations

import types
from types import SimpleNamespace

import pytest
from telethon.crypto import aes as telethon_aes
from telethon.crypto import libssl as telethon_libssl

from app.telegram import client as telegram_client
from app.telegram.bootstrap import (
    TelegramBootstrapStage,
    TelegramNetworkUnavailableError,
    TelegramSessionState,
    TelegramSessionUnauthorizedError,
)


def test_validate_telethon_libssl_backend_detects_missing_symbols() -> None:
    fake_handle = types.SimpleNamespace(AES_set_encrypt_key=object(), AES_ige_encrypt=object())
    fake_libssl = types.SimpleNamespace(_libssl=fake_handle)

    is_valid, missing = telegram_client._validate_telethon_libssl_backend(fake_libssl)

    assert is_valid is False
    assert missing == ("AES_set_decrypt_key",)


def test_prepare_telethon_crypto_backend_disables_invalid_libssl(monkeypatch) -> None:
    fake_handle = types.SimpleNamespace()

    monkeypatch.setattr(
        telegram_client,
        "_validate_telethon_libssl_backend",
        lambda _module: (False, ("AES_set_decrypt_key",)),
    )
    monkeypatch.setattr(telethon_aes, "cryptg", None, raising=False)
    monkeypatch.setattr(telethon_libssl, "_libssl", fake_handle, raising=False)
    monkeypatch.setattr(telethon_libssl, "encrypt_ige", object(), raising=False)
    monkeypatch.setattr(telethon_libssl, "decrypt_ige", object(), raising=False)

    backend = telegram_client.prepare_telethon_crypto_backend()

    assert backend == "python-fallback"
    assert telethon_libssl._libssl is None
    assert telethon_libssl.encrypt_ige is None
    assert telethon_libssl.decrypt_ige is None


def test_prepare_telethon_crypto_backend_prefers_cryptg(monkeypatch) -> None:
    monkeypatch.setattr(telethon_aes, "cryptg", object(), raising=False)
    monkeypatch.setattr(telethon_libssl, "_libssl", None, raising=False)
    monkeypatch.setattr(telethon_libssl, "encrypt_ige", None, raising=False)
    monkeypatch.setattr(telethon_libssl, "decrypt_ige", None, raising=False)

    backend = telegram_client.prepare_telethon_crypto_backend()

    assert backend == "cryptg"


def test_prepare_telethon_crypto_backend_imports_cryptg_when_available(monkeypatch) -> None:
    sentinel = object()

    monkeypatch.setattr(telethon_aes, "cryptg", None, raising=False)
    monkeypatch.setattr(telethon_libssl, "_libssl", None, raising=False)
    monkeypatch.setattr(telethon_libssl, "encrypt_ige", None, raising=False)
    monkeypatch.setattr(telethon_libssl, "decrypt_ige", None, raising=False)
    monkeypatch.setattr(telegram_client.importlib, "import_module", lambda name: sentinel if name == "cryptg" else None)

    backend = telegram_client.prepare_telethon_crypto_backend()

    assert backend == "cryptg"
    assert telethon_aes.cryptg is sentinel


@pytest.mark.asyncio
async def test_export_runtime_session_string_persists_string_from_file_backed_session(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    class FakeAuthorizedClient:
        def __init__(self) -> None:
            self.session = object()
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def is_user_authorized(self) -> bool:
            return True

        async def get_me(self):
            return SimpleNamespace(id=100002)

        async def disconnect(self) -> None:
            self.connected = False

    monkeypatch.setattr(telegram_client, "create_authorization_client", lambda _settings: FakeAuthorizedClient())
    from telethon.sessions import StringSession

    monkeypatch.setattr(StringSession, "save", staticmethod(lambda _session: "runtime-session-string"), raising=False)

    result = await telegram_client.export_runtime_session_string(isolated_settings, force=True)

    assert result == "runtime-session-string"
    assert isolated_settings.runtime_session_string_file.read_text(encoding="utf-8") == "runtime-session-string"


@pytest.mark.asyncio
async def test_connect_runtime_client_uses_string_session_source(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    captured: dict[str, object] = {}

    class FakeStringSession:
        def __init__(self, value: str) -> None:
            self.value = value

    class FakeRuntimeClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def is_user_authorized(self) -> bool:
            return True

        async def get_me(self):
            return SimpleNamespace(id=100002)

        async def disconnect(self) -> None:
            self.connected = False

        def is_connected(self) -> bool:
            return self.connected

    async def fake_ensure_runtime_session_string(_settings):
        return "runtime-session-string"

    def fake_build_telegram_client(_settings, session_source):
        captured["session_source"] = session_source
        return FakeRuntimeClient()

    monkeypatch.setattr("telethon.sessions.StringSession", FakeStringSession)
    monkeypatch.setattr(telegram_client, "ensure_runtime_session_string", fake_ensure_runtime_session_string)
    monkeypatch.setattr(telegram_client, "_build_telegram_client", fake_build_telegram_client)

    client = await telegram_client.connect_runtime_client(isolated_settings)

    assert isinstance(captured["session_source"], FakeStringSession)
    assert captured["session_source"].value == "runtime-session-string"
    assert client.is_connected() is True


@pytest.mark.asyncio
async def test_export_runtime_session_string_classifies_connect_failure_as_network_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    class FailingAuthorizedClient:
        async def connect(self) -> None:
            raise ConnectionError("Connection to Telegram failed 3 time(s)")

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr(telegram_client, "create_authorization_client", lambda _settings: FailingAuthorizedClient())

    with pytest.raises(TelegramNetworkUnavailableError) as exc_info:
        await telegram_client.export_runtime_session_string(isolated_settings, force=True)

    assert exc_info.value.result.state is TelegramSessionState.NETWORK_UNAVAILABLE
    assert exc_info.value.result.stage is TelegramBootstrapStage.RUNTIME_SESSION_REFRESH


@pytest.mark.asyncio
async def test_connect_runtime_client_classifies_unauthorized_runtime_session(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    class FakeStringSession:
        def __init__(self, value: str) -> None:
            self.value = value

    class FakeRuntimeClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def is_user_authorized(self) -> bool:
            return False

        async def disconnect(self) -> None:
            self.connected = False

        def is_connected(self) -> bool:
            return self.connected

    async def fake_ensure_runtime_session_string(_settings):
        return "runtime-session-string"

    monkeypatch.setattr("telethon.sessions.StringSession", FakeStringSession)
    monkeypatch.setattr(telegram_client, "ensure_runtime_session_string", fake_ensure_runtime_session_string)
    monkeypatch.setattr(telegram_client, "_build_telegram_client", lambda _settings, _session_source: FakeRuntimeClient())

    with pytest.raises(TelegramSessionUnauthorizedError) as exc_info:
        await telegram_client.connect_runtime_client(isolated_settings, allow_session_refresh=False)

    assert exc_info.value.result.state is TelegramSessionState.UNAUTHORIZED
    assert exc_info.value.result.stage is TelegramBootstrapStage.RUNTIME_SESSION_CONNECT


@pytest.mark.asyncio
async def test_assess_existing_session_uses_runtime_artifact_when_file_missing(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    isolated_settings.runtime_session_string_file.parent.mkdir(parents=True, exist_ok=True)
    isolated_settings.runtime_session_string_file.write_text("runtime-session-string", encoding="utf-8")

    async def fake_validate_runtime_session(_settings):
        from app.telegram.bootstrap import TelegramBootstrapResult, TelegramOperatorAction

        return TelegramBootstrapResult(
            state=TelegramSessionState.NETWORK_UNAVAILABLE,
            operator_action=TelegramOperatorAction.RESTORE_NETWORK,
            stage=TelegramBootstrapStage.RUNTIME_SESSION_CONNECT,
            detail="Runtime-only artifact present while Telegram is unreachable",
            reused_runtime_session=True,
        )

    monkeypatch.setattr(telegram_client, "validate_runtime_session_artifact", fake_validate_runtime_session)

    result = await telegram_client.assess_existing_session(isolated_settings)

    assert result.state is TelegramSessionState.NETWORK_UNAVAILABLE
    assert result.reused_runtime_session is True


@pytest.mark.asyncio
async def test_validate_runtime_session_artifact_treats_empty_file_as_invalid(
    isolated_settings,
) -> None:
    isolated_settings.runtime_session_string_file.parent.mkdir(parents=True, exist_ok=True)
    isolated_settings.runtime_session_string_file.write_text("   ", encoding="utf-8")

    result = await telegram_client.validate_runtime_session_artifact(isolated_settings)

    assert result.state is TelegramSessionState.INVALID
    assert result.stage is TelegramBootstrapStage.RUNTIME_SESSION_CONNECT
    assert "empty" in result.detail.lower()


@pytest.mark.asyncio
async def test_assess_existing_session_marks_network_unavailable_result_as_runtime_reusable(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    isolated_settings.session_file.touch()
    isolated_settings.runtime_session_string_file.parent.mkdir(parents=True, exist_ok=True)
    isolated_settings.runtime_session_string_file.write_text("runtime-session-string", encoding="utf-8")

    async def fake_validate_file_session(_settings):
        from app.telegram.bootstrap import TelegramBootstrapResult, TelegramOperatorAction

        return TelegramBootstrapResult(
            state=TelegramSessionState.NETWORK_UNAVAILABLE,
            operator_action=TelegramOperatorAction.RESTORE_NETWORK,
            stage=TelegramBootstrapStage.FILE_SESSION_VALIDATION,
            detail="Connection to Telegram failed while validating the file-backed session",
        )

    monkeypatch.setattr(telegram_client, "validate_file_session", fake_validate_file_session)

    result = await telegram_client.assess_existing_session(isolated_settings)

    assert result.state is TelegramSessionState.NETWORK_UNAVAILABLE
    assert result.reused_runtime_session is True
