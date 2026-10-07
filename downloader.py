"""
downloader.py — Universal HLS Downloader v24 (FAST)
═══════════════════════════════════════════════════════════
الأداء:
  • جلسة واحدة لكل حلقة (v18.2)
  • التقاط m3u8 عبر Fetch/Network قبل التنقل (v20)
  • cffi segments أولاً (v18.2) — الأسرع
  • deep m3u8 resolution (v16.4)
  • yt-dlp fallback مع stall detection (v16.4)
"""

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin

from curl_cffi import requests as cffi_requests

from config import config, MEDIA_DIR

# ═══════════════════════════════════════════════════════════════
# إعدادات مُحسّنة للسرعة
# ═══════════════════════════════════════════════════════════════
MIN_SIZE = 100 * 1024
MIN_PARTIAL_ACCEPT = 30 * 1024 * 1024

# ★ قللنا المهل بشكل كبير
M3U8_SEARCH_TIMEOUT = int(os.environ.get("M3U8_SEARCH_TIMEOUT", "20"))
YTDLP_TIMEOUT = int(os.environ.get("YTDLP_TIMEOUT", "900"))
FFMPEG_HLS_TIMEOUT = int(os.environ.get("FFMPEG_HLS_TIMEOUT", "1800"))
OPEN_TIMEOUT = int(os.environ.get("OPEN_TIMEOUT", "15"))
STALL_TIMEOUT = int(os.environ.get("STALL_TIMEOUT", "60"))

BROWSER_BATCH = int(os.environ.get("BROWSER_BATCH", "20"))
BROWSER_BATCH_TIMEOUT = 20
M3U8_CANDIDATE_LIMIT = 5
CURL_CFFI_WORKERS = int(os.environ.get("CURL_CFFI_WORKERS", "16"))

JWPLAYER_WAIT = 12
CLICK_CYCLES = 3  # ★ من 8 إلى 3

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SERVER_PRIORITY = [
    "luluvdo", "vinovo", "vidsonic", "playmate", "firestream",
    "vidaraa", "vids", "bysejikuar", "vidsp", "savefiles", "voe",
]

_REQ_IDS = {}
_RESP_BODIES = {}
_NET_LOCK = threading.Lock()


class TimeoutError_(Exception):
    pass


# ═══════════════════════════════════════════════════════════════
# أدوات مساعدة
# ═══════════════════════════════════════════════════════════════
def run_with_timeout(func, args=(), kwargs=None, timeout=60, default=None):
    if kwargs is None: kwargs = {}
    result = [default]; exception = [None]; done = threading.Event()

    def _run():
        try: result[0] = func(*args, **kwargs)
        except Exception as e: exception[0] = e
        finally: done.set()

    threading.Thread(target=_run, daemon=True).start()
    if not done.wait(timeout=timeout): raise TimeoutError_(f"timeout {timeout}s")
    if exception[0]: raise exception[0]
    return result[0]


def _safe(name): return re.sub(r'[\\/:*?"<>|]', "_", name).strip()[:120]
def _origin(url):
    try: return f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    except Exception: return ""


def _get_ffmpeg_exe():
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if r.returncode == 0: return "ffmpeg"
    except Exception: pass
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception: pass
    return "ffmpeg"


# ═══════════════════════════════════════════════════════════════
# التحقق من URL
# ═══════════════════════════════════════════════════════════════
def _is_valid_episode_url(url):
    if not url or not isinstance(url, str): return False
    u = url.lower()
    bad = ["/moslslat.php", "/topvideos.php", "/all-series.php",
           "/series.php", "/category/", "/cats/", "/list/", "?cat="]
    for b in bad:
        if b in u: return False
    if "modablaj-" in u or "/video/" in u: return True
    if "see.php" in u and "vid=" in u: return True
    if "watch.php" in u and "vid=" in u: return True
    if "/watch/" in u and ".php" not in u: return True
    for m in ["/embed/", "firestream.to/", "playmate.to/",
              "luluvdo.com/", "vidsonic", "vidaraa", "/e/"]:
        if m in u: return True
    return False


# ═══════════════════════════════════════════════════════════════
# CDP helpers
# ═══════════════════════════════════════════════════════════════
def _cdp_cmd(sb, cmd, params=None):
    if params is None: params = {}
    try: return sb.driver.execute_cdp_cmd(cmd, params)
    except Exception: pass
    for meth in ("send_cdp_cmd", "execute_cdp_cmd"):
        try:
            fn = getattr(sb.cdp, meth, None)
            if fn: return fn(cmd, params)
        except Exception: pass
    return None


