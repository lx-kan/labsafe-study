@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 请先运行 setup.cmd
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py --inspect
pause
