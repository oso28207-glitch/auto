#!/usr/bin/env python3
"""
run.py — نقطة التشغيل الوحيدة لنظام شوف

★ الميزات:
  1. فحص قنوات Telegram (shoofcima, shoofFilm) قبل التحميل.
  2. إيقاف تلقائي بعد MAX_RUNTIME_SECONDS (2س 45د).
  3. تحديث فوري للموقع: push بعد كل حلقة.
  4. إحصائيات شاملة.
  5. تخطي المسلسلات الفاشلة.
"""

import asyncio
import os
import subprocess
import sys
import time

from config import config
from database import db
from errors import SooFatalError


BANNER = """
╔══════════════════════════════════════════════════════╗
║           شوف — Shoof Automation                     ║
║  فحص • تحميل • ضغط • رفع • بناء تدريجي              ║
╚══════════════════════════════════════════════════════╝
"""


# ═══════════════════════════════════════════════════════════════
# Git push للتحديث الفوري
# ═══════════════════════════════════════════════════════════════
_last_push_time = [0.0]


def _git_push_docs(min_interval=None, force=False):
    """يدفع docs/ و data/state.json إلى GitHub لتفعيل تحديث الموقع فوراً."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return False

    if not config.REALTIME_PUSH and not force:
        return False

    interval = min_interval if min_interval is not None else config.REALTIME_PUSH_INTERVAL
    now = time.time()
    if not force and (now - _last_push_time[0]) < interval:
        return False

    try:
        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.email",
             "github-actions[bot]@users.noreply.github.com"],
            capture_output=True, timeout=10,
        )

        subprocess.run(
            ["git", "add", "docs/", "data/state.json", "data/state.backup.json"],
            capture_output=True, timeout=30,
        )

        r = subprocess.run(
            ["git", "diff", "--staged", "--quiet"],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0:
            return False

        ts = time.strftime("%Y-%m-%d %H:%M")
        subprocess.run(
            ["git", "commit", "-m", f"🎬 تحديث تلقائي: {ts}"],
            capture_output=True, timeout=30,
        )

        r = subprocess.run(
            ["git", "push"], capture_output=True, text=True, timeout=90,
        )
        if r.returncode == 0:
            _last_push_time[0] = time.time()
            print(f"   🚀 تحديث فوري: تم دفع docs/ إلى GitHub")
            return True

        print(f"   ⚠️ فشل الدفع، محاولة pull --rebase...")
        subprocess.run(
            ["git", "pull", "--rebase", "--autostash"],
            capture_output=True, timeout=90,
        )
        r2 = subprocess.run(
            ["git", "push"], capture_output=True, text=True, timeout=90,
        )
        if r2.returncode == 0:
            _last_push_time[0] = time.time()
            print(f"   🚀 تحديث فوري بعد rebase")
            return True

        print(f"   ⚠️ فشل الدفع النهائي: {r2.stderr[:150]}")
        return False

    except subprocess.TimeoutExpired:
        print("   ⚠️ timeout في git push")
        return False
    except Exception as e:
        print(f"   ⚠️ خطأ في git push: {str(e)[:150]}")
        return False


# ═══════════════════════════════════════════════════════════════
# Pipeline
# ═══════════════════════════════════════════════════════════════
class Pipeline:
    def __init__(self):
        self.start = time.time()
        self.time_limit_reached = False
        self.stats = {
            "series_checked": 0,
            "series_with_new": 0,
            "series_failed": 0,
            "episodes_new": 0,
            "episodes_uploaded": 0,
            "episodes_failed": 0,
            "episodes_skipped_by_channels": 0,
            "channels_existing": 0,
            "changed": [],
            "failed_series": [],
        }

    def elapsed(self):
        return time.time() - self.start

    def remaining(self):
        return max(0, config.MAX_RUNTIME_SECONDS - self.elapsed())

    def time_exceeded(self):
        return self.elapsed() >= config.MAX_RUNTIME_SECONDS

    async def run(self):
        from checker import fetch_series_list, find_new_episodes
        from downloader import download_episode
        from uploader import uploader
        from builder import build_incremental
        from telegram_checker import (
            fetch_existing_episodes,
            is_episode_uploaded,
            get_stats,
        )

        print(BANNER)
        config.validate()
        print("✅ الإعدادات صحيحة")

        print(f"\n⏱️  الحد الزمني: {config.MAX_RUNTIME_SECONDS/60:.1f} دقيقة")
        print(f"🔄 تحديث فوري: {'مفعّل' if config.REALTIME_PUSH else 'معطّل'}")
        print(f"📡 فحص القنوات: {config.CHECK_CHANNELS or 'معطّل'}")

        print(f"\n📊 الحالة الحالية:")
        s = db.stats()
        print(f"   المسلسلات: {s['series']}")
        print(f"   الحلقات المرفوعة: {s['uploaded']} من {s['episodes']}")
        print(f"   الفيديوهات في الفهرس: {s['videos']}")
        print(f"   مسلسلات فاشلة: {s.get('failed_series', 0)}")

        # ★★★ فحص قنوات Telegram قبل التحميل
        existing_episodes = {}
        try:
            existing_episodes = await fetch_existing_episodes()
            ch_stats = get_stats(existing_episodes)
            self.stats["channels_existing"] = ch_stats["episodes"]
            print(f"\n📡 حلقات موجودة في القنوات: "
                  f"{ch_stats['episodes']} حلقة، {ch_stats['series']} مسلسل")
        except Exception as e:
            print(f"\n⚠️ فشل فحص القنوات: {str(e)[:200]}")

        # ═══ 1) جلب قائمة المسلسلات ═══
        print()
        series_list = fetch_series_list()
        self.stats["series_checked"] = len(series_list)

        # ═══ 2) فلترة ═══
        print("🔍 فحص الحلقات الجديدة...")
        work_queue = []
        for s in series_list:
            name = s["name"]

            if db.has_failed(name):
                print(f"   ⏭️  تخطي (فشل سابق): {name}")
                continue

            new_eps = find_new_episodes(s, name)
            if not new_eps:
                continue

            # ★ استبعاد الحلقات الموجودة في القنوات
            if existing_episodes:
                before = len(new_eps)
                new_eps = [
                    ep for ep in new_eps
                    if not is_episode_uploaded(
                        existing_episodes, name, ep["episode"]
                    )
                ]
                skipped = before - len(new_eps)
                if skipped > 0:
                    self.stats["episodes_skipped_by_channels"] += skipped
                    print(f"   🔍 {name}: تجاهل {skipped} حلقة موجودة في القنوات")

            if not new_eps:
                continue

            existing = db.get_series(name)
            is_new = not existing.get("url")

            if is_new:
                print(f"   📺 جديد: {name} ({len(new_eps)} حلقة)")
            else:
                print(f"   🔄 تحديث: {name} ({len(new_eps)} حلقة)")

            db.set_series(name, {
                "url": s["url"],
                "slug": s.get("slug", name.replace(" ", "_")),
                "last_checked": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })

            work_queue.append({
                "name": name,
                "url": s["url"],
                "episodes": new_eps,
            })
            self.stats["series_with_new"] += 1

        if not work_queue:
            print("\n✅ لا توجد حلقات جديدة — لا شيء للتحميل")
            if config.AUTO_BUILD:
                build_incremental()
                _git_push_docs(force=True)
            return

        total_new = sum(len(w["episodes"]) for w in work_queue)
        self.stats["episodes_new"] = total_new
        print(f"\n🎯 {len(work_queue)} مسلسل فيها {total_new} حلقة جديدة")

        # ═══ 3) تشغيل الرفع ═══
        await uploader.start()

        try:
            # ═══ 4) معالجة المسلسلات ═══
            for series_info in work_queue:
                if self.time_exceeded():
                    print(f"\n⏰ وصلنا للحد الزمني — إيقاف نظيف")
                    self.time_limit_reached = True
                    break

                name = series_info["name"]
                eps = series_info["episodes"]
                eps.sort(key=lambda x: x["episode"])

                print(f"\n{'═' * 60}")
                print(f"📺 {name} ({len(eps)} حلقة)")
                print(f"⏱️  مُنقضٍ: {self.elapsed()/60:.1f}د | "
                      f"متبقٍ: {self.remaining()/60:.1f}د")
                print(f"{'═' * 60}")

                series_failed = False
                first_ep = eps[0]["episode"] if eps else 0

                for ep_data in eps:
                    # فحص الوقت قبل كل حلقة
                    if self.time_exceeded():
                        print(f"\n   ⏰ وصلنا للحد الزمني — إيقاف قبل الحلقة التالية")
                        self.time_limit_reached = True
                        break

                    if (config.MAX_EPISODES_PER_RUN > 0 and
                            self.stats["episodes_uploaded"] >= config.MAX_EPISODES_PER_RUN):
                        print(f"\n   ⏸️  وصلنا للحد الأقصى ({config.MAX_EPISODES_PER_RUN})")
                        break

                    ep_num = ep_data["episode"]
                    ep_url = ep_data["link"]

                    print(f"\n   ── الحلقة {ep_num} ──")

                    db.set_episode(
                        name, ep_num,
                        status="downloading",
                        url=ep_url,
                        title=ep_data.get("title", ""),
                    )

                    # التحميل
                    try:
                        video_path = await asyncio.to_thread(
                            download_episode, name, ep_num, ep_url
                        )
                    except SooFatalError as e:
                        print(f"   ❌ خطأ خطير: {e.message}")
                        video_path = None
                    except Exception as e:
                        print(f"   ❌ خطأ: {str(e)[:150]}")
                        video_path = None

                    if video_path is None:
                        print(f"   ⚠️ فشل تحميل الحلقة {ep_num}")
                        db.mark_failed(name, ep_num, "فشلت كل السيرفرات")
                        self.stats["episodes_failed"] += 1

                        if ep_num == first_ep:
                            print(f"\n   🚫 تخطي المسلسل: {name} (فشلت أول حلقة)")
                            series_failed = True
                            break
                        continue

                    # الرفع
                    db.set_episode(name, ep_num, status="uploading")

                    caption = f"📺 {name}\n🎬 الحلقة {ep_num}"
                    upload_info = None
                    try:
                        upload_info = await uploader.upload(video_path, caption)
                    except SooFatalError as e:
                        print(f"   ❌ فشل الرفع: {e.message}")
                    except Exception as e:
                        print(f"   ❌ فشل الرفع: {str(e)[:150]}")

                    if not upload_info:
                        db.mark_failed(name, ep_num, "فشل الرفع")
                        self.stats["episodes_failed"] += 1
                        try: video_path.unlink()
                        except Exception: pass
                        continue

                    # الحفظ
                    db.set_episode(
                        name, ep_num,
                        status="uploaded",
                        message_id=upload_info["message_id"],
                        file_id=upload_info["file_id"],
                        size=upload_info["size"],
                        width=upload_info.get("width", 0),
                        height=upload_info.get("height", 0),
                        duration=upload_info.get("duration", 0),
                    )

                    db.add_video({
                        "id": upload_info["message_id"],
                        "title": f"{name} — الحلقة {ep_num}",
                        "series": name,
                        "episode": ep_num,
                        "file_id": upload_info["file_id"],
                        "size": upload_info["size"],
                        "width": upload_info.get("width", 0),
                        "height": upload_info.get("height", 0),
                        "duration": upload_info.get("duration", 0),
                        "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    })

                    self.stats["episodes_uploaded"] += 1

                    if not config.KEEP_MEDIA:
                        try: video_path.unlink()
                        except Exception: pass

                    # بناء تدريجي + دفع فوري
                    if config.AUTO_BUILD:
                        build_incremental(changed_series=[name])
                        _git_push_docs()

                    print(f"   ✅ الحلقة {ep_num} اكتملت")

                if series_failed:
                    self.stats["series_failed"] += 1
                    self.stats["failed_series"].append(name)
                    db.set_series(name, {
                        "status": "failed",
                        "failed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "reason": "فشلت كل السيرفرات",
                    })
                    if name not in self.stats["changed"]:
                        self.stats["changed"].append(name)
                    continue

                if name not in self.stats["changed"]:
                    self.stats["changed"].append(name)

                db.set_series(name, {
                    "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "status": "ok",
                })

                if self.time_limit_reached:
                    break

            if self.stats["failed_series"]:
                print(f"\n⚠️  مسلسلات متخطاة ({len(self.stats['failed_series'])}):")
                for s in self.stats["failed_series"]:
                    print(f"   · {s}")

        finally:
            await uploader.stop()

        # ═══ 5) بناء ودفع نهائي ═══
        if config.AUTO_BUILD:
            build_incremental(changed_series=self.stats["changed"])
            _git_push_docs(force=True)


def print_summary(stats, start, time_limit_reached):
    elapsed = time.time() - start
    print(f"\n{'═' * 60}")
    print(f"⏱️  المدة: {elapsed/60:.1f} دقيقة")
    if time_limit_reached:
        print(f"⏰ تم الوصول للحد الزمني — سيعود في الدورة التالية")
    print(f"📡 حلقات موجودة في القنوات: {stats.get('channels_existing', 0)}")
    print(f"🔍 حلقات مُتجاوَزة (في القنوات): "
          f"{stats.get('episodes_skipped_by_channels', 0)}")
    print(f"📊 المسلسلات: {stats['series_checked']} مفحوصة، "
          f"{stats['series_with_new']} فيها جديد، "
          f"{stats['series_failed']} متخطاة")
    print(f"🎬 الحلقات: {stats['episodes_new']} جديدة، "
          f"{stats['episodes_uploaded']} مرفوعة، "
          f"{stats['episodes_failed']} فاشلة")
    if stats["changed"]:
        preview = ", ".join(stats["changed"][:5])
        if len(stats["changed"]) > 5:
            preview += "..."
        print(f"🔄 محدّث: {preview}")
    print("═" * 60)


async def main():
    pipeline = Pipeline()
    try:
        await pipeline.run()
        print_summary(pipeline.stats, pipeline.start, pipeline.time_limit_reached)
        print("\n✅ اكتملت الدورة")
        return 0

    except SooFatalError as e:
        print(f"\n{'🛑' * 30}")
        print(f"❌ خطأ خطير — المرحلة: {e.stage}")
        print(f"   {e.message}")
        if e.original:
            print(f"   السبب: {e.original}")
        print(f"{'🛑' * 30}")
        try:
            from builder import build_incremental
            if config.AUTO_BUILD:
                build_incremental()
            _git_push_docs(force=True)
        except Exception:
            pass
        print_summary(pipeline.stats, pipeline.start, pipeline.time_limit_reached)
        return 1

    except KeyboardInterrupt:
        print("\n\n⏹️  أُوقف بواسطة المستخدم")
        return 130

    except Exception as e:
        import traceback
        print(f"\n💥 خطأ غير متوقع: {e}")
        traceback.print_exc()
        try:
            from builder import build_incremental
            if config.AUTO_BUILD:
                build_incremental()
            _git_push_docs(force=True)
        except Exception:
            pass
        print_summary(pipeline.stats, pipeline.start, pipeline.time_limit_reached)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))