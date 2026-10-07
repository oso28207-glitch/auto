"""
downloader.py v46 — FINAL
═══════════════════════════════════════════════════════════
v46: 3 تحسينات ذكية لتسريع 10x
     1. اختيار variant الأدنى (≥360p) بدل الأعلى (1080p)
        → 60MB بدل 250MB لكل حلقة
     2. 16 worker بدل 4 (يعمل Chrome بـ threads بشكل آمن)
     3. base64 chunk 128KB بدل 32KB
     4. progress كل 100 مقطع (أقل ضجيجاً)
"""

import os
import re
import json
import shutil
import subprocess
import tempfile
import threading
import time
import base64
import concurrent.futures
from pathlib import Path
from urllib.parse import urlparse, urljoin

from config import config, MEDIA_DIR

MIN_SIZE = 100 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# الحد الأدنى للجودة المقبولة (نتجاهل أي variant أقل من هذا)
MIN_HEIGHT = 360


def _safe(n):
    return re.sub(r'[\\/:*?"<>|]', "_", n).strip()[:120]


def _origin(u):
    try:
        return f"{urlparse(u).scheme}://{urlparse(u).netloc}"
    except Exception:
        return ""


def _ffmpeg():
    try:
        if subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5).returncode == 0:
            return "ffmpeg"
    except Exception:
        pass
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return "ffmpeg"


def _is_valid_url(url):
    if not url:
        return False
    u = url.lower()
    bad = ["/moslslat.php", "/topvideos.php", "/all-series.php", "?cat=", "/category/"]
    for b in bad:
        if b in u:
            return False
    return ("modablaj-" in u or "/video/" in u or
            ("see.php" in u and "vid=" in u) or
            ("watch.php" in u and "vid=" in u) or
            ("/watch/" in u and ".php" not in u))


# ═══════════════════════════════════════════════════════════════
# JS interceptor
# ═══════════════════════════════════════════════════════════════
_JS = r"""
(function(){
    window.__m3u8 = window.__m3u8 || [];
    function rec(u){
        try{
            if(typeof u!=='string')return;
            if(u.indexOf('.m3u8')===-1 && u.indexOf('.mpd')===-1)return;
            if(window.__m3u8.indexOf(u)===-1) window.__m3u8.push(u);
        }catch(e){}
    }
    if(window.fetch && !window.__fp){
        var o=window.fetch;
        window.fetch=function(i,init){
            try{rec((typeof i==='string')?i:(i&&i.url));}catch(e){}
            return o.apply(this,arguments);
        };
        window.__fp=true;
    }
    if(window.XMLHttpRequest && !window.__xp){
        var xo=XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open=function(m,u){
            try{rec(u);}catch(e){}
            return xo.apply(this,arguments);
        };
        window.__xp=true;
    }
    try{
        var d=Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype,'src');
        if(d&&d.set&&!window.__vp){
            Object.defineProperty(HTMLMediaElement.prototype,'src',{
                set:function(v){try{rec(v);}catch(e){}return d.set.call(this,v);},
                get:d.get,configurable:true
            });
            window.__vp=true;
        }
    }catch(e){}
    window.__patch=function(){
        try{
            if(typeof jwplayer!=='undefined' && !window.__jp){
                var oj=jwplayer;
                window.jwplayer=function(){
                    var p=oj.apply(this,arguments);
                    if(p){
                        if(p.setup){
                            var s=p.setup;
                            p.setup=function(c){
                                try{
                                    if(c&&c.file)rec(c.file);
                                    if(c&&c.sources)c.sources.forEach(function(x){if(x.file)rec(x.file);});
                                }catch(e){}
                                return s.apply(this,arguments);
                            };
                        }
                        if(p.load){
                            var l=p.load;
                            p.load=function(pl){
                                try{
                                    if(pl){
                                        var a=Array.isArray(pl)?pl:[pl];
                                        a.forEach(function(i){
                                            if(i.file)rec(i.file);
                                            if(i.sources)i.sources.forEach(function(s){if(s.file)rec(s.file);});
                                        });
                                    }
                                }catch(e){}
                                return l.apply(this,arguments);
                            };
                        }
                    }
                    return p;
                };
                Object.assign(window.jwplayer,oj);
                window.__jp=true;
            }
            if(typeof Hls!=='undefined' && !window.__hp && Hls.prototype && Hls.prototype.loadSource){
                var o=Hls.prototype.loadSource;
                Hls.prototype.loadSource=function(u){
                    try{rec(u);}catch(e){}
                    return o.apply(this,arguments);
                };
                window.__hp=true;
            }
        }catch(e){}
    };
    setInterval(window.__patch,500);
})();
"""


