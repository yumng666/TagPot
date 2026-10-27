@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Build TagPot.exe

set "PY=E:/Python/python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONPATH=%~dp0vendor"

"%PY%" -c "import sys;sys.path.insert(0,r'%~dp0vendor');import PyInstaller,webview,PIL" 2>nul
if errorlevel 1 (
    echo.
    echo   Need build deps:
    echo     "%PY%" -m pip install pyinstaller pillow
    echo     "%PY%" -m pip install --target "%~dp0vendor" pywebview piexif
    echo.
    pause
    exit /b 1
)

"%PY%" -m PyInstaller --noconfirm --onefile --windowed ^
  --name TagPot ^
  --icon "%~dp0assets\tagpot.ico" ^
  --add-data "%~dp0web;web" ^
  --add-data "%~dp0assets;assets" ^
  --hidden-import webview.platforms.edgechromium ^
  --hidden-import webview.platforms.winforms ^
  --collect-all clr_loader ^
  --collect-all pythonnet ^
  --collect-all piexif ^
  --distpath "%~dp0dist" ^
  --workpath "%~dp0build" ^
  "%~dp0app.py"

if errorlevel 1 (
    echo.
    echo   Build failed.
    pause
    exit /b 1
)

echo.
echo   Done: %~dp0dist\TagPot.exe
echo.
pause
