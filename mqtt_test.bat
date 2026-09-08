@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

set "PYTHON=.venv\Scripts\python.exe"
set "AMQTT=.venv\Scripts\amqtt.exe"
set "BROKER_LOG=%TEMP%\fruit_monitor_broker.log"
set "SUB_LOG=%TEMP%\fruit_monitor_subscriber.log"
set "PUB_LOG=%TEMP%\fruit_monitor_publisher.log"
set "GUI_LOG=%TEMP%\fruit_monitor_gui.log"

if not exist "%PYTHON%" (
  echo [ERROR] Python virtual environment not found: %PYTHON%
  pause
  exit /b 1
)

echo [1/5] Starting MQTT broker...
start "fruit_monitor_broker" /min "%AMQTT%" -d > "%BROKER_LOG%" 2>&1
timeout /t 2 >nul

echo [2/5] Starting MQTT subscriber...
start "fruit_monitor_subscriber" /min "%PYTHON%" mqtt_subscriber.py > "%SUB_LOG%" 2>&1
timeout /t 2 >nul

echo [3/5] Starting MQTT simulator publisher...
start "fruit_monitor_publisher" /min "%PYTHON%" publisher_sim.py --host 127.0.0.1 --port 1883 --gateway GW_001 --node LORA_NODE_01 > "%PUB_LOG%" 2>&1
timeout /t 2 >nul

echo [4/5] Starting PC GUI...
start "fruit_monitor_gui" /min "%PYTHON%" main.py --mqtt > "%GUI_LOG%" 2>&1
timeout /t 3 >nul

echo [5/5] Running analysis check...
"%PYTHON%" analysis.py --gateway GW_001 --node LORA_NODE_01 --model centroid
set "ANALYSIS_CODE=%ERRORLEVEL%"

echo.
echo Broker log: %BROKER_LOG%
echo Subscriber log: %SUB_LOG%
echo Publisher log: %PUB_LOG%
echo GUI log: %GUI_LOG%
echo Analysis exit code: %ANALYSIS_CODE%

echo Done. GUI and MQTT processes remain running.
pause