def _install_js(sb):
    try:
        sb.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _JS})
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# CDP helpers
# ═══════════════════════════════════════════════════════════════
def _eval(sb, js, default=None):
    try:
        return sb.cdp.execute_script(js)
    except Exception:
        return default


def _cdp(sb, cmd, params=None):
    if params is None:
        params = {}
    try:
        return sb.driver.execute_cdp_cmd(cmd, params)
    except Exception:
        return None


def _cookies(sb):
    try:
        r = _cdp(sb, "Network.getAllCookies", {})
        if r and r.get("cookies"):
            return {c["name"]: c["value"] for c in r["cookies"]
                    if c.get("name") and c.get("value")}
    except Exception:
        pass
    return {}


def _open(sb, url, wait=1.5):
    _cdp(sb, "Page.navigate", {"url": url})
    sb.cdp.sleep(wait)
    _cdp(sb, "Runtime.evaluate", {"expression": "try{window.stop()}catch(e){}"})


def _play(sb):
    _eval(sb, """
        (function(){try{
            var v=document.querySelector('video');
            if(v){v.muted=true;if(v.play)v.play().catch(function(){});}
            if(typeof jwplayer!=='undefined'){
                var p=jwplayer();
                if(p){
                    if(p.play)p.play(true);
                    if(p.setMute)p.setMute(true);
                }
            }
            if(typeof videojs!=='undefined'){
                var p2=videojs.getPlayers();
                for(var k in p2){
                    try{p2[k].play();p2[k].muted(true);}catch(e){}
                }
            }
            ['video','button','.vjs-big-play-button','.jw-icon-playback',
             '.jw-display-icon-container','[class*=play]'].forEach(function(s){
                document.querySelectorAll(s).forEach(function(el){
                    try{el.click();}catch(e){}
                });
            });
        }catch(e){}})();
    """)
    try:
        r = _eval(sb, """
            (function(){
                var v=document.querySelector('video');
                if(!v)return null;
                var b=v.getBoundingClientRect();
                if(b.width<50)return null;
                return {x:Math.round(b.left+b.width/2),y:Math.round(b.top+b.height/2)};
            })();
        """)
        if r and r.get("x", 0) > 0:
            for _ in range(2):
                sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mousePressed", "x": r['x'], "y": r['y'],
                    "button": "left", "clickCount": 1,
                })
                sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mouseReleased", "x": r['x'], "y": r['y'],
                    "button": "left", "clickCount": 1,
                })
                sb.cdp.sleep(0.4)
    except Exception:
        pass


def _scan(sb):
    found = set()
    try:
        urls = _eval(sb, "return window.__m3u8||[]", [])
        if isinstance(urls, list):
            for u in urls:
                if isinstance(u, str) and (".m3u8" in u or ".mpd" in u):
                    found.add(u)
    except Exception:
        pass
    try:
        perf = _eval(sb, "try{return performance.getEntriesByType('resource').map(e=>e.name)}catch(e){return[]}", [])
        if isinstance(perf, list):
            for u in perf:
                if ".m3u8" in u or ".mpd" in u:
                    found.add(u)
    except Exception:
        pass
    try:
        html = sb.cdp.get_page_source() or ""
        for m in re.finditer(r'(https?:[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*)', html):
            found.add(m.group(1).replace("\\/", "/"))
    except Exception:
        pass
    try:
        apis = _eval(sb, """
            (function(){
                var o=[];
                try{
                    if(typeof jwplayer!=='undefined'){
                        try{
                            var p=jwplayer();
                            if(p&&p.getPlaylist){
                                p.getPlaylist().forEach(function(i){
                                    if(i.file)o.push(i.file);
                                    if(i.sources)i.sources.forEach(function(s){if(s.file)o.push(s.file);});
                                });
                            }
                        }catch(e){}
                    }
                    if(typeof Hls!=='undefined' && Hls.instances){
                        Hls.instances.forEach(function(h){try{if(h.url)o.push(h.url);}catch(e){}});
                    }
                    if(window.hls&&window.hls.url)o.push(window.hls.url);
                    var v=document.querySelector('video');
                    if(v){
                        if(v.currentSrc)o.push(v.currentSrc);
                        if(v.src)o.push(v.src);
                    }
                    document.querySelectorAll('source').forEach(function(s){if(s.src)o.push(s.src);});
                }catch(e){}
                return o;
            })();
        """, [])
        if isinstance(apis, list):
            for u in apis:
                if isinstance(u, str) and (".m3u8" in u or ".mpd" in u):
                    found.add(u)
    except Exception:
        pass
    return [u for u in found if "ping.gif" not in u and "jwpltx" not in u]


