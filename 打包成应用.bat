@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

echo ==========================================
echo   语音转文字 - 打包成独立应用
echo ==========================================
echo.

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo 还没安装依赖，请先双击运行「安装依赖.bat」。
    pause
    exit /b 1
)

echo [1/3] 检查打包工具 PyInstaller ...
"%~dp0.venv\Scripts\python.exe" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo       正在安装 PyInstaller，请保持网络畅通 ...
    "%~dp0.venv\Scripts\python.exe" -m pip install pyinstaller
    if errorlevel 1 (
        echo.
        echo PyInstaller 安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)

echo.
echo [2/3] 正在打包（第一次会比较慢，请耐心等待）...
"%~dp0.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean ^
    --windowed --name 语音转文字 --icon "%~dp0app.ico" ^
    --collect-all vosk --collect-all pypinyin --collect-data _sounddevice_data ^
    --distpath "%~dp0dist" --workpath "%~dp0build" --specpath "%~dp0build" ^
    "%~dp0app.py"
if errorlevel 1 (
    echo.
    echo 打包失败，请把上面的报错信息发出来。
    pause
    exit /b 1
)

echo.
echo [3/3] 复制模型和配套文件到应用目录 ...
set "APP=%~dp0dist\语音转文字"
if not exist "%APP%" mkdir "%APP%"
if exist "%~dp0models" xcopy /e /i /y "%~dp0models" "%APP%\models" >nul
if exist "%~dp0app.ico" copy /y "%~dp0app.ico" "%APP%\app.ico" >nul
if exist "%~dp0替换词典.txt" copy /y "%~dp0替换词典.txt" "%APP%\替换词典.txt" >nul
if exist "%~dp0README.md" copy /y "%~dp0README.md" "%APP%\README.md" >nul

:: 更新桌面快捷方式，让它直接指向应用（双击即开，不经过终端）
powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $lnk=Join-Path ([Environment]::GetFolderPath('Desktop')) '语音转文字.lnk'; $s=$ws.CreateShortcut($lnk); $s.TargetPath='%APP%\语音转文字.exe'; $s.WorkingDirectory='%APP%'; $s.IconLocation='%APP%\语音转文字.exe,0'; $s.Description='语音转文字'; $s.Save()" >nul 2>&1

echo.
echo 打包完成！
echo 应用位置：%APP%\语音转文字.exe
echo 桌面快捷方式「语音转文字」已指向它，双击即可打开，不会出现黑色终端窗口。
echo.
pause