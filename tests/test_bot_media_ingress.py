from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetFile

from app.bot.media_ingress import BotMediaIngress, MediaIngressError, is_bot_api_file_too_big_error


class FakeBot:
    def __init__(self, *, username: str = "demo_story_bot") -> None:
        self._username = username
        self.download_calls = 0

    async def download(self, _downloadable, destination: Path) -> None:
        self.download_calls += 1
        raise TelegramBadRequest(GetFile(file_id="video"), "Bad Request: file is too big")

    async def get_me(self):
        return SimpleNamespace(username=self._username, id=100001)


class FakeTelethonClient:
    def __init__(self, downloaded_path: Path) -> None:
        self.downloaded_path = downloaded_path
        self.entities: list[object] = []
        self.message_requests: list[tuple[object, int]] = []
        self.iter_message_limit: int | None = None
        self.direct_message = None
        self.iter_candidates: list[object] = []
        self.download_calls: list[object] = []

    async def get_entity(self, peer_ref):
        self.entities.append(peer_ref)
        return peer_ref

    async def get_messages(self, peer, ids: int):
        self.message_requests.append((peer, ids))
        return self.direct_message

    async def iter_messages(self, _peer, limit: int):
        self.iter_message_limit = limit
        for candidate in self.iter_candidates:
            yield candidate

    async def download_media(self, message_or_media, file: str, progress_callback=None):
        self.download_calls.append(message_or_media)
        if progress_callback is not None:
            await progress_callback(50, 100)
            await progress_callback(100, 100)
        Path(file).write_bytes(b"video")
        return file


