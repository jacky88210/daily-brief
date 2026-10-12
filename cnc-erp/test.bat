@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=
python -c "import sys" >nul 2>nul && set PY=python
if not defined PY py -3 -c "import sys" >nul 2>nul && set PY=py -3
if not defined PY (
  echo 這台電腦還沒有安裝 Python，請先雙擊 install-python.bat。
  pause
  exit /b 1
)
set CNC_DB=%TEMP%\cnc-test.db
%PY% -m unittest discover -p "test_*.py"
pause
