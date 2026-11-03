@echo off
setlocal
cd /d "%~dp0"

set "AURALIS_PYTHON=python.exe"
if exist "%~dp0.venv\Scripts\python.exe" set "AURALIS_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0venv\Scripts\python.exe" set "AURALIS_PYTHON=%~dp0venv\Scripts\python.exe"
if "%AURALIS_PYTHON%"=="python.exe" (
    where python.exe >nul 2>&1
    if errorlevel 1 (
        echo Python was not found on PATH. Install Python 3.11+ and try again.
        pause
        exit /b 1
    )
)

rem --plain overrides --tui when the basic terminal prompt is preferred.
"%AURALIS_PYTHON%" "%~dp0start.py" --tui %*
exit /b %errorlevel%
