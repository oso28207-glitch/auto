"""
uploader.py — رفع إلى Telegram مع تنسيق العنوان التلقائي

★ تنسيق العنوان:
  - مسلسل: «مسلسل {الاسم} الحلقة {رقم}»
  - فيلم: «فيلم {الاسم} الجزء {رقم}»
  - بدون جزء: «فيلم {الاسم}»
"""

import asyncio
import json
import subprocess
import time
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import FloodWait

from config import config
from errors import UploadError


# ═══════════════════════════════════════════════════════════════
# ★★ تنسيق العنوان
# ═══════════════════════════════════════════════════════════════
def format_caption(item_name: str, media_type: str,
                   part_number: int = 0, season: int = 0,
                   total_parts: int = 0, source: str = "") -> str:
    """
    ينشئ عنواناً منسقاً للرفع.

    أمثلة:
      - مسلسل احتمال حب، الحلقة 5  → «مسلسل احتمال حب الحلقة 5»
      - فيلم الفيل الأزرق، الجزء 2  → «فيلم الفيل الأزرق الجزء 2»
      - فيلم الفيل الأزرق، جزء 0    → «فيلم الفيل الأزرق»
    """
    name = item_name.strip()

    # احذف "مسلسل" أو "فيلم" من البداية لتجنب التكرار
    name_clean = name
    for prefix in ["مسلسل ", "فيلم ", "movie ", "series "]:
        if name_clean.lower().startswith(prefix.lower()):
            name_clean = name_clean[len(prefix):].strip()
            break

    if media_type == "movie":
        if part_number and part_number > 0:
            return f"فيلم {name_clean} الجزء {part_number}"
        return f"فيلم {name_clean}"
    else:
        # مسلسل
        if season and season > 0:
            return f"مسلسل {name_clean} الموسم {season} الحلقة {part_number}"
        return f"مسلسل {name_clean} الحلقة {part_number}"


# ═══════════════════════════════════════════════════════════════
# ffprobe
# ═══════════════════════════════════════════════════════════════
def _ffprobe(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_type,codec_name,width,height",
             "-show_entries", "format=duration,size",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        info = {"codec_v": None, "codec_a": None,
                "width": 0, "height": 0, "duration": 0, "size": 0}
        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and not info["codec_v"]:
                info["codec_v"] = s.get("codec_name", "").lower()
                info["width"] = int(s.get("width", 0) or 0)
                info["height"] = int(s.get("height", 0) or 0)
            elif s.get("codec_type") == "audio" and not info["codec_a"]:
                info["codec_a"] = s.get("codec_name", "").lower()
        fmt = data.get("format", {})
        info["duration"] = int(float(fmt.get("duration", 0) or 0))
        info["size"] = int(fmt.get("size", 0) or 0)
        return info
    except Exception:
        return None


def _is_valid(info):
    if not info:
        return False
    return (info["codec_v"] == "h264"
            and info["codec_a"] in ("aac", "mp3", None)
            and info["duration"] > 0)


def _thumbnail(video, thumb):
    for ts in ["00:00:10", "00:00:05", "00:00:01"]:
        try:
            r = subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-ss", ts, "-i", str(video), "-vframes", "1",
                 "-vf", "scale=320:-2", "-y", str(thumb)],
                capture_output=True, timeout=30,
            )
            if r.returncode == 0 and thumb.exists() and thumb.stat().st_size > 1024:
                return True
        except Exception:
            continue
    return False


# ═══════════════════════════════════════════════════════════════
# Uploader
# ═══════════════════════════════════════════════════════════════
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
                in_memory=True, no_updates=True,
            )
            await self._client.start()
            me = await self._client.get_me()
            print(f"[Uploader] ✅ {me.first_name or me.id}")

    async def stop(self):
        if self._client:
            await self._client.stop()
            self._client = None

    async def upload(self, file_path, item_name, media_type="series",
                     part_number=0, season=0, source=""):
        """
        يرفع الفيديو مع عنوان منسق.
        ★ item_name: اسم العمل الأصلي.
        """
        if not self._client:
            await self.start()

        file_path = Path(file_path)
        if not file_path.exists():
            raise UploadError(f"ملف غير موجود: {file_path}")

        # فحص
        info = _ffprobe(file_path)
        if not _is_valid(info):
            print(f"   ⚠️ فيديو غير صالح — إعادة ترميز...")
            temp = file_path.with_suffix(".fix.mp4")
            cmd = [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", str(file_path),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-profile:v", "main", "-level", "3.1", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "48k", "-ac", "2",
                "-movflags", "+faststart", "-y", str(temp),
            ]
            try:
                subprocess.run(cmd, capture_output=True, timeout=1800)
                if temp.exists() and temp.stat().st_size > 10000:
                    file_path.unlink()
                    temp.rename(file_path)
                    info = _ffprobe(file_path)
            except Exception:
                pass

        if not _is_valid(info):
            raise UploadError(f"الفيديو غير صالح للرفع")

        # عنوان منسق
        caption = format_caption(
            item_name=item_name,
            media_type=media_type,
            part_number=part_number,
            season=season,
            source=source,
        )

        w = info["width"] or 426
        h = info["height"] or 240
        d = info["duration"] or 0
        thumb = file_path.with_suffix(".jpg")
        has_thumb = _thumbnail(file_path, thumb)

        size_mb = file_path.stat().st_size / 1048576
        print(f"   📤 رفع: «{caption}» | {size_mb:.1f}MB | {w}x{h} | {d}s")

        msg = None
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
                break
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                if attempt < 2:
                    print(f"   ⚠️ محاولة {attempt+1}: {str(e)[:80]}")
                    await asyncio.sleep(5)

        if has_thumb and thumb.exists():
            thumb.unlink()

        if not msg:
            raise UploadError(f"فشل رفع {file_path.name}")

        media = msg.video or msg.document
        file_id = media.file_id if media else ""
        file_size = media.file_size if media and media.file_size else file_path.stat().st_size
        duration = media.duration if media and media.duration else d

        print(f"   ✅ msg_id={msg.id} | fid={file_id[:20]}...")

        return {
            "message_id": msg.id,
            "file_id": file_id,
            "size": file_size,
            "width": w, "height": h,
            "duration": duration,
            "caption": caption,
        }


uploader = Uploader()