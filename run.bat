@echo off
rem The default Auralis experience is Desktop.
call "%~dp0run-desktop.bat" %*
exit /b %errorlevel%
