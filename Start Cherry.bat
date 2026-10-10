@echo off
chcp 65001 >nul
title Cherry
cd /d "%~dp0system"

REM Find a Python that actually runs.
REM
REM Windows ships a fake python.exe stub (an "App execution alias") that exists
REM on PATH but only prints an advert for the Microsoft Store. So it is not
REM enough to check the command exists - it has to be run. `py`, the launcher
REM that python.org installs, sidesteps the stub entirely and is tried second.
set "PY="
python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY py -c "import sys" >nul 2>&1 && set "PY=py"

if not defined PY (
  echo.
  echo   Python was not found.
  echo.
  echo   1. Install it:  winget install --id Python.Python.3.12 -e
  echo   2. CLOSE this window and every PowerShell window, then try again.
  echo.
  echo   Still failing? Windows is intercepting the command. Open
  echo   Settings ^> Apps ^> Advanced app settings ^> App execution aliases
  echo   and switch OFF "python.exe" and "python3.exe".
  echo.
  pause
  exit /b 1
)

:run
%PY% app\server.py
set CODE=%ERRORLEVEL%
REM 3 means "updated - start me again".
if "%CODE%"=="3" goto run

if not "%CODE%"=="0" (
  echo.
  echo   Cherry stopped with an error ^(code %CODE%^).
  echo.
  pause
)
