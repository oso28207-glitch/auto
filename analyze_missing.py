#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""تحليل الفروقات بين المصادر والموقع/تيليجرام."""
import json
from names import norm, base_norm


def load(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception as e:
        print("ERR", p, e)
        return {}


state = load("data/series.json")          # كل المسلسلات من المصادر + حلقاتها
tg = load("data/tg_series.json")          # ما فُحص على تيليجرام
uploaded = load("data/uploaded.json")     # الحلقات المرفوعة
docs = load("docs/series.json")           # الموقع

# خريطة المصادر: name -> set(episodes)
src = {}
for s in state.get("series", []):
    nm = s.get("name", "")
    eps = set()
    for e in s.get("episodes", []):
        try:
            eps.add(int(e.get("num")))
        except Exception:
            pass
    if nm and eps:
        src[nm] = eps

# خريطة تيليجرام: name -> set(episodes)
tgmap = {}
for s in tg.get("series", []):
    nm = s.get("name", "")
    eps = set()
    for e in s.get("episodes", []):
        try:
            eps.add(int(e.get("num")))
        except Exception:
            pass
    if nm:
        tgmap[nm] = eps

# uploaded: "name|epN" -> info
upmap = {}
for k, v in uploaded.items():
    if "|ep" in k:
        nm, ep = k.rsplit("|ep", 1)
        try:
            upmap.setdefault(nm, set()).add(int(ep))
        except Exception:
            pass

print("=" * 70)
print("ملخص عام")
print("=" * 70)
print(f"مسلسلات في المصادر (data/series.json): {len(src)}")
print(f"مسلسلات على تيليجرام (tg_series.json): {len(tgmap)}")
print(f"مسلسلات في uploaded.json: {len(upmap)}")
print(f"مسلسلات على الموقع (docs/series.json): {len(docs.get('series', []))}")
print()

# بناء فهارس تطبيع
src_by_base = {}
for nm in src:
    src_by_base.setdefault(base_norm(nm), nm)

print("=" * 70)
print("تحليل المسلسلات الموجودة على الموقع (50)")
print("=" * 70)
total_src = 0
total_tg = 0
total_missing = 0
rows = []
for s in docs.get("series", []):
    nm = s.get("name", "")
    # ابحث في المصادر
    match = None
    if nm in src:
        match = nm
    else:
        b = base_norm(nm)
        if b in src_by_base:
            match = src_by_base[b]
    src_eps = src.get(match, set()) if match else set()
    tg_eps = tgmap.get(nm, set())
    up_eps = upmap.get(nm, set())
    have = tg_eps | up_eps
    missing = sorted(src_eps - have)
    total_src += len(src_eps)
    total_tg += len(have)
    total_missing += len(missing)
    rows.append((nm, match, len(src_eps), len(have), len(missing), missing))

rows.sort(key=lambda r: -r[4])
for nm, match, ns, nh, nmiss, missing in rows:
    flag = "❌" if match is None else ("⚠️" if nmiss > 0 else "✅")
    m = f" (مصدر: {match})" if match and match != nm else ""
    print(f"{flag} {nm}{m}")
    print(f"     المصدر: {ns} | لدي: {nh} | ناقص: {nmiss}")
    if missing and len(missing) <= 40:
        print(f"     الناقص: {missing}")
    elif missing:
        print(f"     الناقص (أول 40): {missing[:40]} ...")

print()
print("=" * 70)
print(f"الإجمالي للمسلسلات المعروضة: مصدر={total_src} | لدي={total_tg} | ناقص={total_missing}")
print("=" * 70)
