"""yam.ahwaktv.net — يستخرج see.php?vid=XXX

★ v2: إصلاح جذري — لا يعتبر التصنيفات مسلسلات.
     - صفحة التصنيف (تصنيفات مثل "مسلسلات تركية مدبلجة") تحتوي على عدة مسلسلات مختلفة
     - نكتشف ذلك من خلال فحص "series_hint" في نصوص روابط see.php
     - إذا كانت hints متعددة → تصنيف → نزحف لكل مسلسل بداخله
     - إذا كانت hint واحدة → مسلسل حقيقي → استخرج حلقاته مباشرة
"""
import os
import re
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as cffi
from bs4 import BeautifulSoup

BASE = os.environ.get("YAM_BASE_URL", "https://yam.ahwaktv.net").rstrip("/")
SERIES_PATH = os.environ.get("YAM_SERIES_PATH", "/moslslat.php")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SKIP = {
    "الصفحة الرئيسية", "الرئيسية", "جديد الأفلام", "أحدث الحلقات",
    "المسلسلات", "الأفلام", "اهواك تي في", "اتصل بنا", "من نحن",
    "سياسة الخصوصية", "شروط الاستخدام", "DMCA", "حقوق النشر",
    "أفلام", "مسلسلات", "مسرحيات", "برامج", "عرض المزيد", "المزيد",
}

# روابط يجب تجنبها
BAD_URL_PARTS = [
    "see.php", "watch.php", "moslslat.php", "topvideos.php",
    "?page=", "/category/", "/tag/", "/actor/",
    "index.php", "?do=", "?cat=", "?p=", "?page=",
    "javascript:", "#", "mailto:", "tel:",
]

SEE_RE = re.compile(r'see\.php\?vid=([A-Za-z0-9]+)', re.I)
WATCH_RE = re.compile(r'watch\.php\?vid=([A-Za-z0-9]+)', re.I)
EP_RE = re.compile(r'(?:الحلقة|حلقة|الحلقه)[\s\-_:]*?(\d{1,4})', re.I)


def _get(url, timeout=25):
    try:
        return cffi.get(
            url, impersonate="chrome120", timeout=timeout, verify=False,
            headers={"User-Agent": UA, "Accept-Language": "ar,en;q=0.9"},
        )
    except Exception:
        return None


def _clean(n):
    if not n:
        return ""
    n = n.strip()
    n = re.sub(r'\s*[-–—]\s*اهواك.*$', '', n).strip()
    n = re.sub(r'\s*\|.*$', '', n).strip()
    return n


def _clean_series_hint(text: str) -> str:
    """يستخرج اسم المسلسل من نص الرابط (يزيل رقم الحلقة)."""
    if not text:
        return ""
    # إزالة "الحلقة X" من أي مكان
    t = re.sub(r'\s*(?:الحلقة|حلقة|الحلقه|Episode|Ep\.?)\s*[\-_:]?\s*\d+.*$',
               '', text, flags=re.I).strip()
    # إزالة الأرقام الباقية في النهاية
    t = re.sub(r'\s*\d+\s*$', '', t).strip()
    # إزالة الرموز في البداية
    t = re.sub(r'^[📺🎬🎥🎞️🔹\-•\s]+', '', t).strip()
    return t


def _extract_ep_num(text: str) -> int:
    """يستخرج رقم الحلقة من النص، أو 0."""
    if not text:
        return 0
    m = EP_RE.search(text)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return 0
    return 0


# ═══════════════════════════════════════════════════════════════
# قائمة الروابط من moslslat.php
# ═══════════════════════════════════════════════════════════════
def _fetch_list():
    r = _get(f"{BASE}{SERIES_PATH}", 25)
    if not r or r.status_code != 200:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    links, seen = [], set()
    dom = urlparse(BASE).netloc

    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        t = _clean(a.get_text(strip=True))

        if not h or h.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        if t in SKIP or len(t) < 4:
            continue

        f = urljoin(BASE, h)
        if dom not in f:
            continue

        # تجاهل الروابط المعروفة
        if any(x in f for x in BAD_URL_PARTS):
            continue

        # تجاهل الروابط الخارجية
        if urlparse(f).netloc != dom:
            continue

        if f in seen:
            continue
        seen.add(f)
        links.append({"url": f, "name": t})

    return links


