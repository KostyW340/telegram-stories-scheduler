from __future__ import annotations

from pathlib import Path

from app.config.settings import load_settings


def test_load_settings_parses_connectivity_and_proxy_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:token")
    monkeypatch.setenv("BOT_PROXY_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("TELEGRAM_MTPROXY_HOST", "mtproxy.example.com")
    monkeypatch.setenv("TELEGRAM_MTPROXY_PORT", "2002")
    monkeypatch.setenv("TELEGRAM_MTPROXY_SECRET", "abcd")
    monkeypatch.setenv("CONNECTIVITY_SUMMARY_INTERVAL_SECONDS", "90")
    monkeypatch.setenv("MTPROTO_PROBE_INTERVAL_SECONDS", "45")
    monkeypatch.setenv("BOT_POLLING_BACKOFF_MIN_DELAY_SECONDS", "2.5")
    monkeypatch.setenv("BOT_POLLING_BACKOFF_MAX_DELAY_SECONDS", "120")
    monkeypatch.setenv("BOT_POLLING_BACKOFF_FACTOR", "2.1")
    monkeypatch.setenv("BOT_POLLING_BACKOFF_JITTER", "0.5")

    load_settings.cache_clear()
    settings = load_settings(tmp_path)

    assert settings.bot.proxy_url == "http://127.0.0.1:8080"
    assert settings.telegram.mtproto_proxy_host == "mtproxy.example.com"
    assert settings.telegram.mtproto_proxy_port == 2002
    assert settings.telegram.mtproto_proxy_secret == "abcd"
    assert settings.runtime.connectivity_summary_interval_seconds == 90
    assert settings.runtime.mtproto_probe_interval_seconds == 45
    assert settings.runtime.bot_polling_backoff_min_delay_seconds == 2.5
    assert settings.runtime.bot_polling_backoff_max_delay_seconds == 120.0
    assert settings.runtime.bot_polling_backoff_factor == 2.1
    assert settings.runtime.bot_polling_backoff_jitter == 0.5

    load_settings.cache_clear()
