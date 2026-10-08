"""
telegram_checker.py — فحص قنوات Telegram قبل التحميل (v2)

★ البنية الجديدة:
{
    "اسم_مُنظّف": {
        "display_name": "الاسم الأصلي",
        "episodes": set([1, 2, 3, ...]),
        "message_ids": {1: msg_id, 2: msg_id, ...}
    },
    ...
}
"""

import asyncio
import re
import time

from pyrogram import Client
from pyrogram.errors import FloodWait

from config import config


CHECK_CHANNELS = config.CHECK_CHANNELS
MAX_MESSAGES_PER_CHANNEL = config.CHECK_CHANNEL_LIMIT
CACHE_TTL = config.CHECK_CACHE_TTL


# ═══════════════════════════════════════════════════════════════
# استخراج (المسلسل، الحلقة)
# ═══════════════════════════════════════════════════════════════
def _extract_series_episode(caption: str):
    if not caption:
        return None, None

    ep_num = None
    for p in [
        r"الحلقة\s*(\d+)",
        r"[Ee]pisode\s*(\d+)",
        r"حلقة\s*(\d+)",
        r"Ep\.?\s*(\d+)",
    ]:
        m = re.search(p, caption)
        if m:
            ep_num = int(m.group(1))
            break

    if ep_num is None:
        return None, None

    lines = caption.split("\n")
    series_name = None
    for line in lines:
        clean = re.sub(r"^[📺🎬🎥🎞️🔹\-•\s]+", "", line).strip()
        if "الحلقة" in clean or "حلقة" in clean.lower():
            continue
        if "episode" in clean.lower() or "ep." in clean.lower():
            continue
        if "الموسم" in clean:
            continue
        if clean and len(clean) > 2:
            series_name = clean
            break

    if not series_name:
        m = re.search(r"^(.*?)(?=الحلقة|حلقة|[Ee]pisode)", caption, re.DOTALL)
        if m:
            series_name = m.group(1).strip().split("\n")[0]
            series_name = re.sub(r"^[📺🎬🎥🎞️🔹\-•\s]+", "",
                                 series_name).strip()

    return series_name, ep_num


def _extract_media_info(msg):
    """يستخرج (file_id, file_size) من وسائط الرسالة (فيديو/مستند/أنيميشن/صوت)."""
    for attr in ("video", "document", "animation", "audio", "voice"):
        m = getattr(msg, attr, None)
        if m and getattr(m, "file_id", None):
            try:
                size = int(getattr(m, "file_size", 0) or 0)
            except Exception:
                size = 0
            return m.file_id, size
    return "", 0


def _normalize_name(name: str) -> str:
    if not name:
        return ""
    n = name.strip()
    # ★ إزالة التشكيل (harakat) والتطويل (tatweel)
    n = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", n)
    # ★ توحيد الحروف العربية: الهمزات → ا ، ى/ئ → ي ، ؤ → و ، ة → ه
    n = (n.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
          .replace("ٱ", "ا").replace("ى", "ي").replace("ئ", "ي")
          .replace("ؤ", "و").replace("ة", "ه"))
    n = re.sub(r"\s+", " ", n)
    n = re.sub(r"^مسلسل\s+", "", n)
    n = re.sub(r"\s+الموسم\s+.*$", "", n)
    n = re.sub(r"\s+\d+\s*$", "", n)
    n = re.sub(r"[^\w\s\u0600-\u06FF]", "", n)
    return n.strip().lower()


# ═══════════════════════════════════════════════════════════════
# الكاش
# ═══════════════════════════════════════════════════════════════
_cache = {"data": None, "timestamp": 0.0, "channels": []}


def _is_cache_valid() -> bool:
    if _cache["data"] is None:
        return False
    return (time.time() - _cache["timestamp"]) < CACHE_TTL


