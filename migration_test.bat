@echo off
setlocal

cd /d "%~dp0"

set "PYTHON=.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
  echo [ERROR] Python virtual environment not found: %PYTHON%
  pause
  exit /b 1
)

echo Running migration tests...
"%PYTHON%" -m unittest tests.test_migration -v
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo [ERROR] migration tests failed with code %EXITCODE%
  pause
)

endlocal & exit /b %EXITCODE%
