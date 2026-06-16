"""
ytmusic.py — YouTube Music search + download via InnerTube API + pytubefix

Based on the provided script — adapted for async bot use.
NO yt-dlp dependency — uses YouTube Music's own InnerTube API directly.

Search:   POST music.youtube.com/youtubei/v1/search  (WEB_REMIX client)
Download: pytubefix (handles cipher/auth, no sign-in needed for most tracks)
Convert:  ffmpeg subprocess (MP3 192kbps)
Art:      mutagen ID3 APIC
"""

import asyncio
import io
import json
import os
import re
import subprocess
import tempfile
import time
from typing import Optional

import httpx

_YTM_SEARCH  = "https://music.youtube.com/youtubei/v1/search"
_YTM_HOME    = "https://music.youtube.com/"
_SONGS_PARAM = "EgWKAQIIAWoKEAkQBRAKEAMQBA=="   # Songs-only filter

_YTM_CLIENT = {
    "clientName":    "WEB_REMIX",
    "clientVersion": "1.20240101.01.00",
    "hl": "en",
    "gl": "IN",
}

_API_HEADERS = {
    "Content-Type":             "application/json",
    "X-YouTube-Client-Name":    "67",
    "X-YouTube-Client-Version": "1.20240101.01.00",
    "Origin":                   "https://music.youtube.com",
    "Referer":                  "https://music.youtube.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

_visitor_data: str = ""


async def _init_session() -> str:
    """Load music.youtube.com once to get visitorData cookie."""
    global _visitor_data
    if _visitor_data:
        return _visitor_data
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as hx:
            r = await hx.get(_YTM_HOME, headers={"User-Agent": _API_HEADERS["User-Agent"]})
            m = re.search(r'"VISITOR_DATA":"([^"]+)"', r.text)
            if m:
                _visitor_data = m.group(1)
                _YTM_CLIENT["visitorData"] = _visitor_data
    except Exception:
        pass
    return _visitor_data


def _find_all(data, key):
    """Recursively collect every value for `key` in nested dict/list."""
    out = []
    if isinstance(data, dict):
        if key in data:
            out.append(data[key])
        for v in data.values():
            out.extend(_find_all(v, key))
    elif isinstance(data, list):
        for item in data:
            out.extend(_find_all(item, key))
    return out


def _safe_get(d, *keys, default=None):
    for k in keys:
        try:
            d = d[k]
        except Exception:
            return default
    return d


def _parse_renderer(r: dict) -> Optional[dict]:
    """Extract song info from a musicResponsiveListItemRenderer."""
    vid = _safe_get(
        r, "overlay", "musicItemThumbnailOverlayRenderer",
        "content", "musicPlayButtonRenderer",
        "playNavigationEndpoint", "watchEndpoint", "videoId",
    )
    if not vid:
        vid = _safe_get(r, "navigationEndpoint", "watchEndpoint", "videoId")
    if not vid or len(vid) != 11:
        return None

    flex = r.get("flexColumns", [])

    title_runs = _safe_get(flex, 0, "musicResponsiveListItemFlexColumnRenderer",
                            "text", "runs", default=[])
    title = title_runs[0].get("text", "Unknown") if title_runs else "Unknown"

    meta_runs = _safe_get(flex, 1, "musicResponsiveListItemFlexColumnRenderer",
                           "text", "runs", default=[])
    artist = meta_runs[0].get("text", "Unknown") if meta_runs else "Unknown"

    dur_str = ""
    for run in reversed(meta_runs):
        txt = run.get("text", "")
        if re.match(r"^\d+:\d{2}$", txt):
            dur_str = txt
            break

    thumbs    = _safe_get(r, "thumbnail", "musicThumbnailRenderer",
                           "thumbnail", "thumbnails", default=[])
    thumb_url = thumbs[-1].get("url", "") if thumbs else ""
    if not thumb_url:
        thumb_url = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    return {
        "id":        vid,
        "title":     title,
        "artist":    artist,
        "duration":  dur_str,
        "thumbnail": thumb_url,
    }


async def search(query: str, n: int = 8) -> list[dict]:
    """
    Search YouTube Music via InnerTube API.
    Returns list of dicts: id, title, artist, duration, thumbnail.
    """
    await _init_session()

    body = {
        "context": {"client": _YTM_CLIENT},
        "query":   query,
        "params":  _SONGS_PARAM,
    }

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as hx:
        r = await hx.post(
            _YTM_SEARCH,
            headers=_API_HEADERS,
            json=body,
            params={"prettyPrint": "false"},
        )
        r.raise_for_status()
        data = r.json()

    renderers = _find_all(data, "musicResponsiveListItemRenderer")
    songs = []
    for renderer in renderers:
        entry = _parse_renderer(renderer)
        if entry:
            songs.append(entry)
        if len(songs) >= n:
            break
    return songs


async def fetch_thumbnail(thumb_url: str) -> Optional[bytes]:
    """Download thumbnail bytes."""
    try:
        async with httpx.AsyncClient(timeout=10) as hx:
            r = await hx.get(thumb_url)
            if r.status_code == 200:
                return r.content
    except Exception:
        pass
    return None


def _embed_art(mp3_path: str, img_bytes: bytes, title: str, artist: str):
    """Embed album art + ID3 tags."""
    try:
        from PIL import Image
        from mutagen.id3 import ID3, APIC, TIT2, TPE1, error as ID3Error
        from mutagen.mp3 import MP3

        # Convert to JPEG
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        jpeg = buf.getvalue()

        audio = MP3(mp3_path, ID3=ID3)
        try:
            audio.add_tags()
        except ID3Error:
            pass
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg))
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=artist))
        audio.save(v2_version=3)
    except Exception as e:
        print(f"[ytmusic/embed] {e}")