# ═══════════════════════════════════════════════════════════════
# الفحص الرئيسي
# ═══════════════════════════════════════════════════════════════
async def fetch_existing_episodes(session_string: str = None) -> dict:
    global _cache

    if _is_cache_valid():
        print(f"   💾 استخدام الكاش ({len(_cache['data'])} مسلسل)")
        return _cache["data"]

    if not CHECK_CHANNELS:
        print("   ℹ️  CHECK_CHANNELS فارغ — تخطي الفحص")
        return {}

    channels = [c.strip().replace("@", "")
                for c in CHECK_CHANNELS.split(",") if c.strip()]
    if not channels:
        return {}

    session = session_string or config.SESSION_STRING
    if not session:
        print("   ⚠️ لا يوجد SESSION_STRING — تخطي فحص القنوات")
        return {}

    print(f"\n📡 فحص القنوات: {', '.join(channels)}")

    result = {}
    total_episodes = 0

    try:
        app = Client(
            "shoof_checker",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=session,
            in_memory=True,
            no_updates=True,
        )
        await app.start()

        try:
            for channel in channels:
                try:
                    count = await _scan_channel(app, channel, result)
                    total_episodes += count
                    print(f"   ✅ @{channel}: {count} حلقة")
                except FloodWait as e:
                    print(f"   ⏳ FloodWait {e.value}s على @{channel}")
                    await asyncio.sleep(e.value)
                    try:
                        count = await _scan_channel(app, channel, result)
                        total_episodes += count
                        print(f"   ✅ @{channel}: {count} حلقة (بعد انتظار)")
                    except Exception as e2:
                        print(f"   ⚠️ فشل فحص @{channel}: {str(e2)[:150]}")
                except Exception as e:
                    print(f"   ⚠️ فشل فحص @{channel}: {str(e)[:150]}")
        finally:
            await app.stop()
    except Exception as e:
        print(f"   ⚠️ فشل تشغيل العميل: {str(e)[:200]}")
        return {}

    print(f"   📊 الإجمالي: {total_episodes} حلقة في {len(result)} مسلسل")

    _cache["data"] = result
    _cache["timestamp"] = time.time()
    _cache["channels"] = channels

    return result


async def _scan_channel(app: Client, channel: str, result: dict) -> int:
    count = 0
    scanned = 0
    last_log = time.time()

    try:
        async for msg in app.get_chat_history(
            channel, limit=MAX_MESSAGES_PER_CHANNEL
        ):
            scanned += 1

            if time.time() - last_log > 30:
                print(f"      · @{channel}: فُحص {scanned} رسالة، {count} حلقة")
                last_log = time.time()

            if not (msg.video or msg.document or msg.animation):
                continue

            caption = msg.caption or msg.text or ""
            if not caption:
                continue

            series_name, ep_num = _extract_series_episode(caption)
            if not series_name or ep_num is None:
                continue

            key = _normalize_name(series_name)
            if not key or len(key) < 2:
                continue

            entry = result.setdefault(key, {
                "display_name": series_name,
                "episodes": set(),
                "message_ids": {},
                "file_ids": {},
                "sizes": {},
            })

            if not entry["display_name"] and series_name:
                entry["display_name"] = series_name

            # ★ التقاط file_id + size لجعل الحلقة قابلة للبث مباشرة من التليجرام
            fid, fsize = _extract_media_info(msg)
            if fid:
                entry.setdefault("file_ids", {})[ep_num] = fid
                entry.setdefault("sizes", {})[ep_num] = fsize

            entry["episodes"].add(ep_num)
            entry["message_ids"][ep_num] = msg.id
            count += 1

    except Exception as e:
        error_msg = str(e)[:150]
        if ("CHANNEL_INVALID" in error_msg or
                "USERNAME_NOT_OCCUPIED" in error_msg):
            raise Exception(f"القناة @{channel} غير موجودة أو غير متاحة")
        raise

    return count


# ═══════════════════════════════════════════════════════════════
# Helpers متوافقة مع البنيتين
# ═══════════════════════════════════════════════════════════════
def _get_episodes_set(entry) -> set:
    if isinstance(entry, set):
        return entry
    if isinstance(entry, dict):
        eps = entry.get("episodes", set())
        if isinstance(eps, set):
            return eps
        if isinstance(eps, list):
            return set(eps)
    return set()


def is_episode_uploaded(existing: dict, series_name: str,
                         ep_num: int) -> bool:
    if not existing:
        return False

    normalized = _normalize_name(series_name)
    if not normalized:
        return False

    if normalized in existing:
        return ep_num in _get_episodes_set(existing[normalized])

    for key, entry in existing.items():
        if not key:
            continue
        if key == normalized:
            return ep_num in _get_episodes_set(entry)
        if len(key) >= 5 and len(normalized) >= 5:
            if key in normalized or normalized in key:
                if ep_num in _get_episodes_set(entry):
                    return True

    return False


