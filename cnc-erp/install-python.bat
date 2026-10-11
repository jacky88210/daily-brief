@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   安裝 Python（CNC 進銷存系統需要它才能執行）
echo ============================================
echo.
python -c "import sys" >nul 2>nul && (
  echo 這台電腦已經有 Python 了：
  python --version
  echo 可以直接雙擊 start.bat 或 install-autostart.bat。
  pause
  exit /b
)
where winget >nul 2>nul
if errorlevel 1 goto manual
echo 正在用 Windows 內建的 winget 自動下載安裝 Python 3.12，約 1~3 分鐘…
echo （如果跳出「是否允許變更」請按「是」）
echo.
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto manual
echo.
echo ============================================
echo   安裝完成！
echo   請「關掉這個視窗」，再雙擊 install-autostart.bat（正式使用）
echo   或 demo.bat（先試玩）。
echo ============================================
pause
exit /b

:manual
echo.
echo 無法自動安裝，改用手動安裝：
echo  1. 等一下會打開 Python 官網下載頁，下載 Windows installer（64-bit）。
echo  2. 執行安裝檔，第一個畫面最下面「Add python.exe to PATH」一定要打勾！
echo  3. 按「Install Now」，裝完後關掉這個視窗，再雙擊 install-autostart.bat。
echo.
pause
start https://www.python.org/downloads/windows/
