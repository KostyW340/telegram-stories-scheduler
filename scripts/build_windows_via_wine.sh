#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_ROOT="${ROOT}/build/wine"
DOWNLOAD_DIR="${TOOLS_ROOT}/downloads"
COMMAND_DIR="${TOOLS_ROOT}/commands"
WINEPREFIX="${WINEPREFIX:-${TOOLS_ROOT}/prefix}"
WINEARCH="${WINEARCH:-win64}"
PYTHON_WINDOWS_VERSION="${PYTHON_WINDOWS_VERSION:-3.12.10}"
PYTHON_INSTALLER_URL="${PYTHON_WINDOWS_INSTALLER_URL:-https://www.python.org/ftp/python/${PYTHON_WINDOWS_VERSION}/python-${PYTHON_WINDOWS_VERSION}-amd64.exe}"
PYTHON_INSTALLER_PATH="${DOWNLOAD_DIR}/python-${PYTHON_WINDOWS_VERSION}-amd64.exe"
PYTHON_WIN_DIR="${PYTHON_WIN_DIR:-C:\\Python312}"
PYTHON_EXE_PATH="${WINEPREFIX}/drive_c/Python312/python.exe"
DIST_DIR="${WINE_DIST_DIR:-${ROOT}/dist-windows}"
BUILD_DIR="${WINE_BUILD_DIR:-${ROOT}/build/wine/pyinstaller}"
CACHE_DIR="${WINE_PYINSTALLER_CACHE_DIR:-${ROOT}/build/wine/pyinstaller-cache}"
CRYPTG_WHEEL_FILENAME="${CRYPTG_WHEEL_FILENAME:-cryptg-0.5.2-cp312-cp312-win_amd64.whl}"
CRYPTG_WHEEL_URL="${CRYPTG_WHEEL_URL:-https://files.pythonhosted.org/packages/02/a7/54c2a6f3559708a04023f31407628d6db3e2482b059b226dc3f4c41ffbe1/${CRYPTG_WHEEL_FILENAME}}"
CRYPTG_WHEEL_PATH="${DOWNLOAD_DIR}/${CRYPTG_WHEEL_FILENAME}"

log() {
  printf '[%s] %s\n' "$1" "$2"
}

fail() {
  printf '[ERROR] %s\n' "$1" >&2
  exit 1
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail "Required command not found: $1"
  fi
}

download_file_via_python() {
  local url="$1"
  local output_path="$2"
  python3 - "$url" "$output_path" <<'PY'
import pathlib
import shutil
import sys
import urllib.request

url = sys.argv[1]
output_path = pathlib.Path(sys.argv[2])
output_path.parent.mkdir(parents=True, exist_ok=True)
request = urllib.request.Request(url, headers={"User-Agent": "tgstories-wine-builder/1.0"})
with urllib.request.urlopen(request, timeout=60) as response:
    with output_path.open("wb") as target:
        shutil.copyfileobj(response, target)
PY
}

to_windows_path() {
  winepath -w "$1" | tr -d '\r'
}

run_wine_cmd() {
  local command_path="$1"
  local command_path_win
  command_path_win="$(to_windows_path "$command_path")"
  log INFO "Running Wine command file ${command_path_win}"
  wine cmd /c "$command_path_win"
}

create_install_command() {
  local command_file="$1"
  local installer_path_win="$2"
  cat >"$command_file" <<EOF
@echo off
setlocal
"${installer_path_win}" /quiet InstallAllUsers=0 TargetDir=${PYTHON_WIN_DIR} Include_pip=1 Include_test=0 Include_doc=0 Include_launcher=0 AssociateFiles=0 Shortcuts=0 PrependPath=0
if errorlevel 1 exit /b 1
EOF
}

