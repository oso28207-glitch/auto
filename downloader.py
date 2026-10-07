"""
downloader.py — Universal HLS Downloader v25 (FAST + STABLE)
═══════════════════════════════════════════════════════════
إصلاحات v25:
  • لا ننقر على السيرفرات — نقرأ iframe مباشرة من DOM
  • لا نستخدم sb.cdp.open — فقط Page.navigate + window.stop()
  • _safe_eval مع مهلة لكل execute_script
  • iframe محدد بـ 1 بدلاً من 4
"""

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin

from curl_cffi import requests as cffi_requests

from config import config, MEDIA_DIR

# ═══ إعدادات محسّنة للسرعة ═══
MIN_SIZE = 100 * 1024
MIN_PARTIAL_ACCEPT = 30 * 1024 * 1024

M3U8_SEARCH_TIMEOUT = int(os.environ.get("M3U8_SEARCH_TIMEOUT", "20"))
YTDLP_TIMEOUT = int(os.environ.get("YTDLP_TIMEOUT", "600"))
OPEN_TIMEOUT = int(os.environ.get("OPEN_TIMEOUT", "12"))
EVAL_TIMEOUT = int(os.environ.get("EVAL_TIMEOUT", "6"))
STALL_TIMEOUT = int(os.environ.get("STALL_TIMEOUT", "45"))

