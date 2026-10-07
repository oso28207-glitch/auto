"""yam.ahwaktv.net — يستخرج see.php?vid=XXX"""
import os, re
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests as cffi
from bs4 import BeautifulSoup

BASE = os.environ.get("YAM_BASE_URL", "https://yam.ahwaktv.net").rstrip("/")
SERIES_PATH = os.environ.get("YAM_SERIES_PATH", "/moslslat.php")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

SKIP = {"الصفحة الرئيسية", "الرئيسية", "جديد الأفلام", "أحدث الحلقات",
        "المسلسلات", "الأفلام", "اهواك تي في", "اتصل بنا", "من نحن"}

SEE_RE = re.compile(r'see\.php\?vid=([A-Za-z0-9]+)', re.I)
WATCH_RE = re.compile(r'watch\.php\?vid=([A-Za-z0-9]+)', re.I)


def _get(url, timeout=25):
    try:
        return cffi.get(url, impersonate="chrome120", timeout=timeout, verify=False,
                        headers={"User-Agent": UA, "Accept-Language": "ar,en;q=0.9"})
    except: return None


def _clean(n):
    if not n: return ""
    n = n.strip()
    n = re.sub(r'\s*[-–—]\s*اهواك.*$', '', n).strip()
    n = re.sub(r'\s*\|.*$', '', n).strip()
    return n


def _fetch_list():
    r = _get(f"{BASE}{SERIES_PATH}", 20)
    if not r or r.status_code != 200: return []
    soup = BeautifulSoup(r.text, "html.parser")
    links, seen = [], set()
    dom = urlparse(BASE).netloc
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        t = _clean(a.get_text(strip=True))
        if not h or h.startswith(("#", "javascript:", "mailto:")): continue
        if t in SKIP or len(t) < 5: continue
        f = urljoin(BASE, h)
        if dom not in f: continue
        if any(x in f for x in ["moslslat.php", "topvideos.php", "?page=", "see.php", "watch.php", "/category/", "/tag/", "/actor/"]): continue
        if f in seen: continue
        seen.add(f)
        links.append({"url": f, "name": t})
    return links


def _parse(url):
    r = _get(url, 20)
    if not r or r.status_code != 200: return None
    soup = BeautifulSoup(r.text, "html.parser")
    name = ""
    h1 = soup.find("h1")
    if h1: name = _clean(h1.get_text(strip=True))
    poster = ""
    og = soup.find("meta", property="og:image")
    if og: poster = og["content"]
    eps = {}
    for a in soup.find_all("a", href=True):
        h = a["href"]
        sm = SEE_RE.search(h)
        wm = WATCH_RE.search(h)
        if not sm and not wm: continue
        vid = (sm or wm).group(1)
        text = a.get_text(strip=True)
        m = re.search(r'(?:الحلقة|حلقة)[\s\-_]*?(\d{1,4})', text + " " + h)
        num = int(m.group(1)) if m else len(eps) + 1
        if num in eps: continue
        eps[num] = f"{BASE}/see.php?vid={vid}"
    if not eps: return None
    return {"name": name or url.rstrip("/").split("/")[-1], "poster": poster, "genre": "",
            "episodes": [{"num": n, "url": eps[n]} for n in sorted(eps)], "source_url": url}


def fetch():
    links = _fetch_list()
    if not links: return []
    print(f"   🔄 {len(links)} مسلسل...", flush=True)
    series = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(_parse, i["url"]) for i in links]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                s = f.result()
                if s and s["episodes"]:
                    series.append(s)
                    print(f"      [{i}/{len(links)}] ✅ {s['name'][:50]}: {len(s['episodes'])}", flush=True)
            except: pass
    return series