# ═══════════════════════════════════════════════════════════════
# التقاط m3u8 عبر Fetch + Network
# ═══════════════════════════════════════════════════════════════
def _setup_capture(sb):
    """يُفعّل التقاط m3u8 قبل أي تنقل."""
    global _REQ_IDS, _RESP_BODIES
    with _NET_LOCK:
        _REQ_IDS.clear(); _RESP_BODIES.clear()

    # Network.enable
    try:
        sb.driver.execute_cdp_cmd("Network.enable", {})
        print("      ✅ Network.enable", flush=True)
    except Exception: pass

    # Fetch.enable
    fetch_ok = False
    for fn in [
        lambda: sb.driver.execute_cdp_cmd("Fetch.enable", {
            "patterns": [
                {"urlPattern": "*.m3u8*", "requestStage": "Response"},
                {"urlPattern": "*.mpd*", "requestStage": "Response"},
            ],
        }),
        lambda: sb.cdp.send_cdp_cmd("Fetch.enable", {
            "patterns": [
                {"urlPattern": "*.m3u8*", "requestStage": "Response"},
                {"urlPattern": "*.mpd*", "requestStage": "Response"},
            ],
        }),
    ]:
        try:
            fn(); fetch_ok = True; break
        except Exception: pass

    if fetch_ok:
        print("      🎯 Fetch.enable", flush=True)
        def _handle_paused(params):
            try:
                rid = params.get("requestId", "")
                req = params.get("request") or {}
                url = req.get("url", "") if isinstance(req, dict) else ""
                if url and (".m3u8" in url or ".mpd" in url):
                    try:
                        r = _cdp_cmd(sb, "Fetch.getResponseBody", {"requestId": rid})
                        if r and "body" in r:
                            b = r["body"]
                            if r.get("base64Encoded"):
                                try: b = base64.b64decode(b).decode("utf-8", errors="ignore")
                                except Exception: pass
                            with _NET_LOCK: _RESP_BODIES[url] = b
                            print(f"      💾 m3u8 ({len(str(b))}B)", flush=True)
                    except Exception: pass
                if rid:
                    try: _cdp_cmd(sb, "Fetch.continueRequest", {"requestId": rid})
                    except Exception:
                        try: _cdp_cmd(sb, "Fetch.continueResponse", {"requestId": rid})
                        except Exception: pass
            except Exception: pass

        try:
            import mycdp
            cls = getattr(mycdp.fetch, "RequestPaused", None)
            if cls is not None:
                async def _h(p): _handle_paused(p if isinstance(p, dict) else {})
                sb.cdp.add_handler(cls, _h)
                print("      🎯 Fetch handler", flush=True)
        except Exception: pass


def _get_response_body(sb, url):
    with _NET_LOCK:
        if url in _RESP_BODIES: return _RESP_BODIES[url]
        rid = _REQ_IDS.get(url)
    if not rid: return None
    r = _cdp_cmd(sb, "Network.getResponseBody", {"requestId": rid})
    if r and "body" in r:
        body = r["body"]
        if r.get("base64Encoded"):
            try: body = base64.b64decode(body)
            except Exception: pass
        with _NET_LOCK: _RESP_BODIES[url] = body
        return body
    return None


# ═══════════════════════════════════════════════════════════════
# الكوكيز
# ═══════════════════════════════════════════════════════════════
def _dedup_cookies(raw):
    out, seen = [], set()
    for c in raw:
        if not isinstance(c, dict): continue
        n = str(c.get("name", "") or "").strip()
        v = str(c.get("value", "") or "").strip()
        if not n or not v: continue
        dom = str(c.get("domain", "") or "").strip()
        key = (n, dom)
        if key in seen: continue
        seen.add(key)
        out.append({"name": n, "value": v, "domain": dom})
    return out


def _get_cookies(sb):
    try:
        r = _cdp_cmd(sb, "Network.getAllCookies", {})
        if r and r.get("cookies"):
            return {c["name"]: c["value"] for c in _dedup_cookies(r["cookies"])}
    except Exception: pass
    return {}


# ═══════════════════════════════════════════════════════════════
# m3u8 parser + deep resolution
# ═══════════════════════════════════════════════════════════════
def _parse_m3u8(text, base_url):
    segs, variants = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'): continue
        full = line if line.startswith('http') else urljoin(base_url + '/', line)
        if '.m3u8' in line.lower(): variants.append(full)
        else: segs.append(full)
    return segs, variants


