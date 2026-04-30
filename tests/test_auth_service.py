from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telethon import errors

from app.auth.service import AuthPrompts, authorize_interactively
from app.telegram.bootstrap import TelegramNetworkUnavailableError


class FakeClient:
    def __init__(self) -> None:
        self.actions: list[tuple[str, str | None]] = []
        self.session = object()

    async def connect(self) -> None:
        self.actions.append(("connect", None))

    async def get_me(self):
        self.actions.append(("get_me", None))
        return None

    async def send_code_request(self, phone: str) -> None:
        self.actions.append(("send_code_request", phone))

    async def sign_in(self, phone: str | None = None, code: str | None = None, password: str | None = None):
        if password is not None:
            self.actions.append(("sign_in_password", password))
            if password == "correct-password":
                return SimpleNamespace(id=123, username="operator", phone=phone or "+1000")
            raise errors.PasswordHashInvalidError(request=None)

        self.actions.append(("sign_in_code", code))
        raise errors.SessionPasswordNeededError(request=None)

    async def disconnect(self) -> None:
        self.actions.append(("disconnect", None))


def test_authorize_interactively_retries_2fa_password_without_reasking_code(
    monkeypatch,
    isolated_settings,
) -> None:
    fake_client = FakeClient()
    code_prompts: list[str] = ["22019"]
    password_prompts: list[str] = ["wrong-password", "correct-password"]
    prompt_counts = {"phone": 0, "code": 0, "password": 0}

    def prompt_phone() -> str:
        prompt_counts["phone"] += 1
        return "+1000"

    def prompt_code() -> str:
        prompt_counts["code"] += 1
        return code_prompts.pop(0)

    def prompt_password() -> str:
        prompt_counts["password"] += 1
        return password_prompts.pop(0)

    prompts = AuthPrompts(phone=prompt_phone, code=prompt_code, password=prompt_password)

    monkeypatch.setattr("app.auth.service.create_telegram_client", lambda _settings: fake_client)
    monkeypatch.setattr("app.auth.service.save_runtime_session_string_from_client", lambda *_args, **_kwargs: asyncio.sleep(0, result="runtime-session-string"))

    result = asyncio.run(authorize_interactively(isolated_settings, prompts))

    assert result.authorized_user_id == 123
    assert prompt_counts == {"phone": 1, "code": 1, "password": 2}
    assert fake_client.actions.count(("sign_in_code", "22019")) == 1
    assert ("sign_in_password", "wrong-password") in fake_client.actions
    assert ("sign_in_password", "correct-password") in fake_client.actions


def test_authorize_interactively_classifies_initial_connect_failure_as_network_unavailable(
    monkeypatch,
    isolated_settings,
) -> None:
    class FailingClient:
        def __init__(self) -> None:
            self.actions: list[str] = []

        async def connect(self) -> None:
            self.actions.append("connect")
            raise ConnectionError("Connection to Telegram failed 3 time(s)")

        async def disconnect(self) -> None:
            self.actions.append("disconnect")

    prompts = AuthPrompts(phone=lambda: "+1000", code=lambda: "12345", password=lambda: "password")
    fake_client = FailingClient()

    monkeypatch.setattr("app.auth.service.create_telegram_client", lambda _settings: fake_client)

    with pytest.raises(TelegramNetworkUnavailableError):
        asyncio.run(authorize_interactively(isolated_settings, prompts))

    assert fake_client.actions == ["connect", "disconnect"]


def test_authorize_interactively_force_reauth_recreates_existing_session_artifacts(
    monkeypatch,
    isolated_settings,
) -> None:
    isolated_settings.session_file.write_text("old-session", encoding="utf-8")
    isolated_settings.runtime_session_string_file.write_text("old-runtime", encoding="utf-8")

    class FreshAuthClient:
        def __init__(self, settings) -> None:
            self.settings = settings
            self.actions: list[tuple[str, str | None]] = []

        async def connect(self) -> None:
            self.actions.append(("connect", None))

        async def is_user_authorized(self) -> bool:
            self.actions.append(("is_user_authorized", None))
            return False

        async def send_code_request(self, phone: str) -> None:
            self.actions.append(("send_code_request", phone))

        async def sign_in(self, phone: str | None = None, code: str | None = None, password: str | None = None):
            self.actions.append(("sign_in_code", code))
            self.settings.session_file.write_text("new-session", encoding="utf-8")
            return SimpleNamespace(id=321, username="operator", phone=phone or "+1000")

        async def disconnect(self) -> None:
            self.actions.append(("disconnect", None))

    prompts = AuthPrompts(phone=lambda: "+1000", code=lambda: "12345", password=lambda: "password")
    fake_client = FreshAuthClient(isolated_settings)

    async def fake_save_runtime_session_string_from_client(settings, _client, *, user_id=None):
        settings.runtime_session_string_file.write_text("new-runtime", encoding="utf-8")
        return "new-runtime"

    monkeypatch.setattr("app.auth.service.create_telegram_client", lambda _settings: fake_client)
    monkeypatch.setattr("app.auth.service.save_runtime_session_string_from_client", fake_save_runtime_session_string_from_client)

    result = asyncio.run(authorize_interactively(isolated_settings, prompts, force_reauth=True))

    assert result.authorized_user_id == 321
    assert result.reused_existing_session is False
    assert isolated_settings.session_file.read_text(encoding="utf-8") == "new-session"
    assert isolated_settings.runtime_session_string_file.read_text(encoding="utf-8") == "new-runtime"
    assert not isolated_settings.session_file.with_name(f"{isolated_settings.session_file.name}.preauth.bak").exists()
    assert ("send_code_request", "+1000") in fake_client.actions
    assert ("sign_in_code", "12345") in fake_client.actions


def test_authorize_interactively_force_reauth_restores_old_artifacts_after_network_failure(
    monkeypatch,
    isolated_settings,
) -> None:
    isolated_settings.session_file.write_text("old-session", encoding="utf-8")
    isolated_settings.runtime_session_string_file.write_text("old-runtime", encoding="utf-8")

    class FailingClient:
        async def connect(self) -> None:
            raise ConnectionError("Connection to Telegram failed 3 time(s)")

        async def disconnect(self) -> None:
            return None

    prompts = AuthPrompts(phone=lambda: "+1000", code=lambda: "12345", password=lambda: "password")
    monkeypatch.setattr("app.auth.service.create_telegram_client", lambda _settings: FailingClient())

    with pytest.raises(TelegramNetworkUnavailableError):
        asyncio.run(authorize_interactively(isolated_settings, prompts, force_reauth=True))

    assert isolated_settings.session_file.read_text(encoding="utf-8") == "old-session"
    assert isolated_settings.runtime_session_string_file.read_text(encoding="utf-8") == "old-runtime"
    assert not isolated_settings.session_file.with_name(f"{isolated_settings.session_file.name}.preauth.bak").exists()
