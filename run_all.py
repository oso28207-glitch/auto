#!/usr/bin/env python3
"""
run_all.py — المنسق الرئيسي
1. جلب المصادر
2. تحميل الحلقات
3. رفع إلى Telegram
4. بناء الموقع
5. Git commit + push
"""
import os, sys, json, time, asyncio, subprocess, shutil
from datetime import datetime
from pathlib import Path

from config import config, DATA_DIR, MEDIA_DIR
from downloader import download_episode
from build_site import build_site

LOCK_FILE = DATA_DIR / ".lock"
STATE_FILE = DATA_DIR / "series.json"
UPLOADED_FILE = DATA_DIR / "uploaded.json"


# ═══════════════════════════════════════════════════════════════
# قفل بسيط
# ═══════════════════════════════════════════════════════════════
def acquire_lock():
    if os.environ.get("GITHUB_ACTIONS"):
        print("ℹ️  GitHub Actions — تخطي القفل")
        return True
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < 3600:
            print(f"⚠️  قفل موجود ({age/60:.0f}د) — خروج")
            return False
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    try: LOCK_FILE.unlink()
    except Exception: pass


# ═══════════════════════════════════════════════════════════════
# حالة التحميلات
# ═══════════════════════════════════════════════════════════════
def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"series": [], "last_update": "", "total_episodes": 0}


