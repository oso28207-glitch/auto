#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""تحقق شامل من نظام التصنيفات (canonical_category) عبر كل الحالات."""
import json
from collections import Counter
from names import canonical_category, CATEGORY_PRIORITY, ALL_CATEGORIES

# حالات اختبارية تمثل كل قسم مطلوب
CASES = [
    # (name, category, source, expected)
    ("مسلسل تركي مدبلج", "", "u3seq", "مسلسلات تركية مدبلجة"),
    ("حكاية جزيرة", "moslslat-turkiaa-modblga", "yam", "مسلسلات تركية مدبلجة"),
    ("مسلسل هندي مدبلج", "moslslat-hndia-modblja", "yam", "مسلسلات هندية مدبلجة"),
    ("Indian Series", "indian-series", "yam", "مسلسلات مترجمة"),
    ("أنمي مدبلج", "moslslat-cartoon", "yam", "مسلسلات أنمي مدبلجة"),
    ("مسلسل كوري مدبلج", "moslslat-modblga", "yam", "مسلسلات كورية مدبلجة"),
    ("مسلسل مترجم", "moslslat-turkiaa-motrgma", "yam", "مسلسلات مترجمة"),
    ("مسلسل عربي", "1moslslat-arbyaa", "yam", "مسلسلات عربية"),
    ("مسلسل مصري", "moslslat-msriya", "yam", "مسلسلات عربية"),
    ("مسلسل خليجي", "moslslat-", "yam", "مسلسلات عربية"),
    ("Foreign Show", "mosalslat-english", "yam", "مسلسلات أجنبية"),
    ("مسلسل أسيوي", "moslslat-asia", "yam", "مسلسلات أسيوية"),
    ("فيلم مدبلج", "aflam-dub", "yam", "أفلام مدبلجة"),
    ("فيلم عربي", "aflam-arabya1", "yam", "أفلام"),
    # yam تصنيف فارغ — نص عربي → عربية
    ("مسلسل عربي جديد", "", "yam", "مسلسلات عربية"),
    # yam تصنيف فارغ — نص لاتيني → أجنبية
    ("New Show", "", "yam", "مسلسلات أجنبية"),
    # التصنيف العام محايد (لا يجب أن يصير "أفلام")
    ("فتاة النافذة الموسم 3", "مسلسلات وأفلام", "yam", "مسلسلات عربية"),
    ("حد اقصى الموسم 1", "مسلسلات وأفلام", "yam", "مسلسلات عربية"),
]

print("=" * 70)
print("اختبار canonical_category — كل الحالات")
print("=" * 70)
ok = 0
fail = 0
for name, cat, src, exp in CASES:
    got = canonical_category(name=name, category=cat, source=src)
    status = "✅" if got == exp else "❌"
    if got == exp:
        ok += 1
    else:
        fail += 1
    print(f"{status} [{src:6}] '{name[:28]:28}' cat='{cat[:26]:26}' -> {got}")

print(f"\nالنتيجة: {ok} ناجح / {fail} فاشل")

# تحقق أن كل تصنيف ناتج ضمن القائمة المعتمدة
print("\n" + "=" * 70)
print("تحقق أن كل تصنيف ناتج معتمد في ALL_CATEGORIES + له أولوية")
print("=" * 70)
all_ok = True
for cat in ALL_CATEGORIES:
    if cat not in CATEGORY_PRIORITY:
        print(f"❌ {cat} بدون أولوية")
        all_ok = False
print(f"{'✅ كل التصنيفات لها أولوية' if all_ok else '❌ مشكلة في الأولويات'}")

# إحصاء التصنيفات في البيانات المخزّنة
print("\n" + "=" * 70)
print("توزيع التصنيفات في data/series.json + data/tg_series.json")
print("=" * 70)
for fn in ("data/series.json", "data/tg_series.json"):
    try:
        with open(fn, encoding="utf-8") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else data.get("series", list(data.values()) if isinstance(data, dict) else [])
        cnt = Counter()
        for it in items:
            if isinstance(it, dict):
                cnt[it.get("category", "") or "(فارغ)"] += 1
        print(f"\n{fn}: {len(items)} عنصر")
        for k, v in cnt.most_common():
            print(f"   {v:5}  {k}")
    except Exception as e:
        print(f"{fn}: خطأ {e}")
