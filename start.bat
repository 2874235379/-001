@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

set PYTHONIOENCODING=utf-8
echo Starting FacePass with %PY% ...
"%PY%" -m app.main
echo.
echo Server stopped. Press any key to close.
pause >nul
