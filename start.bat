@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title XiaoZhi OpenClaw Server

REM === FFmpeg ===
set "FFMPEG=C:\Users\24628\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin"
if exist "%FFMPEG%\ffmpeg.exe" set "PATH=%FFMPEG%;%PATH%"

REM === Start Server ===
cd /d "%~dp0main\xiaozhi-server"
if not exist "venv\Scripts\python.exe" (
    echo [ERROR] venv not found
    pause
    exit /b 1
)

echo ============================================
echo   XiaoZhi OpenClaw Server
echo   WS : ws://localhost:8000/xiaozhi/v1/
echo   OTA: http://localhost:8003/xiaozhi/ota/
echo   Test Page: http://localhost:8006/test_page.html
echo ============================================
echo.
echo Press Ctrl+C to stop
echo.

venv\Scripts\python.exe app.py
pause
