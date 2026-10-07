"""
downloader.py — حقن JavaScript لاعتراض m3u8 + ffmpeg HLS
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

MIN_SIZE = 100 * 1024
M3U8_SEARCH_TIMEOUT = int(os.environ.get("M3U8_SEARCH_TIMEOUT", "60"))
YTDLP_TIMEOUT = int(os.environ.get("YTDLP_TIMEOUT", "120"))
FFMPEG_HLS_TIMEOUT = int(os.environ.get("FFMPEG_HLS_TIMEOUT", "1800"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class TimeoutError_(Exception):
    pass


def run_with_timeout(func, args=(), kwargs=None, timeout=60, default=None):
    if kwargs is None: kwargs = {}
    result = [default]
    exception = [None]
    done = threading.Event()

    def _run():
        try: result[0] = func(*args, **kwargs)
        except Exception as e: exception[0] = e
        finally: done.set()

    threading.Thread(target=_run, daemon=True).start()
    if not done.wait(timeout=timeout):
        raise TimeoutError_(f"timeout {timeout}s")
    if exception[0]: raise exception[0]
    return result[0]


def _safe(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()[:120]


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
# ★★★ حقن JavaScript لاعتراض m3u8 قبل تحميل الصفحة
# ═══════════════════════════════════════════════════════════════
_INTERCEPTOR_JS = r"""
(function() {
    // احفظ كل m3u8/mpd يتم طلبه
    window.__captured_m3u8 = window.__captured_m3u8 || [];

    function record(url) {
        try {
            if (typeof url === 'string' &&
                (url.indexOf('.m3u8') !== -1 || url.indexOf('.mpd') !== -1)) {
                if (window.__captured_m3u8.indexOf(url) === -1) {
                    window.__captured_m3u8.push(url);
                }
            }
        } catch(e){}
    }

    // ─── اعتراض fetch ───
    if (window.fetch && !window.__fetch_patched) {
        var origFetch = window.fetch;
        window.fetch = function(input, init) {
            try {
                var url = (typeof input === 'string') ? input : (input && input.url);
                record(url);
            } catch(e){}
            return origFetch.apply(this, arguments);
        };
        window.__fetch_patched = true;
    }

    // ─── اعتراض XHR ───
    if (window.XMLHttpRequest && !window.__xhr_patched) {
        var origOpen = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(method, url) {
            try { record(url); } catch(e){}
            return origOpen.apply(this, arguments);
        };
        window.__xhr_patched = true;
    }

    // ─── اعتراض video.src / source.src (المشغلات) ───
    try {
        var desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
        if (desc && desc.set && !window.__video_patched) {
            Object.defineProperty(HTMLMediaElement.prototype, 'src', {
                set: function(v) {
                    try { record(v); } catch(e){}
                    return desc.set.call(this, v);
                },
                get: desc.get,
                configurable: true
            });
            window.__video_patched = true;
        }
    } catch(e){}

    // ─── اعتراض jwplayer ───
    window.__patch_jw = function() {
        try {
            if (typeof jwplayer !== 'undefined' && !window.__jw_patched) {
                var orig = jwplayer;
                window.jwplayer = function() {
                    var player = orig.apply(this, arguments);
                    if (player) {
                        var origSetup = player.setup;
                        if (origSetup) {
                            player.setup = function(cfg) {
                                try {
                                    if (cfg && cfg.file) record(cfg.file);
                                    if (cfg && cfg.sources) {
                                        cfg.sources.forEach(function(s) {
                                            if (s.file) record(s.file);
                                        });
                                    }
                                } catch(e){}
                                return origSetup.apply(this, arguments);
                            };
                        }
                        if (player.load) {
                            var origLoad = player.load;
                            player.load = function(playlist) {
                                try {
                                    if (playlist) {
                                        var items = Array.isArray(playlist) ? playlist : [playlist];
                                        items.forEach(function(item) {
                                            if (item.file) record(item.file);
                                            if (item.sources) item.sources.forEach(function(s){
                                                if (s.file) record(s.file);
                                            });
                                        });
                                    }
                                } catch(e){}
                                return origLoad.apply(this, arguments);
                            };
                        }
                    }
                    return player;
                };
                Object.assign(window.jwplayer, orig);
                window.__jw_patched = true;
            }
        } catch(e){}
    };

    // ─── اعتراض videojs ───
    window.__patch_vjs = function() {
        try {
            if (typeof videojs !== 'undefined' && !window.__vjs_patched) {
                var origReg = videojs.registerComponent;
                if (origReg) {
                    videojs.registerComponent = function(name, comp) {
                        if (comp && comp.prototype && !comp.prototype.__vjs_patched) {
                            var origSrc = comp.prototype.src;
                            if (origSrc) {
                                comp.prototype.src = function(source) {
                                    try {
                                        if (typeof source === 'string') record(source);
                                        if (source && source.src) record(source.src);
                                    } catch(e){}
                                    return origSrc.apply(this, arguments);
                                };
                            }
                            comp.prototype.__vjs_patched = true;
                        }
                        return origReg.apply(this, arguments);
                    };
                }
                window.__vjs_patched = true;
            }
        } catch(e){}
    };

    // جرب بشكل متكرر (المشغل قد يُحمل بعد هذا)
    setInterval(function() {
        window.__patch_jw();
        window.__patch_vjs();
    }, 500);

})();
"""


def _install_interceptor(sb):
    """★ حقن JS في كل صفحة جديدة قبل تحميلها."""
    try:
        # CDP: أضف السكريبت لكل صفحة جديدة
        sb.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": _INTERCEPTOR_JS,
        })
        print("      💉 تم حقن m3u8 interceptor")
    except Exception as e:
        print(f"      ⚠️ فشل الحقن: {str(e)[:100]}")


def _read_captured_m3u8(sb):
    """يقرأ m3u8 من window.__captured_m3u8."""
    try:
        urls = sb.cdp.execute_script("return window.__captured_m3u8 || [];")
        if isinstance(urls, list):
            return [u for u in urls if isinstance(u, str) and
                    (".m3u8" in u or ".mpd" in u)]
    except Exception:
        pass
    return []


# ═══════════════════════════════════════════════════════════════
# كشف m3u8 من مصادر متعددة
# ═══════════════════════════════════════════════════════════════
def _scan_for_m3u8(sb, netlog_read):
    found = set()

    # ★ 1) من الـ interceptor (الأقوى)
    try:
        for u in _read_captured_m3u8(sb):
            found.add(u)
    except Exception:
        pass

    # 2) Network log
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
        for m in re.finditer(r'(https?:[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*)', html):
            found.add(m.group(1).replace("\\/", "/"))
        for m in re.finditer(r'(https?:[^\s"\'<>\\]+\.mpd[^\s"\'<>\\]*)', html):
            found.add(m.group(1).replace("\\/", "/"))
    except Exception:
        pass

    # 5) JS globals
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
                        try {
                            var p2 = videojs.getPlayers();
                            for (var k in p2) {
                                try {
                                    var src = p2[k].currentSrc && p2[k].currentSrc();
                                    if (src) found.push(src);
                                    var t = p2[k].tech && p2[k].tech(true);
                                    if (t && t.hls && t.hls.url) found.push(t.hls.url);
                                } catch(e){}
                            }
                        } catch(e){}
                    }
                    ['sources', 'playlist', 'file', 'video_url', 'hls_url'].forEach(function(g) {
                        var v = window[g];
                        if (typeof v === 'string' && v.indexOf('.m3u8') !== -1) found.push(v);
                    });
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

    # 6) video.currentSrc
    try:
        video_src = sb.cdp.execute_script("""
            (function(){
                try {
                    var res = [];
                    var v = document.querySelector('video');
                    if (v && v.currentSrc && v.currentSrc.indexOf('.m3u8') !== -1)
                        res.push(v.currentSrc);
                    if (v && v.src && v.src.indexOf('.m3u8') !== -1)
                        res.push(v.src);
                    return res;
                } catch(e){ return []; }
            })();
        """)
        if isinstance(video_src, list):
            for u in video_src:
                if isinstance(u, str) and ".m3u8" in u:
                    found.add(u)
    except Exception:
        pass

    urls = list(found)
    urls = [u for u in urls if "ping.gif" not in u and "jwpltx" not in u]

    idx = [u for u in urls if "index" in u.lower()]
    mst = [u for u in urls if "master" in u.lower()]
    mpd = [u for u in urls if ".mpd" in u.lower()]
    oth = [u for u in urls if u not in idx and u not in mst and u not in mpd]

    return idx + mst + mpd + oth


# ═══════════════════════════════════════════════════════════════
# ffmpeg HLS مباشرة
# ═══════════════════════════════════════════════════════════════
def _ffmpeg_hls(m3u8_url, out, iframe_url, cookies=None):
    cookies = cookies or {}
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    origin = _origin(iframe_url)

    headers_parts = [
        f"Referer: {iframe_url}",
        f"Origin: {origin}",
        f"User-Agent: {UA}",
        "Accept: */*",
        "Accept-Language: ar,en;q=0.9",
    ]
    if cookie_str:
        headers_parts.append(f"Cookie: {cookie_str}")
    headers = "\r\n".join(headers_parts) + "\r\n"

    ff = _get_ffmpeg_exe()

    strategies = [
        (["-c", "copy", "-bsf:a", "aac_adtstoasc",
          "-movflags", "+faststart"], "aac-ts"),
        (["-c", "copy", "-movflags", "+faststart"], "copy"),
        (["-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
          "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart"], "re"),
    ]

    for idx, (codec_args, name) in enumerate(strategies):
        if os.path.exists(out):
            try: os.remove(out)
            except Exception: pass

        cmd = [
            ff, "-nostdin", "-hide_banner", "-loglevel", "warning",
            "-threads", "1",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-headers", headers,
            "-user_agent", UA,
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", m3u8_url,
        ] + codec_args + ["-y", str(out)]

        try:
            print(f"         🎬 ffmpeg HLS [{name}]...")
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=FFMPEG_HLS_TIMEOUT)
            if (r.returncode == 0 and os.path.exists(out)
                    and os.path.getsize(out) > MIN_SIZE):
                mb = os.path.getsize(out) / 1048576
                print(f"         ✅ نجح: {mb:.1f}MB")
                return True
            err = (r.stderr or "").strip().split("\n")[-1] if r.stderr else "?"
            print(f"         ⚠️ [{name}]: {err[:120]}")
        except subprocess.TimeoutExpired:
            print(f"         ⏰ [{name}]: timeout")
            subprocess.run(["pkill", "-9", "-f", "ffmpeg"],
                           capture_output=True, timeout=5)
        except Exception as e:
            print(f"         ⚠️ [{name}]: {str(e)[:80]}")

    return False


# ═══════════════════════════════════════════════════════════════
# yt-dlp
# ═══════════════════════════════════════════════════════════════
def _ytdlp_worker(cmd, out):
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                bufsize=1, preexec_fn=os.setsid)
    except Exception as e:
        return False, f"spawn: {str(e)[:80]}"
    try:
        for line in iter(proc.stdout.readline, ""):
            if "PROGRESS:" in line:
                try:
                    pct = float(line.split("PROGRESS:", 1)[1].strip().split("|")[0]
                                .replace("%", "").strip())
                    if int(pct) % 25 == 0:
                        print(f"            📊 {pct:.0f}%")
                except Exception: pass
    except Exception: pass
    try: proc.wait(timeout=10)
    except Exception:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception: proc.kill()
    if (proc.returncode == 0 and os.path.exists(out)
            and os.path.getsize(out) > MIN_SIZE):
        return True, "ok"
    return False, f"code={proc.returncode}"


def _ytdlp_hls(m3u8_url, out, iframe_url, cookies=None):
    cookies = cookies or {}
    cookie_file = tempfile.mktemp(suffix=".txt")
    try:
        domain = urlparse(m3u8_url).hostname or ""
        with open(cookie_file, "w") as f:
            f.write("# Netscape HTTP Cookie File\n\n")
            for k, v in cookies.items():
                if k and v:
                    f.write(f".{domain}\tTRUE\t/\tFALSE\t0\t{k}\t{v}\n")

        origin_url = config.SOURCE_BASE_URL or "https://u.3seq.com"
        ff = _get_ffmpeg_exe()

        cmd = [
            "yt-dlp", "--no-warnings", "--no-playlist", "--no-part",
            "--newline", "--progress",
            "--progress-template", "download:PROGRESS:%(progress._percent_str)s",
            "--hls-prefer-native",
            "--user-agent", UA,
            "--referer", origin_url + "/",
            "--add-header", f"Origin:{origin_url}",
            "--cookies", cookie_file,
            "--retries", "3", "--fragment-retries", "3",
            "--socket-timeout", "30",
            "--concurrent-fragments", "16",
            "-f", "bv*[height<=360]+ba/b[height<=360]/bv*+ba/b",
            "--merge-output-format", "mp4",
            "-o", out, m3u8_url,
        ]
        if ff != "ffmpeg":
            cmd.extend(["--ffmpeg-location", ff])

        try:
            ok, _ = run_with_timeout(_ytdlp_worker, args=(cmd, out),
                                     timeout=YTDLP_TIMEOUT, default=(False, "timeout"))
            return ok
        except TimeoutError_:
            subprocess.run(["pkill", "-9", "-f", "yt-dlp"],
                           capture_output=True, timeout=5)
            return False
    finally:
        try: os.remove(cookie_file)
        except Exception: pass


# ═══════════════════════════════════════════════════════════════
# نقرات عدوانية
# ═══════════════════════════════════════════════════════════════
def _aggressive_play(sb):
    try:
        sb.cdp.execute_script("""
            (function(){try{
                ['iframe[id*="ad"]','div[class*="popup"]','[class*="overlay"]','.ads']
                    .forEach(function(sel){
                        document.querySelectorAll(sel).forEach(function(el){
                            el.style.display = 'none';
                        });
                    });
                document.body.style.overflow = 'auto';
            }catch(e){}})();
        """)
    except Exception: pass

    for sel in ["video", ".jw-icon-playback", ".jw-display-icon-container",
                ".vjs-big-play-button", "[class*='play']", "#play"]:
        for _ in range(2):
            try: sb.cdp.click_if_visible(sel)
            except Exception: pass

    try:
        rect = sb.cdp.execute_script("""
            (function(){
                var v = document.querySelector('video');
                if (!v) return null;
                var r = v.getBoundingClientRect();
                if (r.width < 50 || r.height < 50) return null;
                return {x: Math.round(r.left+r.width/2), y: Math.round(r.top+r.height/2)};
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
                if(v){ v.muted=true; if(v.play)v.play().catch(function(){}); }
            }catch(e){}})();
        """)
    except Exception: pass


def _get_cookies(sb):
    try:
        r = sb.driver.execute_cdp_cmd("Network.getAllCookies", {})
        if r and r.get("cookies"):
            return {c["name"]: c["value"] for c in r["cookies"]
                    if c.get("name") and c.get("value")}
    except Exception: pass
    return {}


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
    except Exception: pass
    return 0


def _compress_to_target(inp, out):
    max_size_mb = config.COMPRESS_MAX_SIZE_MB
    im = Path(inp).stat().st_size / 1048576
    print(f"   🗜️  {im:.2f}MB → {config.COMPRESS_SCALE}p (حد {max_size_mb}MB)...")

    ff = _get_ffmpeg_exe()
    duration = _get_duration(inp)

    cmd_crf = [
        ff, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(inp),
        "-vf", f"scale=-2:{config.COMPRESS_SCALE}",
        "-c:v", "libx264", "-preset", config.COMPRESS_PRESET,
        "-crf", str(config.COMPRESS_CRF),
        "-profile:v", "main", "-level", "3.1", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", config.COMPRESS_AUDIO_BITRATE,
        "-ac", "2", "-ar", "44100", "-movflags", "+faststart",
        "-threads", "2", "-y", str(out),
    ]

    try:
        t0 = time.time()
        r = subprocess.run(cmd_crf, capture_output=True, text=True, timeout=1800)
        if r.returncode == 0 and Path(out).exists():
            om = Path(out).stat().st_size / 1048576
            print(f"   ✅ {im:.2f}→{om:.2f}MB في {time.time()-t0:.1f}s")
            if om <= max_size_mb or duration <= 0: return True
    except Exception: pass

    if duration > 0:
        return _compress_twopass(inp, out, max_size_mb, duration, ff)
    return False


def _compress_twopass(inp, out, max_size_mb, duration, ff):
    target_bits = max_size_mb * 1024 * 1024 * 8
    audio_bits = 32 * 1024 * duration
    video_bits = max(0, target_bits - audio_bits)
    video_bitrate = max(100, int(video_bits / duration / 1000))
    print(f"   🔄 two-pass: {video_bitrate}k")

    pass_log = tempfile.mktemp(suffix=".log")
    base = [
        ff, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(inp),
        "-vf", f"scale=-2:{config.COMPRESS_SCALE}",
        "-c:v", "libx264", "-preset", config.COMPRESS_PRESET,
        "-b:v", f"{video_bitrate}k",
        "-profile:v", "main", "-level", "3.1", "-pix_fmt", "yuv420p",
        "-passlogfile", pass_log,
    ]
    try:
        subprocess.run(base + ["-pass", "1", "-an", "-f", "null", "/dev/null"],
                       capture_output=True, timeout=1800)
        r = subprocess.run(base + ["-pass", "2", "-c:a", "aac",
                                    "-b:a", config.COMPRESS_AUDIO_BITRATE,
                                    "-ac", "2", "-ar", "44100",
                                    "-movflags", "+faststart", "-threads", "2",
                                    "-y", str(out)],
                           capture_output=True, timeout=1800)
        if r.returncode == 0 and Path(out).exists():
            print(f"   ✅ two-pass: {Path(out).stat().st_size/1048576:.2f}MB")
            return True
    except Exception: pass
    finally:
        for f in [pass_log, pass_log + "-0.log", pass_log + "-0.log.mbtree"]:
            try: os.remove(f)
            except Exception: pass
    return False


# ═══════════════════════════════════════════════════════════════
# CF bypass
# ═══════════════════════════════════════════════════════════════
def _bypass_cloudflare(sb, iframe_url, timeout_seconds=30):
    print(f"      🔄 CF bypass ({timeout_seconds}s)...")
    start = time.time()
    try:
        try:
            sb.driver.execute_cdp_cmd("Page.navigate",
                                       {"url": iframe_url, "referrer": ""})
        except Exception: return False
        sb.cdp.sleep(2)

        for _ in range(2):
            if time.time() - start > timeout_seconds: return False
            try: sb.uc_gui_click_captcha()
            except Exception: pass
            sb.cdp.sleep(2)
            try:
                title = sb.get_page_title() or ""
                html = sb.cdp.get_page_source() or ""
                if ("Just a moment" not in title
                        and "Attention Required" not in title
                        and len(html) > 5000):
                    print(f"      ✅ CF OK ({time.time()-start:.0f}s)")
                    return True
            except Exception: pass
        return False
    except Exception: return False


# ═══════════════════════════════════════════════════════════════
# العملية الكاملة
# ═══════════════════════════════════════════════════════════════
def _process_with_browser(url, out_path):
    from seleniumbase import SB

    netlog = tempfile.mktemp(suffix=".txt")
    open(netlog, "w").close()

    def _log(u):
        try:
            with open(netlog, "a", encoding="utf-8") as fh:
                fh.write(u + "\n")
        except Exception: pass

    def _read():
        try:
            with open(netlog, encoding="utf-8") as fh:
                return [l.strip() for l in fh if l.strip()]
        except Exception: return []

    try:
        with SB(uc=True, xvfb=True, headless=False, incognito=True,
                ad_block_on=False, disable_csp=True,
                page_load_strategy="eager", locale_code="en") as sb:
            try:
                sb.activate_cdp_mode()

                # ★★★ حقن الـ interceptor قبل أي navigation
                _install_interceptor(sb)

                try:
                    import mycdp
                    async def on_req(e):
                        try: _log(e.request.url)
                        except Exception: pass
                    sb.cdp.add_handler(mycdp.network.RequestWillBeSent, on_req)
                except Exception: pass

                print(f"🖥️  فتح: {url[:80]}")
                sb.cdp.open(url)
                sb.cdp.sleep(2)

                cur = sb.cdp.get_current_url() or url
                if "?" in cur: cur = cur.split("?")[0]
                if not cur.endswith("/"): cur += "/"
                watch_url = cur + "?do=watch"
                sb.cdp.open(watch_url)
                sb.cdp.sleep(3)

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
                            print(f"   ✅ {len(servers)} سيرفر")
                            break
                    except Exception: pass

                if not servers: return None

                print(f"\n   📋 جمع iframes...")
                iframe_map = {}
                seen = set()

                for i in range(15):
                    sb.cdp.sleep(0.5)
                    if sb.cdp.execute_script("return typeof getServer2 === 'function'"):
                        break

                for server in servers:
                    sid = server.get("id")
                    if not sid: continue
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
                    except Exception: continue

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
                        if c and c != before: after = c; break
                    if after and after != before:
                        u2 = after.replace("&amp;", "&")
                        if u2 not in seen:
                            seen.add(u2)
                            iframe_map[f"{server.get('name')}_{sid}"] = u2

                if not iframe_map: return None
                print(f"   ✅ {len(iframe_map)} iframe")

                # أولوية: luluvdo أولاً
                def _prio(item):
                    k = item[0].lower()
                    for i, s in enumerate([
                        "luluvdo", "vinovo", "vidsonic", "playmate",
                        "firestream", "vidaraa", "vids", "bysejikuar",
                        "vidsp", "savefiles", "voe",
                    ]):
                        if s in k: return i
                    return 99

                ordered = sorted(iframe_map.items(), key=_prio)

                for sname, iframe_url in ordered:
                    print(f"\n   ═══ {sname} ═══")
                    try:
                        if "luluvdo" in sname.lower():
                            if not _bypass_cloudflare(sb, iframe_url, 30):
                                print(f"      ⏭️ تخطي (CF)")
                                continue
                            sb.cdp.sleep(2)
                        else:
                            try:
                                sb.driver.execute_cdp_cmd("Page.navigate",
                                    {"url": iframe_url, "referrer": watch_url})
                            except Exception: continue
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
                                    print(f"      🔄 متداخل: {n[:70]}")
                                    iframe_url = n
                                    sb.driver.execute_cdp_cmd("Page.navigate",
                                        {"url": iframe_url, "referrer": watch_url})
                                    sb.cdp.sleep(3)
                                else: break
                            except Exception: break

                        # انتظار المشغل
                        for _ in range(15):
                            sb.cdp.sleep(1)
                            try:
                                p = sb.cdp.execute_script(
                                    "return typeof jwplayer!=='undefined'?'jw':"
                                    "(typeof videojs!=='undefined'?'vjs':"
                                    "(document.querySelector('video')?'h5':'none'))"
                                )
                                if p in ("jw", "vjs", "h5"):
                                    print(f"      ✅ {p}")
                                    break
                            except Exception: pass

                        # البحث عن m3u8
                        print(f"      🎬 البحث عن m3u8 ({M3U8_SEARCH_TIMEOUT}s)...")
                        search_start = time.time()
                        m3u8_urls = []
                        while time.time() - search_start < M3U8_SEARCH_TIMEOUT:
                            _aggressive_play(sb)
                            sb.cdp.sleep(1.5)
                            if int(time.time() - search_start) % 5 == 0:
                                found = _scan_for_m3u8(sb, _read)
                                if found:
                                    m3u8_urls = found
                                    print(f"      ✨ {len(found)} m3u8 بعد "
                                          f"{time.time()-search_start:.0f}s")
                                    break

                        if not m3u8_urls:
                            m3u8_urls = _scan_for_m3u8(sb, _read)
                        if not m3u8_urls:
                            print(f"      ❌ لا m3u8")
                            continue

                        print(f"      🎯 {len(m3u8_urls)} مرشح")
                        for u in m3u8_urls[:3]:
                            print(f"         · {u[:100]}")

                        cookies = _get_cookies(sb)

                        # ★ ffmpeg HLS أولاً
                        for m_url in m3u8_urls[:2]:
                            if _ffmpeg_hls(m_url, str(out_path), iframe_url, cookies):
                                return (os.path.getsize(out_path), True)

                        # ★ yt-dlp
                        for m_url in m3u8_urls[:2]:
                            if _ytdlp_hls(m_url, str(out_path), iframe_url, cookies):
                                return (os.path.getsize(out_path), True)
                            break

                        print(f"   ⏭️ فشل: {sname}")
                    except Exception as e:
                        print(f"   ❌ {str(e)[:120]}")
                        continue

                return None
            except Exception as e:
                import traceback
                print(f"   ❌ {str(e)[:200]}")
                traceback.print_exc()
    except Exception as e:
        import traceback
        print(f"   ❌ {str(e)[:200]}")
        traceback.print_exc()
    finally:
        try: os.remove(netlog)
        except Exception: pass

    return None


def download_episode(series_name, episode_num, url,
                     media_type="series", item_name=None):
    safe_name = _safe(item_name or series_name)
    file_prefix = f"movie_{episode_num:02d}" if media_type == "movie" else f"ep{episode_num:03d}"

    out_dir = MEDIA_DIR / safe_name
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / f"{file_prefix}_raw.mp4"
    final = out_dir / f"{file_prefix}.mp4"

    if final.exists() and final.stat().st_size > MIN_SIZE:
        print(f"    ↳ موجودة: {final.name}")
        return final

    print(f"    ↳ تحميل {file_prefix}...")
    try:
        res = run_with_timeout(_process_with_browser, args=(url, raw),
                                timeout=600, default=None)
    except TimeoutError_:
        print(f"    ⏰ تجاوز التحميل 600s")
        for p in ["yt-dlp", "chrome", "ffmpeg"]:
            subprocess.run(["pkill", "-9", "-f", p],
                           capture_output=True, timeout=5)
        res = None
    except Exception as e:
        print(f"    ⚠️ {str(e)[:150]}")
        res = None

    if not res or not raw.exists():
        print(f"    ⚠️ فشل تحميل {series_name} — {file_prefix}")
        try:
            if raw.exists(): raw.unlink()
        except Exception: pass
        return None

    size = raw.stat().st_size
    print(f"    📦 {size/1048576:.1f}MB")

    if config.SKIP_COMPRESS:
        shutil.move(str(raw), str(final))
    else:
        if not _compress_to_target(raw, final):
            print("    ⚠️ فشل الضغط — استخدام الأصلي")
            shutil.move(str(raw), str(final))

    if raw.exists(): raw.unlink()
    if not final.exists(): return None

    print(f"    ✅ {final.name} ({final.stat().st_size/1048576:.1f}MB)")
    return final