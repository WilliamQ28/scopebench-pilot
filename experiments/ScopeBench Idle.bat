@echo off
echo Selecting Idle: up to 12 tasks, 6 GiB soft Docker budget.
wsl.exe --cd /home/wwwwi/scopebench-pilot .venv/bin/python experiments/set_information_mode.py idle
if errorlevel 1 echo Could not change mode. Check the error above.
pause
