@echo off
setlocal
cd /d "%~dp0"

set "AURALIS_PYTHONW=pythonw.exe"
if exist "%~dp0.venv\Scripts\pythonw.exe" set "AURALIS_PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%~dp0venv\Scripts\pythonw.exe" set "AURALIS_PYTHONW=%~dp0venv\Scripts\pythonw.exe"
if "%AURALIS_PYTHONW%"=="pythonw.exe" (
    where pythonw.exe >nul 2>&1
    if errorlevel 1 (
        echo Python's windowed launcher pythonw.exe was not found.
        echo Install Python 3.11+ with PATH enabled, then install requirements.txt.
        pause
        exit /b 1
    )
)

rem Startup failures appear in a native dialog and data\desktop.log.
start "" "%AURALIS_PYTHONW%" "%~dp0desktop.pyw" %*
exit /b %errorlevel%
