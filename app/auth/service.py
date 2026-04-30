from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.config.settings import Settings
from app.telegram.bootstrap import (
    TelegramBootstrapResult,
    TelegramBootstrapStage,
    TelegramNetworkUnavailableError,
    TelegramOperatorAction,
    TelegramSessionRevokedError,
    TelegramSessionState,
    detect_synced_runtime_paths,
    format_synced_runtime_warning,
)
from app.telegram.client import create_authorization_client, export_runtime_session_string, save_runtime_session_string_from_client

logger = logging.getLogger(__name__)

# Backwards-compatible alias for older tests and monkeypatch points.
create_telegram_client = create_authorization_client
AUTH_RESET_BACKUP_SUFFIX = ".preauth.bak"

PromptFn = Callable[[], Awaitable[str] | str]


@dataclass(slots=True)
class AuthPrompts:
    phone: PromptFn
    code: PromptFn
    password: PromptFn


@dataclass(slots=True, frozen=True)
class AuthResult:
    session_file: Path
    authorized_user_id: int
    username: str | None
    phone: str | None
    reused_existing_session: bool


def _mask_phone(phone: str | None) -> str:
    if not phone:
        return "<unset>"
    if len(phone) <= 4:
        return "*" * len(phone)
    return f"{phone[:2]}***{phone[-2:]}"


async def _resolve_prompt(label: str, prompt: PromptFn) -> str:
    logger.debug("Resolving prompt value for %s", label)
    value = prompt()
    if asyncio.iscoroutine(value):
        value = await value
    return str(value).strip()


def _backup_auth_artifact_path(path: Path) -> Path:
    return path.with_name(f"{path.name}{AUTH_RESET_BACKUP_SUFFIX}")


