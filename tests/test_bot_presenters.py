from __future__ import annotations

from datetime import date, time

from app.bot.presenters.story_jobs import format_story_jobs_chunks, format_story_jobs_list
from app.db.models import MediaType, ScheduleType, StoryJob, StoryJobStatus


def _build_job(job_id: int, caption: str) -> StoryJob:
    return StoryJob(
        id=job_id,
        photo_path="photos/demo.jpg",
        media_path="photos/demo.jpg",
        prepared_media_path="prepared/photos/demo.jpg",
        caption=caption,
        scheduled_time=time(hour=9, minute=0),
        scheduled_date=date(2026, 3, 20),
        status=StoryJobStatus.PENDING,
        schedule_type=ScheduleType.ONCE,
        days=None,
        timezone="Europe/Moscow",
        media_type=MediaType.PHOTO,
        attempt_count=0,
        next_run_at=None,
    )


def test_format_story_jobs_list_contains_expected_fields() -> None:
    text = format_story_jobs_list([_build_job(1, "caption text")])
    assert "ID: 1" in text
    assert "Фото" in text
    assert "caption text" in text
    assert "20.03.2026" in text


def test_format_story_jobs_chunks_splits_long_output() -> None:
    jobs = [_build_job(index, f"caption {index}" * 20) for index in range(1, 10)]
    chunks = format_story_jobs_chunks(jobs, max_chars=500)
    assert len(chunks) > 1


def test_format_story_jobs_list_marks_pending_retry_as_waiting_for_network() -> None:
    job = _build_job(1, "caption text")
    job.retry_count = 2
    job.last_error = "Временный сбой Telegram/сети: Cannot send requests while disconnected"

    text = format_story_jobs_list([job])

    assert "повторяется автоматически до конца текущих суток" in text


def test_format_story_jobs_list_marks_failed_one_time_job_as_auto_window_closed() -> None:
    job = _build_job(2, "caption text")
    job.status = StoryJobStatus.FAILED
    job.last_error = "Автоматическая отправка завершена после окончания дня: наступили следующие сутки"

    text = format_story_jobs_list([job])

    assert "автоокно закрыто" in text


def test_format_story_jobs_list_marks_failed_weekly_job_as_waiting_for_schedule() -> None:
    job = _build_job(3, "caption text")
    job.status = StoryJobStatus.FAILED
    job.schedule_type = ScheduleType.WEEKLY
    job.days = "mon"
    job.last_error = "Пропущенная еженедельная отправка перенесена на следующий день по графику: пропущенный день завершён"

    text = format_story_jobs_list([job])

    assert "следующая попытка будет по графику" in text


def test_format_story_jobs_list_marks_weekly_pending_job_as_next_scheduled_send() -> None:
    job = _build_job(4, "caption text")
    job.schedule_type = ScheduleType.WEEKLY
    job.days = "sun"
    job.last_sent_date = date(2026, 4, 19)

    text = format_story_jobs_list([job])

    assert "следующая отправка по графику" in text
