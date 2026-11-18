@echo off
cd /d "%~dp0"
echo ==== LiveCaption setup (run once on a new PC) ====

echo [1/3] Checking Python
set "PY="
where py >nul 2>nul
if errorlevel 1 goto no_launcher
set "PY=py -3"
py -3.12 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3.12"
goto have_python
:no_launcher
rem Some installs (e.g. per-user without the launcher) only put python.exe on PATH.
where python >nul 2>nul
if errorlevel 1 goto need_python
python -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>nul
if errorlevel 1 goto need_python
set "PY=python"
:have_python

echo [2/3] Preparing .venv
if not exist ".venv\Scripts\python.exe" goto make_venv
".venv\Scripts\python.exe" -c "import PySide6, yt_dlp" >nul 2>nul
if not errorlevel 1 goto venv_update
echo .venv was made on another PC - rebuilding it.
rmdir /s /q ".venv"
:make_venv
%PY% -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install --upgrade pip
:venv_update
rem Also picks up packages added to requirements.txt since the .venv was made (e.g. streamlink).
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto fail
:venv_ok
echo .venv is ready.

echo [3/3] Checking Node.js
where node >nul 2>nul
if not errorlevel 1 goto node_ok
where deno >nul 2>nul
if not errorlevel 1 goto node_ok
echo WARNING: Node.js not found. YouTube lookups may fail - install the LTS from https://nodejs.org
:node_ok

echo.
echo Done.
echo   Desktop player : run.bat
pause
exit /b 0

:need_python
echo Install Python 3.12 first: https://www.python.org/downloads/
echo Tick "Add python.exe to PATH" in the installer.
pause
exit /b 1

:fail
echo Setup failed - see the error above.
pause
exit /b 1
