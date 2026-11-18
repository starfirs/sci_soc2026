@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY=C:\ProgramData\Anaconda3\python.exe"
if not exist "%PY%" set "PY=python"
title AI Memory Companion

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

:menu
cls
echo ==========================================
echo            AI Memory Companion
echo ==========================================
echo   1. Start desktop pet (real data)
echo   2. Demo mode (3 sample cards)
echo   3. Start screen capture (Ctrl+C to stop)
echo   4. Start audio capture (Ctrl+C to stop)
echo   5. Run tests
echo   6. Open USAGE.md
echo   7. Import PDF/DOCX/TXT/MD
echo   8. List / view memory nodes
echo   9. Run nightly (consolidate + next cards)
echo  10. Open control center
echo   0. Exit
echo.
set "CHOICE="
set /p CHOICE=Select [0-10]:
if "%CHOICE%"=="1" goto pet
if "%CHOICE%"=="2" goto demo
if "%CHOICE%"=="3" goto screen
if "%CHOICE%"=="4" goto audio
if "%CHOICE%"=="5" goto test
if "%CHOICE%"=="6" goto docs
if "%CHOICE%"=="7" goto import
if "%CHOICE%"=="8" goto memories
if "%CHOICE%"=="9" goto nightly
if "%CHOICE%"=="10" goto center
if "%CHOICE%"=="0" goto end
goto menu

:pet
"%PY%" -m backend.desktop_pet
goto menu

:demo
call "%~dp0start_demo.bat"
goto menu

:screen
start "AI Screen Capture" cmd /k "%PY% -m backend.screen_capture --ocr-backend local"
goto menu

:audio
start "AI Audio Capture" cmd /k "%PY% -m backend.audio_capture --source both"
goto menu

:test
"%PY%" -m pytest -q
pause
goto menu

:docs
start "" notepad "%~dp0USAGE.md"
goto menu

:import
"%PY%" -m backend.import_doc --pick
pause
goto menu

:memories
"%PY%" -m backend.node_edit --list
set "NODE_ID="
set /p NODE_ID=Node id to view (empty to skip):
if not "%NODE_ID%"=="" (
    "%PY%" -m backend.node_edit --id %NODE_ID% --show
)
pause
goto menu

:nightly
"%PY%" -m backend.nightly
pause
goto menu

:center
call "%~dp0start_center.bat"
goto menu

:end
exit /b 0
