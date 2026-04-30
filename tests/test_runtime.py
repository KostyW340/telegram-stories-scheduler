from __future__ import annotations

import logging
import os
import types
from pathlib import Path

import pytest

from app.config import runtime


def test_prepare_windows_runtime_environment_prepends_runtime_dirs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    python_home = tmp_path / "python"
    dll_dir = python_home / "DLLs"
    dll_dir.mkdir(parents=True)

    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.sys, "executable", str(runtime_root / "stories.exe"))
    monkeypatch.setattr(runtime.sys, "base_prefix", str(python_home))
    monkeypatch.setattr(runtime.sys, "_MEIPASS", str(bundle_root), raising=False)
    monkeypatch.setenv("PATH", r"C:\Windows\System32")
    runtime._REGISTERED_DLL_DIRS.clear()

    added_dirs: list[str] = []

    def fake_add_dll_directory(path: str) -> object:
        added_dirs.append(path)
        return object()

    monkeypatch.setattr(runtime.os, "add_dll_directory", fake_add_dll_directory, raising=False)

    discovered = runtime.prepare_windows_runtime_environment(logging.getLogger("test"))

    assert discovered == [runtime_root.resolve(), dll_dir.resolve(), bundle_root.resolve()]
    path_entries = os.environ["PATH"].split(os.pathsep)
    assert path_entries[:3] == [str(runtime_root.resolve()), str(dll_dir.resolve()), str(bundle_root.resolve())]
    assert added_dirs == [str(runtime_root.resolve()), str(dll_dir.resolve()), str(bundle_root.resolve())]


def test_prepare_windows_asyncio_policy_switches_to_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSelectorPolicy:
        pass

    captured: dict[str, object] = {}
    fake_asyncio = types.SimpleNamespace(
        WindowsSelectorEventLoopPolicy=FakeSelectorPolicy,
        get_event_loop_policy=lambda: object(),
        set_event_loop_policy=lambda policy: captured.setdefault("policy", policy),
    )

    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime, "asyncio", fake_asyncio)

    assert runtime.prepare_windows_asyncio_policy(logging.getLogger("test")) is True
    assert isinstance(captured["policy"], FakeSelectorPolicy)


def test_prepare_windows_socketpair_shim_patches_socketpair(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = object()

    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime, "_ORIGINAL_SOCKETPAIR", marker)
    monkeypatch.setattr(runtime.socket, "socketpair", marker)
    monkeypatch.setattr(runtime, "_WINDOWS_SOCKETPAIR_SHIM_APPLIED", False)

    assert runtime.prepare_windows_socketpair_shim(logging.getLogger("test")) is True
    assert runtime.socket.socketpair is runtime._windows_loopback_socketpair


def test_windows_loopback_socketpair_returns_connected_pair() -> None:
    try:
        left, right = runtime._windows_loopback_socketpair()
    except PermissionError as exc:
        pytest.skip(f"Local socket creation is blocked by this sandbox: {exc}")

    try:
        right.sendall(b"ping")
        assert left.recv(4) == b"ping"

        left.sendall(b"pong")
        assert right.recv(4) == b"pong"
    finally:
        left.close()
        right.close()


def test_run_async_entrypoint_does_not_create_coroutine_before_windows_loop_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoked = False

    def fake_prepare_windows_runtime(_logger: logging.Logger | None = None) -> None:
        return None

    def fail_new_event_loop() -> object:
        raise ConnectionError("Unexpected peer connection")

    async def sample() -> int:
        return 1

    def factory() -> object:
        nonlocal invoked
        invoked = True
        return sample()

    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime, "prepare_windows_runtime", fake_prepare_windows_runtime)
    monkeypatch.setattr(runtime.asyncio, "new_event_loop", fail_new_event_loop)

    with pytest.raises(ConnectionError):
        runtime.run_async_entrypoint(factory, logging.getLogger("test"))

    assert invoked is False


def test_explain_windows_asyncio_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime.sys, "platform", "win32")

    message = runtime.explain_windows_asyncio_failure(ConnectionError("Unexpected peer connection"))

    assert message is not None
    assert "socketpair/self-pipe" in message