@pytest.mark.asyncio
async def test_media_ingress_falls_back_to_mtproto_when_bot_api_file_is_too_big(
    isolated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = isolated_settings.paths.videos_dir / "video.mp4"
    bot = FakeBot()
    telethon_client = FakeTelethonClient(destination)
    telethon_client.direct_message = SimpleNamespace(
        id=77,
        out=True,
        media=object(),
        date=datetime(2026, 3, 21, 16, 15, tzinfo=UTC),
        file=SimpleNamespace(
            id="bot-file-id",
            size=32 * 1024 * 1024,
            name="video.mp4",
            mime_type="video/mp4",
            width=1080,
            height=1920,
            duration=15,
        ),
    )

    @asynccontextmanager
    async def fake_connected_user_client(_settings):
        yield telethon_client

    monkeypatch.setattr("app.bot.media_ingress.connected_user_client", fake_connected_user_client)
    async def no_resolved_download(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.bot.media_ingress._download_via_resolved_bot_file_id", no_resolved_download)

    ingress = BotMediaIngress(isolated_settings)
    result = await ingress.download_message_media(
        bot=bot,
        message=SimpleNamespace(message_id=77, date=datetime(2026, 3, 21, 16, 15, tzinfo=UTC)),
        downloadable=SimpleNamespace(
            file_id="bot-file-id",
            file_size=32 * 1024 * 1024,
            file_name="video.mp4",
            mime_type="video/mp4",
            width=1080,
            height=1920,
            duration=15,
        ),
        destination=destination,
    )

    assert result == destination
    assert destination.exists()
    assert telethon_client.entities == ["@demo_story_bot"]
    assert telethon_client.message_requests == [("@demo_story_bot", 77)]
    assert telethon_client.download_calls == [telethon_client.direct_message]


@pytest.mark.asyncio
async def test_media_ingress_reuses_supplied_telegram_runtime_for_mtproto_fallback(
    isolated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = isolated_settings.paths.videos_dir / "video.mp4"
    bot = FakeBot()
    telethon_client = FakeTelethonClient(destination)
    telethon_client.direct_message = SimpleNamespace(
        id=77,
        out=True,
        media=object(),
        date=datetime(2026, 3, 21, 16, 15, tzinfo=UTC),
        file=SimpleNamespace(id="bot-file-id", size=32 * 1024 * 1024, name="video.mp4", mime_type="video/mp4"),
    )

    class FakeTelegramRuntime:
        def __init__(self) -> None:
            self.context_calls = 0
            self.roles: list[object] = []
            self.peer_resolution_calls: list[tuple[object, object]] = []

        @asynccontextmanager
        async def client_context(self, role):
            self.context_calls += 1
            self.roles.append(role)
            yield telethon_client

        async def resolve_input_peer(self, role, client, peer_reference):
            self.peer_resolution_calls.append((role, peer_reference))
            return await client.get_entity(peer_reference)

    def fail_connected_user_client(_settings):
        raise AssertionError("connected_user_client should not be used when a shared runtime is supplied")

    monkeypatch.setattr("app.bot.media_ingress.connected_user_client", fail_connected_user_client)
    async def no_resolved_download(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.bot.media_ingress._download_via_resolved_bot_file_id", no_resolved_download)

    telegram_runtime = FakeTelegramRuntime()
    ingress = BotMediaIngress(isolated_settings, telegram_runtime=telegram_runtime)
    result = await ingress.download_message_media(
        bot=bot,
        message=SimpleNamespace(message_id=77, date=datetime(2026, 3, 21, 16, 15, tzinfo=UTC)),
        downloadable=SimpleNamespace(
            file_id="bot-file-id",
            file_size=32 * 1024 * 1024,
            file_name="video.mp4",
            mime_type="video/mp4",
        ),
        destination=destination,
    )

    assert result == destination
    assert telegram_runtime.context_calls == 1
    assert len(telegram_runtime.roles) == 1
    assert len(telegram_runtime.peer_resolution_calls) == 1


def test_is_bot_api_file_too_big_error_detects_limit_message() -> None:
    assert is_bot_api_file_too_big_error(RuntimeError("Bad Request: file is too big")) is True
    assert is_bot_api_file_too_big_error(RuntimeError("Bad Request: wrong file identifier")) is False


@pytest.mark.asyncio
async def test_media_ingress_raises_clear_error_for_other_bot_api_failures(
    isolated_settings,
) -> None:
    class SmallBot:
        async def download(self, _downloadable, destination: Path) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            raise TelegramBadRequest(GetFile(file_id="video"), "Bad Request: wrong file identifier")

    ingress = BotMediaIngress(isolated_settings)

    with pytest.raises(MediaIngressError):
        await ingress.download_message_media(
            bot=SmallBot(),
            message=SimpleNamespace(message_id=77),
            downloadable=object(),
            destination=isolated_settings.paths.videos_dir / "video.mp4",
        )


@pytest.mark.asyncio
async def test_media_ingress_uses_resolved_bot_file_id_before_dialog_lookup(
    isolated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = isolated_settings.paths.videos_dir / "video.mp4"
    bot = FakeBot()
    telethon_client = FakeTelethonClient(destination)

    @asynccontextmanager
    async def fake_connected_user_client(_settings):
        yield telethon_client

    async def fake_resolved_download(_client, downloadable, file_destination: Path, **_kwargs):
        assert downloadable.file_id == "bot-file-id"
        file_destination.write_bytes(b"video")
        return file_destination

    monkeypatch.setattr("app.bot.media_ingress.connected_user_client", fake_connected_user_client)
    monkeypatch.setattr("app.bot.media_ingress._download_via_resolved_bot_file_id", fake_resolved_download)

    ingress = BotMediaIngress(isolated_settings)
    result = await ingress.download_message_media(
        bot=bot,
        message=SimpleNamespace(message_id=77, date=datetime(2026, 3, 21, 16, 15, tzinfo=UTC)),
        downloadable=SimpleNamespace(file_id="bot-file-id"),
        destination=destination,
    )

    assert result == destination
    assert telethon_client.entities == []
    assert telethon_client.message_requests == []


@pytest.mark.asyncio
async def test_media_ingress_scans_recent_dialog_messages_when_direct_lookup_misses(
    isolated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = isolated_settings.paths.videos_dir / "video.mp4"
    bot = FakeBot()
    telethon_client = FakeTelethonClient(destination)
    telethon_client.direct_message = None
    telethon_client.iter_candidates = [
        SimpleNamespace(
            id=91,
            out=False,
            media=object(),
            date=datetime(2026, 3, 21, 16, 14, 30, tzinfo=UTC),
            file=SimpleNamespace(id="other-file", size=1024, name="other.mp4", mime_type="video/mp4"),
        ),
        SimpleNamespace(
            id=92,
            out=True,
            media=object(),
            date=datetime(2026, 3, 21, 16, 15, 1, tzinfo=UTC),
            file=SimpleNamespace(
                id="different-file-id",
                size=32 * 1024 * 1024,
                name="video.mp4",
                mime_type="video/mp4",
                width=1080,
                height=1920,
                duration=15,
            ),
        ),
    ]

    @asynccontextmanager
    async def fake_connected_user_client(_settings):
        yield telethon_client

    monkeypatch.setattr("app.bot.media_ingress.connected_user_client", fake_connected_user_client)
    async def no_resolved_download(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.bot.media_ingress._download_via_resolved_bot_file_id", no_resolved_download)

    ingress = BotMediaIngress(isolated_settings)
    result = await ingress.download_message_media(
        bot=bot,
        message=SimpleNamespace(message_id=77, date=datetime(2026, 3, 21, 16, 15, tzinfo=UTC)),
        downloadable=SimpleNamespace(
            file_id="bot-file-id",
            file_size=32 * 1024 * 1024,
            file_name="video.mp4",
            mime_type="video/mp4",
            width=1080,
            height=1920,
            duration=15,
        ),
        destination=destination,
    )

    assert result == destination
    assert telethon_client.iter_message_limit == 15
    assert telethon_client.download_calls == [telethon_client.iter_candidates[1]]


@pytest.mark.asyncio
async def test_media_ingress_reports_progress_during_mtproto_fallback(
    isolated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = isolated_settings.paths.videos_dir / "video.mp4"
    bot = FakeBot()
    telethon_client = FakeTelethonClient(destination)
    telethon_client.direct_message = SimpleNamespace(
        id=77,
        out=True,
        media=object(),
        date=datetime(2026, 3, 21, 16, 15, tzinfo=UTC),
        file=SimpleNamespace(
            id="different-file-id",
            size=32 * 1024 * 1024,
            name="video.mp4",
            mime_type="video/mp4",
            width=1080,
            height=1920,
            duration=15,
        ),
    )

    @asynccontextmanager
    async def fake_connected_user_client(_settings):
        yield telethon_client

    progress_updates: list[str] = []

    async def capture_progress(text: str) -> None:
        progress_updates.append(text)

    async def no_resolved_download(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.bot.media_ingress.connected_user_client", fake_connected_user_client)
    monkeypatch.setattr("app.bot.media_ingress._download_via_resolved_bot_file_id", no_resolved_download)

    ingress = BotMediaIngress(isolated_settings)
    result = await ingress.download_message_media(
        bot=bot,
        message=SimpleNamespace(message_id=77, date=datetime(2026, 3, 21, 16, 15, tzinfo=UTC)),
        downloadable=SimpleNamespace(
            file_id="bot-file-id",
            file_size=32 * 1024 * 1024,
            file_name="video.mp4",
            mime_type="video/mp4",
            width=1080,
            height=1920,
            duration=15,
        ),
        destination=destination,
        progress_reporter=capture_progress,
    )

    assert result == destination
    assert any("Перехожу на скачивание через личный аккаунт" in text for text in progress_updates)
    assert any("Скачиваю видео через личный аккаунт Telegram" in text for text in progress_updates)
