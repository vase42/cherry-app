@echo off
REM Puts a Cherry icon on your Desktop (and one in this folder).
REM You only need to run this once.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0system\make_shortcut.ps1"
echo.
pause
