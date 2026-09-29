@echo off
title Stop Project Alpha 2.0
echo ========================================================
echo  [Project Alpha 2.0] Stopping Backend & Frontend...
echo ========================================================

:: Terminate process listening on backend port 8001
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8001 ^| findstr LISTENING') do (
    echo Stopping Backend PID %%a...
    taskkill /f /pid %%a 2>nul
)

:: Terminate process listening on frontend port 3000
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :3000 ^| findstr LISTENING') do (
    echo Stopping Frontend PID %%a...
    taskkill /f /pid %%a 2>nul
)

:: Stop the Tick Recorder window (does not use a port).
:: NOTE: The recorder flushes ticks every 60s, so at most the last minute is lost.
:: The token metadata sidecar is written by the recorder's own final-flush path;
:: if you need a guaranteed clean sidecar, close its window with Ctrl+C manually.
echo Stopping Tick Recorder...
taskkill /f /fi "WINDOWTITLE eq Alpha Tick Recorder*" 2>nul

echo.
echo [Project Alpha 2.0] System successfully stopped. All ports released.
timeout /t 2 /nobreak >nul
