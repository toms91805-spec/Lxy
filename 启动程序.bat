@echo off
chcp 936 >nul
cd /d "%~dp0"

set "APPEXE=%~dp0dist\语音转文字\语音转文字.exe"
set "APPDIR=%~dp0dist\语音转文字"

:: 已经打包成应用时，直接启动它：双击即开，不会出现黑色终端窗口
if exist "%APPEXE%" (
    powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $lnk=Join-Path ([Environment]::GetFolderPath('Desktop')) '语音转文字.lnk'; $s=$ws.CreateShortcut($lnk); $s.TargetPath='%APPEXE%'; $s.WorkingDirectory='%APPDIR%'; $s.IconLocation='%APPEXE%,0'; $s.Description='语音转文字 · 离线实时识别'; $s.Save()" >nul 2>&1
    start "" "%APPEXE%"
    exit /b
)

:: 还没打包时，退回用源码运行（需要 .venv 虚拟环境）
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo 还没安装依赖，请先双击运行「安装依赖.bat」。
    echo 或者双击「打包成应用.bat」生成独立应用。
    pause
    exit /b 1
)

:: 一梦江湖等游戏以管理员身份运行。Windows 的 UIPI 机制会丢弃「普通权限程序发给高权限窗口」的模拟输入，
:: 导致识别正常但文字打不进去，所以这里主动请求管理员权限。
fltmc >nul 2>&1
if errorlevel 1 (
    echo 正在请求管理员权限……
    echo （为了能向以管理员身份运行的游戏/程序输入文字，请在弹窗中点「是」）
    powershell -NoProfile -Command "try { Start-Process -FilePath '%~f0' -Verb RunAs -ErrorAction Stop } catch { exit 1 }"
    if errorlevel 1 (
        echo.
        echo [提示] 没有获得管理员权限，程序没有启动。
        echo        可以右键本文件，选择「以管理员身份运行」再试一次；
        echo        或者直接双击本文件，以普通权限运行（但无法向游戏输入文字）。
        pause
    )
    exit /b
)

powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $lnk=Join-Path ([Environment]::GetFolderPath('Desktop')) '语音转文字.lnk'; $s=$ws.CreateShortcut($lnk); $s.TargetPath='%~f0'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0icon\icon_b_green.ico,0'; $s.Description='语音转文字 · 离线实时识别'; $s.Save()" >nul 2>&1

start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app.py"
exit /b