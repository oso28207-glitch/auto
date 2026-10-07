#!/usr/bin/env python3
"""
build_site.py — بناء موقع نتفليكس-ستايل كامل
- تحميل الصور محلياً (يحل مشكلة عدم الظهور)
- صفحات HTML لكل مسلسل
- مشغل HLS.js مع autoplay + انتقال تلقائي
- تصنيفات ديناميكية
"""
import os, json, re, hashlib, shutil
from pathlib import Path
from urllib.parse import quote

from curl_cffi import requests as cffi_requests

from config import config, DATA_DIR, DOCS_DIR

POSTERS_DIR = DOCS_DIR / "posters"
POSTERS_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════
# أدوات
# ═══════════════════════════════════════════════════════════════
def slugify(name: str) -> str:
    """slug آمن للأسماء العربية."""
    h = hashlib.md5(name.encode("utf-8")).hexdigest()[:10]
    safe = re.sub(r'[^\w\u0600-\u06FF]+', '-', name).strip('-')[:50]
    return f"{safe}-{h}" if safe else h


def download_poster(url: str, series_name: str) -> str:
    """تحميل البوستر محلياً — يحل مشكلة CORS."""
    if not url or not url.startswith("http"):
        return "posters/placeholder.jpg"

    ext = "jpg"
    low = url.lower().split("?")[0]
    for e in (".png", ".jpeg", ".jpg", ".webp", ".gif"):
        if low.endswith(e):
            ext = e.lstrip(".")
            break

    slug = slugify(series_name)
    out = POSTERS_DIR / f"{slug}.{ext}"
    if out.exists() and out.stat().st_size > 1024:
        return f"posters/{out.name}"

    for imp in ("chrome120", "chrome110"):
        try:
            r = cffi_requests.get(url, impersonate=imp, timeout=30, verify=False)
            if r.status_code == 200 and len(r.content) > 1024:
                with open(out, "wb") as f:
                    f.write(r.content)
                print(f"      💾 {series_name[:40]}")
                return f"posters/{out.name}"
        except Exception:
            continue
    return "posters/placeholder.jpg"


def extract_genre(name: str) -> str:
    """استنتاج النوع من الاسم."""
    patterns = {
        "تركية": ["تركي", "الموسم التركي"],
        "مكسيكية": ["مكسيك", "مدبلج"],
        "هندية": ["هند"],
        "كورية": ["كور"],
        "أمريكية": ["أمريك", "الأمريكي"],
        "عربية": ["عربي", "مصر", "سوري", "لبنان"],
    }
    for genre, keys in patterns.items():
        if any(k in name for k in keys):
            return genre
    return "أخرى"


# ═══════════════════════════════════════════════════════════════
# قوالب HTML
# ═══════════════════════════════════════════════════════════════
CARD_TMPL = """
<a class="card" href="watch/{slug}.html" data-name="{name}" data-genre="{genre}">
    <div class="poster-wrap">
        <img src="../{poster}" alt="{name}" loading="lazy"
             onerror="this.onerror=null;this.src='../posters/placeholder.jpg'">
        <div class="play-overlay">▶</div>
        <div class="badges">{badges}</div>
    </div>
    <div class="card-title">{name}</div>
    <div class="card-meta">{eps} حلقة • {genre}</div>
</a>
"""


def build_index_html(categories: dict, all_series: list, channel_url: str) -> str:
    """بناء الصفحة الرئيسية."""
    rows = ""
    for cat_name, items in categories.items():
        if not items:
            continue
        cards = ""
        for s in items[:40]:
            cards += CARD_TMPL.format(
                slug=s["slug"],
                name=s["name"],
                genre=s.get("genre", ""),
                poster=s.get("local_poster", "posters/placeholder.jpg"),
                badges=s.get("badges", ""),
                eps=s.get("episodes_count", 0),
            )
        rows += f"""
        <section class="row" data-category="{cat_name}">
            <div class="row-header">
                <h2>{cat_name}</h2>
                <button class="row-toggle" onclick="toggleRow(this)">عرض الكل</button>
            </div>
            <div class="carousel">{cards}</div>
        </section>
        """

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="شاهد أحدث المسلسلات التركية والمكسيكية والهندية مترجمة">
<title>Shoof — شاهد أحدث المسلسلات</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="style.css">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎬</text></svg>">
</head>
<body>