def save_state(state):
    state["last_update"] = datetime.utcnow().isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_uploaded():
    if UPLOADED_FILE.exists():
        try:
            with open(UPLOADED_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_uploaded(uploaded):
    with open(UPLOADED_FILE, "w", encoding="utf-8") as f:
        json.dump(uploaded, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════
# تحميل قائمة الحلقات من مصادرنا
# ═══════════════════════════════════════════════════════════════
def fetch_all_sources():
    """يجمع جميع المسلسلات من المصادر المفعّلة."""
    all_series = []

    for source in config.ENABLED_SOURCES.split(","):
        source = source.strip().lower()
        if not source:
            continue
        print(f"\n📡 [{source}] جلب...")
        try:
            if source == "u3seq":
                from sources.u3seq import fetch as fetch_u3seq
                items = fetch_u3seq()
                all_series.extend(items or [])
                print(f"   ✅ {len(items or [])} عنصر")
            elif source == "yam":
                from sources.yam import fetch as fetch_yam
                items = fetch_yam()
                all_series.extend(items or [])
                print(f"   ✅ {len(items or [])} مسلسل")
            elif source == "egybest":
                from sources.egybest import fetch as fetch_egybest
                items = fetch_egybest()
                all_series.extend(items or [])
                print(f"   ✅ {len(items or [])} مسلسل")
        except Exception as e:
            print(f"   ⚠️  {source}: {str(e)[:120]}")

    # دمج حسب الاسم
    merged = {}
    for s in all_series:
        name = s.get("name", "").strip()
        if not name:
            continue
        key = name
        if key not in merged:
            merged[key] = s
        else:
            # ادمج الحلقات
            existing_eps = {e.get("num"): e for e in merged[key].get("episodes", [])}
            for e in s.get("episodes", []):
                if e.get("num") not in existing_eps:
                    existing_eps[e["num"]] = e
            merged[key]["episodes"] = list(existing_eps.values())

    result = list(merged.values())
    for s in result:
        s["episodes"] = sorted(s.get("episodes", []),
                                key=lambda x: x.get("num", 0))
    return result


# ═══════════════════════════════════════════════════════════════
# الحلقة الرئيسية
# ═══════════════════════════════════════════════════════════════
def main():
    if not acquire_lock():
        return 1

    start = time.time()
    max_sec = config.MAX_RUNTIME_SECONDS

    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 12 + "شوف — Shoof Automation" + " " * 22 + "║")
    print("║" + " " * 8 + "مصادر متعددة • ضغط ذكي • موقع تلقائي" + " " * 12 + "║")
    print("╚" + "═" * 58 + "╝\n")

    try:
        config.validate()
        print("✅ الإعدادات صحيحة")
    except Exception as e:
        print(f"❌ {e}")
        return 1

    # تحميل الحالة
    state = load_state()
    uploaded = load_uploaded()
    print(f"💾 الحالة: {len(state.get('series', []))} مسلسل")
    print(f"📤 تم رفعه: {len(uploaded)} حلقة")

    # جلب المصادر
    all_series = fetch_all_sources()
    print(f"\n📊 إجمالي: {len(all_series)} مسلسل")

    if not all_series:
        print("⚠️  لا مسلسلات — إنشاء موقع فارغ")
        build_site()
        git_commit_push()
        return 0

    # معالجة كل مسلسل
    series_output = []
    processed = 0

    for series in all_series:
        if time.time() - start > max_sec:
            print(f"\n⏰ انتهى الوقت ({max_sec//60}د)")
            break

        name = series.get("name", "")
        episodes = series.get("episodes", [])
        if not episodes:
            continue

        # حلقات جديدة فقط
        new_eps = []
        for ep in episodes:
            ep_key = f"{name}|ep{ep.get('num', 0)}"
            if ep_key in uploaded:
                continue
            new_eps.append(ep)

        if not new_eps:
            # أضف المسلسل للموقع بدون تحميل
            series_output.append({
                "name": name,
                "poster": series.get("poster", ""),
                "genre": series.get("genre", ""),
                "episodes": [
                    {
                        "num": ep.get("num"),
                        "url": uploaded.get(f"{name}|ep{ep.get('num')}", {}).get("stream_url", "")
                    }
                    for ep in episodes
                ],
                "episodes_count": len(episodes),
            })
            continue

        print(f"\n{'═' * 60}")
        print(f"📺 {name}")
        print(f"   {len(new_eps)} حلقة جديدة من {len(episodes)}")
        elapsed = (time.time() - start) / 60
        remaining = (max_sec - (time.time() - start)) / 60
        print(f"   ⏱️  مضى: {elapsed:.1f}د | متبقٍ: {remaining:.1f}د")
        print(f"{'═' * 60}")

        success_count = 0
        for ep in new_eps:
            if time.time() - start > max_sec:
                break

            ep_num = ep.get("num", 0)
            ep_url = ep.get("url", "")
            if not ep_url:
                continue

            print(f"\n   ── الحلقة {ep_num} ──")
            try:
                result = download_episode(name, ep_num, ep_url)
                if result and Path(result).exists():
                    # رفع إلى Telegram (إن كان مفعلاً)
                    uploaded_info = upload_to_telegram(result, name, ep_num)
                    if uploaded_info:
                        uploaded[f"{name}|ep{ep_num}"] = uploaded_info
                        success_count += 1
                        print(f"   ✅ تمت الحلقة {ep_num}")
                    else:
                        print(f"   ⚠️ فشل الرفع")
                else:
                    print(f"   ⚠️ فشل التحميل")
            except Exception as e:
                print(f"   ❌ {str(e)[:150]}")

        # أضف للموقع
        series_output.append({
            "name": name,
            "poster": series.get("poster", ""),
            "genre": series.get("genre", ""),
            "episodes": [
                {
                    "num": ep.get("num"),
                    "url": uploaded.get(f"{name}|ep{ep.get('num')}", {}).get("stream_url", "")
                    or ep.get("url", "")  # fallback: استخدم رابط المصدر
                }
                for ep in episodes
            ],
            "episodes_count": len(episodes),
            "is_new": success_count > 0,
            "added_at": datetime.utcnow().isoformat(),
        })

        processed += 1
        save_state({"series": series_output,
                    "total_episodes": sum(s["episodes_count"] for s in series_output)})
        save_uploaded(uploaded)
        print(f"   💾 حالة محدثة")

    # اكتب بيانات الموقع
    print(f"\n💾 كتابة {len(series_output)} مسلسل إلى series.json")
    save_state({
        "series": series_output,
        "total_episodes": sum(s["episodes_count"] for s in series_output),
    })
    save_uploaded(uploaded)

    # بناء الموقع
    build_site()

    # Git push
    git_commit_push()

    elapsed = (time.time() - start) / 60
    print(f"\n{'═' * 60}")
    print(f"⏱️  انتهى في {elapsed:.1f}د")
    print(f"✅ {processed} مسلسل معالج")
    print(f"{'═' * 60}")
    return 0


def upload_to_telegram(video_path, series_name, ep_num):
    """رفع الفيديو إلى Telegram."""
    if os.environ.get("SKIP_UPLOAD", "false").lower() == "true":
        return {"stream_url": f"local://{video_path.name}", "uploaded": False}

    try:
        from uploader import upload_video
        result = upload_video(str(video_path), series_name, ep_num)
        return result
    except Exception as e:
        print(f"   ⚠️  رفع: {str(e)[:120]}")
        return None


def git_commit_push():
    """commit + push إلى GitHub."""
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "config", "user.email",
                        "github-actions[bot]@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", "-f", "data/", "docs/"], check=False)

        r = subprocess.run(["git", "diff", "--staged", "--quiet"], capture_output=True)
        if r.returncode == 0:
            print("ℹ️  لا تغييرات")
            return

        msg = f"🤖 تحديث تلقائي: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
        subprocess.run(["git", "commit", "-m", msg], check=False)

        for i in range(3):
            r = subprocess.run(["git", "push"], capture_output=True, text=True)
            if r.returncode == 0:
                print("✅ push OK")
                return
            print(f"⚠️  push محاولة {i+1} فشل — pull --rebase")
            subprocess.run(["git", "pull", "--rebase", "--autostash"], check=False)
            time.sleep(5)
    except Exception as e:
        print(f"⚠️  git: {str(e)[:120]}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        release_lock()