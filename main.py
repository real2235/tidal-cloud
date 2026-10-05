import os
import re
import sys
import time
import json
import uuid
import shutil
import zipfile
import logging
import asyncio
import threading
import subprocess
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, Any, Union, List, Tuple

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from mutagen.flac import FLAC, Picture
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
    naming_format: Optional[str] = "full"  # 'full' o 'title_only'

class TidalBatchDownloadRequest(BaseModel):
    track_ids: List[str]
    quality: Optional[str] = "master"
    naming_format: Optional[str] = "full" 

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

def make_content_disposition(filename: str, disposition: str = "attachment") -> str:
    """Genera un header Content-Disposition compatible con RFC 6266 / RFC 5987 seguro para caracteres UTF-8"""
    import re
    ascii_name = re.sub(r'[^\x20-\x7E]', '_', filename).replace('"', '')
    encoded_name = urllib.parse.quote(filename, encoding='utf-8')
    return f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'

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
    """Elimina carpetas temporales de descargas mayores a 8 minutos y limpia tareas antiguas en memoria"""
    now = time.time()
    for item in TEMP_DIR.iterdir():
        if item.is_dir():
            try:
                if now - item.stat().st_mtime > 480:
                    shutil.rmtree(item, ignore_errors=True)
            except Exception:
                pass
    expired_ids = [tid for tid, tinfo in list(tasks.items()) if now - tinfo.get("created_at", now) > 900]
    for tid in expired_ids:
        tasks.pop(tid, None)


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

