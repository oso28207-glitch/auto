"""
downloader.py — Universal HLS Downloader v20 (Unified)
═══════════════════════════════════════════════════════
يدمج أفضل ما في v16.4 و v18.2:
  ⚡ جلسة متصفح واحدة لكل حلقة (v18.2)
  ⚡ حل عميق m3u8: master → variant → media (v16.4)
  ⚡ 4 استراتيجيات تحميل متتالية: cffi → browser → yt-dlp → deep-retry
  ⚡ التقاط m3u8 عبر Fetch.enable + Network.enable (v20)
  ⚡ كشف الروابط غير المدعومة (v20)
"""

import os, sys, time, json, base64, subprocess, shutil, asyncio, random, re, tempfile, threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, quote

from curl_cffi import requests as cffi_requests

from config import config, MEDIA_DIR

# ═══════════ الإعدادات ═══════════
MIN_VALID_SIZE = 100 * 1024
MIN_PARTIAL_ACCEPT = 30 * 1024 * 1024
MIN_EPISODE_DURATION = 600

# ⚡ تحميل متعدد
CURL_CFFI_WORKERS = int(os.environ.get("CURL_CFFI_WORKERS", "12"))
BROWSER_BATCH = int(os.environ.get("BROWSER_BATCH", "16"))
BROWSER_BATCH_TIMEOUT = 30
SEGMENT_RETRY = 2
M3U8_CANDIDATE_LIMIT = 5
M3U8_SEARCH_TIMEOUT = int(os.environ.get("M3U8_SEARCH_TIMEOUT", "40"))

# ⏱️ مهلات
YTDLP_TIMEOUT = 900
STALL_TIMEOUT = 90
FFMPEG_HLS_TIMEOUT = 1800
OPEN_TIMEOUT = int(os.environ.get("OPEN_TIMEOUT", "25"))

# ⏱️ إدارة
EPISODE_TIMEOUT_MAX = 22 * 60
MIN_EPISODE_TIME = 4 * 60

# 🗜️ الضغط
COMPRESS_PRESET = os.environ.get("COMPRESS_PRESET", "veryfast")
COMPRESS_CRF = int(os.environ.get("COMPRESS_CRF", "28"))
COMPRESS_THREADS = int(os.environ.get("COMPRESS_THREADS", "2"))
COMPRESS_SCALE = int(os.environ.get("COMPRESS_SCALE", "144"))

# أوقات انتظار
JWPLAYER_WAIT = 25
M3U8_CAPTURE_WAIT = 15

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# أولويات السيرفرات
SERVER_PRIORITY = [
    "luluvdo", "vinovo", "vidsonic", "playmate", "firestream",
    "vidaraa", "vids", "bysejikuar", "vidsp", "savefiles", "voe",
]

# حالة التقاط الشبكة
_REQ_IDS = {}
_RESP_BODIES = {}
_NET_LOCK = threading.Lock()


# ═══════════════════════════════════════════════════════════════
# أدوات مساعدة
# ═══════════════════════════════════════════════════════════════
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


def _is_valid_episode_url(url):
    """يرفض صفحات التصنيف ومواقع القوائم."""
    if not url or not isinstance(url, str):
        return False
    u = url.lower()
    bad = [
        "/moslslat.php", "/topvideos.php", "/all-series.php",
        "/series.php", "/category/", "/cats/", "/list/",
        "?cat=", "?category=",
    ]
    for b in bad:
        if b in u:
            return False
    # u3seq episode pattern
    if "modablaj-" in u or "/video/" in u or "/watch/" in u:
        return True
    # مشغلات مباشرة
    for m in ["/embed/", "firestream.to/", "playmate.to/", "luluvdo.com/",
              "vidsonic", "vidaraa", "/e/", "shhaiid4u.net"]:
        if m in u:
            return True
    return False


def _cdp_cmd(sb, cmd, params=None):
    """مساعد CDP موحّد."""
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
# CDP: Fetch.enable + Network.enable
# ═══════════════════════════════════════════════════════════════
def _enable_fetch_capture(sb):
    """يعترض m3u8/mpd عبر Fetch domain."""
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
    try:
        if len(c) >= 2:
            return {"name": c[0], "value": c[1]}
    except Exception:
        pass
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
        out.append({
            "name": n, "value": v, "domain": dom,
            "path": str(d.get("path", "/") or "/"),
            "secure": bool(d.get("secure", False)),
            "httpOnly": bool(d.get("httpOnly", False)),
            "expiry": int(d.get("expiry", 0) or 0),
        })
    return out


