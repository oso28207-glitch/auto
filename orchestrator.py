"""
orchestrator.py — المنسق
يستخدم الحلقات من fetch_series_list مباشرة، بدون إعادة فحص.
"""

import asyncio
import time

from builder import build_incremental
from checker import fetch_series_list
from config import config
from database import db
from downloader import download_episode
from errors import SooFatalError
from uploader import uploader


class Orchestrator:
    def __init__(self):
        self.running = False

    async def run_once(self):
        print("\n" + "=" * 60)
        print("🚀 بدء دورة جديدة")
        print("=" * 60)

        await uploader.start()

        try:
            # ─── 1) جلب كل المسلسلات مع حلقاتها ───
            print("\n📋 جلب قائمة المسلسلات...")
            series_list = fetch_series_list()
            print(f"   وجدت {len(series_list)} مسلسل")

            total_new = 0
            total_uploaded = 0

            # ─── 2) لكل مسلسل ───
            for s in series_list:
                name = s["name"]
                url = s["url"]
                posts = s.get("posts", [])

                existing = db.get_series(name)
                is_new_series = not existing.get("url")

                if is_new_series:
                    print(f"\n📺 مسلسل جديد: {name}")
                    db.set_series(name, {"url": url, "episodes": {}})
                else:
                    # تحديث URL إذا تغيّر
                    if existing.get("url") != url:
                        existing["url"] = url
                        db.set_series(name, existing)

                # ─── 3) استخراج الحلقات الجديدة من posts مباشرة ───
                new_eps = []
                for post in posts:
                    ep_num = post.get("episode", 0)
                    if not ep_num:
                        continue  # تجاهل المنشورات بدون رقم حلقة

                    if db.episode_status(name, ep_num) == "uploaded":
                        continue

                    new_eps.append({
                        "number": ep_num,
                        "url": post["link"],
                        "title": post.get("title", f"الحلقة {ep_num}"),
                    })

                # إزالة التكرار (نفس الحلقة قد تظهر في منشورات متعددة)
                seen = set()
                unique_eps = []
                for ep in new_eps:
                    if ep["number"] not in seen:
                        seen.add(ep["number"])
                        unique_eps.append(ep)
                new_eps = sorted(unique_eps, key=lambda x: x["number"])

                if not new_eps:
                    print(f"   ⏭️  {name}: لا توجد حلقات جديدة "
                          f"({len(posts)} حلقة إجمالاً)")
                    continue

                print(f"\n🔍 {name}: {len(new_eps)} حلقة جديدة "
                      f"(من أصل {len(posts)})")
                total_new += len(new_eps)

                # ─── 4) تحميل ورفع كل حلقة جديدة بالترتيب ───
                for ep in new_eps:
                    num = ep["number"]
                    print(f"\n   ── الحلقة {num} ──")

                    try:
                        # تحميل
                        db.set_episode(name, num, status="downloading",
                                       url=ep["url"], title=ep["title"])
                        file_path = download_episode(name, num, ep["url"])

                        # رفع
                        db.set_episode(name, num, status="uploading")
                        message_id = await uploader.upload_episode(
                            series=name,
                            episode=num,
                            file_path=file_path,
                        )

                        # حفظ
                        db.set_episode(
                            name, num,
                            status="uploaded",
                            message_id=message_id,
                        )
                        db.add_video({
                            "id": message_id,
                            "title": f"{name} — الحلقة {num}",
                            "series": name,
                            "episode": num,
                            "size": file_path.stat().st_size,
                            "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        })

                        # بناء تدريجي بعد كل حلقة
                        if config.AUTO_BUILD:
                            build_incremental(series_name=name)

                        total_uploaded += 1
                        print(f"   ✅ اكتملت الحلقة {num}")

                        # حذف الملف إذا لم نكن نحتفظ به
                        if not config.KEEP_MEDIA:
                            try:
                                file_path.unlink()
                            except Exception:
                                pass

                    except SooFatalError:
                        # خطأ خطير → ارفعه ليوقف كل شيء
                        raise
                    except Exception as e:
                        raise SooFatalError(
                            "UPLOAD",
                            f"خطأ غير متوقع في {name} الحلقة {num}: {e}",
                            e,
                        )

            # ─── 5) بناء نهائي ───
            if config.AUTO_BUILD:
                build_incremental()

            print("\n" + "=" * 60)
            print(f"✅ اكتملت الدورة: {total_uploaded} رفعت من {total_new} جديدة")
            print("=" * 60)

        except SooFatalError:
            print("\n🛑 إيقاف جميع العمليات")
            raise
        except Exception as e:
            print(f"\n❌ خطأ غير متوقع: {e}")
            raise SooFatalError("GENERAL", str(e), e)
        finally:
            await uploader.stop()

    async def run_forever(self):
        self.running = True
        while self.running:
            try:
                await self.run_once()
            except SooFatalError:
                print("\n🛑 تم إيقاف النظام بسبب خطأ")
                break

            if config.CHECK_INTERVAL <= 0:
                break

            print(f"\n⏳ انتظار {config.CHECK_INTERVAL} ثانية...")
            await asyncio.sleep(config.CHECK_INTERVAL)

    def stop(self):
        self.running = False
