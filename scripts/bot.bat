@echo off
setlocal

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scripts\bot.py
) else (
  py scripts\bot.py
)
