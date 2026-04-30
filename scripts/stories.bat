@echo off
setlocal

if not exist ".venv\\Scripts\\python.exe" (
  echo Python virtual environment not found at .venv\\Scripts\\python.exe
  exit /b 1
)

".venv\\Scripts\\python.exe" scripts\\stories.py %*
