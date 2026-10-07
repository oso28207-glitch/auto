"""
build.py — بناء موقع نتفليكس-ستايل
- تحميل الصور محلياً (يحل مشكلة عدم الظهور)
- مشغل HLS.js مع autoplay
- عداد تنازلي 10s للانتقال التلقائي للحلقة التالية
- تصنيفات ديناميكية
"""

import os, json, shutil, hashlib, re
from pathlib import Path
from urllib.parse import quote

from curl_cffi import requests as cffi_requests

from config import config, DATA_DIR, DOCS_DIR

POSTERS_DIR = DOCS_DIR / "posters"
POSTERS_DIR.mkdir(parents=True, exist_ok=True)


def _slug(name):
    """slug من اسم عربي — نستخدم hash لتفادي المشاكل."""
    h = hashlib.md5(name.encode('utf-8')).hexdigest()[:12]
    return h


def download_poster(url, series_name):
    """تحميل البوستر محلياً — يحل مشكلة عدم ظهور الصور."""
    if not url or not url.startswith("http"):
        return None

    ext = "jpg"
    low = url.lower()
    for e in (".png", ".jpeg", ".jpg", ".webp", ".gif"):
        if e in low:
            ext = e.lstrip(".")
            break

    slug = _slug(series_name)
    out = POSTERS_DIR / f"{slug}.{ext}"
    if out.exists() and out.stat().st_size > 1024:
        return f"posters/{out.name}"

    try:
        r = cffi_requests.get(url, impersonate="chrome120", timeout=30, verify=False)
        if r.status_code == 200 and len(r.content) > 1024:
            with open(out, "wb") as f:
                f.write(r.content)
            return f"posters/{out.name}"
    except Exception as e:
        print(f"      ⚠️ فشل تحميل صورة {series_name}: {str(e)[:80]}")
    return None


def build_site():
    """بناء الموقع الكامل."""
    data_file = DATA_DIR / "series.json"
    if not data_file.exists():
        print("⚠️ لا يوجد series.json")
        return

    with open(data_file, encoding="utf-8") as f:
        data = json.load(f)

    series_list = data.get("series", [])

    # تحميل الصور محلياً
    print(f"🖼️  تحميل {len(series_list)} صورة...")
    for s in series_list:
        s["local_poster"] = download_poster(s.get("poster", ""), s.get("name", ""))

    # تصنيفات
    categories = {
        "أحدث المسلسلات": sorted(series_list,
                                  key=lambda x: x.get("added_at", ""),
                                  reverse=True)[:30],
        "الأكثر مشاهدة": sorted(series_list,
                                key=lambda x: x.get("views", 0),
                                reverse=True)[:30],
        "تركية": [s for s in series_list if "تركي" in s.get("genre", "")],
        "مكسيكية": [s for s in series_list if "مكسيك" in s.get("genre", "")],
        "هندية": [s for s in series_list if "هند" in s.get("genre", "")],
        "جميع المسلسلات": series_list,
    }

    # بناء HTML
    html = _build_html(series_list, categories)

    # احفظ JSON للـ JS
    with open(DOCS_DIR / "series.json", "w", encoding="utf-8") as f:
        json.dump({
            "series": series_list,
            "categories": {k: [s["name"] for s in v] for k, v in categories.items()},
        }, f, ensure_ascii=False, indent=2)

    with open(DOCS_DIR / "index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ تم بناء الموقع: {len(series_list)} مسلسل")


def _build_html(series_list, categories):
    """HTML مع نتفليكس-ستايل + HLS.js player."""

    cats_html = ""
    for cat_name, items in categories.items():
        if not items:
            continue
        cards = ""
        for s in items:
            poster = s.get("local_poster") or "posters/placeholder.jpg"
            name = s.get("name", "")
            slug = _slug(name)
            cards += f"""
            <a class="card" href="#watch-{slug}" data-series="{quote(name)}">
                <div class="poster-wrap">
                    <img src="{poster}" alt="{name}" loading="lazy"
                         onerror="this.src='posters/placeholder.jpg'">
                    <div class="play-overlay">▶</div>
                </div>
                <div class="card-title">{name}</div>
            </a>"""

        cats_html += f"""
        <section class="row">
            <h2>{cat_name}</h2>
            <div class="carousel">{cards}</div>
        </section>"""

    html = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Shoof — شاهد أحدث المسلسلات</title>
<link rel="stylesheet" href="style.css">
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.15/dist/hls.min.js"></script>
</head>
<body>

<header class="topbar">
    <div class="brand">SHOOF</div>
    <nav>
        <a href="#">الرئيسية</a>
        <a href="#series">المسلسلات</a>
        <a href="#movies">أفلام</a>
        <a href="#my-list">قائمتي</a>
    </nav>
    <div class="search">
        <input type="search" id="search-input" placeholder="ابحث...">
    </div>
</header>

<main>
    {cats_html}
</main>

<div id="player-modal" class="modal hidden">
    <div class="modal-content">
        <button class="close-btn" onclick="closePlayer()">✕</button>
        <video id="video-player" controls playsinline></video>
        <div id="autoplay-countdown" class="countdown hidden">
            الحلقة التالية بعد <span id="countdown-num">10</span> ثوان
            <button onclick="playNext()">تشغيل الآن</button>
        </div>
        <div class="episode-info">
            <h3 id="player-title"></h3>
            <p id="player-subtitle"></p>
        </div>
        <div class="episodes-grid" id="episodes-grid"></div>
    </div>
</div>

<script src="app.js"></script>
</body>
</html>"""
    return html


if __name__ == "__main__":
    build_site()