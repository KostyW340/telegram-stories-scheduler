from __future__ import annotations

import asyncio
import importlib
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from typing import TYPE_CHECKING

from app.config.process_lock import RuntimeModeLockError, acquire_runtime_mode_lock
from app.config.settings import Settings, load_settings
from app.telegram.bootstrap import (
    TelegramBootstrapResult,
    TelegramBootstrapStage,
    TelegramOperatorAction,
    TelegramSessionState,
    TelegramSessionUnauthorizedError,
    TelegramSessionRevokedError,
    log_telegram_bootstrap_result,
    raise_for_bootstrap_result,
)

logger = logging.getLogger(__name__)

RUNTIME_SESSION_REFRESH_MODE = "telegram-runtime-session"
RUNTIME_SESSION_WAIT_ATTEMPTS = 40
RUNTIME_SESSION_WAIT_DELAY_SECONDS = 0.25

if TYPE_CHECKING:
    from telethon import TelegramClient


def _validate_telethon_libssl_backend(libssl_module: object) -> tuple[bool, tuple[str, ...]]:
    handle = getattr(libssl_module, "_libssl", None)
    if handle is None:
        return False, ()

    required_symbols = ("AES_set_decrypt_key", "AES_set_encrypt_key", "AES_ige_encrypt")
    missing_symbols: list[str] = []
    for symbol in required_symbols:
        try:
            getattr(handle, symbol)
        except AttributeError:
            missing_symbols.append(symbol)

    return not missing_symbols, tuple(missing_symbols)


def _disable_telethon_libssl_backend(libssl_module: object) -> None:
    setattr(libssl_module, "_libssl", None)
    setattr(libssl_module, "encrypt_ige", None)
    setattr(libssl_module, "decrypt_ige", None)


def prepare_telethon_crypto_backend(current_logger: logging.Logger | None = None) -> str:
    active_logger = current_logger or logger

    from telethon.crypto import aes as telethon_aes
    from telethon.crypto import libssl as telethon_libssl

    if telethon_aes.cryptg is None:
        try:
            telethon_aes.cryptg = importlib.import_module("cryptg")
        except ImportError:
            active_logger.debug("cryptg import is unavailable in the current runtime")
        except Exception as exc:
            active_logger.warning("cryptg is present but could not be imported safely: %s", exc)
        if telethon_aes.cryptg is not None:
            active_logger.info("Telethon crypto backend selected=cryptg")
            return "cryptg"

    if telethon_aes.cryptg is not None:
        active_logger.info("Telethon crypto backend selected=cryptg")
        return "cryptg"

    libssl_valid, missing_symbols = _validate_telethon_libssl_backend(telethon_libssl)
    if libssl_valid and telethon_libssl.encrypt_ige and telethon_libssl.decrypt_ige:
        active_logger.info("Telethon crypto backend selected=libssl")
        return "libssl"

    if missing_symbols:
        active_logger.warning(
            "Disabling Telethon libssl backend because required AES symbols are missing: %s",
            missing_symbols,
        )
        _disable_telethon_libssl_backend(telethon_libssl)

    active_logger.warning("Telethon crypto backend selected=python-fallback")
    return "python-fallback"


def _build_mtproto_proxy_kwargs(settings: Settings) -> dict[str, object]:
    host = settings.telegram.mtproto_proxy_host
    port = settings.telegram.mtproto_proxy_port
    if not host or port is None:
        return {}

    from telethon import connection

    secret = settings.telegram.mtproto_proxy_secret or ("0" * 32)
    logger.info("Using Telethon MTProto proxy host=%s port=%s", host, port)
    return {
        "connection": connection.ConnectionTcpMTProxyRandomizedIntermediate,
        "proxy": (host, port, secret),
    }


def _build_telegram_client(settings: Settings, session_source) -> TelegramClient:
    from telethon import TelegramClient

    api_id, api_hash = settings.telegram.require_api_credentials()
    backend = prepare_telethon_crypto_backend(logger)
    proxy_kwargs = _build_mtproto_proxy_kwargs(settings)
    logger.debug(
        "Creating Telethon client session_source=%s device_model=%s system_version=%s app_version=%s lang_code=%s crypto_backend=%s",
        session_source,
        settings.telegram.device_model,
        settings.telegram.system_version,
        settings.telegram.app_version,
        settings.telegram.lang_code,
        backend,
    )
    return TelegramClient(
        session_source,
        api_id,
        api_hash,
        device_model=settings.telegram.device_model,
        system_version=settings.telegram.system_version,
        app_version=settings.telegram.app_version,
        lang_code=settings.telegram.lang_code,
        system_lang_code=settings.telegram.system_lang_code,
        request_retries=3,
        connection_retries=3,
        retry_delay=2,
        auto_reconnect=True,
        raise_last_call_error=True,
        **proxy_kwargs,
    )


