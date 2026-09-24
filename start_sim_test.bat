@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

set "PYTHON=.venv\Scripts\python.exe"
set "GUI_LOG=%TEMP%\fruit_monitor_gui_sim.log"
set "PUB_LOG=%TEMP%\fruit_monitor_publisher_sim.log"

if not exist "%PYTHON%" (
  echo [ERROR] Python virtual environment not found: %PYTHON%
  pause
  exit /b 1
)

if not exist ".venv\Scripts\amqtt.exe" (
  echo [ERROR] AMQTT not found: .venv\Scripts\amqtt.exe
  pause
  exit /b 1
)

echo [1/3] Starting local MQTT broker...
start "fruit_monitor_broker_sim" /min ".venv\Scripts\amqtt.exe" -d
timeout /t 2 >nul

echo [2/3] Starting simulated publisher...
start "fruit_monitor_publisher_sim" /min ".venv\Scripts\python.exe" publisher_sim.py

echo [3/3] Launching GUI in simulator mode...
start "fruit_monitor_gui_sim" /D "%~dp0" cmd /k ".venv\Scripts\python.exe" main.py --simulator
timeout /t 2 >nul
echo GUI log: %GUI_LOG%
echo Publisher log: %PUB_LOG%
echo [OK] Simulation test started.
pause
exit /b 0
