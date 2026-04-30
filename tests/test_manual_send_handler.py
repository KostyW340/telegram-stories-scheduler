from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot.fsm.create_story import ManualSendStoryStates
from app.bot.handlers import manual_send
from app.db.models import StoryJobStatus
from app.services.story_jobs import ManualSendStoryJobOutcome, ManualSendStoryJobResult


class FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=100002)
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs) -> None:
        self.answers.append(text)


class FakeState:
    def __init__(self) -> None:
        self.current_state = None
        self.cleared = False

    async def set_state(self, value) -> None:
        self.current_state = value

    async def clear(self) -> None:
        self.cleared = True


@pytest.mark.asyncio
async def test_manual_send_start_sets_state_and_prompts_for_job_id(monkeypatch: pytest.MonkeyPatch, isolated_settings) -> None:
    async def allow(*_args, **_kwargs):
        return True

    monkeypatch.setattr(manual_send, "ensure_user_allowed", allow)
    state = FakeState()
    callback_message = FakeMessage()

    async def answer_callback(*_args, **_kwargs):
        return None

    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=100002),
        message=callback_message,
        answer=answer_callback,
    )

    await manual_send.manual_send_start(callback=callback, state=state, settings=isolated_settings)

    assert state.current_state == ManualSendStoryStates.waiting_job_id
    assert any("Введите ID задачи" in answer for answer in callback_message.answers)


@pytest.mark.asyncio
async def test_manual_send_by_id_reprompts_on_non_numeric_input(monkeypatch: pytest.MonkeyPatch, isolated_settings) -> None:
    async def allow(*_args, **_kwargs):
        return True

    monkeypatch.setattr(manual_send, "ensure_user_allowed", allow)
    state = FakeState()
    state.current_state = ManualSendStoryStates.waiting_job_id
    message = FakeMessage("abc")

    await manual_send.manual_send_by_id(
        message=message,
        state=state,
        settings=isolated_settings,
        story_job_service=SimpleNamespace(),
    )

    assert state.cleared is False
    assert any("Введите корректный числовой ID" in answer for answer in message.answers)


@pytest.mark.asyncio
async def test_manual_send_by_id_returns_success_message(monkeypatch: pytest.MonkeyPatch, isolated_settings) -> None:
    async def allow(*_args, **_kwargs):
        return True

    class FakeStoryJobService:
        async def manual_send_job(self, _job_id: int, *, operator_user_id: int):
            assert operator_user_id == 100002
            return ManualSendStoryJobResult(
                job_id=14,
                outcome=ManualSendStoryJobOutcome.PUBLISHED,
                found=True,
                success=True,
                previous_status=StoryJobStatus.PENDING,
                final_status=StoryJobStatus.SENT,
                operator_message="ok",
                story_id=777,
                update_type="UpdateStoryID",
            )

    monkeypatch.setattr(manual_send, "ensure_user_allowed", allow)
    state = FakeState()
    state.current_state = ManualSendStoryStates.waiting_job_id
    message = FakeMessage("14")

    await manual_send.manual_send_by_id(
        message=message,
        state=state,
        settings=isolated_settings,
        story_job_service=FakeStoryJobService(),
    )

    assert state.cleared is True
    assert any("Задача 14 отправлена принудительно" in answer for answer in message.answers)