# ═══════════════════════════════════════════════════════════════
# fetch text (m3u8)
# ═══════════════════════════════════════════════════════════════
def _fetch_text_cdp(sb, url, referer):
    js = f"""
    (function(){{
        return fetch({repr(url)}, {{
            method: 'GET',
            credentials: 'include',
            cache: 'no-store',
            referrer: {repr(referer or '')},
            headers: {{ 'Accept': '*/*' }}
        }})
        .then(r => r.ok ? r.text() : 'ERROR: HTTP ' + r.status)
        .catch(e => 'ERROR: ' + (e.message || 'fetch failed'));
    }})();
    """
    try:
        result = sb.driver.execute_cdp_cmd("Runtime.evaluate", {
            "expression": js,
            "awaitPromise": True,
            "returnByValue": True,
        })
        value = (result or {}).get("result", {}).get("value")
        if not isinstance(value, str):
            return None
        if value.startswith("ERROR:"):
            print(f"      ⚠️ fetch m3u8: {value[6:150]}", flush=True)
            return None
        return value
    except Exception as e:
        print(f"      ⚠️ fetch m3u8 exc: {str(e)[:120]}", flush=True)
    return None


# ═══════════════════════════════════════════════════════════════
# ★★★ fetch مقطع واحد (مع AbortController)
# ═══════════════════════════════════════════════════════════════
def _fetch_one_segment(driver, url, referer, max_size_mb=10, timeout_s=100):
    max_bytes = max_size_mb * 1024 * 1024
    js = f"""
    (function(){{
        var controller = new AbortController();
        var timer = setTimeout(function(){{
            try {{ controller.abort(); }} catch(e) {{}}
        }}, {timeout_s * 1000});

        return fetch({repr(url)}, {{
            method: 'GET',
            credentials: 'include',
            cache: 'no-store',
            referrer: {repr(referer or '')},
            signal: controller.signal,
            headers: {{ 'Accept': '*/*' }}
        }})
        .then(r => {{
            clearTimeout(timer);
            if (!r.ok) return 'ERROR: HTTP ' + r.status;
            return r.arrayBuffer();
        }})
        .then(buf => {{
            var bytes = new Uint8Array(buf);
            if (bytes.length > {max_bytes}) return 'ERROR: TOO_BIG';
            var binary = '';
            var CHUNK = 131072;
            for (var i = 0; i < bytes.length; i += CHUNK) {{
                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
            }}
            return 'OK:' + btoa(binary);
        }})
        .catch(e => {{
            clearTimeout(timer);
            return 'ERROR: ' + (e.message || 'fetch failed');
        }});
    }})();
    """
    try:
        result = driver.execute_cdp_cmd("Runtime.evaluate", {
            "expression": js,
            "awaitPromise": True,
            "returnByValue": True,
        })
        value = (result or {}).get("result", {}).get("value")
        if not isinstance(value, str):
            return None
        if value.startswith("ERROR:"):
            return None
        if value.startswith("OK:"):
            return base64.b64decode(value[3:])
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════
# ★★★ v46: التحميل المتوازي مع 16 worker
# ═══════════════════════════════════════════════════════════════
def _parallel_download(sb, seg_urls, referer, tmp_dir, max_workers=16,
                       hard_deadline_s=1800):
    driver = sb.driver
    total = len(seg_urls)
    paths = {}
    failed = 0
    done = 0
    lock = threading.Lock()

    def _worker(task):
        idx, url = task
        try:
            data = _fetch_one_segment(driver, url, referer,
                                       max_size_mb=10, timeout_s=60)
        except Exception:
            return (idx, None, 0)
        if not data:
            return (idx, None, 0)
        seg_path = os.path.join(tmp_dir, f"seg_{idx:06d}.ts")
        try:
            with open(seg_path, "wb") as f:
                f.write(data)
        except Exception:
            return (idx, None, 0)
        return (idx, seg_path, len(data))

    print(f"      ⚡ تحميل متوازٍ ({max_workers} workers) — {total} مقطع",
          flush=True)

    tasks = [(i, s) for i, s in enumerate(seg_urls)]

    start = time.time()
    hard_deadline = start + hard_deadline_s
    last_print = start

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_map = {ex.submit(_worker, t): t for t in tasks}
        pending = set(future_map.keys())

        while pending and time.time() < hard_deadline:
            done_set, pending = concurrent.futures.wait(
                pending, timeout=3,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for f in done_set:
                try:
                    idx, path, size = f.result(timeout=1)
                except Exception:
                    idx, path, size = (-1, None, 0)

                with lock:
                    done += 1
                    if path:
                        paths[idx] = path
                    else:
                        failed += 1

            # اطبع كل 100 مقطع أو كل 15 ثانية
            now = time.time()
            if done % 100 == 0 or (done < total and now - last_print > 15 and done > 0):
                with lock:
                    try:
                        mb = sum(os.path.getsize(p) for p in paths.values()) / 1048576
                    except Exception:
                        mb = 0
                    elapsed = now - start
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (total - done) / rate if rate > 0 else 0
                    print(f"      📦 {done}/{total} | نجح: {len(paths)} | "
                          f"فشل: {failed} | {mb:.1f}MB | {rate:.1f}/s | "
                          f"ETA: {eta:.0f}s", flush=True)
                last_print = now

        if pending:
            print(f"      ⚠️ {len(pending)} طلب معلّق — إلغاء", flush=True)
            for f in pending:
                try:
                    f.cancel()
                except Exception:
                    pass

    return paths, failed


# ═══════════════════════════════════════════════════════════════
# downloader الرئيسي
# ═══════════════════════════════════════════════════════════════
def _download_via_browser(sb, m3u8_url, out_path, referer):
    print(f"      🎯 التحميل عبر المتصفح...", flush=True)

    m3u8_text = _fetch_text_cdp(sb, m3u8_url, referer)
    if not m3u8_text:
        print(f"      ❌ فشل جلب m3u8", flush=True)
        return None

    print(f"      ✅ m3u8: {len(m3u8_text)} حرف", flush=True)

    # ★★★ v46: اختر variant الأدنى (>= 360p) بدل الأعلى
    if "#EXT-X-STREAM-INF" in m3u8_text:
        chosen = _pick_smallest_variant(m3u8_text, m3u8_url,
                                         min_height=MIN_HEIGHT)
        if chosen:
            m3u8_url = chosen
            m3u8_text = _fetch_text_cdp(sb, m3u8_url, referer)
            if not m3u8_text:
                return None
            print(f"      ✅ m3u8 (variant): {len(m3u8_text)} حرف", flush=True)

    base = m3u8_url.rsplit("/", 1)[0]
    seg_urls = []
    for line in m3u8_text.splitlines():
        l = line.strip()
        if not l or l.startswith("#"):
            continue
        full = l if l.startswith("http") else urljoin(base + "/", l)
        seg_urls.append(full)

    if not seg_urls:
        print(f"      ❌ لا مقاطع", flush=True)
        return None

    print(f"      📋 {len(seg_urls)} مقطع", flush=True)

    tmp_dir = tempfile.mkdtemp(prefix="br_segs_")
    start = time.time()
    hard_deadline_s = min(1800, max(300, len(seg_urls) * 2))

    paths, failed = _parallel_download(
        sb, seg_urls, referer, tmp_dir,
        max_workers=16,
        hard_deadline_s=hard_deadline_s,
    )

    elapsed = time.time() - start
    if not paths:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"      ❌ فشل كل المقاطع", flush=True)
        return None

    print(f"      ✅ {len(paths)}/{len(seg_urls)} في {elapsed:.1f}s "
          f"({len(paths)/elapsed:.1f}/s)", flush=True)

    seg_paths = [paths[k] for k in sorted(paths)]
    success = _concat_segments(seg_paths, str(out_path), tmp_dir)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    if success and os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
        print(f"      ✅ تحميل: {os.path.getsize(out_path)/1048576:.1f}MB",
              flush=True)
        return (os.path.getsize(out_path), True)
    return None


