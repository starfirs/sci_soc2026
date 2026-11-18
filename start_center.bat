@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY=C:\ProgramData\Anaconda3\python.exe"
if not exist "%PY%" set "PY=python"
title AI Memory Companion - Control Center

"%PY%" --version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Edit PY in this file or install Python.
    pause
    exit /b 1
)

"%PY%" -c "import PyQt6" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Dependencies missing. Run:
    echo   python -m pip install --user -r requirements.txt
    pause
    exit /b 1
)

"%PY%" -m backend.control_center %*
