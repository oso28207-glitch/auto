"""
downloader.py — Universal HLS Downloader v21
═══════════════════════════════════════════════════════════
يدعم:
  • u3seq.com  — صفحات /video/modablaj-*  (يحتاج ?do=watch)
  • yam.ahwaktv.net — صفحات watch.php?vid=XXX
  • shhaiid4u.net  — صفحات /watch/*
  • المشغلات المباشرة (firestream, luluvdo, ...)

الاستراتيجيات (بالترتيب):
  1. CDP capture + deep m3u8 resolution
  2. cffi segments (TLS fingerprint صحيح)
  3. browser XHR segments (جلسة المشغل الفعلية)
  4. yt-dlp مع stall detection
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
# الإعدادات
# ═══════════════════════════════════════════════════════════════
MIN_SIZE = 100 * 1024
MIN_PARTIAL_ACCEPT = 30 * 1024 * 1024

M3U8_SEARCH_TIMEOUT = int(os.environ.get("M3U8_SEARCH_TIMEOUT", "40"))
YTDLP_TIMEOUT = int(os.environ.get("YTDLP_TIMEOUT", "900"))
FFMPEG_HLS_TIMEOUT = int(os.environ.get("FFMPEG_HLS_TIMEOUT", "1800"))
OPEN_TIMEOUT = int(os.environ.get("OPEN_TIMEOUT", "25"))
STALL_TIMEOUT = int(os.environ.get("STALL_TIMEOUT", "90"))

BROWSER_BATCH = int(os.environ.get("BROWSER_BATCH", "16"))
BROWSER_BATCH_TIMEOUT = 30
SEGMENT_RETRY = 2
M3U8_CANDIDATE_LIMIT = 5
CURL_CFFI_WORKERS = int(os.environ.get("CURL_CFFI_WORKERS", "12"))

JWPLAYER_WAIT = 25

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
    if kwargs is None:
        kwargs = {}
    result = [default]
    exception = [None]
    done = threading.Event()

    def _run():
        try:
            result[0] = func(*args, **kwargs)
        except Exception as e:
            exception[0] = e
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    if not done.wait(timeout=timeout):
        raise TimeoutError_(f"timeout {timeout}s")
    if exception[0]:
        raise exception[0]
    return result[0]


def _safe(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()[:120]


def _origin(url):
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return ""


def _get_ffmpeg_exe():
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if r.returncode == 0:
            return "ffmpeg"
    except Exception:
        pass
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return "ffmpeg"


# ═══════════════════════════════════════════════════════════════
# ★★★ التحقق من صلاحية URL — يدعم u3seq + yam + مشغلات
# ═══════════════════════════════════════════════════════════════
def _is_valid_episode_url(url):
    """
    يقبل:
      • u3seq:      /video/modablaj-* أو ?do=watch
      • yam:        /watch.php?vid=XXX
      • shhaiid4u:  /watch/*
      • المشغلات:   /embed/, /e/, firestream, playmate, ...
    يرفض:
      • صفحات التصنيف: moslslat.php, all-series.php, topvideos.php
      • صفحات التنقل: الرئيسية، جديد الأفلام، ...
    """
    if not url or not isinstance(url, str):
        return False
    u = url.lower()

    # ─── استثناءات صريحة ───
    bad_markers = [
        "/moslslat.php", "/topvideos.php", "/all-series.php",
        "/series.php", "/category/", "/cats/", "/list/",
        "?cat=", "?category=",
    ]
    for b in bad_markers:
        if b in u:
            return False

    # ─── u3seq (صفحة حلقة) ───
    if "modablaj-" in u or "/video/" in u:
        return True

    # ★ yam: /watch.php?vid=XXX
    if "watch.php" in u and "vid=" in u:
        return True

    # ★ shhaiid4u: /watch/...
    if "/watch/" in u and ".php" not in u:
        return True

    # ─── مشغلات مباشرة ───
    play_markers = [
        "/embed/", "firestream.to/", "playmate.to/",
        "luluvdo.com/", "vidsonic", "vidaraa", "/e/",
    ]
    for m in play_markers:
        if m in u:
            return True

    return False


# ═══════════════════════════════════════════════════════════════
# CDP helpers
# ═══════════════════════════════════════════════════════════════
def _cdp_cmd(sb, cmd, params=None):
    if params is None:
        params = {}
    try:
        return sb.driver.execute_cdp_cmd(cmd, params)
    except Exception:
        pass
    for meth in ("send_cdp_cmd", "execute_cdp_cmd"):
        try:
            fn = getattr(sb.cdp, meth, None)
            if fn:
                return fn(cmd, params)
        except Exception:
            pass
    return None


# ═══════════════════════════════════════════════════════════════
# Fetch.enable + Network.enable
# ═══════════════════════════════════════════════════════════════
def _enable_fetch_capture(sb):
    global _REQ_IDS, _RESP_BODIES
    with _NET_LOCK:
        _REQ_IDS.clear()
        _RESP_BODIES.clear()

    print("      🎯 Fetch.enable...", flush=True)
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
            fn()
            ok = True
            break
        except Exception:
            pass

    if not ok:
        print("      ⚠️ Fetch.enable فشل", flush=True)
        return False

    print("      🎯 Fetch.enable OK", flush=True)

    def _handle_paused(params):
        try:
            rid = params.get("requestId", "")
            req = params.get("request") or {}
            url = req.get("url", "") if isinstance(req, dict) else ""
            status = params.get("responseStatusCode")

            if url and (".m3u8" in url or ".mpd" in url) and status == 200:
                try:
                    r = _cdp_cmd(sb, "Fetch.getResponseBody", {"requestId": rid})
                    if r and "body" in r:
                        b = r["body"]
                        if r.get("base64Encoded"):
                            try:
                                b = base64.b64decode(b).decode("utf-8", errors="ignore")
                            except Exception:
                                pass
                        with _NET_LOCK:
                            _RESP_BODIES[url] = b
                        print(f"      💾 m3u8 محفوظ ({len(str(b))}B)", flush=True)
                except Exception:
                    pass

            if rid:
                try:
                    _cdp_cmd(sb, "Fetch.continueRequest", {"requestId": rid})
                except Exception:
                    try:
                        _cdp_cmd(sb, "Fetch.continueResponse", {"requestId": rid})
                    except Exception:
                        pass
        except Exception:
            pass

    try:
        import mycdp
        cls = getattr(mycdp.fetch, "RequestPaused", None)
        if cls is not None:
            async def _async_handler(params):
                _handle_paused(params if isinstance(params, dict) else {})
            try:
                sb.cdp.add_handler(cls, _async_handler)
                print("      🎯 Fetch handler (async)", flush=True)
                return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _enable_network_capture(sb):
    for fn in [
        lambda: sb.driver.execute_cdp_cmd("Network.enable", {}),
        lambda: sb.cdp.send_cdp_cmd("Network.enable", {}),
    ]:
        try:
            fn()
            print("      ✅ Network.enable OK", flush=True)
            return True
        except Exception:
            pass
    print("      ⚠️ Network.enable فشل", flush=True)
    return False


# ═══════════════════════════════════════════════════════════════
# الكوكيز
# ═══════════════════════════════════════════════════════════════
def _normalize_cookie(c):
    if isinstance(c, dict):
        return c
    d = {}
    for attr in ("name", "value", "domain", "path", "secure",
                 "httpOnly", "expiry", "sameSite", "session"):
        try:
            v = getattr(c, attr, None)
            if v is not None:
                d[attr] = v
        except Exception:
            pass
    if d.get("name") and d.get("value"):
        return d
    return {}


def _dedup_cookies(raw):
    out, seen = [], set()
    for c in raw:
        d = _normalize_cookie(c)
        n = str(d.get("name", "") or "").strip()
        v = str(d.get("value", "") or "").strip()
        if not n or not v:
            continue
        dom = str(d.get("domain", "") or "").strip()
        key = (n, dom)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": n, "value": v, "domain": dom,
                    "path": str(d.get("path", "/") or "/")})
    return out


def get_cookies_full(sb):
    try:
        r = _cdp_cmd(sb, "Network.getAllCookies", {})
        if r and r.get("cookies"):
            return _dedup_cookies(r["cookies"])
    except Exception:
        pass
    return []


def cookies_to_dict(cookies_full):
    d = {}
    for c in cookies_full:
        n, v = c.get("name", ""), c.get("value", "")
        if n and v:
            d[n] = v
    return d


def _get_cookies(sb):
    return cookies_to_dict(get_cookies_full(sb))


# ═══════════════════════════════════════════════════════════════
# m3u8 parser + deep resolution
# ═══════════════════════════════════════════════════════════════
def _parse_m3u8_robust(text, base_url):
    segs, variants = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        full = line if line.startswith('http') else urljoin(base_url + '/', line)
        if '.m3u8' in line.lower():
            variants.append(full)
        else:
            segs.append(full)
    return segs, variants


def _resolve_m3u8_deep(m3u8_url, headers, depth=0, max_depth=5):
    if depth > max_depth:
        return None, []
    try:
        r = cffi_requests.get(m3u8_url, headers=headers,
                              impersonate="chrome120", timeout=30, verify=False)
        if r.status_code != 200:
            return None, []
    except Exception:
        return None, []

    base = m3u8_url.rsplit('/', 1)[0]
    segs, variants = _parse_m3u8_robust(r.text, base)

    if segs:
        return m3u8_url, segs

    if variants:
        for v in variants[:3]:
            final, vsegs = _resolve_m3u8_deep(v, headers, depth + 1, max_depth)
            if vsegs:
                return final, vsegs

    return None, []


# ═══════════════════════════════════════════════════════════════
# ★★★ cffi segments — الأسرع
# ═══════════════════════════════════════════════════════════════
def try_cffi_segments(segments, out_path, iframe_url, cookies_dict):
    if not segments:
        return None

    headers = {
        "Referer": iframe_url or "",
        "Origin": _origin(iframe_url),
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())[:8000]
    if cookie_str:
        headers["Cookie"] = cookie_str

    # اختبار سريع
    test_data = None
    for imp in ["chrome124", "chrome120", "chrome110"]:
        try:
            r = cffi_requests.get(segments[0], headers=headers,
                                  impersonate=imp, timeout=15, verify=False)
            if r.status_code == 200 and len(r.content) > 100:
                test_data = r.content
                print(f"      ⚡ cffi[{imp}] OK ({len(test_data)}b)", flush=True)
                break
        except Exception:
            continue

    if test_data is None:
        return None

    print(f"   ⚡ {len(segments)} segment عبر cffi...", flush=True)
    seg_dir = tempfile.mkdtemp(prefix="hls_cffi_")
    seg_paths, failed, total_bytes = {}, 0, 0

    def _dl(idx_url):
        idx, url = idx_url
        for imp in ["chrome124", "chrome120"]:
            try:
                r = cffi_requests.get(url, headers=headers,
                                      impersonate=imp, timeout=30, verify=False)
                if r.status_code == 200 and len(r.content) > 100:
                    p = os.path.join(seg_dir, f"seg_{idx:06d}.ts")
                    with open(p, 'wb') as f:
                        f.write(r.content)
                    return (idx, p, len(r.content))
            except Exception:
                continue
        return (idx, None, 0)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=CURL_CFFI_WORKERS) as ex:
        futures = [ex.submit(_dl, (i, s)) for i, s in enumerate(segments)]
        done = 0
        for fut in as_completed(futures):
            idx, p, size = fut.result()
            done += 1
            if p:
                seg_paths[idx] = p
                total_bytes += size
            else:
                failed += 1
            if done % 40 == 0 or done == len(segments):
                print(f"      📦 {done}/{len(segments)} | {total_bytes/1048576:.1f}MB | فشل: {failed}",
                      flush=True)

    if not seg_paths or failed > len(segments) * 0.15:
        shutil.rmtree(seg_dir, ignore_errors=True)
        print(f"      ❌ cffi فشل ({failed}/{len(segments)})", flush=True)
        return None

    sorted_segs = [seg_paths[k] for k in sorted(seg_paths.keys())]
    concat_file = os.path.join(seg_dir, "concat.txt")
    with open(concat_file, 'w') as f:
        for p in sorted_segs:
            f.write(f"file '{p}'\n")

    print(f"   🔗 دمج {len(sorted_segs)} segment...", flush=True)
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'warning',
           '-f', 'concat', '-safe', '0', '-i', concat_file,
           '-c', 'copy', '-bsf:a', 'aac_adtstoasc',
           '-movflags', '+faststart',
           '-f', 'mp4', '-y', out_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not os.path.exists(out_path):
            shutil.rmtree(seg_dir, ignore_errors=True)
            return None
    except Exception:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    final_size = os.path.getsize(out_path)
    print(f"   ✅ cffi: {final_size/1048576:.1f}MB", flush=True)
    shutil.rmtree(seg_dir, ignore_errors=True)
    return (final_size, True)


# ═══════════════════════════════════════════════════════════════
# browser XHR segments (fallback)
# ═══════════════════════════════════════════════════════════════
def _poll_js(sb, done_var, result_var, timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        sb.cdp.sleep(0.15)
        try:
            if sb.cdp.execute_script(f"return window.{done_var} === true"):
                return sb.cdp.execute_script(f"return window.{result_var}")
        except Exception:
            pass
    return None


def browser_fetch_batch_b64(sb, urls, timeout=30):
    if not urls:
        return {}
    urls_json = json.dumps(urls)
    js = """
    (function(){
        window.__br = {}; window.__bd = false;
        var urls = %s;
        var results = {}; var pending = urls.length;
        if (pending === 0) { window.__br = results; window.__bd = true; return; }
        urls.forEach(function(u, idx){
            var done = function(val){
                results[String(idx)] = val;
                pending--;
                if (pending === 0) { window.__br = results; window.__bd = true; }
            };
            try {
                fetch(u, {mode:'cors'})
                    .then(function(r){
                        if (!r.ok) throw new Error('HTTP '+r.status);
                        return r.arrayBuffer();
                    })
                    .then(function(buf){
                        var bytes = new Uint8Array(buf);
                        var bin = ''; var chunk = 16384;
                        for (var j=0; j<bytes.length; j+=chunk) {
                            bin += String.fromCharCode.apply(null,
                                bytes.subarray(j, Math.min(j+chunk, bytes.length)));
                        }
                        try { done(btoa(bin)); } catch(e) { done(null); }
                    })
                    .catch(function(){ done(null); });
            } catch(e) { done(null); }
        });
    })();
    """ % urls_json
    try:
        sb.cdp.execute_script(js)
    except Exception:
        return {}
    r = _poll_js(sb, "__bd", "__br", timeout)
    if isinstance(r, dict):
        return r
    return {}


def download_segments_via_browser(sb, segments, out_path):
    total = len(segments)
    print(f"   🌐 المتصفح: {total} segment...", flush=True)
    seg_dir = tempfile.mkdtemp(prefix="hls_br_")
    seg_paths, failed, total_bytes = {}, 0, 0

    for i in range(0, total, BROWSER_BATCH):
        batch = segments[i:i + BROWSER_BATCH]
        result = browser_fetch_batch_b64(sb, batch, timeout=BROWSER_BATCH_TIMEOUT)

        for idx_str, b64 in result.items():
            try:
                li = int(idx_str)
            except Exception:
                continue
            si = i + li
            if b64:
                try:
                    data = base64.b64decode(b64)
                    p = os.path.join(seg_dir, f"seg_{si:06d}.ts")
                    with open(p, 'wb') as f:
                        f.write(data)
                    seg_paths[si] = p
                    total_bytes += len(data)
                except Exception:
                    failed += 1
            else:
                failed += 1

        done = min(i + BROWSER_BATCH, total)
        if done % (BROWSER_BATCH * 2) == 0 or done == total:
            print(f"      📦 {done}/{total} | {total_bytes/1048576:.1f}MB", flush=True)

    if not seg_paths:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    sorted_segs = [seg_paths[k] for k in sorted(seg_paths.keys())]
    concat_file = os.path.join(seg_dir, "concat.txt")
    with open(concat_file, 'w') as f:
        for p in sorted_segs:
            f.write(f"file '{p}'\n")

    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'warning',
           '-f', 'concat', '-safe', '0', '-i', concat_file,
           '-c', 'copy', '-bsf:a', 'aac_adtstoasc',
           '-movflags', '+faststart',
           '-f', 'mp4', '-y', out_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not os.path.exists(out_path):
            shutil.rmtree(seg_dir, ignore_errors=True)
            return None
    except Exception:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    final_size = os.path.getsize(out_path)
    print(f"   ✅ المتصفح: {final_size/1048576:.1f}MB", flush=True)
    shutil.rmtree(seg_dir, ignore_errors=True)
    return (final_size, True)


# ═══════════════════════════════════════════════════════════════
# yt-dlp مع stall detection
# ═══════════════════════════════════════════════════════════════
def _try_ytdlp_with_headers(url, out_path, referer, cookies_dict):
    print(f"      [yt-dlp] {url[:80]}", flush=True)
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())[:8000]

    cmd = [
        "yt-dlp", '--no-warnings', '--no-playlist', '--no-part',
        '--retries', '15', '--fragment-retries', '30',
        '--socket-timeout', '60',
        '--concurrent-fragments', '16',
        '--no-check-certificate', '--continue',
        '--hls-use-mpegts', '--hls-prefer-native',
        '--impersonate', 'chrome',
        '--user-agent', UA,
        '--referer', referer,
        '--add-header', 'Accept:*/*',
    ]
    if cookie_str:
        cmd += ['--add-header', f'Cookie:{cookie_str}']
    cmd += ['-o', out_path, url]

    log_path = out_path + ".ytdlp.log"
    try:
        log_file = open(log_path, 'w', encoding='utf-8', errors='replace')
    except Exception:
        return False

    try:
        proc = subprocess.Popen(cmd, stdout=log_file,
                                stderr=subprocess.STDOUT, text=True)
    except Exception:
        log_file.close()
        return False

    start = time.time()
    last_size = 0
    last_change = start
    best_size = 0
    try:
        while proc.poll() is None:
            time.sleep(3)
            now = time.time()
            size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
            if size > last_size:
                last_size = size
                last_change = now
                if size > best_size:
                    best_size = size
            if now - last_change > STALL_TIMEOUT and size > 0:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
                log_file.close()
                return best_size >= MIN_PARTIAL_ACCEPT
            if now - start > YTDLP_TIMEOUT:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
                log_file.close()
                return best_size >= MIN_PARTIAL_ACCEPT

        log_file.close()
        size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        return size >= MIN_SIZE
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        log_file.close()
        return False


