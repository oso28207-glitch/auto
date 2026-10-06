#!/usr/bin/env python3
"""
run_all.py — المنسق الموحد مع مصادر متعددة ومنع التكرار
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from config import config, DATA_DIR, MEDIA_DIR


# ═══ القفل الذكي ═══
LOCK_FILE = DATA_DIR / ".run.lock"
LOCK_TTL = 3600 * 3


def _is_github_actions():
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _is_process_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_lock():
    if _is_github_actions():
        print("ℹ️  GitHub Actions — تخطي القفل")
        return True

    if LOCK_FILE.exists():
        try:
            content = LOCK_FILE.read_text().strip()
            parts = content.split("|")
            old_pid = int(parts[0]) if parts else 0
            old_ts = float(parts[1]) if len(parts) > 1 else 0
            age = time.time() - old_ts

            if age > LOCK_TTL:
                print(f"⚠️ قفل قديم — حذفه")
                LOCK_FILE.unlink()
            else:
                if _is_process_alive(old_pid) and old_pid != os.getpid():
                    print(f"⚠️ سكربت آخر يعمل (PID {old_pid})")
                    return False
                LOCK_FILE.unlink()
        except Exception:
            try: LOCK_FILE.unlink()
            except Exception: pass

    try:
        LOCK_FILE.write_text(f"{os.getpid()}|{time.time()}")
    except Exception:
        pass
    return True


def release_lock():
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


def git_sync():
    if not _is_github_actions():
        return
    try:
        subprocess.run(["git", "pull", "--rebase", "--autostash"],
                       capture_output=True, timeout=60)
    except Exception:
        pass


def git_push_docs(force=False):
    if not _is_github_actions():
        return False
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"],
                       capture_output=True, timeout=10)
        subprocess.run(["git", "config", "user.email",
                        "github-actions[bot]@users.noreply.github.com"],
                       capture_output=True, timeout=10)
        subprocess.run(["git", "add", "-f", "docs/", "data/"],
                       capture_output=True, timeout=30)

        r = subprocess.run(["git", "diff", "--staged", "--quiet"],
                           capture_output=True, timeout=10)
        if r.returncode == 0:
            return False

        ts = time.strftime("%Y-%m-%d %H:%M")
        subprocess.run(["git", "commit", "-m", f"🎬 تحديث تلقائي: {ts}"],
                       capture_output=True, timeout=30)

        r = subprocess.run(["git", "push"], capture_output=True, timeout=90)
        if r.returncode == 0:
            print(f"   🚀 تم الدفع")
            return True

        subprocess.run(["git", "pull", "--rebase", "--autostash"],
                       capture_output=True, timeout=90)
        r2 = subprocess.run(["git", "push"], capture_output=True, timeout=90)
        return r2.returncode == 0
    except Exception as e:
        print(f"   ⚠️ فشل الدفع: {str(e)[:150]}")
        return False


# ═══════════════════════════════════════════════════════════════
async def main():
    if not acquire_lock():
        print("⏭️ الخروج بسبب القفل")
        return 0

    try:
        git_sync()
        start = time.time()

        print("""