def create_authorization_client(settings: Settings | None = None) -> TelegramClient:
    current_settings = settings or load_settings()
    return _build_telegram_client(current_settings, current_settings.session_file)


def create_telegram_client(settings: Settings | None = None) -> TelegramClient:
    return create_authorization_client(settings)


def _read_runtime_session_string(settings: Settings) -> str | None:
    path = settings.runtime_session_string_file
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        logger.warning("Runtime session string file exists but is empty path=%s", path)
        return None
    return value


def _write_runtime_session_string(settings: Settings, session_string: str) -> None:
    path = settings.runtime_session_string_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session_string.strip(), encoding="utf-8")
    logger.info("Stored runtime session string at %s", path)


def _session_artifact_missing_result(
    *,
    stage: TelegramBootstrapStage,
    detail: str,
) -> TelegramBootstrapResult:
    return TelegramBootstrapResult(
        state=TelegramSessionState.MISSING,
        operator_action=TelegramOperatorAction.RUN_AUTH,
        stage=stage,
        detail=detail,
    )


def _network_unavailable_result(
    *,
    stage: TelegramBootstrapStage,
    detail: str,
    reused_runtime_session: bool = False,
) -> TelegramBootstrapResult:
    return TelegramBootstrapResult(
        state=TelegramSessionState.NETWORK_UNAVAILABLE,
        operator_action=TelegramOperatorAction.RESTORE_NETWORK,
        stage=stage,
        detail=detail,
        reused_runtime_session=reused_runtime_session,
    )


def _invalid_session_result(
    *,
    stage: TelegramBootstrapStage,
    detail: str,
) -> TelegramBootstrapResult:
    return TelegramBootstrapResult(
        state=TelegramSessionState.INVALID,
        operator_action=TelegramOperatorAction.RUN_AUTH,
        stage=stage,
        detail=detail,
    )


def _unauthorized_session_result(
    *,
    stage: TelegramBootstrapStage,
    detail: str,
) -> TelegramBootstrapResult:
    return TelegramBootstrapResult(
        state=TelegramSessionState.UNAUTHORIZED,
        operator_action=TelegramOperatorAction.RUN_AUTH,
        stage=stage,
        detail=detail,
    )


def _revoked_session_result(
    *,
    stage: TelegramBootstrapStage,
    detail: str,
) -> TelegramBootstrapResult:
    return TelegramBootstrapResult(
        state=TelegramSessionState.REVOKED,
        operator_action=TelegramOperatorAction.RUN_AUTH,
        stage=stage,
        detail=detail,
    )


def _authorized_session_result(
    *,
    stage: TelegramBootstrapStage,
    detail: str,
    authorized_user_id: int | None,
    reused_runtime_session: bool = False,
) -> TelegramBootstrapResult:
    return TelegramBootstrapResult(
        state=TelegramSessionState.AUTHORIZED,
        operator_action=TelegramOperatorAction.NONE,
        stage=stage,
        detail=detail,
        authorized_user_id=authorized_user_id,
        reused_runtime_session=reused_runtime_session,
    )


async def _resolve_authorized_identity(client, *, stage: TelegramBootstrapStage) -> TelegramBootstrapResult:
    from telethon import errors as telethon_errors

    try:
        await client.connect()
    except (telethon_errors.AuthKeyUnregisteredError, telethon_errors.SessionRevokedError) as exc:
        return _revoked_session_result(stage=stage, detail=f"{exc.__class__.__name__}: {exc}")
    except (ConnectionError, TimeoutError, OSError) as exc:
        return _network_unavailable_result(stage=stage, detail=f"{exc.__class__.__name__}: {exc}")
    except Exception as exc:
        return _invalid_session_result(stage=stage, detail=f"{exc.__class__.__name__}: {exc}")

    try:
        if hasattr(client, "is_user_authorized"):
            is_authorized = await client.is_user_authorized()
        else:
            is_authorized = await client.get_me() is not None
    except (telethon_errors.AuthKeyUnregisteredError, telethon_errors.SessionRevokedError) as exc:
        return _revoked_session_result(stage=stage, detail=f"{exc.__class__.__name__}: {exc}")
    except (ConnectionError, TimeoutError, OSError) as exc:
        return _network_unavailable_result(stage=stage, detail=f"{exc.__class__.__name__}: {exc}")
    except Exception as exc:
        return _invalid_session_result(stage=stage, detail=f"{exc.__class__.__name__}: {exc}")

    if not is_authorized:
        return _unauthorized_session_result(
            stage=stage,
            detail="Telethon connected successfully but did not confirm an authorized user session.",
        )

    authorized_user_id: int | None = None
    if hasattr(client, "get_me"):
        try:
            me = await client.get_me()
        except Exception as exc:  # pragma: no cover
            logger.warning("Authorized Telethon session identity lookup failed stage=%s error=%s", stage.value, exc)
        else:
            authorized_user_id = getattr(me, "id", None)

    return _authorized_session_result(
        stage=stage,
        detail="Authorized Telegram session confirmed.",
        authorized_user_id=authorized_user_id,
    )


