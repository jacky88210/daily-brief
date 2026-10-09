@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
  echo 請把凌越的備份檔（例如 wstkBKUP.001）拖曳到這個 inspect.bat 圖示上
  pause
  exit /b
)
python tools\inspect_backup.py %* > 檢查結果.txt 2>&1
if errorlevel 9009 py tools\inspect_backup.py %* > 檢查結果.txt 2>&1
notepad 檢查結果.txt
