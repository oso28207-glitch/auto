"""
بناء تدريجي: يحدّث فقط ما تغيّر.
- عند إضافة حلقة جديدة → يحدّث قائمة الحلقات فقط.
- عند إكمال مسلسل → يولّد صفحته.
- لا يعيد بناء الموقع بالكامل.
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

from config import DOCS_DIR, DATA_DIR, config
from database import db
from errors import BuildError


def _ensure_docs():
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "videos.json").parent.mkdir(parents=True, exist_ok=True)


def _write_videos_json():
    """يكتب قائمة الفيديوهات المحدّثة."""
    videos = db.all_videos()
    out = DOCS_DIR / "videos.json"
    out.write_text(json.dumps(videos, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _write_series_page(series_name: str):
    """يولّد صفحة HTML مبسّطة للمسلسل."""
    series = db.get_series(series_name)
    eps = series.get("episodes", {})
    eps_sorted = sorted(eps.items(), key=lambda x: int(x[0]))

    rows = []
    for num, info in eps_sorted:
        if info.get("status") != "uploaded":
            continue
        mid = info.get("message_id", "")
        rows.append(
            f'<li><a href="/stream/{mid}" data-id="{mid}">'
            f'الحلقة {num}</a></li>'
        )

    html = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<title>{series_name}</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<h1>{series_name}</h1>
<ul class="episodes">
{''.join(rows) or '<li>لا توجد حلقات بعد</li>'}
</ul>
<script src="config.js"></script>
<script src="app.js"></script>
</body>
</html>"""
    safe = series_name.replace(" ", "_")
    path = DOCS_DIR / f"series_{safe}.html"
    path.write_text(html, encoding="utf-8")
    return path


def build_incremental(series_name: str = None):
    """
    البناء التدريجي:
    - إذا series_name محدد → يحدّث صفحة المسلسل + videos.json.
    - إذا غير محدد → يحدّث videos.json فقط.
    """
    try:
        _ensure_docs()
        _write_videos_json()

        if series_name:
            _write_series_page(series_name)
            print(f"[Builder] تم تحديث صفحة: {series_name}")

        # تحديث index.html إذا لم يوجد
        index = DOCS_DIR / "index.html"
        if not index.exists():
            _write_index()
            print("[Builder] تم إنشاء index.html")

    except Exception as e:
        raise BuildError(f"فشل البناء: {e}", e)


def _write_index():
    """ينشئ index.html أول مرة فقط."""
    series_list = db.all_series()
    items = "".join(
        f'<li><a href="series_{s.replace(" ", "_")}.html">{s}</a></li>'
        for s in series_list
    )
    html = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<title>مكتبة المسلسلات</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<header><h1>🎬 مكتبة المسلسلات</h1></header>
<main><ul class="series-list">{items}</ul></main>
<script src="config.js"></script>
<script src="app.js"></script>
</body>
</html>"""
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")