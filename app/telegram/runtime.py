from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from enum import StrEnum
import time
from typing import TYPE_CHECKING

from app.config.settings import Settings, load_settings
from app.telegram.client import connect_runtime_client, connected_user_client
from app.telegram.health import TelegramConnectivityChannel, get_connectivity_monitor

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from telethon import TelegramClient


class TelegramRuntimeRole(StrEnum):
    PUBLISHER = "publisher"
    MEDIA_FALLBACK = "media-fallback"


class TelegramRuntime:
    """Owns role-scoped connected Telethon clients for unified-runtime use."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or load_settings()
        self._clients: dict[TelegramRuntimeRole, TelegramClient] = {}
        self._locks: dict[TelegramRuntimeRole, asyncio.Lock] = {}
        self._peer_cache: dict[tuple[TelegramRuntimeRole, str], object] = {}
        self._last_probe_monotonic: dict[TelegramRuntimeRole, float] = {}
        self._connectivity_monitor = get_connectivity_monitor()

    async def start(self, role: TelegramRuntimeRole = TelegramRuntimeRole.PUBLISHER):
        return await self.start_role(role)

    async def start_role(self, role: TelegramRuntimeRole = TelegramRuntimeRole.PUBLISHER) -> TelegramClient:
        lock = self._lock_for(role)
        async with lock:
            client = await self._with_timeout(
                self._ensure_client_locked(role),
                label=f"start-role:{role.value}",
                timeout_seconds=self._settings.runtime.mtproto_connect_timeout_seconds,
            )
            self._record_mtproto_success(detail=f"role-started:{role.value}")
            self._last_probe_monotonic[role] = time.monotonic()
            return client

    async def stop_role(self, role: TelegramRuntimeRole = TelegramRuntimeRole.PUBLISHER) -> None:
        lock = self._lock_for(role)
        async with lock:
            await self._dispose_client_locked(role, reason="stop")

    async def invalidate_role(self, role: TelegramRuntimeRole = TelegramRuntimeRole.PUBLISHER, *, reason: str) -> None:
        lock = self._lock_for(role)
        async with lock:
            await self._dispose_client_locked(role, reason=reason)

    async def ensure_role_ready(
        self,
        role: TelegramRuntimeRole = TelegramRuntimeRole.PUBLISHER,
        *,
        allow_reconnect: bool = True,
    ) -> bool:
        lock = self._lock_for(role)
        async with lock:
            existing = self._clients.get(role)
            probe_interval = self._settings.runtime.mtproto_probe_interval_seconds
            should_probe = (
                existing is not None
                and existing.is_connected()
                and (
                    self._connectivity_monitor.is_degraded(TelegramConnectivityChannel.MTPROTO)
                    or (time.monotonic() - self._last_probe_monotonic.get(role, 0.0)) >= probe_interval
                )
            )

            if should_probe:
                try:
                    await self._with_timeout(
                        existing.get_me(),
                        label=f"health-probe:{role.value}",
                        timeout_seconds=self._settings.runtime.mtproto_probe_timeout_seconds,
                    )
                except Exception as exc:
                    logger.warning("Telegram runtime health probe failed role=%s error=%s", role.value, exc)
                    self._record_mtproto_failure(detail=f"{type(exc).__name__}: {exc}")
                    await self._dispose_client_locked(role, reason="health-probe-failed")
                    if not allow_reconnect:
                        return False
                else:
                    self._record_mtproto_success(detail=f"probe-ok:{role.value}")
                    self._last_probe_monotonic[role] = time.monotonic()
                    return True
            elif existing is not None and existing.is_connected():
                return True

            try:
                client = await self._with_timeout(
                    self._ensure_client_locked(role),
                    label=f"ensure-client:{role.value}",
                    timeout_seconds=self._settings.runtime.mtproto_connect_timeout_seconds,
                )
            except Exception as exc:
                logger.warning("Telegram runtime unavailable role=%s error=%s", role.value, exc)
                self._record_mtproto_failure(detail=f"{type(exc).__name__}: {exc}")
                await self._dispose_client_locked(role, reason="ensure-client-failed")
                return False

            self._record_mtproto_success(detail=f"connected:{role.value}")
            self._last_probe_monotonic[role] = time.monotonic()
            return client.is_connected()

    async def stop(self) -> None:
        for role in list(self._clients):
            await self.stop_role(role)

    @asynccontextmanager
    async def client_context(self, role: TelegramRuntimeRole = TelegramRuntimeRole.PUBLISHER):
        lock = self._lock_for(role)
        async with lock:
            try:
                client = await self._with_timeout(
                    self._ensure_client_locked(role),
                    label=f"client-context:{role.value}",
                    timeout_seconds=self._settings.runtime.mtproto_connect_timeout_seconds,
                )
            except Exception as exc:
                self._record_mtproto_failure(detail=f"{type(exc).__name__}: {exc}")
                await self._dispose_client_locked(role, reason="client-context-failed")
                raise
            self._record_mtproto_success(detail=f"context-ready:{role.value}")
            self._last_probe_monotonic[role] = time.monotonic()
            logger.info("Using Telegram runtime client role=%s for MTProto work", role.value)
            yield client

    async def resolve_input_peer(self, role: TelegramRuntimeRole, client: TelegramClient, peer_reference: object) -> object:
        cache_key = (role, str(peer_reference))
        cached = self._peer_cache.get(cache_key)
        if cached is not None:
            logger.debug("Reusing cached Telethon peer role=%s peer_reference=%s", role.value, peer_reference)
            return cached

        logger.info("Resolving Telethon peer role=%s peer_reference=%s", role.value, peer_reference)
        resolved = await client.get_input_entity(peer_reference)
        self._peer_cache[cache_key] = resolved
        return resolved

    def _lock_for(self, role: TelegramRuntimeRole) -> asyncio.Lock:
        return self._locks.setdefault(role, asyncio.Lock())

    async def _with_timeout(self, awaitable, *, label: str, timeout_seconds: float):
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
        except TimeoutError as exc:
            raise TimeoutError(f"{label} timed out after {timeout_seconds:.1f}s") from exc

    async def _ensure_client_locked(self, role: TelegramRuntimeRole) -> TelegramClient:
        existing = self._clients.get(role)
        if existing is not None and existing.is_connected():
            logger.info("Reusing connected Telegram runtime client role=%s", role.value)
            return existing

        if existing is not None:
            logger.warning("Existing Telegram runtime client is disconnected; rebuilding role=%s", role.value)
            await self._dispose_client_locked(role, reason="detected-disconnected-client")

        logger.info("Starting Telegram runtime client role=%s", role.value)
        client = await connect_runtime_client(self._settings)
        self._clients[role] = client
        return client

    async def _dispose_client_locked(self, role: TelegramRuntimeRole, *, reason: str) -> None:
        client = self._clients.pop(role, None)
        self._last_probe_monotonic.pop(role, None)
        stale_keys = [key for key in self._peer_cache if key[0] == role]
        for key in stale_keys:
            self._peer_cache.pop(key, None)

        if client is None:
            return

        if client.is_connected():
            logger.warning("Stopping Telegram runtime client role=%s reason=%s", role.value, reason)
            with suppress(Exception):
                await client.disconnect()

    def _record_mtproto_failure(self, *, detail: str) -> None:
        self._connectivity_monitor.report_failure(
            TelegramConnectivityChannel.MTPROTO,
            detail,
            current_logger=logger,
        )

    def _record_mtproto_success(self, *, detail: str) -> None:
        self._connectivity_monitor.report_success(
            TelegramConnectivityChannel.MTPROTO,
            detail=detail,
            current_logger=logger,
        )


@asynccontextmanager
async def open_runtime_client(
    settings: Settings | None = None,
    *,
    role: TelegramRuntimeRole = TelegramRuntimeRole.PUBLISHER,
):
    current_settings = settings or load_settings()
    logger.info("Opening dedicated Telegram runtime client context role=%s", role.value)
    async with connected_user_client(current_settings) as client:
        yield client
