@echo off
REM Windows plan task FruitMonitor-GitArchive entry, every 30 min.
"E:\opencode\fruit_monitor\.venv\Scripts\python.exe" "E:\opencode\fruit_monitor\scripts\git_archive.py" >> "E:\opencode\fruit_monitor\logs\archive.stdout.log" 2>&1
