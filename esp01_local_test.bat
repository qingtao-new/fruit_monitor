@echo off
setlocal

cd /d "%~dp0"

set "PYTHON=.venv\Scripts\python.exe"
set "AMQTT=.venv\Scripts\amqtt.exe"
set "BROKER_LOG=%TEMP%\fruit_monitor_broker.log"
set "SUB_LOG=%TEMP%\fruit_monitor_subscriber.log"
set "GUI_LOG=%TEMP%\fruit_monitor_gui.log"

if not exist "%PYTHON%" (
  echo [ERROR] Python virtual environment not found: %PYTHON%
  pause
  exit /b 1
)

echo [1/3] Starting local MQTT broker...
start "fruit_monitor_broker" /min "%AMQTT%" -d > "%BROKER_LOG%" 2>&1
timeout /t 2 >nul

echo [2/3] Starting MQTT subscriber...
start "fruit_monitor_subscriber" /min "%PYTHON%" mqtt_subscriber.py > "%SUB_LOG%" 2>&1
timeout /t 2 >nul

echo [3/3] Starting MQTT GUI...
start "fruit_monitor_gui" /min "%PYTHON%" main.py --mqtt > "%GUI_LOG%" 2>&1
timeout /t 3 >nul

echo.
echo Broker log: %BROKER_LOG%
echo Subscriber log: %SUB_LOG%
echo GUI log: %GUI_LOG%
echo.
echo Done. Watch the subscriber window for ESP01 uploads.
pause