create_build_command() {
  local command_file="$1"
  local root_win="$2"
  local dist_win="$3"
  local build_win="$4"
  local cache_win="$5"
  local cryptg_wheel_win="$6"
  cat >"$command_file" <<EOF
@echo off
setlocal
cd /d "${root_win}"
if errorlevel 1 exit /b 1
set PIP_DISABLE_PIP_VERSION_CHECK=1
set PYINSTALLER_CONFIG_DIR=${cache_win}
set TGSTORIES_DIST_DIR=${dist_win}
set TGSTORIES_BUILD_DIR=${build_win}
set TGSTORIES_WINDOWS_DLL_DIR=${PYTHON_WIN_DIR}\DLLs
set TGSTORIES_CRYPTG_WHEEL_PATH=${cryptg_wheel_win}
if not exist "%TGSTORIES_CRYPTG_WHEEL_PATH%" (
  echo [ERROR] cryptg wheel not found at %TGSTORIES_CRYPTG_WHEEL_PATH%
  exit /b 1
)
${PYTHON_WIN_DIR}\python.exe -m pip show cryptg >nul 2>&1
if errorlevel 1 (
  echo [INFO] Installing cryptg from local wheel %TGSTORIES_CRYPTG_WHEEL_PATH%
  ${PYTHON_WIN_DIR}\python.exe -m pip install --no-deps "%TGSTORIES_CRYPTG_WHEEL_PATH%"
  if errorlevel 1 exit /b 1
) else (
  echo [INFO] Reusing existing Wine cryptg installation.
)
${PYTHON_WIN_DIR}\python.exe -m pip show aiohttp-socks >nul 2>&1
if errorlevel 1 (
  echo [INFO] Installing aiohttp-socks in the Wine Python environment.
  ${PYTHON_WIN_DIR}\python.exe -m pip install aiohttp-socks
  if errorlevel 1 exit /b 1
) else (
  echo [INFO] Reusing existing Wine aiohttp-socks installation.
)
${PYTHON_WIN_DIR}\python.exe -m pip show PyInstaller >nul 2>&1
if errorlevel 1 (
  echo [INFO] PyInstaller is missing in the Wine Python environment. Installing project and dev dependencies.
  ${PYTHON_WIN_DIR}\python.exe -m pip install --no-build-isolation -e .[dev]
  if errorlevel 1 exit /b 1
) else (
  echo [INFO] Reusing existing Wine Python package set.
)
${PYTHON_WIN_DIR}\python.exe scripts\build_executables.py
if errorlevel 1 exit /b 1
EOF
}

main() {
  require_command wine
  require_command wineboot
  require_command winepath
  require_command curl
  require_command python3

  export WINEPREFIX
  export WINEARCH

  mkdir -p "${DOWNLOAD_DIR}"
  mkdir -p "${COMMAND_DIR}"
  mkdir -p "${DIST_DIR}"
  mkdir -p "${BUILD_DIR}"
  mkdir -p "${CACHE_DIR}"

  log INFO "Using Wine prefix ${WINEPREFIX}"
  log INFO "Target Python version ${PYTHON_WINDOWS_VERSION}"
  log INFO "Windows artifacts will be written to ${DIST_DIR}"

  if [ ! -f "${WINEPREFIX}/system.reg" ]; then
    log INFO "Initializing fresh Wine prefix"
    wineboot -u
  fi

  if [ ! -f "${PYTHON_INSTALLER_PATH}" ]; then
    log INFO "Downloading official Windows Python installer"
    curl -fL --retry 3 --output "${PYTHON_INSTALLER_PATH}" "${PYTHON_INSTALLER_URL}"
  else
    log INFO "Reusing downloaded installer ${PYTHON_INSTALLER_PATH}"
  fi

  if [ ! -f "${CRYPTG_WHEEL_PATH}" ]; then
    log INFO "Downloading pinned cryptg wheel for Windows CPython 3.12"
    download_file_via_python "${CRYPTG_WHEEL_URL}" "${CRYPTG_WHEEL_PATH}"
  else
    log INFO "Reusing downloaded cryptg wheel ${CRYPTG_WHEEL_PATH}"
  fi

  if [ ! -f "${PYTHON_EXE_PATH}" ]; then
    local install_cmd_path="${COMMAND_DIR}/install-python.cmd"
    local installer_path_win
    installer_path_win="$(to_windows_path "${PYTHON_INSTALLER_PATH}")"
    log INFO "Installing Windows Python into ${PYTHON_WIN_DIR}"
    create_install_command "${install_cmd_path}" "${installer_path_win}"
    run_wine_cmd "${install_cmd_path}"
  else
    log INFO "Reusing existing Wine Python at ${PYTHON_EXE_PATH}"
  fi

  [ -f "${PYTHON_EXE_PATH}" ] || fail "Windows Python was not installed under ${PYTHON_WIN_DIR}"

  local root_win
  local dist_win
  local build_win
  local cache_win
  local cryptg_wheel_win
  root_win="$(to_windows_path "${ROOT}")"
  dist_win="$(to_windows_path "${DIST_DIR}")"
  build_win="$(to_windows_path "${BUILD_DIR}")"
  cache_win="$(to_windows_path "${CACHE_DIR}")"
  cryptg_wheel_win="$(to_windows_path "${CRYPTG_WHEEL_PATH}")"

  log INFO "Preparing build command file"
  local build_cmd_path="${COMMAND_DIR}/build-stories.cmd"
  create_build_command "${build_cmd_path}" "${root_win}" "${dist_win}" "${build_win}" "${cache_win}" "${cryptg_wheel_win}"

  log INFO "Starting experimental Wine-based stories.exe build"
  run_wine_cmd "${build_cmd_path}"

  if [ ! -f "${DIST_DIR}/stories.exe" ]; then
    fail "Wine build finished without producing ${DIST_DIR}/stories.exe"
  fi

  log INFO "Wine build completed: ${DIST_DIR}/stories.exe"
  log WARN "This path remains best-effort. Native Windows Python 3.12 is still the release-grade build route."
}

main "$@"