def get_active_tidal_api(force_refresh: bool = False) -> TidalApi:
    cfg = ensure_tidal_auth_loaded(TidalConfig.fromFile())
    if not cfg.auth.token:
        raise HTTPException(
            status_code=401,
            detail="No se ha vinculado la cuenta de Tidal. Por favor conecta una cuenta primero.",
        )

    if force_refresh or (cfg.auth.refresh_token and time.time() > cfg.auth.expires):
        logger.info("[TIDAL] Token expirado o renovación forzada con refresh_token...")
        refreshed = refresh_tidal_oauth_token(cfg.auth.refresh_token)
        if refreshed and "access_token" in refreshed:
            cfg.auth.token = refreshed["access_token"]
            cfg.auth.expires = int(time.time()) + refreshed.get("expires_in", 86400)
            if "refresh_token" in refreshed:
                cfg.auth.refresh_token = refreshed["refresh_token"]
            cfg.save()
            logger.info("[TIDAL] Token renovado con éxito.")
        elif not force_refresh:
            raise HTTPException(
                status_code=401,
                detail="La sesión de Tidal expiró. Vuelve a iniciar sesión.",
            )

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
        params = {"countryCode": api.country_code, "query": term, "limit": 30}
        req = api.session.get(
            f"{api.URL}/search",
            params=params,
            expire_after=tiddl.api.EXPIRE_IMMEDIATELY,
        )
        if req.status_code != 200:
            raise Exception(f"Tidal API error ({req.status_code})")
        return req.json()

    loop = asyncio.get_event_loop()
    s_data = await loop.run_in_executor(None, do_search)

    tracks = []
    tracks_raw = s_data.get("tracks", {}).get("items", []) if isinstance(s_data, dict) else []
    if search_type in ["all", "tracks", "track", "songs", "song"]:
        for t in tracks_raw[:30]:
            album_info = t.get("album") or {}
            c_uid = album_info.get("cover")
            cover = f"https://resources.tidal.com/images/{c_uid.replace('-', '/')}/640x640.jpg" if c_uid else None
            artist_list = t.get("artists") or []
            artists = ", ".join([a.get("name", "") for a in artist_list if a.get("name")]) if artist_list else (t.get("artist", {}).get("name") if t.get("artist") else "Desconocido")
            album_title = album_info.get("title") or "Sencillo"
            tracks.append({
                "id": str(t.get("id")),
                "title": t.get("title"),
                "artist": artists,
                "album": album_title,
                "duration": format_duration(t.get("duration")),
                "duration_seconds": t.get("duration"),
                "quality": t.get("audioQuality", "LOSSLESS"),
                "cover": cover,
                "cover_url": cover,
                "url": f"https://tidal.com/browse/track/{t.get('id')}",
            })

    albums = []
    albums_raw = s_data.get("albums", {}).get("items", []) if isinstance(s_data, dict) else []
    if search_type in ["all", "albums", "album"]:
        for a in albums_raw[:25]:
            c_uid = a.get("cover")
            cover = f"https://resources.tidal.com/images/{c_uid.replace('-', '/')}/640x640.jpg" if c_uid else None
            artist_list = a.get("artists") or []
            artists = ", ".join([ar.get("name", "") for ar in artist_list if ar.get("name")]) if artist_list else (a.get("artist", {}).get("name") if a.get("artist") else "Desconocido")
            albums.append({
                "id": str(a.get("id")),
                "title": a.get("title"),
                "artist": artists,
                "tracks_count": a.get("numberOfTracks"),
                "duration": format_duration(a.get("duration")),
                "release_date": str(a.get("releaseDate")) if a.get("releaseDate") else "",
                "cover": cover,
                "cover_url": cover,
                "url": f"https://tidal.com/browse/album/{a.get('id')}",
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

def fetch_netease_lyrics(artist: str, title: str) -> Optional[str]:
    """Capa 3 de Respaldo: Consulta la base de datos de NetEase Cloud Music para letras sincronizadas"""
    try:
        clean_t = re.sub(r"\s*[\(\[](remastered|explicit|deluxe|bonus|version|anniversary|edit|live|mono|stereo|feat\..*?|ft\..*?).*?[\)\]]", "", title, flags=re.IGNORECASE).strip()
        clean_t = re.sub(r"\s*-\s*(remastered|deluxe|bonus).*?$", "", clean_t, flags=re.IGNORECASE).strip() or title
        clean_a = artist.split(',')[0].split('&')[0].strip()
        q = urllib.parse.quote(f"{clean_a} {clean_t}".strip())
        search_url = f"https://music.163.com/api/search/get/web?s={q}&type=1&limit=3"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        r = requests.get(search_url, headers=headers, timeout=6)
        if r.status_code == 200:
            data = r.json()
            songs = data.get("result", {}).get("songs", [])
            if songs:
                song_id = songs[0]["id"]
                l_url = f"https://music.163.com/api/song/lyric?os=pc&id={song_id}&lv=-1&kv=-1&tv=-1"
                l_res = requests.get(l_url, headers=headers, timeout=6)
                if l_res.status_code == 200:
                    lyric = l_res.json().get("lrc", {}).get("lyric", "")
                    if lyric and "[" in lyric:
                        logger.info(f"[NETEASE] Letras sincronizadas encontradas para '{clean_a} - {clean_t}'")
                        return lyric
    except Exception as e:
        logger.debug(f"[NETEASE] Error buscando letras: {e}")
    return None


def safe_get_lyrics(api: TidalApi, track_id: Union[str, int], track = None) -> Optional[Dict[str, Any]]:
    """Obtiene letras sincronizadas (.lrc) o texto plano desde Tidal con fallback global a LRCLIB"""
    # 1. Tidal oficial
    try:
        url = f"{api.URL}/tracks/{track_id}/lyrics"
        params = {"countryCode": api.country_code or "CO"}
        req = api.session.get(url, params=params, timeout=8)
        if req.status_code == 200:
            data = req.json()
            subtitles = data.get("subtitles") or ""
            plain_lyrics = data.get("lyrics") or ""
            if subtitles or plain_lyrics:
                return {
                    "has_lyrics": True,
                    "is_synced": bool(subtitles),
                    "subtitles": subtitles,
                    "lyrics": plain_lyrics,
                    "text": subtitles if subtitles else plain_lyrics
                }
    except Exception as e:
        logger.debug(f"[TIDAL] Error consultando letras oficiales para {track_id}: {e}")

    # 2. Fallback a LRCLIB (Servicio global de letras sincronizadas .lrc)
    try:
        if not track:
            try:
                track = api.getTrack(track_id)
            except Exception:
                track = None

        if track:
            track_title = getattr(track, "title", "")
            artist_name = ""
            if hasattr(track, "artist") and track.artist:
                artist_name = track.artist.name
            elif hasattr(track, "artists") and track.artists and len(track.artists) > 0:
                artist_name = track.artists[0].name

            album_title = ""
            if hasattr(track, "album") and track.album:
                album_title = getattr(track.album, "title", "")

            duration = getattr(track, "duration", None)

            clean_title = re.sub(r"\s*[\(\[](remastered|explicit|deluxe|bonus|version|anniversary|edit|live|mono|stereo|feat\..*?|ft\..*?).*?[\)\]]", "", track_title, flags=re.IGNORECASE).strip()
            clean_title = re.sub(r"\s*-\s*(remastered|deluxe|bonus).*?$", "", clean_title, flags=re.IGNORECASE).strip() or track_title

            clean_artist = artist_name.split(',')[0].split('&')[0].split(' feat.')[0].strip() or artist_name

            p = {"track_name": clean_title, "artist_name": clean_artist}
            if album_title:
                p["album_name"] = album_title
            if duration:
                p["duration"] = int(duration)

            res = requests.get("https://lrclib.net/api/get", params=p, headers={"User-Agent": "TidalFLACStudio/2.0"}, timeout=5.0)
            if res.status_code == 200:
                d = res.json()
                synced = d.get("syncedLyrics")
                plain = d.get("plainLyrics")
                if synced or plain:
                    return {
                        "has_lyrics": True,
                        "is_synced": bool(synced),
                        "subtitles": synced or "",
                        "lyrics": plain or "",
                        "text": synced if synced else plain
                    }

            # Búsqueda abierta si no hubo coincidencia exacta
            res_sr = requests.get("https://lrclib.net/api/search", params={"q": f"{clean_artist} {clean_title}"}, headers={"User-Agent": "TidalFLACStudio/2.0"}, timeout=5.0)
            if res_sr.status_code == 200:
                items = res_sr.json()
                if isinstance(items, list) and items:
                    for it in items:
                        if it.get("syncedLyrics"):
                            return {
                                "has_lyrics": True,
                                "is_synced": True,
                                "subtitles": it["syncedLyrics"],
                                "lyrics": it.get("plainLyrics") or "",
                                "text": it["syncedLyrics"]
                            }
                    return {
                        "has_lyrics": True,
                        "is_synced": False,
                        "subtitles": "",
                        "lyrics": items[0].get("plainLyrics") or "",
                        "text": items[0].get("plainLyrics") or ""
                    }
    except Exception as e:
        logger.debug(f"[LRCLIB] Fallback error para {track_id}: {e}")

    # 3. Fallback a Lyrics.ovh (Texto plano)
    try:
        if track:
            t_title = getattr(track, "title", "")
            t_art = getattr(track.artist, "name", "") if (hasattr(track, "artist") and track.artist) else ""
            if not t_art and hasattr(track, "artists") and track.artists and len(track.artists) > 0:
                t_art = track.artists[0].name
            c_t = re.sub(r"\s*[\(\[].*?[\)\]]", "", t_title).strip() or t_title
            c_a = t_art.split(',')[0].split('&')[0].split(' feat.')[0].strip() or t_art
            if c_a and c_t:
                r_ovh = requests.get(f"https://api.lyrics.ovh/v1/{urllib.parse.quote(c_a)}/{urllib.parse.quote(c_t)}", timeout=5.0)
                if r_ovh.status_code == 200:
                    d_ovh = r_ovh.json()
                    lyr = d_ovh.get("lyrics", "").strip()
                    if lyr:
                        return {
                            "has_lyrics": True,
                            "is_synced": False,
                            "subtitles": "",
                            "lyrics": lyr,
                            "text": lyr,
                        }
    except Exception as e_ovh:
        logger.debug(f"[LYRICS.OVH] Fallback error: {e_ovh}")

    return None

def resolve_track_cover_bytes(api: TidalApi, track, initial_cover: bytes = b"") -> bytes:
    """Obtiene los bytes JPEG de la carátula del álbum en alta resolución con múltiples capas de respaldo."""
    if initial_cover and len(initial_cover) > 256:
        return initial_cover

    cover_uid = None
    if getattr(track, "album", None) and getattr(track.album, "cover", None):
        cover_uid = track.album.cover
    elif getattr(track, "album", None) and getattr(track.album, "id", None):
        try:
            alb = api.getAlbum(track.album.id)
            if alb and getattr(alb, "cover", None):
                cover_uid = alb.cover
        except Exception:
            pass

    if cover_uid:
        for sz in (1280, 640):
            try:
                c_obj = Cover(cover_uid, size=sz)
                if c_obj.content and len(c_obj.content) > 256:
                    return c_obj.content
            except Exception:
                pass
            try:
                uid_path = str(cover_uid).replace("-", "/")
                url = f"https://resources.tidal.com/images/{uid_path}/{sz}x{sz}.jpg"
                r = requests.get(url, timeout=10)
                if r.status_code == 200 and len(r.content) > 256:
                    return r.content
            except Exception:
                pass

    # Respaldo final: buscar carátula HD en iTunes Search API
    try:
        t_title = getattr(track, "title", "") or ""
        t_artist = track.artist.name if getattr(track, "artist", None) else ""
        q = f"{t_artist} {t_title}".strip()
        if q:
            it_url = f"https://itunes.apple.com/search?term={urllib.parse.quote(q)}&entity=song&limit=1"
            r = requests.get(it_url, timeout=6)
            if r.status_code == 200:
                results = r.json().get("results", [])
                if results:
                    art_url = results[0].get("artworkUrl100", "")
                    if art_url:
                        hd_url = art_url.replace("100x100bb", "1000x1000bb")
                        img_r = requests.get(hd_url, timeout=8)
                        if img_r.status_code == 200 and len(img_r.content) > 256:
                            return img_r.content
    except Exception:
        pass

    return b""


def download_single_flac_track(
    api: TidalApi,
    track,
    target_dir: Path,
    quality_mode: str,
    cover_data: bytes = b"",
    on_subprogress=None,
    naming_format: str = "full",
    album_title_fallback: Optional[str] = None
) -> Tuple[Path, Optional[Path]]:
    track_stream = None
    stream_err = None

    for attempt in range(2):
        try:
            track_stream = api.getTrackStream(track.id, quality_mode)
            break
        except Exception as e:
            stream_err = e
            try:
                track_stream = api.getTrackStream(track.id, "LOSSLESS")
                break
            except Exception as e2:
                stream_err = e2

            err_str = (str(e) + " " + str(e2)).lower()
            if ("privileges" in err_str or "403" in err_str or "401" in err_str) and attempt == 0:
                logger.warning(f"[TIDAL] Advertencia de streaming ({e}). Pausando 2s, renovando sesión y reintentando...")
                time.sleep(2.0)
                try:
                    api = get_active_tidal_api(force_refresh=True)
                except Exception as ref_err:
                    logger.error(f"[TIDAL] Error forzando refresco de token: {ref_err}")
            else:
                if attempt == 1:
                    raise Exception(f"No se pudo obtener el stream de audio: {stream_err}")
                time.sleep(1.0)

    if track_stream is None:
        raise Exception(f"Fallo al obtener stream de audio para {getattr(track, 'title', 'pista')}: {stream_err}")

    def stream_cb(frac: float):
        if on_subprogress:
            on_subprogress(frac * 0.75, "Descargando stream FLAC...")

    stream_bytes, file_ext = download_stream_with_progress(track_stream, on_progress=stream_cb)

    # 1. Artista
    artist_name = "Artista"
    if hasattr(track, "artist") and track.artist and getattr(track.artist, "name", None):
        artist_name = track.artist.name
    elif hasattr(track, "artists") and track.artists and len(track.artists) > 0 and getattr(track.artists[0], "name", None):
        artist_name = track.artists[0].name
    artist_name = clean_filename(artist_name)

    # 2. Álbum
    album_name = "Album"
    if hasattr(track, "album") and track.album and getattr(track.album, "title", None):
        album_name = track.album.title
    elif album_title_fallback:
        album_name = album_title_fallback
    album_name = clean_filename(album_name)

    # 3. Título de la pista
    track_title = clean_filename(track.title)

    # 4. Número de pista (ej. 01, 02, 15)
    t_num = getattr(track, "trackNumber", None)
    try:
        track_num = f"{int(t_num):02d}" if t_num is not None else "01"
    except Exception:
        track_num = "01"

    # Seleccionar nombre base según formato solicitado por el usuario
    if naming_format == "title_only":
        base_name = f"{track_title}"
    else:
        base_name = f"{artist_name} - {album_name} - {track_num} - {track_title}"

    raw_file = target_dir / f"{base_name}_temp{file_ext}"
    final_flac_file = target_dir / f"{base_name}.flac"

    with open(raw_file, "wb") as f:
        f.write(stream_bytes)
    # Liberar buffer en RAM inmediatamente
    del stream_bytes

    if on_subprogress:
        on_subprogress(0.85, "Remuxando con FFmpeg a FLAC...")

    # Intentar primero remux rápido sin recodificar (-c:a copy) y respaldar con codificación FLAC rápida
    cmd_copy = ["ffmpeg", "-y", "-i", str(raw_file), "-vn", "-c:a", "copy", str(final_flac_file)]
    res = subprocess.run(cmd_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 or not final_flac_file.exists() or final_flac_file.stat().st_size < 1024:
        cmd_enc = ["ffmpeg", "-y", "-i", str(raw_file), "-vn", "-c:a", "flac", "-compression_level", "5", str(final_flac_file)]
        res = subprocess.run(cmd_enc, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if res.returncode == 0 and final_flac_file.exists():
        raw_file.unlink(missing_ok=True)
    else:
        raw_file.unlink(missing_ok=True)
        raise Exception("Fallo en la remuxación a FLAC")

    # Letras sincronizadas (.lrc) oficiales o fallback global (Tidal -> LRCLIB -> NetEase)
    lyrics_text = ""
    lrc_file_path: Optional[Path] = None
    lyr_info = safe_get_lyrics(api, track.id, track=track)
    if not lyr_info or not lyr_info.get("has_lyrics"):
        ne_lrc = fetch_netease_lyrics(artist_name, track.title)
        if ne_lrc:
            lyr_info = {"has_lyrics": True, "is_synced": True, "text": ne_lrc}

    if lyr_info and lyr_info.get("has_lyrics"):
        lyrics_text = lyr_info.get("text", "")
        lrc_file_path = target_dir / f"{base_name}.lrc"
        try:
            with open(lrc_file_path, "w", encoding="utf-8") as f_lrc:
                f_lrc.write(lyrics_text)
            logger.info(f"[TIDAL] Letra sincronizada (.lrc) guardada: {lrc_file_path.name}")
        except Exception as e:
            logger.warning(f"Error escribiendo archivo .lrc: {e}")
            lrc_file_path = None

    # Garantizar siempre carátula HD (incluso desde la Cola de Descargas)
    cover_data = resolve_track_cover_bytes(api, track, cover_data)

    # Metadatos ID3 / Vorbis + Carátula garantizada
    if on_subprogress:
        on_subprogress(0.95, "Incrustando metadatos, letras y carátula...")

    artist_str = track.artist.name if getattr(track, "artist", None) else ""
    if not artist_str and getattr(track, "artists", None):
        artist_str = track.artists[0].name

    try:
        addMetadata(
            track_path=final_flac_file,
            track=track,
            cover_data=cover_data,
            album_artist=artist_str,
            lyrics=lyrics_text,
        )
    except Exception as e:
        logger.warning(f"[TIDAL] Aviso addMetadata primario: {e}")

    try:
        audio = FLAC(str(final_flac_file))
        if not audio.get("TITLE") and getattr(track, "title", None):
            audio["TITLE"] = [track.title]
        if not audio.get("ARTIST") and artist_str:
            audio["ARTIST"] = [artist_str]
        if not audio.get("ALBUM") and album_name:
            audio["ALBUM"] = [album_name]
        if not audio.get("ALBUMARTIST") and artist_str:
            audio["ALBUMARTIST"] = [artist_str]
        if not audio.get("TRACKNUMBER") and t_num is not None:
            audio["TRACKNUMBER"] = [str(t_num)]

        if cover_data and len(audio.pictures) == 0:
            pic = Picture()
            pic.type = 3  # Front Cover
            pic.mime = "image/jpeg"
            pic.desc = "Front Cover"
            pic.data = cover_data
            audio.add_picture(pic)
            logger.info(f"[METADATA] Carátula incrustada directamente en {final_flac_file.name}")

        if lyrics_text:
            audio["UNSYNCEDLYRICS"] = [lyrics_text]
            audio["SYNCEDLYRICS"] = [lyrics_text]

        query = f"{track.title} {artist_str}"
        itunes_url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&entity=song&limit=1"
        res_gen = requests.get(itunes_url, timeout=3.5)
        if res_gen.status_code == 200:
            data_gen = res_gen.json()
            if data_gen.get("resultCount", 0) > 0:
                genre = data_gen["results"][0].get("primaryGenreName")
                if genre:
                    audio["GENRE"] = [genre]
        audio.save()
    except Exception as ex_m:
        logger.debug(f"[METADATA EXT] Advertencia: {ex_m}")

    if on_subprogress:
        on_subprogress(1.0, "Pista lista")

    return final_flac_file, lrc_file_path


def run_tidal_track_task(task_id: str, track_id: str, api: TidalApi, quality_mode: str, naming_format: str = "full"):
    task_dir = TEMP_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        track = api.getTrack(track_id)
        tasks[task_id]["title"] = track.title
        tasks[task_id]["artist"] = track.artist.name if track.artist else "Artista"

        cover_data = resolve_track_cover_bytes(api, track)

        def on_subprogress(pct: float, msg: str):
            tasks[task_id]["percent"] = round(pct * 100, 1)
            tasks[task_id]["status"] = f"{msg} ({int(pct*100)}%)"

        album_fallback = track.album.title if (hasattr(track, "album") and track.album and getattr(track.album, "title", None)) else "Sencillo"
        final_flac_file, lrc_file = download_single_flac_track(
            api=api,
            track=track,
            target_dir=task_dir,
            quality_mode=quality_mode,
            cover_data=cover_data,
            on_subprogress=on_subprogress,
            naming_format=naming_format,
            album_title_fallback=album_fallback,
        )

        filesize = final_flac_file.stat().st_size
        filename = final_flac_file.name

        has_lrc = bool(lrc_file and lrc_file.exists())
        lrc_filename = lrc_file.name if has_lrc else None

        # NOTA DE OPTIMIZACIÓN DE DISCO:
        # No pre-creamos el archivo .ZIP duplicado en disco; si el usuario solicita el Pack ZIP,
        # se ensambla al vuelo con ZIP_STORED en /api/download/{task_id}/{pack_filename} ahorrando 50% de almacenamiento.
        zip_pack_name = f"{final_flac_file.stem} (FLAC + Letra).zip" if has_lrc else None
        zip_pack_url = f"/api/download/{task_id}/{urllib.parse.quote(zip_pack_name)}" if has_lrc else None

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
            "has_lyrics": has_lrc,
            "lrc_filename": lrc_filename,
            "lrc_download_url": f"/api/download/{task_id}/{urllib.parse.quote(lrc_filename)}" if has_lrc else None,
            "pack_filename": zip_pack_name,
            "pack_download_url": zip_pack_url,
        }
    except Exception as e:
        logger.error(f"[TIDAL] Error en tarea {task_id}: {e}")
        tasks[task_id]["error"] = str(e)
        tasks[task_id]["status"] = f"Error: {str(e)}"
        tasks[task_id]["completed"] = True


def run_tidal_album_task(task_id: str, album_id: str, api: TidalApi, quality_mode: str, naming_format: str = "full"):
    task_dir = TEMP_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        album = api.getAlbum(album_id)
        album_items = api.getAlbumItems(album_id, limit=100)
        tracks = [item.item for item in (album_items.items or []) if hasattr(item, "item") and item.item]
        total_tracks = len(tracks)

        album_artist = clean_filename(album.artist.name if album.artist else "Varios Artistas")
        album_title = clean_filename(album.title)
        album_folder_name = f"{album_artist} - {album_title}"
        album_dir = task_dir / album_folder_name
        album_dir.mkdir(parents=True, exist_ok=True)

        tasks[task_id]["title"] = album.title
        tasks[task_id]["artist"] = album.artist.name if album.artist else "Varios Artistas"

        zip_filename = f"{album_folder_name}.zip"
        zip_path = task_dir / zip_filename

        cover_data = b""
        if album.cover:
            try:
                c_uid = album.cover.replace("-", "/")
                c_url = f"https://resources.tidal.com/images/{c_uid}/1280x1280.jpg"
                res_cov = requests.get(c_url, timeout=15)
                if res_cov.status_code == 200:
                    cover_data = res_cov.content
                    with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_STORED) as zf:
                        zf.writestr("cover.jpg", cover_data)
            except Exception:
                pass

        for idx, track in enumerate(tracks):
            tasks[task_id]["status"] = f"Descargando ({idx+1}/{total_tracks}): {track.title}"
            base_pct = (idx / total_tracks) * 95.0

            def on_track_prog(frac: float, msg: str):
                cur_pct = base_pct + (frac * (95.0 / total_tracks))
                tasks[task_id]["percent"] = round(cur_pct, 1)

            try:
                flac_f, lrc_f = download_single_flac_track(
                    api=api,
                    track=track,
                    target_dir=album_dir,
                    quality_mode=quality_mode,
                    cover_data=cover_data,
                    on_subprogress=on_track_prog,
                    naming_format=naming_format,
                    album_title_fallback=album.title,
                )
                # Mover inmediatamente al ZIP sin recomprimir (ZIP_STORED) y borrar el archivo suelto para no duplicar disco
                with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_STORED) as zf:
                    if flac_f and flac_f.exists():
                        zf.write(flac_f, flac_f.name)
                        flac_f.unlink(missing_ok=True)
                    if lrc_f and lrc_f.exists():
                        zf.write(lrc_f, lrc_f.name)
                        lrc_f.unlink(missing_ok=True)
            except Exception as e:
                logger.error(f"[TIDAL ALBUM] Error en pista {track.title}: {e}")

        shutil.rmtree(album_dir, ignore_errors=True)
        total_size = format_size(zip_path.stat().st_size) if zip_path.exists() else "0 B"

        tasks[task_id]["completed"] = True
        tasks[task_id]["percent"] = 100.0
        tasks[task_id]["status"] = "¡Álbum completado con letras y portada en .ZIP!"
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


def run_tidal_batch_task(task_id: str, track_ids: List[str], api: TidalApi, quality_mode: str, naming_format: str = "full"):
    task_dir = TEMP_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    batch_folder_name = f"Tidal_Coleccion_{len(track_ids)}_canciones"
    batch_dir = task_dir / batch_folder_name
    batch_dir.mkdir(parents=True, exist_ok=True)

    try:
        total_tracks = len(track_ids)
        tasks[task_id]["title"] = f"Colección de {total_tracks} canciones"
        tasks[task_id]["artist"] = "Varios Artistas"
        tasks[task_id]["total_tracks"] = total_tracks

        zip_filename = f"{batch_folder_name}.zip"
        zip_path = task_dir / zip_filename

        for idx, trk_id in enumerate(track_ids):
            try:
                track = api.getTrack(trk_id)
                t_title = track.title
                t_artist = track.artist.name if track.artist else "Artista"
                tasks[task_id]["status"] = f"Descargando ({idx+1}/{total_tracks}): {t_artist} - {t_title}"

                base_pct = (idx / total_tracks) * 95.0

                def on_track_prog(frac: float, msg: str):
                    cur_pct = base_pct + (frac * (95.0 / total_tracks))
                    tasks[task_id]["percent"] = round(cur_pct, 1)

                cover_data = resolve_track_cover_bytes(api, track)
                album_fallback = getattr(track.album, "title", None) if getattr(track, "album", None) else None

                flac_f, lrc_f = download_single_flac_track(
                    api=api,
                    track=track,
                    target_dir=batch_dir,
                    quality_mode=quality_mode,
                    cover_data=cover_data,
                    on_subprogress=on_track_prog,
                    naming_format=naming_format,
                    album_title_fallback=album_fallback,
                )
                # Mover inmediatamente al ZIP con ZIP_STORED y borrar el archivo suelto (ahorro de >50% de disco)
                with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_STORED) as zf:
                    if flac_f and flac_f.exists():
                        zf.write(flac_f, flac_f.name)
                        flac_f.unlink(missing_ok=True)
                    if lrc_f and lrc_f.exists():
                        zf.write(lrc_f, lrc_f.name)
                        lrc_f.unlink(missing_ok=True)
            except Exception as e:
                logger.error(f"[TIDAL BATCH] Error en pista ID {trk_id}: {e}")

        shutil.rmtree(batch_dir, ignore_errors=True)
        total_size = format_size(zip_path.stat().st_size) if zip_path.exists() else "0 B"

        tasks[task_id]["completed"] = True
        tasks[task_id]["percent"] = 100.0
        tasks[task_id]["status"] = "¡Colección completada con FLAC, carátulas y letras en .ZIP!"
        tasks[task_id]["result"] = {
            "type": "batch",
            "filename": zip_filename,
            "tracks_downloaded": total_tracks,
            "total_size": total_size,
            "download_url": f"/api/download/{task_id}/{urllib.parse.quote(zip_filename)}",
        }
    except Exception as e:
        logger.error(f"[TIDAL BATCH] Error en colección {task_id}: {e}")
        tasks[task_id]["error"] = str(e)
        tasks[task_id]["status"] = f"Error: {str(e)}"
        tasks[task_id]["completed"] = True


