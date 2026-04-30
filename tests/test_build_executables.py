from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_executables.py"
SPEC = importlib.util.spec_from_file_location("build_executables", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
build_executables = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_executables)


def test_get_dist_dir_uses_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGSTORIES_DIST_DIR", "/tmp/stories-dist")

    assert build_executables.get_dist_dir() == Path("/tmp/stories-dist")


def test_get_build_dir_uses_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGSTORIES_BUILD_DIR", "/tmp/stories-build")

    assert build_executables.get_build_dir() == Path("/tmp/stories-build")


def test_get_windows_runtime_binaries_uses_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dll_dir = tmp_path / "dlls"
    dll_dir.mkdir()
    for filename in build_executables.WINDOWS_RUNTIME_DLLS:
        (dll_dir / filename).write_text("binary", encoding="utf-8")

    binaries = build_executables.get_windows_runtime_binaries(
        target_platform="win32",
        dll_dir_override=dll_dir,
    )

    assert binaries == [
        (dll_dir / "libssl-3.dll", "."),
        (dll_dir / "libcrypto-3.dll", "."),
    ]


def test_build_target_args_include_windows_runtime_binaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dll_dir = tmp_path / "dlls"
    dll_dir.mkdir()
    for filename in build_executables.WINDOWS_RUNTIME_DLLS:
        (dll_dir / filename).write_text("binary", encoding="utf-8")

    monkeypatch.setenv("TGSTORIES_TARGET_PLATFORM", "win32")
    monkeypatch.setenv("TGSTORIES_WINDOWS_DLL_DIR", str(dll_dir))

    args = build_executables.build_target_args(
        "stories",
        build_executables.ROOT / "scripts" / "stories.py",
        dist_dir=tmp_path / "dist",
        build_dir=tmp_path / "build",
    )

    assert "--add-binary" in args
    assert "--hidden-import" in args
    assert "--collect-binaries" in args
    assert args.count("--hidden-import") >= 3
    assert args.count("--collect-submodules") >= 3
    assert "aiosqlite" in args
    assert "aiohttp_socks" in args
    assert "cryptg" in args
    assert "--runtime-tmpdir" in args
    assert build_executables.WINDOWS_RUNTIME_TMPDIR in args
    assert any("libssl-3.dll" in value for value in args)
    assert any("libcrypto-3.dll" in value for value in args)
    assert not any("ssl.dll" in value for value in args)
