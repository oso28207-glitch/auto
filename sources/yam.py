"""yam.ahwaktv.net — يستخرج المسلسلات والصور من موقع اهواك تي في.

★ v6 (تحسين الزحف وتغطية المدبلج):
    - ترقيم كامل لصفحة المسلسلات (moslslat.php) حتى YAM_MAX_PAGES (افتراضي 95)
      بدلاً من 8 فقط → تغطية ~4000 مسلسل بدل 320.
    - ★ زحف تصنيفات المدبلج (moslslat-turkiaa-modblga, moslslat-modblga, aflam-dub,
      moslslat-hndia-modblja, goda-akbar-modblge, mn-elnazra-elthania-modblge) —
      لأن قنوات تيليجرام محتواها مدبلج، وهذه التصنيفات هي المصدر المطابق.
    - استخراج اسم المسلسل + رقم الحلقة + البوستر من بطاقات التصنيف مباشرةً.
    - استخراج الصور من data-echo (lazy-load) — كان og:image فارغاً دائماً.
    - استخراج الحلقات من watch.php?vid=XXX.
"""
import os
import re
from urllib.parse import urljoin, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as cffi
from bs4 import BeautifulSoup

BASE = os.environ.get("YAM_BASE_URL", "https://yam.ahwaktv.net").rstrip("/")
SERIES_PATH = os.environ.get("YAM_SERIES_PATH", "/moslslat.php")
MAX_PAGES = int(os.environ.get("YAM_MAX_PAGES", "95"))
CATEGORY_PAGES = int(os.environ.get("YAM_CATEGORY_PAGES", "40"))
SERIES_FETCH_LIMIT = int(os.environ.get("YAM_SERIES_FETCH_LIMIT", "900"))
WORKERS = int(os.environ.get("YAM_WORKERS", "10"))

_DEFAULT_CATS = (
    "moslslat-turkiaa-modblga,moslslat-modblga,aflam-dub,"
    "moslslat-hndia-modblja,goda-akbar-modblge,mn-elnazra-elthania-modblge"
)
CATEGORIES = [c.strip() for c in os.environ.get("YAM_CATEGORIES", _DEFAULT_CATS).split(",") if c.strip()]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SKIP = {
    "الصفحة الرئيسية", "الرئيسية", "جديد الأفلام", "أحدث الحلقات",
    "المسلسلات", "الأفلام", "اهواك تي في", "اتصل بنا", "من نحن",
    "سياسة الخصوصية", "شروط الاستخدام", "DMCA", "حقوق النشر",
    "أفلام", "مسلسلات", "مسرحيات", "برامج", "عرض المزيد", "المزيد",
    "تسجيل دخول", "تسجيل", "دخول", "حسابي", "بحث", "Categories",
}

# رابط بطاقة المسلسل
SERIE_RE = re.compile(r'view-serie\.php\?name=([^"&\s]+)', re.I)
# رابط الحلقة
WATCH_RE = re.compile(r'watch\.php\?vid=([A-Za-z0-9]+)', re.I)
SEE_RE = re.compile(r'see\.php\?vid=([A-Za-z0-9]+)', re.I)
EP_RE = re.compile(
    r'(?:الحلقة|حلقة|الحلقه)[\s\-_:]*?(\d{1,4})', re.I)

LAZY_PLACEHOLDERS = ("echo-lzld", "lazy", "placeholder", "blank", "1x1")

# كلمات تُشير إلى محتوى مدبلج/تركي (لأولوية جلب الحلقات)
DUBBED_HINT = ("مدبلج", "مدبلجة", "turkish", "تركي")


def _get(url, timeout=25):
    for imp in ("chrome120", "chrome110"):
        try:
            r = cffi.get(
                url, impersonate=imp, timeout=timeout, verify=False,
                headers={"User-Agent": UA, "Accept-Language": "ar,en;q=0.9"},
            )
            if r.status_code == 200:
                return r
        except Exception:
            continue
    return None