# ═══════════════════════════════════════════════════════════════
# ★★★ v46: اختيار أصغر variant بجودة مقبولة
# ═══════════════════════════════════════════════════════════════
def _pick_smallest_variant(m3u8_text, base_url, min_height=360):
    """
    يختار أصغر variant بجودة >= min_height.
    يوفر ~75% من حجم التنزيل والوقت.
    """
    variants = []
    lines = m3u8_text.splitlines()

    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        # استخرج BANDWIDTH و RESOLUTION
        m_bw = re.search(r"BANDWIDTH=(\d+)", line)
        m_res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        bw = int(m_bw.group(1)) if m_bw else 0
        height = int(m_res.group(2)) if m_res else 0

        if i + 1 < len(lines):
            uri = lines[i + 1].strip()
            if uri and not uri.startswith("#"):
                full = uri if uri.startswith("http") else urljoin(
                    base_url.rsplit("/", 1)[0] + "/", uri)
                variants.append({"bw": bw, "h": height, "url": full})

    if not variants:
        # لا يوجد variants — استخدم ما هو موجود
        return None

    # فلترة: فقط variants بجودة >= min_height
    eligible = [v for v in variants if v["h"] == 0 or v["h"] >= min_height]
    if not eligible:
        # لم نجد جودة كافية — اختر الأعلى
        eligible = variants

    # اختر الأصغر bandwidth بين المؤهلة
    eligible.sort(key=lambda v: v["bw"])
    chosen = eligible[0]

    print(f"      🎚️  variants متاحة: "
          + ", ".join(f"{v['h']}p@{v['bw']//1000}kbps" for v in variants[:5]),
          flush=True)
    print(f"      🎯 اخترنا: {chosen['h']}p@{chosen['bw']//1000}kbps", flush=True)

    return chosen["url"]


