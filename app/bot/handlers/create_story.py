from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.access import ensure_user_allowed
from app.bot.date_formats import format_user_date, parse_user_date_string
from app.bot.fsm.create_story import CreateStoryStates
from app.bot.keyboards.main import (
    WEEKDAY_LABELS,
    cancel_keyboard,
    date_selection_keyboard,
    main_menu_keyboard,
    schedule_type_keyboard,
    time_input_keyboard,
    weekdays_keyboard,
)
from app.bot.media_ingress import BotMediaIngress
from app.config.settings import Settings
from app.db.models import MediaType, ScheduleType
from app.media.service import MediaPreparationService
from app.scheduler.rules import parse_time_string
from app.services.story_jobs import CreateStoryJobCommand, StoryJobInputError, StoryJobService

logger = logging.getLogger(__name__)
RUSSIAN_DAY_BY_CODE = {code: label for label, code in WEEKDAY_LABELS}
BYTES_IN_MEBIBYTE = 1024 * 1024


class MediaProgressReporter:
    def __init__(self, message: Message) -> None:
        self._message = message
        self._status_message: Message | None = None
        self._last_text: str | None = None

    async def send(self, text: str) -> None:
        if text == self._last_text:
            logger.debug("Skipping duplicate media progress update text=%s", text)
            return
        self._last_text = text

        if self._status_message is None:
            logger.info("Sending initial media progress update text=%s", text)
            candidate = await self._message.answer(text, reply_markup=cancel_keyboard())
            self._status_message = candidate if hasattr(candidate, "edit_text") else None
            return

        logger.info("Updating media progress message text=%s", text)
        try:
            await self._status_message.edit_text(text, reply_markup=cancel_keyboard())
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                logger.debug("Skipping no-op media progress edit")
                return
            logger.warning("Media progress edit failed; sending a new message error=%s", exc)
            candidate = await self._message.answer(text, reply_markup=cancel_keyboard())
            self._status_message = candidate if hasattr(candidate, "edit_text") else None


def _format_minimum_allowed_time_hint(error: StoryJobInputError, settings: Settings) -> str:
    if error.earliest_allowed_local is None:
        return ""
    return (
        "\n"
        f"Ближайшее безопасное время: {format_user_date(error.earliest_allowed_local.date())} "
        f"{error.earliest_allowed_local.strftime('%H:%M')} ({settings.runtime.timezone})."
    )


def _time_retry_prompt(error: StoryJobInputError, settings: Settings) -> str:
    return (
        f"❌ {error}"
        f"{_format_minimum_allowed_time_hint(error, settings)}\n"
        "Введите новое время в формате ЧЧ:ММ."
    )


def _video_extension(file_name: str | None) -> str:
    if not file_name:
        return ".mp4"
    suffix = Path(file_name).suffix.lower()
    return suffix if suffix else ".mp4"


def _format_mebibytes(size_bytes: int) -> str:
    value = size_bytes / BYTES_IN_MEBIBYTE
    formatted = f"{value:.1f}".rstrip("0").rstrip(".")
    return formatted


def render_video_size_limit_error(*, limit_bytes: int, actual_bytes: int | None) -> str:
    limit_text = _format_mebibytes(limit_bytes)
    if actual_bytes is None:
        return f"❌ Видео слишком большое. Максимальный размер для загрузки через бота: {limit_text} МБ."
    actual_text = _format_mebibytes(actual_bytes)
    return (
        f"❌ Видео слишком большое: {actual_text} МБ.\n"
        f"Максимальный размер для загрузки через бота: {limit_text} МБ."
    )