# ═══════════════════════════════════════════════════════════════
# ★★★ تحميل m3u8 عبر cffi + deep resolution
# ═══════════════════════════════════════════════════════════════
def _download_with_curl_deep(m3u8_url, out_path, referer, cookies_dict):
    print(f"      [cffi-deep] {m3u8_url[:80]}", flush=True)

    headers = {
        "Referer": referer or "",
        "Origin": _origin(referer),
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "ar,en;q=0.9",
    }
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())[:8000]
    if cookie_str:
        headers["Cookie"] = cookie_str

    final_url, segments = _resolve_m3u8_deep(m3u8_url, headers)
    if not segments:
        print(f"      ❌ لا segments", flush=True)
        return False

    print(f"      ⬇️ {len(segments)} segment...", flush=True)

    seg_dir = tempfile.mkdtemp(prefix="hls_deep_")
    seg_paths, failed, total_bytes = {}, 0, 0

    def _dl(idx_url):
        idx, u = idx_url
        for attempt in range(2):
            for imp in ["chrome124", "chrome120"]:
                try:
                    rr = cffi_requests.get(u, headers=headers,
                                           impersonate=imp,
                                           timeout=90, verify=False)
                    if rr.status_code == 200 and len(rr.content) > 100:
                        p = os.path.join(seg_dir, f"seg_{idx:06d}.ts")
                        with open(p, 'wb') as f:
                            f.write(rr.content)
                        return (idx, p, len(rr.content))
                except Exception:
                    continue
            time.sleep(1)
        return (idx, None, 0)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=CURL_CFFI_WORKERS) as ex:
        futures = [ex.submit(_dl, (i, s)) for i, s in enumerate(segments)]
        done = 0
        for fut in as_completed(futures):
            idx, p, size = fut.result()
            done += 1
            if p:
                seg_paths[idx] = p
                total_bytes += size
            else:
                failed += 1
            if done % 50 == 0 or done == len(segments):
                print(f"         📦 {done}/{len(segments)} | {total_bytes/1048576:.1f}MB",
                      flush=True)

    if not seg_paths:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return False

    sorted_segs = [seg_paths[k] for k in sorted(seg_paths.keys())]
    concat_file = os.path.join(seg_dir, "concat.txt")
    with open(concat_file, 'w') as f:
        for p in sorted_segs:
            f.write(f"file '{p}'\n")

    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'warning',
           '-f', 'concat', '-safe', '0', '-i', concat_file,
           '-c', 'copy', '-bsf:a', 'aac_adtstoasc',
           '-movflags', '+faststart',
           '-f', 'mp4', '-y', out_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        ok = r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        ok = False
    shutil.rmtree(seg_dir, ignore_errors=True)
    if ok:
        print(f"      ✅ {os.path.getsize(out_path)/1048576:.1f}MB", flush=True)
    return ok


