#!/usr/bin/env python3
"""run_all.py — u3seq أولاً ثم yam ثم egybest."""
import os, sys, json, time, subprocess, shutil
from datetime import datetime
from pathlib import Path

from config import config, DATA_DIR, MEDIA_DIR
from downloader import download_episode

STATE = DATA_DIR / "series.json"
UPLOADED = DATA_DIR / "uploaded.json"


def _load(p, default):
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f: return json.load(f)
        except: pass
    return default


def _save(p, data):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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


def main():
    print("╔" + "═"*58 + "╗")
    print("║" + " "*12 + "شوف — Shoof Automation" + " "*22 + "║")
    print("╚" + "═"*58 + "╝\n")

    try:
        config.validate()
        print("✅ الإعدادات صحيحة", flush=True)
    except Exception as e:
        print(f"❌ {e}"); return 1

    state = _load(STATE, {"series": []})
    uploaded = _load(UPLOADED, {})
    print(f"💾 {len(state.get('series', []))} مسلسل | 📤 {len(uploaded)} حلقة\n", flush=True)

    # ★★★ u3seq أولاً
    all_series = []
    for src in ["u3seq", "yam", "egybest"]:
        print(f"\n📡 [{src}]", flush=True)
        items = _fetch(src)
        for s in items: s["_src"] = src
        print(f"   ✅ {len(items)} مسلسل", flush=True)
        all_series.extend(items)

    print(f"\n📊 إجمالي: {len(all_series)}\n", flush=True)
    if not all_series: return 0

    all_series.sort(key=lambda s: {"u3seq": 0, "yam": 1, "egybest": 2}.get(s.get("_src","z"), 9))

    start = time.time()
    max_sec = config.MAX_RUNTIME_SECONDS
    out_series = []

    for series in all_series:
        if time.time() - start > max_sec:
            print(f"\n⏰ انتهى الوقت", flush=True); break

        name = series.get("name", "")
        eps = series.get("episodes", [])
        src = series.get("_src", "?")
        if not eps: continue

        new_eps = [e for e in eps if f"{name}|ep{e.get('num',0)}" not in uploaded]

        if not new_eps:
            out_series.append({
                "name": name, "poster": series.get("poster", ""),
                "genre": series.get("genre", ""), "source": src,
                "episodes": [{"num": e.get("num"), "url": uploaded.get(f"{name}|ep{e.get('num')}", {}).get("url", e.get("url",""))} for e in eps],
                "episodes_count": len(eps),
            })
            continue

        print(f"\n{'═'*60}", flush=True)
        print(f"📺 [{src}] {name}", flush=True)
        print(f"   {len(new_eps)}/{len(eps)} حلقة جديدة | ⏱️ {(time.time()-start)/60:.1f}د", flush=True)
        print(f"{'═'*60}", flush=True)

        ok_count = 0
        for ep in new_eps:
            if time.time() - start > max_sec: break
            n = ep.get("num", 0)
            u = ep.get("url", "")
            if not u: continue
            print(f"\n   ── الحلقة {n} ──", flush=True)
            try:
                r = download_episode(name, n, u)
                if r and Path(r).exists():
                    uploaded[f"{name}|ep{n}"] = {"url": u, "done": True}
                    ok_count += 1
                    print(f"   ✅ تمت", flush=True)
                else:
                    print(f"   ⚠️ فشل", flush=True)
            except Exception as e:
                print(f"   ❌ {str(e)[:120]}", flush=True)

        out_series.append({
            "name": name, "poster": series.get("poster", ""),
            "genre": series.get("genre", ""), "source": src,
            "episodes": [{"num": e.get("num"), "url": uploaded.get(f"{name}|ep{e.get('num')}", {}).get("url", e.get("url",""))} for e in eps],
            "episodes_count": len(eps), "is_new": ok_count > 0,
        })

        _save(STATE, {"series": out_series, "last_update": datetime.utcnow().isoformat()})
        _save(UPLOADED, uploaded)

    _save(STATE, {"series": out_series, "last_update": datetime.utcnow().isoformat()})
    _save(UPLOADED, uploaded)

    try:
        from build_site import build_site
        build_site()
    except Exception as e:
        print(f"⚠️ build_site: {e}", flush=True)

    print(f"\n{'═'*60}\n⏱️ {(time.time()-start)/60:.1f}د | ✅ {len(out_series)}\n{'═'*60}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())