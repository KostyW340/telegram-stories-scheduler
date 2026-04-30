from __future__ import annotations

from datetime import date, datetime, time

from app.db.models import ScheduleType
from app.scheduler.rules import compute_next_run_at, decode_weekdays, encode_weekdays


def test_encode_and_decode_weekdays_roundtrip() -> None:
    encoded = encode_weekdays((0, 2, 4))
    assert encoded == "mon,wed,fri"
    assert decode_weekdays(encoded) == (0, 2, 4)


def test_compute_next_run_for_one_time_job() -> None:
    next_run = compute_next_run_at(
        ScheduleType.ONCE,
        time(hour=9, minute=30),
        "Europe/Moscow",
        scheduled_date=date(2036, 3, 20),
        now_utc=None,
    )
    assert next_run == next_run.replace(year=2036, month=3, day=20, hour=6, minute=30)


def test_compute_next_run_for_weekly_job_skips_last_sent_day() -> None:
    next_run = compute_next_run_at(
        ScheduleType.WEEKLY,
        time(hour=9, minute=0),
        "Europe/Moscow",
        weekdays=(0, 2),
        now_utc=datetime(2036, 3, 24, 6, 1),
        last_sent_date=date(2036, 3, 24),
    )
    assert next_run is not None
