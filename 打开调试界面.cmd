@echo off
setlocal
set "APP_EXE=%~dp0SeatSentinel.exe"
if not exist "%APP_EXE%" set "APP_EXE=%~dp0dist\SeatSentinel\SeatSentinel.exe"
if not exist "%APP_EXE%" (
    echo SeatSentinel.exe was not found.
    pause
    exit /b 1
)
start "" "%APP_EXE%" --debug-ui
