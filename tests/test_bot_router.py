from __future__ import annotations

from aiogram import Dispatcher

from app.bot.router import build_dispatcher


def test_build_dispatcher_can_be_called_multiple_times() -> None:
    first = build_dispatcher()
    second = build_dispatcher()

    assert isinstance(first, Dispatcher)
    assert isinstance(second, Dispatcher)
    assert first is not second
    assert len(first.sub_routers) == 5
    assert len(second.sub_routers) == 5
