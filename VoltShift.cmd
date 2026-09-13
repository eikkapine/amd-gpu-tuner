@echo off
REM Compatibility launcher for existing VoltShift shortcuts.
call "%~dp0AMD-GPU-Tuner.cmd" %*
exit /b %ERRORLEVEL%
