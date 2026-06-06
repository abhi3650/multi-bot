"""
utils/byte_streamer.py
Streams any-size Telegram file directly from Telegram's DC servers via MTProto.
Ported from FileStreamBot. No 20 MB limit.
"""
import asyncio
import logging
import math
from typing import AsyncGenerator, Dict, Optional

from pyrogram import raw, utils
from pyrogram.errors import AuthBytesInvalid
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.session import Auth, Session

log = logging.getLogger(__name__)
CHUNK_SIZE     = 1 << 20   # 1 MB (Telegram's GetFile limit)
PREFETCH_COUNT = 3          # chunks fetched ahead in parallel


class ByteStreamer:
    def __init__(self, client):
        self.client  = client
        self._cache: Dict[str, FileId] = {}
        asyncio.get_event_loop().call_soon(
            lambda: asyncio.ensure_future(self._clean_cache())
        )

    def get_file_id(self, tg_file_id: str, file_size: int, mime: str, name: str) -> FileId:
        if tg_file_id not in self._cache:
            fid           = FileId.decode(tg_file_id)
            fid.file_size = file_size
            fid.mime_type = mime
            fid.file_name = name
            self._cache[tg_file_id] = fid
        return self._cache[tg_file_id]

    async def _media_session(self, file_id: FileId) -> Session:
        client   = self.client
        sessions = getattr(client, "media_sessions", {})
        session  = sessions.get(file_id.dc_id)
        if session is None:
            own_dc = await client.storage.dc_id()
            if file_id.dc_id != own_dc:
                session = Session(
                    client, file_id.dc_id,
                    await Auth(client, file_id.dc_id, await client.storage.test_mode()).create(),
                    await client.storage.test_mode(), is_media=True,
                )
                await session.start()
                for _ in range(6):
                    exp = await client.invoke(raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id))
                    try:
                        await session.invoke(raw.functions.auth.ImportAuthorization(id=exp.id, bytes=exp.bytes))
                        break
                    except AuthBytesInvalid:
                        pass
                else:
                    await session.stop()
                    raise AuthBytesInvalid
            else:
                session = Session(
                    client, file_id.dc_id,
                    await client.storage.auth_key(),
                    await client.storage.test_mode(), is_media=True,
                )
                await session.start()
            sessions[file_id.dc_id] = session
            client.media_sessions = sessions
        return session

    @staticmethod
    def _location(file_id: FileId):
        ft = file_id.file_type
        if ft == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(user_id=file_id.chat_id, access_hash=file_id.chat_access_hash)
            elif file_id.chat_access_hash == 0:
                peer = raw.types.InputPeerChat(chat_id=-file_id.chat_id)
            else:
                peer = raw.types.InputPeerChannel(
                    channel_id=utils.get_channel_id(file_id.chat_id),
                    access_hash=file_id.chat_access_hash,
                )
            return raw.types.InputPeerPhotoFileLocation(
                peer=peer, volume_id=file_id.volume_id, local_id=file_id.local_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )
        elif ft == FileType.PHOTO:
            return raw.types.InputPhotoFileLocation(
                id=file_id.media_id, access_hash=file_id.access_hash,
                file_reference=file_id.file_reference, thumb_size=file_id.thumbnail_size,
            )
        else:
            return raw.types.InputDocumentFileLocation(
                id=file_id.media_id, access_hash=file_id.access_hash,
                file_reference=file_id.file_reference, thumb_size=file_id.thumbnail_size,
            )

    async def stream_file(
        self,
        tg_file_id:  str,
        file_size:   int,
        mime:        str,
        name:        str,
        from_bytes:  int = 0,
        until_bytes: Optional[int] = None,
    ) -> AsyncGenerator[bytes, None]:
        if until_bytes is None or until_bytes >= file_size:
            until_bytes = file_size - 1

        file_id  = self.get_file_id(tg_file_id, file_size, mime, name)
        session  = await self._media_session(file_id)
        location = self._location(file_id)

        offset         = from_bytes - (from_bytes % CHUNK_SIZE)
        first_cut      = from_bytes - offset
        last_cut       = until_bytes % CHUNK_SIZE + 1
        part_count     = math.ceil(until_bytes / CHUNK_SIZE) - math.floor(offset / CHUNK_SIZE)
        pending: Dict[int, asyncio.Task] = {}

        async def _fetch(off: int) -> Optional[bytes]:
            try:
                r = await session.invoke(
                    raw.functions.upload.GetFile(location=location, offset=off, limit=CHUNK_SIZE)
                )
                return r.bytes if isinstance(r, raw.types.upload.File) else None
            except Exception:
                return None

        def _schedule(off: int):
            max_off = offset + (part_count - 1) * CHUNK_SIZE
            if off <= max_off and off not in pending:
                pending[off] = asyncio.ensure_future(_fetch(off))

        next_pre = offset
        for _ in range(min(PREFETCH_COUNT, part_count)):
            _schedule(next_pre)
            next_pre += CHUNK_SIZE

        try:
            cur_part   = 1
            cur_offset = offset
            while cur_part <= part_count:
                task  = pending.pop(cur_offset, None)
                chunk = await task if task else await _fetch(cur_offset)
                if not chunk:
                    break
                _schedule(next_pre)
                next_pre += CHUNK_SIZE
                if part_count == 1:
                    yield chunk[first_cut:last_cut]
                elif cur_part == 1:
                    yield chunk[first_cut:]
                elif cur_part == part_count:
                    yield chunk[:last_cut]
                else:
                    yield chunk
                cur_part   += 1
                cur_offset += CHUNK_SIZE
        except Exception:
            pass
        finally:
            for t in pending.values():
                t.cancel()

    async def _clean_cache(self):
        while True:
            await asyncio.sleep(1800)
            self._cache.clear()