<header class="topbar">
    <div class="brand" onclick="location.href='index.html'">SHOOF</div>
    <nav class="main-nav">
        <a href="#" class="active" data-cat="الرئيسية">الرئيسية</a>
        <a href="#" data-cat="تركية">تركية</a>
        <a href="#" data-cat="مكسيكية">مكسيكية</a>
        <a href="#" data-cat="هندية">هندية</a>
        <a href="#" data-cat="أخرى">أخرى</a>
    </nav>
    <div class="search-box">
        <input type="search" id="search-input" placeholder="🔍 ابحث عن مسلسل...">
        <div id="search-results" class="search-results hidden"></div>
    </div>
    <a href="{channel_url}" class="tg-btn" target="_blank" rel="noopener">📡 قناتنا</a>
</header>

<main>
    <div class="hero">
        <div class="hero-content">
            <h1>مرحباً بك في SHOOF</h1>
            <p>أحدث المسلسلات التركية والمكسيكية والهندية مترجمة بجودة عالية</p>
            <button class="hero-btn" onclick="scrollToRows()">ابدأ المشاهدة</button>
        </div>
    </div>
    <div id="rows-container">
        {rows}
    </div>
</main>

<footer class="footer">
    <div>Shoof © 2026 — جميع الحقوق محفوظة</div>
    <div class="footer-links">
        <a href="{channel_url}" target="_blank">قناة تيليجرام</a>
        <span>•</span>
        <a href="https://github.com/oso28207-glitch/auto" target="_blank">GitHub</a>
    </div>
</footer>

