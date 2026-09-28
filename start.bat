@echo off
setlocal
cd /d "%~dp0"
title BrandBatch

rem ---------- pick Python (3.10+)
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto :havepy
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
:havepy
if not defined PY (
    echo Python 3.10 or newer is required: https://www.python.org/downloads/
    pause & exit /b 1
)

rem ---------- one-time setup
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" || (
        echo Python 3.10 or newer is required. Found an older version.
        pause & exit /b 1
    )
    %PY% -m venv .venv || ( echo Could not create the virtual environment. & pause & exit /b 1 )
)
set "VPY=%~dp0.venv\Scripts\python.exe"

echo Checking dependencies...
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt || (
    echo Dependency install failed. Check your internet connection and try again.
    pause & exit /b 1
)

rem ---------- persistent secret key so logins survive restarts
if not exist ".secret_key" "%VPY%" -c "import secrets; open('.secret_key','w').write(secrets.token_hex(32))"
set /p SECRET_KEY=<.secret_key
set "DATABASE_URL=sqlite:///%~dp0brandbatch.db"
set "STORAGE_DIR=%~dp0storage"

rem ---------- start the render worker in its own window
start "BrandBatch worker (keep open)" cmd /k ""%VPY%" worker.py"

rem ---------- open the browser shortly after the server starts
start "" /min cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000"

echo.
echo  BrandBatch is running at http://127.0.0.1:8000
echo  Keep this window and the "BrandBatch worker" window open.
echo  Press Ctrl+C here to stop the website, then close the worker window.
echo.
"%VPY%" -m flask --app wsgi run --host 127.0.0.1 --port 8000
pause
