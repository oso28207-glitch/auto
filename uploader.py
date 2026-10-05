"""
uploader.py — رفع إلى Telegram مع ضمان صلاحية الفيديو

★ الإضافات:
  1. فحص ffprobe قبل الرفع — يتحقق من:
     - codec الفيديو = h264 (شرط Telegram streaming)
     - codec الصوت = aac
     - duration > 0
     - width/height صحيحة
  2. إعادة ترميز إجبارية إذا فشل الفحص.
  3. رفع thumbnail منفصل.
  4. يحفظ file_id + size + duration الفعلية بعد الرفع.
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import FloodWait

from config import config
from errors import UploadError


# ═══════════════════════════════════════════════════════════════
# أدوات
# ═══════════════════════════════════════════════════════════════
def _ffprobe(path):
    """يعيد dict: {codec_v, codec_a, width, height, duration, size}."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries",
                "stream=codec_type,codec_name,width,height",
                "-show_entries", "format=duration,size",
                "-of", "json",
                str(path),
            ],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)

        info = {
            "codec_v": None, "codec_a": None,
            "width": 0, "height": 0,
            "duration": 0, "size": 0,
        }
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video" and not info["codec_v"]:
                info["codec_v"] = stream.get("codec_name", "").lower()
                info["width"] = int(stream.get("width", 0) or 0)
                info["height"] = int(stream.get("height", 0) or 0)
            elif stream.get("codec_type") == "audio" and not info["codec_a"]:
                info["codec_a"] = stream.get("codec_name", "").lower()

        fmt = data.get("format", {})
        info["duration"] = int(float(fmt.get("duration", 0) or 0))
        info["size"] = int(fmt.get("size", 0) or 0)
        return info
    except Exception:
        return None


def _is_valid_for_telegram(info):
    """
    شرط Telegram streaming:
      - H.264 video
      - AAC/MP3 audio
      - duration > 0
    """
    if not info:
        return False
    if info["codec_v"] != "h264":
        return False
    if info["codec_a"] not in ("aac", "mp3", None):
        return False
    if info["duration"] <= 0:
        return False
    return True


def _reencode_video(src, dst):
    """إعادة ترميز إجبارية لـ H.264 + AAC مع faststart."""
    print(f"   🔄 إعادة ترميز إجبارية إلى H.264+AAC...")
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "28",
        "-profile:v", "main",
        "-level", "3.1",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "64k",
        "-ac", "2",
        "-ar", "44100",
        "-movflags", "+faststart",
        "-y", str(dst),
    ]
    try:
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not Path(dst).exists():
            print(f"   ❌ فشل الترميز: {r.stderr[-200:]}")
            return False
        mb = Path(dst).stat().st_size / 1048576
        print(f"   ✅ أعيد ترميز: {mb:.1f}MB في {time.time()-t0:.1f}s")
        return True
    except Exception as e:
        print(f"   ❌ {str(e)[:150]}")
        return False


def _make_thumbnail(video_path, thumb_path):
    """يولّد صورة مصغرة."""
    for ts in ["00:00:10", "00:00:05", "00:00:01", "00:00:00"]:
        try:
            r = subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-ss", ts,
                    "-i", str(video_path),
                    "-vframes", "1",
                    "-vf", "scale=320:-2",
                    "-y", str(thumb_path),
                ],
                capture_output=True, timeout=30,
            )
            if (r.returncode == 0 and thumb_path.exists()
                    and thumb_path.stat().st_size > 1024):
                return True
        except Exception:
            continue
    return False


# ═══════════════════════════════════════════════════════════════
# ★★★ Uploader
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

    async def upload(self, file_path, caption):
        """
        يرفع الفيديو بعد التحقق من صلاحيته.
        يعيد: {message_id, file_id, size, width, height, duration}
        """
        if not self._client:
            await self.start()

        file_path = Path(file_path)
        if not file_path.exists():
            raise UploadError(f"ملف غير موجود: {file_path}")

        # ★★★ 1) فحص الملف
        info = _ffprobe(file_path)
        if not _is_valid_for_telegram(info):
            print(f"   ⚠️ الفيديو غير صالح للـ Telegram streaming:")
            print(f"      codec_v={info['codec_v'] if info else '?'}, "
                  f"codec_a={info['codec_a'] if info else '?'}, "
                  f"duration={info['duration'] if info else 0}s")

            # ★★★ 2) إعادة ترميز إجبارية
            temp = file_path.with_suffix(".reenc.mp4")
            if not _reencode_video(file_path, temp):
                raise UploadError(f"فشل إعادة ترميز {file_path.name}")

            # استبدل الأصلي
            try:
                file_path.unlink()
            except Exception:
                pass
            temp.rename(file_path)

            # اعد الفحص
            info = _ffprobe(file_path)
            if not _is_valid_for_telegram(info):
                raise UploadError(f"الفيديو لا يزال غير صالح بعد الترميز")
        else:
            print(f"   ✅ الفيديو صالح: "
                  f"{info['codec_v']}+{info['codec_a']}, "
                  f"{info['width']}x{info['height']}, "
                  f"{info['duration']}s")

        # ★★★ 3) thumbnail
        thumb_path = file_path.with_suffix(".jpg")
        has_thumb = _make_thumbnail(file_path, thumb_path)

        size_mb = file_path.stat().st_size / 1048576
        w = info["width"] or 640
        h = info["height"] or 360
        d = info["duration"] or 0

        print(f"   📤 رفع {size_mb:.1f}MB | {w}x{h} | {d}s...")

        # ★★★ 4) الرفع مع 3 محاولات
        msg = None
        for attempt in range(3):
            try:
                msg = await self._client.send_video(
                    chat_id=config.CHANNEL_ID,
                    video=str(file_path),
                    caption=caption,
                    supports_streaming=True,
                    width=w, height=h, duration=d,
                    thumb=str(thumb_path) if has_thumb else None,
                    file_name=file_path.name,
                )
                break
            except FloodWait as e:
                print(f"   ⏳ FloodWait {e.value}s")
                await asyncio.sleep(e.value)
            except Exception as e:
                if attempt < 2:
                    print(f"   ⚠️ محاولة {attempt+1}: {str(e)[:100]}")
                    await asyncio.sleep(5)
                else:
                    if has_thumb and thumb_path.exists():
                        thumb_path.unlink()
                    raise UploadError(f"فشل رفع {file_path.name}: {e}")

        if not msg:
            if has_thumb and thumb_path.exists():
                thumb_path.unlink()
            raise UploadError(f"فشل رفع {file_path.name}")

        # ★★★ 5) استخراج file_id من الرسالة
        media = msg.video or msg.document
        file_id = media.file_id if media else ""
        file_size = (media.file_size if media and media.file_size
                     else file_path.stat().st_size)
        duration = (media.duration if media and media.duration else d)

        if has_thumb and thumb_path.exists():
            thumb_path.unlink()

        print(f"   ✅ message_id={msg.id} | fid={file_id[:20]}... | "
              f"size={file_size}")

        return {
            "message_id": msg.id,
            "file_id": file_id,
            "size": file_size,
            "width": w,
            "height": h,
            "duration": duration,
        }


uploader = Uploader()