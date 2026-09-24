@echo off
REM fruit_monitor auto archive loop, one commit attempt every 30 minutes.
REM Lunched from the user Startup folder, so it needs no admin rights.
rem /n silent mode:
:loop
"E:\opencode\fruit_monitor\.venv\Scripts\python.exe" "E:\opencode\fruit_monitor\scripts\git_archive.py" >> "E:\opencode\fruit_monitor\logs\archive.stdout.log" 2>&1
rem wait 30 minutes
timeout /t 1800 /nobreak >nul

goto loop
