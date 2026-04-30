from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config.settings import load_settings
from app.db.session import dispose_engine


@pytest.fixture()
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("DB_PATH", str(runtime_root / "tasks.db"))
    monkeypatch.setenv("DATA_DIR", str(runtime_root / "data"))
    monkeypatch.setenv("PHOTOS_DIR", str(runtime_root / "photos"))
    monkeypatch.setenv("VIDEOS_DIR", str(runtime_root / "videos"))
    monkeypatch.setenv("PREPARED_PHOTOS_DIR", str(runtime_root / "prepared" / "photos"))
    monkeypatch.setenv("PREPARED_VIDEOS_DIR", str(runtime_root / "prepared" / "videos"))
    monkeypatch.setenv("TEMP_DIR", str(runtime_root / "tmp"))
    monkeypatch.setenv("LOGS_DIR", str(runtime_root / "logs"))
    monkeypatch.setenv("SESSIONS_DIR", str(runtime_root))
    monkeypatch.setenv("APP_TIMEZONE", "Europe/Moscow")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    load_settings.cache_clear()
    settings = load_settings(Path.cwd())
    yield settings
    asyncio.run(dispose_engine())
    load_settings.cache_clear()
