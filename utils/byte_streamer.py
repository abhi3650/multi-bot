"""
utils/byte_streamer.py

Streams Telegram files of ANY size directly from Telegram's DC servers
using raw MTProto — no 20 MB Bot API limit.

Ported from FileStreamBot with concurrent prefetching and Range support.
"""

import asyncio
import logging
import math
from typing import AsyncGenerator, Dict, Optional

from pyrogram import Client, raw, utils
from pyrogram.errors import AuthBytesInvalid
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.session import Auth, Session

logger = logging.getLogger(__name__)

PREFETCH_COUNT = 3
CHUNK_SIZE     = 1 << 20   # 1 MB — Telegram's GetFile hard limit


class ByteStreamer:
    """Streams a Telegram file to an HTTP client using raw MTProto calls."""

    def __init__(self, client: Client):
        self.client  = client
        self._cache: Dict[str, FileId] = {}
        asyncio.get_event_loop().call_soon(
            lambda: asyncio.ensure_future(self._clean_cache())
        )

    def get_file_id(self, tg_file_id: str, file_size: int,
                    mime_type: str, file_name: str) -> FileId:
        if tg_file_id not in self._cache:
            fid           = FileId.decode(tg_file_id)
            fid.file_size = file_size
            fid.mime_type = mime_type
            fid.file_name = file_name
            self._cache[tg_file_id] = fid
        return self._cache[tg_file_id]

    async def _get_media_session(self, file_id: FileId) -> Session:
        sessions      = getattr(self.client, "media_sessions", {})
        media_session = sessions.get(file_id.dc_id)

        if media_session is None:
            own_dc = await self.client.storage.dc_id()

            if file_id.dc_id != own_dc:
                media_session = Session(
                    self.client, file_id.dc_id,
                    await Auth(self.client, file_id.dc_id,
                               await self.client.storage.test_mode()).create(),
                    await self.client.storage.test_mode(),
                    is_media=True,
                )
                await media_session.start()
                for _ in range(6):
                    exported = await self.client.invoke(
                        raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id)
                    )
                    try:
                        await media_session.invoke(
                            raw.functions.auth.ImportAuthorization(
                                id=exported.id, bytes=exported.bytes
                            )
                        )
                        break
                    except AuthBytesInvalid:
                        logger.debug("Auth bytes invalid for DC %d, retrying…", file_id.dc_id)
                else:
                    await media_session.stop()
                    raise AuthBytesInvalid
            else:
                media_session = Session(
                    self.client, file_id.dc_id,
                    await self.client.storage.auth_key(),
                    await self.client.storage.test_mode(),
                    is_media=True,
                )
                await media_session.start()

            sessions[file_id.dc_id] = media_session
            if not hasattr(self.client, "media_sessions"):
                self.client.media_sessions = sessions

        return media_session

    @staticmethod
    def _location(file_id: FileId):
        ftype = file_id.file_type
        if ftype == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id,
                    access_hash=file_id.chat_access_hash,
                )
            elif file_id.chat_access_hash == 0:
                peer = raw.types.InputPeerChat(chat_id=-file_id.chat_id)
            else:
                peer = raw.types.InputPeerChannel(
                    channel_id=utils.get_channel_id(file_id.chat_id),
                    access_hash=file_id.chat_access_hash,
                )
            return raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                volume_id=file_id.volume_id,
                local_id=file_id.local_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )
        elif ftype == FileType.PHOTO:
            return raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        else:
            return raw.types.InputDocumentFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )

    async def stream_file(
        self,
        tg_file_id:  str,
        file_size:   int,
        mime_type:   str,
        file_name:   str,
        from_bytes:  int = 0,
        until_bytes: Optional[int] = None,
    ) -> AsyncGenerator[bytes, None]:
        if until_bytes is None or until_bytes >= file_size:
            until_bytes = file_size - 1

        file_id  = self.get_file_id(tg_file_id, file_size, mime_type, file_name)
        session  = await self._get_media_session(file_id)
        location = self._location(file_id)

        offset         = from_bytes - (from_bytes % CHUNK_SIZE)
        first_part_cut = from_bytes - offset
        last_part_cut  = until_bytes % CHUNK_SIZE + 1
        part_count     = (
            math.ceil(until_bytes / CHUNK_SIZE) - math.floor(offset / CHUNK_SIZE)
        )

        pending: Dict[int, asyncio.Task] = {}

        async def _fetch(off: int) -> Optional[bytes]:
            try:
                r = await session.invoke(
                    raw.functions.upload.GetFile(
                        location=location, offset=off, limit=CHUNK_SIZE
                    )
                )
                if isinstance(r, raw.types.upload.File):
                    return r.bytes
            except (TimeoutError, AttributeError, asyncio.CancelledError):
                pass
            return None

        def _schedule(off: int):
            max_valid = offset + (part_count - 1) * CHUNK_SIZE
            if off <= max_valid and off not in pending:
                pending[off] = asyncio.ensure_future(_fetch(off))

        next_prefetch = offset
        for _ in range(min(PREFETCH_COUNT, part_count)):
            _schedule(next_prefetch)
            next_prefetch += CHUNK_SIZE

        try:
            current_part   = 1
            current_offset = offset
            while current_part <= part_count:
                task  = pending.pop(current_offset, None)
                chunk = await task if task else await _fetch(current_offset)
                if not chunk:
                    break
                _schedule(next_prefetch)
                next_prefetch += CHUNK_SIZE

                if part_count == 1:
                    yield chunk[first_part_cut:last_part_cut]
                elif current_part == 1:
                    yield chunk[first_part_cut:]
                elif current_part == part_count:
                    yield chunk[:last_part_cut]
                else:
                    yield chunk

                current_part   += 1
                current_offset += CHUNK_SIZE
        except (TimeoutError, AttributeError):
            pass
        finally:
            for t in pending.values():
                t.cancel()

    async def _clean_cache(self):
        while True:
            await asyncio.sleep(30 * 60)
            self._cache.clear()