╔══════════════════════════════════════════════════════╗
║           شوف — Shoof Automation                     ║
║   مصادر متعددة • تصنيفات • ضغط ذكي • 45MB           ║
╚══════════════════════════════════════════════════════╝
""")

        config.validate()
        print("✅ الإعدادات صحيحة")

        from database import db
        s = db.stats()
        print(f"\n📊 الحالة الحالية:")
        print(f"   المسلسلات: {s['series']}")
        print(f"   الحلقات المرفوعة: {s['uploaded']} من {s['episodes']}")
        print(f"   الفيديوهات: {s['videos']}")
        print(f"   فاشلة: {s.get('failed_series', 0)}")

        # ★★★ تحميل المصادر المفعّلة
        sources = []
        enabled = [x.strip() for x in config.ENABLED_SOURCES.split(",") if x.strip()]

        if "u3seq" in enabled:
            from sources.u3seq import U3SeqSource
            sources.append(U3SeqSource())
        if "yam" in enabled:
            from sources.yam_ahwak import YamAhwakSource
            sources.append(YamAhwakSource())
        if "egybest" in enabled:
            from sources.egybest import EgyBestSource
            sources.append(EgyBestSource())

        print(f"\n📡 المصادر: {', '.join(s.name for s in sources)}")

        # ★★★ جلب العناصر من كل المصادر
        all_items = []
        for src in sources:
            try:
                items = await src.fetch_items()
                all_items.extend(items)
            except Exception as e:
                print(f"⚠️ فشل {src.name}: {str(e)[:150]}")
            finally:
                try: src.cleanup()
                except Exception: pass

        print(f"\n📊 إجمالي: {len(all_items)} عنصر")
        series_count = sum(1 for x in all_items if x.type == "series")
        movie_count = sum(1 for x in all_items if x.type == "movie")
        print(f"   • مسلسلات: {series_count}")
        print(f"   • أفلام: {movie_count}")

        if not all_items:
            print("\n✅ لا عناصر — الخروج")
            return 0

        # ★★★ فحص القنوات
        existing_eps = {}
        try:
            from telegram_checker import (
                fetch_existing_episodes, is_episode_uploaded, get_stats
            )
            existing_eps = await fetch_existing_episodes()
            ch_stats = get_stats(existing_eps)
            print(f"\n📡 Telegram: {ch_stats['episodes']} حلقة ({ch_stats['series']} عمل)")
        except Exception as e:
            print(f"\n⚠️ فشل فحص Telegram: {str(e)[:150]}")

        # ★★★ تحميل ورفع
        from downloader import download_episode
        from uploader import uploader
        from builder import build_incremental
        from telegram_checker import is_episode_uploaded

        await uploader.start()
        uploaded_count = 0
        skipped_count = 0
        failed_count = 0

        try:
            for item in all_items:
                elapsed = time.time() - start
                if elapsed >= config.MAX_RUNTIME_SECONDS:
                    print(f"\n⏰ انتهى الوقت ({elapsed/60:.1f}د)")
                    break

                if (config.MAX_EPISODES_PER_RUN > 0
                        and uploaded_count >= config.MAX_EPISODES_PER_RUN):
                    break

                # ★★★ منع التكرار: ابحث عن اسم مشابه في قاعدة البيانات
                existing_name = db.find_series_by_name(item.name)
                if existing_name:
                    # استخدم الاسم الموجود
                    if existing_name != item.name:
                        print(f"   🔄 دمج: '{item.name}' → '{existing_name}'")
                    target_name = existing_name
                else:
                    target_name = item.name

                # ★★★ الفحص الثلاثي
                new_parts = []
                for part in item.parts:
                    pnum = part.get("number", 0)
                    purl = part.get("url", "")

                    # فحص 1: state.json
                    status = db.episode_status(target_name, pnum)
                    if status == "uploaded":
                        skipped_count += 1
                        continue

                    # فحص 2: Telegram
                    if existing_eps and is_episode_uploaded(existing_eps, target_name, pnum):
                        db.set_episode(target_name, pnum,
                                       status="uploaded",
                                       source="telegram_existing")
                        skipped_count += 1
                        continue

                    # فحص 3: الرابط
                    if not purl or not purl.startswith("http"):
                        continue

                    new_parts.append(part)

                if not new_parts:
                    continue

                print(f"\n{'═' * 60}")
                print(f"📺 {item.type.upper()}: {item.name}")
                print(f"   أجزاء جديدة: {len(new_parts)} من {item.total_parts}")
                print(f"   ⏱️  مضى: {elapsed/60:.1f}د | "
                      f"متبقٍ: {(config.MAX_RUNTIME_SECONDS - elapsed)/60:.1f}د")
                print(f"{'═' * 60}")

                first_part = new_parts[0].get("number", 1)

                for part in new_parts:
                    if time.time() - start >= config.MAX_RUNTIME_SECONDS:
                        break

                    pnum = part.get("number", 0)
                    purl = part.get("url", "")
                    part_label = "الجزء" if item.type == "movie" else "الحلقة"
                    print(f"\n   ── {part_label} {pnum} ──")

                    try:
                        file_path = await asyncio.to_thread(
                            download_episode,
                            target_name, pnum, purl,
                            item.type, target_name,
                        )
                    except Exception as e:
                        print(f"   ❌ خطأ تحميل: {str(e)[:150]}")
                        file_path = None

                    if not file_path:
                        db.mark_failed(target_name, pnum, "فشل التحميل")
                        failed_count += 1
                        if pnum == first_part:
                            print(f"   🚫 تخطي: {target_name}")
                            db.set_series(target_name, {
                                "status": "failed",
                                "reason": "فشلت أول حلقة",
                            })
                            break
                        continue

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
                        db.mark_failed(target_name, pnum, "فشل الرفع")
                        failed_count += 1
                        try: file_path.unlink()
                        except Exception: pass
                        continue

                    db.set_episode(
                        target_name, pnum,
                        status="uploaded",
                        message_id=info["message_id"],
                        file_id=info["file_id"],
                        size=info["size"],
                        width=info["width"],
                        height=info["height"],
                        duration=info["duration"],
                        url=purl,
                        type=item.type,
                        source=item.source,
                        caption=info["caption"],
                    )
                    db.add_video({
                        "id": info["message_id"],
                        "title": info["caption"],
                        "series": target_name,
                        "episode": pnum,
                        "type": item.type,
                        "source": item.source,
                        "file_id": info["file_id"],
                        "size": info["size"],
                        "duration": info["duration"],
                        "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    })
                    db.set_series(target_name, {
                        "url": item.url,
                        "type": item.type,
                        "source": item.source,
                        "season": item.season,
                        "total_parts": item.total_parts,
                        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "status": "ok",
                    })
                    uploaded_count += 1
                    print(f"   ✅ «{info['caption']}» ({uploaded_count})")

                    if config.AUTO_BUILD:
                        try:
                            build_incremental(changed_series=[target_name])
                            if config.REALTIME_PUSH:
                                git_push_docs()
                        except Exception as e:
                            print(f"   ⚠️ {str(e)[:100]}")

                    if not config.KEEP_MEDIA:
                        try: file_path.unlink()
                        except Exception: pass

        finally:
            await uploader.stop()

        elapsed = time.time() - start
        print(f"\n{'═' * 60}")
        print(f"⏱️  المدة: {elapsed/60:.1f} دقيقة")
        print(f"✅ رُفعت: {uploaded_count}")
        print(f"⏭️  تُجوهلت: {skipped_count}")
        print(f"❌ فشلت: {failed_count}")
        print(f"{'═' * 60}")

        if config.AUTO_BUILD:
            try:
                build_incremental()
                git_push_docs(force=True)
            except Exception:
                pass

        return 0
    except Exception as e:
        import traceback
        print(f"\n💥 خطأ: {e}")
        traceback.print_exc()
        try:
            from builder import build_incremental
            build_incremental()
            git_push_docs(force=True)
        except Exception:
            pass
        return 1
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))