def _resolve_m3u8(m3u8_url, headers, depth=0, max_depth=4):
    if depth > max_depth: return None, []
    try:
        r = cffi_requests.get(m3u8_url, headers=headers,
                              impersonate="chrome124", timeout=20, verify=False)
        if r.status_code != 200: return None, []
    except Exception: return None, []
    base = m3u8_url.rsplit('/', 1)[0]
    segs, variants = _parse_m3u8(r.text, base)
    if segs: return m3u8_url, segs
    if variants:
        for v in variants[:2]:
            final, vsegs = _resolve_m3u8(v, headers, depth+1, max_depth)
            if vsegs: return final, vsegs
    return None, []


# ═══════════════════════════════════════════════════════════════
# ★ cffi segments (الأسرع)
# ═══════════════════════════════════════════════════════════════
def _cffi_segments(segments, out_path, iframe_url, cookies_dict):
    if not segments: return None
    headers = {
        "Referer": iframe_url or "",
        "Origin": _origin(iframe_url),
        "User-Agent": UA, "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())[:8000]
    if cookie_str: headers["Cookie"] = cookie_str

    # اختبار سريع
    test_ok = False
    for imp in ["chrome124", "chrome120"]:
        try:
            r = cffi_requests.get(segments[0], headers=headers,
                                  impersonate=imp, timeout=12, verify=False)
            if r.status_code == 200 and len(r.content) > 100:
                test_ok = True
                print(f"      ⚡ cffi[{imp}] OK", flush=True)
                break
        except Exception: continue
    if not test_ok: return None

    print(f"   ⚡ {len(segments)} segment...", flush=True)
    seg_dir = tempfile.mkdtemp(prefix="hls_cffi_")
    seg_paths, failed, total_bytes = {}, 0, 0

    def _dl(args):
        idx, url = args
        for imp in ["chrome124", "chrome120"]:
            try:
                r = cffi_requests.get(url, headers=headers,
                                      impersonate=imp, timeout=25, verify=False)
                if r.status_code == 200 and len(r.content) > 100:
                    p = os.path.join(seg_dir, f"seg_{idx:06d}.ts")
                    with open(p, 'wb') as f: f.write(r.content)
                    return (idx, p, len(r.content))
            except Exception: continue
        return (idx, None, 0)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=CURL_CFFI_WORKERS) as ex:
        futures = [ex.submit(_dl, (i, s)) for i, s in enumerate(segments)]
        done = 0
        for fut in as_completed(futures):
            idx, p, size = fut.result()
            done += 1
            if p: seg_paths[idx] = p; total_bytes += size
            else: failed += 1
            if done % 50 == 0 or done == len(segments):
                print(f"      📦 {done}/{len(segments)} | {total_bytes/1048576:.1f}MB | فشل:{failed}", flush=True)

    if not seg_paths or failed > len(segments) * 0.15:
        shutil.rmtree(seg_dir, ignore_errors=True); return None

    sorted_segs = [seg_paths[k] for k in sorted(seg_paths.keys())]
    concat_file = os.path.join(seg_dir, "concat.txt")
    with open(concat_file, 'w') as f:
        for p in sorted_segs: f.write(f"file '{p}'\n")

    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'warning',
           '-f', 'concat', '-safe', '0', '-i', concat_file,
           '-c', 'copy', '-bsf:a', 'aac_adtstoasc',
           '-movflags', '+faststart', '-f', 'mp4', '-y', out_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        ok = r.returncode == 0 and os.path.exists(out_path)
    except Exception: ok = False
    shutil.rmtree(seg_dir, ignore_errors=True)
    if ok:
        print(f"   ✅ cffi: {os.path.getsize(out_path)/1048576:.1f}MB", flush=True)
        return (os.path.getsize(out_path), True)
    return None


