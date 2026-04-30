from __future__ import annotations

from pathlib import Path

from app.config.settings import detect_sync_managed_path


def test_detect_sync_managed_path_identifies_yandex_disk() -> None:
    assert detect_sync_managed_path(Path(r"C:/Users/user/OneDrive/Projects/Stories/data")) == "OneDrive"


def test_detect_sync_managed_path_returns_none_for_local_path() -> None:
    assert detect_sync_managed_path(Path(r"C:/Stories/data")) is None