def _park_existing_auth_artifacts(settings: Settings) -> dict[Path, Path]:
    parked: dict[Path, Path] = {}
    for artifact_path in (settings.session_file, settings.runtime_session_string_file):
        if not artifact_path.exists():
            continue
        backup_path = _backup_auth_artifact_path(artifact_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            backup_path.unlink()
        artifact_path.replace(backup_path)
        parked[artifact_path] = backup_path
        logger.info("Parked existing Telegram auth artifact original=%s backup=%s", artifact_path, backup_path)
    return parked


def _restore_parked_auth_artifacts(parked_artifacts: dict[Path, Path]) -> None:
    for artifact_path, backup_path in parked_artifacts.items():
        with contextlib.suppress(FileNotFoundError):
            artifact_path.unlink()
        if backup_path.exists():
            backup_path.replace(artifact_path)
            logger.info("Restored parked Telegram auth artifact original=%s backup=%s", artifact_path, backup_path)


def _discard_parked_auth_artifacts(parked_artifacts: dict[Path, Path]) -> None:
    for artifact_path, backup_path in parked_artifacts.items():
        if backup_path.exists():
            backup_path.unlink()
            logger.info("Discarded parked Telegram auth backup original=%s backup=%s", artifact_path, backup_path)


async def _complete_password_sign_in(
    client,
    prompts: AuthPrompts,
    telethon_errors,
):
    logger.info("Telegram accepted the login code and requires the cloud password")
    print("Код подтверждения принят. Теперь введите облачный пароль Telegram.")

    while True:
        password = await _resolve_prompt("password", prompts.password)
        if not password:
            logger.warning("Empty 2FA password entered")
            print("Пустой облачный пароль. Повторите ввод. Код повторно вводить не нужно.")
            continue

        try:
            logger.info("Attempting to complete sign-in with the provided cloud password")
            return await client.sign_in(password=password)
        except telethon_errors.PasswordHashInvalidError:
            logger.warning("Invalid 2FA password entered; retrying password step")
            print("Неверный облачный пароль. Повторите ввод. Код повторно вводить не нужно.")


async def authorize_interactively(
    settings: Settings,
    prompts: AuthPrompts,
    *,
    force_reauth: bool = False,
) -> AuthResult:
    from telethon import errors as telethon_errors

    logger.info("Starting local Telegram auth utility for session %s", settings.session_file)
    synced_paths = detect_synced_runtime_paths(
        (
            settings.session_file,
            settings.runtime_session_string_file,
            settings.paths.data_dir,
        )
    )
    if synced_paths:
        warning = format_synced_runtime_warning(synced_paths)
        logger.warning("Interactive auth detected synced runtime paths paths=%s", [str(path) for path in synced_paths])
        if warning:
            print(warning)

    parked_artifacts: dict[Path, Path] = {}
    if force_reauth:
        parked_artifacts = _park_existing_auth_artifacts(settings)
        if parked_artifacts:
            print(
                "Запущена полная переавторизация Telegram. Старые session-файлы временно убраны "
                "и будут возвращены обратно, если новый вход не завершится успешно."
            )
            logger.info(
                "Interactive auth is forcing a fresh Telegram reauthorization parked_artifacts=%s",
                {str(original): str(backup) for original, backup in parked_artifacts.items()},
            )

    try:
        restarted_with_fresh_session = False
        while True:
            # Keep the legacy monkeypatch point stable for tests while routing the
            # actual implementation through the authorization-only client factory.
            client = create_telegram_client(settings)
            try:
                try:
                    await client.connect()
                except (ConnectionError, TimeoutError, OSError) as exc:
                    logger.error("Telegram auth bootstrap could not connect to the network")
                    raise TelegramNetworkUnavailableError(
                        TelegramBootstrapResult(
                            state=TelegramSessionState.NETWORK_UNAVAILABLE,
                            operator_action=TelegramOperatorAction.RESTORE_NETWORK,
                            stage=TelegramBootstrapStage.INTERACTIVE_AUTH_BOOTSTRAP,
                            detail=f"{exc.__class__.__name__}: {exc}",
                        )
                    ) from exc
                except (telethon_errors.AuthKeyUnregisteredError, telethon_errors.SessionRevokedError) as exc:
                    if restarted_with_fresh_session:
                        logger.error("Existing session file is invalid or revoked: %s", settings.session_file)
                        raise TelegramSessionRevokedError(
                            TelegramBootstrapResult(
                                state=TelegramSessionState.REVOKED,
                                operator_action=TelegramOperatorAction.RUN_AUTH,
                                stage=TelegramBootstrapStage.INTERACTIVE_AUTH_BOOTSTRAP,
                                detail=f"{exc.__class__.__name__}: {exc}",
                            )
                        ) from exc
                    logger.warning("Session file is invalid or revoked; removing stale session artifacts and retrying auth bootstrap")
                    with contextlib.suppress(FileNotFoundError):
                        settings.session_file.unlink()
                    with contextlib.suppress(FileNotFoundError):
                        settings.runtime_session_string_file.unlink()
                    logger.info(
                        "Removed stale Telegram auth artifacts session=%s runtime=%s before retrying interactive auth bootstrap",
                        settings.session_file,
                        settings.runtime_session_string_file,
                    )
                    restarted_with_fresh_session = True
                    continue

                logger.info("Telethon client connected")

                if hasattr(client, "is_user_authorized"):
                    is_authorized = await client.is_user_authorized()
                else:
                    is_authorized = await client.get_me() is not None

                if is_authorized and not force_reauth:
                    me = await client.get_me()
                    logger.info("Existing authorized session detected for user_id=%s", me.id)
                    await save_runtime_session_string_from_client(settings, client, user_id=me.id)
                    if parked_artifacts:
                        _discard_parked_auth_artifacts(parked_artifacts)
                        parked_artifacts.clear()
                    return AuthResult(
                        session_file=settings.session_file,
                        authorized_user_id=me.id,
                        username=me.username,
                        phone=me.phone,
                        reused_existing_session=True,
                    )

                phone = settings.telegram.phone_number or await _resolve_prompt("phone", prompts.phone)
                if not phone:
                    raise RuntimeError("Phone number is required to authorize the MTProto session")

                logger.info("Requesting login code for phone=%s", _mask_phone(phone))
                await client.send_code_request(phone)

                while True:
                    try:
                        logger.info("Waiting for login code input")
                        code = await _resolve_prompt("code", prompts.code)
                        if not code:
                            logger.info("Empty login code entered; requesting a resend")
                            await client.send_code_request(phone)
                            continue

                        logger.info("Attempting to sign in with the provided login code")
                        me = await client.sign_in(phone=phone, code=code)
                        break
                    except telethon_errors.SessionPasswordNeededError:
                        logger.info("Two-factor authentication is enabled; switching to cloud-password step")
                        me = await _complete_password_sign_in(client, prompts, telethon_errors)
                        break
                    except telethon_errors.PhoneCodeInvalidError:
                        logger.warning("Invalid login code entered; retrying")
                    except telethon_errors.PhoneCodeExpiredError:
                        logger.warning("Login code expired; requesting a new code")
                        print("Старый код больше недействителен. Дождитесь нового кода и введите только его.")
                        await client.send_code_request(phone)
                    except telethon_errors.SendCodeUnavailableError as exc:
                        logger.error("Telegram temporarily refuses to send another login code")
                        raise RuntimeError(
                            "Telegram временно не может отправить новый код. Подождите немного и начните авторизацию заново."
                        ) from exc
                    except telethon_errors.PhoneNumberInvalidError as exc:
                        logger.error("Invalid phone number was provided")
                        raise RuntimeError("Invalid phone number for Telegram authorization") from exc
                    except telethon_errors.FloodWaitError as exc:
                        wait_seconds = int(exc.seconds)
                        logger.warning("Flood wait during auth; sleeping for %s seconds", wait_seconds)
                        await asyncio.sleep(wait_seconds)
                    except telethon_errors.ApiIdInvalidError as exc:
                        logger.error("API_ID/API_HASH pair is invalid")
                        raise RuntimeError("Invalid API_ID/API_HASH pair") from exc
                    except (telethon_errors.AuthKeyUnregisteredError, telethon_errors.SessionRevokedError) as exc:
                        logger.warning("Session file became invalid or revoked during auth flow")
                        raise TelegramSessionRevokedError(
                            TelegramBootstrapResult(
                                state=TelegramSessionState.REVOKED,
                                operator_action=TelegramOperatorAction.RUN_AUTH,
                                stage=TelegramBootstrapStage.INTERACTIVE_AUTH_BOOTSTRAP,
                                detail=f"{exc.__class__.__name__}: {exc}",
                            )
                        ) from exc

                if me is None:
                    raise RuntimeError("Telegram authorization did not complete successfully")

                logger.info("Telegram session authorized for user_id=%s", me.id)
                await save_runtime_session_string_from_client(settings, client, user_id=me.id)
                if settings.session_file.exists():
                    logger.info("Session file is available at %s", settings.session_file)
                else:
                    logger.warning("Authorization succeeded but the session file is not visible yet at %s", settings.session_file)

                if parked_artifacts:
                    _discard_parked_auth_artifacts(parked_artifacts)
                    parked_artifacts.clear()
                return AuthResult(
                    session_file=settings.session_file,
                    authorized_user_id=me.id,
                    username=me.username,
                    phone=me.phone,
                    reused_existing_session=False,
                )
            except telethon_errors.RPCError as exc:
                logger.error("Unhandled Telegram RPC error during authorization: %s", exc.__class__.__name__)
                raise RuntimeError(f"Telegram authorization failed: {exc.__class__.__name__}") from exc
            finally:
                logger.info("Disconnecting Telethon client after auth flow")
                with contextlib.suppress(Exception):
                    await client.disconnect()
    except Exception:
        if parked_artifacts:
            _restore_parked_auth_artifacts(parked_artifacts)
        raise
