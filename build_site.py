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
from names import (
    norm as _norm, base_norm as _base_norm,
    split_season as _split_season,
    canonical_category as _canonical_category,
    CATEGORY_PRIORITY as _CATEGORY_PRIORITY,
    CAT_TURK_DUB, CAT_IND_DUB, CAT_ANIME_DUB, CAT_KOR_DUB, CAT_DUB,
    CAT_DUB_MOVIES, CAT_TRANS, CAT_ARABIC, CAT_FOREIGN, CAT_ASIAN,
    CAT_MOVIES, CAT_GENERIC,
)

POSTERS = DOCS_DIR / "posters"
POSTERS.mkdir(parents=True, exist_ok=True)

# نسخة الأصول الثابتة (لتفادي كاش المتصفح بعد كل تحديث)
ASSET_VER = "3"

TG_SERIES = DATA_DIR / "tg_series.json"
STATE = DATA_DIR / "state.json"
UPLOADED = DATA_DIR / "uploaded.json"

IMPERSONATIONS = ["safari17_0", "chrome120", "chrome110", "chrome104"]

# عناوين الأقسام = التصنيفات الموحّدة (تُستخدم أيضاً كقيم data-category للتصفية)
SEC_TURK_DUB   = CAT_TURK_DUB
SEC_IND_DUB    = CAT_IND_DUB
SEC_ANIME_DUB  = CAT_ANIME_DUB
SEC_KOR_DUB    = CAT_KOR_DUB
SEC_DUB        = CAT_DUB
SEC_TRANS      = CAT_TRANS
SEC_ARABIC     = CAT_ARABIC
SEC_FOREIGN    = CAT_FOREIGN
SEC_ASIAN      = CAT_ASIAN
SEC_DUB_MOVIES = CAT_DUB_MOVIES
SEC_MOVIES     = CAT_MOVIES
SEC_GENERIC    = CAT_GENERIC

# ترتيب عرض الأقسام في الصفحة الرئيسية (الأهم أولاً)
SECTION_ORDER = [
    SEC_TURK_DUB, SEC_IND_DUB, SEC_ANIME_DUB, SEC_KOR_DUB, SEC_DUB,
    SEC_TRANS, SEC_ARABIC, SEC_FOREIGN, SEC_ASIAN,
    SEC_DUB_MOVIES, SEC_MOVIES, SEC_GENERIC,
]

SECTION_ICONS = {
    SEC_TURK_DUB:   "🇹🇷",
    SEC_IND_DUB:    "🇮🇳",
    SEC_ANIME_DUB:  "🎌",
    SEC_KOR_DUB:    "🇰🇷",
    SEC_DUB:        "🎙️",
    SEC_TRANS:      "📝",
    SEC_ARABIC:     "🎭",
    SEC_FOREIGN:    "🌍",
    SEC_ASIAN:      "🌏",
    SEC_DUB_MOVIES: "🎬",
    SEC_MOVIES:     "🎥",
    SEC_GENERIC:    "📺",
}

# وسوم البطاقات المختصرة حسب التصنيف الموحّد
CAT_BADGE = {
    CAT_TURK_DUB:   "تركي مدبلج",
    CAT_IND_DUB:    "هندي مدبلج",
    CAT_ANIME_DUB:  "أنمي مدبلج",
    CAT_KOR_DUB:    "كوري مدبلج",
    CAT_DUB:        "مدبلج",
    CAT_TRANS:      "مترجم",
    CAT_ARABIC:     "عربي",
    CAT_FOREIGN:    "أجنبي",
    CAT_ASIAN:      "أسيوي",
    CAT_DUB_MOVIES: "فيلم مدبلج",
    CAT_MOVIES:     "فيلم",
    CAT_GENERIC:    "",
}

# عكس خريطة الأولويات: رقم الأولوية → اسم القسم
_PRIO_TO_SEC = {v: k for k, v in _CATEGORY_PRIORITY.items()}


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


def _priority_of(name: str, category: str = "", source: str = "") -> int:
    """أولوية العرض (1 = الأعلى) مبنية على التصنيف الموحّد."""
    try:
        cat = _canonical_category(name=name, category=category, source=source)
        return _CATEGORY_PRIORITY.get(cat, 12)
    except Exception:
        return 12


