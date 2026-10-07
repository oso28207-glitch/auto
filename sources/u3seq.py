"""u3seq (عشق) — يجرب نطاقات ومسارات متعددة."""
import os, re, html
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests as cffi
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

DOMAINS = [
    os.environ.get("SOURCE_BASE_URL", "https://u.3seq.cam").rstrip("/"),
    "https://u.3seq.com", "https://3seq.cam",
]


def _get(url, timeout=25):
    try:
        return cffi.get(url, impersonate="chrome120", timeout=timeout, verify=False,
                        headers={"User-Agent": UA, "Accept-Language": "ar,en;q=0.9"})
    except: return None


def _clean(n):
    if not n: return ""
    n = html.unescape(n).strip()
    n = re.sub(r'\s*[-–—]\s*الحلقة\s*\d+.*$', '', n).strip()
    n = re.sub(r'\s*الحلقة\s*\d+\s*(مدبلجة|مترجمة)?\s*$', '', n).strip()
    return n


def _try_domain():
    for d in DOMAINS:
        r = _get(d + "/", 15)
        if r and r.status_code == 200 and len(r.text) > 5000:
            if "Just a moment" in r.text: continue
            print(f"   ✅ {d}")
            return d
    return None


def _find_archive(base):
    for p in ["/video/series/", "/video/series/page/1/", "/series/"]:
        r = _get(base + p, 20)
        if not r or r.status_code != 200: continue
        if "modablaj" not in r.text: continue
        soup = BeautifulSoup(r.text, "html.parser")
        urls = set()
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if "/video/series/" in h or "/series/" in h:
                f = urljoin(base, h).rstrip("/")
                if "/page/" in f or f.endswith("/video/series") or f.endswith("/series"): continue
                urls.add(f)
        if urls:
            print(f"   ✅ {p} ({len(urls)})")
            return p, urls
    return None, set()


def _parse(url):
    r = _get(url, 20)
    if not r or r.status_code != 200: return None
    soup = BeautifulSoup(r.text, "html.parser")

    name = ""
    h1 = soup.find("h1")
    if h1: name = _clean(h1.get_text(strip=True))
    if not name:
        og = soup.find("meta", property="og:title")
        if og: name = _clean(og.get("content", ""))

    poster = ""
    og = soup.find("meta", property="og:image")
    if og: poster = og["content"]

    eps = []
    seen = set()
    eplist = soup.find("ul", class_="eplist")
    if eplist:
        for a in eplist.find_all("a", class_="epNum", href=True):
            sp = a.find("span")
            if not sp: continue
            try: num = int(sp.get_text(strip=True))
            except: continue
            if num in seen or num < 1: continue
            seen.add(num)
            pu = urljoin(url, a["href"])
            if not pu.endswith("/"): pu += "/"
            eps.append({"num": num, "url": pu + "?do=watch"})

    if not eps: return None
    return {"name": name or url.rstrip("/").split("/")[-1], "poster": poster, "genre": "",
            "episodes": sorted(eps, key=lambda x: x["num"]), "source_url": url}


def fetch():
    base = _try_domain()
    if not base: return []
    path, urls = _find_archive(base)
    if not urls:
        print("   ⚠️ لا مسلسلات")
        return []

    # صفحات إضافية
    if path:
        for pg in range(2, 6):
            pu = base + path.rstrip("/").split("/page/")[0] + f"/page/{pg}/"
            r = _get(pu, 15)
            if not r or r.status_code != 200: break
            soup = BeautifulSoup(r.text, "html.parser")
            new = 0
            for a in soup.find_all("a", href=True):
                h = a["href"]
                if "/video/series/" in h:
                    f = urljoin(base, h).rstrip("/")
                    if f not in urls: urls.add(f); new += 1
            if new == 0: break

    print(f"   🔄 جلب {len(urls)} مسلسل...", flush=True)
    series = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_parse, u) for u in urls]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                s = f.result()
                if s and s["episodes"]:
                    series.append(s)
                    print(f"      [{i}/{len(urls)}] ✅ {s['name'][:50]}: {len(s['episodes'])}", flush=True)
            except: pass
    return series