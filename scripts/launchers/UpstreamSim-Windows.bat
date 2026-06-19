@echo off
REM 雙擊執行上位機 CF_ 模擬器（Windows，需先開著 Control，且裝 python3）。
cd /d "%~dp0..\.."
python scripts\upstream_simulator.py --host 127.0.0.1 --port 8787
echo.
pause
