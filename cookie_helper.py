"""
cookie_helper.py

Provides get_cookie_file() — returns a path to a temp cookies.txt file
loaded from MongoDB. Call this before each yt-dlp invocation.

The caller is responsible for deleting the temp file after use,
or use it inside a tempfile.TemporaryDirectory() context.

Usage:
    cookie_path = await get_cookie_file()
    if cookie_path:
        ydl_opts["cookiefile"] = cookie_path
    # ... run yt-dlp ...
    # temp file is cleaned up automatically when TemporaryDirectory exits

Implementation note:
  We write a fresh temp file on each call rather than keeping a persistent
  file on disk. This means:
    - No stale file if admin uploads new cookies while bot is running
    - No disk leaks between requests
    - Works on ephemeral cloud containers (Koyeb, Fly, Railway)
"""

import os
import tempfile

import database as db


async def get_cookie_file(tmp_dir: str | None = None) -> str | None:
    """
    Fetch cookies from MongoDB and write them to a temp file.

    Args:
        tmp_dir: directory to write the file into (e.g. a TemporaryDirectory).
                 If None, uses the system temp dir — caller must delete.

    Returns:
        Absolute path to the cookies.txt temp file, or None if no cookies stored.
    """
    content = await db.get_cookies()
    if not content:
        return None

    # Validate it looks like a Netscape cookie file
    lines = content.strip().splitlines()
    valid = any(
        l.startswith("# Netscape HTTP Cookie File") or
        (not l.startswith("#") and len(l.split("\t")) >= 6)
        for l in lines[:10]
    )
    if not valid:
        return None

    if tmp_dir:
        path = os.path.join(tmp_dir, "yt_cookies.txt")
    else:
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="yt_cookies_")
        os.close(fd)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    return path
