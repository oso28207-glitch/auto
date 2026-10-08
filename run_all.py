#!/usr/bin/env python3
"""
run_all.py — Shoof Automation v3.4

★ التغيير الرئيسي:
    1) فحص Telegram (existing_tg)
    2) ★ زحف المصادر أولاً (all_series) ← للصور والتصنيفات
    3) بناء tg_series.json مع مطابقة posters من all_series
    4) بناء الموقع (من Telegram)
    5) تنزيل + ضغط + رفع الجديد فقط
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

from config import config, DATA_DIR, MEDIA_DIR, DOCS_DIR
from downloader import download_episode
from telegram_checker import (
    fetch_existing_episodes,
    is_episode_uploaded,
    get_stats as tg_stats,
)
from uploader import uploader

STATE = DATA_DIR / "series.json"
UPLOADED = DATA_DIR / "uploaded.json"
SKIPPED_LOG = DATA_DIR / "skipped_episodes.json"
TG_SERIES = DATA_DIR / "tg_series.json"

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
        DATA_DIR / "tg_series.json",
        DOCS_DIR / "watch",
        DOCS_DIR / "posters",
        DOCS_DIR / "index.html",
        DOCS_DIR / "series.json",
        DOCS_DIR / "videos.json",
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

    for d in (MEDIA_DIR, DOCS_DIR / "watch", DOCS_DIR / "posters"):
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


def _norm(s):
    """تطبيع الاسم للمقارنة."""
    import re
    if not s:
        return ""
    n = s.strip().lower()
    n = re.sub(r"^مسلسل\s+", "", n)
    n = re.sub(r"\s+", " ", n)
    n = re.sub(r"[^\w\s\u0600-\u06FF]", "", n)
    return n.strip()


def _find_source_match(tg_name, all_series):
    """
    يبحث عن مصدر مطابق لاسم Telegram.
    يعيد dict المصدر أو None.
    """
    tg_norm = _norm(tg_name)
    if not tg_norm:
        return None

    # 1) مطابقة مباشرة
    for s in all_series:
        if _norm(s.get("name", "")) == tg_norm:
            return s

    # 2) مطابقة جزئية
    if len(tg_norm) >= 5:
        for s in all_series:
            s_norm = _norm(s.get("name", ""))
            if len(s_norm) >= 5:
                if s_norm in tg_norm or tg_norm in s_norm:
                    return s

    return None


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
# الخطوة 2: زحف المصادر (للحصول على posters + categories)
# ═══════════════════════════════════════════════════════════════
def _scrape_sources():
    print("\n" + "═" * 60, flush=True)
    print("📡 [الخطوة 2] زحف المصادر (لجلب الصور)...", flush=True)
    print("═" * 60, flush=True)

    all_series = []
    for src in ["u3seq", "yam", "egybest"]:
        print(f"\n   [{src}]", flush=True)
        items = _fetch(src)
        for s in items:
            s["_src"] = src
        print(f"      ✅ {len(items)} مسلسل", flush=True)
        all_series.extend(items)

    return all_series


# ═══════════════════════════════════════════════════════════════
# الخطوة 3: بناء tg_series.json مع posters من المصادر
# ═══════════════════════════════════════════════════════════════
def _build_tg_series(existing_tg, all_series=None):
    """
    يبني tg_series.json من Telegram.
    إذا مررت all_series، نطابق الـ posters من المصادر.
    """
    if all_series is None:
        all_series = []

    series_list = []
    channel_id = config.CHANNEL_ID.lstrip('@')
    matched = 0

    for norm_name, entry in existing_tg.items():
        if isinstance(entry, dict):
            display_name = entry.get("display_name", norm_name)
            episodes = sorted(entry.get("episodes", set()))
            message_ids = entry.get("message_ids", {})
        else:
            display_name = norm_name
            episodes = sorted(entry)
            message_ids = {}

        # ★ مطابقة المصدر لجلب poster
        poster = ""
        source_category = ""
        src_match = _find_source_match(display_name, all_series)
        if src_match:
            poster = src_match.get("poster", "") or ""
            source_category = src_match.get("category", "") or ""
            if poster:
                matched += 1

        eps_list = []
        for ep in episodes:
            mid = message_ids.get(ep, 0) if isinstance(message_ids, dict) else 0
            eps_list.append({
                "num": ep,
                "url": f"https://t.me/{channel_id}/{mid}" if mid else "",
            })

        # ★ name_enhanced للتصنيف الأدق
        # إذا كان المصدر تركي مدبلج → نضيف التصنيف للاسم
        enhanced_name = display_name
        if source_category and "تركي" in source_category and "مدبلج" in source_category:
            if "تركي" not in enhanced_name:
                enhanced_name = f"{display_name} (تركي مدبلج)"

        series_list.append({
            "name": display_name,
            "display_name": display_name,
            "enhanced_name": enhanced_name,
            "poster": poster,
            "category": source_category,
            "genre": "",
            "source": "telegram",
            "episodes": eps_list,
            "episodes_count": len(eps_list),
        })

    _save(TG_SERIES, {
        "series": series_list,
        "built_at": datetime.now(timezone.utc).isoformat(),
    })
    print(f"   ✅ tg_series.json: {len(series_list)} مسلسل "
          f"({matched} مع صور)", flush=True)
    return series_list


# ═══════════════════════════════════════════════════════════════
# الخطوة 4: بناء الموقع
# ═══════════════════════════════════════════════════════════════
def _build_site_from_tg():
    print("\n" + "═" * 60, flush=True)
    print("🏗️  [الخطوة 4] بناء الموقع من بيانات Telegram...", flush=True)
    print("═" * 60, flush=True)

    try:
        from build_site import build_site
        build_site()
        print("   ✅ تم بناء الموقع", flush=True)
    except Exception as e:
        print(f"   ❌ فشل build_site: {e}", flush=True)
        raise


# ═══════════════════════════════════════════════════════════════
# الخطوة 5: ترتيب للتحميل
# ═══════════════════════════════════════════════════════════════
def _sort_for_download(all_series):
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
# فلترة الحلقات
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
# معالجة مسلسل
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

    new_eps.sort(key=lambda e: int(e.get("num", 0)))
    nums = [e.get("num") for e in new_eps]

    print(f"\n{'═' * 60}", flush=True)
    print(f"📺 [{src}] {name}", flush=True)
    print(f"   🆕 {len(new_eps)}/{len(all_eps)} حلقة جديدة", flush=True)
    print(f"   🔢 الأرقام: {nums[:20]}{'...' if len(nums) > 20 else ''}",
          flush=True)
    print(f"{'═' * 60}", flush=True)

    if name in skipped_log and skipped_log[name]:
        skipped_nums = [s["episode"] for s in skipped_log[name]]
        print(f"   ⚠️  حلقات بدون URL: {skipped_nums}", flush=True)

    ok_count = 0

    for ep in new_eps:
        n = ep.get("num", 0)
        u = ep.get("url", "")

        print(f"\n   ── الحلقة {n} ──", flush=True)

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
    print("║" + " " * 9 + "شوف — Shoof Automation v3.4" + " " * 17 + "║")
    print("╚" + "═" * 58 + "╝", flush=True)

    try:
        config.validate()
        print("✅ الإعدادات صحيحة", flush=True)
    except Exception as e:
        print(f"❌ {e}")
        return 1

    if clean:
        _clean_all()

    # ═══ 1) فحص Telegram ═══
    existing_tg = await _scan_telegram()

    # ═══ 2) زحف المصادر (للصور) ═══
    all_series = _scrape_sources()
    if not all_series:
        print("⚠️ لا مسلسلات من المصادر — الموقع سيبني بدون صور", flush=True)

    # ═══ 3) بناء tg_series.json مع الصور ═══
    print("\n" + "═" * 60, flush=True)
    print("🏗️  [الخطوة 3] بناء tg_series.json...", flush=True)
    print("═" * 60, flush=True)
    _build_tg_series(existing_tg, all_series)

    # ═══ 4) بناء الموقع ═══
    _build_site_from_tg()

    # ═══ 5) ترتيب للتحميل ═══
    if all_series:
        all_series = _sort_for_download(all_series)

    # ═══ تحميل الحالة ═══
    state = _load(STATE, {"series": []})
    uploaded = _load(UPLOADED, {})
    skipped_log = _load(SKIPPED_LOG, {})

    print(
        f"\n💾 محلي: {len(state.get('series', []))} مسلسل | "
        f"📤 {len(uploaded)} حلقة مُسجّلة",
        flush=True,
    )

    if not all_series:
        print("\n⚠️ لا مصادر — توقف بعد بناء الموقع", flush=True)
        return 0

    # بناء out_series للمصادر
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

    _save(STATE, {
        "series": out_series,
        "last_update": datetime.now(timezone.utc).isoformat(),
    })

    # ═══ 6) تنزيل + رفع ═══
    print("\n" + "═" * 60, flush=True)
    print("⬇️  [الخطوة 6] تنزيل + ضغط + رفع بالترتيب...", flush=True)
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