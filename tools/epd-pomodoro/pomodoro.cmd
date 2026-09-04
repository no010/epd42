@echo off
rem EPD42 番茄钟启动器：首次运行自动建虚拟环境并装依赖，之后直接跑
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [pomodoro] 首次运行：创建虚拟环境并安装依赖...
    python -m venv .venv
    if errorlevel 1 goto :err
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :err
)
".venv\Scripts\python.exe" pomodoro.py %*
exit /b %errorlevel%
:err
echo [pomodoro] 环境准备失败：请确认已安装 Python 3.10+，且首次运行需要联网。
exit /b 1