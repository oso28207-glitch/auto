"""
telegram_checker.py — فحص قنوات Telegram قبل التحميل

★ الوظيفة:
  - يجلب قائمة الحلقات الموجودة في القنوات المحددة.
  - يبني dict من {اسم_مسلسل_مُنظّف: set(أرقام حلقات)}.
  - يتيح لـ run.py تخطي هذه الحلقات.
"""

import asyncio
import os
import re
import time
from typing import Optional

from pyrogram import Client
from pyrogram.errors import FloodWait

from config import config


# ═══════════════════════════════════════════════════════════════
# الإعدادات
# ═══════════════════════════════════════════════════════════════
CHECK_CHANNELS = config.CHECK_CHANNELS
MAX_MESSAGES_PER_CHANNEL = config.CHECK_CHANNEL_LIMIT
CACHE_TTL = config.CHECK_CACHE_TTL


# ═══════════════════════════════════════════════════════════════
# استخراج (المسلسل، الحلقة) من رسالة
# ═══════════════════════════════════════════════════════════════
def _extract_series_episode(caption: str) -> tuple:
    """
    يستخرج (اسم المسلسل, رقم الحلقة) من نص الرسالة.
    يدعم صيغ متعددة.
    """
    if not caption:
        return None, None

    # استخرج رقم الحلقة
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

    # اسم المسلسل = أول سطر لا يحتوي على "الحلقة"
    lines = caption.split("\n")
    series_name = None

    for line in lines:
        # احذف الرموز والإيموجي في البداية
        clean = re.sub(r"^[📺🎬🎥🎞️🔹\-•\s]+", "", line).strip()

        # إذا كان السطر يحتوي على "الحلقة" → تجاهله
        if "الحلقة" in clean or "حلقة" in clean.lower():
            continue
        if "episode" in clean.lower() or "ep." in clean.lower():
            continue

        if clean and len(clean) > 2:
            series_name = clean
            break

    # إذا فشل، جرّب استخراج كل شيء قبل "الحلقة"
    if not series_name:
        m = re.search(r"^(.*?)(?=الحلقة|حلقة|[Ee]pisode)", caption, re.DOTALL)
        if m:
            series_name = m.group(1).strip()
            # خذ أول سطر فقط
            series_name = series_name.split("\n")[0]
            series_name = re.sub(r"^[📺🎬🎥🎞️🔹\-•\s]+", "", series_name).strip()

    return series_name, ep_num


def _normalize_name(name: str) -> str:
    """يُنظّف اسم المسلسل للمقارنة."""
    if not name:
        return ""
    n = name.strip()
    n = re.sub(r"\s+", " ", n)
    # احذف "مسلسل" من البداية
    n = re.sub(r"^مسلسل\s+", "", n)
    # احذف "الموسم X" من النهاية
    n = re.sub(r"\s+الموسم\s+.*$", "", n)
    # احذف أرقام في النهاية
    n = re.sub(r"\s+\d+\s*$", "", n)
    # احذف رموز خاصة
    n = re.sub(r"[^\w\s\u0600-\u06FF]", "", n)
    return n.strip().lower()


# ═══════════════════════════════════════════════════════════════
# الكاش
# ═══════════════════════════════════════════════════════════════
_cache = {
    "data": None,
    "timestamp": 0.0,
    "channels": [],
}


def _is_cache_valid() -> bool:
    if _cache["data"] is None:
        return False
    return (time.time() - _cache["timestamp"]) < CACHE_TTL


# ═══════════════════════════════════════════════════════════════
# ★★★ الفحص الرئيسي
# ═══════════════════════════════════════════════════════════════
async def fetch_existing_episodes(session_string: str = None) -> dict:
    """
    يجلب كل الحلقات الموجودة في القنوات المحددة.

    يعيد dict: {
        "normalized_series_name": set([1, 2, 3, ...]),
        ...
    }
    """
    global _cache

    # تحقق من الكاش
    if _is_cache_valid():
        print(f"   💾 استخدام الكاش ({len(_cache['data'])} مسلسل)")
        return _cache["data"]

    if not CHECK_CHANNELS:
        print("   ℹ️  CHECK_CHANNELS فارغ — تخطي الفحص")
        return {}

    channels = [
        c.strip().replace("@", "")
        for c in CHECK_CHANNELS.split(",")
        if c.strip()
    ]
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

    # احفظ في الكاش
    _cache["data"] = result
    _cache["timestamp"] = time.time()
    _cache["channels"] = channels

    return result


async def _scan_channel(app: Client, channel: str, result: dict) -> int:
    """يفحص قناة واحدة ويعيد عدد الحلقات المكتشفة."""
    count = 0
    scanned = 0
    last_log = time.time()

    try:
        async for msg in app.get_chat_history(channel, limit=MAX_MESSAGES_PER_CHANNEL):
            scanned += 1

            # اطبع تقدم كل 30 ثانية
            if time.time() - last_log > 30:
                print(f"      · @{channel}: فُحص {scanned} رسالة، {count} حلقة")
                last_log = time.time()

            # تجاهل الرسائل بدون وسائط
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

            result.setdefault(key, set()).add(ep_num)
            count += 1

    except Exception as e:
        error_msg = str(e)[:150]
        if "CHANNEL_INVALID" in error_msg or "USERNAME_NOT_OCCUPIED" in error_msg:
            raise Exception(f"القناة @{channel} غير موجودة أو غير متاحة")
        raise

    return count


# ═══════════════════════════════════════════════════════════════
# الدالة المساعدة للاستخدام في run.py
# ═══════════════════════════════════════════════════════════════
def is_episode_uploaded(existing: dict, series_name: str, ep_num: int) -> bool:
    """
    هل الحلقة موجودة في القنوات؟
    يجرّب مطابقات متعددة للاسم لتفادي فروق التنظيف.
    """
    if not existing:
        return False

    normalized = _normalize_name(series_name)
    if not normalized:
        return False

    # مطابقة مباشرة
    if normalized in existing:
        return ep_num in existing[normalized]

    # مطابقة جزئية (إذا كان أحد الاسمين يحتوي على الآخر)
    for key, eps in existing.items():
        if not key:
            continue
        if key == normalized:
            return ep_num in eps
        # احتواء جزئي إذا كان الاسم طويلاً بما يكفي
        if len(key) >= 5 and len(normalized) >= 5:
            if key in normalized or normalized in key:
                if ep_num in eps:
                    return True

    return False


def get_stats(existing: dict) -> dict:
    """إحصائيات للعرض."""
    if not existing:
        return {"series": 0, "episodes": 0}
    total = sum(len(eps) for eps in existing.values())
    return {"series": len(existing), "episodes": total}


# ═══════════════════════════════════════════════════════════════
# اختبار
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    async def test():
        result = await fetch_existing_episodes()
        stats = get_stats(result)
        print(f"\n📊 إحصائيات:")
        print(f"   المسلسلات: {stats['series']}")
        print(f"   الحلقات: {stats['episodes']}")
        print(f"\n📋 عينة:")
        for name, eps in list(result.items())[:10]:
            print(f"   · {name}: {sorted(eps)[:5]}...")

    asyncio.run(test())