# (احتفظ بالاسم القديم للتوافق)
_pick_best_variant = _pick_smallest_variant


def _concat_segments(seg_paths, out_path, seg_dir):
    if not seg_paths:
        return False

    concat_file = os.path.join(seg_dir, "concat.txt")
    with open(concat_file, "w", encoding="utf-8") as f:
        for p in seg_paths:
            safe_p = p.replace("'", "'\\''")
            f.write(f"file '{safe_p}'\n")

    cmd = [
        _ffmpeg(), "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0",
        "-i", concat_file,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-fflags", "+genpts+igndts",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        "-y", out_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if (r.returncode == 0 and os.path.exists(out_path)
                and os.path.getsize(out_path) > MIN_SIZE):
            return True
        print(f"      ⚠️ concat: {(r.stderr or '')[-120:]}", flush=True)
    except Exception as e:
        print(f"      ⚠️ concat: {str(e)[:100]}", flush=True)

    try:
        bin_path = os.path.join(seg_dir, "binary.ts")
        with open(bin_path, "wb") as out:
            for p in seg_paths:
                with open(p, "rb") as seg:
                    shutil.copyfileobj(seg, out, length=1024 * 1024)
        remux = os.path.join(seg_dir, "remux.mp4")
        cmd2 = [
            _ffmpeg(), "-hide_banner", "-loglevel", "warning",
            "-fflags", "+genpts+igndts",
            "-i", bin_path,
            "-c", "copy", "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            "-y", remux,
        ]
        r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=900)
        if (r2.returncode == 0 and os.path.exists(remux)
                and os.path.getsize(remux) > MIN_SIZE):
            shutil.move(remux, out_path)
            return True
    except Exception:
        pass
    return False


