from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import inspect
import logging
from pathlib import Path
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from app.config.settings import Settings, load_settings
from app.telegram.client import connected_user_client
from app.telegram.runtime import TelegramRuntime, TelegramRuntimeRole

logger = logging.getLogger(__name__)
RECENT_DIALOG_MEDIA_LIMIT = 25
MESSAGE_MATCH_SCORE_THRESHOLD = 30
NEARBY_DIALOG_MEDIA_LIMIT = 15
ProgressReporter = Callable[[str], Awaitable[None] | None]


async def _get_bot_reference(bot: Bot) -> object:
    cached_reference = getattr(bot, "_stories_cached_reference", None)
    if cached_reference is not None:
        logger.debug("Reusing cached bot reference=%s for media ingress", cached_reference)
        return cached_reference

    bot_profile = await bot.get_me()
    bot_reference = f"@{bot_profile.username}" if bot_profile.username else bot_profile.id
    setattr(bot, "_stories_cached_reference", bot_reference)
    logger.info("Cached bot reference=%s for MTProto media fallback", bot_reference)
    return bot_reference


class MediaIngressError(RuntimeError):
    """Raised when the bot runtime cannot persist uploaded media locally."""


@dataclass(slots=True, frozen=True)
class MediaLookupHints:
    bot_message_id: int | None
    bot_file_id: str | None
    file_size: int | None
    file_name: str | None
    mime_type: str | None
    width: int | None
    height: int | None
    duration: int | None
    sent_at: datetime | None


def is_bot_api_file_too_big_error(exc: BaseException) -> bool:
    return "file is too big" in str(exc).lower()


async def _emit_progress(progress_reporter: ProgressReporter | None, text: str) -> None:
    if progress_reporter is None:
        return
    logger.debug("Reporting media ingress progress text=%s", text)
    result = progress_reporter(text)
    if inspect.isawaitable(result):
        await result


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _build_lookup_hints(message: Message, downloadable) -> MediaLookupHints:
    return MediaLookupHints(
        bot_message_id=getattr(message, "message_id", None),
        bot_file_id=getattr(downloadable, "file_id", None),
        file_size=getattr(downloadable, "file_size", None),
        file_name=getattr(downloadable, "file_name", None),
        mime_type=getattr(downloadable, "mime_type", None),
        width=getattr(downloadable, "width", None),
        height=getattr(downloadable, "height", None),
        duration=getattr(downloadable, "duration", None),
        sent_at=_normalize_datetime(getattr(message, "date", None)),
    )


def _message_match_score(candidate, hints: MediaLookupHints) -> int:
    if candidate is None or getattr(candidate, "media", None) is None:
        return -1

    score = 0
    telethon_file = getattr(candidate, "file", None)
    if telethon_file is not None:
        if hints.bot_file_id and getattr(telethon_file, "id", None) == hints.bot_file_id:
            score += 100
        if hints.file_size and getattr(telethon_file, "size", None) == hints.file_size:
            score += 45
        if hints.file_name and getattr(telethon_file, "name", None) == hints.file_name:
            score += 20
        candidate_mime_type = getattr(telethon_file, "mime_type", None)
        if hints.mime_type and candidate_mime_type:
            if candidate_mime_type == hints.mime_type:
                score += 12
            elif candidate_mime_type.split("/", 1)[0] == hints.mime_type.split("/", 1)[0]:
                score += 6
        if hints.width and getattr(telethon_file, "width", None) == hints.width:
            score += 6
        if hints.height and getattr(telethon_file, "height", None) == hints.height:
            score += 6
        if hints.duration and getattr(telethon_file, "duration", None) == hints.duration:
            score += 8

    if getattr(candidate, "out", False):
        score += 8

    if hints.bot_message_id and getattr(candidate, "id", None) == hints.bot_message_id:
        score += 4

    candidate_date = _normalize_datetime(getattr(candidate, "date", None))
    if hints.sent_at is not None and candidate_date is not None:
        delta_seconds = abs((candidate_date - hints.sent_at).total_seconds())
        if delta_seconds <= 5:
            score += 24
        elif delta_seconds <= 30:
            score += 16
        elif delta_seconds <= 120:
            score += 10
        elif delta_seconds <= 600:
            score += 4

    return score


async def _download_via_resolved_bot_file_id(
    client,
    downloadable,
    destination: Path,
    *,
    progress_reporter: ProgressReporter | None = None,
    progress_callback=None,
) -> Path | None:
    bot_file_id = getattr(downloadable, "file_id", None)
    if not bot_file_id:
        return None

    from telethon.utils import resolve_bot_file_id

    resolved_media = resolve_bot_file_id(bot_file_id)
    if resolved_media is None:
        logger.info("Telethon could not resolve Bot API file_id for MTProto fallback")
        return None

    logger.info("Trying MTProto media download directly from resolved Bot API file_id")
    await _emit_progress(progress_reporter, "⏳ Нашла Telegram-файл. Скачиваю через личный аккаунт...")
    downloaded_path = await client.download_media(
        resolved_media,
        file=str(destination),
        progress_callback=progress_callback,
    )
    if downloaded_path is None:
        return None
    return Path(downloaded_path)


async def _resolve_peer_via_client(client, peer_reference: object):
    if hasattr(client, "get_input_entity"):
        return await client.get_input_entity(peer_reference)
    return await client.get_entity(peer_reference)


