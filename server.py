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
# Priority: YT_COOKIES env var (base64 or raw Netscape text) > cookies.txt file
_COOKIE_FILE = None

def _resolve_cookies():
    global _COOKIE_FILE
    # 1) Render Secret File at /etc/secrets/cookies.txt (most reliable)
    render_secret = "/etc/secrets/cookies.txt"
    if os.path.exists(render_secret):
        _COOKIE_FILE = render_secret
        return
    # 2) YT_COOKIES env var (base64 or raw Netscape text)
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
    # 3) Local cookies.txt (dev environment)
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
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    if _COOKIE_FILE:
        opts["cookiefile"] = _COOKIE_FILE
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
    }


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


def _artists(item):
    return [
        {"name": a.get("name"), "id": a.get("id")}
        for a in (item.get("artists") or [])
        if a.get("name")
    ]


def norm_song(item):
    album = item.get("album") or {}
    return {
        "type": "song",
        "videoId": item.get("videoId"),
        "title": item.get("title"),
        "artists": _artists(item),
        "album": {"name": album.get("name"), "id": album.get("id")} if album else None,
        "duration": item.get("duration"),
        "thumbnail": _thumb(item.get("thumbnails")),
    }


def norm_album(item):
    return {
        "type": "album",
        "browseId": item.get("browseId"),
        "title": item.get("title"),
        "artists": _artists(item),
        "year": item.get("year"),
        "thumbnail": _thumb(item.get("thumbnails")),
    }


def norm_artist(item):
    return {
        "type": "artist",
        "browseId": item.get("browseId"),
        "name": item.get("artist") or item.get("title"),
        "thumbnail": _thumb(item.get("thumbnails")),
    }


# ── API endpoints ──────────────────────────────────────────────────────────
@app.get("/api/search")
async def search(q: str, type: str = "all"):
    if not q.strip():
        return {"songs": [], "albums": [], "artists": []}
    loop = asyncio.get_event_loop()

    async def run(filter_name, limit):
        return await loop.run_in_executor(None, lambda: ytm.search(q, filter=filter_name, limit=limit))

    try:
        if type == "songs":
            songs = await run("songs", 25)
            return {"songs": [norm_song(s) for s in songs if s.get("videoId")], "albums": [], "artists": []}
        if type == "albums":
            albums = await run("albums", 25)
            return {"songs": [], "albums": [norm_album(a) for a in albums if a.get("browseId")], "artists": []}
        if type == "artists":
            artists = await run("artists", 25)
            return {"songs": [], "albums": [], "artists": [norm_artist(a) for a in artists if a.get("browseId")]}
        songs_t, albums_t, artists_t = await asyncio.gather(
            run("songs", 12), run("albums", 8), run("artists", 6)
        )
        return {
            "songs": [norm_song(s) for s in songs_t if s.get("videoId")],
            "albums": [norm_album(a) for a in albums_t if a.get("browseId")],
            "artists": [norm_artist(a) for a in artists_t if a.get("browseId")],
        }
    except Exception as e:
        raise HTTPException(500, f"Search failed: {e}")


@app.get("/api/suggestions")
async def suggestions(q: str):
    if not q.strip():
        return {"suggestions": []}
    loop = asyncio.get_event_loop()
    try:
        res = await loop.run_in_executor(None, lambda: ytm.get_search_suggestions(q))
        return {"suggestions": [s for s in res if isinstance(s, str)][:8]}
    except Exception:
        return {"suggestions": []}


@app.get("/api/album/{browse_id}")
async def album(browse_id: str):
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: ytm.get_album(browse_id))
    except Exception as e:
        raise HTTPException(404, f"Album not found: {e}")
    tracks = []
    for t in data.get("tracks") or []:
        if not t.get("videoId"):
            continue
        tracks.append({
            "videoId": t.get("videoId"),
            "title": t.get("title"),
            "artists": _artists(t),
            "duration": t.get("duration"),
            "thumbnail": _thumb(data.get("thumbnails")),
            "album": {"name": data.get("title"), "id": browse_id},
        })
    return {
        "browseId": browse_id,
        "title": data.get("title"),
        "artists": _artists(data),
        "year": data.get("year"),
        "trackCount": data.get("trackCount"),
        "duration": data.get("duration"),
        "thumbnail": _thumb(data.get("thumbnails")),
        "description": data.get("description"),
        "tracks": tracks,
    }


@app.get("/api/artist/{browse_id}")
async def artist(browse_id: str):
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: ytm.get_artist(browse_id))
    except Exception as e:
        raise HTTPException(404, f"Artist not found: {e}")
    songs_block = (data.get("songs") or {}).get("results") or []
    albums_block = (data.get("albums") or {}).get("results") or []
    singles_block = (data.get("singles") or {}).get("results") or []
    related_block = (data.get("related") or {}).get("results") or []
    return {
        "browseId": browse_id,
        "name": data.get("name"),
        "description": data.get("description"),
        "thumbnail": _thumb(data.get("thumbnails"), 800),
        "subscribers": data.get("subscribers"),
        "songs": [norm_song(s) for s in songs_block if s.get("videoId")],
        "albums": [norm_album(a) for a in albums_block if a.get("browseId")],
        "singles": [norm_album(a) for a in singles_block if a.get("browseId")],
        "related": [norm_artist(r) for r in related_block if r.get("browseId")][:10],
    }


_home_cache: dict = {"data": None, "ts": 0}
HOME_TTL = 3600


