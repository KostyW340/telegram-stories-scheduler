from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import socket
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

logger = logging.getLogger(__name__)

_REGISTERED_DLL_DIRS: dict[str, object] = {}
_ORIGINAL_SOCKETPAIR = getattr(socket, "socketpair", None)
_WINDOWS_SOCKETPAIR_SHIM_APPLIED = False

T = TypeVar("T")


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _iter_runtime_library_dirs() -> list[Path]:
    candidates: list[Path] = []

    extra_dirs = os.getenv("TGSTORIES_EXTRA_DLL_DIRS", "")
    for token in extra_dirs.split(os.pathsep):
        normalized = token.strip()
        if normalized:
            candidates.append(Path(normalized))

    runtime_root = Path(sys.executable).resolve().parent
    candidates.append(runtime_root)

    base_prefix = getattr(sys, "base_prefix", None)
    if base_prefix:
        candidates.append(Path(base_prefix) / "DLLs")

    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root).resolve())

    unique_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.exists():
            continue
        unique_candidates.append(resolved)
        seen.add(resolved)

    return unique_candidates


def prepare_windows_runtime_environment(current_logger: logging.Logger | None = None) -> list[Path]:
    active_logger = current_logger or logger
    if not _is_windows():
        return []

    library_dirs = _iter_runtime_library_dirs()
    active_logger.debug(
        "Preparing Windows runtime environment frozen=%s runtime_dirs=%s",
        getattr(sys, "frozen", False),
        [str(path) for path in library_dirs],
    )
    if not library_dirs:
        active_logger.warning("No Windows runtime library directories were discovered")
        return []

    current_path = os.environ.get("PATH", "")
    path_entries = [entry for entry in current_path.split(os.pathsep) if entry]
    path_lookup = {entry.casefold() for entry in path_entries}
    prepended: list[str] = []

    for candidate in library_dirs:
        value = str(candidate)
        if value.casefold() not in path_lookup:
            prepended.append(value)
            path_lookup.add(value.casefold())

    if prepended:
        os.environ["PATH"] = os.pathsep.join(prepended + path_entries)
        active_logger.info("Prepended Windows runtime library dirs to PATH: %s", prepended)
    else:
        active_logger.debug("Windows runtime library dirs are already present in PATH")

    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is None:
        active_logger.debug("os.add_dll_directory is unavailable in the current interpreter")
        return library_dirs

    for candidate in library_dirs:
        key = str(candidate)
        if key in _REGISTERED_DLL_DIRS:
            continue
        try:
            _REGISTERED_DLL_DIRS[key] = add_dll_directory(key)
            active_logger.debug("Registered Windows DLL directory %s", key)
        except OSError as exc:
            active_logger.warning("Could not register Windows DLL directory %s: %s", key, exc)

    return library_dirs


def prepare_windows_asyncio_policy(current_logger: logging.Logger | None = None) -> bool:
    active_logger = current_logger or logger
    if not _is_windows():
        return False

    selector_policy_factory = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if selector_policy_factory is None:
        active_logger.warning("WindowsSelectorEventLoopPolicy is unavailable in this interpreter")
        return False

    current_policy = asyncio.get_event_loop_policy()
    if isinstance(current_policy, selector_policy_factory):
        active_logger.debug("Windows selector event-loop policy is already active")
        return False

    asyncio.set_event_loop_policy(selector_policy_factory())
    active_logger.info("Applied Windows selector event-loop policy for CLI runtime")
    return True


def _windows_loopback_socketpair(
    family: int | None = None,
    type: int = socket.SOCK_STREAM,
    proto: int = 0,
) -> tuple[socket.socket, socket.socket]:
    if _ORIGINAL_SOCKETPAIR is None:
        raise OSError("socketpair is unavailable in this interpreter")

    if family is None:
        family = socket.AF_INET

    supported_families = {socket.AF_INET}
    if hasattr(socket, "AF_INET6"):
        supported_families.add(socket.AF_INET6)

    if family not in supported_families or type != socket.SOCK_STREAM:
        return _ORIGINAL_SOCKETPAIR(family, type, proto)

    host = "::1" if family == getattr(socket, "AF_INET6", None) else "127.0.0.1"
    bind_address: tuple[object, ...]
    if family == getattr(socket, "AF_INET6", None):
        bind_address = (host, 0, 0, 0)
    else:
        bind_address = (host, 0)

    listener = socket.socket(family, type, proto)
    server: socket.socket | None = None
    client: socket.socket | None = None

    try:
        with contextlib.suppress(OSError):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(bind_address)
        listener.listen(1)

        client = socket.socket(family, type, proto)
        client.settimeout(5.0)
        client.connect(listener.getsockname())

        server, _ = listener.accept()
        server.settimeout(5.0)

        # Authenticate the accepted socket via an in-band nonce instead of
        # relying on peer-address equality. The CPython fallback can fail on
        # some Windows systems even when the local loopback connection itself
        # is otherwise usable.
        challenge = secrets.token_bytes(16)
        client.sendall(challenge)
        if server.recv(len(challenge)) != challenge:
            raise ConnectionError("Windows socketpair handshake mismatch")

        acknowledgement = secrets.token_bytes(16)
        server.sendall(acknowledgement)
        if client.recv(len(acknowledgement)) != acknowledgement:
            raise ConnectionError("Windows socketpair acknowledgement mismatch")

        client.settimeout(None)
        server.settimeout(None)
        return server, client
    except Exception:
        if server is not None:
            server.close()
        if client is not None:
            client.close()
        raise
    finally:
        listener.close()


