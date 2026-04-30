from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)
WINDOWS_RUNTIME_DLLS = ("libssl-3.dll", "libcrypto-3.dll")
WINDOWS_RUNTIME_TMPDIR = r"%LOCALAPPDATA%\TGStoriesRuntime"

TARGETS = (("stories", ROOT / "scripts" / "stories.py"),)


def _path_from_env(env_name: str, default: Path) -> Path:
    value = os.getenv(env_name)
    if not value:
        return default
    return Path(value)


def _data_spec(source: Path, destination: str) -> str:
    separator = ";" if sys.platform.startswith("win") else ":"
    return f"{source}{separator}{destination}"


def get_dist_dir() -> Path:
    return _path_from_env("TGSTORIES_DIST_DIR", ROOT / "dist")


def get_build_dir() -> Path:
    return _path_from_env("TGSTORIES_BUILD_DIR", ROOT / "build" / "pyinstaller")


def get_target_platform() -> str:
    return os.getenv("TGSTORIES_TARGET_PLATFORM", sys.platform)


def get_windows_dll_dir(
    *,
    target_platform: str | None = None,
    dll_dir_override: Path | None = None,
) -> Path | None:
    current_platform = (target_platform or get_target_platform()).lower()
    if not current_platform.startswith("win"):
        return None

    candidates: list[Path] = []
    override_value = os.getenv("TGSTORIES_WINDOWS_DLL_DIR")
    if override_value:
        candidates.append(Path(override_value))
    if dll_dir_override is not None:
        candidates.append(dll_dir_override)
    if sys.platform.startswith("win"):
        candidates.append(Path(sys.base_prefix) / "DLLs")
        candidates.append(Path(sys.exec_prefix) / "DLLs")
        candidates.append(Path(sys.executable).resolve().parent / "DLLs")

    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir():
            LOGGER.debug("Using Windows DLL directory %s", resolved)
            return resolved

    searched = ", ".join(str(candidate) for candidate in candidates) or "<none>"
    raise FileNotFoundError(f"Windows DLL directory not found. Searched: {searched}")


def get_windows_runtime_binaries(
    *,
    target_platform: str | None = None,
    dll_dir_override: Path | None = None,
) -> list[tuple[Path, str]]:
    dll_dir = get_windows_dll_dir(target_platform=target_platform, dll_dir_override=dll_dir_override)
    if dll_dir is None:
        return []

    binaries: list[tuple[Path, str]] = []
    missing: list[Path] = []
    for filename in WINDOWS_RUNTIME_DLLS:
        candidate = dll_dir / filename
        if candidate.is_file():
            binaries.append((candidate, "."))
        else:
            missing.append(candidate)

    if missing:
        missing_paths = ", ".join(str(path) for path in missing)
        raise FileNotFoundError(f"Required Windows runtime DLLs are missing: {missing_paths}")

    return binaries


def build_target_args(name: str, script_path: Path, *, dist_dir: Path, build_dir: Path) -> list[str]:
    args = [
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name",
        name,
        "--paths",
        str(ROOT),
        "--collect-submodules",
        "app",
        "--hidden-import",
        "aiosqlite",
        "--hidden-import",
        "aiohttp_socks",
        "--hidden-import",
        "cryptg",
        "--collect-submodules",
        "aiosqlite",
        "--collect-submodules",
        "aiohttp_socks",
        "--collect-binaries",
        "cryptg",
        "--add-data",
        _data_spec(ROOT / "alembic.ini", "."),
        "--add-data",
        _data_spec(ROOT / "migrations", "migrations"),
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir / name),
        "--specpath",
        str(build_dir / "spec"),
    ]

    if get_target_platform().lower().startswith("win"):
        args.extend(["--runtime-tmpdir", WINDOWS_RUNTIME_TMPDIR])

    for binary_path, destination in get_windows_runtime_binaries():
        LOGGER.info("Bundling Windows runtime binary %s -> %s", binary_path, destination)
        args.extend(["--add-binary", _data_spec(binary_path, destination)])

    args.append(str(script_path))
    return args


def build_all() -> None:
    dist_dir = get_dist_dir()
    build_dir = get_build_dir()

    LOGGER.info("Building PyInstaller targets into %s", dist_dir)
    LOGGER.debug("Using build workspace %s", build_dir)

    shutil.rmtree(build_dir, ignore_errors=True)
    shutil.rmtree(dist_dir, ignore_errors=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    for name, script_path in TARGETS:
        LOGGER.info("Packaging target %s from %s", name, script_path)
        target_args = build_target_args(name, script_path, dist_dir=dist_dir, build_dir=build_dir)
        LOGGER.debug("PyInstaller args for %s: %s", name, target_args)
        PyInstaller.__main__.run(target_args)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    build_all()