# ═══════════════════════════════════════════════════════════════
# كشف m3u8
# ═══════════════════════════════════════════════════════════════
def extract_m3u8_from_text(text):
    if not text:
        return []
    found, seen = [], set()
    for m in re.finditer(r'(https?:[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*)', text):
        u = m.group(1).replace('\\/', '/')
        if u not in seen:
            seen.add(u)
            found.append(u)
    for m in re.finditer(r'(https?:[^\s"\'<>\\]+\.mpd[^\s"\'<>\\]*)', text):
        u = m.group(1).replace('\\/', '/')
        if u not in seen:
            seen.add(u)
            found.append(u)
    return found


def extract_servers(html):
    servers = []
    for pat, order in [
        (re.compile(r'<li[^>]*id=["\'](s_\d+)["\'][^>]*on[Cc]lick=["\']getServer2\([^,]+,\s*(\d+)\s*,\s*(\d+)\s*\)', re.I), "id_first"),
        (re.compile(r'on[Cc]lick=["\']getServer2\([^,]+,\s*(\d+)\s*,\s*(\d+)\s*\)[^>]*id=["\'](s_\d+)["\']', re.I), "click_first"),
    ]:
        for m in pat.finditer(html):
            g = m.groups()
            if order == "id_first":
                servers.append({"id": g[0], "name": g[0], "video": g[1], "serverId": g[2]})
            else:
                servers.append({"id": g[2], "name": g[2], "video": g[0], "serverId": g[1]})
        if servers:
            break
    return servers


