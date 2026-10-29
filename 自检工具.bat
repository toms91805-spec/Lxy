@echo off
chcp 936 >nul
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo 还没安装依赖，请先双击运行「安装依赖.bat」。
    pause
    exit /b 1
)

echo 正在检查运行环境，请稍候……
echo.
"%~dp0.venv\Scripts\python.exe" "%~dp0自检.py"

echo.
echo （按任意键关闭本窗口）
pause >nul
exit /b