def _build_home() -> dict:
    sections = []
    trending_tracks = []
    top_artists = []
    try:
        ch = ytm.get_charts(country="IT")
        for v in ch.get("videos") or []:
            pid = v.get("playlistId")
            if pid and not trending_tracks:
                try:
                    pl = ytm.get_playlist(pid, limit=20)
                    for t in pl.get("tracks") or []:
                        if t.get("videoId"):
                            trending_tracks.append({
                                "type": "song",
                                "videoId": t.get("videoId"),
                                "title": t.get("title"),
                                "artists": _artists(t),
                                "duration": t.get("duration"),
                                "thumbnail": _thumb(t.get("thumbnails")),
                                "album": ({"name": (t.get("album") or {}).get("name"), "id": (t.get("album") or {}).get("id")} if t.get("album") else None),
                            })
                except Exception:
                    pass
        for a in (ch.get("artists") or [])[:12]:
            if a.get("browseId"):
                top_artists.append({
                    "type": "artist",
                    "browseId": a["browseId"],
                    "name": a.get("title"),
                    "subscribers": a.get("subscribers"),
                    "thumbnail": _thumb(a.get("thumbnails")),
                })
    except Exception:
        pass

    if trending_tracks:
        sections.append({"title": "Tendenze in Italia", "items": trending_tracks[:20], "kind": "songs"})
    if top_artists:
        sections.append({"title": "Artisti del momento", "items": top_artists, "kind": "artists"})

    try:
        from ytmusicapi.navigation import nav
        res = ytm._send_request("browse", {"browseId": "FEmusic_new_releases_albums"})
        grid = nav(res, ["contents", "singleColumnBrowseResultsRenderer", "tabs", 0,
                         "tabRenderer", "content", "sectionListRenderer", "contents", 0,
                         "gridRenderer", "items"], True) or []
        albums = []
        for it in grid:
            mtr = it.get("musicTwoRowItemRenderer") or {}
            bid = ((mtr.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId")
            title = ((mtr.get("title") or {}).get("runs") or [{}])[0].get("text")
            subs = (mtr.get("subtitle") or {}).get("runs") or []
            artists = [{"name": r["text"], "id": ((r.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId")}
                       for r in subs if r.get("text") not in (" • ", "Album", "Single", "EP") and r.get("text")]
            thumbs = ((((mtr.get("thumbnailRenderer") or {}).get("musicThumbnailRenderer") or {}).get("thumbnail") or {}).get("thumbnails") or [])
            if bid and title:
                albums.append({"type": "album", "browseId": bid, "title": title,
                               "artists": [a for a in artists if a["name"]][:2],
                               "thumbnail": _thumb(thumbs)})
            if len(albums) >= 16:
                break
        if albums:
            sections.append({"title": "Nuove uscite", "items": albums, "kind": "albums"})
    except Exception:
        pass

    for title, query in [("Hit globali", "top hits 2026"), ("Hip-Hop & Rap", "rap italiano hits")]:
        try:
            res = ytm.search(query, filter="songs", limit=12)
            items = [norm_song(s) for s in res if s.get("videoId")]
            if items:
                sections.append({"title": title, "items": items[:12], "kind": "songs"})
        except Exception:
            pass

    return {"sections": sections, "updated": time.strftime("%Y-%m-%d")}


@app.get("/api/home")
async def home():
    now = time.time()
    if _home_cache["data"] and now - _home_cache["ts"] < HOME_TTL:
        return _home_cache["data"]
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _build_home)
    if data["sections"]:
        _home_cache["data"] = data
        _home_cache["ts"] = now
    return data


@app.get("/api/radio/{video_id}")
async def radio(video_id: str):
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: ytm.get_watch_playlist(videoId=video_id, limit=25))
    except Exception as e:
        raise HTTPException(404, f"Radio failed: {e}")
    tracks = []
    for t in data.get("tracks") or []:
        if not t.get("videoId") or t.get("videoId") == video_id:
            continue
        tracks.append({
            "videoId": t.get("videoId"),
            "title": t.get("title"),
            "artists": _artists(t),
            "duration": t.get("length"),
            "thumbnail": _thumb(t.get("thumbnail")),
            "type": "song",
        })
    return {"tracks": tracks}


@app.get("/api/stream/{video_id}")
async def stream(video_id: str, request: Request):
    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, lambda: get_stream_info(video_id))
    except Exception as e:
        raise HTTPException(502, f"Stream extraction failed: {e}")

    upstream_headers = dict(info.get("http_headers") or {})
    range_header = request.headers.get("range")
    if range_header:
        upstream_headers["Range"] = range_header

    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=60), follow_redirects=True)
    try:
        req = client.build_request("GET", info["url"], headers=upstream_headers)
        upstream = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(502, f"Upstream connect failed: {e}")

    if upstream.status_code in (403, 410):
        await upstream.aclose()
        with _cache_lock:
            _stream_cache.pop(video_id, None)
        try:
            info = await loop.run_in_executor(None, lambda: get_stream_info(video_id))
            req = client.build_request("GET", info["url"], headers=upstream_headers)
            upstream = await client.send(req, stream=True)
        except Exception as e:
            await client.aclose()
            raise HTTPException(502, f"Stream retry failed: {e}")

    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(upstream.status_code, "Upstream error")

    resp_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "Content-Type": upstream.headers.get("Content-Type", "audio/mp4"),
    }
    for h in ("Content-Length", "Content-Range"):
        if h in upstream.headers:
            resp_headers[h] = upstream.headers[h]

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=resp_headers)


@app.get("/api/health")
async def health():
    return {"status": "ok", "date": time.strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/")
async def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))

@app.get("/manifest.json")
async def manifest():
    return FileResponse(os.path.join(BASE_DIR, "static", "manifest.json"))

@app.get("/sw.js")
async def sw():
    return FileResponse(os.path.join(BASE_DIR, "static", "sw.js"), media_type="application/javascript")

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
