@echo off
setlocal
REM ===================================================================
REM  Backup Status dashboard - update, build and launch in one step.
REM
REM    update-and-run.cmd            launch on http://localhost:8010
REM    update-and-run.cmd 8011       launch on a different port
REM
REM  One process serves both the API and the web UI: no second terminal.
REM  Press Ctrl+C in this window to stop it.
REM ===================================================================

set "REPO=%~dp0"
set "PORT=8010"
if not "%~1"=="" set "PORT=%~1"
set "PY=%REPO%backend\.venv\Scripts\python.exe"

cd /d "%REPO%" || goto :fail

echo.
echo === [1/4] Pulling the latest code ===
git pull --ff-only
if errorlevel 1 (
  echo.
  echo Could not fast-forward. You probably have local edits.
  echo Run "git status" to see them, then "git stash" to set them aside.
  goto :fail
)

echo.
echo === [2/4] Checking the Python environment ===
if not exist "%PY%" (
  echo No virtual environment yet - creating one...
  python -m venv "%REPO%backend\.venv" || goto :fail
)
"%PY%" -m pip install -q -r "%REPO%backend\requirements.txt" || goto :fail

echo.
echo === [3/4] Building the web UI ===
cd /d "%REPO%frontend" || goto :fail
REM install first: a pull that adds a dependency makes the build fail otherwise
call npm install --no-audit --no-fund || goto :fail
call npm run build || goto :fail

echo.
echo === [4/4] Starting the dashboard ===
echo.
echo     Open http://localhost:%PORT%
echo     Ctrl+C here stops it.
echo.
cd /d "%REPO%backend" || goto :fail
REM bound to localhost so Windows Firewall stays quiet; use --host 0.0.0.0 to
REM reach it from another machine (and allow the port through the firewall)
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
goto :eof

:fail
echo.
echo *** Stopped - see the error above. ***
pause
exit /b 1
