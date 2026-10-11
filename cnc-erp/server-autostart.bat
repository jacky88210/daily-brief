@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CNC 進銷存（背景執行中，請勿關閉）
set PY=python
where python >nul 2>nul || set PY=py
:loop
%PY% server.py --lan
rem 3 = 已經有一份在執行；0 = 正常關閉；其他 = 意外停止 → 5 秒後自動重啟
if errorlevel 3 if not errorlevel 4 exit /b
if not errorlevel 1 exit /b
echo 系統意外停止，5 秒後自動重新啟動…
timeout /t 5 >nul
goto loop