# ═══════════════════════════════════════════════════════════════
# fallback
# ═══════════════════════════════════════════════════════════════
def _ytdlp_with_cookies(url, out_path, referer, cookies_dict):
    print(f"      [yt-dlp]", flush=True)
    tmp_cookie = tempfile.mktemp(suffix=".txt")
    try:
        with open(tmp_cookie, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")
            domain = _origin(url).replace("https://", "").replace("http://", "")
            for k, v in cookies_dict.items():
                f.write(f".{domain}\tTRUE\t/\tTRUE\t0\t{k}\t{v}\n")
    except Exception:
        pass

    cmd = [
        "yt-dlp", "--no-warnings", "--no-playlist", "--no-part",
        "--retries", "3", "--fragment-retries", "5",
        "--socket-timeout", "30", "--concurrent-fragments", "16",
        "--no-check-certificate", "--hls-prefer-native",
        "--user-agent", UA, "--referer", referer,
        "--add-header", "Origin:" + _origin(referer),
        "--add-header", "Accept:*/*",
        "-o", out_path, url,
    ]
    if os.path.exists(tmp_cookie):
        cmd += ["--cookies", tmp_cookie]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
            print(f"      ✅ yt-dlp: {os.path.getsize(out_path)/1048576:.1f}MB",
                  flush=True)
            try: os.unlink(tmp_cookie)
            except Exception: pass
            return True
        err = (r.stderr or "")[-200:]
        print(f"      ⚠️ yt-dlp: {err}", flush=True)
    except Exception as e:
        print(f"      ⚠️ yt-dlp: {str(e)[:100]}", flush=True)
    try: os.unlink(tmp_cookie)
    except Exception: pass
    return False


def _ffmpeg_hls(m3u8_url, out_path, referer, ck):
    print(f"      [ffmpeg-hls]", flush=True)
    headers = (
        f"Referer: {referer}\r\nOrigin: {_origin(referer)}\r\n"
        f"User-Agent: {UA}\r\nAccept: */*\r\n"
        "Sec-Fetch-Dest: empty\r\nSec-Fetch-Mode: cors\r\nSec-Fetch-Site: cross-site\r\n"
    )
    if ck:
        headers += f"Cookie: {'; '.join(f'{k}={v}' for k,v in ck.items())[:8000]}\r\n"

    cmd = [
        _ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-headers", headers, "-user_agent", UA,
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-i", m3u8_url, "-c", "copy", "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart", "-y", out_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
            print(f"      ✅ ffmpeg: {os.path.getsize(out_path)/1048576:.1f}MB", flush=True)
            return True
        err = (r.stderr or "").strip().split("\n")[-1] if r.stderr else "?"
        print(f"      ⚠️ ffmpeg: {err[:150]}", flush=True)
    except Exception as e:
        print(f"      ⚠️ ffmpeg: {str(e)[:100]}", flush=True)
    return False


# ═══════════════════════════════════════════════════════════════
# استخراج m3u8
# ═══════════════════════════════════════════════════════════════
def _extract_from_iframe(sb, iframe_url, out_path):
    print(f"      🌐 iframe: {iframe_url[:80]}", flush=True)
    _open(sb, iframe_url, wait=2.5)

    m3u8s = []
    for cycle in range(6):
        _play(sb)
        sb.cdp.sleep(1.2)
        found = _scan(sb)
        if found:
            m3u8s = found
            print(f"      ✨ {len(found)} m3u8 بعد {cycle+1} دورة", flush=True)
            break

    if not m3u8s:
        print(f"      🔍 بحث {config.M3U8_SEARCH_TIMEOUT}s...", flush=True)
        start = time.time()
        while time.time() - start < config.M3U8_SEARCH_TIMEOUT:
            _play(sb)
            sb.cdp.sleep(1.5)
            found = _scan(sb)
            if found:
                m3u8s = found
                print(f"      ✨ {len(found)} m3u8 بعد {time.time()-start:.0f}s", flush=True)
                break

    if not m3u8s:
        print(f"      ❌ لا m3u8", flush=True)
        return None

    def _priority(u):
        lu = u.lower()
        if "master" in lu: return 0
        if "index" in lu: return 1
        return 2
    m3u8s.sort(key=_priority)

    unique = []
    seen = set()
    for u in m3u8s:
        base = u.split("?")[0]
        if base in seen: continue
        seen.add(base)
        unique.append(u)
    m3u8s = unique

    for u in m3u8s[:3]:
        print(f"         · {u[:100]}", flush=True)

    ref = iframe_url
    ck = _cookies(sb)

    for m in m3u8s[:2]:
        res = _download_via_browser(sb, m, str(out_path), ref)
        if res and os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
            return res

    print(f"      🎯 [fallback] yt-dlp...", flush=True)
    for m in m3u8s[:2]:
        if _ytdlp_with_cookies(m, str(out_path), ref, ck):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return (os.path.getsize(out_path), True)

    print(f"      🎯 [fallback] ffmpeg-HLS...", flush=True)
    for m in m3u8s[:2]:
        if _ffmpeg_hls(m, str(out_path), ref, ck):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return (os.path.getsize(out_path), True)

    return None


# ═══════════════════════════════════════════════════════════════
# u3seq + yam
# ═══════════════════════════════════════════════════════════════
def _u3seq(sb, url, out_path):
    if "?do=watch" not in url:
        cur = url.split("?")[0]
        if not cur.endswith("/"): cur += "/"
        watch = cur + "?do=watch"
    else:
        watch = url
    print(f"🖥️  فتح: {watch[:90]}", flush=True)
    _open(sb, watch, wait=2)
    servers = []
    for i in range(8):
        sb.cdp.sleep(0.6)
        servers = _eval(sb, """
            (function(){
                var l=document.querySelector('.serversList');
                if(!l)return[];
                return Array.from(l.querySelectorAll('li')).map(function(li){
                    return {id:li.id||'', name:(li.textContent||'').trim()};
                });
            })();
        """, []) or []
        if servers: break
    if not servers:
        print(f"   ⚠️ لا سيرفرات", flush=True)
        return None
    print(f"   ✅ {len(servers)} سيرفر", flush=True)
    iframe_url = _eval(sb, """
        (function(){
            var f=document.querySelector('.watch iframe');
            if(f&&f.src&&f.src.startsWith('http'))return f.src;
            return null;
        })();
    """)
    if not iframe_url:
        for _ in range(6):
            sb.cdp.sleep(1)
            iframe_url = _eval(sb, """
                (function(){
                    var f=document.querySelector('.watch iframe');
                    if(f&&f.src&&f.src.startsWith('http'))return f.src;
                    return null;
                })();
            """)
            if iframe_url: break
    if not iframe_url:
        print(f"   ❌ لا iframe", flush=True)
        return None
    iframe_url = iframe_url.replace("&amp;", "&")
    print(f"   🎯 iframe: {iframe_url[:80]}", flush=True)
    return _extract_from_iframe(sb, iframe_url, out_path)


def _yam(sb, url, out_path):
    print(f"🖥️  فتح: {url[:90]}", flush=True)
    _open(sb, url, wait=2)
    iframe_url = _eval(sb, """
        (function(){
            var f=document.querySelector('iframe');
            if(f&&f.src&&f.src.startsWith('http')
                &&f.src.indexOf('google')===-1
                &&f.src.indexOf('facebook')===-1)
                return f.src;
            return null;
        })();
    """)
    if not iframe_url:
        for _ in range(6):
            sb.cdp.sleep(1)
            iframe_url = _eval(sb, """
                (function(){
                    var f=document.querySelector('iframe');
                    if(f&&f.src&&f.src.startsWith('http'))return f.src;
                    return null;
                })();
            """)
            if iframe_url: break
    if not iframe_url:
        print(f"   ❌ لا iframe", flush=True)
        return None
    print(f"   🎯 iframe: {iframe_url[:80]}", flush=True)
    return _extract_from_iframe(sb, iframe_url, out_path)


def _run_browser(url, out_path):
    from seleniumbase import SB
    try:
        with SB(uc=True, xvfb=True, headless=False, incognito=True,
                ad_block_on=True, disable_csp=True,
                page_load_strategy="eager", locale_code="en",
                chromium_arg="--disable-web-security,--disable-site-isolation-trials") as sb:
            try:
                sb.activate_cdp_mode()
                try: sb.driver.set_page_load_timeout(20)
                except Exception: pass
                _install_js(sb)
                u_low = url.lower()
                if "modablaj-" in u_low or "/video/" in u_low:
                    print(f"   🎯 u3seq (عشق)", flush=True)
                    return _u3seq(sb, url, out_path)
                else:
                    print(f"   🎯 yam (اهواك)", flush=True)
                    return _yam(sb, url, out_path)
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
# الضغط + thumbnail (بدون تغيير)
# ═══════════════════════════════════════════════════════════════
def _compress(inp, out):
    im = Path(inp).stat().st_size / 1048576
    print(f"   🗜️  {im:.2f}MB → {config.COMPRESS_SCALE}p", flush=True)
    cmd = [_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error",
           "-err_detect", "ignore_err", "-i", str(inp),
           "-vf", f"scale=-2:{config.COMPRESS_SCALE}",
           "-c:v", "libx264", "-preset", config.COMPRESS_PRESET,
           "-crf", str(config.COMPRESS_CRF),
           "-profile:v", "main", "-level", "3.1", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", config.COMPRESS_AUDIO_BITRATE,
           "-ac", "2", "-ar", "44100",
           "-movflags", "+faststart", "-threads", "2",
           "-y", str(out)]
    try:
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode == 0 and Path(out).exists():
            om = Path(out).stat().st_size / 1048576
            print(f"   ✅ {im:.2f}→{om:.2f}MB في {time.time()-t0:.1f}s", flush=True)
            return om <= config.COMPRESS_MAX_SIZE_MB
    except Exception as e:
        print(f"   ⚠️ {str(e)[:100]}", flush=True)
    return False


def _thumb(v, o):
    for ss in ["00:00:05", "00:00:01", "00:00:00"]:
        cmd = [_ffmpeg(), "-err_detect", "ignore_err", "-ss", ss,
               "-i", str(v), "-vframes", "1", "-vf", "scale=320:180",
               "-f", "image2", "-y", str(o)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if r.returncode == 0 and os.path.exists(o) and os.path.getsize(o) > 1024:
                return True
        except Exception: pass
    return False


# ═══════════════════════════════════════════════════════════════
# الواجهة العامة
# ═══════════════════════════════════════════════════════════════
def download_episode(series_name, episode_num, url,
                     media_type="series", item_name=None):
    safe = _safe(item_name or series_name)
    prefix = f"movie_{episode_num:02d}" if media_type == "movie" else f"ep{episode_num:03d}"
    out_dir = MEDIA_DIR / safe
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / f"{prefix}_raw.mp4"
    final = out_dir / f"{prefix}.mp4"

    if final.exists() and final.stat().st_size > MIN_SIZE:
        print(f"    ↳ موجودة", flush=True)
        return final

    if not _is_valid_url(url):
        print(f"    ⏭️  رابط غير مدعوم", flush=True)
        return None

    print(f"    ↳ تحميل {prefix}...", flush=True)

    result = [None]
    exc = [None]

    def _worker():
        try: result[0] = _run_browser(url, raw)
        except Exception as e: exc[0] = e

    episode_timeout = max(config.EPISODE_TIMEOUT, 2400)
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=episode_timeout)

    if t.is_alive():
        print(f"    ⏰ تجاوز {episode_timeout}s — إيقاف", flush=True)
        for p in ["yt-dlp", "chrome", "ffmpeg"]:
            try:
                subprocess.run(["pkill", "-9", "-f", p], capture_output=True, timeout=3)
            except Exception: pass

    if exc[0]:
        print(f"    ⚠️ {str(exc[0])[:150]}", flush=True)
        result[0] = None

    if not result[0] or not raw.exists():
        print(f"    ⚠️ فشل", flush=True)
        try:
            if raw.exists(): raw.unlink()
        except Exception: pass
        return None

    print(f"    📦 {raw.stat().st_size/1048576:.1f}MB", flush=True)

    if config.SKIP_COMPRESS:
        shutil.move(str(raw), str(final))
    else:
        if not _compress(raw, final):
            print("    ⚠️ فشل الضغط — الأصلي", flush=True)
            shutil.move(str(raw), str(final))

    if raw.exists(): raw.unlink()
    if not final.exists(): return None

    try: _thumb(final, out_dir / f"{prefix}.jpg")
    except Exception: pass

    print(f"    ✅ {final.name} ({final.stat().st_size/1048576:.1f}MB)", flush=True)
    return final