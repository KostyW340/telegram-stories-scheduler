from __future__ import annotations

import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)

USER_DATE_FORMAT = "%d.%m.%Y"


def parse_user_date_string(value: str) -> date:
    normalized = value.strip()
    logger.debug("Parsing user-facing date value=%s", normalized)
    return datetime.strptime(normalized, USER_DATE_FORMAT).date()


def format_user_date(value: date | None, placeholder: str = "-") -> str:
    if value is None:
        return placeholder
    return value.strftime(USER_DATE_FORMAT)
