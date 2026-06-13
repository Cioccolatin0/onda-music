"""
Onda — Music streaming app
Backend: FastAPI + ytmusicapi (search/metadata) + yt-dlp (audio stream extraction)
Supports cookies via file (local) or YT_COOKIES env var (Render/cloud deploy).
"""
import asyncio
import os
import time
import threading
import tempfile

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from ytmusicapi import YTMusic

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Cookie resolution ──────────────────────────────────────────────────────
_COOKIE_FILE = None

def _resolve_cookies():
    global _COOKIE_FILE
    render_secret = "/etc/secrets/cookies.txt"
    if os.path.exists(render_secret):
        import shutil
        tmp_copy = "/tmp/yt_cookies.txt"
        shutil.copy2(render_secret, tmp_copy)
        os.chmod(tmp_copy, 0o600)
        _COOKIE_FILE = tmp_copy
        return
    env_val = os.environ.get("YT_COOKIES", "").strip()
    if env_val:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        if not env_val.startswith("# Netscape"):
            import base64
            try:
                env_val = base64.b64decode(env_val).decode()
            except Exception:
                pass
        tmp.write(env_val)
        tmp.flush()
        tmp.close()
        _COOKIE_FILE = tmp.name
        return
    local = os.path.join(BASE_DIR, "cookies.txt")
    if os.path.exists(local):
        _COOKIE_FILE = local

_resolve_cookies()

app = FastAPI(title="Onda Music API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ytm = YTMusic()

# ── Stream URL cache ───────────────────────────────────────────────────────
_stream_cache: dict = {}
_cache_lock = threading.Lock()
STREAM_TTL = 3600 * 4


def _extract_stream(video_id: str) -> dict:
    # Try YouTube first
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    if _COOKIE_FILE:
        opts["cookiefile"] = _COOKIE_FILE
    
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        return {
            "url": info["url"],
            "ext": info.get("ext", "m4a"),
            "abr": info.get("abr"),
            "title": info.get("title"),
            "duration": info.get("duration"),
            "http_headers": info.get("http_headers") or {},
            "filesize": info.get("filesize") or info.get("filesize_approx"),
            "ts": time.time(),
            "source": "youtube",
        }
    except Exception as yt_err:
        # Fallback: try SoundCloud
        try:
            opts_sc = {
                "format": "bestaudio",
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "skip_download": True,
            }
            with yt_dlp.YoutubeDL(opts_sc) as ydl:
                info = ydl.extract_info(f"scsearch1:{video_id}", download=False)
            if info and info.get('entries'):
                track = info['entries'][0]
                return {
                    "url": track["url"],
                    "ext": track.get("ext", "m4a"),
                    "abr": track.get("abr"),
                    "title": track.get("title"),
                    "duration": track.get("duration"),
                    "http_headers": track.get("http_headers") or {},
                    "filesize": track.get("filesize") or track.get("filesize_approx"),
                    "ts": time.time(),
                    "source": "soundcloud",
                }
        except Exception as sc_err:
            pass
        raise Exception(f"Stream extraction failed: {str(yt_err)[:200]}")


def get_stream_info(video_id: str) -> dict:
    with _cache_lock:
        cached = _stream_cache.get(video_id)
        if cached and time.time() - cached["ts"] < STREAM_TTL:
            return cached
    info = _extract_stream(video_id)
    with _cache_lock:
        _stream_cache[video_id] = info
    return info


# ── Helpers ────────────────────────────────────────────────────────────────
def _thumb(thumbnails, size=544):
    if not thumbnails:
        return None
    url = thumbnails[-1]["url"]
    if "=w" in url and "-h" in url:
        base = url.split("=w")[0]
        return f"{base}=w{size}-h{size}-l90-rj"
    return url


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/home")
async def home():
    try:
        data = ytm.get_home()
        sections = []
        for sec in data.get("contents", [])[:5]:
            title = sec.get("title", "")
            items_key = None
            if "musicTastebarRenderer" in sec:
                continue
            if "musicCarouselShelfRenderer" in sec:
                items_key = "musicCarouselShelfRenderer"
            elif "musicResponsiveListItemRenderer" in sec:
                items_key = "musicResponsiveListItemRenderer"
            
            if items_key:
                items = sec[items_key].get("contents", [])
                parsed = []
                for item in items[:12]:
                    if "musicResponsiveListItemRenderer" in item:
                        r = item["musicResponsiveListItemRenderer"]
                        parsed.append({
                            "videoId": r.get("flexColumns", [{}])[0].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [{}])[0].get("navigationEndpoint", {}).get("watchPlaylistEndpoint", {}).get("playlistId", ""),
                            "title": r.get("flexColumns", [{}])[0].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [{}])[0].get("text", ""),
                            "artists": [{"name": a.get("text", "")} for a in r.get("flexColumns", [{}])[1].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", []) if a.get("text")],
                            "thumbnail": r.get("thumbnail", {}).get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [{}])[-1].get("url", ""),
                        })
                    elif "musicTwoRowItemRenderer" in item:
                        r = item["musicTwoRowItemRenderer"]
                        parsed.append({
                            "browseId": r.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId", ""),
                            "title": r.get("title", {}).get("runs", [{}])[0].get("text", ""),
                            "subtitle": r.get("subtitle", {}).get("runs", [{}])[0].get("text", "") if r.get("subtitle") else "",
                            "thumbnail": r.get("thumbnailRenderer", {}).get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [{}])[-1].get("url", ""),
                        })
                if parsed:
                    sections.append({"title": title, "items": parsed})
        
        return {
            "sections": sections,
            "trending": sections[0]["items"] if sections else [],
            "new_releases": sections[1]["items"] if len(sections) > 1 else [],
        }
    except Exception as e:
        return {"sections": [], "trending": [], "new_releases": [], "error": str(e)}


@app.get("/api/search")
async def search(q: str, type: str = "all"):
    try:
        results = ytm.search(q, filter=type if type != "all" else None)
        songs = [r for r in results if r.get("resultType") == "song"][:12]
        albums = [r for r in results if r.get("resultType") == "album"][:12]
        artists = [r for r in results if r.get("resultType") == "artist"][:12]
        
        return {
            "songs": [{"videoId": s.get("videoId"), "title": s.get("title"), "artists": s.get("artists", []), "thumbnail": _thumb(s.get("thumbnails"))} for s in songs],
            "albums": [{"browseId": a.get("browseId"), "title": a.get("title"), "artists": a.get("artists", []), "thumbnails": a.get("thumbnails", [])} for a in albums],
            "artists": [{"browseId": a.get("browseId"), "name": a.get("name"), "thumbnails": a.get("thumbnails", [])} for a in artists],
        }
    except Exception as e:
        return {"songs": [], "albums": [], "artists": [], "error": str(e)}


@app.get("/api/suggestions")
async def suggestions(q: str):
    try:
        results = ytm.search(q, filter=None)
        titles = [r.get("title", "") for r in results[:5] if r.get("title")]
        return {"suggestions": list(set(titles))}
    except:
        return {"suggestions": []}


@app.get("/api/stream/{video_id}")
async def stream(video_id: str):
    try:
        info = get_stream_info(video_id)
        return info
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Static files ───────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
