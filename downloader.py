"""
downloader.py — Universal HLS Downloader v28 (NO-FREEZE)
═══════════════════════════════════════════════════════════
إصلاحات v28:
  • لا add_handler — JS interceptor + polling فقط
  • كل سيرفر يُختبر بمفرده مع logs واضحة
  • لا threads مع CDP — يمنع "event loop is already running"
  • u3seq أولاً (من run_all.py)
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

MIN_SIZE = 100 * 1024
MIN_PARTIAL_ACCEPT = 30 * 1024 * 1024

M3U8_SEARCH_TIMEOUT = int(os.environ.get("M3U8_SEARCH_TIMEOUT", "30"))
YTDLP_TIMEOUT = int(os.environ.get("YTDLP_TIMEOUT", "600"))
OPEN_TIMEOUT = int(os.environ.get("OPEN_TIMEOUT", "12"))
STALL_TIMEOUT = int(os.environ.get("STALL_TIMEOUT", "45"))
CURL_CFFI_WORKERS = int(os.environ.get("CURL_CFFI_WORKERS", "16"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class TimeoutError_(Exception):
    pass


# ═══════════════════════════════════════════════════════════════
# أدوات عامة
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
# ★★★ JS interceptor — بدون CDP handlers (يحل التجمّد)
# ═══════════════════════════════════════════════════════════════
_INTERCEPTOR = r"""
(function(){
    window.__cap_m3u8 = window.__cap_m3u8 || [];
    function rec(u){
        try{
            if(typeof u!=='string')return;
            if(u.indexOf('.m3u8')===-1 && u.indexOf('.mpd')===-1)return;
            if(window.__cap_m3u8.indexOf(u)===-1){
                window.__cap_m3u8.push(u);
            }
        }catch(e){}
    }

    // fetch
    if(window.fetch && !window.__fp){
        var o=window.fetch;
        window.fetch=function(i,init){
            try{var u=(typeof i==='string')?i:(i&&i.url);rec(u);}catch(e){}
            return o.apply(this,arguments);
        };
        window.__fp=true;
    }

    // XHR
    if(window.XMLHttpRequest && !window.__xp){
        var xo=XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open=function(m,u){
            try{rec(u);}catch(e){}
            return xo.apply(this,arguments);
        };
        window.__xp=true;
    }

    // video.src
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

    // ★ Hls.js patch — يعترض loadSource()
    window.__patch_hls=function(){
        try{
            if(typeof Hls!=='undefined' && !window.__hls_patched){
                if(Hls.prototype && Hls.prototype.loadSource){
                    var orig=Hls.prototype.loadSource;
                    Hls.prototype.loadSource=function(u){
                        try{rec(u);}catch(e){}
                        return orig.apply(this,arguments);
                    };
                }
                window.__hls_patched=true;
            }
        }catch(e){}
    };

    // ★ jwplayer patch
    window.__patch_jw=function(){
        try{
            if(typeof jwplayer!=='undefined' && !window.__jw_patched){
                var orig=jwplayer;
                window.jwplayer=function(){
                    var p=orig.apply(this,arguments);
                    if(p){
                        if(p.setup){
                            var os=p.setup;
                            p.setup=function(cfg){
                                try{
                                    if(cfg&&cfg.file)rec(cfg.file);
                                    if(cfg&&cfg.sources)cfg.sources.forEach(function(s){if(s.file)rec(s.file);});
                                }catch(e){}
                                return os.apply(this,arguments);
                            };
                        }
                        if(p.load){
                            var ol=p.load;
                            p.load=function(pl){
                                try{
                                    if(pl){
                                        var items=Array.isArray(pl)?pl:[pl];
                                        items.forEach(function(it){
                                            if(it.file)rec(it.file);
                                            if(it.sources)it.sources.forEach(function(s){if(s.file)rec(s.file);});
                                        });
                                    }
                                }catch(e){}
                                return ol.apply(this,arguments);
                            };
                        }
                    }
                    return p;
                };
                Object.assign(window.jwplayer,orig);
                window.__jw_patched=true;
            }
        }catch(e){}
    };

    // ★ videojs patch
    window.__patch_vjs=function(){
        try{
            if(typeof videojs!=='undefined' && !window.__vjs_patched){
                var origReg=videojs.registerComponent;
                if(origReg){
                    videojs.registerComponent=function(name,comp){
                        if(comp&&comp.prototype&&!comp.prototype.__vjs_p){
                            var os=comp.prototype.src;
                            if(os){
                                comp.prototype.src=function(source){
                                    try{
                                        if(typeof source==='string')rec(source);
                                        if(source&&source.src)rec(source.src);
                                    }catch(e){}
                                    return os.apply(this,arguments);
                                };
                            }
                            comp.prototype.__vjs_p=true;
                        }
                        return origReg.apply(this,arguments);
                    };
                }
                window.__vjs_patched=true;
            }
        }catch(e){}
    };

    setInterval(function(){
        window.__patch_hls();
        window.__patch_jw();
        window.__patch_vjs();
    },500);
})();
"""


def _install_interceptor(sb):
    """حقن JS بدون CDP handler."""
    try:
        sb.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": _INTERCEPTOR,
        })
        print("      💉 interceptor", flush=True)
    except Exception as e:
        print(f"      ⚠️ interceptor: {str(e)[:80]}", flush=True)


def _read_captured(sb):
    try:
        urls = sb.cdp.execute_script("return window.__cap_m3u8 || [];")
        if isinstance(urls, list):
            return [u for u in urls if isinstance(u, str) and
                    (".m3u8" in u or ".mpd" in u)]
    except Exception:
        pass
    return []


# ═══════════════════════════════════════════════════════════════
# مساعدات CDP (بسيطة — بدون threads)
# ═══════════════════════════════════════════════════════════════
def _eval(sb, js, default=None):
    try: return sb.cdp.execute_script(js)
    except Exception: return default


def _cdp(sb, cmd, params=None):
    if params is None: params = {}
    try: return sb.driver.execute_cdp_cmd(cmd, params)
    except Exception: return None


def _get_cookies(sb):
    try:
        r = _cdp(sb, "Network.getAllCookies", {})
        if r and r.get("cookies"):
            return {c["name"]: c["value"] for c in r["cookies"]
                    if c.get("name") and c.get("value")}
    except Exception: pass
    return {}


# ═══════════════════════════════════════════════════════════════
# m3u8 parser
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

    print(f"      ⚡ {len(segments)} segment...", flush=True)
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
                print(f"         📦 {done}/{len(segments)} | {total_bytes/1048576:.1f}MB", flush=True)

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
        print(f"      ✅ cffi: {os.path.getsize(out_path)/1048576:.1f}MB", flush=True)
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
           '--socket-timeout', '45', '--concurrent-fragments', '16',
           '--no-check-certificate', '--continue',
           '--hls-use-mpegts', '--hls-prefer-native',
           '--impersonate', 'chrome', '--user-agent', UA,
           '--referer', referer, '--add-header', 'Accept:*/*']
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
# كشف m3u8 (polling فقط — بدون CDP handlers)
# ═══════════════════════════════════════════════════════════════
def _scan_m3u8(sb):
    found = set()

    # 1) JS interceptor
    for u in _read_captured(sb): found.add(u)

    # 2) performance
    try:
        perf = _eval(sb, "try{return performance.getEntriesByType('resource').map(e=>e.name)}catch(e){return[]}", [])
        if isinstance(perf, list):
            for u in perf:
                if ".m3u8" in u or ".mpd" in u: found.add(u)
    except Exception: pass

    # 3) HTML
    try:
        html = sb.cdp.get_page_source() or ""
        for m in re.finditer(r'(https?:[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*)', html):
            found.add(m.group(1).replace("\\/", "/"))
    except Exception: pass

    # 4) APIs
    try:
        urls_js = _eval(sb, """
            (function(){
                var out=[];
                try{
                    if(typeof Hls!=='undefined' && Hls.instances){
                        Hls.instances.forEach(function(h){try{if(h.url)out.push(h.url);}catch(e){}});
                    }
                    if(window.hls&&window.hls.url)out.push(window.hls.url);
                    if(typeof jwplayer!=='undefined'){
                        try{var p=jwplayer();if(p){var pl=p.getPlaylist&&p.getPlaylist();if(pl)pl.forEach(function(it){if(it.file)out.push(it.file);if(it.sources)it.sources.forEach(function(s){if(s.file)out.push(s.file);});});}}catch(e){}
                    }
                    if(typeof videojs!=='undefined'){
                        try{var p2=videojs.getPlayers();for(var k in p2){try{var s=p2[k].currentSrc&&p2[k].currentSrc();if(s)out.push(s);var t=p2[k].tech&&p2[k].tech(true);if(t&&t.hls&&t.hls.url)out.push(t.hls.url);}catch(e){}}}catch(e){}
                    }
                    var v=document.querySelector('video');
                    if(v){if(v.currentSrc)out.push(v.currentSrc);if(v.src)out.push(v.src);}
                    document.querySelectorAll('source').forEach(function(s){if(s.src)out.push(s.src);});
                }catch(e){}
                return out;
            })();
        """, [])
        if isinstance(urls_js, list):
            for u in urls_js:
                if isinstance(u, str) and (".m3u8" in u or ".mpd" in u): found.add(u)
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
    _eval(sb, """
        (function(){try{
            var v=document.querySelector('video');
            if(v){v.muted=true;if(v.play)v.play().catch(function(){});}
            if(typeof jwplayer!=='undefined'){var p=jwplayer();if(p){if(p.play)p.play(true);if(p.setMute)p.setMute(true);}}
            if(typeof videojs!=='undefined'){var p2=videojs.getPlayers();for(var k in p2){try{p2[k].play();p2[k].muted(true);}catch(e){}}}
            ['video','.vjs-big-play-button','.jw-icon-playback','[class*=play]','button'].forEach(function(s){
                var els=document.querySelectorAll(s);
                els.forEach(function(el){try{el.click();}catch(e){}});
            });
        }catch(e){}})();
    """)
    try:
        rect = _eval(sb, """
            (function(){
                var cs=[document.querySelector('video'),document.querySelector('.jwplayer'),document.querySelector('.video-js')];
                for(var i=0;i<cs.length;i++){var el=cs[i];if(!el)continue;var r=el.getBoundingClientRect();if(r.width>50&&r.height>50)return {x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)};}
                return null;
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
                sb.cdp.sleep(0.3)
    except Exception: pass


def _fast_open(sb, url, wait=1.0):
    _cdp(sb, "Page.navigate", {"url": url})
    sb.cdp.sleep(wait)
    _cdp(sb, "Runtime.evaluate", {"expression": "try{window.stop()}catch(e){}"})


def _cf_bypass(sb, url, timeout_s=20):
    print(f"      🔄 CF bypass...", flush=True)
    start = time.time()
    _cdp(sb, "Page.navigate", {"url": url, "referrer": ""})
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


def _get_nested_iframe(sb, exclude=None):
    if exclude is None: exclude = []
    ifr = _eval(sb, """
        (function(){
            var ifs=document.querySelectorAll('iframe');
            for(var i=0;i<ifs.length;i++){
                var f=ifs[i];
                var s=f.src||f.getAttribute('src')||'';
                if(s&&s.startsWith('http')
                    &&s.indexOf('google')===-1
                    &&s.indexOf('facebook')===-1
                    &&s.indexOf('doubleclick')===-1)
                    return s;
            }
            return null;
        })();
    """)
    if not ifr: return None
    ifr = ifr.replace("&amp;", "&")
    for ex in exclude:
        if ifr == ex: return None
    return ifr


# ═══════════════════════════════════════════════════════════════
# ★★★ اختبار سيرفر واحد
# ═══════════════════════════════════════════════════════════════
def _try_one_server(sb, sname, iframe_url, out_path, search_timeout=25):
    """
    يحاول سيرفر واحد فقط. يعيد:
      ('success', size) — نجح
      ('no_m3u8', None) — لا m3u8
      ('fail', None) — فشل
    """
    print(f"\n   ═══ {sname} ═══", flush=True)
    print(f"      🔗 {iframe_url[:90]}", flush=True)

    try:
        # luluvdo → CF bypass
        if "luluvdo" in sname.lower():
            if not _cf_bypass(sb, iframe_url, 25):
                print(f"      ⏭️ CF فشل — تخطي", flush=True)
                return ("fail", None)
        else:
            _fast_open(sb, iframe_url, wait=2.5)
    except Exception as e:
        print(f"      ❌ navigate: {str(e)[:80]}", flush=True)
        return ("fail", None)

    # iframe متداخل
    nested = _get_nested_iframe(sb, exclude=[iframe_url])
    if nested:
        print(f"      🔄 متداخل: {nested[:70]}", flush=True)
        _fast_open(sb, nested, wait=2.0)

    # انتظر المشغل + نقرات
    print(f"      🎬 نقرات...", flush=True)
    for cycle in range(4):
        _trigger_play(sb)
        sb.cdp.sleep(1.0)
        if _scan_m3u8(sb):
            break

    # بحث m3u8
    print(f"      🔍 بحث m3u8 ({search_timeout}s)...", flush=True)
    search_start = time.time()
    m3u8_urls = []
    while time.time() - search_start < search_timeout:
        found = _scan_m3u8(sb)
        if found:
            m3u8_urls = found
            print(f"      ✨ {len(found)} مرشح بعد {time.time()-search_start:.0f}s", flush=True)
            break
        _trigger_play(sb)
        sb.cdp.sleep(1.5)

    if not m3u8_urls:
        print(f"      ❌ لا m3u8", flush=True)
        return ("no_m3u8", None)

    for u in m3u8_urls[:3]:
        print(f"         · {u[:100]}", flush=True)

    cookies = _get_cookies(sb)
    ref = sb.cdp.get_current_url() or iframe_url

    # cffi
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
                return ("success", res[0])

    # yt-dlp
    for m3u8 in m3u8_urls[:2]:
        if _ytdlp(m3u8, str(out_path), ref, cookies):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return ("success", os.path.getsize(out_path))

    print(f"      ❌ فشل التحميل", flush=True)
    return ("fail", None)


# ═══════════════════════════════════════════════════════════════
# ★★★ u3seq — مع اختبار سيرفرات فردي
# ═══════════════════════════════════════════════════════════════
def _process_u3seq(sb, url, out_path):
    if "?do=watch" not in url:
        cur = url
        if "?" in cur: cur = cur.split("?")[0]
        if not cur.endswith("/"): cur += "/"
        watch_url = cur + "?do=watch"
    else:
        watch_url = url

    print(f"🖥️  فتح: {watch_url[:90]}", flush=True)
    _fast_open(sb, watch_url, wait=2.5)

    # انتظار serversList
    servers = []
    for i in range(10):
        sb.cdp.sleep(0.8)
        servers = _eval(sb, """
            (function(){
                var l=document.querySelector('.serversList');
                if(!l)return [];
                return Array.from(l.querySelectorAll('li')).map(li=>({
                    id:li.id||'',
                    name:(li.textContent||'').trim(),
                    onclick:li.getAttribute('onclick')||''
                }));
            })();
        """, []) or []
        if servers:
            print(f"   ✅ {len(servers)} سيرفر", flush=True)
            break

    if not servers:
        print(f"   ⚠️ لا سيرفرات", flush=True)
        return None

    # ★ جمع iframes لكل سيرفر بالتتابع (بدون نقر!)
    iframe_map = {}
    for i, srv in enumerate(servers):
        sid = srv.get("id")
        if not sid: continue
        print(f"   📋 [{i+1}/{len(servers)}] {srv.get('name') or sid}", flush=True)

        # احفظ iframe قبل
        before = _eval(sb, """
            (function(){var f=document.querySelector('.watch iframe');return f?f.src:null;})();
        """)

        # نفّذ onclick يدوياً
        onclick = srv.get("onclick", "")
        if onclick:
            m = re.search(r'getServer2\([^,]+,\s*(\d+)\s*,\s*(\d+)\s*\)', onclick)
            if m:
                vid, sid_num = m.group(1), m.group(2)
                _eval(sb, f"try{{getServer2(null,{vid},{sid_num})}}catch(e){{}}")
                sb.cdp.sleep(1.5)

        after = _eval(sb, """
            (function(){var f=document.querySelector('.watch iframe');return f?f.src:null;})();
        """)

        if after and after != before:
            after = after.replace("&amp;", "&")
            if after not in iframe_map.values():
                iframe_map[srv.get("name") or sid] = after
                print(f"      ✅ {after[:80]}", flush=True)

    if not iframe_map:
        print(f"   ⚠️ لا iframes", flush=True)
        return None

    print(f"\n   📊 {len(iframe_map)} سيرفر جاهز للاختبار:", flush=True)
    for name in iframe_map: print(f"      • {name}", flush=True)

    # ★ اختبر كل سيرفر بمفرده
    for sname, iframe_url in iframe_map.items():
        status, size = _try_one_server(sb, sname, iframe_url, out_path,
                                        search_timeout=25)
        if status == "success":
            print(f"   🏆 نجح عبر: {sname}", flush=True)
            return (size, True)
        elif status == "no_m3u8":
            print(f"   ⏭️ {sname}: لا m3u8 — السيرفر التالي", flush=True)
        else:
            print(f"   ⏭️ {sname}: فشل — السيرفر التالي", flush=True)

    print(f"\n   ❌ فشل كل السيرفرات ({len(iframe_map)})", flush=True)
    return None


# ═══════════════════════════════════════════════════════════════
# yam / shhaiid4u — معالج عام
# ═══════════════════════════════════════════════════════════════
def _process_url(sb, url, out_path):
    print(f"🖥️  فتح: {url[:90]}", flush=True)
    _fast_open(sb, url, wait=2.0)

    # ★ تتبع iframes (مستويين)
    visited = []
    for level in range(3):
        # نقرات في الصفحة الحالية
        for _ in range(3):
            _trigger_play(sb)
            sb.cdp.sleep(1.0)
            if _scan_m3u8(sb): break

        ifr = _get_nested_iframe(sb, exclude=visited)
        if not ifr: break
        print(f"      🔄 [L{level+1}] iframe: {ifr[:80]}", flush=True)
        visited.append(ifr)
        _fast_open(sb, ifr, wait=2.5)

    # بحث m3u8
    print(f"   🎬 بحث m3u8 ({M3U8_SEARCH_TIMEOUT}s)...", flush=True)
    search_start = time.time()
    m3u8_urls = []
    while time.time() - search_start < M3U8_SEARCH_TIMEOUT:
        found = _scan_m3u8(sb)
        if found: m3u8_urls = found; break
        _trigger_play(sb)
        sb.cdp.sleep(1.5)

    if not m3u8_urls:
        print(f"   ⚠️ لا m3u8 — yt-dlp مباشرة على الرابط", flush=True)
        cookies = _get_cookies(sb)
        ref = visited[-1] if visited else url
        if _ytdlp(url, str(out_path), ref, cookies):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return (os.path.getsize(out_path), True)
        return None

    print(f"   🎯 {len(m3u8_urls)} مرشح", flush=True)
    for u in m3u8_urls[:3]: print(f"      · {u[:100]}", flush=True)

    cookies = _get_cookies(sb)
    ref = visited[-1] if visited else url

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
                try: sb.driver.set_page_load_timeout(20)
                except Exception: pass

                # ★ JS interceptor فقط (بدون add_handler!)
                _install_interceptor(sb)

                u_low = url.lower()
                if "modablaj-" in u_low or "/video/" in u_low:
                    print(f"   🎯 u3seq → اختبار السيرفرات فردياً", flush=True)
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
# الضغط + thumbnail
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
                                timeout=300, default=None)
    except TimeoutError_:
        print(f"    ⏰ تجاوز 300s", flush=True)
        res = None
    except Exception as e:
        print(f"    ⚠️ {str(e)[:150]}", flush=True)
        res = None
    finally:
        for p in ["yt-dlp", "ffmpeg"]:
            try:
                subprocess.run(["pkill", "-9", "-f", p], capture_output=True, timeout=3)
            except Exception: pass

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