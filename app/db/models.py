from __future__ import annotations

from datetime import date, datetime, time, timezone
from enum import StrEnum

from sqlalchemy import Date, DateTime, Enum, Integer, String, Text, Time
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class ScheduleType(StrEnum):
    ONCE = "once"
    WEEKLY = "weekly"


class MediaType(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"


class StoryJobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StoryJob(Base):
    __tablename__ = "scheduled_stories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    photo_path: Mapped[str] = mapped_column(Text, nullable=False)
    media_path: Mapped[str] = mapped_column(Text, nullable=False)
    prepared_media_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_time: Mapped[time] = mapped_column(Time, nullable=False)
    scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    status: Mapped[StoryJobStatus] = mapped_column(
        Enum(StoryJobStatus, native_enum=False, length=16),
        default=StoryJobStatus.PENDING,
        nullable=False,
    )
    schedule_type: Mapped[ScheduleType] = mapped_column(
        Enum(ScheduleType, native_enum=False, length=16),
        default=ScheduleType.ONCE,
        nullable=False,
    )
    days: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow", nullable=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sent_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    media_type: Mapped[MediaType] = mapped_column(
        Enum(MediaType, native_enum=False, length=16),
        default=MediaType.PHOTO,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    retry_window_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def to_log_context(self) -> dict[str, object]:
        return {
            "job_id": self.id,
            "schedule_type": self.schedule_type.value,
            "status": self.status.value,
            "media_type": self.media_type.value,
            "scheduled_date": self.scheduled_date.isoformat() if self.scheduled_date else None,
            "scheduled_time": self.scheduled_time.isoformat(timespec="minutes"),
            "retry_count": self.retry_count,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "retry_window_started_at": (
                self.retry_window_started_at.isoformat() if self.retry_window_started_at else None
            ),
        }