def _clean(n):
    if not n:
        return ""
    n = n.strip()
    n = re.sub(r'\s*[-–—]\s*اهواك.*$', '', n).strip()
    n = re.sub(r'\s*\|.*$', '', n).strip()
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def _clean_series_hint(text: str) -> str:
    if not text:
        return ""
    t = re.sub(
        r'\s*(?:الحلقة|حلقة|الحلقه|Episode|Ep\.?)\s*[\-_:]?\s*\d+.*$',
        '', text, flags=re.I).strip()
    t = re.sub(r'\s*(\d+)\s*(?:الاخيرة|الأخيرة|والاخيرة|والأخيرة)?\s*$', '', t).strip()
    t = re.sub(r'^[📺🎬🎥🎞️🔹\-\•\s]+', '', t).strip()
    return t


def _clean_series_name(title: str) -> str:
    """يحوّل عنوان بطاقة حلقة إلى اسم مسلسل نظيف."""
    t = _clean_series_hint(title)
    # إزالة لاحقات الجودة/الدبلجة
    t = re.sub(r'\s*(?:مدبلجة|مدبلج|مترجمة|مترجم|كاملة|كامل|HD|HQ)\s*$',
               '', t, flags=re.I).strip()
    t = re.sub(r'\s*(?:مدبلجة|مترجمة)\s*(?:HD|HQ)\s*$', '', t, flags=re.I).strip()
    t = re.sub(r'\s*(?:HD|HQ)\s*$', '', t, flags=re.I).strip()
    t = re.sub(r'^مسلسل\s+', '', t).strip()
    t = re.sub(r'^فيلم\s+', '', t).strip()
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _extract_ep_num(text: str) -> int:
    if not text:
        return 0
    m = EP_RE.search(text)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return 0
    return 0


def _pick_poster(img) -> str:
    """يستخرج رابط البوستر من img مع دعم lazy-load (data-echo)."""
    if img is None:
        return ""
    for attr in ("data-echo", "data-src", "data-lazy", "data-original", "src"):
        val = (img.get(attr) or "").strip()
        if not val:
            continue
        if any(p in val.lower() for p in LAZY_PLACEHOLDERS):
            continue
        if val.startswith("//"):
            val = "https:" + val
        elif not val.startswith("http"):
            val = urljoin(BASE + "/", val)
        return val
    return ""


def _poster_near(a, levels=4) -> str:
    """يبحث عن صورة داخل البطاقة المحيطة بالرابط."""
    parent = a
    for _ in range(levels):
        parent = parent.parent
        if parent is None:
            break
        img = parent.find("img")
        if img:
            p = _pick_poster(img)
            if p:
                return p
    return ""


# ══════════════════════════════════════════════════════════════════════
# 1) صفحة القائمة (moslslat.php) — البطاقات + الصور + الترقيم
# ══════════════════════════════════════════════════════════════════════
def _parse_list_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    cards, seen = [], set()

    for a in soup.find_all("a", href=True):
        m = SERIE_RE.search(a["href"])
        if not m:
            continue
        url = urljoin(BASE + "/", a["href"])
        if url in seen:
            continue

        name = _clean(a.get("title") or a.get_text(" ", strip=True))
        if not name or len(name) < 3 or name in SKIP:
            continue

        poster = _poster_near(a)
        seen.add(url)
        cards.append({"url": url, "name": name, "poster": poster})

    return cards


def _fetch_list():
    """يجلب كل بطاقات المسلسلات مع صورها عبر كل الصفحات."""
    all_cards = []
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = f"{BASE}{SERIES_PATH}"
        else:
            url = f"{BASE}{SERIES_PATH}?&page={page}"

        r = _get(url, 25)
        if not r or r.status_code != 200:
            break

        cards = _parse_list_page(r.text)
        new = [c for c in cards if c["url"] not in seen]
        if not new:
            break
        for c in new:
            seen.add(c["url"])
        all_cards.extend(new)
        print(f"      yam page {page}: {len(new)} مسلسل (المجموع {len(all_cards)})",
              flush=True)

        if len(cards) < 10:
            break

    return all_cards


