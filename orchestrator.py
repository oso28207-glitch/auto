"""
يربط كل شيء: فحص → تحميل → رفع → تتبع → بناء.
أي خطأ → يوقف كل شيء.
"""

import asyncio
import time
from pathlib import Path

from builder import build_incremental
from checker import fetch_series_list, fetch_episodes, find_new_episodes
from config import config
from database import db
from downloader import download_episode
from errors import SooFatalError
from uploader import uploader


class Orchestrator:
    def __init__(self):
        self.running = False

    async def run_once(self):
        """دورة واحدة كاملة."""
        print("\n" + "=" * 60)
        print("🚀 بدء دورة جديدة")
        print("=" * 60)

        await uploader.start()

        try:
            # 1) جلب قائمة المسلسلات من الموقع
            print("\n📋 جلب قائمة المسلسلات...")
            series_list = fetch_series_list()
            print(f"   وجدت {len(series_list)} مسلسل")

            # 2) لكل مسلسل: فحص الحلقات الجديدة
            for s in series_list:
                name = s["name"]
                url = s["url"]

                # تحديث بيانات المسلسل في قاعدة البيانات
                existing = db.get_series(name)
                if not existing.get("url"):
                    db.set_series(name, {"url": url, "episodes": {}})
                    print(f"\n📺 مسلسل جديد: {name}")

                # 3) جلب الحلقات الجديدة
                print(f"\n🔍 فحص: {name}")
                all_eps = fetch_episodes(url)
                new_eps = [
                    ep for ep in all_eps
                    if db.episode_status(name, ep["number"]) != "uploaded"
                ]

                if not new_eps:
                    print(f"   لا توجد حلقات جديدة")
                    continue

                print(f"   حلقات جديدة: {len(new_eps)}")

                # 4) تحميل ورفع كل حلقة جديدة (بالترتيب)
                for ep in new_eps:
                    num = ep["number"]
                    print(f"\n   ── الحلقة {num} ──")

                    # تحميل
                    db.set_episode(name, num, status="downloading")
                    file_path = download_episode(name, num, ep["url"])

                    # رفع
                    db.set_episode(name, num, status="uploading")
                    message_id = await uploader.upload_episode(
                        series=name,
                        episode=num,
                        file_path=file_path,
                    )

                    # حفظ في قاعدة البيانات
                    db.set_episode(
                        name, num,
                        status="uploaded",
                        message_id=message_id,
                        url=ep["url"],
                    )
                    db.add_video({
                        "id": message_id,
                        "title": f"{name} — الحلقة {num}",
                        "series": name,
                        "episode": num,
                        "size": Path(file_path).stat().st_size,
                        "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    })

                    # 5) بناء تدريجي بعد كل حلقة
                    if config.AUTO_BUILD:
                        build_incremental(series_name=name)

                print(f"\n✅ اكتمل: {name} ({len(new_eps)} حلقة)")

            # 6) بناء نهائي (videos.json)
            if config.AUTO_BUILD:
                build_incremental()

            print("\n" + "=" * 60)
            print("✅ اكتملت الدورة بنجاح")
            print("=" * 60)

        except SooFatalError as e:
            print(f"\n❌ خطأ خطير [{e.stage}]: {e.message}")
            print("🛑 إيقاف جميع العمليات...")
            raise
        except Exception as e:
            print(f"\n❌ خطأ غير متوقع: {e}")
            raise SooFatalError("GENERAL", str(e), e)
        finally:
            await uploader.stop()

    async def run_forever(self):
        """تشغيل مستمر مع فحص دوري."""
        self.running = True
        while self.running:
            try:
                await self.run_once()
            except SooFatalError:
                print("\n🛑 تم إيقاف النظام بسبب خطأ")
                break

            if config.CHECK_INTERVAL <= 0:
                break

            print(f"\n⏳ انتظار {config.CHECK_INTERVAL} ثانية قبل الفحص التالي...")
            await asyncio.sleep(config.CHECK_INTERVAL)

    def stop(self):
        self.running = False