async def validate_file_session(settings: Settings | None = None) -> TelegramBootstrapResult:
    current_settings = settings or load_settings()
    if not current_settings.session_file.exists():
        result = _session_artifact_missing_result(
            stage=TelegramBootstrapStage.FILE_SESSION_VALIDATION,
            detail=f"Session file is missing: {current_settings.session_file}",
        )
        log_telegram_bootstrap_result(logger, prefix="Telegram file-session validation result", result=result)
        return result

    logger.info("Validating file-backed Telethon session path=%s", current_settings.session_file)
    client = create_authorization_client(current_settings)
    try:
        result = await _resolve_authorized_identity(client, stage=TelegramBootstrapStage.FILE_SESSION_VALIDATION)
        log_telegram_bootstrap_result(logger, prefix="Telegram file-session validation result", result=result)
        return result
    finally:
        with suppress(Exception):
            await client.disconnect()


async def validate_runtime_session_artifact(settings: Settings | None = None) -> TelegramBootstrapResult:
    current_settings = settings or load_settings()
    runtime_session_path = current_settings.runtime_session_string_file
    session_string = _read_runtime_session_string(current_settings)
    if session_string is None and runtime_session_path.exists():
        result = _invalid_session_result(
            stage=TelegramBootstrapStage.RUNTIME_SESSION_CONNECT,
            detail=f"Runtime session string is empty or unreadable: {runtime_session_path}",
        )
        log_telegram_bootstrap_result(logger, prefix="Telegram runtime-session validation result", result=result)
        return result
    if not session_string:
        result = _session_artifact_missing_result(
            stage=TelegramBootstrapStage.RUNTIME_SESSION_CONNECT,
            detail=f"Runtime session string is missing: {runtime_session_path}",
        )
        log_telegram_bootstrap_result(logger, prefix="Telegram runtime-session validation result", result=result)
        return result

    from telethon.sessions import StringSession

    logger.info("Validating runtime StringSession path=%s", runtime_session_path)
    client = _build_telegram_client(current_settings, StringSession(session_string))
    try:
        result = await _resolve_authorized_identity(client, stage=TelegramBootstrapStage.RUNTIME_SESSION_CONNECT)
    finally:
        with suppress(Exception):
            await client.disconnect()

    if result.state is TelegramSessionState.AUTHORIZED:
        result = replace(result, reused_runtime_session=True)
        log_telegram_bootstrap_result(logger, prefix="Telegram runtime-session validation result", result=result)
        return result
    if result.state is TelegramSessionState.NETWORK_UNAVAILABLE:
        result = replace(result, reused_runtime_session=True)
        log_telegram_bootstrap_result(logger, prefix="Telegram runtime-session validation result", result=result)
        return result
    log_telegram_bootstrap_result(logger, prefix="Telegram runtime-session validation result", result=result)
    return result


async def assess_existing_session(settings: Settings | None = None) -> TelegramBootstrapResult:
    current_settings = settings or load_settings()
    if current_settings.session_file.exists():
        result = await validate_file_session(current_settings)
        if (
            result.state is TelegramSessionState.NETWORK_UNAVAILABLE
            and _read_runtime_session_string(current_settings) is not None
        ):
            result = replace(result, reused_runtime_session=True)
            log_telegram_bootstrap_result(
                logger,
                prefix="Telegram bootstrap assessment preserved runtime-session fallback",
                result=result,
            )
        return result
    if current_settings.runtime_session_string_file.exists():
        return await validate_runtime_session_artifact(current_settings)
    result = _session_artifact_missing_result(
        stage=TelegramBootstrapStage.FILE_SESSION_VALIDATION,
        detail="No reusable Telegram session artifacts were found.",
    )
    log_telegram_bootstrap_result(logger, prefix="Telegram bootstrap assessment result", result=result)
    return result


async def save_runtime_session_string_from_client(
    settings: Settings,
    client: TelegramClient,
    *,
    user_id: int | None = None,
) -> str:
    from telethon.sessions import StringSession

    session_string = StringSession.save(client.session)
    _write_runtime_session_string(settings, session_string)
    if user_id is None:
        logger.info("Runtime session string refreshed from an already connected Telethon client")
    else:
        logger.info("Runtime session string refreshed for user_id=%s from an already connected Telethon client", user_id)
    return session_string