# ══════════════════════════════════════════════════════════════════════
# 2) تصنيفات المدبلج (category.php) — الحلقات + الصور مباشرة
# ══════════════════════════════════════════════════════════════════════
def _parse_category_page(html: str):
    """يستخرج {اسم المسلسل: {poster, episodes:{num:url}}} من صفحة تصنيف."""
    soup = BeautifulSoup(html, "html.parser")
    series = {}

    for a in soup.find_all("a", href=True):
        h = a["href"]
        m = WATCH_RE.search(h) or SEE_RE.search(h)
        if not m:
            continue
        vid = m.group(1)
        title = (a.get("title") or a.get_text(" ", strip=True)).strip()
        name = _clean_series_name(title)
        if not name or len(name) < 3 or name in SKIP:
            continue

        num = _extract_ep_num(title)
        if not num:
            num = _extract_ep_num(a.get("title", ""))
        if not num:
            continue

        entry = series.setdefault(name, {
            "name": name, "poster": "", "genre": "", "category": "",
            "episodes": {}, "source_url": "", "dubbed": False,
        })
        low = title.lower()
        if "مدبلج" in low or "مدبلجة" in low:
            entry["dubbed"] = True
        if not entry["poster"]:
            p = _poster_near(a)
            if p:
                entry["poster"] = p
        entry["episodes"][num] = f"{BASE}/watch.php?vid={vid}"

    return series


def _fetch_categories():
    """يزحف تصنيفات المدبلج ويجمع المسلسلات + الحلقات + الصور."""
    merged = {}
    for cat in CATEGORIES:
        cat_count = 0
        for page in range(1, CATEGORY_PAGES + 1):
            url = f"{BASE}/category.php?cat={cat}&page={page}&order=DESC"
            r = _get(url, 25)
            if not r or r.status_code != 200:
                break
            page_series = _parse_category_page(r.text)
            if not page_series:
                break
            for name, s in page_series.items():
                tgt = merged.setdefault(name, {
                    "name": name, "poster": "", "genre": "", "category": cat,
                    "episodes": {}, "source_url": "",
                })
                if s.get("poster") and not tgt["poster"]:
                    tgt["poster"] = s["poster"]
                tgt["episodes"].update(s["episodes"])
                cat_count += len(s["episodes"])
            # توقف إذا كانت الصفحة أقل من 20 حلقة (آخر صفحة)
            total_eps = sum(len(v["episodes"]) for v in page_series.values())
            if total_eps < 20:
                break
        if cat_count:
            print(f"      yam cat {cat}: {cat_count} حلقة", flush=True)

    return merged


# ══════════════════════════════════════════════════════════════════════
# 3) صفحة المسلسل (view-serie.php) — الحلقات + البوستر + التصنيف
# ══════════════════════════════════════════════════════════════════════
def _parse_series_page(url, name="", list_poster="", category=""):
    r = _get(url, 25)
    if not r or r.status_code != 200:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    page_name = name
    h1 = soup.find("h1")
    if h1:
        t = _clean(h1.get_text(" ", strip=True))
        if t and t not in SKIP:
            page_name = t

    poster = list_poster
    for img in soup.find_all("img"):
        p = _pick_poster(img)
        if p:
            poster = p
            break

    if not category:
        GENERIC = {"categories", "category", "التصنيفات", "التصنيف", "الاقسام",
                   "الأقسام", "اقسام", "أقسام"}
        bc = soup.find("a", href=lambda x: x and "category.php" in x)
        if bc:
            c = _clean(bc.get_text(" ", strip=True))
            category = c if c and c.lower() not in GENERIC else ""
        else:
            category = ""

    eps = {}
    for a in soup.find_all("a", href=True):
        h = a["href"]
        m = WATCH_RE.search(h) or SEE_RE.search(h)
        if not m:
            continue
        vid = m.group(1)
        text = a.get_text(" ", strip=True) or a.get("title", "")
        num = _extract_ep_num(text)
        if not num:
            num = _extract_ep_num(a.get("title", ""))
        if not num:
            num = len(eps) + 1
        while num in eps:
            num += 1
        eps[num] = f"{BASE}/watch.php?vid={vid}"

    if not eps:
        return None

    last_updated = f"2026-01-01T{min(max(eps.keys()), 23):02d}:00:00"

    return {
        "name": page_name or url.rstrip("/").split("/")[-1],
        "poster": poster,
        "genre": "",
        "category": category,
        "episodes": [{"num": n, "url": eps[n]} for n in sorted(eps)],
        "last_updated": last_updated,
        "source_url": url,
    }


def _is_dubbed_hint(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in DUBBED_HINT)


# ══════════════════════════════════════════════════════════════════════════════
# 4) البحث المباشر (search.php) — لتطابق أسماء قنوات تيليجرام بدقّة
#    ★ اكتشاف: search.php?keywords=XXX يعيد بطاقات حلقات بنفس بنية صفحة التصنيف
#      (title = اسم المسلسل + رقم الحلقة، data-echo = البوستر) — فنُعيد استخدام
#      _parse_category_page ثم نختار أفضل تطابق بالاسم عبر names.similarity.
# ══════════════════════════════════════════════════════════════════════════════
_SEARCH_CACHE = {}


