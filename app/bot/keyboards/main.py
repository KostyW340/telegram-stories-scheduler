from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

WEEKDAY_LABELS: tuple[tuple[str, str], ...] = (
    ("Понедельник", "mon"),
    ("Вторник", "tue"),
    ("Среда", "wed"),
    ("Четверг", "thu"),
    ("Пятница", "fri"),
    ("Суббота", "sat"),
    ("Воскресенье", "sun"),
)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Запланировать сторис", callback_data="schedule_story")],
            [InlineKeyboardButton(text="Мои запланированные", callback_data="my_scheduled")],
            [InlineKeyboardButton(text="Принудительно отправить", callback_data="manual_send_task")],
            [InlineKeyboardButton(text="Удалить задачу", callback_data="delete_task")],
        ]
    )


def schedule_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Единоразовая", callback_data="type_once")],
            [InlineKeyboardButton(text="Еженедельная", callback_data="type_weekly")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ]
    )


def weekdays_keyboard(selected_days: set[str] | None = None) -> InlineKeyboardMarkup:
    chosen = selected_days or set()
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for label, code in WEEKDAY_LABELS:
        mark = "✅ " if code in chosen else ""
        current_row.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"day_{code}"))
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    rows.append([InlineKeyboardButton(text="Подтвердить дни", callback_data="confirm_days")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def date_selection_keyboard(timezone_name: str) -> InlineKeyboardMarkup:
    now = datetime.now(ZoneInfo(timezone_name))
    today = now.date()
    tomorrow = today + timedelta(days=1)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сегодня", callback_data=f"date_{today.isoformat()}")],
            [InlineKeyboardButton(text="Завтра", callback_data=f"date_{tomorrow.isoformat()}")],
            [InlineKeyboardButton(text="Ввести дату вручную", callback_data="input_date_manual")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ]
    )


def time_input_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Ввести время вручную", callback_data="input_time_manual")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel")]]
    )