def _find_ffmpeg() -> str:
    import shutil
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    try:
        import imageio_ffmpeg as ioff
        return ioff.get_ffmpeg_exe()
    except Exception:
        pass
    return "ffmpeg"


_FFMPEG = _find_ffmpeg()


def download_mp3(vid_id: str, title: str, artist: str,
                 out_dir: str, thumb_bytes: Optional[bytes] = None) -> Optional[str]:
    """
    Download audio from YouTube Music using pytubefix.
    Converts to MP3 192kbps with ffmpeg.
    Returns path to the MP3 file, or None on failure.
    """
    from pytubefix import YouTube
    from pytubefix.exceptions import VideoUnavailable

    url = f"https://music.youtube.com/watch?v={vid_id}"
    os.makedirs(out_dir, exist_ok=True)

    try:
        yt     = YouTube(url)
        stream = (
            yt.streams.filter(only_audio=True).order_by("abr").last()
            or yt.streams.filter(only_audio=True).first()
        )
        if not stream:
            return None
    except VideoUnavailable:
        return None
    except Exception:
        # pytubefix sometimes fails on first attempt; retry with regular youtube.com
        try:
            url = f"https://www.youtube.com/watch?v={vid_id}"
            yt  = YouTube(url)
            stream = (
                yt.streams.filter(only_audio=True).order_by("abr").last()
                or yt.streams.filter(only_audio=True).first()
            )
            if not stream:
                return None
        except Exception:
            return None

    with tempfile.TemporaryDirectory() as tmp:
        raw_path = stream.download(output_path=tmp)

        # Convert to MP3
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:120]
        mp3_path   = os.path.join(out_dir, f"{safe_title}.mp3")

        result = subprocess.run(
            [_FFMPEG, "-y", "-i", raw_path,
             "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k",
             mp3_path],
            capture_output=True, timeout=120,
        )

        if result.returncode != 0 or not os.path.exists(mp3_path):
            # Fallback: copy raw file
            import shutil
            ext      = os.path.splitext(raw_path)[1] or ".m4a"
            mp3_path = os.path.join(out_dir, f"{safe_title}{ext}")
            shutil.copy2(raw_path, mp3_path)
            return mp3_path

    # Embed album art
    if thumb_bytes and mp3_path.endswith(".mp3"):
        _embed_art(mp3_path, thumb_bytes, title, artist)

    return mp3_path