# ═══════════════════════════════════════════════════════════════
# yt-dlp مع stall detection
# ═══════════════════════════════════════════════════════════════
def _ytdlp(url, out_path, referer, cookies_dict):
    print(f"      [yt-dlp] {url[:80]}", flush=True)
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())[:8000]
    cmd = ["yt-dlp", '--no-warnings', '--no-playlist', '--no-part',
           '--retries', '10', '--fragment-retries', '20',
           '--socket-timeout', '45',
           '--concurrent-fragments', '16',
           '--no-check-certificate', '--continue',
           '--hls-use-mpegts', '--hls-prefer-native',
           '--impersonate', 'chrome',
           '--user-agent', UA,
           '--referer', referer,
           '--add-header', 'Accept:*/*']
    if cookie_str: cmd += ['--add-header', f'Cookie:{cookie_str}']
    cmd += ['-o', out_path, url]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except Exception: return False

    start = time.time(); last_size = 0; last_change = start; best = 0
    while proc.poll() is None:
        time.sleep(3)
        now = time.time()
        size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        if size > last_size:
            last_size = size; last_change = now
            if size > best: best = size
        if now - last_change > STALL_TIMEOUT and size > 0:
            try: proc.kill(); proc.wait(timeout=5)
            except Exception: pass
            return best >= MIN_PARTIAL_ACCEPT
        if now - start > YTDLP_TIMEOUT:
            try: proc.kill(); proc.wait(timeout=5)
            except Exception: pass
            return best >= MIN_PARTIAL_ACCEPT

    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    return size >= MIN_SIZE


# ═══════════════════════════════════════════════════════════════
# كشف m3u8 (سريع)
# ═══════════════════════════════════════════════════════════════
def _scan_m3u8(sb, netlog_read):
    found = set()
    with _NET_LOCK:
        for u in list(_RESP_BODIES.keys()):
            if ".m3u8" in u or ".mpd" in u: found.add(u)
    try:
        for u in netlog_read():
            if ".m3u8" in u or ".mpd" in u: found.add(u)
    except Exception: pass
    try:
        perf = sb.cdp.execute_script("""
            (function(){try{return performance.getEntriesByType('resource').map(e => e.name);}catch(e){return [];}})();
        """)
        if isinstance(perf, list):
            for u in perf:
                if ".m3u8" in u or ".mpd" in u: found.add(u)
    except Exception: pass
    try:
        html = sb.cdp.get_page_source() or ""
        for m in re.finditer(r'(https?:[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*)', html):
            found.add(m.group(1).replace("\\/", "/"))
    except Exception: pass
    try:
        jw = sb.cdp.execute_script("""
            (function(){try{
                if(typeof jwplayer!=='undefined'){var p=jwplayer();if(p&&p.getPlaylist){var f=[];p.getPlaylist().forEach(function(i){if(i.file)f.push(i.file);});return f;}}
                var v=document.querySelector('video');
                if(v&&v.currentSrc&&v.currentSrc.indexOf('.m3u8')!==-1)return [v.currentSrc];
                return [];
            }catch(e){return [];}})();
        """)
        if isinstance(jw, list):
            for u in jw:
                if isinstance(u, str) and ".m3u8" in u: found.add(u)
    except Exception: pass

    urls = [u for u in found if "ping.gif" not in u and "jwpltx" not in u]
    idx = [u for u in urls if "index" in u.lower()]
    mst = [u for u in urls if "master" in u.lower()]
    mpd = [u for u in urls if ".mpd" in u.lower()]
    oth = [u for u in urls if u not in idx and u not in mst and u not in mpd]
    return idx + mst + mpd + oth


# ═══════════════════════════════════════════════════════════════
# تشغيل الفيديو (خفيف)
# ═══════════════════════════════════════════════════════════════
def _trigger_play(sb):
    try:
        sb.cdp.execute_script("""
            (function(){try{
                var v=document.querySelector('video');
                if(v){v.muted=true;if(v.play)v.play().catch(function(){});}
                if(typeof jwplayer!=='undefined'){var p=jwplayer();if(p&&p.play)p.play(true);}
                if(typeof videojs!=='undefined'){var p2=videojs.getPlayers();for(var k in p2){try{p2[k].play();p2[k].muted(true);}catch(e){}}}
            }catch(e){}})();
        """)
    except Exception: pass
    # نقرة واحدة في المركز
    try:
        rect = sb.cdp.execute_script("""
            (function(){
                var v=document.querySelector('video');
                if(!v)return null;
                var r=v.getBoundingClientRect();
                if(r.width<50)return null;
                return {x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)};
            })();
        """)
        if rect and rect.get("x", 0) > 0:
            for _ in range(2):
                sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mousePressed", "x": rect['x'], "y": rect['y'],
                    "button": "left", "clickCount": 1,
                })
                sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mouseReleased", "x": rect['x'], "y": rect['y'],
                    "button": "left", "clickCount": 1,
                })
                sb.cdp.sleep(0.4)
    except Exception: pass


