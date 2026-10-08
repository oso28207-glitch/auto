"""
import_from_channels.py — استيراد المسلسلات القديمة من قنوات Telegram

★ الوظيفة:
  - يفحص القنوات المحددة (shoofcima, shoofFilm).
  - يستخرج كل حلقة: message_id, file_id, size, duration, caption.
  - يحفظها في state.json كما لو رُفعت من u.3seq.
  - يبني الموقع (series.json + videos.json).
  - يتجاهل الحلقات الموجودة مسبقاً (idempotent).

الاستخدام:
    python import_from_channels.py
    python import_from_channels.py --channels shoofcima,shoofFilm --limit 5000
    python import_from_channels.py --dry-run
"""

import argparse
import asyncio
import os
import re
import sys
import time
from datetime import datetime, timezone

from pyrogram import Client
from pyrogram.errors import FloodWait, ChannelPrivate, UsernameNotOccupied

from config import config
from database import db


# ═══════════════════════════════════════════════════════════════
# استخراج (المسلسل، الحلقة) من caption
# ═══════════════════════════════════════════════════════════════
def _extract_series_episode(caption: str):
    """يعيد (اسم المسلسل, رقم الحلقة, الموسم) أو (None, None, None)."""
    if not caption:
        return None, None, None

    # رقم الحلقة
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

    # الموسم (اختياري)
    season_num = None
    for p in [
        r"الموسم\s+(?:ال)?([\d]+|[أ-ي]+)",
        r"[Ss]eason\s*(\d+)",
    ]:
        m = re.search(p, caption)
        if m:
            season_num = m.group(1)
            break

    if ep_num is None:
        return None, None, season_num

    # اسم المسلسل
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
        m = re.search(r"^(.*?)(?=الحلقة|حلقة|[Ee]pisode|الموسم)", caption, re.DOTALL)
        if m:
            series_name = m.group(1).strip().split("\n")[0]
            series_name = re.sub(r"^[📺🎬🎥🎞️🔹\-•\s]+", "", series_name).strip()

    return series_name, ep_num, season_num


def _normalize_series_name(name: str) -> str:
    """يُنظّف الاسم للمقارنة."""
    if not name:
        return ""
    n = re.sub(r"\s+", " ", name).strip()
    n = re.sub(r"^مسلسل\s+", "", n)
    return n


# ═══════════════════════════════════════════════════════════════
# استخراج بيانات الوسائط
# ═══════════════════════════════════════════════════════════════
def _extract_media_info(msg):
    """يعيد (file_id, file_size, duration, width, height, mime)."""
    media = msg.video or msg.document or msg.animation
    if not media:
        return None

    file_id = getattr(media, "file_id", None)
    file_size = getattr(media, "file_size", 0) or 0
    duration = getattr(media, "duration", 0) or 0
    width = getattr(media, "width", 0) or 0
    height = getattr(media, "height", 0) or 0
    mime = getattr(media, "mime_type", None) or "video/mp4"
    file_name = getattr(media, "file_name", None) or ""

    if not file_id:
        return None

    return {
        "file_id": file_id,
        "file_size": file_size,
        "duration": duration,
        "width": width,
        "height": height,
        "mime": mime,
        "file_name": file_name,
    }


# ═══════════════════════════════════════════════════════════════
# ★★★ المسح الرئيسي
# ═══════════════════════════════════════════════════════════════
async def import_channel(
    app: Client,
    channel: str,
    limit: int = 5000,
    dry_run: bool = False,
):
    """
    يفحص قناة واحدة ويستورد كل حلقاتها.
    يعيد dict إحصائيات.
    """
    stats = {
        "channel": channel,
        "scanned": 0,
        "imported": 0,
        "skipped": 0,
        "errors": 0,
        "series": set(),
    }

    print(f"\n{'═' * 60}")
    print(f"📡 فحص: @{channel}")
    print(f"{'═' * 60}")

    last_log = time.time()

    try:
        async for msg in app.get_chat_history(channel, limit=limit):
            stats["scanned"] += 1

            if time.time() - last_log > 15:
                print(f"   · فُحص {stats['scanned']} | "
                      f"استُورد {stats['imported']} | "
                      f"تخطّي {stats['skipped']}")
                last_log = time.time()

            # تجاهل الرسائل بدون وسائط
            if not (msg.video or msg.document or msg.animation):
                stats["skipped"] += 1
                continue

            caption = msg.caption or msg.text or ""
            if not caption:
                stats["skipped"] += 1
                continue

            series_name, ep_num, season = _extract_series_episode(caption)
            if not series_name or ep_num is None:
                stats["skipped"] += 1
                continue

            media_info = _extract_media_info(msg)
            if not media_info:
                stats["skipped"] += 1
                continue

            # تحقق: هل هذه الحلقة موجودة في قاعدة البيانات؟
            existing_ep = db.get_episode(series_name, ep_num)
            if existing_ep and existing_ep.get("status") == "uploaded":
                stats["skipped"] += 1
                continue

            if dry_run:
                print(f"   [DRY] {series_name} — الحلقة {ep_num} "
                      f"| {media_info['file_size']/1048576:.1f}MB "
                      f"| {media_info['duration']}s")
                stats["imported"] += 1
                stats["series"].add(series_name)
                continue

            # احفظ في قاعدة البيانات
            try:
                # تأكد من وجود المسلسل
                series = db.get_series(series_name)
                if not series.get("url"):
                    db.set_series(series_name, {
                        "url": f"https://t.me/{channel}",
                        "slug": series_name.replace(" ", "_"),
                        "source": f"telegram:@{channel}",
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                        "status": "ok",
                    })

                # حفظ الحلقة
                db.set_episode(
                    series_name, ep_num,
                    status="uploaded",
                    message_id=msg.id,
                    file_id=media_info["file_id"],
                    size=media_info["file_size"],
                    width=media_info["width"],
                    height=media_info["height"],
                    duration=media_info["duration"],
                    url=f"https://t.me/{channel}/{msg.id}",
                    title=caption.split("\n")[0][:200],
                    source=f"telegram:@{channel}",
                    imported_at=datetime.now(timezone.utc).isoformat(),
                )

                # أضف للفهرس
                db.add_video({
                    "id": msg.id,
                    "title": f"{series_name} — الحلقة {ep_num}",
                    "series": series_name,
                    "episode": ep_num,
                    "file_id": media_info["file_id"],
                    "size": media_info["file_size"],
                    "width": media_info["width"],
                    "height": media_info["height"],
                    "duration": media_info["duration"],
                    "date": msg.date.isoformat() if msg.date else
                            datetime.now(timezone.utc).isoformat(),
                    "source": f"telegram:@{channel}",
                })

                stats["imported"] += 1
                stats["series"].add(series_name)

                # اطبع كل 20 استيراد
                if stats["imported"] % 20 == 0:
                    print(f"   ✅ استُورد {stats['imported']} حتى الآن "
                          f"({len(stats['series'])} مسلسل)")

            except Exception as e:
                stats["errors"] += 1
                print(f"   ⚠️ خطأ في حفظ {series_name} #{ep_num}: {str(e)[:100]}")

    except FloodWait as e:
        print(f"\n   ⏳ FloodWait {e.value}s...")
        await asyncio.sleep(e.value)
    except (ChannelPrivate, UsernameNotOccupied) as e:
        print(f"\n   ❌ القناة @{channel} غير متاحة: {str(e)[:100]}")
    except Exception as e:
        print(f"\n   ❌ خطأ في فحص @{channel}: {str(e)[:200]}")

    return stats


