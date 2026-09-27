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
echo 1. Asegúrate de haber creado un repositorio nuevo en GitHub:
echo    https://github.com/new
echo.
echo 2. Pega aquí el enlace HTTPS de tu repositorio de GitHub
echo    (Ejemplo: https://github.com/tu-usuario/tidal-cloud.git)
echo.
set /p REPO_URL="Enlace del repositorio: "

if "%REPO_URL%"=="" (
    echo.
    echo [ERROR] No introdujiste ningún enlace. Operación cancelada.
    pause
    exit /b
)

echo.
echo [1/3] Configurando enlace remoto...
"%GIT_CMD%" remote remove origin >nul 2>&1
"%GIT_CMD%" remote add origin %REPO_URL%

echo [2/3] Preparando rama principal (main)...
"%GIT_CMD%" branch -M main

echo [3/3] Subiendo archivos a GitHub...
echo.
"%GIT_CMD%" push -u origin main

if %errorlevel% equ 0 (
    echo.
    echo ========================================================
    echo  ¡PROYECTO SUBIDO CON ÉXITO A GITHUB!
    echo.
    echo  Ahora ve a Render.com y conéctalo en 1 clic:
    echo  1. Entra a https://render.com
    echo  2. Dale a 'New +' -> 'Web Service'
    echo  3. Elige tu repositorio y dale a 'Deploy Web Service'
    echo ========================================================
) else (
    echo.
    echo [ERROR] Hubo un problema al subir a GitHub.
    echo Verifica que el enlace sea correcto y que hayas iniciado sesión en GitHub.
)

echo.
pause
