@echo off
title Project Alpha 2.0 Launcher
cd /d "e:\Website\Alpha_3.0"

echo ========================================================
echo  [Project Alpha 2.0] Starting Engine & UI at %DATE% %TIME%
echo ========================================================

:: 1. Ensure Docker Desktop is running
docker info >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Docker daemon is offline. Starting Docker Desktop...
    if exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" (
        start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
        timeout /t 12 /nobreak >nul
    )
)

:: Ensure Redis and PostgreSQL containers are active
echo Starting Redis and PostgreSQL containers...
docker compose up -d

:: 2. Launch Backend Daemon on port 8001
echo Starting Backend Engine on port 8001...
start "Alpha Backend (Port 8001)" /min cmd /c "cd /d e:\Website\Alpha_3.0 && python backend/main.py"

:: Wait 4 seconds for backend to bind port 8001
timeout /t 4 /nobreak >nul

:: 3. Launch Frontend Dev Server on port 3000
echo Starting Frontend UI on port 3000...
start "Alpha Frontend (Port 3000)" /min cmd /c "cd /d e:\Website\Alpha_3.0\frontend && npm run dev"

:: Wait 3 seconds for Vite dev server
timeout /t 3 /nobreak >nul

:: 4. Open Advisory Terminal in default browser
echo Opening Live Advisory Terminal at http://localhost:3000/ ...
start http://localhost:3000/

echo [Alpha 2.0] Startup sequence complete!