<script src="app.js"></script>
</body>
</html>"""


def build_watch_html(series: dict, channel_url: str) -> str:
    """بناء صفحة مشاهدة مسلسل."""
    eps_json = json.dumps(series.get("episodes", []), ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="شاهد {series['name']} مترجم كامل">
<title>{series['name']} — Shoof</title>
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="../style.css">
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.15/dist/hls.min.js"></script>
</head>
<body class="watch-page">

<header class="topbar">
    <a href="../index.html" class="back-btn">←</a>
    <div class="brand" onclick="location.href='../index.html'">SHOOF</div>
    <div class="watch-title">{series['name']}</div>
    <a href="{channel_url}" class="tg-btn" target="_blank">📡 قناتنا</a>
</header>

<main class="watch-main">

    <div class="player-section">
        <div class="player-wrap">
            <video id="video-player" controls playsinline
                   poster="../{series.get('local_poster', 'posters/placeholder.jpg')}"></video>
            <div id="player-loading" class="player-loading hidden">
                <div class="spinner"></div>
                <p>جارٍ التحميل...</p>
            </div>
            <div id="autoplay-countdown" class="autoplay-countdown hidden">
                <div class="countdown-text">
                    <span>الحلقة التالية بعد</span>
                    <span id="countdown-num">10</span>
                    <span>ثوان</span>
                </div>
                <button class="countdown-btn" onclick="playNext()">شغّل الآن</button>
                <button class="countdown-cancel" onclick="cancelAutoplay()">إلغاء</button>
            </div>
        </div>

        <div class="player-info">
            <h1>{series['name']}</h1>
            <div class="info-meta">
                <span class="badge genre">{series.get('genre', 'دراما')}</span>
                <span class="badge eps" id="eps-count-badge">{series.get('episodes_count', 0)} حلقة</span>
                <span class="badge" id="current-ep-badge">الحلقة 1</span>
            </div>
            <p class="info-desc">
                شاهد مسلسل {series['name']} كامل مترجم بجودة عالية.
                اختر الحلقة من الشبكة أدناه واضغط عليها للمشاهدة.
            </p>
            <div class="player-controls">
                <button id="btn-prev" onclick="playPrev()">⏮ السابقة</button>
                <button id="btn-next" onclick="playNext()">التالية ⏭</button>
                <button id="btn-autoplay" onclick="toggleAutoplay()" class="active">🔁 تشغيل تلقائي: مفعّل</button>
            </div>
        </div>
    </div>

    <div class="episodes-section">
        <div class="episodes-header">
            <h2>الحلقات</h2>
            <div class="episodes-filter">
                <input type="search" id="ep-search" placeholder="ابحث برقم الحلقة...">
            </div>
        </div>
        <div class="episodes-grid" id="episodes-grid"></div>
    </div>

    <div class="related-section" id="related-section"></div>

</main>

<script>
const SERIES = {json.dumps({
    'name': series['name'],
    'slug': series['slug'],
    'episodes': series.get('episodes', []),
    'poster': series.get('local_poster', ''),
}, ensure_ascii=False)};
const CHANNEL_URL = {json.dumps(channel_url)};
</script>
<script src="../watch.js"></script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
# البناء الرئيسي
# ═══════════════════════════════════════════════════════════════
def build_site():
    print("\n" + "═" * 60)
    print("🏗️  بناء الموقع")
    print("═" * 60)

    # قراءة البيانات
    data_file = DATA_DIR / "series.json"
    if not data_file.exists():
        print("⚠️  لا يوجد data/series.json — تخطي البناء")
        return

    with open(data_file, encoding="utf-8") as f:
        data = json.load(f)

    raw_series = data.get("series", [])
    if not raw_series:
        print("⚠️  لا مسلسلات")
        return

    channel_url = f"https://t.me/{config.CHANNEL_ID.lstrip('@')}" if config.CHANNEL_ID else "#"

    # تجهيز المسلسلات
    series_list = []
    seen_slugs = set()

    for s in raw_series:
        name = (s.get("name") or "").strip()
        if not name:
            continue

        slug = slugify(name)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        episodes = s.get("episodes", [])
        if not episodes:
            # إن لم توجد قائمة حلقات، تجاهل المسلسل
            continue

        # تحميل البوستر
        poster_url = s.get("poster") or s.get("image") or ""
        local_poster = download_poster(poster_url, name)

        # badge "جديد"
        badges = ""
        if s.get("is_new"):
            badges = '<span class="badge new">جديد</span>'

        series_list.append({
            "name": name,
            "slug": slug,
            "poster": poster_url,
            "local_poster": local_poster,
            "genre": extract_genre(name) or s.get("genre", "أخرى"),
            "episodes": episodes,
            "episodes_count": len(episodes),
            "views": s.get("views", 0),
            "added_at": s.get("added_at", ""),
            "badges": badges,
        })

    if not series_list:
        print("⚠️  لا مسلسلات صالحة (كلها بدون حلقات)")
        return

    print(f"✅ {len(series_list)} مسلسل صالح")

    # التصنيفات
    categories = {
        "أحدث الإضافات": sorted(series_list,
                                key=lambda x: x.get("added_at", ""),
                                reverse=True)[:30],
        "الأكثر مشاهدة": sorted(series_list,
                                key=lambda x: x.get("views", 0),
                                reverse=True)[:30],
    }
    for g in ["تركية", "مكسيكية", "هندية", "كورية", "أخرى"]:
        items = [s for s in series_list if s["genre"] == g]
        if items:
            categories[g] = items

    # بناء الصفحة الرئيسية
    print("🏠 index.html...")
    with open(DOCS_DIR / "index.html", "w", encoding="utf-8") as f:
        f.write(build_index_html(categories, series_list, channel_url))

    # بناء صفحات المشاهدة
    watch_dir = DOCS_DIR / "watch"
    watch_dir.mkdir(exist_ok=True)

    # مسح قديم
    for old in watch_dir.glob("*.html"):
        try:
            old.unlink()
        except Exception:
            pass

    print(f"🎬 {len(series_list)} صفحة مشاهدة...")
    for s in series_list:
        with open(watch_dir / f"{s['slug']}.html", "w", encoding="utf-8") as f:
            f.write(build_watch_html(s, channel_url))

    # كتابة series.json للـ JS
    meta = {
        "series": [
            {
                "name": s["name"],
                "slug": s["slug"],
                "poster": s["local_poster"],
                "genre": s["genre"],
                "episodes_count": s["episodes_count"],
            }
            for s in series_list
        ],
        "categories": {k: [s["slug"] for s in v] for k, v in categories.items()},
        "total_series": len(series_list),
        "total_episodes": sum(s["episodes_count"] for s in series_list),
    }
    with open(DOCS_DIR / "series.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # placeholder
    placeholder = POSTERS_DIR / "placeholder.jpg"
    if not placeholder.exists():
        try:
            r = cffi_requests.get(
                "https://via.placeholder.com/400x600/1a1a1a/e50914?text=Shoof",
                impersonate="chrome120", timeout=15)
            if r.status_code == 200:
                with open(placeholder, "wb") as f:
                    f.write(r.content)
        except Exception:
            pass

    print(f"\n✅ اكتمل البناء:")
    print(f"   • {len(series_list)} مسلسل")
    print(f"   • {sum(s['episodes_count'] for s in series_list)} حلقة")
    print(f"   • {len(list(POSTERS_DIR.glob('*.jpg')) + list(POSTERS_DIR.glob('*.png')))} صورة")


if __name__ == "__main__":
    build_site()