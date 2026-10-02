"""
رفع الحلقات إلى قناة تليجرام عبر Pyrogram (MTProto).
يدعم الملفات حتى 2GB+.
"""

import asyncio
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import FloodWait

from config import config
from errors import UploadError


class Uploader:
    def __init__(self):
        self._client = None
        self._lock = asyncio.Lock()

    async def start(self):
        async with self._lock:
            if self._client:
                return
            kwargs = dict(
                api_id=config.API_ID,
                api_hash=config.API_HASH,
                no_updates=True,
            )
            if config.SESSION_STRING:
                kwargs["session_string"] = config.SESSION_STRING
            else:
                kwargs["bot_token"] = config.BOT_TOKEN

            self._client = Client("soo_uploader", **kwargs)
            await self._client.start()
            print("[Uploader] تم الاتصال بتليجرام")

    async def stop(self):
        if self._client:
            await self._client.stop()
            self._client = None

    async def upload_episode(
        self,
        series: str,
        episode: int,
        file_path: Path,
        caption: str = None,
    ) -> int:
        """يرفع الحلقة ويعيد message_id. يرفع UploadError عند الفشل."""
        if not self._client:
            await self.start()

        caption = caption or f"📺 {series} — الحلقة {episode}"

        for attempt in range(config.MAX_RETRIES + 1):
            try:
                msg = await self._client.send_video(
                    chat_id=config.CHANNEL_ID,
                    video=str(file_path),
                    caption=caption,
                    supports_streaming=True,
                    file_name=file_path.name,
                )
                print(f"    ✓ رُفعت الحلقة {episode} (message_id={msg.id})")
                return msg.id
            except FloodWait as e:
                print(f"    ↳ FloodWait: انتظار {e.value}s")
                await asyncio.sleep(e.value)
            except Exception as e:
                if attempt < config.MAX_RETRIES:
                    print(f"    ↳ محاولة {attempt + 1} فشلت: {e}")
                    await asyncio.sleep(config.RETRY_DELAY)
                else:
                    raise UploadError(f"فشل رفع {series} الحلقة {episode}: {e}", e)

        raise UploadError(f"فشل رفع {series} الحلقة {episode} بعد كل المحاولات")


uploader = Uploader()