def _section_of(prio: int) -> str:
    """رقم الأولوية → اسم القسم الموحّد."""
    return _PRIO_TO_SEC.get(prio, SEC_GENERIC)


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
    """ينزّل البوستر محلياً ويعيد المسار النسبي 'posters/xxx.ext'.

    ★ مقاومة للحجب: نجرّب عدة بصمات متصفح، ثم محاولة ثانية بدون Referer،
      ثم محاولة عبر نطاقات u3seq البديلة — لأن سيرفرات GitHub Actions
      قد تُحجب من Cloudflare على النطاق الأساسي.
    """
    if not url or not url.startswith("http"):
        return "posters/placeholder.jpg"

    ext = _poster_ext(url)
    slug = _slug(name)
    out = POSTERS / f"{slug}.{ext}"
    if out.exists() and out.stat().st_size > 1024:
        return f"posters/{out.name}"

    referer = "{u.scheme}://{u.netloc}/".format(u=urlparse(url))
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/17.0 Safari/605.1.15")

    # بناء قائمة عناوين بديلة (نطاقات u3seq المتكافئة)
    urls = [url]
    try:
        pu = urlparse(url)
        if "u.3seq." in pu.netloc:
            for alt in ("u.3seq.cam", "u.3seq.com"):
                if alt != pu.netloc:
                    urls.append(url.replace(pu.netloc, alt))
    except Exception:
        pass

    # محاولة 1: بكل البصمات + Referer
    for u in urls:
        for imp in IMPERSONATIONS:
            try:
                r = cffi.get(u, impersonate=imp, timeout=25, verify=False,
                             headers={"Referer": referer, "User-Agent": ua})
                if r.status_code == 200 and len(r.content) > 1024:
                    with open(out, "wb") as f:
                        f.write(r.content)
                    return f"posters/{out.name}"
            except Exception:
                continue

    # محاولة 2: بدون Referer (بعض المضيفات ترفض الـ Referer)
    for u in urls:
        for imp in ("chrome120", "safari17_0"):
            try:
                r = cffi.get(u, impersonate=imp, timeout=25, verify=False,
                             headers={"User-Agent": ua})
                if r.status_code == 200 and len(r.content) > 1024:
                    with open(out, "wb") as f:
                        f.write(r.content)
                    return f"posters/{out.name}"
            except Exception:
                continue

    return "posters/placeholder.jpg"


