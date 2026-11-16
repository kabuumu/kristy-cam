@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "HOST=127.0.0.1"
set "PORT=8420"
set "BASE_URL=http://%HOST%:%PORT%"
set "HEALTH_URL=%BASE_URL%/api/health"

REM 1. Single-instance / port-conflict guard
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "try {" ^
  "  $resp = Invoke-WebRequest -Uri '%HEALTH_URL%' -TimeoutSec 2 -UseBasicParsing;" ^
  "  if ($resp.StatusCode -eq 200 -and $resp.Content -match 'sims-vertical-studio') { exit 10 }" ^
  "  else { exit 11 }" ^
  "} catch [System.Net.WebException] {" ^
  "  $tcp = New-Object System.Net.Sockets.TcpClient;" ^
  "  try { $iar = $tcp.BeginConnect('%HOST%', %PORT%, $null, $null); if ($iar.AsyncWaitHandle.WaitOne(500)) { $tcp.EndConnect($iar); exit 11 } } catch {} finally { $tcp.Close() }" ^
  "  exit 0" ^
  "} catch { exit 0 }"

if %errorlevel% equ 10 (
    echo Sims 4 Vertical Video Studio is already running at %BASE_URL%. Opening browser...
    start "" "%BASE_URL%"
    exit /b 0
)
if %errorlevel% equ 11 (
    echo [ERROR] Port %PORT% on %HOST% is already in use by another application.
    pause
    exit /b 1
)

REM 2. Ensure uv is installed
where uv >nul 2>nul
if %errorlevel% neq 0 (
    echo Installing uv package manager...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

where uv >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Failed to locate uv after installation.
    pause
    exit /b 1
)

REM 3. Sync locked Python 3.12 environment
echo Verifying locked Python environment...
uv sync --locked
if %errorlevel% neq 0 (
    echo [ERROR] Failed to sync Python dependencies. Check internet connection on first run.
    pause
    exit /b 1
)

REM 4. Bootstrap FFmpeg, bundled fonts, and Kokoro ONNX models
echo Verifying runtime assets and Kokoro models...
uv run python -m backend.bootstrap
if %errorlevel% neq 0 (
    echo [ERROR] Runtime asset or FFmpeg verification failed.
    pause
    exit /b 1
)

REM 5. Open browser once /api/health responds, then run uvicorn in foreground
echo Starting Sims 4 Vertical Video Studio at %BASE_URL%...
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "for ($i = 0; $i -lt 30; $i++) {" ^
  "  try { $r = Invoke-WebRequest -Uri '%HEALTH_URL%' -TimeoutSec 1 -UseBasicParsing; if ($r.StatusCode -eq 200) { Start-Process '%BASE_URL%'; break } } catch {}" ^
  "  Start-Sleep -Milliseconds 500" ^
  "}"

uv run uvicorn backend.main:app --host %HOST% --port %PORT%
if %errorlevel% neq 0 (
    echo [ERROR] Studio server exited with an error.
    pause
    exit /b 1
)