def get_cookies_full(sb):
    try:
        r = _cdp_cmd(sb, "Network.getAllCookies", {})
        if r and r.get("cookies"):
            return _dedup_cookies(r["cookies"])
    except Exception:
        pass
    try:
        raw = sb.cdp.get_all_cookies()
        if raw:
            return _dedup_cookies(raw)
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


# ═══════════════════════════════════════════════════════════════
# ★★★ من v16.4: الحل العميق لـ m3u8 + parser محسن
# ═══════════════════════════════════════════════════════════════
def _parse_m3u8_robust(text, base_url):
    """
    Parser محسن: يتعامل مع segments بدون .ts
    - أي سطر فيه .m3u8 → variant
    - أي سطر آخر (غير #) → segment
    """
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
    """
    ✅ من v16.4: يحل master → variant → media بشكل تكراري.
    يرجع (final_url, segments_list).
    """
    if depth > max_depth:
        return None, []
    try:
        r = cffi_requests.get(m3u8_url, headers=headers,
                              impersonate="chrome120", timeout=30, verify=False)
        if r.status_code != 200:
            print(f"      ⚠️ depth={depth} HTTP {r.status_code}", flush=True)
            return None, []
    except Exception as e:
        print(f"      ⚠️ depth={depth}: {str(e)[:80]}", flush=True)
        return None, []

    base = m3u8_url.rsplit('/', 1)[0]
    segs, variants = _parse_m3u8_robust(r.text, base)

    if segs:
        print(f"      ✅ depth={depth}: {len(segs)} segment", flush=True)
        return m3u8_url, segs

    if variants:
        print(f"      📋 depth={depth}: master → {len(variants)} variant", flush=True)
        for v in variants[:3]:
            final_url, vsegs = _resolve_m3u8_deep(v, headers, depth + 1, max_depth)
            if vsegs:
                return final_url, vsegs

    return None, []


