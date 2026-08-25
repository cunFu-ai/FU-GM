@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\open_character_workshop.ps1"
if errorlevel 1 (
    echo.
    echo Character Workshop failed to start. See logs\character-workshop.err.log.
    pause
)
