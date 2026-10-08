"""
build_site.py — يبني موقع "شوف" كاملاً من بيانات Telegram + صور المصادر الخارجية.

★ v3 (إصلاح عرض الموقع):
    - إصلاح مسارات الصور: "posters/..." للصفحة الرئيسية (root) و"../posters/..." لصفحات المشاهدة.
    - إنشاء docs/posters/placeholder.jpg حقيقي عند غيابه.
    - صفحات مشاهدة متوافقة 100% مع docs/watch.js
      (btn-prev/btn-next/btn-autoplay/ep-search/player-loading/current-ep-badge).
    - ربط الحلقات بمشغّل البث عبر API_BASE/stream باستخدام file_id + size + message_id.
    - كتابة docs/videos.json و docs/config.js.
    - صفحة رئيسية بتصميم docs/style.css (topbar + hero + rows + cards + search).
"""
import json
import re
import hashlib
import shutil
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as cffi

from config import config, DATA_DIR, DOCS_DIR
from names import norm as _norm, base_norm as _base_norm

POSTERS = DOCS_DIR / "posters"
POSTERS.mkdir(parents=True, exist_ok=True)

# نسخة الأصول الثابتة (لتفادي كاش المتصفح بعد كل تحديث)
ASSET_VER = "3"

TG_SERIES = DATA_DIR / "tg_series.json"
STATE = DATA_DIR / "state.json"
UPLOADED = DATA_DIR / "uploaded.json"

IMPERSONATIONS = ["safari17_0", "chrome120", "chrome110", "chrome104"]

# عناوين الأقسام (تُستخدم أيضاً كقيم data-category للتصفية)
SEC_TURKISH = "مسلسلات تركية مدبلجة"
SEC_DUBBED = "مسلسلات مدبلجة"
SEC_DUB_MOVIES = "أفلام مدبلجة"
SEC_REGULAR = "مسلسلات وأفلام"


# ═══════════════════════════════════════════════════════════════════
# أدوات
# ═══════════════════════════════════════════════════════════════════
def _slug(name):
    h = hashlib.md5(name.encode()).hexdigest()[:8]
    s = re.sub(r'[^\w\u0600-\u06FF]+', '-', name).strip('-')[:40]
    return f"{s}-{h}" if s else h


def _norm_old(name):
    """(احتياطي) تطبيع قديم — لم يعد مستخدماً؛ انظر names.py"""
    n = (name or "").strip().lower()
    n = re.sub(r"^مسلسل\s+", "", n)
    n = re.sub(r"^فيلم\s+", "", n)
    n = re.sub(r"[\s\-_\.]+", "", n)
    return n


def _load_json(path, default):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _priority_of(name: str, category: str = "") -> int:
    n = (name or "").strip()
    c = (category or "").strip()
    combined = f"{n} {c}".lower()

    is_turkish = any(k in combined for k in
                     ["تركي", "turkish", "ترك", "turkiaa", "turk"])
    is_dubbed = any(k in combined for k in
                    ["مدبلج", "مدبلجة", "dubbed",
                     "modblga", "modblja", "modblge", "dub"])
    is_movie = any(k in combined for k in
                   ["فيلم", "أفلام", "افلام", "movie", "film", "aflam"])

    if is_turkish and is_dubbed and not is_movie:
        return 1
    if is_dubbed and not is_movie:
        return 2
    if is_dubbed and is_movie:
        return 3
    if not is_movie:
        return 4
    return 5


def _section_of(prio: int) -> str:
    return {
        1: SEC_TURKISH,
        2: SEC_DUBBED,
        3: SEC_DUB_MOVIES,
    }.get(prio, SEC_REGULAR)


