@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv\Scripts\python.exe not found.
  pause
  exit /b 1
)

echo Starting fruit_monitor simulator mode...
".venv\Scripts\python.exe" main.py --simulator
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" (
  echo [ERROR] fruit_monitor exited with code %EXITCODE%
  pause
)
endlocal & exit /b %EXITCODE%
