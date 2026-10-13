@echo off
REM ============================================================
REM  Nobitex Smart Trading Bot - one-click launcher (Windows)
REM  Double-click to run. Installing missing dependencies is automatic.
REM  The bot runs QUIETLY in this window (no per-event printing, no extra
REM  status() per cycle) so it stays light; watch the data in the browser
REM  dashboard. Stop it with Ctrl+C here, or by simply closing this window
REM  (both stop the engine before the process goes away).
REM ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
title Nobitex Smart Trading Bot

cd /d "%~dp0"

REM  The dashboard port is resolved after Python is found (step 2):
REM  config.json (engine.server_port) is the single source of truth so
REM  this file and the bot can never drift apart; NOBOT_PORT overrides
REM  it for a single run.

REM ------------------------------------------------------------
REM 1) Find a Python launcher (py -3, python, python3)
REM ------------------------------------------------------------
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY python3 -c "import sys" >nul 2>&1 && set "PY=python3"

if not defined PY (
  echo [X] Python 3 not found on this PC.
  echo.
  choice /C YN /M "Download and install Python 3.12 now (opens python.org)"
  if errorlevel 2 (
    echo Cancelled. Install Python from https://www.python.org/downloads/
    echo then re-run start.bat. Remember to tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
  start "" https://www.python.org/downloads/
  echo.
  echo After installing Python, tick "Add python.exe to PATH" in the
  echo installer, finish the setup, then close this window and run
  echo start.bat again.
  pause
  exit /b 1
)

echo [i] Python launcher: !PY!

REM ------------------------------------------------------------
REM 2) Dashboard port: config.json -> engine.server_port
REM    (NOBOT_PORT, e.g. `set NOBOT_PORT=9000`, overrides for one run)
REM ------------------------------------------------------------
set "PORT="
if "%NOBOT_PORT%" NEQ "" set "PORT=%NOBOT_PORT%"
if not defined PORT (
  for /f "usebackq delims=" %%P in (`!PY! -c "import json;print(json.load(open('config.json',encoding='utf-8'))['engine']['server_port'])"`) do set "PORT=%%P"
)
if not defined PORT set "PORT=8787"

echo.
echo ============================================================
echo   Nobitex Smart Trading Bot
echo   Dashboard : http://127.0.0.1:%PORT%
echo   Mode      : see config.json (demo / live)
echo   Stop      : Ctrl+C here, or close this window
echo ============================================================
echo.

REM ------------------------------------------------------------
REM 3) Self-heal dependencies: install anything missing
REM ------------------------------------------------------------
!PY! -c "import requests" >nul 2>&1 || (
  echo [i] Installing dependency: requests ...
  !PY! -m pip install --quiet --disable-pip-version-check requests || (
    echo [X] pip install failed. Check your internet connection and retry.
    pause
    exit /b 1
  )
)

!PY! -c "import nobitex_bot" >nul 2>&1 || (
  echo [i] Initializing project modules ...
  !PY! -c "import sys; sys.path.insert(0,'.'); import nobitex_bot; print('ok')" >nul 2>&1 || (
    echo [X] Project modules could not load - see message above.
    pause
    exit /b 1
  )
)

REM ------------------------------------------------------------
REM 4) If another copy of the bot is already listening on this
REM    port, tell the user instead of failing obscurely later.
REM ------------------------------------------------------------
netstat -ano | %SystemRoot%\System32\find.exe ":%PORT%" | %SystemRoot%\System32\find.exe "LISTENING" >nul 2>&1 && (
  echo.
  echo [X] Port %PORT% is already in use - the bot is probably already running.
  echo     A window titled "Nobitex Smart Trading Bot" may be open, maybe minimized.
  echo     If you are sure no other copy is running, close that window first,
  echo     then run start.bat again.
  echo.
  start "" "http://127.0.0.1:%PORT%"
  pause
  exit /b 0
)

REM ------------------------------------------------------------
REM 5) Start engine + dashboard in THIS window (quiet: no live view)
REM ------------------------------------------------------------
echo [i] Starting bot on port %PORT% ...
echo [i] Data: http://127.0.0.1:%PORT%  ^|  stop: Ctrl+C here or close this window.
echo.

!PY! -X utf8 run.py --port %PORT%
set RC=!errorlevel!

REM If the engine exits with an error and the port is now taken,
REM show a friendly hint instead of closing silently.
if !RC! NEQ 0 (
  netstat -ano | %SystemRoot%\System32\find.exe ":%PORT%" | %SystemRoot%\System32\find.exe "LISTENING" >nul 2>&1 && (
    echo.
    echo   شروع نشد: به نظر می‌رسد یک نسخه دیگر از ربات روی همین پورت روشن است.
    echo   پنجره‌ای با عنوان "Nobitex Smart Trading Bot" را پیدا و ببندید،
    echo   سپس start.bat را دوباره اجرا کنید.
    echo   برای دیدن شماره پروسه:  netstat -ano ^| findstr :%PORT%
    echo   برای بستن آن:          taskkill /F /PID [number]
    echo.
  )
)

echo.
echo Bot stopped. You can close this window now.
pause
