@echo off
setlocal
set "APP_EXE=%~dp0AwayLock.exe"
if not exist "%APP_EXE%" set "APP_EXE=%~dp0dist\AwayLock\AwayLock.exe"
if not exist "%APP_EXE%" (
    echo AwayLock.exe was not found.
    pause
    exit /b 1
)
start "" "%APP_EXE%" --debug-ui
