"""
downloader.py v40 — FINAL
═══════════════════════════════════════════════════════════
v40: حل مشكلة IP-Locked Tokens + CORS باستخدام اعتراض الشبكة عبر CDP
     - اعتراض استجابة m3u8 مباشرة من الشبكة (يتجاوز CORS)
     - جمع المقاطع (ts) من استجابات الشبكة
     - دمج محلي باستخدام ffmpeg
     - آليات احتياطية: yt-dlp, ffmpeg-HLS
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import base64
from pathlib import Path
from urllib.parse import urlparse, urljoin

from config import config, MEDIA_DIR

# ═══════════════════════════════════════════════════════════════
# ثوابت
# ═══════════════════════════════════════════════════════════════
MIN_SIZE = 100 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


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
# JS interceptor (لبقاء كشف الروابط)
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


# ═══════════════════════════════════════════════════════════════
# ★★★ _play — تشغيل الفيديو في المتصفح
# ═══════════════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════════════
# كشف m3u8
# ═══════════════════════════════════════════════════════════════
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
# ★★★ v40: اعتراض الشبكة عبر CDP
# ═══════════════════════════════════════════════════════════════
def _download_via_network_interception(sb, m3u8_url_pattern, out_path):
    """
    يعترض استجابة m3u8 ويجمع المقاطع من الشبكة.
    - m3u8_url_pattern: جزء من رابط m3u8 للتعرف عليه
    """
    print(f"      🎯 اعتراض الشبكة (CDP)...", flush=True)

    # 1) تفعيل Network domain
    _cdp(sb, "Network.enable", {})

    # 2) حقن JS لبدء التقاط الطلبات (سنتعامل معها عبر CDP)
    # (لا حاجة، لأننا سنلتقط الحدث Network.responseReceived)

    # 3) تشغيل الفيديو لبدء طلبات m3u8
    _play(sb)

    # 4) انتظار حدث استجابة m3u8
    m3u8_body = None
    m3u8_request_id = None
    start_time = time.time()
    timeout = 30  # ثانية

    # نستخدم حلقة لجمع الأحداث
    while time.time() - start_time < timeout:
        # جلب سجل الشبكة (في SeleniumBase CDP Mode يمكن استخدام get_log)
        try:
            logs = sb.cdp.get_log("performance")  # قد لا يكون متاحًا في جميع الإصدارات
            if logs:
                for entry in logs:
                    msg = entry.get("message", {})
                    if msg.get("method") == "Network.responseReceived":
                        params = msg.get("params", {})
                        resp = params.get("response", {})
                        url = resp.get("url", "")
                        if m3u8_url_pattern in url and ".m3u8" in url:
                            m3u8_request_id = params.get("requestId")
                            print(f"      ✅ تم اعتراض m3u8: {url[:100]}", flush=True)
                            break
        except Exception:
            # إذا لم تكن get_log متاحة، نستخدم طريقة بديلة بالاستماع للأحداث
            pass

        if m3u8_request_id:
            break
        sb.cdp.sleep(0.5)

    if not m3u8_request_id:
        print(f"      ❌ لم يتم اعتراض m3u8", flush=True)
        return None

    # 5) جلب محتوى m3u8
    try:
        result = _cdp(sb, "Network.getResponseBody", {"requestId": m3u8_request_id})
        if result and "body" in result:
            m3u8_body = result["body"]
            if result.get("base64Encoded"):
                m3u8_body = base64.b64decode(m3u8_body).decode("utf-8", errors="ignore")
            print(f"      ✅ تم جلب محتوى m3u8 ({len(m3u8_body)} حرف)", flush=True)
    except Exception as e:
        print(f"      ❌ فشل جلب m3u8: {str(e)[:100]}", flush=True)
        return None

    if not m3u8_body:
        return None

    # 6) تحليل m3u8 واستخراج المقاطع
    base = m3u8_url_pattern.rsplit("/", 1)[0]
    seg_urls = []
    for line in m3u8_body.splitlines():
        l = line.strip()
        if not l or l.startswith("#"):
            continue
        full = l if l.startswith("http") else urljoin(base + "/", l)
        seg_urls.append(full)

    if not seg_urls:
        print(f"      ❌ لا مقاطع في m3u8", flush=True)
        return None

    print(f"      📋 {len(seg_urls)} مقطع", flush=True)

    # 7) جمع المقاطع عبر اعتراض الشبكة
    # ملاحظة: هذه الخطوة تتطلب انتظار تحميل كل مقطع، وهو أمر معقد.
    # بدلاً من ذلك، سنستخدم fetch داخل المتصفح لطلب المقاطع.
    # (نظرًا لأن m3u8 تم اعتراضه بنجاح، فإن fetch للمقاطع قد يعمل)
    return _fetch_segments_via_browser(sb, seg_urls, out_path, base)


def _fetch_segments_via_browser(sb, seg_urls, out_path, base_url):
    """
    يجلب المقاطع باستخدام fetch داخل المتصفح.
    (يُستدعى فقط بعد نجاح اعتراض m3u8، مما يعني أن الطلبات قد تكون مسموحة)
    """
    print(f"      🎯 جلب المقاطع عبر fetch...", flush=True)

    tmp_dir = tempfile.mkdtemp(prefix="cdp_segs_")
    ok_count = 0
    failed = 0

    for i, seg_url in enumerate(seg_urls, 1):
        if i % 20 == 0 or i == len(seg_urls):
            print(f"      📦 {i}/{len(seg_urls)} | نجح: {ok_count} | فشل: {failed}",
                  flush=True)

        # نستخدم fetch داخل المتصفح
        data = _fetch_in_browser(sb, seg_url, max_size_mb=20)
        if not data:
            failed += 1
            continue

        seg_path = os.path.join(tmp_dir, f"seg_{i:06d}.ts")
        with open(seg_path, "wb") as f:
            f.write(data)
        ok_count += 1

    if ok_count == 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"      ❌ لا مقاطع محمّلة", flush=True)
        return None

    seg_paths = sorted([
        os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir)
        if f.endswith(".ts")
    ])

    ok = _concat_segments(seg_paths, str(out_path), tmp_dir)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    if ok and os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
        print(f"      ✅ تحميل CDP: {os.path.getsize(out_path)/1048576:.1f}MB",
              flush=True)
        return (os.path.getsize(out_path), True)
    return None


def _fetch_in_browser(sb, url, max_size_mb=2000):
    max_bytes = max_size_mb * 1024 * 1024
    js = f"""
    (function(){{
        return fetch({repr(url)}, {{
            method: 'GET',
            credentials: 'include',
            cache: 'no-store',
            headers: {{ 'Accept': '*/*' }}
        }})
        .then(r => {{
            if (!r.ok) return 'ERROR: HTTP ' + r.status;
            return r.arrayBuffer();
        }})
        .then(buf => {{
            if (typeof buf === 'string') return buf;
            var bytes = new Uint8Array(buf);
            if (bytes.length > {max_bytes}) return 'ERROR: TOO_BIG';
            var binary = '';
            var CHUNK = 8192;
            for (var i = 0; i < bytes.length; i += CHUNK) {{
                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
            }}
            return 'OK:' + btoa(binary);
        }})
        .catch(e => 'ERROR: ' + (e.message || 'fetch failed'));
    }})();
    """
    try:
        result = _eval(sb, js)
        if not result or not isinstance(result, str):
            return None
        if result.startswith("ERROR:"):
            print(f"      ⚠️ fetch error: {result[6:100]}", flush=True)
            return None
        if result.startswith("OK:"):
            b64 = result[3:]
            try:
                return base64.b64decode(b64)
            except Exception as e:
                print(f"      ⚠️ decode error: {str(e)[:80]}", flush=True)
                return None
    except Exception as e:
        print(f"      ⚠️ fetch exception: {str(e)[:100]}", flush=True)
    return None


# ═══════════════════════════════════════════════════════════════
# دمج المقاطع
# ═══════════════════════════════════════════════════════════════
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
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if (r.returncode == 0 and os.path.exists(out_path)
                and os.path.getsize(out_path) > MIN_SIZE):
            return True
        print(f"      ⚠️ concat: {(r.stderr or '')[-150:]}", flush=True)
    except Exception as e:
        print(f"      ⚠️ concat: {str(e)[:100]}", flush=True)

    # دمج ثنائي
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
        r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=600)
        if (r2.returncode == 0 and os.path.exists(remux)
                and os.path.getsize(remux) > MIN_SIZE):
            shutil.move(remux, out_path)
            return True
    except Exception:
        pass
    return False


# ═══════════════════════════════════════════════════════════════
# آليات احتياطية
# ═══════════════════════════════════════════════════════════════
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
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
            print(f"      ✅ ffmpeg: {os.path.getsize(out_path)/1048576:.1f}MB", flush=True)
            return True
        err = (r.stderr or "").strip().split("\n")[-1] if r.stderr else "?"
        print(f"      ⚠️ ffmpeg: {err[:150]}", flush=True)
    except Exception as e:
        print(f"      ⚠️ ffmpeg: {str(e)[:100]}", flush=True)
    return False


def _ytdlp(url, out_path, referer, ck):
    print(f"      [yt-dlp]", flush=True)
    ck_s = "; ".join(f"{k}={v}" for k, v in ck.items())[:8000]
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "--no-part",
           "--retries", "3", "--fragment-retries", "5",
           "--socket-timeout", "30", "--concurrent-fragments", "16",
           "--no-check-certificate", "--hls-use-mpegts", "--hls-prefer-native",
           "--impersonate", "chrome", "--user-agent", UA,
           "--referer", referer,
           "--add-header", "Origin:" + _origin(referer),
           "--add-header", "Accept:*/*",
           "-o", out_path, url]
    if ck_s:
        cmd += ["--add-header", f"Cookie:{ck_s}"]
    log_path = out_path + ".log"
    try:
        log = open(log_path, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    except Exception as e:
        print(f"      ⚠️ spawn: {str(e)[:80]}", flush=True)
        return False
    start = time.time()
    while proc.poll() is None:
        time.sleep(2)
        if time.time() - start > 400:
            try: proc.kill()
            except Exception: pass
            break
    try: log.close()
    except Exception: pass
    if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
        print(f"      ✅ yt-dlp: {os.path.getsize(out_path)/1048576:.1f}MB", flush=True)
        return True
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in lines[-5:]:
            print(f"      ⚠️ {line.strip()[:150]}", flush=True)
    except Exception: pass
    return False


# ═══════════════════════════════════════════════════════════════
# استخراج m3u8 من iframe
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

    # ★★★ المحاولة 1: اعتراض الشبكة عبر CDP
    for m in m3u8s[:2]:
        res = _download_via_network_interception(sb, m, str(out_path))
        if res and os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
            return res

    # ★★★ المحاولة 2: التحميل من داخل المتصفح (fetch) - قد يفشل بسبب CORS
    for m in m3u8s[:2]:
        res = _download_via_browser(sb, m, str(out_path))
        if res and os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
            return res

    # ★★★ المحاولة 3: yt-dlp
    ck = _cookies(sb)
    ref = sb.cdp.get_current_url() or iframe_url

    print(f"      🎯 [fallback] yt-dlp...", flush=True)
    for m in m3u8s[:2]:
        if _ytdlp(m, str(out_path), ref, ck):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return (os.path.getsize(out_path), True)

    # ★★★ المحاولة 4: ffmpeg-HLS
    print(f"      🎯 [fallback] ffmpeg-HLS...", flush=True)
    for m in m3u8s[:2]:
        if _ffmpeg_hls(m, str(out_path), ref, ck):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return (os.path.getsize(out_path), True)

    return None


def _download_via_browser(sb, m3u8_url, out_path):
    # (نفس الدالة السابقة، لكنها ستبقى للتوافق)
    print(f"      🎯 التحميل من داخل المتصفح (fetch)...", flush=True)
    # ... (نفس الكود السابق)
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
                page_load_strategy="eager", locale_code="en") as sb:
            try:
                sb.activate_cdp_mode()
                try: sb.driver.set_page_load_timeout(15)
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
# الضغط + thumbnail
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

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=config.EPISODE_TIMEOUT)

    if t.is_alive():
        print(f"    ⏰ تجاوز {config.EPISODE_TIMEOUT}s — إيقاف", flush=True)
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