# ═══════════════════════════════════════════════════════════════
# ★★★ من v18.2: cffi segments (الأسرع)
# ═══════════════════════════════════════════════════════════════
def try_cffi_segments(segments, out_path, iframe_url, cookies_dict):
    """
    ⚡ تحميل segments عبر cffi (بصمة TLS صحيحة + workers متوازية).
    """
    if not segments:
        return None

    ref_origin = _origin(iframe_url) or ""
    headers = {
        "Referer": iframe_url,
        "Origin": ref_origin,
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
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
        print(f"      ⚠️ cffi test فشل", flush=True)
        return None

    print(f"   ⚡ {len(segments)} segment عبر cffi...", flush=True)
    seg_dir = tempfile.mkdtemp(prefix="hls_cffi_")
    seg_paths, failed, total_bytes = {}, 0, 0

    def _dl(idx_url):
        idx, url = idx_url
        try:
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
        except Exception:
            pass
        return (idx, None, 0)

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
                print(f"      📦 {done}/{len(segments)} | {total_bytes/1048576:.1f}MB | فشل: {failed}", flush=True)

    if not seg_paths or failed > len(segments) * 0.15:
        shutil.rmtree(seg_dir, ignore_errors=True)
        print(f"      ❌ نسبة فشل عالية ({failed}/{len(segments)})", flush=True)
        return None

    sorted_segs = [seg_paths[k] for k in sorted(seg_paths.keys())]
    concat_file = os.path.join(seg_dir, "concat.txt")
    with open(concat_file, 'w') as f:
        for p in sorted_segs:
            f.write(f"file '{p}'\n")

    print(f"   🔗 دمج {len(sorted_segs)} segment...", flush=True)
    concat_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'warning',
                  '-f', 'concat', '-safe', '0', '-i', concat_file,
                  '-c', 'copy', '-f', 'mpegts', '-y', out_path]
    try:
        r = subprocess.run(concat_cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not os.path.exists(out_path):
            shutil.rmtree(seg_dir, ignore_errors=True)
            return None
    except Exception:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    final_size = os.path.getsize(out_path)
    print(f"   ✅ cffi نجح: {final_size/1048576:.1f}MB", flush=True)
    shutil.rmtree(seg_dir, ignore_errors=True)
    return (final_size, True)


# ═══════════════════════════════════════════════════════════════
# ★★★ من v18.2: تحميل segments داخل المتصفح (fallback)
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
    print(f"   🌐 المتصفح: {total} segment (batch={BROWSER_BATCH})...", flush=True)
    seg_dir = tempfile.mkdtemp(prefix="hls_br_")
    seg_paths, failed, total_bytes = {}, 0, 0

    for i in range(0, total, BROWSER_BATCH):
        batch = segments[i:i + BROWSER_BATCH]
        result = browser_fetch_batch_b64(sb, batch, timeout=BROWSER_BATCH_TIMEOUT)

        retry = []
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
                    retry.append((si, batch[li]))
            else:
                retry.append((si, batch[li]))

        if retry and SEGMENT_RETRY > 0:
            retry_urls = [u for _, u in retry]
            rr = browser_fetch_batch_b64(sb, retry_urls, timeout=BROWSER_BATCH_TIMEOUT)
            for idx_str, b64 in rr.items():
                try:
                    li = int(idx_str)
                except Exception:
                    continue
                si, _ = retry[li]
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
        elif retry:
            failed += len(retry)

        done = min(i + BROWSER_BATCH, total)
        if done % (BROWSER_BATCH * 2) == 0 or done == total:
            print(f"      📦 {done}/{total} | {total_bytes/1048576:.1f}MB | فشل: {failed}", flush=True)

    if not seg_paths:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    sorted_segs = [seg_paths[k] for k in sorted(seg_paths.keys())]
    concat_file = os.path.join(seg_dir, "concat.txt")
    with open(concat_file, 'w') as f:
        for p in sorted_segs:
            f.write(f"file '{p}'\n")

    print(f"   🔗 دمج {len(sorted_segs)} segment...", flush=True)
    concat_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'warning',
                  '-f', 'concat', '-safe', '0', '-i', concat_file,
                  '-c', 'copy', '-f', 'mpegts', '-y', out_path]
    try:
        r = subprocess.run(concat_cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not os.path.exists(out_path):
            shutil.rmtree(seg_dir, ignore_errors=True)
            return None
    except Exception:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    final_size = os.path.getsize(out_path)
    print(f"   ✅ المتصفح نجح: {final_size/1048576:.1f}MB", flush=True)
    shutil.rmtree(seg_dir, ignore_errors=True)
    return (final_size, True)


# ═══════════════════════════════════════════════════════════════
# ★★★ من v16.4: yt-dlp مع stall detection
# ═══════════════════════════════════════════════════════════════
def _try_ytdlp_with_headers(url, out_path, referer, cookies_dict):
    print(f"      [yt-dlp] {url[:80]}", flush=True)
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())[:8000]

    cmd = [
        sys.executable, '-m', 'yt_dlp',
        '--no-warnings', '--no-playlist', '--no-part',
        '--retries', '15', '--fragment-retries', '30',
        '--socket-timeout', '60',
        '--concurrent-fragments', '16',
        '--no-check-certificate', '--continue',
        '--hls-use-mpegts',
        '--hls-prefer-native',
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
        proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, text=True)
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
            # stall detection
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
        return size >= MIN_VALID_SIZE
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        log_file.close()
        return False


# ═══════════════════════════════════════════════════════════════
# ★★★ v20: تحميل أي m3u8 عبر cffi + deep resolution
# ═══════════════════════════════════════════════════════════════
def _download_with_curl_deep(m3u8_url, out_path, referer, cookies_dict):
    """من v16.4: يحل m3u8 بعمق ثم يحمل segments بـ cffi."""
    print(f"      [cffi deep] {m3u8_url[:80]}", flush=True)

    headers = {
        "Referer": referer,
        "Origin": _origin(referer),
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "ar,en;q=0.9",
    }
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())[:8000]
    if cookie_str:
        headers["Cookie"] = cookie_str

    # حل عميق
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
                print(f"         📦 {done}/{len(segments)} | {total_bytes/1048576:.1f}MB | فشل: {failed}", flush=True)

    if not seg_paths:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return False

    sorted_segs = [seg_paths[k] for k in sorted(seg_paths.keys())]
    concat_file = os.path.join(seg_dir, "concat.txt")
    with open(concat_file, 'w') as f:
        for p in sorted_segs:
            f.write(f"file '{p}'\n")

    concat_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'warning',
                  '-f', 'concat', '-safe', '0', '-i', concat_file,
                  '-c', 'copy', '-f', 'mpegts', '-y', out_path]
    try:
        r = subprocess.run(concat_cmd, capture_output=True, text=True, timeout=600)
        ok = r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        ok = False
    shutil.rmtree(seg_dir, ignore_errors=True)
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

    # 1) من Fetch capture
    with _NET_LOCK:
        for u in list(_RESP_BODIES.keys()):
            if ".m3u8" in u or ".mpd" in u:
                found.add(u)

    # 2) network log
    try:
        for u in netlog_read():
            if ".m3u8" in u or ".mpd" in u:
                found.add(u)
    except Exception:
        pass

    # 3) performance
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

    # 4) HTML
    try:
        html = sb.cdp.get_page_source() or ""
        for u in extract_m3u8_from_text(html):
            found.add(u)
    except Exception:
        pass

    # 5) JS players
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

    # 5 نقرات على مركز الفيديو (بناءً على ملاحظتك)
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

    # JS
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


