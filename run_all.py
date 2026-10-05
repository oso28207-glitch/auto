#!/usr/bin/env python3
"""
run_all.py — المنسق الموحد مع دعم مصادر متعددة

★ يضمن التشغيل المتناغم:
  1. فحص القنوات (اختياري).
  2. جلب الصور.
  3. تحميل + رفع من جميع المصادر.
  4. بناء الموقع مع تصنيفات.
  5. push فوري.
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from config import config, DATA_DIR, MEDIA_DIR


LOCK_FILE = DATA_DIR / ".run.lock"
LOCK_TTL = 3600 * 3


def acquire_lock():
    if LOCK_FILE.exists():
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age < LOCK_TTL:
                print(f"⚠️ سكربت آخر يعمل (منذ {age/60:.1f}د)")
                return False
        except Exception:
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    try:
        LOCK_FILE.unlink()
    except Exception:
        pass


def git_sync():
    try:
        subprocess.run(["git", "pull", "--rebase", "--autostash"],
                       capture_output=True, timeout=60)
    except Exception:
        pass


async def main():
    if not acquire_lock():
        return 1

    try:
        git_sync()
        start = time.time()

        print("""
╔══════════════════════════════════════════════════════╗
║           شوف — Shoof Automation                     ║
║   مصادر متعددة • تصنيفات • ضغط ذكي • 45MB           ║
╚══════════════════════════════════════════════════════╝
""")

        # ★ تحميل المصادر المفعّلة
        from sources.u3seq import U3SeqSource
        sources = []
        enabled = [s.strip() for s in config.ENABLED_SOURCES.split(",") if s.strip()]
        if "u3seq" in enabled:
            sources.append(U3SeqSource())
        print(f"📡 المصادر: {', '.join(s.name for s in sources)}")

        # جلب العناصر من كل المصادر
        all_items = []
        for src in sources:
            try:
                items = await src.fetch_items()
                all_items.extend(items)
                src.cleanup()
            except Exception as e:
                print(f"⚠️ فشل {src.name}: {str(e)[:150]}")

        print(f"\n📊 إجمالي: {len(all_items)} عنصر")

        # تصنيف
        series_count = sum(1 for x in all_items if x.type == "series")
        movie_count = sum(1 for x in all_items if x.type == "movie")
        print(f"   • مسلسلات: {series_count}")
        print(f"   • أفلام: {movie_count}")

        # ★ تحميل ورفع
        from downloader import download_episode
        from uploader import uploader
        from database import db

        await uploader.start()
        uploaded = 0

        try:
            for item in all_items:
                elapsed = time.time() - start
                if elapsed >= config.MAX_RUNTIME_SECONDS:
                    print(f"\n⏰ انتهى الوقت ({elapsed/60:.1f}د)")
                    break

                # فحص الموجود في القنوات
                from telegram_checker import fetch_existing_episodes, is_episode_uploaded
                existing = await fetch_existing_episodes()

                # لكل جزء
                new_parts = []
                for part in item.parts:
                    pnum = part.get("number", 0)
                    if db.episode_status(item.name, pnum) == "uploaded":
                        continue
                    if existing and is_episode_uploaded(existing, item.name, pnum):
                        continue
                    new_parts.append(part)

                if not new_parts:
                    continue

                print(f"\n{'═' * 60}")
                print(f"📺 {item.type.upper()}: {item.name}")
                print(f"   أجزاء جديدة: {len(new_parts)} من {item.total_parts}")
                print(f"   ⏱️  مضى: {elapsed/60:.1f}د")
                print(f"{'═' * 60}")

                for part in new_parts:
                    pnum = part.get("number", 0)
                    purl = part.get("url", "")
                    if not purl:
                        continue

                    print(f"\n   ── {'الجزء' if item.type == 'movie' else 'الحلقة'} {pnum} ──")

                    # تحميل
                    file_path = await asyncio.to_thread(
                        download_episode,
                        item.name, pnum, purl,
                        item.type, item.name,
                    )
                    if not file_path:
                        db.mark_failed(item.name, pnum, "فشل التحميل")
                        continue

                    # رفع مع عنوان منسق
                    try:
                        info = await uploader.upload(
                            file_path=file_path,
                            item_name=item.name_ar or item.name,
                            media_type=item.type,
                            part_number=pnum,
                            season=item.season,
                        )
                    except Exception as e:
                        print(f"   ❌ فشل رفع: {str(e)[:150]}")
                        db.mark_failed(item.name, pnum, "فشل الرفع")
                        try: file_path.unlink()
                        except Exception: pass
                        continue

                    # حفظ
                    db.set_episode(
                        item.name, pnum,
                        status="uploaded",
                        message_id=info["message_id"],
                        file_id=info["file_id"],
                        size=info["size"],
                        width=info["width"], height=info["height"],
                        duration=info["duration"],
                        url=purl,
                        type=item.type,
                        source=item.source,
                        caption=info["caption"],
                    )
                    db.add_video({
                        "id": info["message_id"],
                        "title": info["caption"],
                        "series": item.name,
                        "episode": pnum,
                        "type": item.type,
                        "source": item.source,
                        "file_id": info["file_id"],
                        "size": info["size"],
                        "duration": info["duration"],
                        "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    })
                    db.set_series(item.name, {
                        "url": item.url,
                        "type": item.type,
                        "source": item.source,
                        "season": item.season,
                        "total_parts": item.total_parts,
                        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "status": "ok",
                    })
                    uploaded += 1

                    # بناء + push فوري
                    try:
                        from builder import build_incremental
                        build_incremental(changed_series=[item.name])
                        if config.REALTIME_PUSH:
                            subprocess.run(
                                ["git", "add", "-f", "docs/", "data/"],
                                capture_output=True, timeout=30,
                            )
                            subprocess.run(
                                ["git", "commit", "-m",
                                 f"🎬 {info['caption']}"],
                                capture_output=True, timeout=30,
                            )
                            subprocess.run(["git", "push"],
                                           capture_output=True, timeout=90)
                    except Exception:
                        pass

                    try: file_path.unlink()
                    except Exception: pass
        finally:
            await uploader.stop()

        elapsed = time.time() - start
        print(f"\n{'═' * 60}")
        print(f"⏱️  المدة: {elapsed/60:.1f} دقيقة")
        print(f"🎬 رُفعت: {uploaded} حلقة/فيلم")
        print(f"{'═' * 60}")

        # بناء نهائي
        try:
            from builder import build_incremental
            build_incremental()
            subprocess.run(["git", "add", "-f", "docs/", "data/"],
                           capture_output=True, timeout=30)
            subprocess.run(["git", "commit", "-m", "🤖 تحديث نهائي"],
                           capture_output=True, timeout=30)
            subprocess.run(["git", "push"], capture_output=True, timeout=90)
        except Exception:
            pass

        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))