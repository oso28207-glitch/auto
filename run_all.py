#!/usr/bin/env python3
"""
run_all.py — Shoof Automation v3

★ الميزات الجديدة:
    1) فحص قنوات Telegram أولاً وحفظ النتائج
    2) تقرير واضح بالحلقات الناقصة لكل مسلسل
    3) إكمال الناقص بالترتيب (1، 2، 3...) قبل الجديد
    4) تنزيل + ضغط + رفع
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
MISSING_REPORT = DATA_DIR / "missing_report.json"

SOURCE_ORDER = {"u3seq": 0, "yam": 1, "egybest": 2}


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
# ★★★ تقرير الحلقات الناقصة
# ═══════════════════════════════════════════════════════════════
def build_missing_report(all_series, existing_tg, uploaded):
    """
    يبني تقريراً كاملاً بالحلقات الناقصة لكل مسلسل.
    يرتب الناقص تصاعدياً (1، 2، 3...) لضمان الإكمال بالترتيب.
    """
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "series": [],
    }

    for series in all_series:
        name = series.get("name", "")
        src = series.get("_src", "?")
        eps = series.get("episodes", [])

        if not eps:
            continue

        available_nums = set()
        for e in eps:
            n = e.get("num")
            if n:
                available_nums.add(int(n))

        # الموجود محلياً أو على TG
        present_nums = set()
        for n in available_nums:
            local_key = f"{name}|ep{n}"
            if local_key in uploaded:
                present_nums.add(n)
            elif existing_tg and is_episode_uploaded(existing_tg, name, n):
                present_nums.add(n)

        missing = sorted(available_nums - present_nums)

        if missing:
            report["series"].append({
                "name": name,
                "source": src,
                "total_available": len(available_nums),
                "present": len(present_nums),
                "missing_count": len(missing),
                "missing_episodes": missing,
            })

    # ترتيب حسب عدد الناقص (الأكثر أولاً)
    report["series"].sort(key=lambda x: -x["missing_count"])
    return report


def print_missing_report(report):
    """طباعة تقرير واضح عن الحلقات الناقصة."""
    if not report.get("series"):
        print("\n   ✨ لا توجد حلقات ناقصة — كل المتاح مرفوع", flush=True)
        return

    total_missing = sum(s["missing_count"] for s in report["series"])
    print(f"\n{'═' * 60}", flush=True)
    print(f"📋 تقرير الحلقات الناقصة ({len(report['series'])} مسلسل | {total_missing} حلقة)",
          flush=True)
    print(f"{'═' * 60}", flush=True)

    for s in report["series"][:20]:  # أول 20
        miss_preview = s["missing_episodes"][:10]
        miss_str = ", ".join(str(x) for x in miss_preview)
        if len(s["missing_episodes"]) > 10:
            miss_str += "..."
        print(f"   📺 [{s['source']}] {s['name']}", flush=True)
        print(f"      ✅ موجود: {s['present']}/{s['total_available']} | "
              f"❌ ناقص: {s['missing_count']}", flush=True)
        print(f"      🔢 الأرقام الناقصة: {miss_str}", flush=True)

    if len(report["series"]) > 20:
        print(f"\n   ... و{len(report['series']) - 20} مسلسل آخر", flush=True)


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
# الخطوة 3: تنزيل + ضغط + رفع (بالترتيب)
# ═══════════════════════════════════════════════════════════════
async def _process_series(series, existing_tg, uploaded, missing_report):
    name = series.get("name", "")
    src = series.get("_src", "?")
    poster = series.get("poster", "")
    genre = series.get("genre", "")

    # جمع كل الحلقات المتاحة
    all_eps_by_num = {}
    for e in series.get("episodes", []):
        n = e.get("num")
        if n:
            all_eps_by_num[int(n)] = e

    all_eps_sorted = [all_eps_by_num[k] for k in sorted(all_eps_by_num.keys())]

    # تحديد الحلقات التي تحتاج تنزيلاً
    to_download = []
    for n in sorted(all_eps_by_num.keys()):
        local_key = f"{name}|ep{n}"

        # موجود محلياً؟
        if local_key in uploaded:
            continue

        # موجود على TG؟
        if existing_tg and is_episode_uploaded(existing_tg, name, n):
            uploaded[local_key] = {
                "url": all_eps_by_num[n].get("url", ""),
                "done": True,
                "source": "telegram",
                "at": datetime.now(timezone.utc).isoformat(),
            }
            continue

        # ناقص — أضف للتنزيل
        to_download.append(all_eps_by_num[n])

    # ★ ترتيب تصاعدي لضمان الإكمال بالترتيب
    to_download.sort(key=lambda e: int(e.get("num", 0)))

    if not to_download:
        return {
            "name": name,
            "poster": poster,
            "genre": genre,
            "source": src,
            "episodes": [
                {"num": e.get("num"),
                 "url": uploaded.get(f"{name}|ep{e.get('num')}", {}).get(
                     "url", e.get("url", ""))}
                for e in all_eps_sorted
            ],
            "episodes_count": len(all_eps_sorted),
        }

    print(f"\n{'═' * 60}", flush=True)
    print(f"📺 [{src}] {name}", flush=True)
    print(f"   🆕 {len(to_download)}/{len(all_eps_sorted)} حلقة للتنزيل", flush=True)
    print(f"   🔢 الأرقام: {[e.get('num') for e in to_download[:10]]}"
          f"{'...' if len(to_download) > 10 else ''}", flush=True)
    print(f"{'═' * 60}", flush=True)

    ok_count = 0

    for ep in to_download:
        n = ep.get("num", 0)
        u = ep.get("url", "")
        if not u:
            continue

        print(f"\n   ── الحلقة {n} ──", flush=True)

        try:
            r = download_episode(name, n, u)
        except Exception as e:
            print(f"   ❌ تنزيل: {str(e)[:120]}", flush=True)
            continue

        if not r or not Path(r).exists():
            print(f"   ⚠️ فشل التنزيل", flush=True)
            continue

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
        "episodes": [
            {"num": e.get("num"),
             "url": uploaded.get(f"{name}|ep{e.get('num')}", {}).get(
                 "url", e.get("url", ""))}
            for e in all_eps_sorted
        ],
        "episodes_count": len(all_eps_sorted),
        "is_new": ok_count > 0,
    }


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════
async def _main_async():
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 10 + "شوف — Shoof Automation v3" + " " * 18 + "║")
    print("╚" + "═" * 58 + "╝", flush=True)

    try:
        config.validate()
        print("✅ الإعدادات صحيحة\n", flush=True)
    except Exception as e:
        print(f"❌ {e}")
        return 1

    # ─── 1) فحص Telegram ───
    existing_tg = await _scan_telegram()

    # ─── تحميل الحالة ───
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

    # ─── ★ بناء تقرير الناقص ───
    print("\n" + "═" * 60, flush=True)
    print("📋 [الخطوة 2.5] تحليل الحلقات الناقصة...", flush=True)
    print("═" * 60, flush=True)

    missing_report = build_missing_report(all_series, existing_tg, uploaded)
    print_missing_report(missing_report)
    _save(MISSING_REPORT, missing_report)

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
                item = await _process_series(series, existing_tg, uploaded, missing_report)
                if item:
                    out_series.append(item)
            except Exception as e:
                print(f"   ❌ [{series.get('name','?')}]: {str(e)[:150]}", flush=True)
                continue

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