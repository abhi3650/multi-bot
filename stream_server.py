"""
stream_server.py

aiohttp streaming server.
  - Pyrogram available → ByteStreamer via raw MTProto (any file size, up to 4 GB)
  - Fallback           → CDN proxy (≤ 20 MB Bot API limit)

Endpoints:
  GET /watch/{token}  → HTML player page
  GET /dl/{token}     → raw bytes, range-aware
"""

import logging
import mimetypes
import secrets
import time
from typing import Optional

from aiohttp import web, ClientSession

logger = logging.getLogger(__name__)

_TOKENS: dict[str, dict] = {}
LINK_TTL = 3600   # 1 hour

# ── Inline watch page — no external file dependency ───────────────────────────
# Placeholders filled at request time:
#   {title}  {filename}  {size}  {mime}  {tag}  {dl_url}
_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{title}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      min-height: 100vh;
      background: #0f0f0f;
      color: #e0e0e0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 24px 16px;
      gap: 20px;
    }}
    header {{
      width: 100%;
      max-width: 860px;
      border-bottom: 1px solid #2a2a2a;
      padding-bottom: 12px;
    }}
    header h1 {{
      font-size: 1.1rem;
      font-weight: 600;
      color: #fff;
      word-break: break-all;
    }}
    header p {{
      font-size: 0.82rem;
      color: #888;
      margin-top: 4px;
    }}
    .player-wrap {{
      width: 100%;
      max-width: 860px;
      background: #1a1a1a;
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 8px 32px rgba(0,0,0,.6);
    }}
    {tag} {{
      width: 100%;
      display: block;
      max-height: 72vh;
      background: #000;
    }}
    .actions {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      width: 100%;
      max-width: 860px;
    }}
    a.btn {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 20px;
      border-radius: 8px;
      font-size: 0.9rem;
      font-weight: 500;
      text-decoration: none;
      transition: opacity .15s;
    }}
    a.btn:hover {{ opacity: .85; }}
    .btn-dl  {{ background: #2563eb; color: #fff; }}
    .btn-cp  {{ background: #1f2937; color: #e0e0e0; border: 1px solid #374151; cursor: pointer; }}
    footer {{
      font-size: 0.75rem;
      color: #444;
      margin-top: auto;
    }}
  </style>
</head>
<body>
  <header>
    <h1>📄 {filename}</h1>
    <p>Size: {size} &nbsp;•&nbsp; Type: {mime}</p>
  </header>

  <div class="player-wrap">
    <{tag} controls preload="metadata" src="{dl_url}">
      Your browser does not support this media type.
    </{tag}>
  </div>

  <div class="actions">
    <a class="btn btn-dl" href="{dl_url}" download="{filename}">⬇️ Download</a>
    <a class="btn btn-cp" onclick="copyLink(this)">🔗 Copy Link</a>
  </div>

  <footer>Link expires in 1 hour &nbsp;•&nbsp; Powered by the bot</footer>

  <script>
    function copyLink(btn) {{
      navigator.clipboard.writeText('{dl_url}').then(() => {{
        btn.textContent = '✅ Copied!';
        setTimeout(() => btn.textContent = '🔗 Copy Link', 2000);
      }});
    }}
    // Attempt to resume from last position via localStorage
    const player = document.querySelector('video, audio');
    const key = 'pos_{title}';
    if (player) {{
      const saved = parseFloat(localStorage.getItem(key) || '0');
      if (saved > 2) player.currentTime = saved;
      player.addEventListener('timeupdate', () => {{
        if (!player.paused) localStorage.setItem(key, player.currentTime);
      }});
    }}
  </script>
</body>
</html>"""


def create_token(
    filename:   str,
    size:       int,
    mime:       str,
    tg_file_id: Optional[str] = None,
    cdn_url:    Optional[str] = None,
) -> str:
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
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
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
        title=e["filename"], filename=e["filename"],
        size=_human_size(e["size"]), mime=mime, tag=tag, dl_url=dl_url,
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
        rng   = range_hdr.replace("bytes=", "")
        parts = rng.split("-", 1)
        from_bytes  = int(parts[0]) if parts[0] else 0
        until_bytes = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
    else:
        from_bytes  = 0
        until_bytes = file_size - 1

    until_bytes = min(until_bytes, file_size - 1)
    if from_bytes < 0 or until_bytes < from_bytes or until_bytes >= file_size:
        return web.Response(
            status=416, text="Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    req_length = until_bytes - from_bytes + 1
    status     = 206 if range_hdr else 200
    resp_headers = {
        "Content-Type":        e["mime"],
        "Content-Length":      str(req_length),
        "Content-Range":       f"bytes {from_bytes}-{until_bytes}/{file_size}",
        "Content-Disposition": f'inline; filename="{e["filename"]}"',
        "Accept-Ranges":       "bytes",
        "Cache-Control":       "public, max-age=3600",
    }

    # Backend A: Pyrogram ByteStreamer
    import pyrogram_helper as pyro
    if pyro.is_available() and e.get("tg_file_id"):
        streamer = pyro.get_streamer()
        body = streamer.stream_file(
            tg_file_id=e["tg_file_id"], file_size=file_size,
            mime_type=e["mime"], file_name=e["filename"],
            from_bytes=from_bytes, until_bytes=until_bytes,
        )
        return web.Response(status=status, body=body, headers=resp_headers)

    # Backend B: CDN proxy fallback
    cdn_url = e.get("cdn_url")
    if not cdn_url:
        return web.Response(status=503, text="File not accessible.")

    fwd_headers = {"Range": range_hdr} if range_hdr else {}
    async with ClientSession() as session:
        async with session.get(cdn_url, headers=fwd_headers) as tg:
            resp = web.StreamResponse(status=tg.status, headers=resp_headers)
            await resp.prepare(request)
            async for chunk in tg.content.iter_chunked(512 * 1024):
                await resp.write(chunk)
            return resp


_runner: Optional[web.AppRunner] = None


async def start_stream_server(port: int):
    global _runner
    app = web.Application()
    app.add_routes(routes)
    _runner = web.AppRunner(app)
    await _runner.setup()
    await web.TCPSite(_runner, "0.0.0.0", port).start()
    logger.info("Stream server started on port %d", port)


async def stop_stream_server():
    global _runner
    if _runner:
        await _runner.cleanup()
        _runner = None
