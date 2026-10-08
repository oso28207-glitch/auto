"""build_site.py — يبني الموقع من بيانات Telegram."""
import json
import re
import hashlib
import shutil
from pathlib import Path
from urllib.parse import quote

from curl_cffi import requests as cffi

from config import config, DATA_DIR, DOCS_DIR

POSTERS = DOCS_DIR / "posters"
POSTERS.mkdir(parents=True, exist_ok=True)

TG_SERIES = DATA_DIR / "tg_series.json"


def _slug(name):
    h = hashlib.md5(name.encode()).hexdigest()[:8]
    s = re.sub(r'[^\w\u0600-\u06FF]+', '-', name).strip('-')[:40]
    return f"{s}-{h}" if s else h


def _dl_poster(url, name):
    if not url or not url.startswith("http"):
        return "posters/placeholder.jpg"
    ext = "jpg"
    for e in (".png", ".jpeg", ".jpg", ".webp"):
        if url.lower().split("?")[0].endswith(e):
            ext = e.lstrip(".")
            break
    slug = _slug(name)
    out = POSTERS / f"{slug}.{ext}"
    if out.exists() and out.stat().st_size > 1024:
        return f"posters/{out.name}"
    for imp in ("chrome120", "chrome110"):
        try:
            r = cffi.get(url, impersonate=imp, timeout=20, verify=False)
            if r.status_code == 200 and len(r.content) > 1024:
                with open(out, "wb") as f:
                    f.write(r.content)
                return f"posters/{out.name}"
        except Exception:
            continue
    return "posters/placeholder.jpg"


CARD = """<a class="card" href="watch/{slug}.html">
  <div class="poster-wrap">
    <img src="../{poster}" alt="{name}" loading="lazy"
         onerror="this.src='../posters/placeholder.jpg'">
    <div class="play">▶</div>
  </div>
  <div class="title">{name}</div>
  <div class="meta">{eps} حلقة</div>
</a>"""


def _watch_html(s, ch):
    eps_json = json.dumps(
        [{"num": e.get("num"), "url": e.get("url", "")}
         for e in s.get("episodes", [])],
        ensure_ascii=False,
    )
    return f"""<!DOCTYPE html><html lang="ar" dir="rtl"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{s['name']} — Shoof</title>
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="../style.css">
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.15/dist/hls.min.js"></script>
</head><body class="watch-page">
<header class="topbar">
<a href="../index.html" class="back">←</a>
<div class="brand">SHOOF</div>
<div class="wtitle">{s['name']}</div>
</header>
<main class="watch-main">
<div class="player-wrap"><video id="v" controls playsinline poster="../{s.get('poster', 'posters/placeholder.jpg')}"></video>
<div id="count" class="countdown hidden">الحلقة التالية بعد <span id="cn">10</span> <button onclick="next()">الآن</button></div>
</div>
<div class="info"><h1>{s['name']}</h1><div class="meta"><span class="badge">{s.get('episodes_count', 0)} حلقة</span></div>
<div class="controls"><button onclick="prev()">⏮ السابقة</button><button onclick="next()">التالية ⏭</button></div></div>
<div class="eps"><h2>الحلقات</h2><div id="grid" class="grid"></div></div>
</main>
<script>const S = {eps_json};const C = {json.dumps(ch)};</script>
<script src="../watch.js"></script></body></html>"""


def _priority_of(name: str) -> int:
    return config.priority_of(name)


def _load_tg_series():
    if not TG_SERIES.exists():
        return []
    try:
        data = json.loads(TG_SERIES.read_text(encoding="utf-8"))
        return data.get("series", [])
    except Exception:
        return []


def build_site():
    print("\n🏗️  بناء الموقع من بيانات Telegram", flush=True)

    series = _load_tg_series()

    if not series:
        print("   ⚠️ لا tg_series.json — استخدام series.json", flush=True)
        f = DATA_DIR / "series.json"
        if f.exists():
            try:
                with open(f, encoding="utf-8") as fh:
                    state = json.load(fh)
                series = state.get("series", [])
            except Exception:
                pass

    series = [s for s in series
              if s.get("name") and
              s.get("name") not in ("الصفحة الرئيسية", "جديد الأفلام")]

    if not series:
        print("⚠️ لا مسلسلات", flush=True)
        return

    series.sort(key=lambda s: (
        _priority_of(s.get("name", "")),
        s.get("name", ""),
    ))

    ch = (f"https://t.me/{config.CHANNEL_ID.lstrip('@')}"
          if config.CHANNEL_ID else "#")

    for s in series:
        if not s.get("local_poster"):
            s["local_poster"] = _dl_poster(
                s.get("poster", ""), s.get("name", "")
            )

    watch = DOCS_DIR / "watch"
    shutil.rmtree(watch, ignore_errors=True)
    watch.mkdir(parents=True, exist_ok=True)
    for s in series:
        slug = _slug(s["name"])
        with open(watch / f"{slug}.html", "w", encoding="utf-8") as fh:
            fh.write(_watch_html(s, ch))

    turkish_dubbed = [s for s in series if _priority_of(s["name"]) == 1]
    other_dubbed = [s for s in series if _priority_of(s["name"]) == 2]
    dubbed_movies = [s for s in series if _priority_of(s["name"]) == 3]
    regular = [s for s in series if _priority_of(s["name"]) >= 4]

    rows = ""

    def _make_cards(items):
        return "".join(CARD.format(
            slug=_slug(s["name"]), name=s["name"],
            poster=s.get("local_poster", "posters/placeholder.jpg"),
            eps=s.get("episodes_count", 0)) for s in items)

    if turkish_dubbed:
        rows += f'<section class="row"><h2>🇹🇷 مسلسلات تركية مدبلجة</h2><div class="grid">{_make_cards(turkish_dubbed)}</div></section>'

    if other_dubbed:
        rows += f'<section class="row"><h2>🎙️ مسلسلات مدبلجة</h2><div class="grid">{_make_cards(other_dubbed)}</div></section>'

    if dubbed_movies:
        rows += f'<section class="row"><h2>🎬 أفلام مدبلجة</h2><div class="grid">{_make_cards(dubbed_movies)}</div></section>'

    if regular:
        rows += f'<section class="row"><h2>📺 مسلسلات وأفلام</h2><div class="grid">{_make_cards(regular[:60])}</div></section>'

    html = f"""<!DOCTYPE html><html lang="ar" dir="rtl"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shoof — شاهد أحدث المسلسلات</title>
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="style.css"></head><body>
<header class="topbar"><div class="brand">SHOOF</div>
<nav><a href="#">الرئيسية</a><a href="#series">المسلسلات</a></nav>
<a href="{ch}" class="tg">📡 قناتنا</a></header>
<main>{rows}</main>
<footer class="footer">Shoof © 2026</footer>
</body></html>"""

    with open(DOCS_DIR / "index.html", "w", encoding="utf-8") as fh:
        fh.write(html)

    meta = {"series": [
        {"name": s["name"], "slug": _slug(s["name"]),
         "poster": s.get("local_poster", ""),
         "eps": s.get("episodes_count", 0)}
        for s in series
    ]}
    with open(DOCS_DIR / "series.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    print(f"✅ {len(series)} مسلسل "
          f"(تركي مدبلج: {len(turkish_dubbed)}, "
          f"مدبلج: {len(other_dubbed)}, "
          f"أفلام مدبلجة: {len(dubbed_movies)})", flush=True)


if __name__ == "__main__":
    build_site()