async def _wait_for_runtime_session_string(settings: Settings) -> str:
    logger.info(
        "Waiting for runtime session string to become available path=%s",
        settings.runtime_session_string_file,
    )
    for _attempt in range(RUNTIME_SESSION_WAIT_ATTEMPTS):
        session_string = _read_runtime_session_string(settings)
        if session_string:
            logger.info("Observed runtime session string after waiting path=%s", settings.runtime_session_string_file)
            return session_string
        await asyncio.sleep(RUNTIME_SESSION_WAIT_DELAY_SECONDS)
    raise RuntimeError("Telegram runtime session preparation is still in progress. Подождите и повторите попытку.")


async def export_runtime_session_string(settings: Settings | None = None, *, force: bool = False) -> str:
    current_settings = settings or load_settings()
    if not force:
        existing = _read_runtime_session_string(current_settings)
        if existing:
            logger.info("Reusing existing runtime session string path=%s", current_settings.runtime_session_string_file)
            return existing

    try:
        with acquire_runtime_mode_lock(RUNTIME_SESSION_REFRESH_MODE, current_settings.paths.data_dir, logger):
            if not force:
                existing = _read_runtime_session_string(current_settings)
                if existing:
                    logger.info(
                        "Runtime session string became available before refresh path=%s",
                        current_settings.runtime_session_string_file,
                    )
                    return existing

            client = create_authorization_client(current_settings)
            # Telethon string sessions let runtime clients avoid opening a second
            # SQLite-backed `account.session` connection, which is what caused the
            # reproduced `database is locked` failure during large-video fallback.
            logger.info(
                "Refreshing runtime session string from file-backed Telethon session source=%s target=%s",
                current_settings.session_file,
                current_settings.runtime_session_string_file,
            )
            try:
                result = await _resolve_authorized_identity(client, stage=TelegramBootstrapStage.RUNTIME_SESSION_REFRESH)
                log_telegram_bootstrap_result(logger, prefix="Telegram runtime-session refresh validation result", result=result)
                if result.state is not TelegramSessionState.AUTHORIZED:
                    raise_for_bootstrap_result(result)
                return await save_runtime_session_string_from_client(
                    current_settings,
                    client,
                    user_id=result.authorized_user_id,
                )
            finally:
                with suppress(Exception):
                    await client.disconnect()
    except RuntimeModeLockError:
        logger.warning("Another process is already refreshing the runtime session string")
        return await _wait_for_runtime_session_string(current_settings)


async def ensure_runtime_session_string(settings: Settings | None = None) -> str:
    current_settings = settings or load_settings()
    session_string = _read_runtime_session_string(current_settings)
    if session_string:
        return session_string
    return await export_runtime_session_string(current_settings)


async def create_runtime_client(settings: Settings | None = None) -> TelegramClient:
    current_settings = settings or load_settings()
    session_string = await ensure_runtime_session_string(current_settings)
    from telethon.sessions import StringSession

    logger.info(
        "Creating lock-safe Telethon runtime client using StringSession path=%s",
        current_settings.runtime_session_string_file,
    )
    return _build_telegram_client(current_settings, StringSession(session_string))


async def connect_runtime_client(
    settings: Settings | None = None,
    *,
    allow_session_refresh: bool = True,
) -> TelegramClient:
    current_settings = settings or load_settings()
    client = await create_runtime_client(current_settings)
    from telethon import errors as telethon_errors

    try:
        logger.info("Connecting Telethon runtime client")
        result = await _resolve_authorized_identity(client, stage=TelegramBootstrapStage.RUNTIME_SESSION_CONNECT)
        log_telegram_bootstrap_result(logger, prefix="Telegram runtime-session connect result", result=result)
        if result.state is not TelegramSessionState.AUTHORIZED:
            raise_for_bootstrap_result(result)
        if result.authorized_user_id is None:
            logger.info("Connected Telethon runtime client for an authorized session")
        else:
            logger.info("Connected Telethon runtime client for user_id=%s", result.authorized_user_id)
        return client
    except (TelegramSessionUnauthorizedError, TelegramSessionRevokedError):
        with suppress(Exception):
            await client.disconnect()
        if allow_session_refresh:
            logger.warning("Runtime StringSession is invalid or unauthorized; rebuilding it from account.session")
            await export_runtime_session_string(current_settings, force=True)
            return await connect_runtime_client(current_settings, allow_session_refresh=False)
        raise
    except Exception:
        with suppress(Exception):
            await client.disconnect()
        raise


@asynccontextmanager
async def connected_user_client(settings: Settings | None = None):
    current_settings = settings or load_settings()
    client = await connect_runtime_client(current_settings)
    try:
        yield client
    finally:
        if client.is_connected():
            logger.info("Disconnecting Telethon runtime client")
            await client.disconnect()
