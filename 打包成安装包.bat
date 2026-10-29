@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

echo ==========================================
echo   语音转文字 - 打包成可分享的安装包
echo ==========================================
echo.
echo 产物：发布\语音转文字安装包.zip
echo       对方解压后双击「安装程序.exe」即可打开安装页面。
echo.

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo 还没安装依赖，请先双击运行「安装依赖.bat」。
    pause
    exit /b 1
)

if not exist "%~dp0dist\语音转文字\语音转文字.exe" (
    echo 还没打包应用本体，请先双击运行「打包成应用.bat」。
    pause
    exit /b 1
)

echo [1/2] 打包安装程序（PyInstaller）...
"%~dp0.venv\Scripts\python.exe" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo       正在安装 PyInstaller ...
    "%~dp0.venv\Scripts\python.exe" -m pip install pyinstaller
)
"%~dp0.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean ^
    --windowed --name "安装程序" --icon "%~dp0app.ico" ^
    --distpath "%~dp0dist_installer" --workpath "%~dp0build_installer" ^
    --specpath "%~dp0build_installer" "%~dp0installer.py"
if errorlevel 1 (
    echo.
    echo 安装程序打包失败，请把上面的报错信息发出来。
    pause
    exit /b 1
)

echo.
echo [2/2] 组装目录并压缩 ...
"%~dp0.venv\Scripts\python.exe" "%~dp0打包安装包.py"
if errorlevel 1 (
    echo.
    echo 组装 / 压缩失败，请把上面的报错信息发出来。
    pause
    exit /b 1
)

echo.
pause