@app.api_route("/api/tidal/lyrics/download/{track_id}", methods=["GET", "HEAD"])
async def download_track_lyrics_direct(track_id: str, naming_format: Optional[str] = "full"):
    """Descarga directa del archivo de letra sincronizada (.lrc) para cualquier canción (compatible con Safari iOS)"""
    api = get_active_tidal_api()
    try:
        track = api.getTrack(track_id)
        lyr_info = safe_get_lyrics(api, track_id, track=track)
        if not lyr_info or not lyr_info.get("has_lyrics"):
            raise HTTPException(status_code=404, detail="No se encontraron letras disponibles para esta canción.")

        artist_name = "Artista"
        if hasattr(track, "artist") and track.artist and getattr(track.artist, "name", None):
            artist_name = track.artist.name
        elif hasattr(track, "artists") and track.artists and len(track.artists) > 0 and getattr(track.artists[0], "name", None):
            artist_name = track.artists[0].name
        artist_name = clean_filename(artist_name)

        album_name = "Album"
        if hasattr(track, "album") and track.album and getattr(track.album, "title", None):
            album_name = track.album.title
        album_name = clean_filename(album_name)

        track_title = clean_filename(track.title)

        t_num = getattr(track, "trackNumber", None)
        try:
            track_num = f"{int(t_num):02d}" if t_num is not None else "01"
        except Exception:
            track_num = "01"

        if naming_format == "title_only":
            filename = f"{track_title}.lrc"
        else:
            filename = f"{artist_name} - {album_name} - {track_num} - {track_title}.lrc"

        content_bytes = lyr_info["text"].encode("utf-8")

        return Response(
            content=content_bytes,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": make_content_disposition(filename),
                "Content-Length": str(len(content_bytes)),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-cache, no-store, must-revalidate",
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error obteniendo letra: {str(e)}")


@app.get("/api/tidal/album/{album_id}/tracks")
async def get_album_tracks(album_id: str):
    """Devuelve la lista detallada de canciones de un álbum con sus carátulas para visualización y descarga directa"""
    api = get_active_tidal_api()
    try:
        def fetch_album_data():
            album = api.getAlbum(album_id)
            cover_url = None
            if album.cover:
                c_uid = album.cover.replace("-", "/")
                cover_url = f"https://resources.tidal.com/images/{c_uid}/640x640.jpg"

            items_res = api.getAlbumItems(album_id, limit=100)
            tracks = []
            for it in (items_res.items or []):
                if hasattr(it, "item") and it.item:
                    t = it.item
                    artists = ", ".join([a.name for a in t.artists]) if t.artists else (t.artist.name if t.artist else "Desconocido")
                    t_cover = cover_url
                    if getattr(t, "album", None) and getattr(t.album, "cover", None):
                        tc_uid = t.album.cover.replace("-", "/")
                        t_cover = f"https://resources.tidal.com/images/{tc_uid}/640x640.jpg"
                    tracks.append({
                        "id": str(t.id),
                        "track_number": getattr(t, "trackNumber", None),
                        "volume_number": getattr(t, "volumeNumber", 1),
                        "title": t.title,
                        "artist": artists,
                        "album": album.title,
                        "album_id": str(album.id),
                        "cover": t_cover,
                        "cover_url": t_cover,
                        "duration": format_duration(t.duration),
                        "duration_seconds": t.duration,
                        "quality": getattr(t, "audioQuality", "LOSSLESS"),
                        "url": f"https://tidal.com/browse/track/{t.id}",
                    })

            return {
                "id": str(album.id),
                "title": album.title,
                "artist": album.artist.name if album.artist else (album.artists[0].name if album.artists else "Varios Artistas"),
                "cover": cover_url,
                "release_date": str(album.releaseDate) if getattr(album, "releaseDate", None) else "",
                "tracks_count": len(tracks),
                "duration": format_duration(album.duration) if getattr(album, "duration", None) else "--:--",
                "tracks": tracks,
                "url": f"https://tidal.com/browse/album/{album.id}",
            }

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, fetch_album_data)
    except Exception as e:
        logger.error(f"[TIDAL] Error obteniendo canciones del álbum {album_id}: {e}")
        raise HTTPException(status_code=400, detail=f"Error obteniendo canciones del álbum: {str(e)}")


