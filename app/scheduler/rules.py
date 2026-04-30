from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db.models import ScheduleType

logger = logging.getLogger(__name__)

WEEKDAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_TO_INDEX = {code: index for index, code in enumerate(WEEKDAY_CODES)}


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_time_string(value: str) -> time:
    logger.debug("Parsing scheduled time value=%s", value)
    return datetime.strptime(value.strip(), "%H:%M").time()


def parse_date_string(value: str) -> date:
    logger.debug("Parsing scheduled date value=%s", value)
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def encode_weekdays(days: tuple[int, ...]) -> str:
    encoded = ",".join(WEEKDAY_CODES[day] for day in sorted(set(days)))
    logger.debug("Encoded weekdays %s -> %s", days, encoded)
    return encoded


def decode_weekdays(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    decoded = tuple(
        WEEKDAY_TO_INDEX[token.strip().lower()]
        for token in value.split(",")
        if token.strip()
    )
    logger.debug("Decoded weekdays %s -> %s", value, decoded)
    return decoded


def localize_utc_naive(value: datetime, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    return value.replace(tzinfo=timezone.utc).astimezone(zone)


def to_utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def compute_next_run_at(
    schedule_type: ScheduleType,
    scheduled_time: time,
    timezone_name: str,
    *,
    scheduled_date: date | None = None,
    weekdays: tuple[int, ...] = (),
    now_utc: datetime | None = None,
    last_sent_date: date | None = None,
) -> datetime | None:
    reference_utc = now_utc or utcnow_naive()
    reference_local = localize_utc_naive(reference_utc, timezone_name)
    logger.debug(
        "Computing next run schedule_type=%s timezone=%s scheduled_date=%s scheduled_time=%s weekdays=%s last_sent_date=%s now_utc=%s",
        schedule_type.value,
        timezone_name,
        scheduled_date,
        scheduled_time,
        weekdays,
        last_sent_date,
        reference_utc,
    )

    zone = ZoneInfo(timezone_name)
    if schedule_type == ScheduleType.ONCE:
        if scheduled_date is None:
            raise ValueError("scheduled_date is required for one-time jobs")
        candidate_local = datetime.combine(scheduled_date, scheduled_time, tzinfo=zone)
        if candidate_local < reference_local:
            logger.warning("One-time schedule is already in the past for date=%s time=%s", scheduled_date, scheduled_time)
            return None
        return to_utc_naive(candidate_local)

    if not weekdays:
        raise ValueError("At least one weekday is required for weekly jobs")

    for day_offset in range(0, 8):
        candidate_date = reference_local.date() + timedelta(days=day_offset)
        if candidate_date.weekday() not in weekdays:
            continue
        if last_sent_date and candidate_date == last_sent_date:
            logger.debug("Skipping candidate date=%s because it already sent on that date", candidate_date)
            continue

        candidate_local = datetime.combine(candidate_date, scheduled_time, tzinfo=zone)
        if candidate_local < reference_local:
            logger.debug("Skipping past weekly candidate %s", candidate_local)
            continue
        return to_utc_naive(candidate_local)

    logger.warning("Could not compute next weekly run for weekdays=%s", weekdays)
    return None


def local_schedule_date(timestamp_utc: datetime, timezone_name: str) -> date:
    return localize_utc_naive(timestamp_utc, timezone_name).date()
