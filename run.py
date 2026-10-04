#!/usr/bin/env python3
"""
run.py — 🚀 نقطة التشغيل الوحيدة لنظام شوف
يشتغل بضغطة واحدة: فحص → تحميل → ضغط → رفع → بناء تدريجي.
★ عند فشل مسلسل كامل: يتخطاه وينتقل للتالي.
"""

import asyncio
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


class Pipeline:
    def __init__(self):
        self.start = time.time()
        self.stats = {
            "series_checked": 0,
            "series_with_new": 0,
            "series_failed": 0,
            "episodes_new": 0,
            "episodes_uploaded": 0,
            "episodes_failed": 0,
            "changed": [],
            "failed_series": [],
        }

    async def run(self):
        from checker import fetch_series_list, find_new_episodes
        from downloader import download_episode
        from uploader import uploader
        from builder import build_incremental

        print(BANNER)
        config.validate()
        print("✅ الإعدادات صحيحة")

        print(f"\n📊 الحالة الحالية:")
        s = db.stats()
        print(f"   المسلسلات: {s['series']}")
        print(f"   الحلقات المرفوعة: {s['uploaded']} من {s['episodes']}")
        print(f"   الفيديوهات في الفهرس: {s['videos']}")
        print(f"   مسلسلات فاشلة: {s.get('failed_series', 0)}")

        # ═══ 1) جلب قائمة المسلسلات ═══
        print()
        series_list = fetch_series_list()
        self.stats["series_checked"] = len(series_list)

        # ═══ 2) فلترة المسلسلات التي فيها جديد ═══
        print("🔍 فحص الحلقات الجديدة...")
        work_queue = []
        for s in series_list:
            name = s["name"]

            # ★ تجاهل المسلسلات الفاشلة
            if db.has_failed(name):
                print(f"   ⏭️  تخطي (فشل سابق): {name}")
                continue

            new_eps = find_new_episodes(s, name)
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
            return

        total_new = sum(len(w["episodes"]) for w in work_queue)
        self.stats["episodes_new"] = total_new
        print(f"\n🎯 {len(work_queue)} مسلسل فيها {total_new} حلقة جديدة")

        # ═══ 3) تشغيل الرفع ═══
        await uploader.start()

        try:
            # ═══ 4) معالجة المسلسلات بالترتيب ═══
            for series_info in work_queue:
                name = series_info["name"]
                eps = series_info["episodes"]
                eps.sort(key=lambda x: x["episode"])

                print(f"\n{'═' * 60}")
                print(f"📺 {name} ({len(eps)} حلقة)")
                print(f"{'═' * 60}")

                series_failed = False
                first_ep = eps[0]["episode"] if eps else 0

                for ep_data in eps:
                    # حد أقصى؟
                    if (config.MAX_EPISODES_PER_RUN > 0 and
                            self.stats["episodes_uploaded"] >= config.MAX_EPISODES_PER_RUN):
                        print(f"\n⏸️  وصلنا للحد الأقصى ({config.MAX_EPISODES_PER_RUN})")
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

                    # ★ التحميل — يرجع None عند الفشل
                    try:
                        video_path = await asyncio.to_thread(download_episode, name, ep_num, ep_url)
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

                        # ★ إذا فشلت أول حلقة، نتجاهل المسلسل كاملاً
                        if ep_num == first_ep:
                            print(f"\n   🚫 تخطي المسلسل: {name} (فشلت أول حلقة)")
                            series_failed = True
                            break

                        continue  # جرّب الحلقة التالية

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
                        try:
                            video_path.unlink()
                        except Exception:
                            pass
                        continue

                    # حفظ
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
                        try:
                            video_path.unlink()
                        except Exception:
                            pass

                    if config.AUTO_BUILD:
                        build_incremental(changed_series=[name])

                    print(f"   ✅ الحلقة {ep_num} اكتملت")

                if series_failed:
                    self.stats["series_failed"] += 1
                    self.stats["failed_series"].append(name)
                    db.set_series(name, {
                        "status": "failed",
                        "failed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "reason": "فشلت كل السيرفرات",
                    })
                    self.stats["changed"].append(name)
                    continue

                if name not in self.stats["changed"]:
                    self.stats["changed"].append(name)

                db.set_series(name, {
                    "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "status": "ok",
                })

            # ★ طباعة المسلسلات المتخطاة
            if self.stats["failed_series"]:
                print(f"\n⚠️  مسلسلات متخطاة ({len(self.stats['failed_series'])}):")
                for s in self.stats["failed_series"]:
                    print(f"   · {s}")
                print("\n   💡 لإعادة المحاولة، احذف state.json أو استخدم db.reset_all_failed()")

        finally:
            await uploader.stop()

        # ═══ 5) بناء نهائي ═══
        if config.AUTO_BUILD:
            build_incremental(changed_series=self.stats["changed"])


def print_summary(stats, start):
    elapsed = time.time() - start
    print(f"\n{'═' * 60}")
    print(f"⏱️  المدة: {elapsed/60:.1f} دقيقة")
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
        print_summary(pipeline.stats, pipeline.start)
        print("\n✅ اكتملت الدورة بنجاح")
        return 0

    except SooFatalError as e:
        print(f"\n{'🛑' * 30}")
        print(f"❌ خطأ خطير — المرحلة: {e.stage}")
        print(f"   {e.message}")
        if e.original:
            print(f"   السبب: {e.original}")
        print(f"{'🛑' * 30}")
        print_summary(pipeline.stats, pipeline.start)
        return 1

    except KeyboardInterrupt:
        print("\n\n⏹️  أُوقف بواسطة المستخدم")
        return 130

    except Exception as e:
        import traceback
        print(f"\n💥 خطأ غير متوقع: {e}")
        traceback.print_exc()
        print_summary(pipeline.stats, pipeline.start)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))