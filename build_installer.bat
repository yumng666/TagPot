@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Build TagPot Installer

set "MAKENSIS="
if exist "%~dp0tools\nsis\makensis.exe" set "MAKENSIS=%~dp0tools\nsis\makensis.exe"
if not defined MAKENSIS if exist "%~dp0tools\nsis\Bin\makensis.exe" set "MAKENSIS=%~dp0tools\nsis\Bin\makensis.exe"
if not defined MAKENSIS where makensis >nul 2>&1 && set "MAKENSIS=makensis"

if not defined MAKENSIS (
    echo.
    echo   NSIS not found.
    echo   Download nsis-3.x.zip from https://nsis.sourceforge.io and unzip it
    echo   so that makensis.exe lands in  %~dp0tools\nsis\
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0dist\TagPot.exe" (
    echo.
    echo   dist\TagPot.exe missing. Run build_exe.bat first.
    echo.
    pause
    exit /b 1
)

"%MAKENSIS%" /V3 /INPUTCHARSET UTF8 "%~dp0installer\tagpot.nsi"
if errorlevel 1 (
    echo.
    echo   Build failed.
    pause
    exit /b 1
)

echo.
echo   Done: %~dp0dist\TagPot-Setup-1.0.0.exe
echo.
pause