# ═══════════════════════════════════════════════════════════════
# فحص صفحة واحدة: مسلسل أم تصنيف؟
# ═══════════════════════════════════════════════════════════════
def _parse(url, depth=0, visited=None):
    """
    يعيد:
      - None إذا فشل
      - dict إذا كان مسلسلاً حقيقياً (بحلقات)
      - list[dict] إذا كان تصنيفاً (نزحف بداخله)
    """
    if visited is None:
        visited = set()

    if url in visited or depth > 2:
        return None
    visited.add(url)

    r = _get(url, 20)
    if not r or r.status_code != 200:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # ── الاسم الأساسي ──
    page_name = ""
    h1 = soup.find("h1")
    if h1:
        page_name = _clean(h1.get_text(strip=True))

    # ── البوستر ──
    poster = ""
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        poster = og["content"]

    # ── جمع روابط الحلقات مع hints ──
    candidates = []  # كل عنصر: {vid, ep_num, hint, text}
    for a in soup.find_all("a", href=True):
        h = a["href"]
        sm = SEE_RE.search(h)
        wm = WATCH_RE.search(h)
        if not sm and not wm:
            continue

        vid = (sm or wm).group(1)
        text = a.get_text(" ", strip=True)
        if not text:
            continue

        ep_num = _extract_ep_num(text)
        hint = _clean_series_hint(text)

        candidates.append({
            "vid": vid,
            "ep_num": ep_num,
            "hint": hint,
            "text": text,
        })

    if not candidates:
        return None

    # ── تحديد: مسلسل أم تصنيف؟ ──
    unique_hints = set(c["hint"] for c in candidates if c["hint"])
    unique_ep_nums = set(c["ep_num"] for c in candidates if c["ep_num"])

    is_series = True
    reason = ""

    # قاعدة 1: إذا كانت هناك hints متعددة → تصنيف
    if len(unique_hints) > 1:
        is_series = False
        reason = f"{len(unique_hints)} hints مختلفة"

    # قاعدة 2: إذا كان عدد الحلقات كبيراً (>30) و ep_nums قليلة → تصنيف
    if is_series and len(candidates) > 30 and len(unique_ep_nums) <= 2:
        is_series = False
        reason = f"{len(candidates)} رابط لكن {len(unique_ep_nums)} رقم حلقة فقط"

    # ═══════════════════════════════════════════════════════════
    # الحالة 1: مسلسل حقيقي — استخرج حلقاته
    # ═══════════════════════════════════════════════════════════
    if is_series:
        eps = {}
        for c in candidates:
            num = c["ep_num"] or (len(eps) + 1)
            while num in eps:
                num += 1
            eps[num] = f"{BASE}/see.php?vid={c['vid']}"

        return {
            "name": page_name or url.rstrip("/").split("/")[-1],
            "poster": poster,
            "genre": "",
            "episodes": [{"num": n, "url": eps[n]} for n in sorted(eps)],
            "source_url": url,
        }

    # ═══════════════════════════════════════════════════════════
    # الحالة 2: تصنيف — ابحث عن صفحات المسلسلات داخله
    # ═══════════════════════════════════════════════════════════
    print(f"      📂 تصنيف [{len(unique_hints) or len(candidates)}] "
          f"{page_name[:45]} ({reason})", flush=True)

    series_urls = {}  # url → name
    dom = urlparse(BASE).netloc

    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if not h or h.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        f = urljoin(BASE, h)
        if urlparse(f).netloc != dom:
            continue
        if any(x in f for x in BAD_URL_PARTS):
            continue
        if f == url or f in visited:
            continue

        text = _clean(a.get_text(" ", strip=True))
        if not text or len(text) < 4 or text in SKIP:
            continue

        # إذا كان النص يشابه one of the hints → هو رابط المسلسل
        # أو نأخذ كل رابط معقول
        if f in series_urls:
            continue
        series_urls[f] = text

    if not series_urls:
        # fallback: اعتبره مسلسلاً بالحلقات الموجودة
        print(f"      ⚠️ لا روابط مسلسلات — نعيد كمسلسل", flush=True)
        eps = {}
        for c in candidates:
            num = c["ep_num"] or (len(eps) + 1)
            while num in eps:
                num += 1
            eps[num] = f"{BASE}/see.php?vid={c['vid']}"
        return {
            "name": page_name or url.rstrip("/").split("/")[-1],
            "poster": poster,
            "genre": "",
            "episodes": [{"num": n, "url": eps[n]} for n in sorted(eps)],
            "source_url": url,
        }

    print(f"      🔎 نزحف {len(series_urls)} مسلسل داخله...", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_parse, u, depth + 1, visited): u
                for u in series_urls.keys()}
        for f in as_completed(futs):
            try:
                s = f.result()
                if isinstance(s, dict):
                    results.append(s)
                elif isinstance(s, list):
                    results.extend(s)
            except Exception:
                pass

    return results


# ═══════════════════════════════════════════════════════════════
# الواجهة الرئيسية
# ═══════════════════════════════════════════════════════════════
def fetch():
    links = _fetch_list()
    if not links:
        print("   ⚠️ لا روابط من moslslat.php", flush=True)
        return []

    print(f"   🔄 {len(links)} رابط رئيسي (تصنيفات + مسلسلات)...", flush=True)

    all_series = []
    seen_names = set()
    done = 0

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_parse, i["url"]): i for i in links}
        for f in as_completed(futs):
            done += 1
            try:
                result = f.result()

                # نتيجة واحدة (dict) أو متعددة (list)
                items = []
                if isinstance(result, dict):
                    items = [result]
                elif isinstance(result, list):
                    items = result

                for s in items:
                    if not s or not s.get("episodes"):
                        continue
                    key = (s.get("name") or "").strip().lower()
                    if not key or key in seen_names:
                        continue
                    seen_names.add(key)
                    all_series.append(s)
                    print(f"      [{done}/{len(links)}] ✅ "
                          f"{s['name'][:50]}: {len(s['episodes'])}",
                          flush=True)
            except Exception as e:
                pass

    print(f"   ✅ {len(all_series)} مسلسل حقيقي بعد المعالجة", flush=True)
    return all_series


if __name__ == "__main__":
    series = fetch()
    print(f"\n📊 النتيجة: {len(series)} مسلسل")
    for s in series[:10]:
        print(f"   · {s['name']}: {len(s['episodes'])} حلقة")