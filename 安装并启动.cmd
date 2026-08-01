@echo off
setlocal
chcp 65001 >nul
title SeatSentinel

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0一键启动.ps1" -InstallPythonIfMissing %*
exit /b %ERRORLEVEL%
