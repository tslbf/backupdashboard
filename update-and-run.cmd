@echo off
setlocal EnableExtensions
REM ===================================================================
REM  Backup Status dashboard - update, build and launch in one step.
REM
REM    update-and-run.cmd            pull, build, launch on port 8010
REM    update-and-run.cmd 8011       ...on a different port
REM    update-and-run.cmd demo       ...against a separate demo database
REM    update-and-run.cmd demo 8011  both
REM
REM  One process serves both the API and the web UI: no second terminal.
REM  Press Ctrl+C in this window to stop it.
REM ===================================================================

set "REPO=%~dp0"
set "PORT=8010"
set "DEMO="

:parseargs
if "%~1"=="" goto :doneargs
if /i "%~1"=="demo" (set "DEMO=1") else (set "PORT=%~1")
shift
goto :parseargs
:doneargs

set "PY=%REPO%backend\.venv\Scripts\python.exe"

cd /d "%REPO%" || goto :fail

echo.
echo === [1/5] Checking prerequisites ===
where git >nul 2>&1
if errorlevel 1 (
  echo   git is not on PATH.
  goto :fail
)
where node >nul 2>&1
if errorlevel 1 (
  echo   Node.js is not on PATH - install Node 18 or newer.
  goto :fail
)
if not exist "%PY%" (
  where python >nul 2>&1
  if errorlevel 1 (
    echo   Python is not on PATH - install Python 3.11 or newer.
    goto :fail
  )
)
echo   ok

echo.
echo === [2/5] Pulling the latest code ===
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo   Not a git checkout - skipping the pull.
) else (
  git pull --ff-only
  if errorlevel 1 (
    echo.
    echo   Could not fast-forward. You probably have local edits.
    echo   Run "git status" to see them, then "git stash" to set them aside.
    goto :fail
  )
)

echo.
echo === [3/5] Checking the Python environment ===
if not exist "%PY%" (
  echo   No virtual environment yet - creating one...
  python -m venv "%REPO%backend\.venv" || goto :fail
)
REM "python -m pip" rather than bare "pip": the latter hits Access Denied
REM under the owner's execution policy.
"%PY%" -m pip install -q -r "%REPO%backend\requirements.txt" || goto :fail
echo   ok

echo.
echo === [4/5] Building the web UI ===
cd /d "%REPO%frontend" || goto :fail
REM install first: a pull that adds a dependency otherwise fails the build
REM with "Rollup failed to resolve import".
call npm install --no-audit --no-fund || goto :fail
call npm run build || goto :fail

cd /d "%REPO%backend" || goto :fail

if defined DEMO (
  REM A separate database on purpose. seed-demo REPLACES the whole estate, so
  REM pointing it at the real one would delete collected history. This env var
  REM also overrides whatever APP_DB_URL is set to in .env.
  set "APP_DB_URL=sqlite:///./demo.db"
  echo.
  echo === [5/5] Loading demo data ===
  "%PY%" -m app.cli seed-demo || goto :fail
  echo   ~49 fake servers over 75 nights, in backend\demo.db
) else (
  echo.
  echo === [5/5] Preparing the database ===
  "%PY%" -m app.cli init-db || goto :fail
)

echo.
echo ===================================================
echo    Open http://localhost:%PORT%
if defined DEMO echo    [demo data - your real database is untouched]
echo    Ctrl+C here stops it.
echo ===================================================
echo.

REM Bound to localhost so Windows Firewall stays quiet. Use --host 0.0.0.0 to
REM reach it from another machine, and allow the port through the firewall.
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
goto :eof

:fail
echo.
echo *** Stopped - see the error above. ***
pause
exit /b 1
