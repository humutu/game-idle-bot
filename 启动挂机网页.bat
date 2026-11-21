@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 还没有安装运行环境，请先双击「首次安装.bat」。
  echo.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" web.py %*
pause
