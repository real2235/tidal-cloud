import os
import sys
import time
import json
import uuid
import shutil
import zipfile
import logging
import asyncio
import threading
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, Any, Union, List

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import uvicorn

BASE_DIR = Path(__file__).resolve().parent

# Asegurar que tiddl use siempre el directorio de la aplicación
if not os.environ.get("TIDDL_PATH"):
    os.environ["TIDDL_PATH"] = str(BASE_DIR)

# Sincronizar tiddl.json entre BASE_DIR y Home si uno de los dos existe
try:
    _local_cfg = BASE_DIR / "tiddl.json"
    _home_cfg = Path.home() / "tiddl.json"
    if _local_cfg.exists() and not _home_cfg.exists():
        shutil.copy2(_local_cfg, _home_cfg)
    elif _home_cfg.exists() and not _local_cfg.exists():
        shutil.copy2(_home_cfg, _local_cfg)
except Exception:
    pass

# Tidal dependencies
from tiddl.config import Config as TidalConfig, AuthConfig as TidalAuthConfig, CONFIG_PATH
from tiddl.auth import getDeviceAuth, get_auth_credentials, AUTH_URL
from tiddl.api import TidalApi
from tiddl.download import parseTrackStream, downloadTrackStream
from tiddl.metadata import addMetadata, Cover
from tiddl.utils import sanitizeString

# Optimizar búfer de transmisión de archivos
FileResponse.chunk_size = 512 * 1024

# Configurar Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("TidalCloud")

