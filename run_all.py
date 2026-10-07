#!/usr/bin/env python3
"""
run_all.py — Shoof Automation v3.2

★ الميزات:
    1) ترتيب الأولوية: مسلسلات تركية مدبلجة → مسلسلات مدبلجة أخرى → أفلام مدبلجة → الباقي
    2) عند الخطأ الحرج: يتوقف فوراً (exit 1)
    3) تحميل الحلقات بالترتيب (1، 2، 3، ...)
    4) تسجيل الحلقات بدون URL في skipped_episodes.json
    5) استئناف من آخر حلقة على Telegram
    6) بناء out_series لكل المسلسلات أولاً (حتى لو لم تُحمَّل بعد)
    7) خيار --clean لتنظيف كل شيء
"""
import argparse
import asyncio
import os
import sys
import json
import shutil
import time
import traceback
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
SKIPPED_LOG = DATA_DIR / "skipped_episodes.json"

SOURCE_ORDER = {"u3seq": 0, "yam": 1, "egybest": 2}


# ═══════════════════════════════════════════════════════════════
# تنظيف شامل
# ═══════════════════════════════════════════════════════════════
def _clean_all():
    print("\n" + "═" * 60, flush=True)
    print("🧹 تنظيف شامل...", flush=True)
    print("═" * 60, flush=True)

    targets = [
        DATA_DIR / "media",
        DATA_DIR / "state.json",
        DATA_DIR / "state.backup.json",
        DATA_DIR / "series.json",
        DATA_DIR / "uploaded.json",
        DATA_DIR / "missing_report.json",
        DATA_DIR / "skipped_episodes.json",
        config.DOCS_DIR / "watch",
        config.DOCS_DIR / "posters",
        config.DOCS_DIR / "index.html",
        config.DOCS_DIR / "series.json",
        config.DOCS_DIR / "videos.json",
    ]

    for t in targets:
        try:
            if t.exists():
                if t.is_dir():
                    shutil.rmtree(t)
                    print(f"   🗑️  حذف مجلد: {t.name}", flush=True)
                else:
                    t.unlink()
                    print(f"   🗑️  حذف ملف: {t.name}", flush=True)
        except Exception as e:
            print(f"   ⚠️  فشل حذف {t}: {str(e)[:80]}", flush=True)

    for d in (MEDIA_DIR, config.DOCS_DIR / "watch", config.DOCS_DIR / "posters"):
        d.mkdir(parents=True, exist_ok=True)

    print("   ✅ تم التنظيف\n", flush=True)


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

    existing = await fetch_existing_episodes()
    stats = tg_stats(existing)
    print(f"   📊 Telegram: {stats['series']} مسلسل | "
          f"{stats['episodes']} حلقة", flush=True)
    return existing


# ═══════════════════════════════════════════════════════════════
# الخطوة 2: زحف + ترتيب بالأولوية
# ═══════════════════════════════════════════════════════════════
def _scrape_and_sort():
    print("\n" + "═" * 60, flush=True)
    print("📡 [الخطوة 2] فحص المصادر + ترتيب بالأولوية...", flush=True)
    print("═" * 60, flush=True)

    all_series = []
    for src in ["u3seq", "yam", "egybest"]:
        print(f"\n   [{src}]", flush=True)
        items = _fetch(src)
        for s in items:
            s["_src"] = src
        print(f"      ✅ {len(items)} مسلسل", flush=True)
        all_series.extend(items)

    def sort_key(s):
        name = s.get("name", "")
        return (config.priority_of(name),
                SOURCE_ORDER.get(s.get("_src", "z"), 9),
                name)

    all_series.sort(key=sort_key)

    prio_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for s in all_series:
        prio_counts[config.priority_of(s.get("name", ""))] += 1

    print(f"\n📊 إجمالي: {len(all_series)} مسلسل", flush=True)
    print(f"   1️⃣ مسلسلات تركية مدبلجة: {prio_counts[1]}", flush=True)
    print(f"   2️⃣ مسلسلات مدبلجة أخرى:  {prio_counts[2]}", flush=True)
    print(f"   3️⃣ أفلام مدبلجة:          {prio_counts[3]}", flush=True)
    print(f"   4️⃣ مسلسلات عادية:         {prio_counts[4]}", flush=True)
    print(f"   5️⃣ أفلام عادية:            {prio_counts[5]}", flush=True)
    print(flush=True)

    for prio in [1, 2]:
        top = [s for s in all_series
               if config.priority_of(s.get("name", "")) == prio][:5]
        if top:
            print(f"   🔝 أولوية {prio} (أول 5):", flush=True)
            for s in top:
                print(f"      · {s.get('name')}", flush=True)
            print(flush=True)

    return all_series