def scan_for_m3u8(sb, netlog_read):
    found = set()

    with _NET_LOCK:
        for u in list(_RESP_BODIES.keys()):
            if ".m3u8" in u or ".mpd" in u:
                found.add(u)

    try:
        for u in netlog_read():
            if ".m3u8" in u or ".mpd" in u:
                found.add(u)
    except Exception:
        pass

    try:
        perf = sb.cdp.execute_script("""
            (function(){try{
                return performance.getEntriesByType('resource').map(e => e.name);
            }catch(e){return [];}})();
        """)
        if isinstance(perf, list):
            for u in perf:
                if ".m3u8" in u or ".mpd" in u:
                    found.add(u)
    except Exception:
        pass

    try:
        html = sb.cdp.get_page_source() or ""
        for u in extract_m3u8_from_text(html):
            found.add(u)
    except Exception:
        pass

    try:
        js_urls = sb.cdp.execute_script("""
            (function(){
                var found = [];
                try {
                    if (typeof jwplayer !== 'undefined') {
                        var p = jwplayer();
                        if (p && p.getPlaylist) {
                            var pl = p.getPlaylist();
                            if (pl) pl.forEach(function(item) {
                                if (item.file) found.push(item.file);
                                if (item.sources) item.sources.forEach(function(s){
                                    if (s.file) found.push(s.file);
                                });
                            });
                        }
                    }
                    if (typeof videojs !== 'undefined') {
                        var p2 = videojs.getPlayers();
                        for (var k in p2) {
                            try {
                                var src = p2[k].currentSrc && p2[k].currentSrc();
                                if (src) found.push(src);
                            } catch(e){}
                        }
                    }
                } catch(e){}
                return found;
            })();
        """)
        if isinstance(js_urls, list):
            for u in js_urls:
                if isinstance(u, str) and (".m3u8" in u or ".mpd" in u):
                    found.add(u)
    except Exception:
        pass

    urls = [u for u in found if "ping.gif" not in u and "jwpltx" not in u]
    idx = [u for u in urls if "index" in u.lower()]
    mst = [u for u in urls if "master" in u.lower()]
    mpd = [u for u in urls if ".mpd" in u.lower()]
    oth = [u for u in urls if u not in idx and u not in mst and u not in mpd]
    return idx + mst + mpd + oth


