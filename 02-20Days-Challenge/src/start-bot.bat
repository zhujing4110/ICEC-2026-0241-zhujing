@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set WECHATAUTO_RHYTHM=fast

echo ==========================================
echo  WeChat AI Auto-Reply Bridge
echo  Account: Yu Xia (small account)
echo ==========================================
echo.
echo  [1] Keep WeChat window OPEN and NOT minimized.
echo  [2] Press Ctrl+C to stop.
echo.

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Run: python -m venv .venv
    pause
    exit /b 1
)

".venv\Scripts\python.exe" bridge.py
set RC=%ERRORLEVEL%
if "%RC%"=="2" (
    echo.
    echo ERROR: Another bot instance is already running.
    echo Please close that window first, then run this again.
    echo.
)
echo.
echo Bot stopped.
pause
