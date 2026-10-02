#!/usr/bin/env python3
"""
🚀 نقطة التشغيل الوحيدة.
    python run.py

تقوم بكل شيء: فحص → تحميل → رفع → تتبع → بناء.
"""

import asyncio
import sys

from config import config
from errors import SooFatalError
from orchestrator import Orchestrator


def main():
    print("""
    ╔══════════════════════════════════════╗
    ║   Soo Automation — نظام متكامل       ║
    ║   تحميل • رفع • تتبع • بناء          ║
    ╚══════════════════════════════════════╝
    """)

    # التحقق من الإعدادات
    try:
        config.validate()
        print("✓ الإعدادات صحيحة")
    except SooFatalError as e:
        print(f"✗ خطأ في الإعدادات: {e.message}")
        sys.exit(1)

    # التشغيل
    orch = Orchestrator()
    try:
        if config.CHECK_INTERVAL > 0:
            print(f"🔄 وضع المراقبة المستمرة (كل {config.CHECK_INTERVAL}s)")
            asyncio.run(orch.run_forever())
        else:
            print("▶️  تشغيل مرة واحدة")
            asyncio.run(orch.run_once())
    except KeyboardInterrupt:
        print("\n\n⏹  تم الإيقاف بواسطة المستخدم")
    except SooFatalError as e:
        print(f"\n🛑 توقف النظام: [{e.stage}] {e.message}")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 خطأ غير متوقع: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()