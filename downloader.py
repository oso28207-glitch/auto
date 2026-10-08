"""
downloader.py v57 — FINAL
═══════════════════════════════════════════════════════════
v57: دعم عدة سيرفرات في yam.ahwaktv.net
     - _yam يجرب جميع iframes بالترتيب
     - إذا فشل سيرفر، ينتقل للتالي
     - إذا فشل الكل، يرفع استثناء لإيقاف السكربت
     - الإعدادات: 144p / CRF=28 / veryfast / threads=2
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
from pathlib import Path
from urllib.parse import urlparse, urljoin

from config import config, MEDIA_DIR

MIN_SIZE = 100 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

MIN_HEIGHT = 360
BATCH_SIZE = 6
PER_SEG_MS = 20000
BATCH_MS = 60000


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
# fetch دفعة
# ═══════════════════════════════════════════════════════════════
def _fetch_batch_in_browser(sb, urls_batch, referer,
                             per_seg_ms=PER_SEG_MS, batch_ms=BATCH_MS):
    urls_json = json.dumps(urls_batch)

    js = f"""
    (async function(){{
        const urls = {urls_json};
        const referer = {repr(referer or '')};
        const PER_SEG_MS = {per_seg_ms};
        const BATCH_MS = {batch_ms};

        const controllers = [];

        const batchPromise = Promise.all(urls.map(async (u, i) => {{
            const controller = new AbortController();
            controllers.push(controller);
            const t = setTimeout(() => {{
                try {{ controller.abort(); }} catch(e) {{}}
            }}, PER_SEG_MS);

            try {{
                const r = await fetch(u, {{
                    method: 'GET',
                    credentials: 'include',
                    cache: 'no-store',
                    referrer: referer,
                    signal: controller.signal,
                    headers: {{ 'Accept': '*/*' }}
                }});
                if (!r.ok) {{
                    clearTimeout(t);
                    return {{ ok: false, error: 'HTTP ' + r.status }};
                }}
                const buf = await r.arrayBuffer();
                clearTimeout(t);
                const bytes = new Uint8Array(buf);
                if (bytes.length === 0) {{
                    return {{ ok: false, error: 'EMPTY' }};
                }}
                let binary = '';
                const CHUNK = 8192;
                for (let j = 0; j < bytes.length; j += CHUNK) {{
                    binary += String.fromCharCode.apply(
                        null, bytes.subarray(j, j + CHUNK));
                }}
                try {{
                    return {{ ok: true, data: btoa(binary) }};
                }} catch(e) {{
                    return {{ ok: false, error: 'btoa:' + e.message }};
                }}
            }} catch(e) {{
                clearTimeout(t);
                const msg = (e.name || 'err') + ':' +
                             (e.message || '').substring(0, 30);
                return {{ ok: false, error: msg }};
            }}
        }}));

        const hardTimeout = new Promise((resolve) => {{
            setTimeout(() => {{
                for (const c of controllers) {{
                    try {{ c.abort(); }} catch(e) {{}}
                }}
                resolve('__HARD__');
            }}, BATCH_MS);
        }});

        const result = await Promise.race([batchPromise, hardTimeout]);
        if (result === '__HARD__') {{
            return JSON.stringify(
                urls.map(() => ({{ ok: false, error: 'batch_timeout' }}))
            );
        }}
        return JSON.stringify(result);
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

        parsed = json.loads(value)
        decoded = []
        for item in parsed:
            if item.get("ok"):
                try:
                    decoded.append(base64.b64decode(item["data"]))
                except Exception:
                    decoded.append(None)
            else:
                decoded.append(None)
        return decoded
    except Exception as e:
        print(f"      ⚠️ batch exc: {str(e)[:100]}", flush=True)
        return None


# ═══════════════════════════════════════════════════════════════
# تحميل بدفعات
# ═══════════════════════════════════════════════════════════════
def _download_all_segments(sb, seg_urls, referer, tmp_dir,
                            batch_size=BATCH_SIZE):
    total = len(seg_urls)
    print(f"      ⚡ تحميل بدفعات ({batch_size} مقطع/دفعة) — {total} مقطع",
          flush=True)

    paths = {}
    failed_indices = []
    start = time.time()
    empty_batches = 0

    for i in range(0, total, batch_size):
        batch = seg_urls[i:i + batch_size]
        batch_end = min(i + batch_size, total)
        results = _fetch_batch_in_browser(sb, batch, referer)

        if results is None:
            for j in range(len(batch)):
                failed_indices.append(i + j)
            empty_batches += 1
        else:
            batch_ok = 0
            for j, data in enumerate(results):
                idx = i + j
                if data:
                    seg_path = os.path.join(tmp_dir, f"seg_{idx:06d}.ts")
                    try:
                        with open(seg_path, "wb") as f:
                            f.write(data)
                        paths[idx] = seg_path
                        batch_ok += 1
                    except Exception:
                        failed_indices.append(idx)
                else:
                    failed_indices.append(idx)

            if batch_ok == 0:
                empty_batches += 1
            else:
                empty_batches = 0

        if empty_batches >= 2:
            print(f"      ❌ دفعتان فارغتان — إيقاف التحميل فوراً", flush=True)
            break

        done = batch_end
        elapsed = time.time() - start
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        try:
            mb = sum(os.path.getsize(p) for p in paths.values()) / 1048576
        except Exception:
            mb = 0
        print(f"      📦 {done}/{total} | نجح: {len(paths)} | "
              f"فشل: {len(failed_indices)} | {mb:.1f}MB | "
              f"{rate:.1f}/s | ETA: {eta:.0f}s", flush=True)

    return paths, failed_indices


# ═══════════════════════════════════════════════════════════════
# yt-dlp
# ═══════════════════════════════════════════════════════════════
def _ytdlp_simple(url, out_path, referer):
    print(f"      [yt-dlp simple]", flush=True)

    cmd = [
        "yt-dlp",
        "--no-warnings", "--no-playlist", "--no-part",
        "--retries", "5", "--fragment-retries", "10",
        "--socket-timeout", "30",
        "--concurrent-fragments", "16",
        "--no-check-certificate",
        "--hls-prefer-native",
        "--user-agent", UA,
        "--referer", referer,
        "--add-header", f"Origin:{_origin(referer)}",
        "--add-header", "Accept:*/*",
        "--add-header", "Accept-Language:en-US,en;q=0.9",
        "-o", out_path, url,
    ]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1500)
        if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
            print(f"      ✅ yt-dlp: {os.path.getsize(out_path)/1048576:.1f}MB",
                  flush=True)
            return True
        err = (r.stderr or "")[-250:]
        print(f"      ⚠️ yt-dlp: {err}", flush=True)
    except Exception as e:
        print(f"      ⚠️ yt-dlp: {str(e)[:100]}", flush=True)
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
# downloader الرئيسي
# ═══════════════════════════════════════════════════════════════
def _download_via_browser(sb, m3u8_url, out_path, referer):
    print(f"      🎯 التحميل عبر المتصفح...", flush=True)

    m3u8_text = _fetch_text_cdp(sb, m3u8_url, referer)
    if not m3u8_text:
        print(f"      ❌ فشل جلب m3u8", flush=True)
        return None

    print(f"      ✅ m3u8: {len(m3u8_text)} حرف", flush=True)

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

    # 1) yt-dlp
    print(f"      🎯 [1/3] yt-dlp simple...", flush=True)
    if _ytdlp_simple(m3u8_url, str(out_path), referer):
        if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
            return (os.path.getsize(out_path), True)

    # 2) ffmpeg-hls
    print(f"      🎯 [2/3] ffmpeg-hls...", flush=True)
    ck = _cookies(sb)
    if _ffmpeg_hls(m3u8_url, str(out_path), referer, ck):
        if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
            return (os.path.getsize(out_path), True)

    # 3) browser-fetch
    print(f"      🎯 [3/3] browser-fetch...", flush=True)
    tmp_dir = tempfile.mkdtemp(prefix="br_segs_")
    start = time.time()

    paths, failed = _download_all_segments(sb, seg_urls, referer, tmp_dir)
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


def _pick_smallest_variant(m3u8_text, base_url, min_height=360):
    variants = []
    lines = m3u8_text.splitlines()

    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
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
        return None

    if len(variants) == 1:
        v = variants[0]
        print(f"      ℹ️ variant واحد فقط ({v['h']}p@{v['bw']//1000}kbps) — "
              f"نستخدمه مباشرة", flush=True)
        return v["url"]

    eligible = [v for v in variants if v["h"] == 0 or v["h"] >= min_height]
    if not eligible:
        eligible = variants

    eligible.sort(key=lambda v: v["bw"])
    chosen = eligible[0]

    print(f"      🎚️  {len(variants)} variants: "
          + ", ".join(f"{v['h']}p@{v['bw']//1000}kbps" for v in variants[:6]),
          flush=True)
    print(f"      🎯 اخترنا: {chosen['h']}p@{chosen['bw']//1000}kbps", flush=True)

    return chosen["url"]


_pick_best_variant = _pick_smallest_variant


# ═══════════════════════════════════════════════════════════════
# binary concat
# ═══════════════════════════════════════════════════════════════
def _concat_segments(seg_paths, out_path, seg_dir):
    if not seg_paths:
        return False

    total = len(seg_paths)

    print(f"      🔗 دمج {total} مقطع (binary)...", flush=True)
    t0 = time.time()
    raw_ts = os.path.join(seg_dir, "combined.ts")

    try:
        with open(raw_ts, "wb") as out:
            for i, p in enumerate(seg_paths, 1):
                if not os.path.exists(p):
                    continue
                with open(p, "rb") as seg:
                    shutil.copyfileobj(seg, out, length=1024 * 1024)
                if i % 100 == 0:
                    print(f"         ↳ {i}/{total}", flush=True)
        size_mb = os.path.getsize(raw_ts) / 1048576
        print(f"      ✅ دمج ثنائي: {size_mb:.1f}MB في {time.time()-t0:.1f}s",
              flush=True)
    except Exception as e:
        print(f"      ❌ فشل الدمج الثنائي: {str(e)[:100]}", flush=True)
        return False

    print(f"      📼 تحويل TS → MP4...", flush=True)
    t0 = time.time()
    cmd = [
        _ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+igndts",
        "-i", raw_ts,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        "-y", out_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if (r.returncode == 0 and os.path.exists(out_path)
                and os.path.getsize(out_path) > MIN_SIZE):
            size_mb = os.path.getsize(out_path) / 1048576
            print(f"      ✅ remux: {size_mb:.1f}MB في {time.time()-t0:.1f}s",
                  flush=True)
            try:
                os.unlink(raw_ts)
            except Exception:
                pass
            return True
    except subprocess.TimeoutExpired:
        print(f"      ❌ remux timeout (300s)", flush=True)
        try:
            subprocess.run(["pkill", "-9", "-f", "ffmpeg"],
                           capture_output=True, timeout=3)
        except Exception:
            pass
    except Exception as e:
        print(f"      ❌ remux: {str(e)[:100]}", flush=True)

    print(f"      🔄 fallback: استخدام TS مباشرة", flush=True)
    try:
        shutil.move(raw_ts, out_path)
        if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
            print(f"      ✅ TS مباشر: {os.path.getsize(out_path)/1048576:.1f}MB",
                  flush=True)
            return True
    except Exception as e:
        print(f"      ❌ fallback: {str(e)[:100]}", flush=True)

    return False


# ═══════════════════════════════════════════════════════════════
# استخراج m3u8 من iframe واحد
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
                print(f"      ✨ {len(found)} m3u8 بعد {time.time()-start:.0f}s",
                      flush=True)
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

    for m in m3u8s[:2]:
        res = _download_via_browser(sb, m, str(out_path), ref)
        if res and os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
            return res

    return None


# ═══════════════════════════════════════════════════════════════
# u3seq
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
        raise RuntimeError("لا سيرفرات في u3seq")
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
        raise RuntimeError("لا iframe في u3seq")
    iframe_url = iframe_url.replace("&amp;", "&")
    print(f"   🎯 iframe: {iframe_url[:80]}", flush=True)
    return _extract_from_iframe(sb, iframe_url, out_path)


# ═══════════════════════════════════════════════════════════════
# ★★★ yam — يدعم عدة سيرفرات (v57)
# ═══════════════════════════════════════════════════════════════
def _yam(sb, url, out_path):
    """
    v58: يفتح صفحة السيرفرات see.php ويستخرج الروابط من ul.WatchList > li[data-embed-url]
    (كان الإصدار القديم يفتح watch.php ويجمع iframes الإعلانات فقط).
    """
    # ── بناء رابط see.php من watch.php?vid=XXX ──
    see_url = url
    m = re.search(r"[?&]vid=([A-Za-z0-9]+)", url)
    if m:
        vid = m.group(1)
        base = _origin(url) or "https://yam.ahwaktv.net"
        see_url = f"{base}/see.php?vid={vid}"

    print(f"🖥️  فتح صفحة السيرفرات: {see_url[:90]}", flush=True)
    _open(sb, see_url, wait=2)

    # ── استخراج روابط السيرفرات من ul.WatchList > li[data-embed-url] ──
    servers = []
    for _ in range(8):
        sb.cdp.sleep(0.6)
        servers = _eval(sb, """
            (function(){
                var out = [];
                document.querySelectorAll('ul.WatchList li[data-embed-url]').forEach(function(li){
                    var u = (li.getAttribute('data-embed-url')||'').trim();
                    if(u && u.indexOf('http')===0 && out.indexOf(u)===-1) out.push(u);
                });
                return out;
            })();
        """, []) or []
        if servers:
            break

    # ── خطة بديلة 1: أي عنصر فيه data-embed-url ──
    if not servers:
        servers = _eval(sb, """
            (function(){
                var out = [];
                document.querySelectorAll('[data-embed-url]').forEach(function(el){
                    var u = (el.getAttribute('data-embed-url')||'').trim();
                    if(u && u.indexOf('http')===0 && out.indexOf(u)===-1) out.push(u);
                });
                return out;
            })();
        """, []) or []

    # ── خطة بديلة 2: iframe المشغّل (Playerholder) ──
    if not servers:
        servers = _eval(sb, """
            (function(){
                var out = [];
                var f = document.querySelector('#Playerholder iframe, .embedded-video iframe, iframe');
                if(f && f.src && f.src.indexOf('http')===0) out.push(f.src);
                return out;
            })();
        """, []) or []

    if not servers:
        raise RuntimeError("لا سيرفرات في yam (see.php)")

    print(f"   🎯 {len(servers)} سيرفر", flush=True)
    for u in servers[:8]:
        print(f"      · {u[:100]}", flush=True)

    # ── جرّب كل سيرفر بالترتيب ──
    for i, embed in enumerate(servers[:8]):
        embed = embed.replace("&amp;", "&")
        print(f"\n   ── سيرفر {i+1}/{len(servers)} ──", flush=True)
        try:
            res = _extract_from_iframe(sb, embed, out_path)
            if res and os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                print(f"   ✅ نجح السيرفر {i+1}/{len(servers)}", flush=True)
                return res
        except Exception as e:
            print(f"   ⚠️ سيرفر {i+1} فشل: {str(e)[:100]}", flush=True)

        # ارجع لصفحة السيرفرات قبل السيرفر التالي
        if i < len(servers) - 1:
            _open(sb, see_url, wait=1.2)

    raise RuntimeError(f"فشل كل السيرفرات ({len(servers)})")

# ═══════════════════════════════════════════════════════════════
# نقطة الدخول
# ═══════════════════════════════════════════════════════════════
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
# ضغط بإعدادات ثابتة
# ═══════════════════════════════════════════════════════════════
def _get_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        return float((r.stdout or "0").strip())
    except Exception:
        return 0


def _compress_once(inp, out, scale, crf):
    try:
        cmd = [
            _ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error",
            "-err_detect", "ignore_err",
            "-i", str(inp),
            "-vf", f"scale=-2:{scale}",
            "-c:v", "libx264",
            "-preset", config.COMPRESS_PRESET,
            "-crf", str(crf),
            "-profile:v", "main", "-level", "3.1",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", config.COMPRESS_AUDIO_BITRATE,
            "-ac", "2", "-ar", "44100",
            "-movflags", "+faststart",
            "-threads", str(config.COMPRESS_THREADS),
            "-y", str(out),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode == 0 and Path(out).exists():
            return Path(out).stat().st_size / 1048576
    except Exception as e:
        print(f"      ⚠️ compress_once: {str(e)[:100]}", flush=True)
    return 0


def _compress(inp, out):
    max_mb = config.COMPRESS_MAX_SIZE_MB
    im = Path(inp).stat().st_size / 1048576

    dur = _get_duration(inp)
    dur_min = dur / 60 if dur > 0 else 0

    if dur_min > 0:
        print(f"   ⏱️  المدة: {int(dur_min)} دقيقة | الحجم: {im:.1f}MB | "
              f"الحد: {max_mb}MB", flush=True)

    base_scale = config.COMPRESS_SCALE
    base_crf = config.COMPRESS_CRF

    attempts = [
        (base_scale, base_crf),
        (base_scale, base_crf + 4),
        (base_scale, base_crf + 8),
        (base_scale, base_crf + 12),
        (120, base_crf + 6),
    ]

    print(f"   🎬 {base_scale}p CRF={base_crf} "
          f"({config.COMPRESS_PRESET}, threads={config.COMPRESS_THREADS})",
          flush=True)

    for i, (scale, crf) in enumerate(attempts, 1):
        print(f"   🗜️  [{i}/{len(attempts)}] {scale}p CRF={crf} "
              f"({im:.1f}MB → ?)", flush=True)

        try:
            if Path(out).exists():
                Path(out).unlink()
        except Exception:
            pass

        t0 = time.time()
        om = _compress_once(inp, out, scale, crf)
        elapsed = time.time() - t0

        if om > 0:
            print(f"   ✅ {im:.1f}→{om:.1f}MB في {elapsed:.0f}s", flush=True)
            if om <= max_mb:
                return True
            print(f"   ⚠️ {om:.1f}MB > {max_mb}MB — إعادة محاولة بأقوى...",
                  flush=True)
        else:
            print(f"   ❌ فشل الترميز — المحاولة التالية", flush=True)

    print(f"   ❌ فشلت كل محاولات الضغط ({len(attempts)} محاولة)", flush=True)
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
            print("    ❌ فشل الضغط — حذف الحلقة (لن تُرفع)", flush=True)
            try:
                if raw.exists():
                    raw.unlink()
            except Exception:
                pass
            try:
                if final.exists():
                    final.unlink()
            except Exception:
                pass
            return None

    if raw.exists(): raw.unlink()
    if not final.exists(): return None

    try: _thumb(final, out_dir / f"{prefix}.jpg")
    except Exception: pass

    print(f"    ✅ {final.name} ({final.stat().st_size/1048576:.1f}MB)", flush=True)
    return final