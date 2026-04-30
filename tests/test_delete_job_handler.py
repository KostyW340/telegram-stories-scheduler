from __future__ import annotations

from app.bot.handlers.delete_job import render_delete_result
from app.db.models import StoryJobStatus
from app.services.story_jobs import DeleteStoryJobAction


def test_render_delete_result_for_purged_sent_job() -> None:
    assert (
        render_delete_result(
            7,
            found=True,
            success=True,
            action=DeleteStoryJobAction.PURGED,
            status=StoryJobStatus.SENT,
        )
        == "✅ Задача 7 удалена из списка."
    )


def test_render_delete_result_for_cancelled_pending_job() -> None:
    assert (
        render_delete_result(
            7,
            found=True,
            success=True,
            action=DeleteStoryJobAction.CANCELLED,
            status=StoryJobStatus.CANCELLED,
        )
        == "✅ Задача 7 отменена и больше не будет отправлена."
    )


def test_render_delete_result_for_missing_job() -> None:
    assert render_delete_result(7, found=False, success=False, action=None, status=None) == "❌ Задача с ID 7 не найдена."


def test_render_delete_result_for_processing_job() -> None:
    assert (
        render_delete_result(
            7,
            found=True,
            success=False,
            action=None,
            status=StoryJobStatus.PROCESSING,
        )
        == "❌ Задача 7 сейчас обрабатывается. Попробуйте удалить её позже."
    )
