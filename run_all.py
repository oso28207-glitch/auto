#!/usr/bin/env python3
"""
run_all.py — Shoof Automation v2

★ ترتيب العمل الجديد:
    1) فحص قنوات Telegram أولاً لمعرفة المرفوع مسبقاً
    2) زحف المصادر (u3seq, yam, egybest)
    3) تنزيل + ضغط + رفع الحلقات الجديدة فقط
"""
import asyncio
import os
import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from config import config, DATA_DIR, MEDIA_DIR
from downloader import download_episode
from telegram_checker import (
    fetch_existing_episodes,
    is_episode_uploaded,
    get_stats as tg_stats,
)
from uploader import uploader

STATE = DATA_DIR / "series.json"
UPLOADED = DATA_DIR / "uploaded.json"

SOURCE_ORDER = {"u3seq": 0, "yam": 1, "egybest": 2}


# ═══════════════════════════════════════════════════════════════
# أدوات
# ═══════════════════════════════════════════════════════════════
def _load(p, default):
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _fetch(src):
    try:
        if src == "u3seq":
            from sources.u3seq import fetch
            return fetch() or []
        if src == "yam":
            from sources.yam import fetch
            return fetch() or []
        if src == "egybest":
            from sources.egybest import fetch
            return fetch() or []
    except Exception as e:
        print(f"   ⚠️ {src}: {str(e)[:120]}", flush=True)
    return []


# ═══════════════════════════════════════════════════════════════
# الخطوة 1: فحص Telegram
# ═══════════════════════════════════════════════════════════════
async def _scan_telegram():
    print("\n" + "═" * 60, flush=True)
    print("📡 [الخطوة 1] فحص قنوات Telegram أولاً...", flush=True)
    print("═" * 60, flush=True)

    try:
        existing = await fetch_existing_episodes()
    except Exception as e:
        print(f"   ⚠️ فشل فحص Telegram: {str(e)[:200]}", flush=True)
        print(f"   ↳ سيتم الاعتماد على uploaded.json فقط", flush=True)
        return {}

    stats = tg_stats(existing)
    print(f"   📊 Telegram: {stats['series']} مسلسل | "
          f"{stats['episodes']} حلقة", flush=True)
    return existing


# ═══════════════════════════════════════════════════════════════
# الخطوة 2: زحف المصادر
# ═══════════════════════════════════════════════════════════════
def _scrape_all():
    print("\n" + "═" * 60, flush=True)
    print("📡 [الخطوة 2] فحص المصادر...", flush=True)
    print("═" * 60, flush=True)

    all_series = []
    for src in ["u3seq", "yam", "egybest"]:
        print(f"\n   [{src}]", flush=True)
        items = _fetch(src)
        for s in items:
            s["_src"] = src
        print(f"      ✅ {len(items)} مسلسل", flush=True)
        all_series.extend(items)

    all_series.sort(key=lambda s: SOURCE_ORDER.get(s.get("_src", "z"), 9))
    print(f"\n📊 إجمالي: {len(all_series)} مسلسل\n", flush=True)
    return all_series


# ═══════════════════════════════════════════════════════════════
# تحديد الحلقات الجديدة (Telegram + محلي)
# ═══════════════════════════════════════════════════════════════
def _filter_new_episodes(series, existing_tg, uploaded):
    """
    يعيد (new_eps, all_eps, newly_marked)
      - new_eps: حلقات تحتاج تنزيلاً فعلياً
      - all_eps: كل الحلقات بعد ضمّ URLs من uploaded (للفهرسة)
      - newly_marked: حلقات اكتُشفت على TG وعُلّمت محلياً
    """
    name = series.get("name", "")
    eps = series.get("episodes", [])
    new_eps = []
    newly_marked = []

    for e in eps:
        n = e.get("num", 0)
        if not n:
            continue

        local_key = f"{name}|ep{n}"

        # 1) موجود محلياً؟
        if local_key in uploaded:
            continue

        # 2) موجود على Telegram؟
        if existing_tg and is_episode_uploaded(existing_tg, name, n):
            uploaded[local_key] = {
                "url": e.get("url", ""),
                "done": True,
                "source": "telegram",
            }
            newly_marked.append(local_key)
            continue

        # 3) جديد فعلاً
        new_eps.append(e)

    all_eps = [
        {
            "num": e.get("num"),
            "url": uploaded.get(f"{name}|ep{e.get('num')}", {}).get(
                "url", e.get("url", "")
            ),
        }
        for e in eps
    ]

    return new_eps, all_eps, newly_marked