def find_external_preview_url(artist: str, title: str) -> Optional[str]:
    """Busca un stream de preescucha rápido (MP3/M4A) en Deezer o iTunes cuando Tidal usa DASH o restringe el preview."""
    clean_t = re.sub(r"\s*[\(\[].*?[\)\]]", "", title or "").strip() or (title or "").strip()
    clean_a = (artist or "").split(",")[0].split("&")[0].strip()
    query = f"{clean_a} {clean_t}".strip()
    if not query:
        return None

    # 1. Intentar Deezer API (30s MP3 directo)
    try:
        dz_url = f"https://api.deezer.com/search?q={urllib.parse.quote(query)}&limit=5"
        r = requests.get(dz_url, timeout=5)
        if r.status_code == 200:
            items = r.json().get("data", [])
            for item in items:
                prev = item.get("preview")
                if prev:
                    return prev
    except Exception:
        pass

    # 2. Intentar iTunes Search API (30s M4A directo)
    try:
        it_url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&entity=song&limit=5"
        r = requests.get(it_url, timeout=5)
        if r.status_code == 200:
            items = r.json().get("results", [])
            for item in items:
                prev = item.get("previewUrl")
                if prev:
                    return prev
    except Exception:
        pass

    return None


@app.get("/api/tidal/stream-preview")
async def tidal_stream_preview(id: str, title: Optional[str] = None, artist: Optional[str] = None):
    """Retorna la URL de streaming para preescucha con soporte DASH, auto-refresco de token y respaldo Deezer/iTunes."""
    def resolve_preview():
        t_title = (title or "").strip()
        t_artist = (artist or "").strip()
        api = None
        try:
            api = get_active_tidal_api()
        except Exception:
            api = None

        if api:
            if not t_title or not t_artist:
                try:
                    trk = api.getTrack(id)
                    if trk:
                        t_title = t_title or getattr(trk, "title", "")
                        if getattr(trk, "artist", None):
                            t_artist = t_artist or trk.artist.name
                except Exception:
                    pass

            for attempt in range(2):
                for q_mode in ("LOW", "HIGH", "LOSSLESS"):
                    try:
                        s = api.getTrackStream(id, q_mode)
                        urls, _ = parseTrackStream(s)
                        if urls:
                            proxy_url = f"/api/tidal/stream-preview-audio?id={urllib.parse.quote(str(id))}&title={urllib.parse.quote(t_title)}&artist={urllib.parse.quote(t_artist)}"
                            if len(urls) == 1:
                                return {"url": urls[0], "fallback_url": proxy_url, "source": f"tidal_{q_mode.lower()}"}
                            else:
                                return {"url": proxy_url, "fallback_url": find_external_preview_url(t_artist, t_title), "source": "tidal_dash_proxy"}
                    except Exception as err:
                        err_str = str(err).lower()
                        if ("401" in err_str or "403" in err_str or "token" in err_str or "privilege" in err_str) and attempt == 0:
                            try:
                                api = get_active_tidal_api(force_refresh=True)
                            except Exception:
                                pass
                            break

        ext_url = find_external_preview_url(t_artist, t_title)
        if ext_url:
            return {"url": ext_url, "fallback_url": ext_url, "source": "external_preview"}

        return None

    loop = asyncio.get_event_loop()
    res_data = await loop.run_in_executor(None, resolve_preview)
    if not res_data or not res_data.get("url"):
        raise HTTPException(status_code=404, detail="No se pudo obtener el audio de preescucha para esta pista.")
    return res_data


