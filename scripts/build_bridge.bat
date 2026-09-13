@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_bridge.ps1"
set "TUNER_EXIT=%ERRORLEVEL%"
if not "%TUNER_EXIT%"=="0" pause
exit /b %TUNER_EXIT%