def get_stats(existing: dict) -> dict:
    if not existing:
        return {"series": 0, "episodes": 0}
    total = sum(len(_get_episodes_set(entry))
                for entry in existing.values())
    return {"series": len(existing), "episodes": total}


def get_display_name(existing: dict, series_name: str) -> str:
    if not existing:
        return series_name

    normalized = _normalize_name(series_name)
    if normalized in existing:
        entry = existing[normalized]
        if isinstance(entry, dict):
            return entry.get("display_name", series_name)

    for key, entry in existing.items():
        if not key:
            continue
        if len(key) >= 5 and len(normalized) >= 5:
            if key in normalized or normalized in key:
                if isinstance(entry, dict):
                    return entry.get("display_name", series_name)

    return series_name


def get_message_id(existing: dict, series_name: str, ep_num: int) -> int:
    if not existing:
        return 0

    normalized = _normalize_name(series_name)
    if normalized in existing:
        entry = existing[normalized]
        if isinstance(entry, dict):
            mids = entry.get("message_ids", {})
            return int(mids.get(ep_num, 0))

    for key, entry in existing.items():
        if not key:
            continue
        if len(key) >= 5 and len(normalized) >= 5:
            if key in normalized or normalized in key:
                if isinstance(entry, dict):
                    mids = entry.get("message_ids", {})
                    return int(mids.get(ep_num, 0))

    return 0


def _match_entry(existing: dict, series_name: str):
    """يُرجع (entry) المطابق للاسم (تطبيع كامل ثم احتواء نصّي)."""
    if not existing:
        return None
    normalized = _normalize_name(series_name)
    if not normalized:
        return None
    if normalized in existing:
        return existing[normalized]
    for key, entry in existing.items():
        if not key:
            continue
        if len(key) >= 5 and len(normalized) >= 5:
            if key in normalized or normalized in key:
                return entry
    return None


def get_file_id(existing: dict, series_name: str, ep_num: int) -> str:
    """يُرجع file_id للحلقة من نتيجة فحص التليجرام (أو "")."""
    entry = _match_entry(existing, series_name)
    if isinstance(entry, dict):
        fids = entry.get("file_ids", {})
        if isinstance(fids, dict):
            return fids.get(ep_num, "") or ""
    return ""


def get_size(existing: dict, series_name: str, ep_num: int) -> int:
    """يُرجع حجم ملف الحلقة (bytes) من نتيجة فحص التليجرام (أو 0)."""
    entry = _match_entry(existing, series_name)
    if isinstance(entry, dict):
        sizes = entry.get("sizes", {})
        if isinstance(sizes, dict):
            try:
                return int(sizes.get(ep_num, 0) or 0)
            except Exception:
                return 0
    return 0


def to_serializable(existing: dict) -> dict:
    if not existing:
        return {}

    out = {}
    for key, entry in existing.items():
        if isinstance(entry, set):
            out[key] = {
                "display_name": key,
                "episodes": sorted(entry),
                "message_ids": {},
                "file_ids": {},
                "sizes": {},
            }
        elif isinstance(entry, dict):
            eps = entry.get("episodes", set())
            if isinstance(eps, set):
                eps = sorted(eps)
            out[key] = {
                "display_name": entry.get("display_name", key),
                "episodes": eps,
                "message_ids": entry.get("message_ids", {}),
                "file_ids": entry.get("file_ids", {}),
                "sizes": entry.get("sizes", {}),
            }
    return out


if __name__ == "__main__":
    async def test():
        result = await fetch_existing_episodes()
        stats = get_stats(result)
        print(f"\n📊 إحصائيات:")
        print(f"   المسلسلات: {stats['series']}")
        print(f"   الحلقات: {stats['episodes']}")
        print(f"\n📋 عينة (5):")
        for name, entry in list(result.items())[:5]:
            if isinstance(entry, dict):
                dn = entry.get("display_name", name)
                eps = sorted(entry.get("episodes", set()))[:5]
                print(f"   · {dn} → {eps}")

    asyncio.run(test())