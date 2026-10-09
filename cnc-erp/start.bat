@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8080"
python server.py --lan
if errorlevel 1 py server.py --lan
pause
