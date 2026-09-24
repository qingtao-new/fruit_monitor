@echo off
cd /d "%~dp0"
start "fruit_monitor" /normal cmd /c start_real_mqtt.bat --mqtt %*
