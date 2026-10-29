@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

echo ==========================================
echo    语音转文字 - 安装依赖
echo ==========================================
echo.

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [1/2] 正在创建虚拟环境 .venv ...
    python -m venv "%~dp0.venv"
    if errorlevel 1 (
        echo.
        echo 创建失败：没有找到 python 命令。
        echo 请先安装 Python 3.9 以上版本，安装时记得勾选 Add Python to PATH。
        pause
        exit /b 1
    )
) else (
    echo [1/2] 虚拟环境已存在，跳过创建。
)

echo.
echo [2/2] 正在安装依赖库，请保持网络畅通 ...
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
)

echo.
echo 安装完成！现在可以双击「启动程序.bat」使用了。
pause