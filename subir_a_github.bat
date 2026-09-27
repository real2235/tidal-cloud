@echo off
title Subir Tidal Cloud a GitHub
color 0B
chcp 65001 >nul

set "GIT_CMD=C:\Program Files\Git\cmd\git.exe"
if not exist "%GIT_CMD%" set "GIT_CMD=git"

echo ========================================================
echo        SUBIR PROYECTO TIDAL CLOUD A GITHUB
echo ========================================================
echo.
echo Repositorio: https://github.com/real2235/tidal-cloud.git
set "REPO_URL=https://github.com/real2235/tidal-cloud.git"
echo.
echo [1/3] Configurando enlace remoto...
"%GIT_CMD%" remote remove origin >nul 2>&1
"%GIT_CMD%" remote add origin %REPO_URL%

echo [2/3] Preparando rama principal (main)...
"%GIT_CMD%" branch -M main

echo [3/3] Subiendo archivos a GitHub...
echo (Si se abre una ventana en tu pantalla, autoriza el acceso a tu cuenta de GitHub)
echo.
"%GIT_CMD%" push -u origin main

if %errorlevel% equ 0 (
    echo.
    echo ========================================================
    echo  PROYECTO SUBIDO CON EXITO A GITHUB!
    echo.
    echo  Ahora ve a Render.com y conectalo en 1 clic:
    echo  1. Entra a https://render.com
    echo  2. Dale a 'New +' -> 'Web Service'
    echo  3. Elige tu repositorio 'tidal-cloud' y dale a 'Deploy'
    echo ========================================================
) else (
    echo.
    echo [AVISO] Si te pidio iniciar sesion, autorizalo en la ventana que aparecio.
)

echo.
pause