def _search_poster(name: str) -> str:
    """بحث احتياطي عن بوستر في yam بالاسم (عند غياب بوستر المطابقة)."""
    if not name or len(name.strip()) < 3:
        return ""
    try:
        from sources.yam import search_series
        m = search_series(name)
        if not m:
            base = re.sub(r"\s+الموسم\s+\d+\s*$", "", name).strip()
            base = re.sub(r"\s+\d+\s*$", "", base).strip()
            if base and base != name:
                m = search_series(base)
        return (m or {}).get("poster", "") or ""
    except Exception:
        return ""


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

    # ★ مصدر التليجرام المفحوص (tg_series.json): يملأ الحلقات الناقصة دون استبدال الموجود
    tg = _load_json(TG_SERIES, {})
    for s in (tg.get("series") or []):
        name = s.get("name") or ""
        if not name:
            continue
        d = {}
        for e in (s.get("episodes") or []):
            try:
                num = int(e.get("num"))
            except Exception:
                continue
            fid = e.get("file_id")
            size = e.get("size")
            if fid and size:
                try:
                    d[num] = {
                        "fid": fid, "size": int(size),
                        "mid": int(e.get("mid") or 0),
                    }
                except Exception:
                    pass
        if d:
            bucket = raw.setdefault(name, {})
            for num, info in d.items():
                bucket.setdefault(num, info)

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
# ★ دمج المواسم: تجميع المسلسلات في "أعمال" حسب اسم العمل + رقم الموسم
def _group_into_works(series):
    """يجمع مدخلات المسلسلات في "أعمال" موحّدة.

    كل عمل = اسم أساسي (بدون رقم الموسم) + قائمة مواسم، وكل موسم يحمل حلقاته.
    مثال: ["حب للايجار الموسم 1", "حب للايجار الموسم 2"] → عمل واحد بموسمين.
    """
    groups = {}     # key -> work
    order = []      # ترتيب أول ظهور للمفاتيح

    for s in series:
        raw_name = (s.get("name") or "").strip()
        if not raw_name:
            continue
        show, season = _split_season(raw_name)
        show = (show or raw_name).strip()
        key = _base_norm(show) or _norm(show) or raw_name

        w = groups.get(key)
        if w is None:
            w = {"key": key, "seasons": {}, "order": len(order)}
            groups[key] = w
            order.append(key)

        prev = w["seasons"].get(season)
        if prev is None:
            w["seasons"][season] = s
        else:
            # نفس الموسم مرتين (مصدران) → نُبقي الأكثر حلقات وندمج الحلقات
            if len(s.get("episodes", [])) > len(prev.get("episodes", [])):
                w["seasons"][season] = s
            else:
                merged = dict(prev)
                seen = {e.get("num") for e in prev.get("episodes", [])}
                extra = [e for e in s.get("episodes", [])
                         if e.get("num") not in seen]
                if extra:
                    merged["episodes"] = list(prev.get("episodes", [])) + extra
                    merged["episodes_count"] = len(merged["episodes"])
                    w["seasons"][season] = merged

    works = []
    for key in order:
        w = groups[key]
        seasons = [{"season": num, "series": w["seasons"][num]}
                   for num in sorted(w["seasons"].keys())]

        # الاسم المعروض من أدنى موسم (عادةً الموسم الأول)
        lowest = seasons[0]["series"]
        disp, _ = _split_season(lowest.get("name", ""))
        disp = (disp or lowest.get("name", "")).strip()

        # البوستر/التصنيف من الموسم الأكثر حلقات (أفضل تغطية)
        best = max(seasons, key=lambda x: len(x["series"].get("episodes", [])))
        bs = best["series"]

        total_eps = sum(len(x["series"].get("episodes", [])) for x in seasons)
        is_new = any(bool(x["series"].get("is_new")) for x in seasons)
        last_updated = max(
            (x["series"].get("last_updated", "") or "" for x in seasons),
            default="")

        works.append({
            "name": disp,
            "key": key,
            "seasons": seasons,
            "seasons_count": len(seasons),
            "poster": bs.get("poster", ""),
            "category": bs.get("category", ""),
            "genre": bs.get("genre", ""),
            "source": bs.get("source", ""),
            "episodes_count": total_eps,
            "is_new": is_new,
            "last_updated": last_updated,
            "order": w["order"],
        })
    return works


CARD = """<a class="card" href="watch/{slug}.html" title="{name}">
  <div class="poster-wrap">
    <img src="{poster}" alt="{name}" loading="lazy"
         onerror="this.onerror=null;this.src='posters/placeholder.jpg'">
    <div class="play-overlay">▶</div>
    <div class="badges">{badges}</div>
  </div>
  <div class="title">{name}</div>
  <div class="meta">{meta}</div>
</a>"""


def _cat_label(category: str) -> str:
    """وسم مختصر للبطاقة حسب التصنيف الموحّد (يعمل أيضاً مع التصنيفات القديمة)."""
    c = (category or "").strip()
    if not c:
        return ""
    if c in CAT_BADGE:
        return CAT_BADGE[c]
    # احتياطي: التصنيفات المخزّنة القديمة (slug أو نص)
    return CAT_BADGE.get(_canonical_category(category=c), "")


def _is_movie_cat(category: str) -> bool:
    """هل التصنيف الموحّد يعني فيلماً؟"""
    return (category or "").strip() in (CAT_MOVIES, CAT_DUB_MOVIES)


def _card_meta(s) -> str:
    """نص أسفل البطاقة: عدد الحلقات للمسلسل أو 'فيلم' للأفلام."""
    if _is_movie_cat(s.get("category")):
        return "🎬 فيلم"
    return f"{s.get('episodes_count', 0)} حلقة"


def _badges(s):
    out = ""
    if s.get("is_new"):
        out += '<span class="badge new">جديد</span>'
    label = _cat_label(s.get("category"))
    if label:
        out += f'<span class="badge genre">{label}</span>'
    sc = s.get("seasons_count") or 0
    if sc > 1:
        word = "أجزاء" if _is_movie_cat(s.get("category")) else "مواسم"
        out += f'<span class="badge seasons">{sc} {word}</span>'
    return out