@app.get("/api/tidal/stream-preview-audio")
async def tidal_stream_preview_audio(id: str, title: Optional[str] = None, artist: Optional[str] = None):
    """Ensambla y transmite en vivo fragmentos DASH de Tidal (o el respaldo externo) para que <audio> nunca falle."""
    def build_preview_bytes() -> Tuple[bytes, str]:
        t_title = (title or "").strip()
        t_artist = (artist or "").strip()
        try:
            api = get_active_tidal_api()
            for q_mode in ("LOW", "HIGH", "LOSSLESS"):
                try:
                    s = api.getTrackStream(id, q_mode)
                    urls, file_ext = parseTrackStream(s)
                    if not urls:
                        continue
                    max_segs = min(len(urls), 18)
                    raw_buf = bytearray()
                    with requests.Session() as sess:
                        for u in urls[:max_segs]:
                            with sess.get(u, timeout=12) as resp:
                                resp.raise_for_status()
                                raw_buf.extend(resp.content)
                    if len(raw_buf) > 1024:
                        mime = "audio/flac" if file_ext == ".flac" else "audio/mp4"
                        return bytes(raw_buf), mime
                except Exception:
                    continue
        except Exception:
            pass

        ext_url = find_external_preview_url(t_artist, t_title)
        if ext_url:
            r = requests.get(ext_url, timeout=10)
            if r.status_code == 200 and len(r.content) > 1024:
                mime = "audio/mpeg" if ".mp3" in ext_url.lower() else "audio/mp4"
                return r.content, mime

        return b"", "audio/mp4"

    loop = asyncio.get_event_loop()
    audio_bytes, media_type = await loop.run_in_executor(None, build_preview_bytes)
    if not audio_bytes:
        raise HTTPException(status_code=404, detail="No se pudo ensamblar la preescucha.")
    return Response(content=audio_bytes, media_type=media_type, headers={"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=600"})


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

    naming_format = req.naming_format if req.naming_format in ["full", "title_only"] else "full"
    if res_type == "track":
        threading.Thread(
            target=run_tidal_track_task,
            args=(task_id, res_id, api, quality_mode, naming_format),
            daemon=True,
        ).start()
    else:
        threading.Thread(
            target=run_tidal_album_task,
            args=(task_id, res_id, api, quality_mode, naming_format),
            daemon=True,
        ).start()

    return {"task_id": task_id, "status": "Iniciando descarga..."}


