@echo off
cd /d "%~dp0"

REM === FFmpeg ===
set "FFMPEG=C:\Users\24628\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin"
if exist "%FFMPEG%\ffmpeg.exe" set "PATH=%FFMPEG%;%PATH%"

REM === Start Test Page HTTP Server (port 8006) ===
start "XiaoZhi-TestPage" python -m http.server 8006 -d "%~dp0main\xiaozhi-server\test"

REM === Start OpenClaw Server ===
cd /d "%~dp0main\xiaozhi-server"
if not exist "venv\Scripts\python.exe" (
    echo [ERROR] venv not found at %cd%
    pause
    exit /b 1
)

echo ============================================
echo   XiaoZhi AI - All-in-One Launcher
echo ============================================
echo   Test Page : http://localhost:8006/test_page.html
echo   WebSocket : ws://localhost:8000/xiaozhi/v1/
echo   HTTP OTA  : http://localhost:8003/xiaozhi/ota/
echo   Feishu    : http://localhost:8003/feishu/callback
echo ============================================
echo.
echo Opening browser...
start "" http://localhost:8006/test_page.html
echo.

venv\Scripts\python.exe app.py

echo.
echo Server stopped.
pause
