"""
stream_server.py  —  aiohttp HTTP server for file streaming.

Always starts on localhost so ffmpeg can access ANY Telegram file
(any size) via http://127.0.0.1:PORT/dl/TOKEN with Range support.

Public URLs (STREAM_BASE_URL) are only used for /link command output.
"""

import logging
import mimetypes
import secrets
import time
from pathlib import Path
from typing import Optional

from aiohttp import web, ClientSession

log      = logging.getLogger(__name__)
_TOKENS: dict[str, dict] = {}
LINK_TTL = 3600
_TEMPLATE = (Path(__file__).parent / "templates" / "watch.html").read_text()

_pyro_client = None     # set on startup


def set_pyrogram_client(client):
    global _pyro_client
    _pyro_client = client


def create_token(filename: str, size: int, mime: str,
                 tg_file_id: Optional[str] = None,
                 cdn_url:    Optional[str] = None) -> str:
    token = secrets.token_urlsafe(14)
    _TOKENS[token] = {
        "tg_file_id": tg_file_id,
        "cdn_url":    cdn_url,
        "filename":   filename,
        "size":       size,
        "mime":       mime or mimetypes.guess_type(filename)[0] or "application/octet-stream",
        "expires":    time.time() + LINK_TTL,
    }
    return token


def _entry(token: str) -> Optional[dict]:
    e = _TOKENS.get(token)
    return e if e and time.time() < e["expires"] else None


def _human_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


routes = web.RouteTableDef()


@routes.get("/")
async def home(_: web.Request):
    return web.Response(text="<h2>Stream server running ✅</h2>", content_type="text/html")


@routes.get("/watch/{token}")
async def watch_handler(request: web.Request):
    e = _entry(request.match_info["token"])
    if not e:
        return web.Response(status=404, text="Link expired or not found.")
    token  = request.match_info["token"]
    dl_url = f"{request.scheme}://{request.host}/dl/{token}"
    mime   = e["mime"]
    tag    = "video" if mime.startswith("video") else "audio" if mime.startswith("audio") else "video"
    html   = _TEMPLATE.format(
        file_name=e["filename"], file_url=dl_url,
        file_size=_human_size(e["size"]), tag=tag,
    )
    return web.Response(text=html, content_type="text/html")


@routes.get("/dl/{token}")
async def dl_handler(request: web.Request):
    e = _entry(request.match_info["token"])
    if not e:
        return web.Response(status=404, text="Link expired.")

    file_size = e["size"]
    range_hdr = request.headers.get("Range", "")

    if range_hdr:
        parts       = range_hdr.replace("bytes=", "").split("-", 1)
        from_bytes  = int(parts[0]) if parts[0] else 0
        until_bytes = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
    else:
        from_bytes, until_bytes = 0, file_size - 1

    until_bytes = min(until_bytes, file_size - 1)
    req_length  = until_bytes - from_bytes + 1
    status      = 206 if range_hdr else 200

    resp_headers = {
        "Content-Type":        e["mime"],
        "Content-Length":      str(req_length),
        "Content-Range":       f"bytes {from_bytes}-{until_bytes}/{file_size}",
        "Content-Disposition": f'inline; filename="{e["filename"]}"',
        "Accept-Ranges":       "bytes",
    }

    # ── Pyrogram ByteStreamer (any size, fast) ────────────────────────────────
    if _pyro_client and e.get("tg_file_id"):
        if not hasattr(_pyro_client, "_streamer"):
            from utils.byte_streamer import ByteStreamer
            _pyro_client._streamer = ByteStreamer(_pyro_client)

        body = _pyro_client._streamer.stream_file(
            tg_file_id=e["tg_file_id"], file_size=file_size,
            mime=e["mime"], name=e["filename"],
            from_bytes=from_bytes, until_bytes=until_bytes,
        )
        return web.Response(status=status, body=body, headers=resp_headers)

    # ── CDN proxy fallback (≤ 20 MB) ─────────────────────────────────────────
    cdn_url = e.get("cdn_url")
    if not cdn_url:
        return web.Response(status=503, text="No backend available.")

    fwd = {"Range": range_hdr} if range_hdr else {}
    async with ClientSession() as sess:
        async with sess.get(cdn_url, headers=fwd) as tg:
            resp = web.StreamResponse(status=tg.status, headers=resp_headers)
            await resp.prepare(request)
            async for chunk in tg.content.iter_chunked(512 * 1024):
                await resp.write(chunk)
            return resp


_runner: Optional[web.AppRunner] = None


async def start_stream_server(port: int, pyro_client=None):
    global _runner
    if pyro_client:
        set_pyrogram_client(pyro_client)
    app = web.Application()
    app.add_routes(routes)
    _runner = web.AppRunner(app)
    await _runner.setup()
    await web.TCPSite(_runner, "0.0.0.0", port).start()
    log.info("Stream server on port %d", port)


async def stop_stream_server():
    global _runner
    if _runner:
        await _runner.cleanup()
        _runner = None