def detect_player(sb):
    try:
        return sb.cdp.execute_script("""
            (function(){
                try {
                    var out = {player:'none'};
                    if (typeof jwplayer !== 'undefined') {
                        var p = jwplayer();
                        if (p) out.player = 'jw';
                    }
                    var v = document.querySelector('video');
                    if (v && out.player === 'none') out.player = 'h5';
                    if (typeof videojs !== 'undefined' && out.player === 'none') out.player = 'vjs';
                    return out;
                } catch(e) { return {player:'error'}; }
            })();
        """)
    except Exception:
        return {"player": "none"}


# ═══════════════════════════════════════════════════════════════
# ★★★ v20: العملية الكاملة — جلسة واحدة لكل حلقة
# ═══════════════════════════════════════════════════════════════
def _open_with_timeout(sb, url, timeout=OPEN_TIMEOUT):
    """فتح URL بمهلة."""
    result = [False]

    def _do():
        try:
            sb.cdp.open(url)
            result[0] = True
        except Exception:
            pass
        finally:
            try:
                sb.driver.execute_cdp_cmd("Page.stopLoading", {})
            except Exception:
                pass

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        try:
            sb.driver.execute_cdp_cmd("Page.stopLoading", {})
        except Exception:
            pass
        print(f"      ⏱️ تجاوز {timeout}s — متابعة", flush=True)
        return True
    return result[0]


def _bypass_cloudflare(sb, iframe_url, timeout_seconds=30):
    print(f"      🔄 CF bypass...", flush=True)
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


