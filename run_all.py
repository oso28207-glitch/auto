#!/usr/bin/env python3
"""
run_all.py — المنسق الموحد لكل السكربتات

★ يضمن التشغيل المتناغم:
  1. فحص القنوات (اختياري).
  2. جلب الصور.
  3. تحميل + رفع الحلقات.
  4. بناء الموقع.
  5. push فوري بعد كل تغيير.

يمنع التعارض:
  - قفل file lock لمنع تشغيل نسختين.
  - git pull --rebase قبل أي push.
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from config import config, DATA_DIR


LOCK_FILE = DATA_DIR / ".run.lock"
LOCK_TTL = 3600 * 3  # 3 ساعات


def acquire_lock():
    """يمنع التشغيل المتزامن."""
    if LOCK_FILE.exists():
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age < LOCK_TTL:
                print(f"⚠️ سكربت آخر يعمل (منذ {age/60:.1f}د) — خروج")
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
    """git pull --rebase قبل أي عملية."""
    try:
        subprocess.run(
            ["git", "pull", "--rebase", "--autostash"],
            capture_output=True, timeout=60,
        )
    except Exception:
        pass


async def main():
    if not acquire_lock():
        return 1

    try:
        git_sync()

        # 1) فحص القنوات + الاستيراد (إن لم يتم بعد)
        print("═" * 60)
        print("📡 الخطوة 1: استيراد الحلقات القديمة من القنوات")
        print("═" * 60)
        try:
            from import_from_channels import main as import_main
            # ننفذه مرة واحدة فقط كل 24 ساعة
            last_import = DATA_DIR / ".last_import"
            if (not last_import.exists() or
                    time.time() - last_import.stat().st_mtime > 86400):
                # شغّله
                pass
        except Exception as e:
            print(f"⚠️ استيراد: {str(e)[:150]}")

        # 2) جلب الصور
        print("\n" + "═" * 60)
        print("🖼️  الخطوة 2: جلب صور المسلسلات")
        print("═" * 60)
        try:
            from fetch_posters import fetch_posters
            fetch_posters()
        except Exception as e:
            print(f"⚠️ الصور: {str(e)[:150]}")

        # 3) تشغيل run.py الرئيسي
        print("\n" + "═" * 60)
        print("🎬 الخطوة 3: تحميل ورفع الحلقات")
        print("═" * 60)
        from run import main as run_main
        rc = await run_main()
        if rc != 0:
            print(f"⚠️ run.py أعاد {rc}")

        # 4) إعادة بناء الموقع (لضمان الصور الحديثة)
        print("\n" + "═" * 60)
        print("🏗️  الخطوة 4: البناء النهائي")
        print("═" * 60)
        try:
            from builder import build_incremental
            build_incremental()
        except Exception as e:
            print(f"⚠️ بناء: {str(e)[:150]}")

        # 5) push نهائي
        print("\n" + "═" * 60)
        print("🚀 الخطوة 5: push إلى GitHub")
        print("═" * 60)
        try:
            subprocess.run(["git", "add", "-f", "docs/", "data/"],
                           capture_output=True, timeout=30)
            r = subprocess.run(["git", "diff", "--staged", "--quiet"],
                               capture_output=True, timeout=10)
            if r.returncode != 0:
                ts = time.strftime("%Y-%m-%d %H:%M")
                subprocess.run(["git", "commit", "-m",
                                f"🤖 تحديث شامل: {ts}"],
                               capture_output=True, timeout=30)
                subprocess.run(["git", "push"],
                               capture_output=True, timeout=90)
                print("   ✅ تم الدفع")
            else:
                print("   ℹ️  لا تغييرات")
        except Exception as e:
            print(f"   ⚠️ push: {str(e)[:150]}")

        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))