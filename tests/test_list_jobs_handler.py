from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from app.bot.handlers import list_jobs


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def edit_text(self, *_args, **_kwargs) -> None:
        raise TelegramBadRequest(EditMessageText(text="same", chat_id=1, message_id=1), "Bad Request: message is not modified")

    async def answer(self, text: str, **_kwargs) -> None:
        self.answers.append(text)


@pytest.mark.asyncio
async def test_show_scheduled_jobs_treats_message_not_modified_as_noop(
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings,
) -> None:
    async def allow(*_args, **_kwargs):
        return True

    monkeypatch.setattr(list_jobs, "ensure_user_allowed", allow)

    async def answer_callback(*_args, **_kwargs):
        return None

    async def list_jobs_async():
        return []

    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=100002),
        message=FakeMessage(),
        answer=answer_callback,
    )

    service = SimpleNamespace(list_jobs=list_jobs_async)

    await list_jobs.show_scheduled_jobs(
        callback=callback,
        settings=isolated_settings,
        story_job_service=service,
    )
