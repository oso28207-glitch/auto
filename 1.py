#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
1.py — إنشاء جلستي Pyrogram منفصلتين لنظام Soo
  • uploader → GitHub Actions (تحميل ورفع الحلقات)
  • streamer → Railway/Render (خادم البث)

⚠️ لماذا جلستان؟
   استخدام نفس STRING_SESSION في مكانين متزامنين يسبب
   AUTH_KEY_DUPLICATED وقد يؤدي لإلغاء الجلسة من Telegram.

✅ متوافق مع Python 3.10 حتى 3.14
"""

# ═══════════════════════════════════════════════════════════════
# 1) إصلاح توافق asyncio مع Python 3.12+
#    يجب أن يكون هذا قبل أي استيراد لـ pyrogram
# ═══════════════════════════════════════════════════════════════
import asyncio

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

# ═══════════════════════════════════════════════════════════════
# 2) الاستيرادات
# ═══════════════════════════════════════════════════════════════
import getpass
import os
import sys
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import (
    ApiIdInvalid,
    PasswordHashInvalid,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
)

# ═══════════════════════════════════════════════════════════════
# 3) ثوابت
# ═══════════════════════════════════════════════════════════════
SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

BANNER = """
╔══════════════════════════════════════════════════════╗
║      مولّد جلسات Pyrogram — نظام Soo                 ║
║      جلستان منفصلتان: uploader + streamer            ║
╚══════════════════════════════════════════════════════╝
"""


# ═══════════════════════════════════════════════════════════════
# 4) دوال مساعدة
# ═══════════════════════════════════════════════════════════════
def save_to_file(name: str, session_string: str) -> Path:
    """يحفظ الجلسة في ملف محلي بصلاحيات محدودة."""
    out = SESSIONS_DIR / f"{name}_session.txt"
    out.write_text(session_string, encoding="utf-8")
    try:
        os.chmod(out, 0o600)  # قراءة/كتابة للمالك فقط
    except Exception:
        pass
    return out


def cleanup_old_sessions(name: str) -> None:
    """يحذف أي جلسات قديمة بنفس الاسم."""
    for f in SESSIONS_DIR.glob(f"{name}*"):
        try:
            f.unlink()
        except Exception:
            pass


async def interactive_login(client: Client, label: str) -> None:
    """يطلب الرقم ثم الكود ثم 2FA إن لزم."""
    phone = input(f"[{label}] رقم الهاتف (بصيغة دولية، مثال +201234567890): ").strip()
    if not phone:
        print("❌ لم تُدخل رقماً.")
        sys.exit(1)
    if not phone.startswith("+"):
        phone = "+" + phone

    # الاتصال
    try:
        await client.connect()
    except ApiIdInvalid:
        print("❌ API_ID أو API_HASH غير صحيح.")
        sys.exit(1)
    except Exception as e:
        print(f"❌ فشل الاتصال بـ Telegram: {e}")
        sys.exit(1)

    # إرسال الكود
    try:
        sent = await client.send_code(phone)
    except PhoneNumberInvalid:
        print("❌ رقم الهاتف غير صحيح.")
        sys.exit(1)
    except Exception as e:
        print(f"❌ فشل إرسال الكود: {e}")
        sys.exit(1)

    # إدخال الكود
    code = input(f"[{label}] أدخل كود التحقق (أرقام فقط): ").strip()
    code = code.replace(" ", "").replace("-", "")

    # تسجيل الدخول
    try:
        await client.sign_in(phone, sent.phone_code_hash, code)
    except SessionPasswordNeeded:
        pw = getpass.getpass(f"[{label}] كلمة مرور التحقق بخطوتين (2FA): ")
        try:
            await client.check_password(pw)
        except PasswordHashInvalid:
            print("❌ كلمة مرور 2FA خاطئة.")
            sys.exit(1)
    except PhoneCodeInvalid:
        print("❌ كود التحقق خاطئ.")
        sys.exit(1)
    except Exception as e:
        print(f"❌ فشل تسجيل الدخول: {e}")
        sys.exit(1)


async def create_session(name: str, label: str, api_id: int, api_hash: str) -> str:
    """ينشئ جلسة واحدة ويعيد STRING_SESSION."""
    print(f"\n{'═' * 60}")
    print(f"  إنشاء جلسة: {label}  ({name})")
    print(f"{'═' * 60}")

    cleanup_old_sessions(name)

    session_path = SESSIONS_DIR / name
    client = Client(
        name=name,
        api_id=api_id,
        api_hash=api_hash,
        workdir=str(SESSIONS_DIR),
        in_memory=False,
        no_updates=True,
    )

    try:
        await interactive_login(client, label)
        me = await client.get_me()
        session_string = await client.export_session_string()
        saved = save_to_file(name, session_string)

        print(f"\n✅ تم تسجيل الدخول كـ: {me.first_name or ''} (@{me.username or me.id})")
        print(f"💾 حُفظت الجلسة في: {saved}")
        print(f"\n──── {name.upper()}_SESSION ────")
        print(session_string)
        print(f"──── نهاية {name.upper()}_SESSION ────\n")

        return session_string
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def ensure_gitignore() -> None:
    """يتأكد من أن مجلد sessions/ مُستبعد من git."""
    gi = Path(".gitignore")
    needed = ["sessions/", "*_session.txt", "*.session", "*.session-journal"]
    if gi.exists():
        content = gi.read_text(encoding="utf-8")
    else:
        content = ""

    to_add = [line for line in needed if line not in content]
    if to_add:
        with gi.open("a", encoding="utf-8") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write("\n# Pyrogram sessions\n")
            for line in to_add:
                f.write(line + "\n")
        print(f"✓ تمت إضافة {len(to_add)} سطر إلى .gitignore")


# ═══════════════════════════════════════════════════════════════
# 5) الدالة الرئيسية
# ═══════════════════════════════════════════════════════════════
async def main():
    print(BANNER)

    # ─── قراءة API_ID و API_HASH ───
    api_id_env = os.environ.get("API_ID", "").strip()
    api_hash_env = os.environ.get("API_HASH", "").strip()

    if api_id_env and api_hash_env:
        try:
            api_id = int(api_id_env)
        except ValueError:
            print("❌ API_ID في المتغيرات البيئية ليس رقماً.")
            sys.exit(1)
        api_hash = api_hash_env
        print(f"✓ استخدام API_ID={api_id} من المتغيرات البيئية")
    else:
        try:
            api_id = int(input("API ID (من my.telegram.org): ").strip())
        except ValueError:
            print("❌ API_ID يجب أن يكون رقماً.")
            sys.exit(1)
        api_hash = input("API Hash: ").strip()

    if not api_id or not api_hash:
        print("❌ API_ID و API_HASH مطلوبان.")
        sys.exit(1)

    print("\n⚠️  تنبيه:")
    print("   • ستسجّل الدخول مرتين.")
    print("   • يمكنك استخدام نفس الحساب أو حسابين مختلفين.")
    print("   • الأفضل حسابان مختلفان لتجنب حظر أمني.")
    print("   • لن يُحفظ أي شيء في Git — فقط ملفات محلية.\n")

    ans = input("هل تريد المتابعة؟ (y/n): ").strip().lower()
    if ans != "y":
        print("تم الإلغاء.")
        sys.exit(0)

    # ─── الجلسة 1: uploader ───
    await create_session("uploader", "Uploader — GitHub Actions", api_id, api_hash)

    print("\n" + "=" * 60)
    print("  الآن الجلسة الثانية. يمكنك استخدام نفس الحساب أو حساب آخر.")
    print("=" * 60 + "\n")

    # ─── الجلسة 2: streamer ───
    await create_session("streamer", "Streamer — Railway/Render", api_id, api_hash)

    # ─── تحديث .gitignore ───
    ensure_gitignore()

    # ─── ملخص نهائي ───
    print("\n" + "═" * 60)
    print("  ✅ اكتمل! الخطوات التالية:")
    print("═" * 60)
    print("""
  1) GitHub → Settings → Secrets and variables → Actions:
        STRING_SESSION  ←  محتوى sessions/uploader_session.txt
        API_ID
        API_HASH
        CHANNEL_ID

  2) Railway / Render → Variables:
        STRING_SESSION  ←  محتوى sessions/streamer_session.txt
        API_ID
        API_HASH
        CHANNEL

  ⚠️  لا ترفع مجلد sessions/ إلى GitHub أبداً.
      تمت إضافته تلقائياً إلى .gitignore.
""")

    print("📂 الملفات المُنشأة:")
    for f in sorted(SESSIONS_DIR.glob("*_session.txt")):
        print(f"   • {f}")


# ═══════════════════════════════════════════════════════════════
# 6) نقطة التشغيل
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⏹  تم الإلغاء بواسطة المستخدم.")
        sys.exit(0)
    except Exception as e:
        print(f"\n💥 خطأ غير متوقع: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)