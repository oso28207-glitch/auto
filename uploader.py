"""رفع إلى قناة Telegram عبر Pyrogram."""

import asyncio
import subprocess
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import FloodWait

from config import config
from errors import UploadError


def _meta(video_path):
    """(width, height, duration)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=60,
        )
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        w = int(float(lines[0])) if len(lines) > 0 else 0
        h = int(float(lines[1])) if len(lines) > 1 else 0
        d = int(float(lines[2])) if len(lines) > 2 else 0
        return w or 640, h or 360, d
    except Exception:
        return 640, 360, 0


def _thumb(video_path, thumb_path):
    """يولّد صورة مصغّرة."""
    for ts in ["00:00:05", "00:00:01", "00:00:00"]:
        r = subprocess.run(
            ["ffmpeg", "-err_detect", "ignore_err", "-fflags", "+discardcorrupt",
             "-ss", ts, "-i", str(video_path),
             "-vframes", "1", "-vf", "scale=320:180",
             "-f", "image2", "-y", str(thumb_path)],
            capture_output=True, timeout=20,
        )
        if r.returncode == 0 and thumb_path.exists() and thumb_path.stat().st_size > 1024:
            return True
    return False


class Uploader:
    def __init__(self):
        self._client = None
        self._lock = asyncio.Lock()

    async def start(self):
        async with self._lock:
            if self._client:
                return
            self._client = Client(
                "shoof_uploader",
                api_id=config.API_ID,
                api_hash=config.API_HASH,
                session_string=config.SESSION_STRING,
                in_memory=True,
                no_updates=True,
            )
            await self._client.start()
            me = await self._client.get_me()
            print(f"[Uploader] ✅ {me.first_name or me.id}")

    async def stop(self):
        if self._client:
            await self._client.stop()
            self._client = None

    async def upload(self, file_path: Path, caption: str) -> int:
        if not self._client:
            await self.start()
        if not file_path.exists():
            raise UploadError(f"ملف غير موجود: {file_path}")

        w, h, d = _meta(file_path)
        thumb = file_path.with_suffix(".jpg")
        has_thumb = _thumb(file_path, thumb)

        size_mb = file_path.stat().st_size / 1048576
        print(f"   📤 رفع {size_mb:.1f}MB | {w}x{h} | {d}s...")

        for attempt in range(3):
            try:
                msg = await self._client.send_video(
                    chat_id=config.CHANNEL_ID,
                    video=str(file_path),
                    caption=caption,
                    supports_streaming=True,
                    width=w, height=h, duration=d,
                    thumb=str(thumb) if has_thumb else None,
                    file_name=file_path.name,
                )
                if has_thumb and thumb.exists():
                    thumb.unlink()
                print(f"   ✅ message_id={msg.id}")
                return msg.id
            except FloodWait as e:
                print(f"   ⏳ FloodWait {e.value}s")
                await asyncio.sleep(e.value)
            except Exception as e:
                if attempt < 2:
                    print(f"   ⚠️ محاولة {attempt+1}: {str(e)[:100]}")
                    await asyncio.sleep(5)
                else:
                    raise UploadError(f"فشل رفع {file_path.name}: {e}")

        raise UploadError(f"فشل رفع {file_path.name}")


uploader = Uploader()