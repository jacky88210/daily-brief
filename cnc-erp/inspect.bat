@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
  echo 請把凌越的備份檔（例如 wstkBKUP.001）拖曳到這個 inspect.bat 圖示上
  pause
  exit /b
)
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
%PY% tools\inspect_backup.py %* > 檢查結果.txt 2>&1
notepad 檢查結果.txt
