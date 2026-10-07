"""
downloader.py v30 — NO-FREEZE
• لا add_handler • لا threads مع CDP • JS interceptor فقط
"""
import base64, json, os, re, shutil, subprocess, tempfile, time
from pathlib import Path
from urllib.parse import urlparse, urljoin

from curl_cffi import requests as cffi_requests
from config import config, MEDIA_DIR

MIN_SIZE = 100 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _safe(name): return re.sub(r'[\\/:*?"<>|]', "_", name).strip()[:120]
def _origin(url):
    try: return f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    except: return ""


def _ffmpeg():
    try:
        if subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5).returncode == 0:
            return "ffmpeg"
    except: pass
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except: pass
    return "ffmpeg"


def _is_valid_url(url):
    if not url: return False
    u = url.lower()
    bad = ["/moslslat.php", "/topvideos.php", "/all-series.php", "?cat=", "/category/"]
    for b in bad:
        if b in u: return False
    if "modablaj-" in u or "/video/" in u: return True
    if "see.php" in u and "vid=" in u: return True
    if "watch.php" in u and "vid=" in u: return True
    if "/watch/" in u and ".php" not in u: return True
    return False


# ═══════════ JS interceptor ═══════════
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
        window.fetch=function(i,init){try{rec((typeof i==='string')?i:(i&&i.url));}catch(e){}return o.apply(this,arguments);};
        window.__fp=true;
    }
    if(window.XMLHttpRequest && !window.__xp){
        var xo=XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open=function(m,u){try{rec(u);}catch(e){}return xo.apply(this,arguments);};
        window.__xp=true;
    }
    try{
        var d=Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype,'src');
        if(d&&d.set&&!window.__vp){
            Object.defineProperty(HTMLMediaElement.prototype,'src',{set:function(v){try{rec(v);}catch(e){}return d.set.call(this,v);},get:d.get,configurable:true});
            window.__vp=true;
        }
    }catch(e){}
    window.__patch=function(){
        try{
            if(typeof Hls!=='undefined' && !window.__hp && Hls.prototype && Hls.prototype.loadSource){
                var o=Hls.prototype.loadSource;
                Hls.prototype.loadSource=function(u){try{rec(u);}catch(e){}return o.apply(this,arguments);};
                window.__hp=true;
            }
            if(typeof jwplayer!=='undefined' && !window.__jp){
                var oj=jwplayer;
                window.jwplayer=function(){
                    var p=oj.apply(this,arguments);
                    if(p){
                        if(p.setup){var s=p.setup;p.setup=function(c){try{if(c&&c.file)rec(c.file);if(c&&c.sources)c.sources.forEach(function(x){if(x.file)rec(x.file);});}catch(e){}return s.apply(this,arguments);};}
                        if(p.load){var l=p.load;p.load=function(pl){try{if(pl){var a=Array.isArray(pl)?pl:[pl];a.forEach(function(i){if(i.file)rec(i.file);if(i.sources)i.sources.forEach(function(s){if(s.file)rec(s.file);});});}}catch(e){}return l.apply(this,arguments);};}
                    }
                    return p;
                };
                Object.assign(window.jwplayer,oj);
                window.__jp=true;
            }
        }catch(e){}
    };
    setInterval(window.__patch,500);
})();
"""


def _install_js(sb):
    try:
        sb.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _JS})
    except: pass


def _eval(sb, js, default=None):
    """execute_script مباشر — NO thread."""
    try: return sb.cdp.execute_script(js)
    except: return default


def _cdp(sb, cmd, params=None):
    if params is None: params = {}
    try: return sb.driver.execute_cdp_cmd(cmd, params)
    except: return None


def _cookies(sb):
    try:
        r = _cdp(sb, "Network.getAllCookies", {})
        if r and r.get("cookies"):
            return {c["name"]: c["value"] for c in r["cookies"] if c.get("name") and c.get("value")}
    except: pass
    return {}


def _open(sb, url, wait=1.5):
    """فتح سريع — لا ينتظر DOMContentLoaded."""
    _cdp(sb, "Page.navigate", {"url": url})
    sb.cdp.sleep(wait)
    _cdp(sb, "Runtime.evaluate", {"expression": "try{window.stop()}catch(e){}"})


def _scan(sb):
    """يجمع كل m3u8 من JS + performance + HTML."""
    found = set()
    # JS interceptor
    try:
        urls = _eval(sb, "return window.__m3u8||[]", [])
        if isinstance(urls, list):
            for u in urls:
                if isinstance(u, str) and (".m3u8" in u or ".mpd" in u): found.add(u)
    except: pass
    # performance
    try:
        perf = _eval(sb, "try{return performance.getEntriesByType('resource').map(e=>e.name)}catch(e){return[]}", [])
        if isinstance(perf, list):
            for u in perf:
                if ".m3u8" in u or ".mpd" in u: found.add(u)
    except: pass
    # HTML
    try:
        html = sb.cdp.get_page_source() or ""
        for m in re.finditer(r'(https?:[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*)', html):
            found.add(m.group(1).replace("\\/", "/"))
    except: pass
    # APIs
    try:
        apis = _eval(sb, """
            (function(){var o=[];try{
                if(typeof Hls!=='undefined'&&Hls.instances)Hls.instances.forEach(function(h){try{if(h.url)o.push(h.url);}catch(e){}});
                if(window.hls&&window.hls.url)o.push(window.hls.url);
                if(typeof jwplayer!=='undefined'){try{var p=jwplayer();if(p&&p.getPlaylist)p.getPlaylist().forEach(function(i){if(i.file)o.push(i.file);if(i.sources)i.sources.forEach(function(s){if(s.file)o.push(s.file);});});}catch(e){}}
                if(typeof videojs!=='undefined'){try{var p2=videojs.getPlayers();for(var k in p2){try{var s=p2[k].currentSrc&&p2[k].currentSrc();if(s)o.push(s);}catch(e){}}}catch(e){}}
                var v=document.querySelector('video');if(v){if(v.currentSrc)o.push(v.currentSrc);if(v.src)o.push(v.src);}
                document.querySelectorAll('source').forEach(function(s){if(s.src)o.push(s.src);});
            }catch(e){}return o;})();
        """, [])
        if isinstance(apis, list):
            for u in apis:
                if isinstance(u, str) and (".m3u8" in u or ".mpd" in u): found.add(u)
    except: pass
    urls = [u for u in found if "ping.gif" not in u and "jwpltx" not in u]
    return urls


def _play(sb):
    _eval(sb, """
        (function(){try{
            var v=document.querySelector('video');
            if(v){v.muted=true;if(v.play)v.play().catch(function(){});}
            if(typeof jwplayer!=='undefined'){var p=jwplayer();if(p){if(p.play)p.play(true);if(p.setMute)p.setMute(true);}}
            if(typeof videojs!=='undefined'){var p2=videojs.getPlayers();for(var k in p2){try{p2[k].play();p2[k].muted(true);}catch(e){}}}
            ['video','button','.vjs-big-play-button','.jw-icon-playback','[class*=play]'].forEach(function(s){
                document.querySelectorAll(s).forEach(function(el){try{el.click();}catch(e){}});
            });
        }catch(e){}})();
    """)
    try:
        r = _eval(sb, """
            (function(){var v=document.querySelector('video');if(!v)return null;var b=v.getBoundingClientRect();if(b.width<50)return null;return{x:Math.round(b.left+b.width/2),y:Math.round(b.top+b.height/2)};})();
        """)
        if r and r.get("x", 0) > 0:
            for _ in range(2):
                sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": r['x'], "y": r['y'], "button": "left", "clickCount": 1})
                sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": r['x'], "y": r['y'], "button": "left", "clickCount": 1})
                sb.cdp.sleep(0.4)
    except: pass


# ═══════════ cffi segments ═══════════
def _parse_m3u8(text, base):
    segs, vars_ = [], []
    for line in text.splitlines():
        l = line.strip()
        if not l or l.startswith("#"): continue
        full = l if l.startswith("http") else urljoin(base + "/", l)
        if ".m3u8" in l.lower(): vars_.append(full)
        else: segs.append(full)
    return segs, vars_


def _resolve_m3u8(url, headers, depth=0):
    if depth > 3: return None, []
    try:
        r = cffi_requests.get(url, headers=headers, impersonate="chrome124", timeout=15, verify=False)
        if r.status_code != 200: return None, []
    except: return None, []
    base = url.rsplit("/", 1)[0]
    segs, vars_ = _parse_m3u8(r.text, base)
    if segs: return url, segs
    for v in vars_[:2]:
        f, s = _resolve_m3u8(v, headers, depth + 1)
        if s: return f, s
    return None, []


def _cffi_download(segments, out_path, referer, ck_dict):
    headers = {"Referer": referer or "", "Origin": _origin(referer), "User-Agent": UA, "Accept": "*/*"}
    if ck_dict:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in ck_dict.items())[:8000]
    # اختبار
    try:
        r = cffi_requests.get(segments[0], headers=headers, impersonate="chrome124", timeout=10, verify=False)
        if r.status_code != 200 or len(r.content) < 100: return None
    except: return None

    print(f"      ⚡ {len(segments)} segments...", flush=True)
    seg_dir = tempfile.mkdtemp(prefix="hls_")
    seg_paths, failed, total = {}, 0, 0

    from concurrent.futures import ThreadPoolExecutor, as_completed
    def _dl(args):
        idx, u = args
        for imp in ["chrome124", "chrome120"]:
            try:
                r = cffi_requests.get(u, headers=headers, impersonate=imp, timeout=20, verify=False)
                if r.status_code == 200 and len(r.content) > 100:
                    p = os.path.join(seg_dir, f"seg_{idx:06d}.ts")
                    with open(p, "wb") as f: f.write(r.content)
                    return (idx, p, len(r.content))
            except: continue
        return (idx, None, 0)

    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(_dl, (i, s)) for i, s in enumerate(segments)]
        done = 0
        for f in as_completed(futs):
            idx, p, sz = f.result(); done += 1
            if p: seg_paths[idx] = p; total += sz
            else: failed += 1
            if done % 40 == 0 or done == len(segments):
                print(f"      📦 {done}/{len(segments)} | {total/1048576:.1f}MB", flush=True)

    if not seg_paths or failed > len(segments) * 0.2:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    sorted_segs = [seg_paths[k] for k in sorted(seg_paths)]
    concat = os.path.join(seg_dir, "concat.txt")
    with open(concat, "w") as f:
        for p in sorted_segs: f.write(f"file '{p}'\n")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-f", "concat", "-safe", "0",
           "-i", concat, "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart",
           "-f", "mp4", "-y", out_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        ok = r.returncode == 0 and os.path.exists(out_path)
    except: ok = False
    shutil.rmtree(seg_dir, ignore_errors=True)
    if ok:
        print(f"      ✅ {os.path.getsize(out_path)/1048576:.1f}MB", flush=True)
        return (os.path.getsize(out_path), True)
    return None


def _ytdlp(url, out_path, referer, ck_dict):
    print(f"      [yt-dlp]", flush=True)
    ck = "; ".join(f"{k}={v}" for k, v in ck_dict.items())[:8000]
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "--no-part", "--retries", "5",
           "--fragment-retries", "10", "--socket-timeout", "30", "--concurrent-fragments", "16",
           "--no-check-certificate", "--hls-use-mpegts", "--hls-prefer-native",
           "--impersonate", "chrome", "--user-agent", UA, "--referer", referer,
           "-o", out_path, url]
    if ck: cmd += ["--add-header", f"Cookie:{ck}"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: return False
    start = time.time()
    while proc.poll() is None:
        time.sleep(2)
        if time.time() - start > 600:
            try: proc.kill()
            except: pass
            break
    return os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE


# ═══════════ اختبار سيرفر واحد ═══════════
def _try_server(sb, name, iframe_url, out_path):
    """يحاول سيرفر واحد. يعيد: ('ok', size) | ('no_m3u8', None) | ('fail', None)."""
    print(f"\n   ═══ {name} ═══", flush=True)
    print(f"      🔗 {iframe_url[:90]}", flush=True)

    try:
        _open(sb, iframe_url, wait=2.5)
    except Exception as e:
        print(f"      ❌ {str(e)[:80]}", flush=True)
        return ("fail", None)

    # iframe متداخل
    nested = _eval(sb, """
        (function(){
            var fs=document.querySelectorAll('iframe');
            for(var i=0;i<fs.length;i++){
                var s=fs[i].src||'';
                if(s.startsWith('http')&&s.indexOf('google')===-1&&s.indexOf('facebook')===-1)return s;
            }
            return null;
        })();
    """)
    if nested and nested != iframe_url:
        print(f"      🔄 متداخل: {nested[:70]}", flush=True)
        _open(sb, nested, wait=2)

    # نقرات
    for i in range(4):
        _play(sb)
        sb.cdp.sleep(1)
        if _scan(sb): break

    # بحث m3u8
    print(f"      🔍 بحث m3u8 ({config.M3U8_SEARCH_TIMEOUT}s)...", flush=True)
    start = time.time()
    m3u8s = []
    while time.time() - start < config.M3U8_SEARCH_TIMEOUT:
        found = _scan(sb)
        if found:
            m3u8s = found
            print(f"      ✨ {len(found)} بعد {time.time()-start:.0f}s", flush=True)
            break
        _play(sb)
        sb.cdp.sleep(1.5)

    if not m3u8s:
        print(f"      ❌ لا m3u8", flush=True)
        return ("no_m3u8", None)

    for u in m3u8s[:2]: print(f"         · {u[:100]}", flush=True)
    ck = _cookies(sb)
    ref = sb.cdp.get_current_url() or iframe_url

    # cffi
    for m in m3u8s[:3]:
        headers = {"Referer": ref, "Origin": _origin(ref), "User-Agent": UA, "Accept": "*/*"}
        if ck: headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in ck.items())[:8000]
        _, segs = _resolve_m3u8(m, headers)
        if segs:
            res = _cffi_download(segs, str(out_path), ref, ck)
            if res and os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return ("ok", res[0])

    # yt-dlp
    for m in m3u8s[:2]:
        if _ytdlp(m, str(out_path), ref, ck):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return ("ok", os.path.getsize(out_path))

    return ("fail", None)


# ═══════════ u3seq ═══════════
def _u3seq(sb, url, out_path):
    if "?do=watch" not in url:
        cur = url.split("?")[0]
        if not cur.endswith("/"): cur += "/"
        watch = cur + "?do=watch"
    else:
        watch = url

    print(f"🖥️  فتح: {watch[:90]}", flush=True)
    _open(sb, watch, wait=2.5)

    servers = []
    for i in range(10):
        sb.cdp.sleep(0.7)
        servers = _eval(sb, """
            (function(){
                var l=document.querySelector('.serversList');
                if(!l)return [];
                return Array.from(l.querySelectorAll('li')).map(function(li){
                    return {id:li.id||'',name:(li.textContent||'').trim(),onclick:li.getAttribute('onclick')||''};
                });
            })();
        """, []) or []
        if servers: break

    if not servers:
        print(f"   ⚠️ لا سيرفرات → معالج عام", flush=True)
        return _generic(sb, url, out_path)

    print(f"   ✅ {len(servers)} سيرفر", flush=True)

    # جمع iframes
    iframes = {}
    for srv in servers:
        sid = srv.get("id")
        if not sid: continue
        before = _eval(sb, "try{var f=document.querySelector('.watch iframe');return f?f.src:null}catch(e){return null}")
        onclick = srv.get("onclick", "")
        m = re.search(r'getServer2\([^,]+,\s*(\d+)\s*,\s*(\d+)\s*\)', onclick)
        if m:
            _eval(sb, f"try{{getServer2(null,{m.group(1)},{m.group(2)})}}catch(e){{}}")
            sb.cdp.sleep(1.5)
        after = _eval(sb, "try{var f=document.querySelector('.watch iframe');return f?f.src:null}catch(e){return null}")
        if after and after != before:
            after = after.replace("&amp;", "&")
            if after not in iframes.values():
                nm = srv.get("name") or sid
                iframes[nm] = after

    if not iframes:
        print(f"   ⚠️ لا iframes → معالج عام", flush=True)
        return _generic(sb, url, out_path)

    print(f"   📊 {len(iframes)} سيرفر:", flush=True)
    for k in iframes: print(f"      • {k}", flush=True)

    # اختبار كل سيرفر بمفرده
    for name, ifr in iframes.items():
        status, size = _try_server(sb, name, ifr, out_path)
        if status == "ok":
            print(f"   🏆 نجح: {name}", flush=True)
            return (size, True)
        elif status == "no_m3u8":
            print(f"   ⏭️  {name}: لا m3u8", flush=True)
        else:
            print(f"   ⏭️  {name}: فشل", flush=True)

    print(f"   ❌ فشل كل السيرفرات", flush=True)
    return None


# ═══════════ معالج عام (yam) ═══════════
def _generic(sb, url, out_path):
    print(f"🖥️  فتح: {url[:90]}", flush=True)
    _open(sb, url, wait=2)

    for lvl in range(3):
        for _ in range(3):
            _play(sb)
            sb.cdp.sleep(1)
            if _scan(sb): break
        nested = _eval(sb, """
            (function(){
                var fs=document.querySelectorAll('iframe');
                for(var i=0;i<fs.length;i++){
                    var s=fs[i].src||'';
                    if(s.startsWith('http')&&s.indexOf('google')===-1&&s.indexOf('facebook')===-1)return s;
                }
                return null;
            })();
        """)
        if not nested: break
        print(f"      🔄 [L{lvl+1}] {nested[:70]}", flush=True)
        _open(sb, nested, wait=2)

    print(f"   🎬 بحث m3u8 ({config.M3U8_SEARCH_TIMEOUT}s)...", flush=True)
    start = time.time()
    m3u8s = []
    while time.time() - start < config.M3U8_SEARCH_TIMEOUT:
        found = _scan(sb)
        if found: m3u8s = found; break
        _play(sb)
        sb.cdp.sleep(1.5)

    if not m3u8s:
        print(f"   ❌ لا m3u8", flush=True)
        return None

    for u in m3u8s[:2]: print(f"      · {u[:100]}", flush=True)
    ck = _cookies(sb)
    ref = sb.cdp.get_current_url() or url

    for m in m3u8s[:3]:
        headers = {"Referer": ref, "Origin": _origin(ref), "User-Agent": UA, "Accept": "*/*"}
        if ck: headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in ck.items())[:8000]
        _, segs = _resolve_m3u8(m, headers)
        if segs:
            res = _cffi_download(segs, str(out_path), ref, ck)
            if res and os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return res

    for m in m3u8s[:2]:
        if _ytdlp(m, str(out_path), ref, ck):
            if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
                return (os.path.getsize(out_path), True)
    return None


# ═══════════ نقطة الدخول ═══════════
def _run_browser(url, out_path):
    from seleniumbase import SB
    try:
        with SB(uc=True, xvfb=True, headless=False, incognito=True,
                ad_block_on=True, disable_csp=True,
                page_load_strategy="eager", locale_code="en") as sb:
            try:
                sb.activate_cdp_mode()
                try: sb.driver.set_page_load_timeout(15)
                except: pass
                _install_js(sb)

                if "modablaj-" in url.lower() or "/video/" in url.lower():
                    print(f"   🎯 u3seq", flush=True)
                    return _u3seq(sb, url, out_path)
                else:
                    print(f"   🎯 generic", flush=True)
                    return _generic(sb, url, out_path)
            except Exception as e:
                import traceback
                print(f"   ❌ {str(e)[:200]}", flush=True)
                traceback.print_exc()
    except Exception as e:
        import traceback
        print(f"   ❌ {str(e)[:200]}", flush=True)
        traceback.print_exc()
    return None


# ═══════════ الضغط ═══════════
def _compress(inp, out):
    im = Path(inp).stat().st_size / 1048576
    print(f"   🗜️  {im:.2f}MB → {config.COMPRESS_SCALE}p", flush=True)
    cmd = [_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error",
           "-i", str(inp), "-vf", f"scale=-2:{config.COMPRESS_SCALE}",
           "-c:v", "libx264", "-preset", config.COMPRESS_PRESET, "-crf", str(config.COMPRESS_CRF),
           "-profile:v", "main", "-level", "3.1", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", config.COMPRESS_AUDIO_BITRATE, "-ac", "2", "-ar", "44100",
           "-movflags", "+faststart", "-threads", "2", "-y", str(out)]
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
        cmd = [_ffmpeg(), "-err_detect", "ignore_err", "-ss", ss, "-i", str(v),
               "-vframes", "1", "-vf", "scale=320:180", "-f", "image2", "-y", str(o)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if r.returncode == 0 and os.path.exists(o) and os.path.getsize(o) > 1024: return True
        except: pass
    return False


# ═══════════ الواجهة العامة ═══════════
def download_episode(series_name, episode_num, url, media_type="series", item_name=None):
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
    import threading
    def _worker():
        try: result[0] = _run_browser(url, raw)
        except Exception as e: exc[0] = e
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=config.EPISODE_TIMEOUT)
    if t.is_alive():
        print(f"    ⏰ تجاوز {config.EPISODE_TIMEOUT}s", flush=True)
        for p in ["yt-dlp", "chrome", "ffmpeg"]:
            try: subprocess.run(["pkill", "-9", "-f", p], capture_output=True, timeout=3)
            except: pass
    if exc[0]:
        print(f"    ⚠️ {str(exc[0])[:150]}", flush=True)
        result[0] = None

    if not result[0] or not raw.exists():
        print(f"    ⚠️ فشل", flush=True)
        try:
            if raw.exists(): raw.unlink()
        except: pass
        return None

    print(f"    📦 {raw.stat().st_size/1048576:.1f}MB", flush=True)

    if config.SKIP_COMPRESS:
        shutil.move(str(raw), str(final))
    else:
        if not _compress(raw, final):
            shutil.move(str(raw), str(final))
    if raw.exists(): raw.unlink()
    if not final.exists(): return None

    try: _thumb(final, out_dir / f"{prefix}.jpg")
    except: pass

    print(f"    ✅ {final.name} ({final.stat().st_size/1048576:.1f}MB)", flush=True)
    return final