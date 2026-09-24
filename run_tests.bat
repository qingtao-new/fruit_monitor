@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

set "PYTHON=.venv\Scripts\python.exe"
set "UNITTEST_CODE=0"
set "PYTEST_CODE=0"

if not exist "%PYTHON%" (
  echo [ERROR] Python virtual environment not found: %PYTHON%
  pause
  exit /b 1
)

echo [1/4] Checking imports and compiling source...
"%PYTHON%" -m py_compile main.py mqtt_client.py gui.py db.py protocol.py spectrum.py maturity.py broker_lib.py analysis.py publisher_sim.py mqtt_subscriber.py migration\__init__.py migration\data_manager.py migration\stats_analysis.py migration\ml_model.py migration\perf_report.py
set "COMPILE_CODE=%ERRORLEVEL%"
if not "%COMPILE_CODE%"=="0" (
  echo [ERROR] py_compile failed with code %COMPILE_CODE%
  pause
  exit /b %COMPILE_CODE%
)

echo [2/4] Running migration tests...
"%PYTHON%" -m unittest tests.test_migration -v
set "MIGRATION_CODE=%ERRORLEVEL%"
if not "%MIGRATION_CODE%"=="0" (
  echo [ERROR] migration tests failed with code %MIGRATION_CODE%
  pause
  exit /b %MIGRATION_CODE%
)

echo [3/4] Running unittest suite...
"%PYTHON%" -m unittest -v
set "UNITTEST_CODE=%ERRORLEVEL%"
if not "%UNITTEST_CODE%"=="0" (
  echo [ERROR] unittest failed with code %UNITTEST_CODE%
  pause
  exit /b %UNITTEST_CODE%
)

echo [4/4] Running pytest suite if available...
"%PYTHON%" -c "import pytest" 1>nul 2>nul
if errorlevel 1 (
  echo [SKIP] pytest is not installed in the virtual environment.
  set "PYTEST_CODE=-1"
) else (
  "%PYTHON%" -m pytest -q
  set "PYTEST_CODE=%ERRORLEVEL%"
  if not "%PYTEST_CODE%"=="0" (
    echo [WARN] pytest failed with code %PYTEST_CODE%
  )
)

echo.
echo Compile exit code: %COMPILE_CODE%
echo Migration test exit code: %MIGRATION_CODE%
echo Unit test exit code: %UNITTEST_CODE%
echo Pytest exit code: %PYTEST_CODE%

if "%UNITTEST_CODE%"=="0" (
  echo [OK] Tests passed.
) else (
  echo [ERROR] Tests failed.
)

pause
endlocal & exit /b %UNITTEST_CODE%