def process_episode_all_in_one(url, out_path):
    """
    ⚡ v20: جلسة متصفح واحدة لكل حلقة.
    يدعم: u.3seq.com + shhaiid4u.net + المواقع المباشرة.
    """
    from seleniumbase import SB

    if not _is_valid_episode_url(url):
        print(f"    ⏭️ رابط غير مدعوم", flush=True)
        return None

    netlog = tempfile.mktemp(suffix="_netlog.txt")
    open(netlog, "w").close()

    def _log(u):
        try:
            with open(netlog, "a", encoding="utf-8") as fh:
                fh.write(u + "\n")
                fh.flush()
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
                ad_block_on=False, disable_csp=True,
                page_load_strategy="eager", locale_code="en") as sb:
            try:
                sb.activate_cdp_mode()

                # ★ التقاط متعدد المستويات
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
                    print("      💉 interceptor", flush=True)
                except Exception:
                    pass

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
                                    with _NET_LOCK:
                                        _REQ_IDS[u] = rid
                        except Exception:
                            pass
                    sb.cdp.add_handler(mycdp.network.RequestWillBeSent, on_req)
                except Exception:
                    pass

                # ═══ الخطوة 1: افتح الصفحة ═══
                print(f"🖥️  فتح: {url[:90]}", flush=True)
                if not _open_with_timeout(sb, url, timeout=OPEN_TIMEOUT):
                    return None
                sb.cdp.sleep(2)

                cur = sb.cdp.get_current_url() or url
                if "?" in cur:
                    cur = cur.split("?")[0]
                if not cur.endswith("/"):
                    cur += "/"
                watch_url = cur + "?do=watch"

                # جرّب ?do=watch إذا كان u3seq
                if "u.3seq" in url or "/video/" in url:
                    if not _open_with_timeout(sb, watch_url, timeout=OPEN_TIMEOUT):
                        return None
                    sb.cdp.sleep(3)

                # ═══ الخطوة 2: جمع السيرفرات/iframes ═══
                servers = []
                for i in range(15):
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

                # إذا لا سيرفرات → جرّب الصفحة مباشرة (shhaiid4u.net)
                if not servers:
                    print(f"   🔄 لا سيرفرات — الصفحة مباشرة", flush=True)
                    iframe_url = url
                else:
                    # جمع iframes
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
                        print(f"   ⚠️ لا iframes — استخدام الصفحة مباشرة", flush=True)
                        iframe_url = url
                    else:
                        print(f"   ✅ {len(iframe_map)} iframe", flush=True)

                        def _prio(item):
                            k = item[0].lower()
                            for i, s in enumerate(SERVER_PRIORITY):
                                if s in k:
                                    return i
                            return 99

                        ordered = sorted(iframe_map.items(), key=_prio)

                        # ابدأ بأول iframe
                        sname, iframe_url = ordered[0]
                        print(f"   🎯 أول سيرفر: {sname}", flush=True)

                # ═══ الخطوة 3: انتقل إلى iframe (إن وجد) ═══
                if iframe_url and iframe_url != url:
                    if "luluvdo" in iframe_url.lower():
                        if not _bypass_cloudflare(sb, iframe_url, 30):
                            print(f"      ⏭️ CF فشل", flush=True)
                            return None
                        sb.cdp.sleep(2)
                    else:
                        try:
                            sb.driver.execute_cdp_cmd("Page.navigate", {
                                "url": iframe_url, "referrer": watch_url,
                            })
                        except Exception:
                            pass
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

                # ═══ الخطوة 4: انتظر المشغل ═══
                player_seen = False
                for tick in range(JWPLAYER_WAIT):
                    sb.cdp.sleep(1)
                    info = detect_player(sb)
                    if info.get("player") not in ("none", "error"):
                        print(f"   ✅ {info['player']} بعد {tick+1}s", flush=True)
                        player_seen = True
                        break

                if not player_seen:
                    print(f"   ⚠️ المشغل لم يظهر", flush=True)

                # ═══ الخطوة 5: تشغيل + التقاط m3u8 ═══
                print(f"   🎬 البحث عن m3u8 ({M3U8_SEARCH_TIMEOUT}s)...", flush=True)
                search_start = time.time()
                m3u8_urls = []

                while time.time() - search_start < M3U8_SEARCH_TIMEOUT:
                    trigger_play(sb)
                    sb.cdp.sleep(2)
                    found = scan_for_m3u8(sb, _read)
                    if found:
                        m3u8_urls = found
                        print(f"   ✨ {len(found)} m3u8 بعد {time.time()-search_start:.0f}s", flush=True)
                        break

                if not m3u8_urls:
                    m3u8_urls = scan_for_m3u8(sb, _read)
                if not m3u8_urls:
                    print(f"   ❌ لا m3u8", flush=True)
                    return None

                print(f"   🎯 {len(m3u8_urls)} مرشح:", flush=True)
                for u in m3u8_urls[:3]:
                    print(f"      · {u[:110]}", flush=True)

                # ═══ الخطوة 6: التحميل ═══
                cookies_full = get_cookies_full(sb)
                cookies_dict = cookies_to_dict(cookies_full)
                print(f"   🍪 {len(cookies_dict)} كوكي", flush=True)

                sb.cdp.sleep(2)

                # الاستراتيجية 1: cffi + deep resolution
                for m3u8_url in m3u8_urls[:M3U8_CANDIDATE_LIMIT]:
                    print(f"\n   🎯 [1/3 cffi-deep] {m3u8_url[:80]}", flush=True)
                    if _download_with_curl_deep(m3u8_url, str(out_path),
                                                 iframe_url or url, cookies_dict):
                        if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_VALID_SIZE:
                            return (os.path.getsize(out_path), True)
                    if os.path.exists(out_path):
                        try:
                            os.remove(out_path)
                        except Exception:
                            pass

                # الاستراتيجية 2: cffi segments (بدون deep)
                for m3u8_url in m3u8_urls[:3]:
                    print(f"\n   🎯 [2/3 cffi-segments] {m3u8_url[:80]}", flush=True)
                    headers = {
                        "Referer": iframe_url or url,
                        "Origin": _origin(iframe_url or url),
                        "User-Agent": UA,
                        "Accept": "*/*",
                    }
                    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())[:8000]
                    if cookie_str:
                        headers["Cookie"] = cookie_str

                    try:
                        r = cffi_requests.get(m3u8_url, headers=headers,
                                              impersonate="chrome120",
                                              timeout=30, verify=False)
                        if r.status_code == 200 and r.text:
                            base = m3u8_url.rsplit('/', 1)[0]
                            segments, variants = _parse_m3u8_robust(r.text, base)
                            if not segments and variants:
                                v = variants[0]
                                r2 = cffi_requests.get(v, headers=headers,
                                                       impersonate="chrome120",
                                                       timeout=30, verify=False)
                                if r2.status_code == 200:
                                    segments, _ = _parse_m3u8_robust(r2.text, v.rsplit('/', 1)[0])
                            if segments:
                                result = try_cffi_segments(segments, str(out_path),
                                                             iframe_url or url, cookies_dict)
                                if result:
                                    return result
                    except Exception:
                        pass
                    if os.path.exists(out_path):
                        try:
                            os.remove(out_path)
                        except Exception:
                            pass

                # الاستراتيجية 3: yt-dlp
                for m3u8_url in m3u8_urls[:3]:
                    print(f"\n   🎯 [3/3 yt-dlp] {m3u8_url[:80]}", flush=True)
                    if _try_ytdlp_with_headers(m3u8_url, str(out_path),
                                                iframe_url or url, cookies_dict):
                        if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_VALID_SIZE:
                            return (os.path.getsize(out_path), True)
                    if os.path.exists(out_path):
                        try:
                            os.remove(out_path)
                        except Exception:
                            pass

                print(f"   ⏭️ فشل جميع الاستراتيجيات", flush=True)

            except Exception as e:
                import traceback
                print(f"   ❌ {str(e)[:150]}", flush=True)
                traceback.print_exc()
    except Exception as e:
        import traceback
        print(f"   ❌ {str(e)[:150]}", flush=True)
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
def get_duration(path):
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