def search_series(query: str, threshold: float = 0.70):
    """يبحث في yam عن مسلسل بالاسم ويعيد أفضل تطابق (name, poster, episodes) أو None.

    يُستخدم كطبقة احتياطية عندما لا يُعثر على المسلسل في القائمة/التصنيفات المزحوفة.
    """
    if not query or len(query.strip()) < 3:
        return None

    key = query.strip()
    if key in _SEARCH_CACHE:
        return _SEARCH_CACHE[key]

    from names import norm as _n, similarity as _sim

    url = f"{BASE}/search.php?keywords={quote(key)}"
    r = _get(url, 25)
    result = None

    if r and r.status_code == 200:
        merged = _parse_category_page(r.text)
        qn = _n(key)
        best, best_score = None, 0.0
        for name, s in merged.items():
            if not s.get("episodes"):
                continue
            if qn and _n(name) == qn:
                best, best_score = s, 1.0
                break
            sc = _sim(key, name)
            if sc > best_score:
                best_score, best = sc, s
        if best is not None and best_score >= threshold:
            best["episodes"] = [{"num": n, "url": u}
                                for n, u in sorted(best["episodes"].items())]
            best["last_updated"] = "2026-01-01T00:00:00"
            best["source_url"] = url
            # تصنيف القسم بناءً على حالة الدبلجة في البطاقات
            best["category"] = ("مسلسلات مدبلجة" if best.get("dubbed")
                                else "مسلسلات مترجمة")
            result = best

    _SEARCH_CACHE[key] = result
    return result


# ══════════════════════════════════════════════════════════════════════
# الواجهة الرئيسية
# ══════════════════════════════════════════════════════════════════════
def fetch():
    all_series = []
    seen_names = set()

    # 1) تصنيفات المدبلج (الأعلى قيمة — تطابق قنوات تيليجرام)
    cat_series = _fetch_categories()
    for s in cat_series.values():
        if not s.get("episodes"):
            continue
        key = s["name"].strip().lower()
        if not key or key in seen_names:
            continue
        seen_names.add(key)
        s["episodes"] = [{"num": n, "url": u}
                         for n, u in sorted(s["episodes"].items())]
        s["last_updated"] = "2026-01-01T00:00:00"
        all_series.append(s)
    print(f"      ✅ yam cats: {len(all_series)} مسلسل مدبلج", flush=True)

    # 2) صفحة القائمة الكاملة
    cards = _fetch_list()
    if not cards:
        print("      ⚠️ yam: لم يتم العثور على بطاقات", flush=True)
        return all_series

    # أولوية: المدبلج/التركي أولاً ثم الباقي (مع سقف لعدد صفحات المسلسلات)
    dubbed = [c for c in cards if _is_dubbed_hint(c["name"])]
    others = [c for c in cards if not _is_dubbed_hint(c["name"])]
    ordered = (dubbed + others)[:SERIES_FETCH_LIMIT]

    print(f"      yam: {len(cards)} بطاقة، جلب حلقات {len(ordered)} "
          f"(مدبلج {len(dubbed)})", flush=True)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {
            ex.submit(_parse_series_page, c["url"], c["name"], c["poster"]): c
            for c in ordered
        }
        for f in as_completed(futs):
            try:
                s = f.result()
                if not s or not s.get("episodes"):
                    continue
                key = (s.get("name") or "").strip().lower()
                if not key or key in seen_names:
                    continue
                seen_names.add(key)
                all_series.append(s)
            except Exception:
                pass

    with_poster = sum(1 for s in all_series if s.get("poster"))
    print(f"      ✅ yam: {len(all_series)} مسلسل ({with_poster} مع صور)",
          flush=True)
    return all_series


if __name__ == "__main__":
    series = fetch()
    print(f"\n📊 النتيجة: {len(series)} مسلسل")
    for s in series[:10]:
        flag = "🖼️" if s.get("poster") else "❌"
        print(f"   · {flag} {s['name']}: {len(s['episodes'])} حلقة | "
              f"{s.get('category', '')[:30]}")