# ═══════════════════════════════════════════════════════════════
# فتح صفحة بسرعة (بدون انتظار كامل)
# ═══════════════════════════════════════════════════════════════
def _fast_open(sb, url, wait=1.0):
    """فتح سريع — لا ينتظر DOMContentLoaded."""
    try:
        sb.driver.execute_cdp_cmd("Page.navigate", {"url": url})
    except Exception:
        try: sb.cdp.open(url)
        except Exception: pass
    sb.cdp.sleep(wait)
    # أوقف أي تحميل معلق
    try:
        sb.driver.execute_cdp_cmd("Runtime.evaluate",
            {"expression": "try{window.stop()}catch(e){}"})
    except Exception: pass


# ═══════════════════════════════════════════════════════════════
# CF bypass (سريع)
# ═══════════════════════════════════════════════════════════════
def _cf_bypass(sb, url, timeout_s=20):
    print(f"      🔄 CF...", flush=True)
    start = time.time()
    try: sb.driver.execute_cdp_cmd("Page.navigate", {"url": url, "referrer": ""})
    except Exception: return False
    sb.cdp.sleep(2)
    for _ in range(2):
        if time.time() - start > timeout_s: return False
        try: sb.uc_gui_click_captcha()
        except Exception: pass
        sb.cdp.sleep(1.5)
        try:
            title = sb.get_page_title() or ""
            if "Just a moment" not in title and "Attention Required" not in title:
                print(f"      ✅ CF ({time.time()-start:.0f}s)", flush=True)
                return True
        except Exception: pass
    return False


# ═══════════════════════════════════════════════════════════════
# ★ المعالج الموحّد السريع
# ═══════════════════════════════════════════════════════════════
def _process_url(sb, url, out_path, netlog_read):
    """
    معالج موحّد يعمل مع أي رابط:
      • see.php / watch.php (yam)
      • /watch/ (shhaiid4u)
      • /video/modablaj-* (u3seq)
    """
    u_low = url.lower()

    # ═══ 1) فتح سريع ═══
    print(f"🖥️  فتح: {url[:90]}", flush=True)
    _fast_open(sb, url, wait=1.5)

    # ═══ 2) نقرات سريعة (3 دورات × 1.5s = 4.5s) ═══
    for cycle in range(CLICK_CYCLES):
        _trigger_play(sb)
        sb.cdp.sleep(1.2)
        # تحقق مبكر
        if _scan_m3u8(sb, netlog_read):
            print(f"   ✨ m3u8 بعد {cycle+1} دورة", flush=True)
            break

    # ═══ 3) iframe متداخل (yam) ═══
    for _ in range(2):
        try:
            ifr = sb.cdp.execute_script("""
                (function(){
                    var f=document.querySelector('iframe');
                    if(f&&f.src&&f.src.startsWith('http')
                        &&f.src.indexOf('google')===-1
                        &&f.src.indexOf('facebook')===-1)
                        return f.src;
                    return null;
                })();
            """)
            if not ifr: break
            print(f"      🔄 iframe: {ifr[:80]}", flush=True)
            sb.driver.execute_cdp_cmd("Page.navigate", {"url": ifr, "referrer": url})
            sb.cdp.sleep(2)
            for _ in range(2):
                _trigger_play(sb)
                sb.cdp.sleep(0.8)
            if _scan_m3u8(sb, netlog_read): break
        except Exception: break

    # ═══ 4) انتظار m3u8 (حتى M3U8_SEARCH_TIMEOUT) ═══
    print(f"   🎬 بحث m3u8 ({M3U8_SEARCH_TIMEOUT}s)...", flush=True)
    search_start = time.time()
    m3u8_urls = []
    while time.time() - search_start < M3U8_SEARCH_TIMEOUT:
        found = _scan_m3u8(sb, netlog_read)
        if found:
            m3u8_urls = found; break
        _trigger_play(sb)
        sb.cdp.sleep(1.5)

    if not m3u8_urls:
        print(f"   ❌ لا m3u8", flush=True)
        return None

    print(f"   🎯 {len(m3u8_urls)} مرشح", flush=True)
    for u in m3u8_urls[:2]: print(f"      · {u[:100]}", flush=True)

    cookies = _get_cookies(sb)
    ref = sb.cdp.get_current_url() or url

    # ═══ 5) تحميل: cffi (الأسرع) → yt-dlp ═══
    for m3u8 in m3u8_urls[:3]:
        # cffi direct
        if _cffi_segments_deep(m3u8, str(out_path), ref, cookies):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return (os.path.getsize(out_path), True)

    # yt-dlp
    for m3u8 in m3u8_urls[:3]:
        if _ytdlp(m3u8, str(out_path), ref, cookies):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return (os.path.getsize(out_path), True)

    return None


