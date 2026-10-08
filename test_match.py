"""test_match.py — اختبار مطابقة أسماء Telegram مع المصادر (poster/category)."""
import json
import sys
import time

from run_all import _fetch, _find_source_match

TG = "data/tg_series.json"


def main():
    tg = json.load(open(TG, encoding="utf-8"))["series"]
    print(f"TG series: {len(tg)}")

    all_series = []
    for src in ["u3seq", "yam"]:
        t = time.time()
        items = _fetch(src)
        for s in items:
            s["_src"] = src
        print(f"  [{src}] {len(items)} items in {time.time()-t:.1f}s")
        all_series.extend(items)

    print(f"\nTotal source items: {len(all_series)}")

    matched = 0
    with_poster = 0
    for s in tg:
        name = s["name"]
        m = _find_source_match(name, all_series)
        if m:
            matched += 1
            p = (m.get("poster") or "").strip()
            if p:
                with_poster += 1
            print(f"  ✅ {name!r} -> {m.get('name')!r} [{m.get('_src')}] "
                  f"poster={'YES' if p else 'no'} cat={m.get('category','')!r}")
        else:
            print(f"  ❌ {name!r} -> NO MATCH")

    print(f"\nMatched: {matched}/{len(tg)} | with poster: {with_poster}")


if __name__ == "__main__":
    main()
