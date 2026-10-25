@echo off
wsl.exe --cd /home/wwwwi/scopebench-pilot .venv/bin/python experiments/high_memory_mode.py --windows-profile "%USERPROFILE%"
if errorlevel 1 echo High-memory setup failed. Check the message above.
pause