def _watch_html(work, ch, stream_map):
    """يبني صفحة مشاهدة لعمل واحد (قد يحوي عدة مواسم).

    يُصدّر للمتصفح:
        window.SEASONS = [{season, eps:[{num,url}]}, ...]
        window.S        = حلقات الموسم الافتراضي (توافق خلفي)
        window.DEFAULT_SEASON = فهرس الموسم الافتراضي
    """
    name = work["name"]
    poster = work.get("local_poster") or "posters/placeholder.jpg"
    count = work.get("episodes_count", 0)
    seasons_count = work.get("seasons_count", 1)

    # ── كشف الفيلم: من التصنيف الموحّد ──
    cat = work.get("category", "")
    is_movie = cat in (CAT_MOVIES, CAT_DUB_MOVIES)
    season_word = "الجزء" if is_movie else "الموسم"
    seasons_word = "أجزاء" if is_movie else "مواسم"
    episode_word = "الفيلم" if is_movie else "الحلقة"
    episodes_word = "الأفلام" if is_movie else "الحلقات"
    kind = "movie" if is_movie else "series"
    eps_badge = ('<span class="badge eps">🎬 فيلم</span>' if is_movie
                 else f'<span class="badge eps">{count} حلقة</span>')

    seasons_payload = []
    total_playable = 0
    for season in work.get("seasons", []):
        s = season["series"]
        stream_eps = _lookup_stream(stream_map, s.get("name", ""))
        eps_list = []
        for e in s.get("episodes", []):
            try:
                num = int(e.get("num"))
            except Exception:
                continue
            info = stream_eps.get(num) or {}
            url = _stream_url(info.get("fid"), info.get("size"), info.get("mid"))
            eps_list.append({"num": num, "url": url})
        eps_list.sort(key=lambda x: x["num"])
        total_playable += sum(1 for e in eps_list if e["url"])
        seasons_payload.append({
            "season": int(season.get("season") or 1),
            "eps": eps_list,
        })

    # الموسم الافتراضي = أول موسم فيه حلقات قابلة للبث، وإلا الأول
    default_season = 0
    for i, sp in enumerate(seasons_payload):
        if any(e["url"] for e in sp["eps"]):
            default_season = i
            break

    first_eps = seasons_payload[default_season]["eps"] if seasons_payload else []
    seasons_json = json.dumps(seasons_payload, ensure_ascii=False)
    eps_json = json.dumps(first_eps, ensure_ascii=False)
    playable = total_playable
    season_badge = (f'<span class="badge seasons">{seasons_count} {seasons_word}</span>'
                    if seasons_count > 1 else "")

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
        {eps_badge}
        {season_badge}
        <span class="badge genre" id="current-ep-badge">{episode_word} 1</span>
      </div>
      <div class="player-controls">
        <button id="btn-prev">⏮ السابقة</button>
        <button id="btn-autoplay" class="active">🔁 تشغيل تلقائي: مفعّل</button>
        <button id="btn-next">التالية ⏭</button>
      </div>
    </div>
  </section>
  <section class="episodes-section">
    <div id="seasons-bar" class="seasons-bar"></div>
    <div class="episodes-header">
      <h2>{episodes_word} <span id="season-label" class="season-label"></span></h2>
      <div class="episodes-filter">
        <input id="ep-search" type="search" placeholder="ابحث عن رقم {episode_word}...">
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
<script>window.SEASONS = {seasons_json}; window.S = {eps_json}; window.DEFAULT_SEASON = {default_season}; window.C = {json.dumps(ch)}; window.PLAYABLE = {playable}; window.KIND = {json.dumps(kind)};</script>
<script src="../watch.js?v={ASSET_VER}"></script>
</body></html>"""


def _index_html(series, ch, built_at):
    rows = ""
    order = SECTION_ORDER
    icons = SECTION_ICONS
    by_sec = {k: [] for k in order}
    for s in series:
        sec = _section_of(_priority_of(s.get("name", ""), s.get("category", ""),
                                       s.get("source", "")))
        by_sec.setdefault(sec, []).append(s)

    for sec in order:
        items = by_sec.get(sec) or []
        if not items:
            continue
        cards = "".join(CARD.format(
            slug=_slug(s["name"]), name=s["name"],
            poster=s.get("local_poster", "posters/placeholder.jpg"),
            meta=_card_meta(s),
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
    if not (data and data.get("series")):
        data = _load_json(DATA_DIR / "series.json", None)
    series = (data or {}).get("series") or []
    # ★ توحيد التصنيفات عند التحميل (حتى لو كانت البيانات المخزّنة قديمة/مختلطة)
    for s in series:
        try:
            s["category"] = _canonical_category(
                s.get("name", ""), s.get("category", ""), s.get("source", ""))
        except Exception:
            pass
    return series


def build_site():
    print("\n🏗️  بناء الموقع من بيانات Telegram", flush=True)

    series = _load_tg_series()
    series = [s for s in series
              if s.get("name") and
              s.get("name") not in ("الصفحة الرئيسية", "جديد الأفلام")]

    if not series:
        print("⚠️ لا مسلسلات لبناء الموقع", flush=True)
        return

    # ── ★ دمج المواسم: تجميع المسلسلات في أعمال موحّدة ──
    works = _group_into_works(series)
    multi = sum(1 for w in works if w.get("seasons_count", 1) > 1)
    print(f"   🔗 {len(series)} مدخل → {len(works)} عمل "
          f"({multi} عمل متعدد المواسم)", flush=True)

    # ── الترتيب: الأولوية ثم الأحدث ──
    works.sort(key=lambda w: w.get("last_updated", "") or "", reverse=True)
    works.sort(key=lambda w: _priority_of(w.get("name", ""), w.get("category", ""),
                                          w.get("source", "")))

    # ── الصورة البديلة ──
    _ensure_placeholder()

    # ── تنزيل الصور ──
    print(f"   🖼️  تنزيل صور {len(works)} عمل...", flush=True)
    ok = 0
    for w in works:
        purl = w.get("poster", "")
        # ★ احتياطي: إن لم يوجد بوستر من المطابقة، نبحث عنه في yam بالاسم
        if not purl or not purl.startswith("http"):
            purl = _search_poster(w.get("name", ""))
        lp = _dl_poster(purl, w.get("name", ""))
        w["local_poster"] = lp
        if lp != "posters/placeholder.jpg":
            ok += 1
    print(f"   ✅ {ok}/{len(works)} صورة حقيقية", flush=True)

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
    for w in works:
        for season in w.get("seasons", []):
            stream_eps = _lookup_stream(stream_map, season["series"].get("name", ""))
            playable_total += len(stream_eps)
        slug = _slug(w["name"])
        with open(watch / f"{slug}.html", "w", encoding="utf-8") as fh:
            fh.write(_watch_html(w, ch, stream_map))

    # ── الصفحة الرئيسية ──
    built_at = datetime.now().strftime("%Y-%m-%d")
    with open(DOCS_DIR / "index.html", "w", encoding="utf-8") as fh:
        fh.write(_index_html(works, ch, built_at))

    # ── series.json (للبحث في app.js) ──
    meta = {"built_at": datetime.now().isoformat(), "series": [
        {
            "name": w["name"],
            "slug": _slug(w["name"]),
            "poster": w.get("local_poster", "posters/placeholder.jpg"),
            "episodes_count": w.get("episodes_count", 0),
            "eps": w.get("episodes_count", 0),
            "seasons_count": w.get("seasons_count", 1),
            "seasons": [s.get("season") for s in w.get("seasons", [])],
            "genre": w.get("genre", ""),
            "category": w.get("category", ""),
            "last_updated": w.get("last_updated", ""),
        }
        for w in works
    ]}
    with open(DOCS_DIR / "series.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    # ── videos.json + config.js ──
    _write_videos_json()
    _write_config_js()

    # إحصاء الأقسام
    counts = {}
    for w in works:
        sec = _section_of(_priority_of(w.get("name", ""), w.get("category", ""),
                                       w.get("source", "")))
        counts[sec] = counts.get(sec, 0) + 1

    print(f"✅ {len(works)} عمل | {playable_total} حلقة قابلة للبث | "
          f"صور: {ok}", flush=True)
    for sec in SECTION_ORDER:
        if counts.get(sec):
            print(f"      · {SECTION_ICONS.get(sec, '')} {sec}: {counts[sec]}", flush=True)


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
