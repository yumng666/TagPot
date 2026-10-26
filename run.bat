@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Tag Pot

set "PY="
if exist "E:\Python\python.exe" set "PY=E:\Python\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY set "PY=python"

rem ---- check runtime deps: WebView2 shell (vendor) + Pillow ----
"%PY%" -c "import sys;sys.path.insert(0,r'%~dp0vendor');import webview,PIL" 2>nul
if errorlevel 1 goto nodep

rem ---- launch without leaving a console window ----
set "PYW=%PY:python.exe=pythonw.exe%"
if exist "%PYW%" goto launchw
start "" "%PY%" "%~dp0app.py"
exit /b 0

:launchw
start "" "%PYW%" "%~dp0app.py"
exit /b 0

:nodep
echo.
echo   Missing runtime dependencies for Tag Pot.
echo   Current Python: %PY%
echo.
echo   Run these once:
echo.
echo     "%PY%" -m pip install pillow
echo     "%PY%" -m pip install --target "%~dp0vendor" pywebview
echo.
pause
exit /b 1
