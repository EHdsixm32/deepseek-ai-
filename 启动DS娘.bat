@echo off
setlocal
cd /d "%~dp0"

set "PYEXE="
if exist "venv\Scripts\pythonw.exe" set "PYEXE=venv\Scripts\pythonw.exe"
if not defined PYEXE if exist "venv\Scripts\python.exe" set "PYEXE=venv\Scripts\python.exe"
if not defined PYEXE (
    where pythonw.exe >nul 2>nul && set "PYEXE=pythonw.exe"
)
if not defined PYEXE set "PYEXE=python.exe"

echo Starting DS Assistant...
start "DS Assistant" "%PYEXE%" "%~dp0run.py"
exit /b
