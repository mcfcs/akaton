@echo off
rem Start Akaton: the Discord bot, the dashboard, and the scheduler are one process
rem (`akaton run`), so this launches all three.
rem
rem If the dashboard port is already held - usually a previous run that was closed
rem without stopping - that listener is killed first, because uvicorn would otherwise
rem exit immediately with "address already in use".
rem
rem Run it from anywhere; it switches to its own folder so config and .env resolve.

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PORT="
set "HOST="

rem Read the port and host from .env so this keeps working if you change them there.
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
        if /i "%%A"=="DASHBOARD_PORT" set "PORT=%%B"
        if /i "%%A"=="DASHBOARD_HOST" set "HOST=%%B"
    )
) else (
    echo [ERROR] No .env found in "%CD%".
    echo     Copy .env.example to .env and fill in the Discord values first.
    exit /b 1
)

rem Trim stray spaces or a trailing carriage return.
for /f "tokens=* delims= " %%V in ("!PORT!") do set "PORT=%%V"
for /f "tokens=* delims= " %%V in ("!HOST!") do set "HOST=%%V"
set "PORT=!PORT: =!"
set "HOST=!HOST: =!"
if "!PORT!"=="" set "PORT=8765"
if "!HOST!"=="" set "HOST=127.0.0.1"

echo === Akaton ===
echo   folder    : %CD%
echo   dashboard : http://!HOST!:!PORT!

rem The package has gone missing from site-packages before, and the console script
rem then fails with a bare ModuleNotFoundError. Say what to do about it.
python -c "import akaton" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] The akaton package is not importable by this Python.
    echo     Fix it with:  python -m pip install -e .
    exit /b 1
)

rem Discovery is silent without SearXNG rather than loud, so warn but do not block:
rem the bot and dashboard are still worth running.
python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8888/',timeout=3).status_code<500 else 1)" >nul 2>&1
if errorlevel 1 (
    echo   [warning] SearXNG is not answering on 127.0.0.1:8888.
    echo             Web search will find nothing until you start it:
    echo             docker compose -f compose.free.yaml up -d searxng
)

rem Free the port. Only LISTENING sockets on this exact port are considered, and each
rem one is named before it is killed so nothing is terminated silently.
set "FOUND="
for /f "tokens=5" %%P in ('netstat -ano -p TCP ^| findstr /r /c:":!PORT! .*LISTENING"') do (
    if not "%%P"=="0" (
        set "FOUND=1"
        for /f "tokens=1 delims=," %%N in ('tasklist /fi "PID eq %%P" /fo csv /nh') do (
            echo   port !PORT! is held by %%~N ^(pid %%P^) - stopping it
        )
        taskkill /F /PID %%P >nul 2>&1
        if errorlevel 1 (
            echo   [ERROR] Could not stop pid %%P. Try again from an elevated prompt.
            exit /b 1
        )
    )
)
if defined FOUND (
    rem Give Windows a moment to release the socket before uvicorn binds it. `ping` is
    rem used rather than `timeout`, which aborts with "Input redirection is not
    rem supported" whenever this script is launched with a redirected stdin.
    ping -n 3 127.0.0.1 >nul 2>&1
) else (
    echo   port !PORT! is free
)

echo.
echo Starting bot, dashboard and scheduler. Press Ctrl+C to stop.
echo.
rem Clear any errorlevel left by the checks above so CODE reflects akaton alone.
cmd /c exit 0
akaton run
set "CODE=%ERRORLEVEL%"

echo.
if "%CODE%"=="0" (
    echo Akaton exited normally.
) else (
    echo Akaton exited with code %CODE%.
)
endlocal & exit /b %CODE%
