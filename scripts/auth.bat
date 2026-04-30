@echo off
setlocal

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scripts\auth.py
) else (
  py scripts\auth.py
)
