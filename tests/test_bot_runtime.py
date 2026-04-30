from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import replace

import pytest
from aiogram.client.session.aiohttp import AiohttpSession

from app.bot.runtime import build_polling_backoff_config, create_bot_client, run_supervised_polling


def test_create_bot_client_uses_custom_bot_api_base_url(isolated_settings, monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:token")
    monkeypatch.setenv("BOT_API_BASE_URL", "http://127.0.0.1:8082")
    from app.config.settings import load_settings

    load_settings.cache_clear()
    settings = load_settings(isolated_settings.paths.project_root)

    bot = create_bot_client(settings)

    assert isinstance(bot.session, AiohttpSession)
    assert bot.session.api.base == "http://127.0.0.1:8082/bot{token}/{method}"


def test_create_bot_client_uses_proxy_url(isolated_settings, monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:token")
    monkeypatch.setenv("BOT_PROXY_URL", "http://127.0.0.1:8080")
    from app.config.settings import load_settings

    captured: dict[str, object] = {}

    def fake_setup_proxy_connector(self, proxy) -> None:
        captured["proxy"] = proxy

    monkeypatch.setattr(AiohttpSession, "_setup_proxy_connector", fake_setup_proxy_connector)
    load_settings.cache_clear()
    settings = load_settings(isolated_settings.paths.project_root)

    bot = create_bot_client(settings)

    assert isinstance(bot.session, AiohttpSession)
    assert captured["proxy"] == "http://127.0.0.1:8080"


def test_build_polling_backoff_config_reads_runtime_settings(isolated_settings, monkeypatch) -> None:
    monkeypatch.setenv("BOT_POLLING_BACKOFF_MIN_DELAY_SECONDS", "2")
    monkeypatch.setenv("BOT_POLLING_BACKOFF_MAX_DELAY_SECONDS", "90")
    monkeypatch.setenv("BOT_POLLING_BACKOFF_FACTOR", "2.5")
    monkeypatch.setenv("BOT_POLLING_BACKOFF_JITTER", "0.4")
    from app.config.settings import load_settings

    load_settings.cache_clear()
    settings = load_settings(isolated_settings.paths.project_root)

    config = build_polling_backoff_config(settings)

    assert config.min_delay == 2.0
    assert config.max_delay == 90.0
    assert config.factor == 2.5
    assert config.jitter == 0.4


@pytest.mark.asyncio
async def test_run_supervised_polling_restarts_after_clean_exit(
    isolated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async def fake_run_once() -> None:
        calls.append("run")
        if len(calls) == 1:
            return
        raise asyncio.CancelledError()

    monkeypatch.setattr("app.bot.runtime.asyncio.sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await run_supervised_polling(fake_run_once, settings=isolated_settings, current_logger=logging.getLogger("test"))

    assert calls == ["run", "run"]
    assert delays == [isolated_settings.runtime.bot_polling_backoff_min_delay_seconds]


@pytest.mark.asyncio
async def test_run_supervised_polling_retries_on_retryable_transport_error(
    isolated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async def fake_run_once() -> None:
        calls.append("run")
        if len(calls) == 1:
            raise ConnectionError("Cannot connect to host api.telegram.org:443")
        raise asyncio.CancelledError()

    monkeypatch.setattr("app.bot.runtime.asyncio.sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await run_supervised_polling(fake_run_once, settings=isolated_settings, current_logger=logging.getLogger("test"))

    assert calls == ["run", "run"]
    assert delays == [isolated_settings.runtime.bot_polling_backoff_min_delay_seconds]


@pytest.mark.asyncio
async def test_run_supervised_polling_stops_on_fatal_runtime_error(
    isolated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async def fake_run_once() -> None:
        raise RuntimeError("invalid token")

    monkeypatch.setattr("app.bot.runtime.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="invalid token"):
        await run_supervised_polling(fake_run_once, settings=isolated_settings, current_logger=logging.getLogger("test"))

    assert delays == []


@pytest.mark.asyncio
async def test_run_supervised_polling_runtime_error_is_nonretryable_not_fatal(
    isolated_settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async def fake_run_once() -> None:
        raise RuntimeError("router lifecycle mismatch")

    monkeypatch.setattr("app.bot.runtime.asyncio.sleep", fake_sleep)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="router lifecycle mismatch"):
            await run_supervised_polling(fake_run_once, settings=isolated_settings, current_logger=logging.getLogger("test"))

    assert delays == []
    assert "non-retryable error" in caplog.text
    assert "fatal error" not in caplog.text


@pytest.mark.asyncio
async def test_run_supervised_polling_resets_backoff_after_healthy_runtime(
    isolated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    monotonic_values: Iterator[float] = iter((100.0, 101.0, 200.0, 231.0, 300.0))
    settings = replace(
        isolated_settings,
        runtime=replace(isolated_settings.runtime, bot_polling_backoff_reset_after_seconds=30),
    )

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async def fake_run_once() -> None:
        if not delays:
            raise ConnectionError("first outage")
        if len(delays) == 1:
            return
        raise asyncio.CancelledError()

    monkeypatch.setattr("app.bot.runtime.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("app.bot.runtime._supervisor_monotonic", lambda: next(monotonic_values))

    with pytest.raises(asyncio.CancelledError):
        await run_supervised_polling(fake_run_once, settings=settings, current_logger=logging.getLogger("test"))

    assert delays == [
        settings.runtime.bot_polling_backoff_min_delay_seconds,
        settings.runtime.bot_polling_backoff_min_delay_seconds,
    ]