# ═══════════════════════════════════════════════════════════════
# نقرات تشغيل
# ═══════════════════════════════════════════════════════════════
def trigger_play(sb):
    for sel in ["video", ".jw-icon-playback", ".jw-icon-display",
                ".jw-display-icon-container", "[class*='play']",
                ".vjs-big-play-button", ".jwplayer",
                "button[aria-label*='Play']", ".play-button"]:
        for _ in range(2):
            try:
                sb.cdp.click_if_visible(sel)
            except Exception:
                pass

    try:
        rect = sb.cdp.execute_script("""
            (function(){
                var cs = [document.querySelector('video'), document.querySelector('iframe')];
                for (var i=0;i<cs.length;i++) {
                    var el = cs[i];
                    if (!el) continue;
                    var r = el.getBoundingClientRect();
                    if (r.width > 50 && r.height > 50)
                        return {x: Math.round(r.left+r.width/2), y: Math.round(r.top+r.height/2)};
                }
                return null;
            })();
        """)
        if rect and rect.get("x", 0) > 0:
            for _ in range(5):
                sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mousePressed", "x": rect['x'], "y": rect['y'],
                    "button": "left", "clickCount": 1,
                })
                sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mouseReleased", "x": rect['x'], "y": rect['y'],
                    "button": "left", "clickCount": 1,
                })
                sb.cdp.sleep(0.5)
    except Exception:
        pass

    try:
        sb.cdp.execute_script("""
            (function(){try{
                if(typeof jwplayer!=='undefined'){
                    var p=jwplayer();
                    if(p){ if(p.play)p.play(true); if(p.setMute)p.setMute(true); }
                }
                if(typeof videojs!=='undefined'){
                    var p2 = videojs.getPlayers();
                    for (var k in p2) { try { p2[k].play(); p2[k].muted(true); } catch(e){} }
                }
                var v=document.querySelector('video');
                if(v){ v.muted=true; if(v.play)v.play().catch(function(){}); v.click(); }
            }catch(e){}})();
        """)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# فتح الصفحة بمهلة
# ═══════════════════════════════════════════════════════════════
def _open_with_timeout(sb, url, timeout=OPEN_TIMEOUT):
    result = [False]

    def _do():
        try:
            sb.driver.execute_cdp_cmd("Page.navigate", {"url": url})
            result[0] = True
            # انتظر DOMContentLoaded فقط
            for _ in range(int(timeout * 2)):
                time.sleep(0.5)
                try:
                    state = sb.driver.execute_cdp_cmd("Runtime.evaluate", {
                        "expression": "document.readyState",
                        "returnByValue": True,
                    })
                    if state and state.get("result", {}).get("value") in ("interactive", "complete"):
                        break
                except Exception:
                    pass
        except Exception as e:
            print(f"      ⚠️ navigate: {str(e)[:80]}", flush=True)

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=timeout + 10)

    if t.is_alive():
        try:
            sb.driver.execute_cdp_cmd("Page.stopLoading", {})
        except Exception:
            pass
        print(f"      ⏱️ تجاوز {timeout}s — متابعة", flush=True)

    return True