# ═══════════════════════════════════════════════════════════════
# فلترة الحلقات + تسجيل المُتخطاة
# ═══════════════════════════════════════════════════════════════
def _filter_new_episodes(series, existing_tg, uploaded, skipped_log):
    name = series.get("name", "")
    eps = series.get("episodes", [])
    new_eps = []
    newly_marked = []

    for e in eps:
        n = e.get("num", 0)
        if not n:
            continue
        local_key = f"{name}|ep{n}"

        if local_key in uploaded:
            continue

        if existing_tg and is_episode_uploaded(existing_tg, name, n):
            uploaded[local_key] = {
                "url": e.get("url", ""),
                "done": True,
                "source": "telegram",
            }
            newly_marked.append(local_key)
            continue

        # ★ تسجيل الحلقات بدون URL
        if not e.get("url"):
            skipped_log.setdefault(name, []).append({
                "episode": n,
                "reason": "no_url",
            })
            continue

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
# معالجة مسلسل واحد
# ═══════════════════════════════════════════════════════════════
async def _process_series(series, existing_tg, uploaded, skipped_log):
    name = series.get("name", "")
    src = series.get("_src", "?")
    poster = series.get("poster", "")
    genre = series.get("genre", "")

    new_eps, all_eps, newly_marked = _filter_new_episodes(
        series, existing_tg, uploaded, skipped_log
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

    # ★ ترتيب الحلقات تصاعدياً (1، 2، 3، ...)
    new_eps.sort(key=lambda e: int(e.get("num", 0)))

    nums = [e.get("num") for e in new_eps]
    print(f"\n{'═' * 60}", flush=True)
    print(f"📺 [{src}] {name}", flush=True)
    print(f"   🆕 {len(new_eps)}/{len(all_eps)} حلقة جديدة", flush=True)
    print(f"   🔢 الأرقام: {nums[:20]}{'...' if len(nums) > 20 else ''}",
          flush=True)
    print(f"{'═' * 60}", flush=True)

    # اطبع الحلقات المُتخطاة (بدون URL)
    if name in skipped_log and skipped_log[name]:
        skipped_nums = [s["episode"] for s in skipped_log[name]]
        print(f"   ⚠️  حلقات بدون URL: {skipped_nums}", flush=True)

    ok_count = 0

    for ep in new_eps:
        n = ep.get("num", 0)
        u = ep.get("url", "")

        print(f"\n   ── الحلقة {n} ──", flush=True)

        # ★ عند الفشل: ارفع استثناء لإيقاف السكربت
        r = download_episode(name, n, u)
        if not r or not Path(r).exists():
            raise RuntimeError(f"فشل تحميل {name} حلقة {n}")

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
async def _main_async(clean=False):
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 9 + "شوف — Shoof Automation v3.2" + " " * 17 + "║")
    print("╚" + "═" * 58 + "╝", flush=True)

    try:
        config.validate()
        print("✅ الإعدادات صحيحة", flush=True)
    except Exception as e:
        print(f"❌ {e}")
        return 1

    if clean:
        _clean_all()

    # ─── 1) فحص Telegram ───
    existing_tg = await _scan_telegram()

    # ─── تحميل الحالة ───
    state = _load(STATE, {"series": []})
    uploaded = _load(UPLOADED, {})
    skipped_log = _load(SKIPPED_LOG, {})

    print(
        f"\n💾 محلي: {len(state.get('series', []))} مسلسل | "
        f"📤 {len(uploaded)} حلقة مُسجّلة",
        flush=True,
    )

    # ─── 2) زحف + ترتيب ───
    all_series = _scrape_and_sort()
    if not all_series:
        print("⚠️ لا مسلسلات", flush=True)
        return 0

    # ★★★ بناء out_series لكل المسلسلات أولاً
    # حتى لو لم تُحمَّل بعد، تُحفظ في series.json ليبنيها الموقع
    out_series = []
    for series in all_series:
        name = series.get("name", "")
        eps = series.get("episodes", [])
        all_eps = [
            {"num": e.get("num"),
             "url": uploaded.get(f"{name}|ep{e.get('num')}", {}).get(
                 "url", e.get("url", ""))}
            for e in eps
        ]
        out_series.append({
            "name": name,
            "poster": series.get("poster", ""),
            "genre": series.get("genre", ""),
            "source": series.get("_src", "?"),
            "episodes": all_eps,
            "episodes_count": len(all_eps),
        })

    # حفظ مبدئي
    _save(STATE, {
        "series": out_series,
        "last_update": datetime.now(timezone.utc).isoformat(),
    })

    # ─── 3) معالجة ───
    print("\n" + "═" * 60, flush=True)
    print("⬇️  [الخطوة 3] تنزيل + ضغط + رفع بالترتيب...", flush=True)
    print("═" * 60, flush=True)

    await uploader.start()

    start = time.time()
    max_sec = config.MAX_RUNTIME_SECONDS
    ok_total = 0
    series_index = {s["name"]: i for i, s in enumerate(out_series)}

    try:
        for series in all_series:
            if time.time() - start > max_sec:
                print(f"\n⏰ انتهى وقت التشغيل — توقف آمن", flush=True)
                break

            name = series.get("name", "")

            try:
                item = await _process_series(
                    series, existing_tg, uploaded, skipped_log
                )
                if item:
                    idx = series_index.get(name)
                    if idx is not None:
                        out_series[idx] = item
                    if item.get("is_new"):
                        ok_total += 1
            except Exception as e:
                print(f"\n❌ خطأ في [{name}]: {str(e)[:200]}", flush=True)
                traceback.print_exc()

                _save(STATE, {
                    "series": out_series,
                    "last_update": datetime.now(timezone.utc).isoformat(),
                })
                _save(UPLOADED, uploaded)
                _save(SKIPPED_LOG, skipped_log)
                raise

            # حفظ تدريجي
            _save(STATE, {
                "series": out_series,
                "last_update": datetime.now(timezone.utc).isoformat(),
            })
            _save(UPLOADED, uploaded)
            _save(SKIPPED_LOG, skipped_log)

    finally:
        try:
            await uploader.stop()
        except Exception:
            pass

    # حفظ نهائي
    _save(STATE, {
        "series": out_series,
        "last_update": datetime.now(timezone.utc).isoformat(),
    })
    _save(UPLOADED, uploaded)
    _save(SKIPPED_LOG, skipped_log)

    total_skipped = sum(len(v) for v in skipped_log.values())
    if total_skipped > 0:
        print(f"\n⚠️  إجمالي الحلقات بدون URL: {total_skipped} "
              f"({len(skipped_log)} مسلسل)", flush=True)
        for sname, items in list(skipped_log.items())[:5]:
            nums = [i["episode"] for i in items]
            print(f"   · {sname}: {nums}", flush=True)

    # بناء الموقع
    try:
        from build_site import build_site
        build_site()
    except Exception as e:
        print(f"❌ build_site: {e}", flush=True)
        raise

    elapsed = (time.time() - start) / 60
    print(f"\n{'═' * 60}")
    print(f"⏱️  انتهى في {elapsed:.1f}د | ✅ {ok_total} مسلسل جديد")
    print(f"{'═' * 60}", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true",
                        help="تنظيف كل شيء قبل البدء")
    args = parser.parse_args()

    try:
        return asyncio.run(_main_async(clean=args.clean))
    except Exception as e:
        print(f"\n❌ فشل حرج: {str(e)[:300]}", flush=True)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())