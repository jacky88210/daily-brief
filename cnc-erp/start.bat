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
start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8080"
%PY% server.py --lan
if errorlevel 3 if not errorlevel 4 (
  echo.
  echo 系統已經在背景執行中，已幫你打開瀏覽器，這個視窗可以關掉。
  timeout /t 5 >nul
  exit /b
)
pause
