"""normalize_categories.py — يوحّد التصنيفات المخزّنة في ملفات البيانات.

يُشغَّل مرة واحدة (أو ضمن CI) لتطبيع الـ slugs الخام القديمة إلى التصنيفات
العربية الموحّدة (names.canonical_category) دون فقدان أي بيانات أخرى.
"""
import json
import sys
from pathlib import Path

from names import canonical_category, CATEGORY_PRIORITY

DATA_DIR = Path(__file__).resolve().parent / "data"
FILES = ["series.json", "tg_series.json"]


def _iter_series(data):
    if isinstance(data, list):
        return data
    return data.get("series") or []


def normalize_file(path: Path):
    if not path.exists():
        print(f"   ⚠️  {path.name}: غير موجود")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"   ❌ {path.name}: {e}")
        return

    series = _iter_series(data)
    changed = 0
    for s in series:
        if not isinstance(s, dict):
            continue
        old = s.get("category", "")
        new = canonical_category(
            s.get("name", ""), old, s.get("source", ""))
        if new != old:
            s["category"] = new
            changed += 1

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")

    # إحصاء
    counts = {}
    for s in series:
        c = s.get("category", "")
        counts[c] = counts.get(c, 0) + 1
    print(f"   ✅ {path.name}: {len(series)} عنصر، تغيّر {changed}")
    for cat, prio in sorted(CATEGORY_PRIORITY.items(), key=lambda x: x[1]):
        if counts.get(cat):
            print(f"        {prio:>2}. {cat}: {counts[cat]}")


def main():
    print("🔧 تطبيع التصنيفات المخزّنة...")
    for fn in FILES:
        normalize_file(DATA_DIR / fn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