STATIC_DIR = BASE_DIR / "static"
TEMP_DIR = BASE_DIR / "temp_downloads"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Comprobar si hay credenciales preconfiguradas por variable de entorno (Render.com)
def init_tidal_env_config():
    raw_env = os.environ.get("TIDAL_CONFIG_JSON", "").strip()
    if not raw_env:
        return
    if (raw_env.startswith("'") and raw_env.endswith("'")) or (raw_env.startswith('"') and raw_env.endswith('"')):
        raw_env = raw_env[1:-1].strip()
    try:
        data = json.loads(raw_env)
        # Si contiene 'auth' o el tiddl.json completo
        if isinstance(data, dict) and "auth" in data and isinstance(data["auth"], dict):
            with open(BASE_DIR / "tiddl.json", "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            try:
                shutil.copy2(BASE_DIR / "tiddl.json", Path.home() / "tiddl.json")
            except Exception:
                pass
            logger.info("[TIDAL] Archivo tiddl.json completo configurado desde TIDAL_CONFIG_JSON.")
            return

        cfg = TidalConfig.fromFile()
        auth_data = data.get("auth", data) if isinstance(data, dict) else {}
        token = auth_data.get("token") or auth_data.get("access_token")
        if token:
            cfg.auth = TidalAuthConfig(
                token=token,
                refresh_token=auth_data.get("refresh_token", ""),
                expires=int(auth_data.get("expires", 0)) or (int(time.time()) + 86400 * 30),
                user_id=str(auth_data.get("user_id", "")),
                country_code=str(auth_data.get("country_code", "CO") or "CO"),
            )
            cfg.save()
            try:
                shutil.copy2(CONFIG_PATH, BASE_DIR / "tiddl.json")
                shutil.copy2(CONFIG_PATH, Path.home() / "tiddl.json")
            except Exception:
                pass
            logger.info(f"[TIDAL] Credenciales aplicadas exitosamente para usuario ID: {cfg.auth.user_id} (País: {cfg.auth.country_code})")
    except Exception as e:
        logger.error(f"[TIDAL] Error procesando TIDAL_CONFIG_JSON: {e}")

init_tidal_env_config()

def ensure_tidal_auth_loaded(cfg: TidalConfig) -> TidalConfig:
    if cfg.auth.token:
        return cfg
    # Reintento 1: Cargar directamente tiddl.json local
    for path in [BASE_DIR / "tiddl.json", Path.home() / "tiddl.json"]:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                auth_d = d.get("auth") if isinstance(d, dict) and "auth" in d else d
                token = auth_d.get("token") or auth_d.get("access_token") if isinstance(auth_d, dict) else None
                if token:
                    cfg.auth = TidalAuthConfig(
                        token=token,
                        refresh_token=auth_d.get("refresh_token", ""),
                        expires=int(auth_d.get("expires", 0)) or (int(time.time()) + 86400 * 30),
                        user_id=str(auth_d.get("user_id", "")),
                        country_code=str(auth_d.get("country_code", "CO") or "CO"),
                    )
                    cfg.save()
                    logger.info(f"[TIDAL] Cuenta cargada automáticamente desde {path}")
                    return cfg
            except Exception as e:
                logger.error(f"[TIDAL] Error leyendo {path}: {e}")
    # Reintento 2: Intentar variable de entorno de nuevo
    init_tidal_env_config()
    return TidalConfig.fromFile()


# Tareas en memoria (aisladas por sesión/ID)
tasks: Dict[str, Dict[str, Any]] = {}

app = FastAPI(
    title="Tidal FLAC Master • Cloud Studio",
    description="Descargador de audio en FLAC Hi-Res 24-bit y Lossless 16-bit sin pérdida",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Modelos Pydantic
class SearchRequest(BaseModel):
    query: str
    type: Optional[str] = "all"

class TidalDownloadRequest(BaseModel):
    url: str
    quality: Optional[str] = "master"  # 'master' (24-bit) o 'hifi' (16-bit)

class TidalInfoRequest(BaseModel):
    url: str


# ==========================================
# UTILIDADES
# ==========================================

def clean_filename(name: str) -> str:
    """Limpia caracteres inválidos para nombres de archivo"""
    invalid_chars = '<>:"/\\|?*'
    for ch in invalid_chars:
        name = name.replace(ch, "_")
    return name.strip(". ")

def format_duration(seconds: Optional[Union[int, float]]) -> str:
    """Convierte segundos a formato MM:SS"""
    if not seconds:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def format_size(bytes_size: int) -> str:
    """Formatea bytes a formato legible (MB, KB)"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} TB"

def clean_old_temp_dirs():
    """Elimina carpetas temporales de descargas mayores a 30 minutos"""
    now = time.time()
    for item in TEMP_DIR.iterdir():
        if item.is_dir():
            try:
                if now - item.stat().st_mtime > 1800:
                    shutil.rmtree(item, ignore_errors=True)
            except Exception:
                pass


# ==========================================
# GESTIÓN DE AUTENTICACIÓN TIDAL
# ==========================================

def refresh_tidal_oauth_token(refresh_token_str: str) -> Optional[Dict[str, Any]]:
    client_id, client_secret = get_auth_credentials()
    url = f"{AUTH_URL}/token"
    try:
        resp = requests.post(
            url,
            data={
                "client_id": client_id,
                "refresh_token": refresh_token_str,
                "grant_type": "refresh_token",
                "scope": "r_usr+w_usr+w_sub",
            },
            auth=(client_id, client_secret) if client_secret else None,
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"[TIDAL] Error refrescando token: {e}")
    return None

def get_active_tidal_api() -> TidalApi:
    cfg = ensure_tidal_auth_loaded(TidalConfig.fromFile())
    if not cfg.auth.token:
        raise HTTPException(
            status_code=401,
            detail="No se ha vinculado la cuenta de Tidal. Por favor conecta una cuenta primero.",
        )

    if cfg.auth.refresh_token and time.time() > cfg.auth.expires:
        logger.info("[TIDAL] Token expirado, renovando con refresh_token...")
        refreshed = refresh_tidal_oauth_token(cfg.auth.refresh_token)
        if refreshed and "access_token" in refreshed:
            cfg.auth.token = refreshed["access_token"]
            cfg.auth.expires = int(time.time()) + refreshed.get("expires_in", 86400)
            if "refresh_token" in refreshed:
                cfg.auth.refresh_token = refreshed["refresh_token"]
            cfg.save()
            logger.info("[TIDAL] Token renovado con éxito.")

    return TidalApi(
        token=cfg.auth.token,
        user_id=cfg.auth.user_id,
        country_code=cfg.auth.country_code or "US",
    )


@app.get("/api/tidal/status")
async def tidal_status():
    """Verifica si la cuenta de Tidal está conectada"""
    try:
        cfg = ensure_tidal_auth_loaded(TidalConfig.fromFile())
        is_logged = bool(cfg.auth.token)
        return {
            "logged_in": is_logged,
            "user_id": cfg.auth.user_id if is_logged else None,
            "country_code": cfg.auth.country_code if is_logged else None,
            "expires": cfg.auth.expires,
        }
    except Exception as e:
        return {"logged_in": False, "error": str(e)}

@app.post("/api/tidal/auth/start")
async def tidal_auth_start():
    """Inicia el flujo OAuth para vincular cuenta Tidal"""
    try:
        device_auth = getDeviceAuth()
        return {
            "device_code": device_auth.deviceCode,
            "user_code": device_auth.userCode,
            "verification_url": f"https://{device_auth.verificationUriComplete}",
            "expires_in": device_auth.expiresIn,
            "interval": device_auth.interval,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error iniciando autenticación: {str(e)}")

@app.post("/api/tidal/auth/poll")
async def tidal_auth_poll(data: Dict[str, str]):
    """Comprueba si el usuario ya autorizó el dispositivo en Tidal"""
    device_code = data.get("device_code")
    if not device_code:
        raise HTTPException(status_code=400, detail="device_code requerido")

    client_id, client_secret = get_auth_credentials()
    url = f"{AUTH_URL}/token"
    try:
        resp = requests.post(
            url,
            data={
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "scope": "r_usr+w_usr+w_sub",
            },
            auth=(client_id, client_secret) if client_secret else None,
            timeout=15,
        )
        res_json = resp.json()
        if resp.status_code == 200:
            cfg = TidalConfig.fromFile()
            cfg.auth = TidalAuthConfig(
                token=res_json["access_token"],
                refresh_token=res_json.get("refresh_token"),
                expires=int(time.time()) + res_json.get("expires_in", 86400),
                user_id=res_json.get("user", {}).get("userId"),
                country_code=res_json.get("user", {}).get("countryCode", "US"),
            )
            cfg.save()
            return {"status": "success", "user_id": cfg.auth.user_id}
        elif res_json.get("error") == "authorization_pending":
            return {"status": "pending"}
        else:
            return {"status": "error", "detail": res_json.get("error_description", "Error desconocido")}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.post("/api/tidal/logout")
async def tidal_logout():
    try:
        cfg = TidalConfig.fromFile()
        cfg.auth = TidalAuthConfig()
        cfg.save()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# BÚSQUEDA Y METADATOS DE TIDAL
# ==========================================

@app.post("/api/tidal/search")
@app.get("/api/tidal/search")
async def tidal_search(
    req: Optional[SearchRequest] = None,
    query: Optional[str] = None,
    type: Optional[str] = None
):
    """Buscador en catálogo de Tidal HiFi & Master"""
    term = (query or (req.query if req else "") or "").strip()
    search_type = (type or (req.type if req else "") or "all").lower().strip()
    if not term:
        raise HTTPException(status_code=400, detail="Escribe un término de búsqueda.")

    api = get_active_tidal_api()

    def do_search():
        import tiddl.api
        return api.fetch(
            tiddl.api.Search,
            "search",
            {"countryCode": api.country_code, "query": term, "limit": 30},
            expire_after=tiddl.api.EXPIRE_IMMEDIATELY,
        )

    loop = asyncio.get_event_loop()
    s = await loop.run_in_executor(None, do_search)

    tracks = []
    if search_type in ["all", "tracks", "track", "songs", "song"] and hasattr(s, "tracks") and s.tracks and hasattr(s.tracks, "items") and s.tracks.items:
        for t in s.tracks.items[:30]:
            c_uid = t.album.cover.replace("-", "/") if (t.album and t.album.cover) else None
            cover = f"https://resources.tidal.com/images/{c_uid}/640x640.jpg" if c_uid else None
            artists = ", ".join([a.name for a in t.artists]) if t.artists else (t.artist.name if t.artist else "Desconocido")
            album_title = t.album.title if t.album else "Sencillo"
            tracks.append({
                "id": str(t.id),
                "title": t.title,
                "artist": artists,
                "album": album_title,
                "duration": format_duration(t.duration),
                "duration_seconds": t.duration,
                "quality": t.audioQuality,
                "cover": cover,
                "cover_url": cover,
                "url": f"https://tidal.com/browse/track/{t.id}",
            })

    albums = []
    if search_type in ["all", "albums", "album"] and hasattr(s, "albums") and s.albums and hasattr(s.albums, "items") and s.albums.items:
        for a in s.albums.items[:25]:
            c_uid = a.cover.replace("-", "/") if a.cover else None
            cover = f"https://resources.tidal.com/images/{c_uid}/640x640.jpg" if c_uid else None
            artists = ", ".join([ar.name for ar in a.artists]) if a.artists else (a.artist.name if a.artist else "Desconocido")
            albums.append({
                "id": str(a.id),
                "title": a.title,
                "artist": artists,
                "tracks_count": a.numberOfTracks,
                "duration": format_duration(a.duration),
                "release_date": str(a.releaseDate) if getattr(a, "releaseDate", None) else "",
                "cover": cover,
                "cover_url": cover,
                "url": f"https://tidal.com/browse/album/{a.id}",
            })

    return {"tracks": tracks, "albums": albums}


# ==========================================
# MOTOR DE DESCARGA (AISLADO POR TAREA)
# ==========================================

def parse_tidal_url(url: str):
    import re
    m_track = re.search(r"track/(\d+)", url)
    if m_track:
        return "track", m_track.group(1)
    m_album = re.search(r"album/(\d+)", url)
    if m_album:
        return "album", m_album.group(1)
    raise ValueError("El enlace no corresponde a una pista o álbum de Tidal válido.")

def download_stream_with_progress(track_stream, on_progress=None):
    urls, file_extension = parseTrackStream(track_stream)
    stream_data = bytearray()
    with requests.Session() as s:
        total = len(urls)
        for i, u in enumerate(urls):
            r = s.get(u, timeout=30)
            r.raise_for_status()
            stream_data.extend(r.content)
            if on_progress:
                on_progress((i + 1) / total)
    return bytes(stream_data), file_extension

def download_single_flac_track(api: TidalApi, track, target_dir: Path, quality_mode: str, cover_data: bytes = b"", on_subprogress=None) -> Path:
    try:
        track_stream = api.getTrackStream(track.id, quality_mode)
    except Exception:
        track_stream = api.getTrackStream(track.id, "LOSSLESS")

    def stream_cb(frac: float):
        if on_subprogress:
            on_subprogress(frac * 0.75, "Descargando stream FLAC...")

    stream_bytes, file_ext = download_stream_with_progress(track_stream, on_progress=stream_cb)

    artist_name = clean_filename(track.artist.name if track.artist else "Artista")
    track_title = clean_filename(track.title)

    if track.trackNumber:
        base_name = f"{track.trackNumber:02d}. {track_title}"
    else:
        base_name = f"{artist_name} - {track_title}"

    raw_file = target_dir / f"{base_name}_temp{file_ext}"
    final_flac_file = target_dir / f"{base_name}.flac"

    with open(raw_file, "wb") as f:
        f.write(stream_bytes)

    if on_subprogress:
        on_subprogress(0.85, "Remuxando con FFmpeg a FLAC...")

    import subprocess
    cmd = ["ffmpeg", "-y", "-i", str(raw_file), str(final_flac_file)]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if raw_file.exists():
        raw_file.unlink(missing_ok=True)
    if res.returncode != 0 or not final_flac_file.exists():
        raise Exception("Fallo en la remuxación a FLAC")

    # Letras sincronizadas (.lrc)
    lyrics_text = ""
    try:
        lyrics_obj = api.getLyrics(track.id)
        if lyrics_obj:
            lyrics_text = getattr(lyrics_obj, "subtitles", "") or getattr(lyrics_obj, "lyrics", "") or ""
            if getattr(lyrics_obj, "subtitles", None):
                lrc_path = target_dir / f"{base_name}.lrc"
                with open(lrc_path, "w", encoding="utf-8") as f:
                    f.write(lyrics_obj.subtitles)
    except Exception:
        pass

    # Metadatos ID3 / Vorbis
    if on_subprogress:
        on_subprogress(0.95, "Incrustando metadatos y carátula...")

    try:
        artist_str = track.artist.name if track.artist else ""
        addMetadata(
            track_path=final_flac_file,
            track=track,
            cover_data=cover_data,
            album_artist=artist_str,
            lyrics=lyrics_text,
        )
    except Exception as e:
        logger.warning(f"[TIDAL] Aviso metadatos: {e}")

    if on_subprogress:
        on_subprogress(1.0, "Pista lista")

    return final_flac_file


def run_tidal_track_task(task_id: str, track_id: str, api: TidalApi, quality_mode: str):
    task_dir = TEMP_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        track = api.getTrack(track_id)
        tasks[task_id]["title"] = track.title
        tasks[task_id]["artist"] = track.artist.name if track.artist else "Artista"

        cover_data = b""
        if track.album and track.album.cover:
            try:
                c_uid = track.album.cover.replace("-", "/")
                c_url = f"https://resources.tidal.com/images/{c_uid}/1280x1280.jpg"
                res_cov = requests.get(c_url, timeout=15)
                if res_cov.status_code == 200:
                    cover_data = res_cov.content
            except Exception:
                pass

        def on_subprogress(pct: float, msg: str):
            tasks[task_id]["percent"] = round(pct * 100, 1)
            tasks[task_id]["status"] = f"{msg} ({int(pct*100)}%)"

        final_flac_file = download_single_flac_track(
            api=api,
            track=track,
            target_dir=task_dir,
            quality_mode=quality_mode,
            cover_data=cover_data,
            on_subprogress=on_subprogress,
        )

        filesize = final_flac_file.stat().st_size
        filename = final_flac_file.name

        tasks[task_id]["completed"] = True
        tasks[task_id]["percent"] = 100.0
        tasks[task_id]["status"] = "¡Descarga completada!"
        tasks[task_id]["result"] = {
            "type": "track",
            "filename": filename,
            "title": track.title,
            "artist": track.artist.name if track.artist else "Artista",
            "album": track.album.title if track.album else "Sencillo",
            "filesize": format_size(filesize),
            "quality": track.audioQuality,
            "download_url": f"/api/download/{task_id}/{urllib.parse.quote(filename)}",
        }
    except Exception as e:
        logger.error(f"[TIDAL] Error en tarea {task_id}: {e}")
        tasks[task_id]["error"] = str(e)
        tasks[task_id]["status"] = f"Error: {str(e)}"
        tasks[task_id]["completed"] = True


def run_tidal_album_task(task_id: str, album_id: str, api: TidalApi, quality_mode: str):
    task_dir = TEMP_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        album = api.getAlbum(album_id)
        album_items = api.getAlbumItems(album_id)
        tracks = [item.item for item in album_items.items if hasattr(item, "item")]
        total_tracks = len(tracks)

        album_artist = clean_filename(album.artist.name if album.artist else "Varios Artistas")
        album_title = clean_filename(album.title)
        album_folder_name = f"{album_artist} - {album_title}"
        album_dir = task_dir / album_folder_name
        album_dir.mkdir(parents=True, exist_ok=True)

        tasks[task_id]["title"] = album.title
        tasks[task_id]["artist"] = album.artist.name if album.artist else "Varios Artistas"

        cover_data = b""
        if album.cover:
            try:
                c_uid = album.cover.replace("-", "/")
                c_url = f"https://resources.tidal.com/images/{c_uid}/1280x1280.jpg"
                res_cov = requests.get(c_url, timeout=15)
                if res_cov.status_code == 200:
                    cover_data = res_cov.content
                    with open(album_dir / "cover.jpg", "wb") as f_cov:
                        f_cov.write(cover_data)
            except Exception:
                pass

        for idx, track in enumerate(tracks):
            tasks[task_id]["status"] = f"Descargando ({idx+1}/{total_tracks}): {track.title}"
            base_pct = (idx / total_tracks) * 90.0

            def on_track_prog(frac: float, msg: str):
                cur_pct = base_pct + (frac * (90.0 / total_tracks))
                tasks[task_id]["percent"] = round(cur_pct, 1)

            try:
                download_single_flac_track(
                    api=api,
                    track=track,
                    target_dir=album_dir,
                    quality_mode=quality_mode,
                    cover_data=cover_data,
                    on_subprogress=on_track_prog,
                )
            except Exception as e:
                logger.error(f"[TIDAL ALBUM] Error en pista {track.title}: {e}")

        # Empaquetar todo el álbum en un archivo .ZIP
        tasks[task_id]["status"] = "Empaquetando álbum en archivo .ZIP..."
        tasks[task_id]["percent"] = 95.0

        zip_filename = f"{album_folder_name}.zip"
        zip_path = task_dir / zip_filename
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for file_path in album_dir.rglob("*"):
                if file_path.is_file():
                    arcname = file_path.relative_to(album_dir)
                    zip_file.write(file_path, arcname)

        total_size = format_size(zip_path.stat().st_size)

        tasks[task_id]["completed"] = True
        tasks[task_id]["percent"] = 100.0
        tasks[task_id]["status"] = "¡Álbum completado y empaquetado en .ZIP!"
        tasks[task_id]["result"] = {
            "type": "album",
            "album_title": album.title,
            "filename": zip_filename,
            "tracks_downloaded": total_tracks,
            "total_size": total_size,
            "download_url": f"/api/download/{task_id}/{urllib.parse.quote(zip_filename)}",
        }
    except Exception as e:
        logger.error(f"[TIDAL] Error en álbum {task_id}: {e}")
        tasks[task_id]["error"] = str(e)
        tasks[task_id]["status"] = f"Error: {str(e)}"
        tasks[task_id]["completed"] = True


@app.post("/api/tidal/download")
async def download_tidal(req: TidalDownloadRequest):
    """Inicia la descarga de audio en FLAC auténtico en segundo plano"""
    url = req.url.strip()
    try:
        res_type, res_id = parse_tidal_url(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Enlace de Tidal inválido: {str(e)}")

    quality_mode = "HI_RES_LOSSLESS" if req.quality == "master" else "LOSSLESS"
    api = get_active_tidal_api()

    clean_old_temp_dirs()
    task_id = uuid.uuid4().hex
    tasks[task_id] = {
        "id": task_id,
        "type": f"tidal_{res_type}",
        "status": "Iniciando proceso...",
        "percent": 0.0,
        "completed": False,
        "error": None,
        "result": None,
        "created_at": time.time(),
    }

    if res_type == "track":
        threading.Thread(
            target=run_tidal_track_task,
            args=(task_id, res_id, api, quality_mode),
            daemon=True,
        ).start()
    else:
        threading.Thread(
            target=run_tidal_album_task,
            args=(task_id, res_id, api, quality_mode),
            daemon=True,
        ).start()

    return {"task_id": task_id, "status": "Iniciando descarga..."}


@app.get("/api/task-status/{task_id}")
async def get_task_status(task_id: str):
    """Consulta el progreso en tiempo real de una tarea específica"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Tarea no encontrada o expirada.")
    return tasks[task_id]


def cleanup_task_dir(task_id: str):
    """Limpia el directorio temporal de la tarea después de que el usuario lo descarga"""
    def _delayed_delete():
        time.sleep(120)  # Espera 2 minutos para asegurar que la descarga del navegador haya terminado
        task_dir = TEMP_DIR / task_id
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)
            logger.info(f"[CLEANUP] Tarea {task_id} y archivos temporales eliminados del servidor.")
    threading.Thread(target=_delayed_delete, daemon=True).start()


