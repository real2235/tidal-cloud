# Tidal FLAC Master • Cloud Studio

Servidor de descarga de música en calidad audiófila **FLAC 24-bit (Hi-Res)** y **16-bit (Lossless)** optimizado para desplegar gratuitamente en **Render.com**.

---

## 🚀 Despliegue en Render.com (100% Gratis)

### Paso 1: Subir a GitHub
1. Crea un repositorio nuevo en tu cuenta de GitHub (puede ser público o privado), por ejemplo llamado `tidal-cloud`.
2. Sube esta carpeta a tu repositorio:
   ```bash
   git remote add origin https://github.com/TU_USUARIO/tidal-cloud.git
   git branch -M main
   git push -u origin main
   ```

### Paso 2: Crear el Web Service en Render
1. Entra a [render.com](https://render.com/) e inicia sesión con tu cuenta de GitHub.
2. Haz clic en **"New +"** y selecciona **"Web Service"**.
3. Selecciona tu repositorio `tidal-cloud`.
4. En **Language / Environment**, selecciona **Docker** (Render detectará el `Dockerfile` automáticamente con FFmpeg y Python).
5. En **Plan**, selecciona **Free ($0/month)**.

### Paso 3: Vincular tu cuenta de Tidal en la Nube
Tienes dos formas sencillas:
* **Opción A (Automática):** En la configuración de tu servicio en Render, ve a la sección **"Environment Variables"** y añade:
  * Key: `TIDAL_CONFIG_JSON`
  * Value: (Pega el contenido de tu archivo local `tiddl.json`)
* **Opción B (Desde la web):** Cuando la página esté online, entra al enlace de Render, haz clic en **"Cuenta Tidal"** y aprueba con el código de verificación en tu celular o PC.

---

## 💻 Prueba Local en tu PC
Simplemente haz doble clic en `iniciar_local.bat` y abre `http://localhost:8000` en tu navegador.
