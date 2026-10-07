"""
downloader.py v34 — FINAL
═══════════════════════════════════════════════════════════
v34: إصلاح جذري لمشكلة ffmpeg concat:
     - قبول أي صيغة فيديو (mp4 / mpegts / mov) وليس mpegts فقط
     - استخدام concat: protocol كخيار أول (بدون ملف نصي)
     - fallback: concat demuxer → binary concat → ffmpeg HLS مباشر
     - إضافة تحقق صامت لا يمنع الدمج
"""

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

# ═══════════════════════════════════════════════════════════════
# ثوابت
# ═══════════════════════════════════════════════════════════════
MIN_SIZE = 100 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

IMPERSONATES = ["chrome124", "chrome120", "chrome110", "firefox133"]


# ═══════════════════════════════════════════════════════════════
# أدوات عامة
# ═══════════════════════════════════════════════════════════════
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
    if "modablaj-" in u or "/video/" in u:
        return True
    if "see.php" in u and "vid=" in u:
        return True
    if "watch.php" in u and "vid=" in u:
        return True
    if "/watch/" in u and ".php" not in u:
        return True
    return False


def _is_valid_video(path):
    """
    ★ v34: قبول أي صيغة فيديو (mp4, mpegts, mov, matroska, ...)
    بدل حصرها على mpegts فقط.
    """
    if not path or not os.path.exists(path) or os.path.getsize(path) < 100:
        return False
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False
        # يكفي وجود stream واحد على الأقل
        return "video" in (r.stdout or "").lower()
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# JS interceptor (كما هو)
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


