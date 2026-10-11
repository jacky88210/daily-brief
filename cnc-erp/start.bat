@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=python
where python >nul 2>nul || set PY=py
start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8080"
%PY% server.py --lan
if errorlevel 3 if not errorlevel 4 (
  echo.
  echo 系統已經在背景執行中，已幫你打開瀏覽器，這個視窗可以關掉。
  timeout /t 5 >nul
  exit /b
)
pause