# ═══════════════════════════════════════════════════════════════════
# الصور
# ═══════════════════════════════════════════════════════════════════
def _ensure_placeholder():
    """ينشئ صورة بديلة حقيقية إذا لم تكن موجودة."""
    p = POSTERS / "placeholder.jpg"
    if p.exists() and p.stat().st_size > 800:
        return
    try:
        from PIL import Image, ImageDraw
        W, H = 340, 510
        img = Image.new("RGB", (W, H), (18, 18, 20))
        d = ImageDraw.Draw(img)
        for y in range(H):
            t = y / H
            c = int(18 + (44 - 18) * t)
            d.line([(0, y), (W, y)], fill=(c, c, c + 6))
        cx, cy = W // 2, H // 2 - 20
        d.ellipse([cx - 52, cy - 52, cx + 52, cy + 52], outline=(229, 9, 20), width=4)
        d.polygon([(cx - 16, cy - 26), (cx - 16, cy + 26), (cx + 30, cy)],
                  fill=(229, 9, 20))
        try:
            d.text((W // 2 - 34, H - 70), "SHOOF", fill=(210, 210, 210))
        except Exception:
            pass
        img.save(p, "JPEG", quality=86)
        print(f"   🖼️  أُنشئت صورة بديلة: {p.name}")
    except Exception as e:
        # احتياطي: JPEG رمادي صغير
        try:
            import base64
            data = base64.b64decode(
                "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsL"
                "DBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/"
                "wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAA"
                "AAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
            )
            p.write_bytes(data)
        except Exception:
            pass


def _poster_ext(url: str) -> str:
    path = urlparse(url).path.lower()
    for e in (".png", ".jpeg", ".jpg", ".webp"):
        if path.endswith(e):
            return e.lstrip(".")
    return "jpg"


def _dl_poster(url, name):
    """ينزّل البوستر محلياً ويعيد المسار النسبي 'posters/xxx.ext'."""
    if not url or not url.startswith("http"):
        return "posters/placeholder.jpg"

    ext = _poster_ext(url)
    slug = _slug(name)
    out = POSTERS / f"{slug}.{ext}"
    if out.exists() and out.stat().st_size > 1024:
        return f"posters/{out.name}"

    referer = "{u.scheme}://{u.netloc}/".format(u=urlparse(url))
    for imp in IMPERSONATIONS:
        try:
            r = cffi.get(
                url, impersonate=imp, timeout=25, verify=False,
                headers={"Referer": referer, "User-Agent":
                         "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                         "Version/17.0 Safari/605.1.15"},
            )
            if r.status_code == 200 and len(r.content) > 1024:
                with open(out, "wb") as f:
                    f.write(r.content)
                return f"posters/{out.name}"
        except Exception:
            continue
    return "posters/placeholder.jpg"


# ═══════════════════════════════════════════════════════════════════
# خريطة البث (file_id) من state.json / uploaded.json
# ═══════════════════════════════════════════════════════════════════
def _stream_url(fid, size, mid):
    base = (config.API_BASE or "").rstrip("/")
    if not base or not fid or not size:
        return ""
    try:
        size = int(size)
        mid = int(mid or 0)
    except Exception:
        return ""
    return f"{base}/stream?fid={fid}&size={size}&mid={mid}"


def _load_stream_map():
    """
    يعيد dict: {normalized_name: {ep_num: {fid, size, mid}}}
    من data/state.json (الأساسي) و data/uploaded.json (احتياطي).
    """
    raw = {}

    st = _load_json(STATE, {})
    for name, s in (st.get("series") or {}).items():
        eps = s.get("episodes") or {}
        d = {}
        for num, e in eps.items():
            fid = e.get("file_id")
            size = e.get("size")
            if fid and size:
                try:
                    d[int(num)] = {
                        "fid": fid, "size": int(size),
                        "mid": int(e.get("message_id") or 0),
                    }
                except Exception:
                    pass
        if d:
            raw[name] = d

    up = _load_json(UPLOADED, {})
    for k, v in (up or {}).items():
        if "|ep" not in k:
            continue
        name, ep = k.rsplit("|ep", 1)
        try:
            ep = int(ep)
        except Exception:
            continue
        if v.get("file_id") and v.get("size"):
            raw.setdefault(name, {})[ep] = {
                "fid": v["file_id"], "size": int(v["size"]),
                "mid": int(v.get("message_id") or 0),
            }

    # فهرسة بعدة مفاتيح مطبَّعة لتحسين المطابقة
    #   exact : مفتاح مطبَّع كامل (يتضمّن الموسم)
    #   base  : مفتاح بدون رقم الموسم (احتياطي)
    exact, base = {}, {}
    for name, eps in raw.items():
        k = _norm(name)
        if k:
            exact.setdefault(k, {}).update(eps)
        bk = _base_norm(name)
        if bk:
            base.setdefault(bk, {}).update(eps)
    return {"exact": exact, "base": base}


def _lookup_stream(stream_map, name):
    exact = stream_map.get("exact", {}) if isinstance(stream_map, dict) else {}
    base = stream_map.get("base", {}) if isinstance(stream_map, dict) else {}
    k = _norm(name)
    if k and k in exact:
        return exact[k]
    bk = _base_norm(name)
    if bk and bk in base:
        return base[bk]
    return {}


# ═══════════════════════════════════════════════════════════════════
# قوالب HTML
# ═══════════════════════════════════════════════════════════════════
CARD = """<a class="card" href="watch/{slug}.html" title="{name}">
  <div class="poster-wrap">
    <img src="{poster}" alt="{name}" loading="lazy"
         onerror="this.onerror=null;this.src='posters/placeholder.jpg'">
    <div class="play-overlay">▶</div>
    <div class="badges">{badges}</div>
  </div>
  <div class="title">{name}</div>
  <div class="meta">{eps} حلقة</div>
</a>"""


def _cat_label(category: str) -> str:
    """يحوّل تصنيف المصدر (slug) إلى وسم عربي نظيف."""
    c = (category or "").strip().lower()
    if not c:
        return ""
    if "turkiaa" in c or "turk" in c or "تركي" in c:
        return "تركي مدبلج"
    if "modblga" in c or "modblja" in c or "modblge" in c or "مدبلج" in c or "dub" in c:
        return "مدبلج"
    if "aflam" in c or "فيلم" in c or "movie" in c:
        return "فيلم"
    if "مترجم" in c:
        return "مترجم"
    if "hndia" in c or "هندي" in c:
        return "هندي مدبلج"
    return ""


def _badges(s):
    out = ""
    if s.get("is_new"):
        out += '<span class="badge new">جديد</span>'
    label = _cat_label(s.get("category"))
    if label:
        out += f'<span class="badge genre">{label}</span>'
    return out


def _watch_html(s, ch, stream_eps):
    name = s["name"]
    slug = _slug(name)
    poster = s.get("local_poster") or "posters/placeholder.jpg"
    count = s.get("episodes_count", 0)

    eps_list = []
    for e in s.get("episodes", []):
        num = e.get("num")
        try:
            num = int(num)
        except Exception:
            continue
        info = stream_eps.get(num) or {}
        url = _stream_url(info.get("fid"), info.get("size"), info.get("mid"))
        eps_list.append({"num": num, "url": url})
    eps_list.sort(key=lambda x: x["num"])

    playable = sum(1 for e in eps_list if e["url"])
    eps_json = json.dumps(eps_list, ensure_ascii=False)

    return f"""<!DOCTYPE html><html lang="ar" dir="rtl"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0a0a0a">
<title>{name} — شوف</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="../style.css?v={ASSET_VER}">
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.15/dist/hls.min.js"></script>
</head><body class="watch-page">
<header class="topbar">
  <a href="../index.html" class="back" title="رجوع">←</a>
  <div class="brand">شوف</div>
  <div class="wtitle">{name}</div>
  <a href="{ch}" class="tg-btn" target="_blank" rel="noopener">📡 القناة</a>
</header>
<main class="watch-main">
  <section class="player-section">
    <div class="player-wrap">
      <video id="v" controls playsinline preload="metadata"
             poster="../{poster}"></video>
      <div id="player-loading" class="player-loading hidden">
        <div class="spinner"></div><p>جارٍ التحميل...</p>
      </div>
      <div id="count" class="autoplay-countdown hidden">
        <span class="countdown-text">الحلقة التالية بعد
          <span id="cn">10</span> ثانية</span>
        <button class="countdown-btn" onclick="window.playNext()">شاهد الآن</button>
        <button class="countdown-cancel" onclick="window.cancelAutoplay()">إلغاء</button>
      </div>
    </div>
    <div class="player-info">
      <h1>{name}</h1>
      <div class="info-meta">
        <span class="badge eps">{count} حلقة</span>
        <span class="badge genre" id="current-ep-badge">الحلقة 1</span>
      </div>
      <div class="player-controls">
        <button id="btn-prev">⏮ السابقة</button>
        <button id="btn-autoplay" class="active">🔁 تشغيل تلقائي: مفعّل</button>
        <button id="btn-next">التالية ⏭</button>
      </div>
    </div>
  </section>
  <section class="episodes-section">
    <div class="episodes-header">
      <h2>الحلقات</h2>
      <div class="episodes-filter">
        <input id="ep-search" type="search" placeholder="ابحث عن رقم الحلقة...">
      </div>
    </div>
    <div id="grid" class="episodes-grid"></div>
  </section>
</main>
<footer class="footer">
  <div>شوف — منصة المشاهدة</div>
  <div class="footer-links">
    <a href="../index.html">الرئيسية</a>
    <a href="{ch}" target="_blank" rel="noopener">قناة تيليجرام</a>
  </div>
</footer>
<script>window.S = {eps_json}; window.C = {json.dumps(ch)}; window.PLAYABLE = {playable};</script>
<script src="../watch.js?v={ASSET_VER}"></script>
</body></html>"""


def _index_html(series, ch, built_at):
    rows = ""
    order = [SEC_TURKISH, SEC_DUBBED, SEC_DUB_MOVIES, SEC_REGULAR]
    icons = {
        SEC_TURKISH: "🇹🇷", SEC_DUBBED: "🎙️",
        SEC_DUB_MOVIES: "🎬", SEC_REGULAR: "📺",
    }
    by_sec = {k: [] for k in order}
    for s in series:
        sec = _section_of(_priority_of(s.get("name", ""), s.get("category", "")))
        by_sec.setdefault(sec, []).append(s)

    for sec in order:
        items = by_sec.get(sec) or []
        if not items:
            continue
        cards = "".join(CARD.format(
            slug=_slug(s["name"]), name=s["name"],
            poster=s.get("local_poster", "posters/placeholder.jpg"),
            eps=s.get("episodes_count", 0),
            badges=_badges(s),
        ) for s in items)
        toggle = (f'<button class="row-toggle" onclick="toggleRow(this)">عرض الكل</button>'
                  if len(items) > 10 else "")
        rows += (
            f'<section class="row" data-category="{sec}">'
            f'<div class="row-header"><h2>{icons.get(sec, "")} {sec}</h2>{toggle}</div>'
            f'<div class="carousel">{cards}</div></section>'
        )

    nav_items = "".join(
        f'<a href="#" data-cat="{sec}">{icons.get(sec, "")} {sec}</a>'
        for sec in order if by_sec.get(sec)
    )

    total_eps = sum(s.get("episodes_count", 0) for s in series)

    return f"""<!DOCTYPE html><html lang="ar" dir="rtl"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0a0a0a">
<meta name="description" content="شوف — منصة مشاهدة المسلسلات والأفلام المدبلجة والمترجمة">
<title>شوف — شاهد أحدث المسلسلات والأفلام</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="style.css?v={ASSET_VER}">
<style>
  .row-toggle{{background:#2a2a2a;color:var(--text);border:1px solid var(--border);
    padding:6px 14px;border-radius:6px;cursor:pointer;font-family:inherit;
    font-size:12px;font-weight:600}}
  .row-toggle:hover{{background:#3a3a3a;border-color:var(--accent)}}
  .carousel{{max-height:430px;overflow:hidden}}
  .carousel.expanded{{max-height:none}}
  .hero-stats{{display:flex;gap:24px;margin-top:18px;flex-wrap:wrap}}
  .hero-stat{{display:flex;flex-direction:column}}
  .hero-stat b{{font-size:26px;color:#fff}}
  .hero-stat span{{font-size:12px;color:var(--dim)}}
</style>
</head><body>
<header class="topbar">
  <div class="brand" onclick="window.scrollTo({{top:0,behavior:'smooth'}})">شوف</div>
  <div class="search-box">
    <input id="search-input" type="search" placeholder="ابحث عن مسلسل أو فيلم..."
           autocomplete="off" aria-label="بحث">
    <div id="search-results" class="search-results hidden"></div>
  </div>
  <nav class="main-nav">
    <a href="#" data-cat="__all__" class="active">الرئيسية</a>
    {nav_items}
  </nav>
  <a href="{ch}" class="tg-btn" target="_blank" rel="noopener">📡 قناتنا</a>
</header>

<section class="hero">
  <div class="hero-content">
    <h1>شاهد أحدث المسلسلات والأفلام</h1>
    <p>مكتبة من المسلسلات المدبلجة والمترجمة بجودة عالية — مباشرة من قنواتنا.</p>
    <div class="hero-stats">
      <div class="hero-stat"><b>{len(series)}</b><span>عمل</span></div>
      <div class="hero-stat"><b>{total_eps}</b><span>حلقة</span></div>
      <div class="hero-stat"><b>{built_at}</b><span>آخر تحديث</span></div>
    </div>
    <button class="hero-btn" onclick="document.getElementById('rows-container').scrollIntoView({{behavior:'smooth'}})">
      استكشف الآن
    </button>
  </div>
</section>

<main id="rows-container" style="padding:32px;max-width:1600px;margin:0 auto">
  {rows}
</main>

<footer class="footer">
  <div>شوف — منصة المشاهدة © {datetime.now().year}</div>
  <div class="footer-links">
    <a href="{ch}" target="_blank" rel="noopener">قناة تيليجرام</a>
    <a href="#" onclick="window.scrollTo({{top:0,behavior:'smooth'}});return false">أعلى الصفحة</a>
  </div>
</footer>
<script src="config.js?v={ASSET_VER}"></script>
<script src="app.js?v={ASSET_VER}"></script>
</body></html>"""


# ═══════════════════════════════════════════════════════════════════
# البناء
# ═══════════════════════════════════════════════════════════════════
def _load_tg_series():
    data = _load_json(TG_SERIES, None)
    if data and data.get("series"):
        return data["series"]
    data = _load_json(DATA_DIR / "series.json", None)
    if data and data.get("series"):
        return data["series"]
    return []


def build_site():
    print("\n🏗️  بناء الموقع من بيانات Telegram", flush=True)

    series = _load_tg_series()
    series = [s for s in series
              if s.get("name") and
              s.get("name") not in ("الصفحة الرئيسية", "جديد الأفلام")]

    if not series:
        print("⚠️ لا مسلسلات لبناء الموقع", flush=True)
        return

    # ── الترتيب: الأولوية ثم الأحدث ──
    def _date_key(s):
        return s.get("last_updated", "") or s.get("built_at", "")

    series.sort(key=_date_key, reverse=True)
    series.sort(key=lambda s: _priority_of(s.get("name", ""), s.get("category", "")))

    # ── الصورة البديلة ──
    _ensure_placeholder()

    # ── تنزيل الصور ──
    print(f"   🖼️  تنزيل صور {len(series)} عمل...", flush=True)
    ok = 0
    for s in series:
        lp = _dl_poster(s.get("poster", ""), s.get("name", ""))
        s["local_poster"] = lp
        if lp != "posters/placeholder.jpg":
            ok += 1
    print(f"   ✅ {ok}/{len(series)} صورة حقيقية", flush=True)

    # ── خريطة البث ──
    stream_map = _load_stream_map()
    print(f"   🎬 {len(stream_map.get('exact', {}))} عمل متاح للبث (file_id)", flush=True)

    # ── صفحات المشاهدة ──
    watch = DOCS_DIR / "watch"
    shutil.rmtree(watch, ignore_errors=True)
    watch.mkdir(parents=True, exist_ok=True)

    ch = (f"https://t.me/{config.CHANNEL_ID.lstrip('@')}"
          if config.CHANNEL_ID else "https://t.me/shoofcima")

    playable_total = 0
    for s in series:
        stream_eps = _lookup_stream(stream_map, s["name"])
        playable_total += len(stream_eps)
        slug = _slug(s["name"])
        with open(watch / f"{slug}.html", "w", encoding="utf-8") as fh:
            fh.write(_watch_html(s, ch, stream_eps))

    # ── الصفحة الرئيسية ──
    built_at = datetime.now().strftime("%Y-%m-%d")
    with open(DOCS_DIR / "index.html", "w", encoding="utf-8") as fh:
        fh.write(_index_html(series, ch, built_at))

    # ── series.json (للبحث في app.js) ──
    meta = {"built_at": datetime.now().isoformat(), "series": [
        {
            "name": s["name"],
            "slug": _slug(s["name"]),
            "poster": s.get("local_poster", "posters/placeholder.jpg"),
            "episodes_count": s.get("episodes_count", 0),
            "eps": s.get("episodes_count", 0),
            "genre": s.get("genre", ""),
            "category": s.get("category", ""),
            "last_updated": s.get("last_updated", ""),
        }
        for s in series
    ]}
    with open(DOCS_DIR / "series.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    # ── videos.json + config.js ──
    _write_videos_json()
    _write_config_js()

    # إحصاء الأقسام
    counts = {}
    for s in series:
        sec = _section_of(_priority_of(s.get("name", ""), s.get("category", "")))
        counts[sec] = counts.get(sec, 0) + 1

    print(f"✅ {len(series)} عمل | {playable_total} حلقة قابلة للبث | "
          f"صور: {ok}", flush=True)
    for sec in (SEC_TURKISH, SEC_DUBBED, SEC_DUB_MOVIES, SEC_REGULAR):
        if counts.get(sec):
            print(f"      · {sec}: {counts[sec]}", flush=True)


def _write_videos_json():
    st = _load_json(STATE, {})
    videos = []
    for v in (st.get("videos") or []):
        fid = v.get("file_id")
        size = v.get("size")
        if not fid or not size:
            continue
        videos.append({
            "id": v.get("id"),
            "mid": v.get("id"),
            "title": v.get("title", ""),
            "series": v.get("series", ""),
            "episode": v.get("episode"),
            "file_id": fid,
            "size": size,
            "width": v.get("width"),
            "height": v.get("height"),
            "duration": v.get("duration"),
            "date": v.get("date", ""),
        })
    with open(DOCS_DIR / "videos.json", "w", encoding="utf-8") as fh:
        json.dump(videos, fh, ensure_ascii=False)
    print(f"   🎞️  videos.json: {len(videos)} فيديو", flush=True)


def _write_config_js():
    api = (config.API_BASE or "").rstrip("/")
    cfg = f'window.APP_CONFIG = {{ API_BASE: "{api}" }};\n'
    with open(DOCS_DIR / "config.js", "w", encoding="utf-8") as fh:
        fh.write(cfg)
    print(f"   ⚙️  config.js: API_BASE={api or '(فارغ)'}", flush=True)


if __name__ == "__main__":
    build_site()
