from __future__ import annotations

from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.bot.handlers import create_story
from app.bot.fsm.create_story import CreateStoryStates
from app.config.settings import load_settings
from app.services.story_jobs import StoryJobInputError


class FakeMessage:
    def __init__(self, *, text: str | None = None, video=None, document=None, photo=None) -> None:
        self.video = video
        self.document = document
        self.photo = photo
        self.text = text
        self.message_id = 77
        self.from_user = SimpleNamespace(id=100002)
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs) -> None:
        self.answers.append(text)


class FakeState:
    def __init__(self, initial_data: dict | None = None) -> None:
        self._data = dict(initial_data or {})
        self.current_state = None
        self.cleared = False

    async def get_data(self) -> dict:
        return dict(self._data)

    async def update_data(self, **kwargs) -> None:
        self._data.update(kwargs)

    async def set_state(self, value) -> None:
        self.current_state = value

    async def clear(self) -> None:
        self.cleared = True
        self._data.clear()


@pytest.mark.asyncio
async def test_handle_media_rejects_video_above_size_limit(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    monkeypatch.setenv("MAX_VIDEO_SIZE_BYTES", str(10 * 1024 * 1024))
    load_settings.cache_clear()
    settings = load_settings(isolated_settings.paths.project_root)

    async def fake_ensure_user_allowed(_message, _settings):
        return True

    async def fail_download_message_media(self, **_kwargs):
        raise AssertionError("download_message_media should not be called for oversized videos")

    monkeypatch.setattr(create_story, "ensure_user_allowed", fake_ensure_user_allowed)
    monkeypatch.setattr(create_story.BotMediaIngress, "download_message_media", fail_download_message_media)

    message = FakeMessage(
        video=SimpleNamespace(
            file_id="video-file",
            file_name="oversized.mp4",
            file_size=32 * 1024 * 1024,
        )
    )

    await create_story.handle_media(
        message=message,
        state=SimpleNamespace(),
        bot=object(),
        settings=settings,
        media_service=SimpleNamespace(),
        telegram_runtime=None,
    )

    assert len(message.answers) == 1
    assert "Видео слишком большое" in message.answers[0]
    assert "10 МБ" in message.answers[0]


def test_render_video_size_limit_error_includes_actual_and_limit_values() -> None:
    message = create_story.render_video_size_limit_error(
        limit_bytes=500 * 1024 * 1024,
        actual_bytes=32 * 1024 * 1024,
    )

    assert "32 МБ" in message
    assert "500 МБ" in message


@pytest.mark.asyncio
async def test_handle_time_input_keeps_state_on_recoverable_story_job_error(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    async def fake_ensure_user_allowed(_message, _settings):
        return True

    class FailingStoryJobService:
        async def create_job(self, _command):
            raise StoryJobInputError(
                "Время выбрано слишком близко. Укажите публикацию минимум через 2 мин.",
                code="schedule_too_close",
                earliest_allowed_local=datetime(2026, 3, 22, 12, 31, tzinfo=ZoneInfo("Europe/Moscow")),
            )

    monkeypatch.setattr(create_story, "ensure_user_allowed", fake_ensure_user_allowed)

    state = FakeState(
        {
            "schedule_type": "once",
            "media_type": "video",
            "media_path": "videos/test.mp4",
            "prepared_media_path": "prepared/videos/test.mp4",
            "caption": "caption",
            "scheduled_date": "2026-03-22",
            "selected_days": [],
        }
    )
    message = FakeMessage(text="12:28")

    await create_story.handle_time_input(
        message=message,
        state=state,
        settings=isolated_settings,
        story_job_service=FailingStoryJobService(),
    )

    assert state.cleared is False
    assert state.current_state == CreateStoryStates.waiting_time_input
    assert any("Ближайшее безопасное время" in answer for answer in message.answers)


@pytest.mark.asyncio
async def test_waiting_time_choice_accepts_direct_time_input(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    async def fake_ensure_user_allowed(_message, _settings):
        return True

    class SuccessfulStoryJobService:
        async def create_job(self, _command):
            return SimpleNamespace(id=15, scheduled_time=time(12, 35), scheduled_date=None)

    monkeypatch.setattr(create_story, "ensure_user_allowed", fake_ensure_user_allowed)

    state = FakeState(
        {
            "schedule_type": "weekly",
            "media_type": "video",
            "media_path": "videos/test.mp4",
            "prepared_media_path": "prepared/videos/test.mp4",
            "caption": "caption",
            "selected_days": ["mon", "wed"],
        }
    )
    message = FakeMessage(text="12:35")

    await create_story.handle_time_input_without_manual_button(
        message=message,
        state=state,
        settings=isolated_settings,
        story_job_service=SuccessfulStoryJobService(),
    )

    assert state.cleared is True
    assert any("Еженедельная сторис запланирована" in answer for answer in message.answers)


@pytest.mark.asyncio
async def test_waiting_date_accepts_direct_manual_date_input(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    async def fake_ensure_user_allowed(_message, _settings):
        return True

    monkeypatch.setattr(create_story, "ensure_user_allowed", fake_ensure_user_allowed)

    state = FakeState({"schedule_type": "once"})
    future_date = create_story.datetime_now_local_date(isolated_settings.runtime.timezone) + timedelta(days=1)
    message = FakeMessage(text=future_date.strftime("%d.%m.%Y"))

    await create_story.handle_date_input_without_manual_button(
        message=message,
        state=state,
        settings=isolated_settings,
    )

    data = await state.get_data()
    assert data["scheduled_date"] == future_date.isoformat()
    assert state.current_state == CreateStoryStates.waiting_time_choice
    assert any("Выберите способ ввода времени" in answer for answer in message.answers)
