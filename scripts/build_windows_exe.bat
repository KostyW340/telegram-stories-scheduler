@echo off
setlocal

if not exist ".venv\Scripts\python.exe" (
  echo Python virtual environment not found at .venv\Scripts\python.exe
  exit /b 1
)

if not exist ".venv\Scripts\pyinstaller.exe" (
  echo PyInstaller is not installed. Run: .venv\Scripts\pip install -e .[dev]
  exit /b 1
)

".venv\Scripts\python.exe" scripts\build_executables.py
