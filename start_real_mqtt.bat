@echo off
title fruit_monitor - real MQTT uplink
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PYTHON=.venv\Scripts\python.exe"
set "LOG=run.log"

rem ============================================================
rem  Real MQTT uplink launcher (embedded broker + GUI, 1 process)
rem
rem      start_real_mqtt.bat                  preflight + broker + GUI
rem      start_real_mqtt.bat --check          link self-check, no GUI
rem      start_real_mqtt.bat --check --secs 60
rem      start_real_mqtt.bat --check --publish-esp   send an ESP-shaped msg
rem
rem  Run log: run.log   (full preflight + any traceback)
rem  NOTE: keep this file ASCII-only.  cmd.exe on a GBK console
rem  mis-parses UTF-8 Chinese bytes and drops lines.
rem ============================================================

echo ====================================================
echo  Real MQTT uplink (embedded broker + GUI, 1 process)
echo  Full log: %CD%\%LOG%
echo ====================================================
echo.

if not exist "%PYTHON%" (
    echo [ERROR] virtualenv not found: %PYTHON%
    echo Create it first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip.exe install -r requirements.txt
    pause
    exit /b 1
)

echo [env]
"%PYTHON%" --version
echo.

"%PYTHON%" -u real_mqtt.py %* 2>&1 > "%LOG%"
set "RC=%ERRORLEVEL%"

type "%LOG%"

echo.
echo ====================================================
if "%RC%"=="0" (
    echo  exited normally
) else if "%RC%"=="2" (
    echo  self-check finished: NO uplink received
    echo  check ESP power / wifi / firewall port 1883
) else if "%RC%"=="3" (
    echo  message received but protocol parsing FAILED
    echo  check topic format and payload fields
) else (
    echo  abnormal exit code %RC%
    echo  read the preflight steps above for the first ERROR
    echo  full traceback saved to %LOG%
)
echo ====================================================
pause
exit /b %RC%