def prepare_windows_socketpair_shim(current_logger: logging.Logger | None = None) -> bool:
    active_logger = current_logger or logger
    if not _is_windows():
        return False
    if os.getenv("TGSTORIES_DISABLE_WINDOWS_SOCKETPAIR_SHIM", "").strip() == "1":
        active_logger.warning("Windows socketpair shim is disabled by environment override")
        return False
    if _ORIGINAL_SOCKETPAIR is None:
        active_logger.warning("socket.socketpair is unavailable; Windows socketpair shim cannot be applied")
        return False

    global _WINDOWS_SOCKETPAIR_SHIM_APPLIED
    if _WINDOWS_SOCKETPAIR_SHIM_APPLIED and socket.socketpair is _windows_loopback_socketpair:
        active_logger.debug("Windows socketpair shim is already active")
        return False

    socket.socketpair = _windows_loopback_socketpair
    _WINDOWS_SOCKETPAIR_SHIM_APPLIED = True
    active_logger.info("Applied Windows loopback socketpair shim for asyncio startup")
    return True


def prepare_windows_runtime(current_logger: logging.Logger | None = None) -> None:
    prepare_windows_runtime_environment(current_logger)
    prepare_windows_asyncio_policy(current_logger)
    prepare_windows_socketpair_shim(current_logger)


def run_async_entrypoint(
    coroutine_factory: Callable[[], Awaitable[T]],
    current_logger: logging.Logger | None = None,
) -> T:
    active_logger = current_logger or logger
    prepare_windows_runtime(active_logger)

    if not _is_windows():
        active_logger.debug("Running async entrypoint through asyncio.run on non-Windows platform")
        return asyncio.run(coroutine_factory())

    active_logger.debug("Running async entrypoint through manual Windows event-loop bootstrap")
    loop: asyncio.AbstractEventLoop | None = None
    main_task: asyncio.Task[T] | None = None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        main_task = loop.create_task(coroutine_factory())
        return loop.run_until_complete(main_task)
    except KeyboardInterrupt:
        active_logger.info("KeyboardInterrupt received; cancelling active Windows async entrypoint task")
        if loop is not None and main_task is not None and not main_task.done():
            main_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                loop.run_until_complete(main_task)
        raise
    finally:
        if loop is not None:
            pending_tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]
            if pending_tasks:
                active_logger.debug("Cancelling %s pending asyncio tasks before loop shutdown", len(pending_tasks))
                for task in pending_tasks:
                    task.cancel()
                with contextlib.suppress(Exception):
                    loop.run_until_complete(asyncio.gather(*pending_tasks, return_exceptions=True))
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            shutdown_default_executor = getattr(loop, "shutdown_default_executor", None)
            if shutdown_default_executor is not None:
                with contextlib.suppress(Exception):
                    loop.run_until_complete(shutdown_default_executor())
            asyncio.set_event_loop(None)
            with contextlib.suppress(Exception):
                loop.close()


def explain_windows_asyncio_failure(exc: BaseException) -> str | None:
    if not _is_windows():
        return None

    message = str(exc)
    if "Unexpected peer connection" not in message and "socketpair" not in message.lower():
        return None

    return (
        "Ошибка запуска Windows asyncio: Python не смог создать внутреннюю локальную "
        "socket-пару для event loop. В этой сборке включён дополнительный обход для "
        "Windows socketpair/self-pipe. Если ошибка повторилась даже после обновлённой "
        "сборки, пришлите скриншот окна и файл data/logs/app.log."
    )
