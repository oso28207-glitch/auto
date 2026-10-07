"""u3seq source — WordPress REST API على u.3seq.cam"""
import os
import re
import html
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as cffi

BASE = os.environ.get("SOURCE_BASE_URL", "https://u.3seq.cam").rstrip("/")
CATEGORY = int(os.environ.get("SOURCE_CATEGORY", "712"))
MAX_PAGES = int(os.environ.get("SOURCE_MAX_PAGES", "20"))
PER_PAGE = 100

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _get(url, timeout=30):
    try:
        return cffi.get(url, impersonate="chrome120", timeout=timeout,
                        verify=False,
                        headers={"User-Agent": UA,
                                 "Accept": "application/json,text/html,*/*"})
    except Exception:
        return None


def _fetch_posts_page(page):
    url = (f"{BASE}/wp-json/wp/v2/posts"
           f"?categories={CATEGORY}&per_page={PER_PAGE}&page={page}"
           f"&_fields=id,link,title,content,date")
    r = _get(url)
    if not r or r.status_code != 200:
        return []
    try:
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _extract_image(content_html):
    """استخراج أول صورة من محتوى المنشور."""
    if not content_html:
        return ""
    m = re.search(
        r'<img[^>]+src=["\']([^"\']+\.(?:jpg|jpeg|png|webp))["\']',
        content_html, re.I)
    if m:
        return m.group(1)
    return ""


def _extract_slug(link):
    m = re.search(r'/([^/]+?)/?$', link or "")
    return m.group(1) if m else ""


def _extract_episodes_from_html(html_text):
    """
    استخراج جميع روابط الحلقات من صفحة المسلسل.
    الأنماط المدعومة:
      - /video/modablaj-{slug}-episode-{num}
      - /video/...-episode-NN
      - episode-NN
    """
    episodes = {}

    patterns = [
        r'href=["\']([^"\']*?/video/[^"\']*?episode[-_](\d+)[^"\']*)["\']',
        r'href=["\']([^"\']*?episode[-_](\d+)[^"\']*)["\']',
    ]

    for pat in patterns:
        for m in re.finditer(pat, html_text, re.I):
            raw_url = m.group(1)
            try:
                num = int(m.group(2))
            except ValueError:
                continue
            if num < 1 or num > 5000:
                continue
            if raw_url.startswith("http"):
                full = raw_url
            elif raw_url.startswith("/"):
                full = BASE + raw_url
            else:
                full = f"{BASE}/{raw_url}"
            if num not in episodes:
                episodes[num] = full
        if episodes:
            break

    return [{"num": n, "url": episodes[n]} for n in sorted(episodes)]


def _fetch_post_details(link):
    r = _get(link, timeout=30)
    if not r or r.status_code != 200:
        return []
    return _extract_episodes_from_html(r.text)


def fetch():
    """الواجهة العامة — ترجع قائمة مسلسلات."""
    print(f"   📄 جلب {MAX_PAGES} صفحة...")

    all_posts = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_posts_page, p): p
                   for p in range(1, MAX_PAGES + 1)}
        for fut in as_completed(futures):
            page = futures[fut]
            try:
                posts = fut.result() or []
                if posts:
                    all_posts.extend(posts)
                    print(f"   صفحة {page}: {len(posts)} منشور")
            except Exception:
                pass

    if not all_posts:
        print(f"   ⚠️ لا منشورات")
        return []

    print(f"   📊 إجمالي: {len(all_posts)} منشور")

    # استخراج المسلسلات الفريدة
    seen_slugs = set()
    unique_posts = []
    for post in all_posts:
        link = post.get("link", "")
        slug = _extract_slug(link)
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        unique_posts.append({
            "slug": slug,
            "link": link,
            "title": html.unescape(post.get("title", {}).get("rendered", "")),
            "content": post.get("content", {}).get("rendered", ""),
        })

    print(f"   🎬 مسلسلات فريدة: {len(unique_posts)}")
    if not unique_posts:
        return []

    print(f"   🔄 جلب تفاصيل {len(unique_posts)} مسلسل...")

    def _enrich(p):
        poster = _extract_image(p["content"])
        episodes = _fetch_post_details(p["link"])
        return {
            "name": p["title"].strip() or p["slug"],
            "poster": poster,
            "genre": "",
            "episodes": episodes,
            "source_url": p["link"],
        }

    series_list = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(_enrich, p) for p in unique_posts]
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                s = fut.result()
                if s["episodes"]:
                    series_list.append(s)
                    print(f"      [{done}/{len(unique_posts)}] "
                          f"✅ {s['name'][:50]}: {len(s['episodes'])} حلقة")
                else:
                    print(f"      [{done}/{len(unique_posts)}] "
                          f"⚠️ {s['name'][:50]}: لا حلقات")
            except Exception as e:
                print(f"      [{done}] ❌ {str(e)[:80]}")

    return series_list