CURL_CFFI_WORKERS = int(os.environ.get("CURL_CFFI_WORKERS", "16"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SERVER_PRIORITY = [
    "luluvdo", "vinovo", "vidsonic", "playmate", "firestream",
    "vidaraa", "vids", "bysejikuar", "vidsp", "savefiles", "voe",
]

_RESP_BODIES = {}
_NET_LOCK = threading.Lock()


class TimeoutError_(Exception):
    pass


# ═══════════════════════════════════════════════════════════════
# أدوات
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
# ★★★ _safe_eval — execute_script مع مهلة صارمة
# ═══════════════════════════════════════════════════════════════
def _safe_eval(sb, js, timeout=EVAL_TIMEOUT, default=None):
    """تنفيذ JS مع مهلة — يمنع التجمّد على الصفحات البطيئة."""
    result = [default]
    done = threading.Event()

    def _run():
        try:
            result[0] = sb.cdp.execute_script(js)
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    done.wait(timeout=timeout)
    return result[0]


def _safe_cdp(sb, cmd, params=None, timeout=EVAL_TIMEOUT):
    """تنفيذ أمر CDP مع مهلة."""
    if params is None: params = {}
    result = [None]
    done = threading.Event()

    def _run():
        try:
            result[0] = sb.driver.execute_cdp_cmd(cmd, params)
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    done.wait(timeout=timeout)
    return result[0]


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
# التقاط m3u8 (Fetch.enable)
# ═══════════════════════════════════════════════════════════════
def _setup_capture(sb):
    global _RESP_BODIES
    with _NET_LOCK: _RESP_BODIES.clear()

    try:
        sb.driver.execute_cdp_cmd("Network.enable", {})
        print("      ✅ Network.enable", flush=True)
    except Exception: pass

    ok = False
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
            fn(); ok = True; break
        except Exception: pass

    if not ok:
        print("      ⚠️ Fetch فشل", flush=True)
        return

    print("      🎯 Fetch.enable", flush=True)

    def _handle(params):
        try:
            rid = params.get("requestId", "")
            req = params.get("request") or {}
            url = req.get("url", "") if isinstance(req, dict) else ""
            if url and (".m3u8" in url or ".mpd" in url):
                try:
                    r = _safe_cdp(sb, "Fetch.getResponseBody", {"requestId": rid}, timeout=5)
                    if r and "body" in r:
                        b = r["body"]
                        if r.get("base64Encoded"):
                            try: b = base64.b64decode(b).decode("utf-8", errors="ignore")
                            except Exception: pass
                        with _NET_LOCK: _RESP_BODIES[url] = b
                        print(f"      💾 m3u8 ({len(str(b))}B)", flush=True)
                except Exception: pass
            if rid:
                try: _safe_cdp(sb, "Fetch.continueRequest", {"requestId": rid}, timeout=3)
                except Exception:
                    try: _safe_cdp(sb, "Fetch.continueResponse", {"requestId": rid}, timeout=3)
                    except Exception: pass
        except Exception: pass

    try:
        import mycdp
        cls = getattr(mycdp.fetch, "RequestPaused", None)
        if cls is not None:
            async def _h(p): _handle(p if isinstance(p, dict) else {})
            sb.cdp.add_handler(cls, _h)
            print("      🎯 Fetch handler", flush=True)
    except Exception: pass


# ═══════════════════════════════════════════════════════════════
# الكوكيز
# ═══════════════════════════════════════════════════════════════
def _get_cookies(sb):
    try:
        r = _safe_cdp(sb, "Network.getAllCookies", {}, timeout=5)
        if r and r.get("cookies"):
            return {c["name"]: c["value"] for c in r["cookies"]
                    if c.get("name") and c.get("value")}
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
# cffi segments
# ═══════════════════════════════════════════════════════════════
def _cffi_segments(segments, out_path, iframe_url, cookies_dict):
    if not segments: return None
    headers = {
        "Referer": iframe_url or "",
        "Origin": _origin(iframe_url),
        "User-Agent": UA, "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    ck = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())[:8000]
    if ck: headers["Cookie"] = ck

    test_ok = False
    for imp in ["chrome124", "chrome120"]:
        try:
            r = cffi_requests.get(segments[0], headers=headers,
                                  impersonate=imp, timeout=12, verify=False)
            if r.status_code == 200 and len(r.content) > 100:
                test_ok = True; break
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
            idx, p, size = fut.result(); done += 1
            if p: seg_paths[idx] = p; total_bytes += size
            else: failed += 1
            if done % 50 == 0 or done == len(segments):
                print(f"      📦 {done}/{len(segments)} | {total_bytes/1048576:.1f}MB", flush=True)

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
# yt-dlp
# ═══════════════════════════════════════════════════════════════
def _ytdlp(url, out_path, referer, cookies_dict):
    print(f"      [yt-dlp] {url[:80]}", flush=True)
    ck = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())[:8000]
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
    if ck: cmd += ['--add-header', f'Cookie:{ck}']
    cmd += ['-o', out_path, url]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception: return False

    start = time.time(); last_size = 0; last_change = start; best = 0
    while proc.poll() is None:
        time.sleep(3)
        now = time.time()
        size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        if size > last_size: last_size = size; last_change = now
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
def _scan_m3u8(sb):
    found = set()
    with _NET_LOCK:
        for u in list(_RESP_BODIES.keys()):
            if ".m3u8" in u or ".mpd" in u: found.add(u)

    try:
        perf = _safe_eval(sb, """
            (function(){try{return performance.getEntriesByType('resource').map(e => e.name);}catch(e){return [];}})();
        """, timeout=3)
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
        jw = _safe_eval(sb, """
            (function(){try{
                if(typeof jwplayer!=='undefined'){var p=jwplayer();if(p&&p.getPlaylist){var f=[];p.getPlaylist().forEach(function(i){if(i.file)f.push(i.file);});return f;}}
                var v=document.querySelector('video');
                if(v&&v.currentSrc&&v.currentSrc.indexOf('.m3u8')!==-1)return [v.currentSrc];
                return [];
            }catch(e){return [];}})();
        """, timeout=3)
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
# تشغيل الفيديو
# ═══════════════════════════════════════════════════════════════
def _trigger_play(sb):
    _safe_eval(sb, """
        (function(){try{
            var v=document.querySelector('video');
            if(v){v.muted=true;if(v.play)v.play().catch(function(){});}
            if(typeof jwplayer!=='undefined'){var p=jwplayer();if(p&&p.play)p.play(true);}
            if(typeof videojs!=='undefined'){var p2=videojs.getPlayers();for(var k in p2){try{p2[k].play();p2[k].muted(true);}catch(e){}}}
        }catch(e){}})();
    """, timeout=3)


# ═══════════════════════════════════════════════════════════════
# فتح سريع
# ═══════════════════════════════════════════════════════════════
def _fast_open(sb, url, wait=1.0):
    """فتح سريع — Page.navigate + window.stop(). لا نستخدم cdp.open أبداً."""
    _safe_cdp(sb, "Page.navigate", {"url": url}, timeout=OPEN_TIMEOUT)
    sb.cdp.sleep(wait)
    _safe_cdp(sb, "Runtime.evaluate",
              {"expression": "try{window.stop()}catch(e){}"}, timeout=3)


# ═══════════════════════════════════════════════════════════════
# CF bypass
# ═══════════════════════════════════════════════════════════════
def _cf_bypass(sb, url, timeout_s=15):
    print(f"      🔄 CF...", flush=True)
    start = time.time()
    _safe_cdp(sb, "Page.navigate", {"url": url, "referrer": ""}, timeout=OPEN_TIMEOUT)
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
# ★★★ معالج موحّد سريع
# ═══════════════════════════════════════════════════════════════
def _process_url(sb, url, out_path):
    """معالج عام لأي رابط فيديو (see.php, watch.php, /watch/, generic)."""
    print(f"🖥️  فتح: {url[:90]}", flush=True)
    _fast_open(sb, url, wait=1.5)

    # نقرات سريعة
    for cycle in range(3):
        _trigger_play(sb)
        sb.cdp.sleep(1.2)
        if _scan_m3u8(sb): break

    # iframe متداخل (yam)
    ifr = _safe_eval(sb, """
        (function(){
            var f=document.querySelector('iframe');
            if(f&&f.src&&f.src.startsWith('http')
                &&f.src.indexOf('google')===-1
                &&f.src.indexOf('facebook')===-1)
                return f.src;
            return null;
        })();
    """, timeout=3)
    if ifr:
        print(f"      🔄 iframe: {ifr[:80]}", flush=True)
        _fast_open(sb, ifr, wait=2)
        for _ in range(3):
            _trigger_play(sb)
            sb.cdp.sleep(1)

    # انتظار m3u8
    print(f"   🎬 بحث m3u8 ({M3U8_SEARCH_TIMEOUT}s)...", flush=True)
    search_start = time.time()
    m3u8_urls = []
    while time.time() - search_start < M3U8_SEARCH_TIMEOUT:
        found = _scan_m3u8(sb)
        if found: m3u8_urls = found; break
        _trigger_play(sb)
        sb.cdp.sleep(1.5)

    if not m3u8_urls:
        print(f"   ❌ لا m3u8", flush=True)
        return None

    print(f"   🎯 {len(m3u8_urls)} مرشح", flush=True)
    for u in m3u8_urls[:2]: print(f"      · {u[:100]}", flush=True)

    cookies = _get_cookies(sb)
    ref = sb.cdp.get_current_url() or url

    # cffi-deep
    for m3u8 in m3u8_urls[:3]:
        headers = {
            "Referer": ref, "Origin": _origin(ref),
            "User-Agent": UA, "Accept": "*/*",
        }
        ck = "; ".join(f"{k}={v}" for k, v in cookies.items())[:8000]
        if ck: headers["Cookie"] = ck
        _, segments = _resolve_m3u8(m3u8, headers)
        if segments:
            res = _cffi_segments(segments, str(out_path), ref, cookies)
            if res and os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return res

    # yt-dlp
    for m3u8 in m3u8_urls[:3]:
        if _ytdlp(m3u8, str(out_path), ref, cookies):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return (os.path.getsize(out_path), True)

    return None


# ═══════════════════════════════════════════════════════════════
# ★★★ معالج u3seq — لا نقر على السيرفرات (يحل مشكلة التجمّد)
# ═══════════════════════════════════════════════════════════════
def _process_u3seq(sb, url, out_path):
    """
    يتعامل مع u3seq بدون النقر على السيرفرات:
      1. يفتح ?do=watch
      2. يقرأ iframe.src مباشرة من DOM
      3. إذا لم يوجد، ينفّذ getServer2() عبر JS
    """
    # إضافة ?do=watch
    if "?do=watch" not in url:
        cur = url
        if "?" in cur: cur = cur.split("?")[0]
        if not cur.endswith("/"): cur += "/"
        watch_url = cur + "?do=watch"
    else:
        watch_url = url

    print(f"🖥️  فتح: {watch_url[:90]}", flush=True)
    _fast_open(sb, watch_url, wait=2.5)

    # ★ 1) انتظر ظهور serversList
    servers = []
    for i in range(10):
        sb.cdp.sleep(0.8)
        servers = _safe_eval(sb, """
            (function(){
                var l=document.querySelector('.serversList');
                if(!l)return [];
                return Array.from(l.querySelectorAll('li')).map(li=>({
                    id:li.id||'',
                    name:(li.textContent||'').trim(),
                    onclick:li.getAttribute('onclick')||''
                }));
            })();
        """, timeout=3) or []
        if servers:
            print(f"   ✅ {len(servers)} سيرفر", flush=True)
            break

    if not servers:
        print(f"   🔄 لا سيرفرات → معالج عام", flush=True)
        return _process_url(sb, url, out_path)

    # ★ 2) اقرأ iframe.src مباشرة (بدون أي نقرة!)
    print(f"   📋 قراءة iframe مباشرة...", flush=True)
    iframe_url = None
    for i in range(6):
        sb.cdp.sleep(1)
        iframe_url = _safe_eval(sb, """
            (function(){
                var ifr=document.querySelector('.watch iframe');
                if(ifr&&ifr.src&&ifr.src.startsWith('http'))return ifr.src;
                return null;
            })();
        """, timeout=3)
        if iframe_url:
            print(f"   ✅ iframe جاهز: {iframe_url[:80]}", flush=True)
            break

    # ★ 3) إذا لم يوجد، استخرج onclick من أول سيرفر ونفّذه عبر JS
    if not iframe_url and servers:
        first = servers[0]
        onclick = first.get("onclick", "")
        print(f"   🔄 تنفيذ onclick يدوياً: {onclick[:80]}", flush=True)
        if onclick:
            try:
                # getServer2(video, server) → نمرر فقط الأرقام
                m = re.search(r'getServer2\([^,]+,\s*(\d+)\s*,\s*(\d+)\s*\)', onclick)
                if m:
                    vid, sid = m.group(1), m.group(2)
                    _safe_eval(sb, f"try{{getServer2(null,{vid},{sid})}}catch(e){{}}", timeout=5)
                    sb.cdp.sleep(2)
                    iframe_url = _safe_eval(sb, """
                        (function(){
                            var ifr=document.querySelector('.watch iframe');
                            if(ifr&&ifr.src&&ifr.src.startsWith('http'))return ifr.src;
                            return null;
                        })();
                    """, timeout=3)
            except Exception as e:
                print(f"   ⚠️ onclick: {str(e)[:80]}", flush=True)

    if not iframe_url:
        print(f"   ⚠️ لا iframe → معالج عام", flush=True)
        return _process_url(sb, url, out_path)

    iframe_url = iframe_url.replace("&amp;", "&")

    # ★ 4) انتقل إلى iframe
    print(f"   ➡️ الدخول إلى: {iframe_url[:80]}", flush=True)
    _fast_open(sb, iframe_url, wait=2)

    # ★ 5) نقرات لتشغيل الفيديو
    for cycle in range(4):
        _trigger_play(sb)
        sb.cdp.sleep(1)
        if _scan_m3u8(sb): break

    # iframe متداخل
    ifr2 = _safe_eval(sb, """
        (function(){
            var f=document.querySelector('iframe');
            if(f&&f.src&&f.src.startsWith('http')
                &&f.src.indexOf('google')===-1
                &&f.src!=='""" + iframe_url + """')
                return f.src;
            return null;
        })();
    """, timeout=3)
    if ifr2:
        print(f"      🔄 متداخل: {ifr2[:70]}", flush=True)
        _fast_open(sb, ifr2, wait=2)
        for _ in range(3):
            _trigger_play(sb)
            sb.cdp.sleep(1)

    # ★ 6) بحث m3u8
    print(f"   🎬 بحث m3u8 ({M3U8_SEARCH_TIMEOUT}s)...", flush=True)
    search_start = time.time()
    m3u8_urls = []
    while time.time() - search_start < M3U8_SEARCH_TIMEOUT:
        found = _scan_m3u8(sb)
        if found: m3u8_urls = found; break
        _trigger_play(sb)
        sb.cdp.sleep(1.5)

    if not m3u8_urls:
        print(f"   ❌ لا m3u8", flush=True)
        return None

    print(f"   🎯 {len(m3u8_urls)} مرشح", flush=True)
    for u in m3u8_urls[:3]: print(f"      · {u[:100]}", flush=True)

    cookies = _get_cookies(sb)
    ref = iframe_url

    # ★ 7) تحميل
    # cffi-deep أولاً
    for m3u8 in m3u8_urls[:3]:
        headers = {
            "Referer": ref, "Origin": _origin(ref),
            "User-Agent": UA, "Accept": "*/*",
        }
        ck = "; ".join(f"{k}={v}" for k, v in cookies.items())[:8000]
        if ck: headers["Cookie"] = ck
        _, segments = _resolve_m3u8(m3u8, headers)
        if segments:
            res = _cffi_segments(segments, str(out_path), ref, cookies)
            if res and os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return res

    # yt-dlp
    for m3u8 in m3u8_urls[:3]:
        if _ytdlp(m3u8, str(out_path), ref, cookies):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return (os.path.getsize(out_path), True)

    return None


# ═══════════════════════════════════════════════════════════════
# نقطة الدخول
# ═══════════════════════════════════════════════════════════════
def _process_with_browser(url, out_path):
    from seleniumbase import SB

    try:
        with SB(uc=True, xvfb=True, headless=False, incognito=True,
                ad_block_on=True, disable_csp=True,
                page_load_strategy="eager", locale_code="en") as sb:
            try:
                sb.activate_cdp_mode()

                # ★ اضبط مهلة الصفحة لتفادي التجمّد
                try:
                    sb.driver.set_page_load_timeout(15)
                except Exception: pass

                # التقاط m3u8
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

                # ★ التوجيه
                u_low = url.lower()
                if "modablaj-" in u_low or "/video/" in u_low:
                    print(f"   🎯 u3seq → مسار السيرفرات", flush=True)
                    return _process_u3seq(sb, url, out_path)
                else:
                    print(f"   🎯 رابط مباشر → معالج عام", flush=True)
                    return _process_url(sb, url, out_path)

            except Exception as e:
                import traceback
                print(f"   ❌ {str(e)[:200]}", flush=True)
                traceback.print_exc()
    except Exception as e:
        import traceback
        print(f"   ❌ {str(e)[:200]}", flush=True)
        traceback.print_exc()

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
                                timeout=600, default=None)
    except TimeoutError_:
        print(f"    ⏰ تجاوز 600s", flush=True)
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