def _cffi_segments_deep(m3u8_url, out_path, referer, cookies_dict):
    """cffi مع deep resolution."""
    headers = {
        "Referer": referer or "",
        "Origin": _origin(referer),
        "User-Agent": UA, "Accept": "*/*",
    }
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())[:8000]
    if cookie_str: headers["Cookie"] = cookie_str

    final_url, segments = _resolve_m3u8(m3u8_url, headers)
    if not segments: return False
    return _cffi_segments(segments, out_path, referer, cookies_dict) is not None


# ═══════════════════════════════════════════════════════════════
# u3seq servers (سريع)
# ═══════════════════════════════════════════════════════════════
def _process_u3seq(sb, url, out_path, netlog_read):
    """مسار u3seq: serversList → iframes → m3u8."""
    u_low = url.lower()

    # إضافة ?do=watch
    if "?do=watch" not in url:
        cur = url
        if "?" in cur: cur = cur.split("?")[0]
        if not cur.endswith("/"): cur += "/"
        watch_url = cur + "?do=watch"
    else:
        watch_url = url

    print(f"🖥️  فتح: {watch_url[:90]}", flush=True)
    _fast_open(sb, watch_url, wait=2)

    # انتظار serversList (سرعة 1s)
    servers = []
    for i in range(12):
        sb.cdp.sleep(1)
        try:
            servers = sb.cdp.execute_script("""
                (function(){
                    var l=document.querySelector('.serversList');
                    if(!l)return [];
                    return Array.from(l.querySelectorAll('li')).map(li=>({
                        id:li.id||'',name:(li.textContent||'').trim()
                    }));
                })();
            """) or []
            if servers:
                print(f"   ✅ {len(servers)} سيرفر", flush=True)
                break
        except Exception: pass

    if not servers:
        print(f"   🔄 لا سيرفرات → معالج عام", flush=True)
        return _process_url(sb, url, out_path, netlog_read)

    # جمع iframes
    print(f"   📋 iframes...", flush=True)
    iframe_map = {}
    seen = set()
    for i in range(8):
        sb.cdp.sleep(0.4)
        try:
            if sb.cdp.execute_script("return typeof getServer2==='function'"):
                break
        except Exception: pass

    for srv in servers:
        sid = srv.get("id")
        if not sid: continue
        before = sb.cdp.execute_script("""
            (function(){var i=document.querySelector('.watch iframe');return i?i.src:null;})();
        """)
        try:
            rect = sb.cdp.execute_script(f"""
                (function(){{
                    var e=document.getElementById('{sid}');
                    if(!e)return null;
                    var r=e.getBoundingClientRect();
                    if(r.width<5||r.height<5)return null;
                    return {{x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)}};
                }})();
            """)
            if rect and rect.get("x", 0) > 0:
                for _ in range(1):
                    sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                        "type": "mousePressed", "x": rect['x'], "y": rect['y'],
                        "button": "left", "clickCount": 1,
                    })
                    sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                        "type": "mouseReleased", "x": rect['x'], "y": rect['y'],
                        "button": "left", "clickCount": 1,
                    })
        except Exception: continue

        after = before
        deadline = time.time() + 3
        while time.time() < deadline:
            sb.cdp.sleep(0.25)
            c = sb.cdp.execute_script("""
                (function(){var i=document.querySelector('.watch iframe');return i?i.src:null;})();
            """)
            if c and c != before: after = c; break
        if after and after != before:
            u2 = after.replace("&amp;", "&")
            if u2 not in seen:
                seen.add(u2); iframe_map[f"{srv.get('name')}_{sid}"] = u2

    if not iframe_map:
        return _process_url(sb, url, out_path, netlog_read)

    print(f"   ✅ {len(iframe_map)} iframe", flush=True)

    def _prio(item):
        k = item[0].lower()
        for i, s in enumerate(SERVER_PRIORITY):
            if s in k: return i
        return 99

    ordered = sorted(iframe_map.items(), key=_prio)

    for sname, iframe_url in ordered[:3]:  # أول 3 سيرفرات
        print(f"\n   ═══ {sname} ═══", flush=True)
        try:
            if "luluvdo" in sname.lower():
                if not _cf_bypass(sb, iframe_url, 20):
                    continue
            else:
                sb.driver.execute_cdp_cmd("Page.navigate",
                    {"url": iframe_url, "referrer": watch_url})
                sb.cdp.sleep(2)

            # تشغيل
            for _ in range(4):
                _trigger_play(sb)
                sb.cdp.sleep(1.2)
                if _scan_m3u8(sb, netlog_read): break

            # بحث m3u8
            m3u8_urls = []
            search_start = time.time()
            while time.time() - search_start < M3U8_SEARCH_TIMEOUT:
                found = _scan_m3u8(sb, netlog_read)
                if found: m3u8_urls = found; break
                sb.cdp.sleep(1.5)

            if not m3u8_urls: continue

            print(f"      🎯 {len(m3u8_urls)} مرشح", flush=True)
            cookies = _get_cookies(sb)

            # cffi أولاً
            for m3u8 in m3u8_urls[:2]:
                if _cffi_segments_deep(m3u8, str(out_path), iframe_url, cookies):
                    if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                        return (os.path.getsize(out_path), True)

            # yt-dlp
            for m3u8 in m3u8_urls[:2]:
                if _ytdlp(m3u8, str(out_path), iframe_url, cookies):
                    if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                        return (os.path.getsize(out_path), True)
        except Exception as e:
            print(f"   ❌ {str(e)[:100]}", flush=True)
            continue

    # fallback
    return _process_url(sb, url, out_path, netlog_read)


