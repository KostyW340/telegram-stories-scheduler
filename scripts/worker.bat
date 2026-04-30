@echo off
setlocal

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scripts\worker.py
) else (
  py scripts\worker.py
)