# ═══════════════════════════════════════════════════════════════
# الخطوة 3: تنزيل + ضغط + رفع
# ═══════════════════════════════════════════════════════════════
async def _process_series(series, existing_tg, uploaded):
    name = series.get("name", "")
    src = series.get("_src", "?")
    poster = series.get("poster", "")
    genre = series.get("genre", "")

    new_eps, all_eps, newly_marked = _filter_new_episodes(
        series, existing_tg, uploaded
    )

    if newly_marked:
        print(f"   🎯 [{src}] {name}: {len(newly_marked)} حلقة موجودة على Telegram",
              flush=True)

    if not new_eps:
        return {
            "name": name,
            "poster": poster,
            "genre": genre,
            "source": src,
            "episodes": all_eps,
            "episodes_count": len(all_eps),
        }

    print(f"\n{'═' * 60}", flush=True)
    print(f"📺 [{src}] {name}", flush=True)
    print(f"   🆕 {len(new_eps)}/{len(all_eps)} حلقة جديدة", flush=True)
    print(f"{'═' * 60}", flush=True)

    ok_count = 0

    for ep in new_eps:
        n = ep.get("num", 0)
        u = ep.get("url", "")
        if not u:
            continue

        print(f"\n   ── الحلقة {n} ──", flush=True)

        # 1) تنزيل + ضغط (الإعدادات كما هي)
        try:
            r = download_episode(name, n, u)
        except Exception as e:
            print(f"   ❌ تنزيل: {str(e)[:120]}", flush=True)
            continue

        if not r or not Path(r).exists():
            print(f"   ⚠️ فشل التنزيل", flush=True)
            continue

        # 2) رفع على Telegram
        try:
            info = await uploader.upload(
                file_path=r,
                item_name=name,
                media_type="series",
                part_number=n,
            )
            uploaded[f"{name}|ep{n}"] = {
                "url": u,
                "done": True,
                "message_id": info.get("message_id"),
                "file_id": info.get("file_id"),
                "size": info.get("size"),
                "at": datetime.now(timezone.utc).isoformat(),
            }
            ok_count += 1
            print(f"   ✅ تمت + رُفعت (msg={info.get('message_id')})", flush=True)

        except Exception as e:
            print(f"   ⚠️ فشل الرفع: {str(e)[:150]}", flush=True)
            uploaded[f"{name}|ep{n}"] = {
                "url": u,
                "done": True,
                "upload_failed": True,
                "at": datetime.now(timezone.utc).isoformat(),
            }

    return {
        "name": name,
        "poster": poster,
        "genre": genre,
        "source": src,
        "episodes": all_eps,
        "episodes_count": len(all_eps),
        "is_new": ok_count > 0,
    }


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════
async def _main_async():
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 12 + "شوف — Shoof Automation v2" + " " * 20 + "║")
    print("╚" + "═" * 58 + "╝", flush=True)

    try:
        config.validate()
        print("✅ الإعدادات صحيحة\n", flush=True)
    except Exception as e:
        print(f"❌ {e}")
        return 1

    # ─── 1) فحص Telegram ───
    existing_tg = await _scan_telegram()

    # ─── تحميل الحالة المحلية ───
    state = _load(STATE, {"series": []})
    uploaded = _load(UPLOADED, {})
    print(
        f"\n💾 محلي: {len(state.get('series', []))} مسلسل | "
        f"📤 {len(uploaded)} حلقة مُسجّلة",
        flush=True,
    )

    # ─── 2) زحف المصادر ───
    all_series = _scrape_all()
    if not all_series:
        return 0

    # ─── 3) المعالجة ───
    print("\n" + "═" * 60, flush=True)
    print("⬇️  [الخطوة 3] تنزيل + ضغط + رفع الجديد...", flush=True)
    print("═" * 60, flush=True)

    await uploader.start()

    start = time.time()
    max_sec = config.MAX_RUNTIME_SECONDS
    out_series = []

    try:
        for series in all_series:
            if time.time() - start > max_sec:
                print(f"\n⏰ انتهى وقت التشغيل — توقف آمن", flush=True)
                break

            try:
                item = await _process_series(series, existing_tg, uploaded)
                if item:
                    out_series.append(item)
            except Exception as e:
                print(f"   ❌ [{series.get('name','?')}]: {str(e)[:150]}", flush=True)
                continue

            # حفظ تدريجي
            _save(
                STATE,
                {
                    "series": out_series,
                    "last_update": datetime.now(timezone.utc).isoformat(),
                },
            )
            _save(UPLOADED, uploaded)

    finally:
        try:
            await uploader.stop()
        except Exception:
            pass

    # حفظ نهائي
    _save(
        STATE,
        {
            "series": out_series,
            "last_update": datetime.now(timezone.utc).isoformat(),
        },
    )
    _save(UPLOADED, uploaded)

    # بناء الموقع
    try:
        from build_site import build_site
        build_site()
    except Exception as e:
        print(f"⚠️ build_site: {e}", flush=True)

    elapsed = (time.time() - start) / 60
    print(f"\n{'═' * 60}")
    print(f"⏱️  انتهى في {elapsed:.1f}د | ✅ {len(out_series)} مسلسل")
    print(f"{'═' * 60}", flush=True)
    return 0


def main():
    return asyncio.run(_main_async())


if __name__ == "__main__":
    sys.exit(main())