# ═══════════════════════════════════════════════════════════════
# _process_with_browser (نقطة الدخول)
# ═══════════════════════════════════════════════════════════════
def _process_with_browser(url, out_path):
    from seleniumbase import SB

    netlog = tempfile.mktemp(suffix="_net.txt")
    open(netlog, "w").close()

    def _log(u):
        try:
            with open(netlog, "a", encoding="utf-8") as fh: fh.write(u + "\n")
        except Exception: pass

    def _read():
        try:
            with open(netlog, encoding="utf-8") as fh:
                return [l.strip() for l in fh if l.strip()]
        except Exception: return []

    try:
        with SB(uc=True, xvfb=True, headless=False, incognito=True,
                ad_block_on=True, disable_csp=True,
                page_load_strategy="eager", locale_code="en") as sb:
            try:
                sb.activate_cdp_mode()

                # ★ التقاط مبكر (قبل أي تنقل)
                _setup_capture(sb)

                # interceptor JS
                try:
                    sb.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                        "source": """
                        (function(){
                            window.__captured_m3u8=window.__captured_m3u8||[];
                            function rec(u){try{if(typeof u==='string'&&(u.indexOf('.m3u8')!==-1||u.indexOf('.mpd')!==-1)){if(window.__captured_m3u8.indexOf(u)===-1)window.__captured_m3u8.push(u);}}catch(e){}}
                            if(window.fetch&&!window.__fp){var o=window.fetch;window.fetch=function(i,init){try{var u=(typeof i==='string')?i:(i&&i.url);rec(u);}catch(e){}return o.apply(this,arguments);};window.__fp=true;}
                            if(window.XMLHttpRequest&&!window.__xp){var xo=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){try{rec(u);}catch(e){}return xo.apply(this,arguments);};window.__xp=true;}
                        })();
                        """,
                    })
                except Exception: pass

                # network handler
                try:
                    import mycdp
                    async def on_req(params):
                        try:
                            req = params.get("request", {}) or {}
                            u = req.get("url", "")
                            rid = params.get("requestId", "")
                            if u:
                                _log(u)
                                if rid:
                                    with _NET_LOCK: _REQ_IDS[u] = rid
                        except Exception: pass
                    sb.cdp.add_handler(mycdp.network.RequestWillBeSent, on_req)
                except Exception: pass

                # ★★ التوجيه السريع
                u_low = url.lower()

                # yam / see.php / watch.php / shhaiid4u → معالج عام سريع
                if "see.php" in u_low or "watch.php" in u_low or ("/watch/" in u_low and ".php" not in u_low):
                    print(f"   🎯 رابط مباشر → معالج عام", flush=True)
                    res = _process_url(sb, url, out_path, _read)
                    try: os.remove(netlog)
                    except Exception: pass
                    return res

                # u3seq → مسار السيرفرات
                if "modablaj-" in u_low or "/video/" in u_low:
                    print(f"   🎯 u3seq → مسار السيرفرات", flush=True)
                    res = _process_u3seq(sb, url, out_path, _read)
                    try: os.remove(netlog)
                    except Exception: pass
                    return res

                # أي رابط آخر → معالج عام
                print(f"   🎯 رابط generic", flush=True)
                res = _process_url(sb, url, out_path, _read)
                try: os.remove(netlog)
                except Exception: pass
                return res

            except Exception as e:
                import traceback
                print(f"   ❌ {str(e)[:200]}", flush=True)
                traceback.print_exc()
    except Exception as e:
        import traceback
        print(f"   ❌ {str(e)[:200]}", flush=True)
        traceback.print_exc()
    finally:
        try: os.remove(netlog)
        except Exception: pass

    return None


