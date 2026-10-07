#!/usr/bin/env python3
"""
run_all.py — تنسيق: عشق (u3seq) أولاً ثم yam ثم egybest
"""
import os, sys, json, time, asyncio, subprocess, shutil
from datetime import datetime
from pathlib import Path

from config import config, DATA_DIR, MEDIA_DIR
from downloader import download_episode

STATE_FILE = DATA_DIR / "series.json"
UPLOADED_FILE = DATA_DIR / "uploaded.json"


def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception: pass
    return {"series": []}


def save_state(state):
    state["last_update"] = datetime.utcnow().isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_uploaded():
    if UPLOADED_FILE.exists():
        try:
            with open(UPLOADED_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception: pass
    return {}


def save_uploaded(u):
    with open(UPLOADED_FILE, "w", encoding="utf-8") as f:
        json.dump(u, f, ensure_ascii=False, indent=2)


def fetch_source(source_name):
    """يجلب مصدراً واحداً."""
    try:
        if source_name == "u3seq":
            from sources.u3seq import fetch as fetch_u3seq
            return fetch_u3seq() or []
        elif source_name == "yam":
            from sources.yam import fetch as fetch_yam
            return fetch_yam() or []
        elif source_name == "egybest":
            from sources.egybest import fetch as fetch_egybest
            return fetch_egybest() or []
    except Exception as e:
        print(f"   ⚠️ {source_name}: {str(e)[:120]}", flush=True)
    return []


def main():
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 12 + "شوف — Shoof Automation" + " " * 22 + "║")
    print("║" + " " * 8 + "مصادر متعددة • ضغط ذكي • موقع تلقائي" + " " * 12 + "║")
    print("╚" + "═" * 58 + "╝\n")

    try:
        config.validate()
        print("✅ الإعدادات صحيحة", flush=True)
    except Exception as e:
        print(f"❌ {e}"); return 1

    state = load_state()
    uploaded = load_uploaded()
    print(f"💾 الحالة: {len(state.get('series', []))} مسلسل", flush=True)
    print(f"📤 تم رفعه: {len(uploaded)} حلقة", flush=True)

    # ★★★ المصادر بالترتيب: u3seq أولاً (عشق)
    sources_order = ["u3seq", "yam", "egybest"]
    all_series = []

    for src in sources_order:
        print(f"\n📡 [{src}] جلب...", flush=True)
        items = fetch_source(src)
        print(f"   ✅ {len(items)} مسلسل", flush=True)
        # أضف علامة المصدر
        for s in items:
            s["_source"] = src
        all_series.extend(items)

    print(f"\n📊 إجمالي: {len(all_series)} مسلسل", flush=True)

    if not all_series:
        print("⚠️  لا مسلسلات")
        return 0

    # ★★★ معالجة u3seq أولاً
    all_series.sort(key=lambda s: {"u3seq": 0, "yam": 1, "egybest": 2}.get(s.get("_source", "z"), 9))

    start = time.time()
    max_sec = config.MAX_RUNTIME_SECONDS
    series_output = []
    processed = 0

    for series in all_series:
        if time.time() - start > max_sec:
            print(f"\n⏰ انتهى الوقت", flush=True)
            break

        name = series.get("name", "")
        episodes = series.get("episodes", [])
        src = series.get("_source", "?")
        if not episodes: continue

        # حلقات جديدة فقط
        new_eps = []
        for ep in episodes:
            key = f"{name}|ep{ep.get('num', 0)}"
            if key in uploaded: continue
            new_eps.append(ep)

        if not new_eps:
            series_output.append({
                "name": name,
                "poster": series.get("poster", ""),
                "genre": series.get("genre", ""),
                "source": src,
                "episodes": [{"num": ep.get("num"), "url": uploaded.get(f"{name}|ep{ep.get('num')}", {}).get("url", ep.get("url", ""))} for ep in episodes],
                "episodes_count": len(episodes),
            })
            continue

        print(f"\n{'═' * 60}", flush=True)
        print(f"📺 [{src}] {name}", flush=True)
        print(f"   {len(new_eps)} حلقة جديدة من {len(episodes)}", flush=True)
        elapsed = (time.time() - start) / 60
        remaining = (max_sec - (time.time() - start)) / 60
        print(f"   ⏱️  مضى: {elapsed:.1f}د | متبقٍ: {remaining:.1f}د", flush=True)
        print(f"{'═' * 60}", flush=True)

        success_count = 0
        for ep in new_eps:
            if time.time() - start > max_sec: break

            ep_num = ep.get("num", 0)
            ep_url = ep.get("url", "")
            if not ep_url: continue

            print(f"\n   ── الحلقة {ep_num} ──", flush=True)
            try:
                result = download_episode(name, ep_num, ep_url)
                if result and Path(result).exists():
                    uploaded[f"{name}|ep{ep_num}"] = {"url": ep_url, "uploaded": True}
                    success_count += 1
                    print(f"   ✅ تمت الحلقة {ep_num}", flush=True)
                else:
                    print(f"   ⚠️ فشل التحميل", flush=True)
            except Exception as e:
                print(f"   ❌ {str(e)[:150]}", flush=True)

        series_output.append({
            "name": name,
            "poster": series.get("poster", ""),
            "genre": series.get("genre", ""),
            "source": src,
            "episodes": [{"num": ep.get("num"), "url": uploaded.get(f"{name}|ep{ep.get('num')}", {}).get("url", ep.get("url", ""))} for ep in episodes],
            "episodes_count": len(episodes),
            "is_new": success_count > 0,
        })

        processed += 1
        save_state({"series": series_output})
        save_uploaded(uploaded)

    save_state({"series": series_output})
    save_uploaded(uploaded)

    # بناء الموقع
    try:
        from build_site import build_site
        build_site()
    except Exception as e:
        print(f"⚠️ build_site: {e}")

    elapsed = (time.time() - start) / 60
    print(f"\n{'═' * 60}", flush=True)
    print(f"⏱️  انتهى في {elapsed:.1f}د", flush=True)
    print(f"✅ {processed} مسلسل معالج", flush=True)
    print(f"{'═' * 60}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())