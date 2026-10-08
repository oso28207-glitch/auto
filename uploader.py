"""
uploader.py — رفع مع تنسيق الأسماء المطلوب

★ التنسيق:
  مسلسل: {اسم} الموسم {X} الحلقة {Y}
  فيلم:  {اسم} الجزء {X}
"""

import asyncio
import json
import re
import subprocess
import time
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import FloodWait

from config import config
from errors import UploadError


# ═══════════════════════════════════════════════════════════════
# ★★★ تنسيق الاسم الصحيح
# ═══════════════════════════════════════════════════════════════
def format_caption(item_name: str, media_type: str,
                   part_number: int = 0, season: int = 0,
                   item_name_ar: str = "") -> str:
    """
    أمثلة:
      مسلسل "حياتي الرائعة" موسم 1 حلقة 5  →  «حياتي الرائعة الموسم 1 الحلقة 5»
      فيلم "افاتار" جزء 2                    →  «افاتار الجزء 2»
      مسلسل بدون موسم                        →  «حياتي الرائعة الحلقة 5»
    """
    name = (item_name_ar or item_name or "").strip()

    # احذف البوادئ المكررة
    for prefix in ["مسلسل ", "فيلم ", "series ", "movie "]:
        if name.lower().startswith(prefix.lower()):
            name = name[len(prefix):].strip()
            break

    # نظّف الرموز الخاصة (احتفظ بالعربية والأرقام والمسافات)
    name = re.sub(r"[^\w\s\u0600-\u06FF\-\.]", "", name).strip()

    if media_type == "movie":
        if part_number and part_number > 0:
            return f"{name} الجزء {part_number}"
        return name
    else:
        if season and season > 0:
            return f"{name} الموسم {season} الحلقة {part_number}"
        return f"{name} الحلقة {part_number}"


# ═══════════════════════════════════════════════════════════════
# ★★★ التحقق من الفيديو قبل الرفع
# ═══════════════════════════════════════════════════════════════
def _verify_video(path):
    """
    فحص شامل:
      - codec الفيديو = h264
      - codec الصوت = aac
      - duration > 0
      - resolution صحيحة
      - يقرأ 2 frames حقيقية
    يعيد: (info, error_msg)
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_type,codec_name,width,height",
             "-show_entries", "format=duration,size",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return None, "ffprobe فشل"

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

        if info["codec_v"] != "h264":
            return info, f"codec فيديو غير صالح: {info['codec_v']}"
        if info["codec_a"] not in ("aac", "mp3", None):
            return info, f"codec صوت غير صالح: {info['codec_a']}"
        if info["duration"] <= 0:
            return info, "المدة صفر"
        if info["width"] < 100 or info["height"] < 100:
            return info, f"resolution صغيرة: {info['width']}x{info['height']}"

        # اقرأ 2 frames
        r2 = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", str(path), "-vframes", "2", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        if r2.returncode != 0:
            return info, f"الفيديو لا يُقرأ: {(r2.stderr or '')[:200]}"

        return info, None
    except Exception as e:
        return None, str(e)[:200]


def _reencode_h264_aac(src, dst):
    """إعادة ترميز لضمان صلاحية Telegram."""
    print(f"   🔄 إعادة ترميز H.264 + AAC...")
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-profile:v", "main", "-level", "3.1", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "48k", "-ac", "2", "-ar", "44100",
        "-movflags", "+faststart",
        "-y", str(dst),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not Path(dst).exists():
            return False
        mb = Path(dst).stat().st_size / 1048576
        print(f"   ✅ أُعيد الترميز: {mb:.1f}MB")
        return True
    except Exception as e:
        print(f"   ❌ {str(e)[:100]}")
        return False


def _make_thumbnail(video, thumb):
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
                     part_number=0, season=0, item_name_ar=""):
        """يرفع بعد التحقق الكامل من الفيديو."""
        if not self._client:
            await self.start()

        file_path = Path(file_path)
        if not file_path.exists():
            raise UploadError(f"ملف غير موجود: {file_path}")

        # ★★★ 1) تحقق
        print(f"   🔍 فحص الفيديو قبل الرفع...")
        info, err = _verify_video(file_path)

        if err:
            print(f"   ⚠️ {err}")
            print(f"   🔄 محاولة إعادة الترميز...")
            temp = file_path.with_suffix(".fix.mp4")
            if not _reencode_h264_aac(file_path, temp):
                raise UploadError(f"فشل إعادة الترميز: {err}")
            try:
                file_path.unlink()
                temp.rename(file_path)
            except Exception:
                pass
            info, err = _verify_video(file_path)
            if err:
                raise UploadError(f"الفيديو لا يزال غير صالح: {err}")

        print(f"   ✅ صالح: {info['codec_v']}+{info['codec_a']}, "
              f"{info['width']}x{info['height']}, {info['duration']}s")

        # ★★★ 2) تكوين الاسم
        caption = format_caption(
            item_name=item_name,
            media_type=media_type,
            part_number=part_number,
            season=season,
            item_name_ar=item_name_ar,
        )

        # ★★★ 3) thumbnail
        thumb = file_path.with_suffix(".jpg")
        has_thumb = _make_thumbnail(file_path, thumb)

        w = info["width"]
        h = info["height"]
        d = info["duration"]
        size_mb = file_path.stat().st_size / 1048576

        print(f"   📤 «{caption}» | {size_mb:.1f}MB | {w}x{h} | {d}s")

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
                    thumb=str(thumb) if has_thumb else None,
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
                    if has_thumb and thumb.exists():
                        thumb.unlink()
                    raise UploadError(f"فشل الرفع: {e}")

        if has_thumb and thumb.exists():
            thumb.unlink()

        if not msg:
            raise UploadError("فشل الرفع")

        media = msg.video or msg.document
        file_id = media.file_id if media else ""
        file_size = media.file_size if media and media.file_size else file_path.stat().st_size
        duration = media.duration if media and media.duration else d

        print(f"   ✅ msg_id={msg.id}")

        return {
            "message_id": msg.id,
            "file_id": file_id,
            "size": file_size,
            "width": w, "height": h,
            "duration": duration,
            "caption": caption,
        }


uploader = Uploader()