# ═══════════════════════════════════════════════════════════════
# الضغط
# ═══════════════════════════════════════════════════════════════
def _compress_to_target(inp, out):
    max_size_mb = getattr(config, "COMPRESS_MAX_SIZE_MB", 45)
    scale = getattr(config, "COMPRESS_SCALE", 240)
    crf = getattr(config, "COMPRESS_CRF", 32)
    preset = getattr(config, "COMPRESS_PRESET", "veryfast")
    audio_br = getattr(config, "COMPRESS_AUDIO_BITRATE", "32k")

    im = Path(inp).stat().st_size / 1048576
    print(f"   🗜️  {im:.2f}MB → {scale}p...", flush=True)
    ff = _get_ffmpeg_exe()
    cmd = [ff, "-nostdin", "-hide_banner", "-loglevel", "error",
           "-err_detect", "ignore_err", "-i", str(inp),
           "-vf", f"scale=-2:{scale}",
           "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
           "-profile:v", "main", "-level", "3.1", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", audio_br, "-ac", "2", "-ar", "44100",
           "-movflags", "+faststart", "-threads", "2", "-y", str(out)]
    try:
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode == 0 and Path(out).exists():
            om = Path(out).stat().st_size / 1048576
            print(f"   ✅ {im:.2f}→{om:.2f}MB في {time.time()-t0:.1f}s", flush=True)
            return om <= max_size_mb
    except Exception as e:
        print(f"   ⚠️ ضغط: {str(e)[:100]}", flush=True)
    return False


def _make_thumbnail(video_path, out_path):
    ff = _get_ffmpeg_exe()
    for ss in ['00:00:05', '00:00:01', '00:00:00']:
        cmd = [ff, '-err_detect', 'ignore_err', '-fflags', '+discardcorrupt',
               '-ss', ss, '-i', str(video_path),
               '-vframes', '1', '-vf', 'scale=320:180',
               '-f', 'image2', '-y', str(out_path)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                return True
        except Exception: pass
    return False


# ═══════════════════════════════════════════════════════════════
# الواجهة العامة
# ═══════════════════════════════════════════════════════════════
def download_episode(series_name, episode_num, url,
                     media_type="series", item_name=None):
    safe_name = _safe(item_name or series_name)
    file_prefix = f"movie_{episode_num:02d}" if media_type == "movie" else f"ep{episode_num:03d}"
    out_dir = MEDIA_DIR / safe_name
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / f"{file_prefix}_raw.mp4"
    final = out_dir / f"{file_prefix}.mp4"

    if final.exists() and final.stat().st_size > MIN_SIZE:
        print(f"    ↳ موجودة: {final.name}", flush=True)
        return final

    if not _is_valid_episode_url(url):
        print(f"    ⏭️ تخطي (رابط غير مدعوم): {url[:80]}", flush=True)
        return None

    print(f"    ↳ تحميل {file_prefix}...", flush=True)
    try:
        res = run_with_timeout(_process_with_browser, args=(url, raw),
                                timeout=900, default=None)
    except TimeoutError_:
        print(f"    ⏰ تجاوز 900s", flush=True)
        for p in ["yt-dlp", "chrome", "ffmpeg"]:
            subprocess.run(["pkill", "-9", "-f", p], capture_output=True, timeout=5)
        res = None
    except Exception as e:
        print(f"    ⚠️ {str(e)[:150]}", flush=True)
        res = None

    if not res or not raw.exists():
        print(f"    ⚠️ فشل تحميل {series_name} — {file_prefix}", flush=True)
        try:
            if raw.exists(): raw.unlink()
        except Exception: pass
        return None

    size = raw.stat().st_size
    print(f"    📦 {size/1048576:.1f}MB", flush=True)

    if getattr(config, "SKIP_COMPRESS", False):
        shutil.move(str(raw), str(final))
    else:
        if not _compress_to_target(raw, final):
            print("    ⚠️ فشل الضغط — الأصلي", flush=True)
            shutil.move(str(raw), str(final))

    if raw.exists(): raw.unlink()
    if not final.exists(): return None

    try:
        thumb_path = out_dir / f"{file_prefix}.jpg"
        _make_thumbnail(final, thumb_path)
    except Exception: pass

    print(f"    ✅ {final.name} ({final.stat().st_size/1048576:.1f}MB)", flush=True)
    return final