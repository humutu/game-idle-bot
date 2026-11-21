@echo off
cd /d "%~dp0"
echo ==========================================
echo    小游戏挂机助手 - 首次安装
echo ==========================================
echo.
set "PY="
py -3.12 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3.12"
if defined PY goto found
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto found
python --version >nul 2>nul
if not errorlevel 1 set "PY=python"
:found
if not defined PY (
  echo 没有找到 Python。
  echo 请先安装 Python 3.12，安装时勾选 Add python.exe to PATH：
  echo   https://www.python.org/downloads/windows/
  echo.
  pause
  exit /b 1
)
echo 使用的 Python 版本：
%PY% --version
echo.
if exist ".venv\Scripts\python.exe" goto deps
echo 正在创建运行环境 .venv（只做一次，请稍等）...
%PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo 创建运行环境失败，请把上面的报错截图发给开发者。
  pause
  exit /b 1
)
:deps
echo 正在安装依赖（需要联网，约 1~2 分钟）...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo 依赖安装失败，请检查网络后重新双击本文件。
  pause
  exit /b 1
)
echo.
echo 安装完成！以后双击「启动挂机网页.bat」即可开始。
pause