async def schedule_story_start(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(callback, settings):
        return
    await state.clear()
    logger.info("Starting schedule creation flow for user_id=%s", callback.from_user.id if callback.from_user else None)
    await callback.message.edit_text("Выберите тип планирования:", reply_markup=schedule_type_keyboard())
    await state.set_state(CreateStoryStates.choosing_type)
    await callback.answer()


async def choose_schedule_type(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(callback, settings):
        return
    if callback.data == "type_once":
        await state.update_data(schedule_type=ScheduleType.ONCE.value)
        await callback.message.edit_text("Отправьте фото или видео для сторис:", reply_markup=cancel_keyboard())
        await state.set_state(CreateStoryStates.waiting_media)
    else:
        await state.update_data(schedule_type=ScheduleType.WEEKLY.value, selected_days=[])
        await callback.message.edit_text("Выберите дни недели для отправки:", reply_markup=weekdays_keyboard())
        await state.set_state(CreateStoryStates.choosing_days)
    await callback.answer()


async def toggle_weekday(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(callback, settings):
        return
    day = callback.data.split("_", 1)[1]
    data = await state.get_data()
    selected_days = set(data.get("selected_days", []))
    if day in selected_days:
        selected_days.remove(day)
    else:
        selected_days.add(day)
    logger.debug("Toggled weekday=%s selected=%s", day, selected_days)
    await state.update_data(selected_days=sorted(selected_days))
    await callback.message.edit_reply_markup(reply_markup=weekdays_keyboard(selected_days))
    await callback.answer()


async def confirm_weekdays(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(callback, settings):
        return
    data = await state.get_data()
    selected_days = data.get("selected_days", [])
    if not selected_days:
        await callback.answer("Выберите хотя бы один день недели.", show_alert=True)
        return
    logger.info("Confirmed weekdays=%s", selected_days)
    await callback.message.edit_text("Отправьте фото или видео для сторис:", reply_markup=cancel_keyboard())
    await state.set_state(CreateStoryStates.waiting_media)
    await callback.answer()


async def handle_media(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    media_service: MediaPreparationService,
    telegram_runtime=None,
) -> None:
    if not await ensure_user_allowed(message, settings):
        return

    ingress = BotMediaIngress(settings, telegram_runtime=telegram_runtime)
    progress = MediaProgressReporter(message)
    media_type: MediaType | None = None
    original_path: Path | None = None
    file_to_download = None
    media_size_bytes: int | None = None

    if message.photo:
        photo = message.photo[-1]
        media_type = MediaType.PHOTO
        original_path = settings.paths.photos_dir / f"{photo.file_id}.jpg"
        file_to_download = photo
    elif message.video:
        video = message.video
        media_type = MediaType.VIDEO
        original_path = settings.paths.videos_dir / f"{video.file_id}{_video_extension(video.file_name)}"
        file_to_download = video
        media_size_bytes = getattr(video, "file_size", None)
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("video/"):
        document = message.document
        media_type = MediaType.VIDEO
        original_path = settings.paths.videos_dir / f"{document.file_id}{_video_extension(document.file_name)}"
        file_to_download = document
        media_size_bytes = getattr(document, "file_size", None)
    else:
        await message.answer("Отправьте именно фото или видео для сторис.", reply_markup=cancel_keyboard())
        return

    if media_type == MediaType.VIDEO:
        limit_bytes = settings.media.max_video_size_bytes
        logger.debug(
            "Received video upload metadata message_id=%s size_bytes=%s limit_bytes=%s",
            message.message_id,
            media_size_bytes,
            limit_bytes,
        )
        if media_size_bytes is not None and media_size_bytes > limit_bytes:
            logger.warning(
                "Rejecting uploaded video above size policy message_id=%s size_bytes=%s limit_bytes=%s",
                message.message_id,
                media_size_bytes,
                limit_bytes,
            )
            await message.answer(
                render_video_size_limit_error(limit_bytes=limit_bytes, actual_bytes=media_size_bytes),
                reply_markup=cancel_keyboard(),
            )
            return
        logger.info(
            "Accepted video upload within size policy message_id=%s size_bytes=%s limit_bytes=%s",
            message.message_id,
            media_size_bytes,
            limit_bytes,
        )

    try:
        await progress.send("⏳ Получила медиа. Начинаю загрузку...")
        logger.info("Downloading bot media user_id=%s media_type=%s target=%s", message.from_user.id if message.from_user else None, media_type.value, original_path)
        await ingress.download_message_media(
            bot=bot,
            message=message,
            downloadable=file_to_download,
            destination=original_path,
            progress_reporter=progress.send,
        )
        if media_type == MediaType.VIDEO:
            await progress.send("⏳ Видео скачано. Подготавливаю файл для публикации в сторис...")
        else:
            await progress.send("⏳ Фото скачано. Проверяю и подготавливаю файл...")
        prepared = await media_service.prepare(media_type, original_path)
    except Exception as exc:
        logger.exception("Failed to prepare uploaded media")
        await progress.send(f"❌ Не удалось обработать медиа: {exc}")
        return

    await state.update_data(
        media_type=media_type.value,
        media_path=settings.to_relative_runtime_path(original_path),
        prepared_media_path=settings.to_relative_runtime_path(prepared.prepared_path),
    )
    await progress.send("✅ Медиа подготовлено. Теперь введите подпись или отправьте /skip.")
    await message.answer(
        "Введите подпись для сторис или отправьте /skip, чтобы пропустить.",
        reply_markup=cancel_keyboard(),
    )
    await state.set_state(CreateStoryStates.waiting_caption)


async def handle_caption(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(message, settings):
        return
    if not message.text:
        await message.answer("Подпись должна быть текстом или используйте /skip.", reply_markup=cancel_keyboard())
        return

    caption = "" if message.text.strip() == "/skip" else message.text.strip()
    await state.update_data(caption=caption)
    data = await state.get_data()
    logger.debug("Caption accepted length=%s", len(caption))

    if data["schedule_type"] == ScheduleType.ONCE.value:
        await message.answer(
            "Выберите дату публикации:",
            reply_markup=date_selection_keyboard(settings.runtime.timezone),
        )
        await state.set_state(CreateStoryStates.waiting_date)
    else:
        await message.answer("Выберите способ ввода времени:", reply_markup=time_input_keyboard())
        await state.set_state(CreateStoryStates.waiting_time_choice)


async def choose_date_from_keyboard(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(callback, settings):
        return
    selected_date = callback.data.split("_", 1)[1]
    await state.update_data(scheduled_date=selected_date)
    logger.info("Selected scheduled date=%s", selected_date)
    await callback.message.edit_text("Выберите способ ввода времени:", reply_markup=time_input_keyboard())
    await state.set_state(CreateStoryStates.waiting_time_choice)
    await callback.answer()


async def request_manual_date(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(callback, settings):
        return
    await callback.message.edit_text(
        "Введите дату в формате ДД.ММ.ГГГГ. Дата должна быть сегодняшней или будущей.",
        reply_markup=cancel_keyboard(),
    )
    await state.set_state(CreateStoryStates.waiting_date_manual)
    await callback.answer()


async def handle_manual_date(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    await _accept_date_input(message, state, settings)


async def _accept_date_input(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(message, settings):
        return
    try:
        parsed = parse_user_date_string(message.text or "")
    except ValueError:
        await message.answer("Неверный формат даты. Используйте ДД.ММ.ГГГГ.", reply_markup=cancel_keyboard())
        return

    today = datetime_now_local_date(settings.runtime.timezone)
    if parsed < today:
        await message.answer("Дата уже прошла. Укажите сегодняшнюю или будущую дату.", reply_markup=cancel_keyboard())
        return

    await state.update_data(scheduled_date=parsed.isoformat())
    logger.info("Accepted manual scheduled date=%s", parsed.isoformat())
    await message.answer("Выберите способ ввода времени:", reply_markup=time_input_keyboard())
    await state.set_state(CreateStoryStates.waiting_time_choice)


def datetime_now_local_date(timezone_name: str):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(timezone_name)).date()


async def request_manual_time(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(callback, settings):
        return
    await callback.message.edit_text("Введите время в формате ЧЧ:ММ.", reply_markup=cancel_keyboard())
    await state.set_state(CreateStoryStates.waiting_time_input)
    await callback.answer()


async def handle_date_input_without_manual_button(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    await _accept_date_input(message, state, settings)


async def _finalize_time_input(
    message: Message,
    state: FSMContext,
    settings: Settings,
    story_job_service: StoryJobService,
) -> None:
    if not await ensure_user_allowed(message, settings):
        return
    try:
        scheduled_time = parse_time_string(message.text or "")
    except ValueError:
        await message.answer("Неверный формат времени. Используйте ЧЧ:ММ.", reply_markup=cancel_keyboard())
        return

    data = await state.get_data()
    schedule_type = ScheduleType(data["schedule_type"])
    media_type = MediaType(data["media_type"])
    weekdays = tuple(
        ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(day)
        for day in data.get("selected_days", [])
    )

    try:
        job = await story_job_service.create_job(
            CreateStoryJobCommand(
                schedule_type=schedule_type,
                media_type=media_type,
                media_path=data["media_path"],
                prepared_media_path=data["prepared_media_path"],
                caption=data.get("caption", ""),
                scheduled_time=scheduled_time,
                scheduled_date=data.get("scheduled_date"),
                weekdays=weekdays,
                timezone=settings.runtime.timezone,
            )
        )
    except StoryJobInputError as exc:
        logger.warning("Recoverable story job validation error code=%s error=%s", exc.code, exc)
        await message.answer(_time_retry_prompt(exc, settings), reply_markup=cancel_keyboard())
        await state.set_state(CreateStoryStates.waiting_time_input)
        return
    except Exception as exc:
        logger.exception("Failed to create story job")
        await message.answer(f"❌ Не удалось сохранить задачу: {exc}", reply_markup=main_menu_keyboard())
        await state.clear()
        return

    logger.info("Created scheduled job id=%s", job.id)
    await state.clear()
    if schedule_type == ScheduleType.WEEKLY:
        days_text = ", ".join(RUSSIAN_DAY_BY_CODE.get(code, code) for code in data.get("selected_days", []))
        await message.answer(
            f"✅ Еженедельная сторис запланирована.\nID: {job.id}\nДни: {days_text}\nВремя: {job.scheduled_time.strftime('%H:%M')}",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await message.answer(
            f"✅ Сторис запланирована.\nID: {job.id}\nДата: {format_user_date(job.scheduled_date)}\nВремя: {job.scheduled_time.strftime('%H:%M')}",
            reply_markup=main_menu_keyboard(),
        )


async def handle_time_input_without_manual_button(
    message: Message,
    state: FSMContext,
    settings: Settings,
    story_job_service: StoryJobService,
) -> None:
    await _finalize_time_input(message, state, settings, story_job_service)


async def handle_time_input(
    message: Message,
    state: FSMContext,
    settings: Settings,
    story_job_service: StoryJobService,
) -> None:
    await _finalize_time_input(message, state, settings, story_job_service)


async def handle_choose_type_fallback(
    message: Message,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(message, settings):
        return
    logger.debug("Unexpected text during schedule type selection user_id=%s", message.from_user.id if message.from_user else None)
    await message.answer("Выберите тип планирования кнопками ниже.", reply_markup=schedule_type_keyboard())


async def handle_choose_days_fallback(
    message: Message,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(message, settings):
        return
    logger.debug("Unexpected text during weekday selection user_id=%s", message.from_user.id if message.from_user else None)
    await message.answer("Выберите один или несколько дней кнопками и нажмите «Готово».", reply_markup=weekdays_keyboard())


async def handle_waiting_media_callback_fallback(
    callback: CallbackQuery,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(callback, settings):
        return
    logger.debug("Unexpected callback during media upload wait user_id=%s", callback.from_user.id if callback.from_user else None)
    await callback.answer("Сейчас нужно отправить фото или видео.", show_alert=True)


async def handle_waiting_time_choice_callback_fallback(
    callback: CallbackQuery,
    settings: Settings,
) -> None:
    if not await ensure_user_allowed(callback, settings):
        return
    logger.debug("Unexpected callback during time choice user_id=%s data=%s", callback.from_user.id if callback.from_user else None, callback.data)
    await callback.answer("Нажмите кнопку ввода времени или отправьте время сообщением в формате ЧЧ:ММ.", show_alert=True)


def build_create_story_router() -> Router:
    router = Router(name="create_story")
    router.callback_query.register(schedule_story_start, F.data == "schedule_story")
    router.callback_query.register(choose_schedule_type, CreateStoryStates.choosing_type, F.data.in_({"type_once", "type_weekly"}))
    router.callback_query.register(toggle_weekday, CreateStoryStates.choosing_days, F.data.startswith("day_"))
    router.callback_query.register(confirm_weekdays, CreateStoryStates.choosing_days, F.data == "confirm_days")
    router.message.register(handle_media, CreateStoryStates.waiting_media)
    router.message.register(handle_caption, CreateStoryStates.waiting_caption)
    router.callback_query.register(choose_date_from_keyboard, CreateStoryStates.waiting_date, F.data.startswith("date_"))
    router.callback_query.register(request_manual_date, CreateStoryStates.waiting_date, F.data == "input_date_manual")
    router.message.register(handle_manual_date, CreateStoryStates.waiting_date_manual)
    router.callback_query.register(request_manual_time, CreateStoryStates.waiting_time_choice, F.data == "input_time_manual")
    router.message.register(handle_date_input_without_manual_button, CreateStoryStates.waiting_date)
    router.message.register(handle_time_input_without_manual_button, CreateStoryStates.waiting_time_choice)
    router.message.register(handle_time_input, CreateStoryStates.waiting_time_input)
    router.message.register(handle_choose_type_fallback, CreateStoryStates.choosing_type)
    router.message.register(handle_choose_days_fallback, CreateStoryStates.choosing_days)
    router.callback_query.register(handle_waiting_media_callback_fallback, CreateStoryStates.waiting_media)
    router.callback_query.register(handle_waiting_time_choice_callback_fallback, CreateStoryStates.waiting_time_choice)
    return router
