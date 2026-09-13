@echo off
setlocal
cd /d "%~dp0"
py -3.12 "src\voltshift_gui.py" %*
set "TUNER_EXIT=%ERRORLEVEL%"
if not "%TUNER_EXIT%"=="0" (
    echo AMD GPU Tuner exited with code %TUNER_EXIT%.
    pause
)
exit /b %TUNER_EXIT%
