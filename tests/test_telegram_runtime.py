from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.telegram.runtime import TelegramRuntime, TelegramRuntimeRole
from app.telegram.health import TelegramConnectivityChannel, TelegramConnectivityMonitor


@pytest.mark.asyncio
async def test_telegram_runtime_client_context_reuses_started_client(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    calls = {"connect_runtime_client": 0}

    class FakeClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def get_me(self):
            return SimpleNamespace(id=100002)

        async def disconnect(self) -> None:
            self.connected = False

        def is_connected(self) -> bool:
            return self.connected

    fake_client = FakeClient()
    fake_client.connected = True

    async def fake_connect_runtime_client(_settings, *, allow_session_refresh: bool = True):
        calls["connect_runtime_client"] += 1
        return fake_client

    monkeypatch.setattr("app.telegram.runtime.connect_runtime_client", fake_connect_runtime_client)

    runtime = TelegramRuntime(isolated_settings)
    started = await runtime.start()

    async with runtime.client_context() as client:
        assert client is started

    assert calls["connect_runtime_client"] == 1
    await runtime.stop()
    assert fake_client.is_connected() is False


@pytest.mark.asyncio
async def test_telegram_runtime_client_context_opens_dedicated_client_when_not_started(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    fake_client = SimpleNamespace(is_connected=lambda: True, disconnect=lambda: None)
    calls = {"connect_runtime_client": 0}

    async def fake_connect_runtime_client(_settings, *, allow_session_refresh: bool = True):
        calls["connect_runtime_client"] += 1
        return fake_client

    monkeypatch.setattr("app.telegram.runtime.connect_runtime_client", fake_connect_runtime_client)

    runtime = TelegramRuntime(isolated_settings)

    async with runtime.client_context() as client:
        assert client is fake_client

    async with runtime.client_context() as client:
        assert client is fake_client

    assert calls["connect_runtime_client"] == 1


@pytest.mark.asyncio
async def test_telegram_runtime_uses_separate_clients_per_role(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    clients = {
        TelegramRuntimeRole.PUBLISHER: SimpleNamespace(is_connected=lambda: True, disconnect=lambda: None),
        TelegramRuntimeRole.MEDIA_FALLBACK: SimpleNamespace(is_connected=lambda: True, disconnect=lambda: None),
    }
    calls = {"connect_runtime_client": 0}

    async def fake_connect_runtime_client(_settings, *, allow_session_refresh: bool = True):
        role = TelegramRuntimeRole.PUBLISHER if calls["connect_runtime_client"] == 0 else TelegramRuntimeRole.MEDIA_FALLBACK
        calls["connect_runtime_client"] += 1
        return clients[role]

    monkeypatch.setattr("app.telegram.runtime.connect_runtime_client", fake_connect_runtime_client)

    runtime = TelegramRuntime(isolated_settings)

    async with runtime.client_context(TelegramRuntimeRole.PUBLISHER) as publisher_client:
        async with runtime.client_context(TelegramRuntimeRole.MEDIA_FALLBACK) as media_client:
            assert publisher_client is clients[TelegramRuntimeRole.PUBLISHER]
            assert media_client is clients[TelegramRuntimeRole.MEDIA_FALLBACK]

    assert calls["connect_runtime_client"] == 2


@pytest.mark.asyncio
async def test_telegram_runtime_rebuilds_after_failed_health_probe(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    class FailingClient:
        def __init__(self) -> None:
            self.connected = True
            self.disconnect_calls = 0

        async def get_me(self):
            raise ConnectionError("Cannot send requests while disconnected")

        async def disconnect(self) -> None:
            self.connected = False
            self.disconnect_calls += 1

        def is_connected(self) -> bool:
            return self.connected

    class HealthyClient:
        def __init__(self) -> None:
            self.connected = True

        async def get_me(self):
            return SimpleNamespace(id=100002)

        async def disconnect(self) -> None:
            self.connected = False

        def is_connected(self) -> bool:
            return self.connected

    monitor = TelegramConnectivityMonitor(summary_interval_seconds=60)
    monitor.report_failure(TelegramConnectivityChannel.MTPROTO, "down")
    monkeypatch.setattr("app.telegram.runtime.get_connectivity_monitor", lambda: monitor)

    replacement = HealthyClient()
    calls = {"connect_runtime_client": 0}

    async def fake_connect_runtime_client(_settings, *, allow_session_refresh: bool = True):
        calls["connect_runtime_client"] += 1
        return replacement

    monkeypatch.setattr("app.telegram.runtime.connect_runtime_client", fake_connect_runtime_client)

    runtime = TelegramRuntime(isolated_settings)
    stale = FailingClient()
    runtime._clients[TelegramRuntimeRole.PUBLISHER] = stale

    ready = await runtime.ensure_role_ready(TelegramRuntimeRole.PUBLISHER)

    assert ready is True
    assert stale.disconnect_calls == 1
    assert calls["connect_runtime_client"] == 1
    assert runtime._clients[TelegramRuntimeRole.PUBLISHER] is replacement
    assert monitor.is_degraded(TelegramConnectivityChannel.MTPROTO) is False


@pytest.mark.asyncio
async def test_telegram_runtime_reports_unavailable_when_reconnect_fails(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    monitor = TelegramConnectivityMonitor(summary_interval_seconds=60)
    monkeypatch.setattr("app.telegram.runtime.get_connectivity_monitor", lambda: monitor)

    async def fake_connect_runtime_client(_settings, *, allow_session_refresh: bool = True):
        raise ConnectionError("telegram down")

    monkeypatch.setattr("app.telegram.runtime.connect_runtime_client", fake_connect_runtime_client)

    runtime = TelegramRuntime(isolated_settings)

    ready = await runtime.ensure_role_ready(TelegramRuntimeRole.PUBLISHER)

    assert ready is False
    assert monitor.is_degraded(TelegramConnectivityChannel.MTPROTO) is True


@pytest.mark.asyncio
async def test_telegram_runtime_times_out_and_disposes_stalled_health_probe(
    isolated_settings,
) -> None:
    runtime_settings = replace(
        isolated_settings.runtime,
        mtproto_probe_interval_seconds=0,
        mtproto_probe_timeout_seconds=0.01,
    )
    settings = replace(isolated_settings, runtime=runtime_settings)

    class StalledProbeClient:
        def __init__(self) -> None:
            self.connected = True
            self.disconnect_calls = 0

        async def get_me(self):
            await asyncio.Event().wait()

        async def disconnect(self) -> None:
            self.connected = False
            self.disconnect_calls += 1

        def is_connected(self) -> bool:
            return self.connected

    client = StalledProbeClient()
    runtime = TelegramRuntime(settings)
    runtime._clients[TelegramRuntimeRole.PUBLISHER] = client

    ready = await runtime.ensure_role_ready(
        TelegramRuntimeRole.PUBLISHER,
        allow_reconnect=False,
    )

    assert ready is False
    assert client.disconnect_calls == 1
    assert TelegramRuntimeRole.PUBLISHER not in runtime._clients
