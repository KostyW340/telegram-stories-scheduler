from __future__ import annotations

from datetime import date

import pytest

from app.bot.date_formats import format_user_date, parse_user_date_string


def test_parse_user_date_string_accepts_russian_date_format() -> None:
    assert parse_user_date_string("21.03.2026") == date(2026, 3, 21)


def test_parse_user_date_string_rejects_iso_format() -> None:
    with pytest.raises(ValueError):
        parse_user_date_string("2026-03-21")


def test_format_user_date_returns_russian_date_format() -> None:
    assert format_user_date(date(2026, 3, 21)) == "21.03.2026"


def test_format_user_date_uses_placeholder_for_none() -> None:
    assert format_user_date(None) == "-"