async def _resolve_source_message(client, peer, message: Message, downloadable):
    hints = _build_lookup_hints(message, downloadable)
    direct_candidate = await client.get_messages(peer, ids=message.message_id)
    direct_score = _message_match_score(direct_candidate, hints)
    logger.info(
        "Direct MTProto message lookup completed bot_message_id=%s match_score=%s",
        message.message_id,
        direct_score,
    )
    if direct_score >= MESSAGE_MATCH_SCORE_THRESHOLD:
        return direct_candidate

    if hints.sent_at is not None:
        best_candidate = None
        best_score = -1
        try:
            nearby_candidates = client.iter_messages(
                peer,
                limit=NEARBY_DIALOG_MEDIA_LIMIT,
                offset_date=hints.sent_at,
            )
        except TypeError:
            logger.debug("Telethon client wrapper does not support offset_date in iter_messages; falling back to plain scan")
            nearby_candidates = client.iter_messages(peer, limit=NEARBY_DIALOG_MEDIA_LIMIT)
        async for candidate in nearby_candidates:
            candidate_score = _message_match_score(candidate, hints)
            if candidate_score <= best_score:
                continue
            best_candidate = candidate
            best_score = candidate_score
            if candidate_score >= 100:
                break

        logger.info(
            "Scanned nearby dialog messages for MTProto fallback best_score=%s limit=%s offset_date=%s",
            best_score,
            NEARBY_DIALOG_MEDIA_LIMIT,
            hints.sent_at,
        )
        if best_candidate is not None and best_score >= MESSAGE_MATCH_SCORE_THRESHOLD:
            return best_candidate

    best_candidate = None
    best_score = -1
    async for candidate in client.iter_messages(peer, limit=RECENT_DIALOG_MEDIA_LIMIT):
        candidate_score = _message_match_score(candidate, hints)
        if candidate_score <= best_score:
            continue
        best_candidate = candidate
        best_score = candidate_score
        if candidate_score >= 100:
            break

    logger.info(
        "Scanned recent dialog messages for MTProto fallback best_score=%s limit=%s",
        best_score,
        RECENT_DIALOG_MEDIA_LIMIT,
    )
    if best_candidate is not None and best_score >= MESSAGE_MATCH_SCORE_THRESHOLD:
        return best_candidate
    raise MediaIngressError(
        "Не удалось надёжно сопоставить исходное сообщение с медиа в диалоге с ботом для MTProto-скачивания."
    )


class BotMediaIngress:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        telegram_runtime: TelegramRuntime | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        self._telegram_runtime = telegram_runtime

    async def download_message_media(
        self,
        *,
        bot: Bot,
        message: Message,
        downloadable,
        destination: Path,
        progress_reporter: ProgressReporter | None = None,
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            await _emit_progress(progress_reporter, "⏳ Скачиваю медиа через бота...")
            logger.info("Downloading media through Bot API destination=%s message_id=%s", destination, message.message_id)
            await bot.download(downloadable, destination=destination)
            return destination
        except TelegramBadRequest as exc:
            logger.warning("Bot API media download failed message_id=%s error=%s", message.message_id, exc)
            destination.unlink(missing_ok=True)
            if not is_bot_api_file_too_big_error(exc):
                raise MediaIngressError(f"Не удалось скачать файл через Bot API: {exc}") from exc
            await _emit_progress(progress_reporter, "⏳ Видео слишком большое для Bot API. Перехожу на скачивание через личный аккаунт...")
            logger.info("Switching media ingress to MTProto fallback for message_id=%s", message.message_id)
            return await self._download_via_mtproto(
                bot=bot,
                message=message,
                downloadable=downloadable,
                destination=destination,
                progress_reporter=progress_reporter,
            )

    async def _download_via_mtproto(
        self,
        *,
        bot: Bot,
        message: Message,
        downloadable,
        destination: Path,
        progress_reporter: ProgressReporter | None = None,
    ) -> Path:
        last_reported_bucket = -1

        async def progress_callback(current: int, total: int) -> None:
            nonlocal last_reported_bucket
            if total <= 0:
                return
            percent = min(100, int((current / total) * 100))
            bucket = percent // 10
            if bucket <= last_reported_bucket or bucket <= 0:
                return
            last_reported_bucket = bucket
            await _emit_progress(
                progress_reporter,
                f"⏳ Скачиваю видео через личный аккаунт Telegram... {bucket * 10}%",
            )

        bot_reference = await _get_bot_reference(bot)
        logger.info("Downloading media through MTProto fallback destination=%s bot_reference=%s message_id=%s", destination, bot_reference, message.message_id)
        if self._telegram_runtime is not None:
            client_context = self._telegram_runtime.client_context(TelegramRuntimeRole.MEDIA_FALLBACK)
        else:
            client_context = connected_user_client(self._settings)
        async with client_context as client:
            resolved_download = await _download_via_resolved_bot_file_id(
                client,
                downloadable,
                destination,
                progress_reporter=progress_reporter,
                progress_callback=progress_callback,
            )
            if resolved_download is not None:
                logger.info("MTProto media download completed from resolved Bot API file_id destination=%s", resolved_download)
                return resolved_download

            await _emit_progress(progress_reporter, "⏳ Ищу исходное видео в диалоге с ботом...")
            if self._telegram_runtime is not None:
                peer = await self._telegram_runtime.resolve_input_peer(
                    TelegramRuntimeRole.MEDIA_FALLBACK,
                    client,
                    bot_reference,
                )
            else:
                peer = await _resolve_peer_via_client(client, bot_reference)
            source_message = await _resolve_source_message(client, peer, message, downloadable)
            await _emit_progress(progress_reporter, "⏳ Нашла исходное видео. Скачиваю через личный аккаунт...")
            downloaded_path = await client.download_media(
                source_message,
                file=str(destination),
                progress_callback=progress_callback,
            )

        if downloaded_path is None:
            raise MediaIngressError("Telegram не вернул путь к скачанному медиафайлу при MTProto-скачивании.")

        resolved = Path(downloaded_path)
        logger.info("MTProto media download completed destination=%s", resolved)
        return resolved