# ═══════════════════════════════════════════════════════════════
# CF bypass
# ═══════════════════════════════════════════════════════════════
def _bypass_cloudflare(sb, iframe_url, timeout_seconds=30):
    print(f"      🔄 CF bypass ({timeout_seconds}s)...", flush=True)
    start = time.time()
    try:
        try:
            sb.driver.execute_cdp_cmd("Page.navigate", {"url": iframe_url, "referrer": ""})
        except Exception:
            return False
        sb.cdp.sleep(2)

        for _ in range(2):
            if time.time() - start > timeout_seconds:
                return False
            try:
                sb.uc_gui_click_captcha()
            except Exception:
                pass
            sb.cdp.sleep(2)
            try:
                title = sb.get_page_title() or ""
                html = sb.cdp.get_page_source() or ""
                if ("Just a moment" not in title
                        and "Attention Required" not in title
                        and len(html) > 5000):
                    print(f"      ✅ CF OK ({time.time()-start:.0f}s)", flush=True)
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# ★★★ معالج عام لأي صفحة فيديو (yam, shhaiid4u, ...)
# ═══════════════════════════════════════════════════════════════
def _process_generic_video_page(sb, url, out_path):
    """
    يفتح أي صفحة فيديو ويحاول التحميل:
      • نقرات متعددة على الفيديو
      • iframe متداخل
      • التقاط m3u8 عبر Network / performance / DOM
      • تحميل بـ cffi-deep → yt-dlp
    """
    print(f"   🌐 معالج عام: {url[:80]}", flush=True)

    netlog = tempfile.mktemp(suffix="_gen.txt")
    open(netlog, "w").close()

    def _log(u):
        try:
            with open(netlog, "a", encoding="utf-8") as fh:
                fh.write(u + "\n")
        except Exception:
            pass

    def _read():
        try:
            with open(netlog, encoding="utf-8") as fh:
                return [l.strip() for l in fh if l.strip()]
        except Exception:
            return []

    # network handler
    try:
        import mycdp
        async def on_req(params):
            try:
                req = params.get("request", {}) or {}
                u = req.get("url", "")
                if u:
                    _log(u)
            except Exception:
                pass
        sb.cdp.add_handler(mycdp.network.RequestWillBeSent, on_req)
    except Exception:
        pass

    # فتح الصفحة
    print(f"🖥️  فتح: {url[:90]}", flush=True)
    if not _open_with_timeout(sb, url, timeout=OPEN_TIMEOUT):
        try:
            os.remove(netlog)
        except Exception:
            pass
        return None
    sb.cdp.sleep(3)

    # نقرات متعددة على الفيديو
    for cycle in range(8):
        try:
            sb.cdp.execute_script("""
                (function(){try{
                    var v = document.querySelector('video');
                    if (v) { v.muted=true; v.play && v.play().catch(function(){}); }
                    ['video','.vjs-big-play-button','.jw-icon-playback',
                     '[class*=play]','button'].forEach(function(s){
                        var el = document.querySelector(s);
                        if (el) try { el.click(); } catch(e){}
                    });
                }catch(e){}})();
            """)
        except Exception:
            pass

        try:
            rect = sb.cdp.execute_script("""
                (function(){
                    var v = document.querySelector('video');
                    if (!v) return null;
                    var r = v.getBoundingClientRect();
                    if (r.width < 50) return null;
                    return {x: Math.round(r.left+r.width/2),
                            y: Math.round(r.top+r.height/2)};
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
        except Exception:
            pass

        sb.cdp.sleep(1.5)

    # iframe متداخل (yam)
    for _ in range(3):
        try:
            ifr = sb.cdp.execute_script("""
                (function(){
                    var f = document.querySelector('iframe');
                    if (f && f.src && f.src.startsWith('http')
                        && f.src.indexOf('google') === -1
                        && f.src.indexOf('facebook') === -1) {
                        return f.src;
                    }
                    return null;
                })();
            """)
            if not ifr:
                break
            print(f"      🔄 iframe: {ifr[:80]}", flush=True)
            sb.driver.execute_cdp_cmd("Page.navigate", {"url": ifr, "referrer": url})
            sb.cdp.sleep(3)
            # نقرات داخل iframe
            for _ in range(3):
                try:
                    sb.cdp.execute_script("""
                        (function(){try{
                            var v = document.querySelector('video');
                            if (v) { v.muted=true; v.play && v.play().catch(function(){}); }
                        }catch(e){}})();
                    """)
                except Exception:
                    pass
                sb.cdp.sleep(1)
        except Exception:
            break

    # بحث عن m3u8
    print(f"      🎬 البحث عن m3u8 ({M3U8_SEARCH_TIMEOUT}s)...", flush=True)
    search_start = time.time()
    m3u8_urls = []

    while time.time() - search_start < M3U8_SEARCH_TIMEOUT:
        found = scan_for_m3u8(sb, _read)
        if found:
            m3u8_urls = found
            print(f"      ✨ {len(found)} m3u8 بعد {time.time()-search_start:.0f}s", flush=True)
            break
        sb.cdp.sleep(2)

    if not m3u8_urls:
        print(f"      ❌ لا m3u8", flush=True)
        try:
            os.remove(netlog)
        except Exception:
            pass
        return None

    for u in m3u8_urls[:3]:
        print(f"         · {u[:110]}", flush=True)

    cookies = _get_cookies(sb)
    ref = sb.cdp.get_current_url() or url

    # 1) cffi-deep
    for m3u8 in m3u8_urls[:3]:
        if _download_with_curl_deep(m3u8, str(out_path), ref, cookies):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                try:
                    os.remove(netlog)
                except Exception:
                    pass
                return (os.path.getsize(out_path), True)

    # 2) yt-dlp
    for m3u8 in m3u8_urls[:3]:
        if _try_ytdlp_with_headers(m3u8, str(out_path), ref, cookies):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                try:
                    os.remove(netlog)
                except Exception:
                    pass
                return (os.path.getsize(out_path), True)

    try:
        os.remove(netlog)
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════
# العملية الرئيسية — u3seq مع جلسة متصفح كاملة
# ═══════════════════════════════════════════════════════════════
def _process_with_browser(url, out_path):
    from seleniumbase import SB

    netlog = tempfile.mktemp(suffix="_netlog.txt")
    open(netlog, "w").close()

    def _log(u):
        try:
            with open(netlog, "a", encoding="utf-8") as fh:
                fh.write(u + "\n")
        except Exception:
            pass

    def _read():
        try:
            with open(netlog, encoding="utf-8") as fh:
                return [l.strip() for l in fh if l.strip()]
        except Exception:
            return []

    try:
        with SB(uc=True, xvfb=True, headless=False, incognito=True,
                ad_block_on=True, disable_csp=True,
                page_load_strategy="eager", locale_code="en") as sb:
            try:
                sb.activate_cdp_mode()

                # التقاط
                _enable_network_capture(sb)
                _enable_fetch_capture(sb)

                # interceptor JS
                try:
                    sb.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                        "source": """
                        (function(){
                            window.__captured_m3u8 = window.__captured_m3u8 || [];
                            function record(u){
                                try{
                                    if(typeof u === 'string' &&
                                       (u.indexOf('.m3u8')!==-1 || u.indexOf('.mpd')!==-1)){
                                        if(window.__captured_m3u8.indexOf(u)===-1)
                                            window.__captured_m3u8.push(u);
                                    }
                                }catch(e){}
                            }
                            if(window.fetch && !window.__fp){
                                var o = window.fetch;
                                window.fetch = function(i,init){
                                    try{ var u=(typeof i==='string')?i:(i&&i.url); record(u);}catch(e){}
                                    return o.apply(this,arguments);
                                };
                                window.__fp = true;
                            }
                            if(window.XMLHttpRequest && !window.__xp){
                                var xo = XMLHttpRequest.prototype.open;
                                XMLHttpRequest.prototype.open = function(m,u){
                                    try{record(u);}catch(e){}
                                    return xo.apply(this,arguments);
                                };
                                window.__xp = true;
                            }
                        })();
                        """,
                    })
                except Exception:
                    pass

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
                                    with _NET_LOCK:
                                        _REQ_IDS[u] = rid
                        except Exception:
                            pass
                    sb.cdp.add_handler(mycdp.network.RequestWillBeSent, on_req)
                except Exception:
                    pass

                # ═══ فتح الصفحة الرئيسية ═══
                print(f"🖥️  فتح: {url[:90]}", flush=True)
                _open_with_timeout(sb, url, timeout=OPEN_TIMEOUT)
                sb.cdp.sleep(2)

                # إذا كان الرابط يحتوي ?do=watch، استخدمه مباشرة
                if "?do=watch" in url:
                    watch_url = url
                else:
                    cur = sb.cdp.get_current_url() or url
                    if "?" in cur:
                        cur = cur.split("?")[0]
                    if not cur.endswith("/"):
                        cur += "/"
                    watch_url = cur + "?do=watch"
                    _open_with_timeout(sb, watch_url, timeout=OPEN_TIMEOUT)
                    sb.cdp.sleep(3)

                # ═══ انتظار السيرفرات ═══
                servers = []
                for i in range(20):
                    sb.cdp.sleep(1)
                    try:
                        servers = sb.cdp.execute_script("""
                            (function(){
                                var l = document.querySelector('.serversList');
                                if (!l) return [];
                                return Array.from(l.querySelectorAll('li')).map(li => ({
                                    id: li.id || '',
                                    name: (li.textContent || '').trim()
                                }));
                            })();
                        """) or []
                        if servers:
                            print(f"   ✅ {len(servers)} سيرفر", flush=True)
                            break
                    except Exception:
                        pass

                # ★★★ لا سيرفرات → المعالج العام ═══
                if not servers:
                    print(f"   🔄 لا سيرفرات — المعالج العام", flush=True)
                    res = _process_generic_video_page(sb, url, out_path)
                    try:
                        os.remove(netlog)
                    except Exception:
                        pass
                    return res

                # ═══ جمع iframes ═══
                print(f"\n   📋 جمع iframes...", flush=True)
                iframe_map = {}
                seen = set()

                for i in range(10):
                    sb.cdp.sleep(0.5)
                    if sb.cdp.execute_script("return typeof getServer2 === 'function'"):
                        break

                for srv in servers:
                    sid = srv.get("id")
                    if not sid:
                        continue
                    before = sb.cdp.execute_script("""
                        (function(){
                            var ifr = document.querySelector('.watch iframe');
                            return ifr ? ifr.src : null;
                        })();
                    """)
                    try:
                        rect = sb.cdp.execute_script(f"""
                            (function(){{
                                var el = document.getElementById('{sid}');
                                if (!el) return null;
                                var r = el.getBoundingClientRect();
                                if (r.width < 5 || r.height < 5) return null;
                                return {{x: Math.round(r.left+r.width/2),
                                        y: Math.round(r.top+r.height/2)}};
                            }})();
                        """)
                        if rect and rect.get("x", 0) > 0:
                            sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                                "type": "mousePressed", "x": rect['x'], "y": rect['y'],
                                "button": "left", "clickCount": 1,
                            })
                            sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                                "type": "mouseReleased", "x": rect['x'], "y": rect['y'],
                                "button": "left", "clickCount": 1,
                            })
                    except Exception:
                        continue

                    after = before
                    deadline = time.time() + 4
                    while time.time() < deadline:
                        sb.cdp.sleep(0.3)
                        c = sb.cdp.execute_script("""
                            (function(){
                                var ifr = document.querySelector('.watch iframe');
                                return ifr ? ifr.src : null;
                            })();
                        """)
                        if c and c != before:
                            after = c
                            break
                    if after and after != before:
                        u2 = after.replace("&amp;", "&")
                        if u2 not in seen:
                            seen.add(u2)
                            iframe_map[f"{srv.get('name')}_{sid}"] = u2

                if not iframe_map:
                    print(f"   ⚠️ لا iframes", flush=True)
                    res = _process_generic_video_page(sb, url, out_path)
                    try:
                        os.remove(netlog)
                    except Exception:
                        pass
                    return res

                print(f"   ✅ {len(iframe_map)} iframe", flush=True)

                def _prio(item):
                    k = item[0].lower()
                    for i, s in enumerate(SERVER_PRIORITY):
                        if s in k:
                            return i
                    return 99

                ordered = sorted(iframe_map.items(), key=_prio)

                # ═══ تجربة كل سيرفر ═══
                for sname, iframe_url in ordered:
                    print(f"\n   ═══ {sname} ═══", flush=True)
                    try:
                        if "luluvdo" in sname.lower():
                            if not _bypass_cloudflare(sb, iframe_url, 30):
                                print(f"      ⏭️ CF فشل", flush=True)
                                continue
                            sb.cdp.sleep(2)
                        else:
                            try:
                                sb.driver.execute_cdp_cmd("Page.navigate", {
                                    "url": iframe_url, "referrer": watch_url,
                                })
                            except Exception:
                                continue
                            sb.cdp.sleep(3)

                        # iframe متداخل
                        for _ in range(3):
                            try:
                                n = sb.cdp.execute_script("""
                                    (function(){
                                        var ifr = document.querySelector('iframe');
                                        if (ifr && ifr.src && ifr.src.startsWith('http')
                                            && ifr.src.indexOf('google') === -1)
                                            return ifr.src;
                                        return null;
                                    })();
                                """)
                                if n and n != iframe_url:
                                    print(f"      🔄 متداخل: {n[:70]}", flush=True)
                                    iframe_url = n
                                    sb.driver.execute_cdp_cmd("Page.navigate", {
                                        "url": iframe_url, "referrer": watch_url,
                                    })
                                    sb.cdp.sleep(3)
                                else:
                                    break
                            except Exception:
                                break

                        # انتظار المشغل
                        for tick in range(JWPLAYER_WAIT):
                            sb.cdp.sleep(1)
                            try:
                                p = sb.cdp.execute_script(
                                    "return typeof jwplayer!=='undefined'?'jw':"
                                    "(typeof videojs!=='undefined'?'vjs':"
                                    "(document.querySelector('video')?'h5':'none'))"
                                )
                                if p in ("jw", "vjs", "h5"):
                                    print(f"      ✅ {p}", flush=True)
                                    break
                            except Exception:
                                pass

                        # تشغيل
                        trigger_play(sb)

                        # بحث عن m3u8
                        print(f"      🎬 البحث عن m3u8 ({M3U8_SEARCH_TIMEOUT}s)...", flush=True)
                        search_start = time.time()
                        m3u8_urls = []

                        while time.time() - search_start < M3U8_SEARCH_TIMEOUT:
                            trigger_play(sb)
                            sb.cdp.sleep(2)
                            found = scan_for_m3u8(sb, _read)
                            if found:
                                m3u8_urls = found
                                print(f"      ✨ {len(found)} m3u8 بعد {time.time()-search_start:.0f}s",
                                      flush=True)
                                break

                        if not m3u8_urls:
                            m3u8_urls = scan_for_m3u8(sb, _read)
                        if not m3u8_urls:
                            print(f"      ❌ لا m3u8", flush=True)
                            continue

                        for u in m3u8_urls[:3]:
                            print(f"         · {u[:100]}", flush=True)

                        cookies = _get_cookies(sb)
                        sb.cdp.sleep(2)

                        # 1) cffi-deep
                        ok = False
                        for m3u8 in m3u8_urls[:M3U8_CANDIDATE_LIMIT]:
                            if _download_with_curl_deep(m3u8, str(out_path),
                                                         iframe_url, cookies):
                                if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                                    ok = True
                                    break
                            if os.path.exists(out_path):
                                try:
                                    os.remove(out_path)
                                except Exception:
                                    pass

                        # 2) yt-dlp
                        if not ok:
                            for m3u8 in m3u8_urls[:3]:
                                if _try_ytdlp_with_headers(m3u8, str(out_path),
                                                            iframe_url, cookies):
                                    if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                                        ok = True
                                        break
                                if os.path.exists(out_path):
                                    try:
                                        os.remove(out_path)
                                    except Exception:
                                        pass

                        if ok:
                            try:
                                os.remove(netlog)
                            except Exception:
                                pass
                            return (os.path.getsize(out_path), True)

                        print(f"   ⏭️ فشل: {sname}", flush=True)
                    except Exception as e:
                        print(f"   ❌ {str(e)[:120]}", flush=True)
                        continue

                # كل السيرفرات فشلت → جرّب المعالج العام
                print(f"\n   🔄 جميع السيرفرات فشلت — المعالج العام", flush=True)
                res = _process_generic_video_page(sb, url, out_path)
                try:
                    os.remove(netlog)
                except Exception:
                    pass
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
        try:
            os.remove(netlog)
        except Exception:
            pass

    return None


# ═══════════════════════════════════════════════════════════════
# الضغط
# ═══════════════════════════════════════════════════════════════
def _get_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return 0


def _compress_to_target(inp, out):
    max_size_mb = getattr(config, "COMPRESS_MAX_SIZE_MB", 45)
    scale = getattr(config, "COMPRESS_SCALE", 240)
    crf = getattr(config, "COMPRESS_CRF", 32)
    preset = getattr(config, "COMPRESS_PRESET", "slow")
    audio_br = getattr(config, "COMPRESS_AUDIO_BITRATE", "32k")

    im = Path(inp).stat().st_size / 1048576
    print(f"   🗜️  {im:.2f}MB → {scale}p (حد {max_size_mb}MB)...", flush=True)

    ff = _get_ffmpeg_exe()
    cmd = [
        ff, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-err_detect", "ignore_err",
        "-i", str(inp),
        "-vf", f"scale=-2:{scale}",
        "-c:v", "libx264", "-preset", preset,
        "-crf", str(crf),
        "-profile:v", "main", "-level", "3.1", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", audio_br,
        "-ac", "2", "-ar", "44100",
        "-movflags", "+faststart", "-threads", "2",
        "-y", str(out),
    ]

    try:
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode == 0 and Path(out).exists():
            om = Path(out).stat().st_size / 1048576
            print(f"   ✅ {im:.2f}→{om:.2f}MB في {time.time()-t0:.1f}s", flush=True)
            if om <= max_size_mb:
                return True
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
        except Exception:
            pass
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

    # ★ فحص صلاحية الرابط
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
            subprocess.run(["pkill", "-9", "-f", p],
                           capture_output=True, timeout=5)
        res = None
    except Exception as e:
        print(f"    ⚠️ {str(e)[:150]}", flush=True)
        res = None

    if not res or not raw.exists():
        print(f"    ⚠️ فشل تحميل {series_name} — {file_prefix}", flush=True)
        try:
            if raw.exists():
                raw.unlink()
        except Exception:
            pass
        return None

    size = raw.stat().st_size
    print(f"    📦 {size/1048576:.1f}MB", flush=True)

    if getattr(config, "SKIP_COMPRESS", False):
        shutil.move(str(raw), str(final))
    else:
        if not _compress_to_target(raw, final):
            print("    ⚠️ فشل الضغط — استخدام الأصلي", flush=True)
            shutil.move(str(raw), str(final))

    if raw.exists():
        raw.unlink()
    if not final.exists():
        return None

    # thumbnail
    try:
        thumb_path = out_dir / f"{file_prefix}.jpg"
        _make_thumbnail(final, thumb_path)
    except Exception:
        pass

    print(f"    ✅ {final.name} ({final.stat().st_size/1048576:.1f}MB)", flush=True)
    return final