def _play(sb):
    _eval(sb, """
        (function(){try{
            var v=document.querySelector('video');
            if(v){v.muted=true;if(v.play)v.play().catch(function(){});}
            if(typeof jwplayer!=='undefined'){var p=jwplayer();if(p){if(p.play)p.play(true);if(p.setMute)p.setMute(true);}}
            ['video','button','.vjs-big-play-button','.jw-icon-playback','.jw-display-icon-container','[class*=play]'].forEach(function(s){
                document.querySelectorAll(s).forEach(function(el){try{el.click();}catch(e){}});
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
# m3u8 parser + resolve
# ═══════════════════════════════════════════════════════════════
def _parse_m3u8(text, base):
    segs, variants = [], []
    for line in text.splitlines():
        l = line.strip()
        if not l or l.startswith("#"):
            continue
        full = l if l.startswith("http") else urljoin(base + "/", l)
        if ".m3u8" in l.lower():
            variants.append(full)
        else:
            segs.append(full)
    return segs, variants


def _resolve_m3u8(url, headers, depth=0, max_depth=4):
    if depth > max_depth:
        return None, []
    r = None
    last_error = "unknown"
    for imp in IMPERSONATES:
        try:
            r = cffi_requests.get(
                url, headers=headers, impersonate=imp,
                timeout=15, verify=False, allow_redirects=True,
            )
            if r.status_code == 200 and r.text:
                last_error = None
                break
            last_error = f"HTTP {r.status_code}"
        except Exception as e:
            last_error = str(e)[:80]
    if last_error:
        print(f"      ⚠️ resolve[{depth}]: {last_error}", flush=True)
        return None, []
    base = url.rsplit("/", 1)[0]
    segs, variants = _parse_m3u8(r.text, base)
    if segs:
        print(f"      ✅ resolve[{depth}]: {len(segs)} seg", flush=True)
        return url, segs
    if variants:
        print(f"      📋 resolve[{depth}]: {len(variants)} variant", flush=True)
        for v in variants[:2]:
            f, s = _resolve_m3u8(v, headers, depth + 1, max_depth)
            if s:
                return f, s
    print(f"      ⚠️ resolve[{depth}]: لا segments", flush=True)
    return None, []


# ═══════════════════════════════════════════════════════════════
# ★★★ v34: دمج محسّن (concat protocol أولاً)
# ═══════════════════════════════════════════════════════════════
def _try_concat_protocol(seg_paths, out_path):
    """
    الطريقة الأسرع: concat: protocol مباشرة بدون ملف نصي.
    تعمل مع MPEG-TS و fMP4.
    """
    concat_input = "concat:" + "|".join(seg_paths)
    cmd = [
        _ffmpeg(), "-hide_banner", "-loglevel", "warning",
        "-i", concat_input,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-fflags", "+genpts+igndts",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        "-y", out_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if (r.returncode == 0
                and os.path.exists(out_path)
                and os.path.getsize(out_path) > MIN_SIZE):
            return True
        err = (r.stderr or "")[-200:]
        print(f"      ⚠️ concat protocol: {err}", flush=True)
    except Exception as e:
        print(f"      ⚠️ concat protocol exception: {str(e)[:100]}", flush=True)
    return False


def _try_concat_demuxer(seg_paths, out_path, seg_dir):
    """طريقة concat demuxer عبر ملف نصي."""
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
        if (r.returncode == 0
                and os.path.exists(out_path)
                and os.path.getsize(out_path) > MIN_SIZE):
            return True
        err = (r.stderr or "")[-200:]
        print(f"      ⚠️ concat demuxer: {err}", flush=True)
    except Exception as e:
        print(f"      ⚠️ concat demuxer exception: {str(e)[:100]}", flush=True)
    return False


def _try_binary_concat(seg_paths, out_path, seg_dir):
    """دمج ثنائي + remux — الحل الأخير."""
    print(f"      🔄 محاولة دمج ثنائي (binary concat)...", flush=True)
    try:
        bin_path = os.path.join(seg_dir, "binary.ts")
        with open(bin_path, "wb") as out:
            for p in seg_paths:
                with open(p, "rb") as seg:
                    shutil.copyfileobj(seg, out, length=1024 * 1024)

        remux = os.path.join(seg_dir, "remux.mp4")
        cmd = [
            _ffmpeg(), "-hide_banner", "-loglevel", "warning",
            "-fflags", "+genpts+igndts",
            "-i", bin_path,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            "-y", remux,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if (r.returncode == 0
                and os.path.exists(remux)
                and os.path.getsize(remux) > MIN_SIZE):
            shutil.move(remux, out_path)
            print(f"      ✅ دمج ثنائي نجح", flush=True)
            return True
        err = (r.stderr or "")[-200:]
        print(f"      ⚠️ remux: {err}", flush=True)
    except Exception as e:
        print(f"      ⚠️ binary concat: {str(e)[:100]}", flush=True)
    return False


def _cffi_download(segments, out_path, referer, ck):
    """
    ★ v34: ترتيب الدمج الجديد
       1. concat: protocol (الأسرع، بدون ملف نصي)
       2. concat demuxer (مع -f concat -safe 0)
       3. binary concat + remux (الحل الأخير)
    """
    if not segments:
        return None

    headers = {
        "Referer": referer or "",
        "Origin": _origin(referer),
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    if ck:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in ck.items())[:8000]

    # اختبار
    test_ok = False
    last_err = "?"
    for imp in IMPERSONATES:
        try:
            r = cffi_requests.get(segments[0], headers=headers,
                                   impersonate=imp, timeout=15, verify=False)
            if r.status_code == 200 and len(r.content) > 100:
                test_ok = True
                print(f"      ⚡ test OK [{imp}] ({len(r.content)}B)", flush=True)
                break
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)[:80]

    if not test_ok:
        print(f"      ⚠️ cffi test فشل: {last_err}", flush=True)
        return None

    print(f"      ⚡ {len(segments)} segments...", flush=True)
    seg_dir = tempfile.mkdtemp(prefix="hls_")
    paths, failed, total = {}, 0, 0

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _dl(args):
        idx, u = args
        for imp in IMPERSONATES[:3]:
            try:
                r = cffi_requests.get(u, headers=headers, impersonate=imp,
                                       timeout=20, verify=False)
                if r.status_code == 200 and len(r.content) > 100:
                    p = os.path.join(seg_dir, f"seg_{idx:06d}.ts")
                    with open(p, "wb") as f:
                        f.write(r.content)
                    return (idx, p, len(r.content))
            except Exception:
                continue
        return (idx, None, 0)

    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(_dl, (i, s)) for i, s in enumerate(segments)]
        done = 0
        for f in as_completed(futs):
            idx, p, sz = f.result()
            done += 1
            if p:
                paths[idx] = p
                total += sz
            else:
                failed += 1
            if done % 40 == 0 or done == len(segments):
                print(f"      📦 {done}/{len(segments)} | "
                      f"{total/1048576:.1f}MB | فشل: {failed}", flush=True)

    if not paths or failed > len(segments) * 0.2:
        print(f"      ⚠️ فشل عالٍ: {failed}/{len(segments)}", flush=True)
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    sorted_s = [paths[k] for k in sorted(paths)]

    # ★★★ v34: دمج بدون استبعاد مسبق
    ok = False

    # 1) concat protocol
    if not ok:
        ok = _try_concat_protocol(sorted_s, out_path)

    # 2) concat demuxer
    if not ok:
        ok = _try_concat_demuxer(sorted_s, out_path, seg_dir)

    # 3) binary concat
    if not ok:
        ok = _try_binary_concat(sorted_s, out_path, seg_dir)

    shutil.rmtree(seg_dir, ignore_errors=True)

    if ok:
        size_mb = os.path.getsize(out_path) / 1048576
        print(f"      ✅ cffi: {size_mb:.1f}MB", flush=True)
        return (os.path.getsize(out_path), True)

    return None


# ═══════════════════════════════════════════════════════════════
# yt-dlp + ffmpeg HLS (كما هما)
# ═══════════════════════════════════════════════════════════════
def _ytdlp(url, out_path, referer, ck):
    print(f"      [yt-dlp]", flush=True)
    ck_s = "; ".join(f"{k}={v}" for k, v in ck.items())[:8000]
    cmd = ["yt-dlp",
           "--no-warnings", "--no-playlist", "--no-part",
           "--retries", "3", "--fragment-retries", "5",
           "--socket-timeout", "30",
           "--concurrent-fragments", "16",
           "--no-check-certificate",
           "--hls-use-mpegts", "--hls-prefer-native",
           "--impersonate", "chrome",
           "--user-agent", UA,
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
            try:
                proc.kill()
            except Exception:
                pass
            break
    try:
        log.close()
    except Exception:
        pass
    if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
        print(f"      ✅ yt-dlp: {os.path.getsize(out_path)/1048576:.1f}MB", flush=True)
        return True
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in lines[-5:]:
            print(f"      ⚠️ {line.strip()[:150]}", flush=True)
    except Exception:
        pass
    return False


def _ffmpeg_hls(m3u8_url, out_path, referer, ck):
    print(f"      [ffmpeg-hls]", flush=True)
    headers = (
        f"Referer: {referer}\r\n"
        f"Origin: {_origin(referer)}\r\n"
        f"User-Agent: {UA}\r\n"
        "Accept: */*\r\n"
        "Sec-Fetch-Dest: empty\r\n"
        "Sec-Fetch-Mode: cors\r\n"
        "Sec-Fetch-Site: cross-site\r\n"
    )
    if ck:
        ck_s = "; ".join(f"{k}={v}" for k, v in ck.items())[:8000]
        headers += f"Cookie: {ck_s}\r\n"
    cmd = [_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "warning",
           "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
           "-headers", headers,
           "-user_agent", UA,
           "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
           "-i", m3u8_url,
           "-c", "copy", "-bsf:a", "aac_adtstoasc",
           "-movflags", "+faststart",
           "-y", out_path]
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
    for u in m3u8s[:3]:
        print(f"         · {u[:100]}", flush=True)
    ck = _cookies(sb)
    ref = sb.cdp.get_current_url() or iframe_url
    # 1) cffi
    for m in m3u8s[:3]:
        headers = {
            "Referer": ref,
            "Origin": _origin(ref),
            "User-Agent": UA,
            "Accept": "*/*",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
        }
        if ck:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in ck.items())[:8000]
        _, segs = _resolve_m3u8(m, headers)
        if segs:
            res = _cffi_download(segs, str(out_path), ref, ck)
            if res and os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return res
    # 2) yt-dlp
    for m in m3u8s[:2]:
        if _ytdlp(m, str(out_path), ref, ck):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return (os.path.getsize(out_path), True)
    # 3) ffmpeg HLS
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
        if not cur.endswith("/"):
            cur += "/"
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
        if servers:
            break
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
        print(f"   ⚠️ لا iframe مباشر — ننتظر", flush=True)
        for _ in range(6):
            sb.cdp.sleep(1)
            iframe_url = _eval(sb, """
                (function(){
                    var f=document.querySelector('.watch iframe');
                    if(f&&f.src&&f.src.startsWith('http'))return f.src;
                    return null;
                })();
            """)
            if iframe_url:
                break
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
        print(f"   ⚠️ لا iframe — انتظار", flush=True)
        for _ in range(6):
            sb.cdp.sleep(1)
            iframe_url = _eval(sb, """
                (function(){
                    var f=document.querySelector('iframe');
                    if(f&&f.src&&f.src.startsWith('http'))return f.src;
                    return null;
                })();
            """)
            if iframe_url:
                break
    if not iframe_url:
        print(f"   ❌ لا iframe", flush=True)
        return None
    print(f"   🎯 iframe: {iframe_url[:80]}", flush=True)
    return _extract_from_iframe(sb, iframe_url, out_path)


# ═══════════════════════════════════════════════════════════════
# نقطة الدخول
# ═══════════════════════════════════════════════════════════════════
def _run_browser(url, out_path):
    from seleniumbase import SB
    try:
        with SB(uc=True, xvfb=True, headless=False, incognito=True,
                ad_block_on=True, disable_csp=True,
                page_load_strategy="eager", locale_code="en") as sb:
            try:
                sb.activate_cdp_mode()
                try:
                    sb.driver.set_page_load_timeout(15)
                except Exception:
                    pass
                _install_js(sb)
                u_low = url.lower()
                if "modablaj-" in u_low or "/video/" in u_low:
                    print(f"   🎯 u3seq (عشق)", flush=True)
                    return _u3seq(sb, url, out_path)
                elif "see.php" in u_low or "watch.php" in u_low:
                    print(f"   🎯 yam (اهواك)", flush=True)
                    return _yam(sb, url, out_path)
                else:
                    print(f"   🎯 generic", flush=True)
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
           "-err_detect", "ignore_err",
           "-i", str(inp),
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
        except Exception:
            pass
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
        try:
            result[0] = _run_browser(url, raw)
        except Exception as e:
            exc[0] = e
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=config.EPISODE_TIMEOUT)
    if t.is_alive():
        print(f"    ⏰ تجاوز {config.EPISODE_TIMEOUT}s — إيقاف", flush=True)
        for p in ["yt-dlp", "chrome", "ffmpeg"]:
            try:
                subprocess.run(["pkill", "-9", "-f", p], capture_output=True, timeout=3)
            except Exception:
                pass
    if exc[0]:
        print(f"    ⚠️ {str(exc[0])[:150]}", flush=True)
        result[0] = None
    if not result[0] or not raw.exists():
        print(f"    ⚠️ فشل", flush=True)
        try:
            if raw.exists():
                raw.unlink()
        except Exception:
            pass
        return None
    print(f"    📦 {raw.stat().st_size/1048576:.1f}MB", flush=True)
    if config.SKIP_COMPRESS:
        shutil.move(str(raw), str(final))
    else:
        if not _compress(raw, final):
            print("    ⚠️ فشل الضغط — الأصلي", flush=True)
            shutil.move(str(raw), str(final))
    if raw.exists():
        raw.unlink()
    if not final.exists():
        return None
    try:
        _thumb(final, out_dir / f"{prefix}.jpg")
    except Exception:
        pass
    print(f"    ✅ {final.name} ({final.stat().st_size/1048576:.1f}MB)", flush=True)
    return final