@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=
python -c "import sys" >nul 2>nul && set PY=python
if not defined PY py -3 -c "import sys" >nul 2>nul && set PY=py -3
if not defined PY (
  echo.
  echo 這台電腦還沒有安裝 Python。
  echo 請先雙擊同一個資料夾裡的「install-python.bat」安裝，裝好後再執行一次。
  echo.
  pause
  exit /b 1
)
set CNC_DB=%~dp0demo.db
start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8080"
%PY% server.py --demo
if errorlevel 3 if not errorlevel 4 echo 正式系統正在執行中（佔用 8080），請先關掉它的視窗再開示範版。
pause