# ═══════════════════════════════════════════════════════════════
# الدالة الرئيسية
# ═══════════════════════════════════════════════════════════════
async def main():
    parser = argparse.ArgumentParser(description="Import old series from Telegram channels")
    parser.add_argument(
        "--channels",
        default=config.CHECK_CHANNELS or "shoofcima,shoofFilm",
        help="القنوات مفصولة بفاصلة",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("IMPORT_LIMIT", "5000")),
        help="حد الرسائل لكل قناة",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="اعرض ما سيُستورد فقط",
    )
    args = parser.parse_args()

    print("""
╔══════════════════════════════════════════════════════╗
║   استيراد المسلسلات القديمة من Telegram             ║
╚══════════════════════════════════════════════════════╝
""")

    channels = [c.strip().replace("@", "") for c in args.channels.split(",") if c.strip()]
    if not channels:
        print("❌ لا توجد قنوات")
        return 1

    print(f"📡 القنوات: {', '.join(channels)}")
    print(f"📊 الحد: {args.limit} رسالة لكل قناة")
    print(f"🧪 Dry-run: {'نعم' if args.dry_run else 'لا'}")
    print()

    config.validate()

    # افتح Pyrogram
    print("🔐 الاتصال بـ Telegram...")
    app = Client(
        "shoof_importer",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=config.SESSION_STRING,
        in_memory=True,
        no_updates=True,
    )
    await app.start()
    me = await app.get_me()
    print(f"✅ متصل كـ: {me.first_name or me.id}\n")

    all_stats = []
    start = time.time()

    try:
        for channel in channels:
            s = await import_channel(app, channel, limit=args.limit, dry_run=args.dry_run)
            all_stats.append(s)
    finally:
        await app.stop()

    # ملخص
    elapsed = time.time() - start
    print(f"\n{'═' * 60}")
    print(f"📊 الملخص (في {elapsed:.0f}s)")
    print(f"{'═' * 60}")

    total_imported = 0
    total_scanned = 0
    all_series = set()

    for s in all_stats:
        print(f"\n@{s['channel']}:")
        print(f"   فُحص: {s['scanned']}")
        print(f"   استُورد: {s['imported']}")
        print(f"   تخطّي: {s['skipped']}")
        print(f"   أخطاء: {s['errors']}")
        print(f"   مسلسلات: {len(s['series'])}")
        total_imported += s["imported"]
        total_scanned += s["scanned"]
        all_series.update(s["series"])

    print(f"\n{'─' * 60}")
    print(f"🎯 الإجمالي:")
    print(f"   فُحص: {total_scanned}")
    print(f"   استُورد: {total_imported}")
    print(f"   مسلسلات فريدة: {len(all_series)}")
    print(f"{'═' * 60}")

    # ★ إذا لم يكن dry-run، ابنِ الموقع
    if not args.dry_run and total_imported > 0:
        print(f"\n🏗️  بناء الموقع...")
        try:
            from build_site import build_site
            build_site()
            print(f"✅ تم بناء الموقع بنجاح")

            # اطبع إحصاءات الموقع
            s = db.stats()
            print(f"\n📊 إحصاءات قاعدة البيانات:")
            print(f"   المسلسلات: {s['series']}")
            print(f"   الحلقات: {s['uploaded']} مرفوعة من {s['episodes']}")
            print(f"   الفيديوهات: {s['videos']}")

        except Exception as e:
            print(f"❌ فشل البناء: {e}")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))