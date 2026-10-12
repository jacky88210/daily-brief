@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal
echo ============================================
echo   更新 CNC 進銷存系統（客戶資料不會被動到）
echo ============================================
set PY=
python -c "import sys" >nul 2>nul && set PY=python
if not defined PY py -3 -c "import sys" >nul 2>nul && set PY=py -3
if not defined PY (
  echo 這台電腦還沒有安裝 Python，請先雙擊 install-python.bat。
  pause
  exit /b 1
)

where git >nul 2>nul
if errorlevel 1 (
  echo 這台電腦沒有 git，無法自動更新。
  echo 請改用手動更新：見 README「更新系統」。
  pause
  exit /b 1
)
if not exist ".git" if not exist "..\.git" (
  echo 這個資料夾不是用 git 下載的，無法自動更新。請見 README「更新系統」。
  pause
  exit /b 1
)

echo.
echo [1/4] 更新前先備份一份快照…
%PY% server.py --snapshot
if errorlevel 1 (
  echo 備份失敗，為了安全已停止更新。
  pause
  exit /b 1
)

echo.
echo [2/4] 下載最新版程式…
for /f %%i in ('git rev-parse HEAD') do set OLD=%%i
git pull --ff-only
if errorlevel 1 (
  echo 下載失敗（網路或程式有本機修改）。系統沒有變動，仍是舊版。
  pause
  exit /b 1
)

echo.
echo [3/4] 檢查新版程式（約 30 秒）…
set CNC_DB=%TEMP%\cnc-update-test.db
%PY% -m unittest discover -p "test_*.py" -q
if errorlevel 1 (
  echo.
  echo 新版程式檢查沒有通過，已自動退回舊版，系統照常使用。請把上面的訊息拍照給維護的人。
  git reset --hard %OLD%
  pause
  exit /b 1
)
set CNC_DB=

echo.
echo [4/4] 重新啟動系統…
if not exist server.pid goto nopid
set /p PID=<server.pid
taskkill /PID %PID% /F >nul 2>nul
:nopid
echo 開機自動啟動的系統會在 5 秒內用新版重新開起來。
echo 如果平常是用 start.bat 開的，請重新雙擊 start.bat。
timeout /t 8 >nul
start http://127.0.0.1:8080
echo.
echo 更新完成！
type VERSION
pause
