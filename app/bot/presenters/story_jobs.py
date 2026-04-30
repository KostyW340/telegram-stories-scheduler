from __future__ import annotations

from app.bot.date_formats import format_user_date
from app.db.models import MediaType, ScheduleType, StoryJob, StoryJobStatus
from app.telegram.failures import (
    looks_like_auto_post_expired_message,
    looks_like_transient_failure_message,
    looks_like_weekly_rollover_message,
)

RUSSIAN_WEEKDAYS = {
    "mon": "Понедельник",
    "tue": "Вторник",
    "wed": "Среда",
    "thu": "Четверг",
    "fri": "Пятница",
    "sat": "Суббота",
    "sun": "Воскресенье",
}


def _caption_preview(caption: str | None, limit: int = 32) -> str:
    if not caption:
        return "нет"
    normalized = " ".join(caption.split())
    return normalized if len(normalized) <= limit else f"{normalized[:limit - 3]}..."


def _status_prefix(status: StoryJobStatus) -> str:
    if status == StoryJobStatus.SENT:
        return "✅"
    if status == StoryJobStatus.FAILED:
        return "❌"
    if status == StoryJobStatus.CANCELLED:
        return "🚫"
    if status == StoryJobStatus.PROCESSING:
        return "⚙️"
    return "⏳"


def _render_status_text(job: StoryJob) -> str:
    if job.status == StoryJobStatus.PENDING and looks_like_transient_failure_message(job.last_error):
        return "pending (повторяется автоматически до конца текущих суток)"
    if job.status == StoryJobStatus.PENDING and job.schedule_type == ScheduleType.WEEKLY and job.last_sent_date:
        return "pending (следующая отправка по графику)"
    if job.status == StoryJobStatus.FAILED and looks_like_auto_post_expired_message(job.last_error):
        return "failed (автоокно закрыто в 00:00, можно отправить вручную по ID)"
    if job.status == StoryJobStatus.FAILED and looks_like_weekly_rollover_message(job.last_error):
        return "failed (пропущенный день завершён, следующая попытка будет по графику)"
    return job.status.value


def format_story_job(job: StoryJob) -> str:
    lines = [f"{_status_prefix(job.status)} ID: {job.id}"]
    lines.append(f"Тип: {'Еженедельная' if job.schedule_type == ScheduleType.WEEKLY else 'Единоразовая'}")
    if job.schedule_type == ScheduleType.ONCE and job.scheduled_date:
        lines.append(f"Дата: {format_user_date(job.scheduled_date)}")
    if job.schedule_type == ScheduleType.WEEKLY and job.days:
        labels = [RUSSIAN_WEEKDAYS.get(code.strip(), code.strip()) for code in job.days.split(",") if code.strip()]
        lines.append(f"Дни: {', '.join(labels)}")
    lines.append(f"Время: {job.scheduled_time.strftime('%H:%M')}")
    lines.append(f"Медиа: {'Видео' if job.media_type == MediaType.VIDEO else 'Фото'}")
    lines.append(f"Подпись: {_caption_preview(job.caption)}")
    lines.append(f"Статус: {_render_status_text(job)}")
    if job.last_sent_date:
        lines.append(f"Последняя отправка: {format_user_date(job.last_sent_date)}")
    return "\n".join(lines)


def format_story_jobs_list(jobs: list[StoryJob]) -> str:
    if not jobs:
        return "📭 Нет запланированных сторис."
    return "📋 Ваши запланированные сторис:\n\n" + "\n\n".join(format_story_job(job) for job in jobs)


def format_story_jobs_chunks(jobs: list[StoryJob], max_chars: int = 3500) -> list[str]:
    if not jobs:
        return ["📭 Нет запланированных сторис."]

    chunks: list[str] = []
    current = "📋 Ваши запланированные сторис:\n\n"
    for entry in [format_story_job(job) for job in jobs]:
        addition = entry + "\n\n"
        if len(current) + len(addition) > max_chars and current.strip():
            chunks.append(current.rstrip())
            current = addition
        else:
            current += addition

    if current.strip():
        chunks.append(current.rstrip())
    return chunks