def compress_video(inp, out):
    if not os.path.exists(inp):
        return False
    im = os.path.getsize(inp) / 1048576
    print(f"   🗜️  {im:.2f}MB → {COMPRESS_SCALE}p...", flush=True)

    ff = _get_ffmpeg_exe()
    cmd = [
        ff, '-err_detect', 'ignore_err',
        '-fflags', '+discardcorrupt+genpts',
        '-analyzeduration', '50M', '-probesize', '50M',
        '-i', str(inp),
        '-vf', f'scale=-2:{COMPRESS_SCALE}',
        '-c:v', 'libx264',
        '-crf', str(COMPRESS_CRF),
        '-preset', COMPRESS_PRESET,
        '-threads', str(COMPRESS_THREADS),
        '-c:a', 'aac', '-b:a', '64k',
        '-f', 'mp4', '-movflags', '+faststart',
        '-max_muxing_queue_size', '4096',
        '-y', str(out),
    ]
    try:
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 10 * 1024:
            return False
        om = os.path.getsize(out) / 1048576
        print(f"   ✅ {im:.2f}→{om:.2f}MB في {time.time()-t0:.1f}s", flush=True)
        return True
    except Exception as e:
        print(f"   ❌ {e}", flush=True)
        return False


def make_thumbnail(video_path, out_path):
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
    """
    الواجهة العامة — تحميل حلقة واحدة.
    """
    safe_name = _safe(item_name or series_name)
    file_prefix = f"movie_{episode_num:02d}" if media_type == "movie" else f"ep{episode_num:03d}"

    out_dir = MEDIA_DIR / safe_name
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / f"{file_prefix}_raw.ts"
    final = out_dir / f"{file_prefix}.mp4"

    if final.exists() and final.stat().st_size > MIN_VALID_SIZE:
        print(f"    ↳ موجودة: {final.name}", flush=True)
        return final

    if not _is_valid_episode_url(url):
        print(f"    ⏭️ تخطي (رابط غير مدعوم): {url[:80]}", flush=True)
        return None

    print(f"    ↳ تحميل {file_prefix}...", flush=True)
    try:
        res = process_episode_all_in_one(url, raw)
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

    # ضغط
    if config.SKIP_COMPRESS:
        shutil.move(str(raw), str(final))
    else:
        if not compress_video(raw, final):
            print("    ⚠️ فشل الضغط — استخدام الأصلي", flush=True)
            shutil.move(str(raw), str(final))

    if raw.exists():
        raw.unlink()
    if not final.exists():
        return None

    # thumbnail
    try:
        thumb_path = out_dir / f"{file_prefix}.jpg"
        make_thumbnail(final, thumb_path)
    except Exception:
        pass

    print(f"    ✅ {final.name} ({final.stat().st_size/1048576:.1f}MB)", flush=True)
    return final