@app.get("/api/download/{task_id}/{filename}")
async def download_file_by_task(task_id: str, filename: str, background_tasks: BackgroundTasks):
    """Descarga directa aislada por tarea con limpieza automática de disco"""
    task_dir = TEMP_DIR / task_id
    decoded_filename = urllib.parse.unquote(filename)
    file_path = task_dir / decoded_filename

    # Prevenir Directory Traversal
    if not file_path.resolve().is_relative_to(task_dir.resolve()):
        raise HTTPException(status_code=403, detail="Acceso denegado.")

    if not file_path.exists() or not file_path.is_file():
        # Puede ser un archivo dentro del zip o carpeta
        candidates = list(task_dir.rglob(decoded_filename))
        if candidates:
            file_path = candidates[0]
        else:
            raise HTTPException(status_code=404, detail="Archivo temporal no encontrado.")

    ext = file_path.suffix.lower()
    media_type = "audio/flac" if ext == ".flac" else "application/zip"

    # Programar la limpieza automática del disco
    background_tasks.add_task(cleanup_task_dir, task_id)

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{file_path.name}"'},
    )


# ==========================================
# RUTA PRINCIPAL
# ==========================================

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="index.html no encontrado")
    return FileResponse(str(index_file))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Iniciando Tidal Cloud Studio en el puerto {port}...")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