@app.post("/api/tidal/download-batch")
async def download_tidal_batch(req: TidalBatchDownloadRequest):
    """Inicia la descarga por lotes de una lista de canciones en un paquete ZIP con letras .LRC y carátulas"""
    if not req.track_ids:
        raise HTTPException(status_code=400, detail="No se proporcionaron canciones para descargar.")

    quality_mode = "HI_RES_LOSSLESS" if req.quality == "master" else "LOSSLESS"
    api = get_active_tidal_api()

    clean_old_temp_dirs()
    task_id = uuid.uuid4().hex
    tasks[task_id] = {
        "id": task_id,
        "type": "tidal_batch",
        "status": "Iniciando colección personalizada...",
        "percent": 0.0,
        "completed": False,
        "error": None,
        "result": None,
        "created_at": time.time(),
    }

    naming_format = req.naming_format if req.naming_format in ["full", "title_only"] else "full"
    threading.Thread(
        target=run_tidal_batch_task,
        args=(task_id, req.track_ids, api, quality_mode, naming_format),
        daemon=True,
    ).start()

    return {"task_id": task_id, "status": "Iniciando descarga de colección..."}


@app.get("/api/task-status/{task_id}")
async def get_task_status(task_id: str):
    """Consulta el progreso en tiempo real de una tarea específica"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Tarea no encontrada o expirada.")
    return tasks[task_id]


def cleanup_task_dir(task_id: str):
    """Limpia el directorio temporal tras 180s de inactividad (permite las múltiples peticiones HEAD/GET de Safari iOS)"""
    now = time.time()
    if task_id in tasks:
        tasks[task_id]["last_access"] = now

    def _delayed_delete(scheduled_at: float):
        time.sleep(180)
        # Si hubo otra petición posterior (ej. nsurlsessiond de iOS o descarga de LRC/ZIP adicional), esperar a su propio timer
        if task_id in tasks and tasks[task_id].get("last_access", 0) > scheduled_at + 1.0:
            return
        task_dir = TEMP_DIR / task_id
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)
            logger.info(f"[CLEANUP] Tarea {task_id} y archivos temporales eliminados del servidor.")
    threading.Thread(target=_delayed_delete, args=(now,), daemon=True).start()


@app.api_route("/api/download/{task_id}/{filename}", methods=["GET", "HEAD"])
async def download_file_by_task(task_id: str, filename: str, background_tasks: BackgroundTasks):
    """Descarga directa aislada por tarea con soporte nativo para Safari iOS (GET/HEAD + application/octet-stream)"""
    task_dir = TEMP_DIR / task_id
    decoded_filename = urllib.parse.unquote(filename)
    file_path = task_dir / decoded_filename

    # Prevenir Directory Traversal
    if not file_path.resolve().is_relative_to(task_dir.resolve()):
        raise HTTPException(status_code=403, detail="Acceso denegado.")

    # Si el usuario pidió el Pack (FLAC + Letra).zip de una pista individual, construirlo al vuelo con ZIP_STORED
    if not file_path.exists() and decoded_filename.endswith("(FLAC + Letra).zip") and task_dir.exists():
        try:
            with zipfile.ZipFile(file_path, "w", zipfile.ZIP_STORED) as zf:
                for item in task_dir.iterdir():
                    if item.is_file() and item.suffix.lower() in (".flac", ".lrc"):
                        zf.write(item, item.name)
        except Exception as e:
            logger.warning(f"Error creando Pack ZIP bajo demanda: {e}")

    if not file_path.exists() or not file_path.is_file():
        candidates = list(task_dir.rglob(decoded_filename)) if task_dir.exists() else []
        if candidates:
            file_path = candidates[0]
        else:
            raise HTTPException(status_code=404, detail="Archivo temporal no encontrado.")

    # Actualizar mtime del directorio para evitar que clean_old_temp_dirs lo borre mientras se descarga
    try:
        os.utime(task_dir, None)
    except Exception:
        pass

    # IMPORTANTE PARA SAFARI iOS (iPhone / iPad):
    # Si se envía "audio/flac", Safari iOS intercepta el enlace con el reproductor QuickTime en lugar de descargarlo
    # y falla con archivos FLAC de 24-bit. Con "application/octet-stream" + "X-Content-Type-Options: nosniff" + "attachment",
    # Safari iOS abre siempre el gestor nativo "¿Quieres descargar...?" y lo guarda directo en la app "Archivos" (Files).
    ext = file_path.suffix.lower()
    if ext == ".zip":
        media_type = "application/zip"
    else:
        media_type = "application/octet-stream"

    # Programar limpieza diferida con ventana de 180s desde el último acceso
    background_tasks.add_task(cleanup_task_dir, task_id)

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type=media_type,
        headers={
            "Content-Disposition": make_content_disposition(file_path.name),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-cache, no-store, must-revalidate",
        },
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
    port = int(os.environ.get("PORT", 7860))
    logger.info(f"Iniciando Tidal Cloud Studio en el puerto {port}...")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
