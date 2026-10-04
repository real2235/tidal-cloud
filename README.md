---
title: Tidal FLAC Master Studio
emoji: 🎵
colorFrom: yellow
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Tidal FLAC Master • Cloud Studio

Servidor de descarga de música en calidad audiófila **FLAC 24-bit (Hi-Res)** y **16-bit (Lossless)** optimizado para desplegar gratuitamente en **Hugging Face Spaces (16 GB RAM / 50 GB Disco)** o **Render.com**.

---

## 🚀 Opción Recomendada: Despliegue en Hugging Face Spaces (Gratis, Sin Límite de 5 GB)

1. Crea una cuenta gratuita en [huggingface.co](https://huggingface.co/).
2. Haz clic en tu perfil (arriba a la derecha) → **"New Space"**.
3. Configura tu Space:
   * **Space name:** `tidal-flac-studio` (o el nombre que prefieras).
   * **Select the Space SDK:** Selecciona **Docker** → **Blank**.
   * **Space Hardware:** `CPU basic · 2 vCPU · 16 GB · FREE`.
   * **Visibility:** **Public** (o Private).
4. Sube los archivos de esta carpeta `tidal-cloud` directamente desde la pestaña **"Files"** → **"Add file"** → **"Upload files"** (o por Git) incluyendo:
   * `Dockerfile`
   * `README.md`
   * `main.py`
   * `requirements.txt`
   * `tiddl.json`
   * Carpeta `static/` (`index.html`)
5. ¡Listo! Hugging Face construirá el contenedor Docker en ~2 minutos y tendrás una URL directa (`https://TU_USUARIO-tidal-flac-studio.hf.space`) sin el límite de 5 GB de Render.

---

## 🌐 Alternativa: Despliegue en Render.com

1. Sube esta carpeta a un repositorio de GitHub.
2. En [render.com](https://render.com/), haz clic en **"New +"** → **"Web Service"** y conecta el repositorio.
3. Selecciona **Docker** y el plan **Free**. Render inyectará su variable `$PORT` automáticamente.

---

## 💻 Prueba Local en tu PC
Simplemente haz doble clic en `iniciar_local.bat` y abre `http://localhost:8000` en tu navegador.

