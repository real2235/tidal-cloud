@echo off
title Tidal FLAC Cloud Studio (Local Test)
color 0E
chcp 65001 >nul

echo ========================================================
echo       TIDAL FLAC MASTER - CLOUD STUDIO (LOCAL)
echo ========================================================
echo.
echo Iniciando servidor en http://localhost:8000 ...
echo.

"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%~dp0main.py"
pause
