"""
stream_server.py — Permanent file streaming server

Architecture (adapted from FileStreamBot):
  - Files sent to /link are forwarded to DUMP_CHANNEL
  - MongoDB stores {msg_id, file_id, file_name, file_size, mime_type}
  - Permanent URL: {STREAM_BASE_URL}/watch/{mongo_id}  (HTML player)
  -                {STREAM_BASE_URL}/dl/{mongo_id}      (raw bytes, Range-aware)
  - Links never expire — files live in DUMP_CHANNEL forever

Streaming backend: Pyrogram ByteStreamer (MTProto, any file size, up to 4 GB)
"""

import json
import logging
import math
import mimetypes
import time
from typing import Optional

from aiohttp import web, ClientSession

logger = logging.getLogger(__name__)

# ── Inline HTML player ────────────────────────────────────────────────────────
_WATCH_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>{title}</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{min-height:100vh;background:#0f0f0f;color:#e0e0e0;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         display:flex;flex-direction:column;align-items:center;padding:24px 16px;gap:20px}}
    header{{width:100%;max-width:860px;border-bottom:1px solid #2a2a2a;padding-bottom:12px}}
    header h1{{font-size:1.1rem;font-weight:600;color:#fff;word-break:break-all}}
    header p{{font-size:.82rem;color:#888;margin-top:4px}}
    .player-wrap{{width:100%;max-width:860px;background:#1a1a1a;border-radius:12px;
                  overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,.6)}}
    {tag}{{width:100%;display:block;max-height:72vh;background:#000}}
    .actions{{display:flex;gap:12px;flex-wrap:wrap;width:100%;max-width:860px}}
    a.btn{{display:inline-flex;align-items:center;gap:8px;padding:10px 20px;
           border-radius:8px;font-size:.9rem;font-weight:500;text-decoration:none;
           transition:opacity .15s}}
    a.btn:hover{{opacity:.85}}
    .btn-dl{{background:#2563eb;color:#fff}}
    footer{{font-size:.75rem;color:#444;margin-top:auto}}
  </style>
</head>
<body>
  <header>
    <h1>📄 {filename}</h1>
    <p>Size: {size} &nbsp;•&nbsp; {mime}</p>
  </header>
  <div class="player-wrap">
    <{tag} controls preload="metadata" src="{dl_url}">
      Your browser does not support this media type.
    </{tag}>
  </div>
  <div class="actions">
    <a class="btn btn-dl" href="{dl_url}" download="{filename}">⬇️ Download</a>
  </div>
  <footer>Permanent link • Powered by @{bot}</footer>
  <script>
    var player=document.querySelector('video,audio');
    var key='pos_{safe_title}';
    if(player){{
      var saved=parseFloat(localStorage.getItem(key)||'0');
      if(saved>2)player.currentTime=saved;
      player.addEventListener('timeupdate',function(){{
        if(!player.paused)localStorage.setItem(key,player.currentTime);
      }});
    }}
  </script>
</body>
</html>"""


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


routes = web.RouteTableDef()


@routes.get("/")
async def health(_: web.Request):
    return web.Response(text="<h2>Stream server ✅</h2>", content_type="text/html")


@routes.get("/watch/{file_id}")
async def watch_handler(request: web.Request):
    mongo_id = request.match_info["file_id"]
    import database as db_module
    file_info = await db_module.get_link_file(mongo_id)
    if not file_info:
        return web.Response(status=404, text="File not found.")

    base    = request.scheme + "://" + request.host
    dl_url  = f"{base}/dl/{mongo_id}"
    fname   = file_info.get("file_name", "file")
    mime    = file_info.get("mime_type", "application/octet-stream")
    size    = _human_size(file_info.get("file_size", 0))
    tag     = "video" if mime.startswith("video") else "audio" if mime.startswith("audio") else "video"
    from config import BOT_USERNAME

    html = _WATCH_TEMPLATE.format(
        title=fname, filename=fname, size=size, mime=mime,
        tag=tag, dl_url=dl_url, bot=BOT_USERNAME,
        safe_title=fname.replace("'", "").replace('"', "")[:30],
    )
    return web.Response(text=html, content_type="text/html")


@routes.get("/dl/{file_id}")
async def dl_handler(request: web.Request):
    mongo_id = request.match_info["file_id"]
    import database as db_module
    import pyrogram_helper as pyro

    file_info = await db_module.get_link_file(mongo_id)
    if not file_info:
        return web.Response(status=404, text="File not found.")

    file_size  = file_info.get("file_size", 0)
    mime_type  = file_info.get("mime_type", "application/octet-stream")
    file_name  = file_info.get("file_name", "file")
    tg_file_id = file_info.get("tg_file_id", "")

    range_hdr = request.headers.get("Range", "")
    if range_hdr:
        parts       = range_hdr.replace("bytes=", "").split("-", 1)
        from_bytes  = int(parts[0]) if parts[0] else 0
        until_bytes = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
    else:
        from_bytes  = 0
        until_bytes = file_size - 1

    until_bytes = min(until_bytes, file_size - 1)
    if from_bytes < 0 or until_bytes < from_bytes:
        return web.Response(
            status=416, text="Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    req_length = until_bytes - from_bytes + 1
    status     = 206 if range_hdr else 200
    headers    = {
        "Content-Type":        mime_type,
        "Content-Length":      str(req_length),
        "Content-Range":       f"bytes {from_bytes}-{until_bytes}/{file_size}",
        "Content-Disposition": f'inline; filename="{file_name}"',
        "Accept-Ranges":       "bytes",
        "Cache-Control":       "public, max-age=86400",
    }

    if pyro.is_available() and tg_file_id:
        streamer = pyro.get_streamer()
        body = streamer.stream_file(
            tg_file_id=tg_file_id, file_size=file_size,
            mime_type=mime_type, file_name=file_name,
            from_bytes=from_bytes, until_bytes=until_bytes,
        )
        return web.Response(status=status, body=body, headers=headers)

    return web.Response(status=503, text="Streaming backend unavailable.")


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
