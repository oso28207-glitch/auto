"""
downloader.py — تحميل من u.3seq.com مع كشف m3u8 محسّن

★ التحسينات:
  1. ★ نقرات عدوانية على المشغل + polling أطول.
  2. ★ كشف m3u8 من: network log + performance + HTML + JS globals.
  3. ★ wait أطول لكل سيرفر (حتى 45s).
  4. ★ معالجة خاصة لكل سيرفر (vinovo, playmate, firestream, luluvdo).
  5. yt-dlp مع watchdog + cache.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as cffi_requests

from config import config, MEDIA_DIR

IMPERSONATE = os.environ.get("IMPERSONATE_TARGET", "chrome120")
CURL_WORKERS = int(os.environ.get("CURL_CFFI_WORKERS", "8"))
MIN_SIZE = 100 * 1024
CONCAT_CHUNK = 50

SERVER_TIMEOUT = int(os.environ.get("SERVER_TIMEOUT", "240"))
YTDLP_TOTAL_TIMEOUT = int(os.environ.get("YTDLP_TIMEOUT", "120"))
CF_TIMEOUT = int(os.environ.get("CF_TIMEOUT", "30"))
M3U8_SEARCH_TIMEOUT = int(os.environ.get("M3U8_SEARCH_TIMEOUT", "45"))

YTDLP_CONCURRENT_FRAGMENTS = int(os.environ.get("YTDLP_CONCURRENT", "16"))
YTDLP_THROTTLED_RATE = os.environ.get("YTDLP_THROTTLED_RATE", "2M")
YTDLP_MIN_SPEED_KB = int(os.environ.get("YTDLP_MIN_SPEED_KB", "800"))
YTDLP_MAX_STALL = int(os.environ.get("YTDLP_MAX_STALL", "20"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_SEGMENT_CACHE = {}


# ═══════════════════════════════════════════════════════════════
# أدوات
# ═══════════════════════════════════════════════════════════════
class TimeoutError_(Exception):
    pass


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

    t = threading.Thread(target=_run, daemon=True)
    t.start()

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
        path = imageio_ffmpeg.get_ffmpeg_exe()
        r = subprocess.run([path, "-version"], capture_output=True, timeout=5)
        if r.returncode == 0:
            return path
    except Exception:
        pass
    return "ffmpeg"


def _m3u8_cache_key(m3u8_url):
    base = m3u8_url.split("?")[0]
    return hashlib.md5(base.encode()).hexdigest()[:12]


def _parse_m3u8(text, base):
    segs, variants = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ".m3u8" in line:
            variants.append(line if line.startswith("http") else urljoin(base + "/", line))
        elif line.endswith(".ts") or ".ts?" in line or ".m4s" in line:
            segs.append(line if line.startswith("http") else urljoin(base + "/", line))
    return segs, variants


# ═══════════════════════════════════════════════════════════════
# ★★★ كشف m3u8 من مصادر متعددة
# ═══════════════════════════════════════════════════════════════
def _scan_for_m3u8(sb, netlog_read):
    """
    يبحث عن m3u8 من:
      1. Network log
      2. performance.getEntriesByType
      3. HTML page source
      4. JavaScript global variables
      5. video.currentSrc
    """
    found = set()

    # 1) Network log
    try:
        for u in netlog_read():
            if ".m3u8" in u or ".mpd" in u:
                found.add(u)
    except Exception:
        pass

    # 2) performance entries
    try:
        perf = sb.cdp.execute_script("""
            (function(){try{
                return performance.getEntriesByType('resource')
                    .map(e => e.name);
            }catch(e){return [];}})();
        """)
        if isinstance(perf, list):
            for u in perf:
                if ".m3u8" in u or ".mpd" in u:
                    found.add(u)
    except Exception:
        pass

    # 3) HTML source
    try:
        html = sb.cdp.get_page_source() or ""
        for m in re.finditer(r'(https?:[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*)', html):
            found.add(m.group(1).replace("\\/", "/"))
        for m in re.finditer(r'(https?:[^\s"\'<>\\]+\.mpd[^\s"\'<>\\]*)', html):
            found.add(m.group(1).replace("\\/", "/"))
        # روابط نسبية
        for m in re.finditer(r'["\'](/[^"\']+\.m3u8[^"\']*)["\']', html):
            u = m.group(1)
            try:
                cur = sb.cdp.get_current_url() or ""
                origin = _origin(cur)
                found.add(origin + u)
            except Exception:
                pass
    except Exception:
        pass

    # 4) JS globals
    try:
        js_urls = sb.cdp.execute_script("""
            (function(){
                var found = [];
                try {
                    var globals = ['sources', 'playlist', 'file', 'video_url',
                                   'hls_url', 'master_url', 'm3u8_url'];
                    for (var i = 0; i < globals.length; i++) {
                        var v = window[globals[i]];
                        if (typeof v === 'string' && v.indexOf('.m3u8') !== -1) {
                            found.push(v);
                        }
                        if (typeof v === 'object' && v) {
                            try {
                                var str = JSON.stringify(v);
                                var m = str.match(/https?:[^"']+\\.m3u8[^"']*/g);
                                if (m) found = found.concat(m);
                            } catch(e){}
                        }
                    }
                    // jwplayer
                    if (typeof jwplayer !== 'undefined') {
                        try {
                            var p = jwplayer();
                            if (p && p.getPlaylist) {
                                var pl = p.getPlaylist();
                                if (pl && pl[0] && pl[0].file) found.push(pl[0].file);
                            }
                            if (p && p.getConfig) {
                                var cfg = p.getConfig();
                                if (cfg && cfg.file) found.push(cfg.file);
                                if (cfg && cfg.sources) {
                                    cfg.sources.forEach(function(s) {
                                        if (s.file) found.push(s.file);
                                    });
                                }
                            }
                        } catch(e){}
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

    # 5) video.currentSrc
    try:
        video_src = sb.cdp.execute_script("""
            (function(){
                try {
                    var v = document.querySelector('video');
                    if (v) {
                        if (v.currentSrc && v.currentSrc.indexOf('.m3u8') !== -1)
                            return [v.currentSrc];
                        if (v.src && v.src.indexOf('.m3u8') !== -1)
                            return [v.src];
                    }
                    // المصادر داخل video
                    var sources = document.querySelectorAll('video source');
                    var res = [];
                    for (var i = 0; i < sources.length; i++) {
                        if (sources[i].src && sources[i].src.indexOf('.m3u8') !== -1)
                            res.push(sources[i].src);
                    }
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

    # ترتيب: index → master → mpd → البقية
    urls = list(found)
    urls = [u for u in urls if "ping.gif" not in u and "jwpltx" not in u]

    idx = [u for u in urls if "index" in u.lower()]
    mst = [u for u in urls if "master" in u.lower()]
    mpd = [u for u in urls if ".mpd" in u.lower()]
    oth = [u for u in urls if u not in idx and u not in mst and u not in mpd]

    return idx + mst + mpd + oth


# ═══════════════════════════════════════════════════════════════
# ★★★ نقرات عدوانية على المشغل
# ═══════════════════════════════════════════════════════════════
def _aggressive_play(sb):
    """إزالة الإعلانات + نقرات متعددة."""
    # إزالة إعلانات
    try:
        sb.cdp.execute_script("""
            (function(){
                try {
                    ['iframe[id*="ad"]', 'div[class*="popup"]',
                     '[class*="overlay"]', '.ads'].forEach(function(sel){
                        document.querySelectorAll(sel).forEach(function(el){
                            el.style.display = 'none';
                            el.style.pointerEvents = 'none';
                        });
                    });
                    document.body.style.overflow = 'auto';
                } catch(e){}
            })();
        """)
    except Exception:
        pass

    # نقرات على أزرار التشغيل
    for sel in [
        "video", ".jw-icon-playback", ".jw-display-icon-container",
        ".jw-icon-display", ".vjs-big-play-button",
        "[class*='play']", "button[aria-label*='play']",
        ".play-btn", "#play", ".play-button",
    ]:
        for _ in range(2):
            try:
                sb.cdp.click_if_visible(sel)
            except Exception:
                pass

    # نقر في مركز الفيديو
    try:
        rect = sb.cdp.execute_script("""
            (function(){
                var v = document.querySelector('video');
                if (!v) return null;
                var r = v.getBoundingClientRect();
                if (r.width < 50 || r.height < 50) return null;
                return {x: Math.round(r.left + r.width/2),
                        y: Math.round(r.top + r.height/2)};
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
    except Exception:
        pass

    # تشغيل برمجي
    try:
        sb.cdp.execute_script("""
            (function(){try{
                if(typeof jwplayer!=='undefined'){
                    var p=jwplayer();
                    if(p){
                        if(p.play){p.play(true);}
                        if(p.setMute){p.setMute(true);}
                    }
                }
                if(typeof videojs!=='undefined'){
                    try {
                        var p2 = videojs.getPlayers();
                        for (var k in p2) {
                            try { p2[k].play(); p2[k].muted(true); } catch(e){}
                        }
                    } catch(e){}
                }
                var v=document.querySelector('video');
                if(v){
                    v.muted=true;
                    if(v.play) v.play().catch(function(){});
                }
            }catch(e){}})();
        """)
    except Exception:
        pass


def _get_cookies(sb):
    try:
        r = sb.driver.execute_cdp_cmd("Network.getAllCookies", {})
        if r and r.get("cookies"):
            return {c["name"]: c["value"] for c in r["cookies"]
                    if c.get("name") and c.get("value")}
    except Exception:
        pass
    return {}


# ═══════════════════════════════════════════════════════════════
# الدمج
# ═══════════════════════════════════════════════════════════════
def _concat_attempt(listfile, out, args, timeout=900):
    ff = _get_ffmpeg_exe()
    cmd = [ff, "-nostdin", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", listfile] + args + ["-y", out]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        stderr = (r.stderr or "").strip()
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 5000:
            return True, ""
        errs = [l for l in stderr.split("\n") if l.strip()]
        return False, " || ".join(errs[-3:])[:400] if errs else f"code={r.returncode}"
    except subprocess.TimeoutExpired:
        return False, f"timeout"
    except Exception as e:
        return False, str(e)[:150]


def _verify_merged(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return False, "ffprobe فشل"
        duration = float(r.stdout.strip())
        if duration < 30:
            return False, f"مدة قصيرة ({duration}s)"

        r2 = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", str(path), "-vframes", "3", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        if r2.returncode != 0:
            return False, f"لا يُقرأ"
        return True, f"{duration:.0f}s"
    except Exception as e:
        return False, str(e)[:100]


def _smart_concat(sorted_paths, out):
    total = len(sorted_paths)
    if total == 0:
        return False

    print(f"      🔗 دمج {total} segment...")

    strategies = [
        (["-c", "copy", "-bsf:a", "aac_adtstoasc",
          "-movflags", "+faststart",
          "-max_muxing_queue_size", "4096",
          "-fflags", "+genpts+igndts",
          "-f", "mp4"], "mp4-aac"),
        (["-c", "copy", "-movflags", "+faststart",
          "-max_muxing_queue_size", "4096",
          "-fflags", "+genpts",
          "-f", "mp4"], "mp4"),
        (["-c", "copy", "-max_muxing_queue_size", "4096",
          "-fflags", "+genpts",
          "-f", "mpegts"], "ts"),
        (["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
          "-c:a", "aac", "-b:a", "64k",
          "-movflags", "+faststart",
          "-f", "mp4"], "reencode"),
    ]

    # محاولة مباشرة
    print(f"      📌 دمج مباشر...")
    listfile = tempfile.mktemp(suffix=".txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for p in sorted_paths:
            sp = str(p).replace("\\", "/").replace("'", r"'\''")
            f.write(f"file '{sp}'\n")

    for args, name in strategies:
        if os.path.exists(out):
            try: os.remove(out)
            except Exception: pass
        ok, err = _concat_attempt(listfile, out, args, timeout=1200)
        if ok:
            print(f"         ✅ {name}")
            try: os.remove(listfile)
            except Exception: pass
            return True
        print(f"         ❌ {name}: {err[:150]}")

    try: os.remove(listfile)
    except Exception: pass

    # chunked
    print(f"      📦 chunked ({CONCAT_CHUNK}/دفعة)...")
    groups = [sorted_paths[i:i + CONCAT_CHUNK]
              for i in range(0, total, CONCAT_CHUNK)]
    group_files = []

    for gi, grp in enumerate(groups):
        gpath = out + f".part{gi:04d}.ts"
        if os.path.exists(gpath):
            try: os.remove(gpath)
            except Exception: pass

        lf = tempfile.mktemp(suffix=".txt")
        with open(lf, "w", encoding="utf-8") as f:
            for p in grp:
                sp = str(p).replace("\\", "/").replace("'", r"'\''")
                f.write(f"file '{sp}'\n")

        ok = False
        for args, name in strategies:
            if os.path.exists(gpath):
                try: os.remove(gpath)
                except Exception: pass
            ok, err = _concat_attempt(lf, gpath, args, timeout=600)
            if ok:
                break

        try: os.remove(lf)
        except Exception: pass

        if ok:
            group_files.append(gpath)
        else:
            for gf in group_files:
                try: os.remove(gf)
                except Exception: pass
            return False

    final_ok = False
    if group_files:
        fl = tempfile.mktemp(suffix=".txt")
        with open(fl, "w", encoding="utf-8") as f:
            for p in group_files:
                sp = str(p).replace("\\", "/").replace("'", r"'\''")
                f.write(f"file '{sp}'\n")

        for args, name in strategies:
            if os.path.exists(out):
                try: os.remove(out)
                except Exception: pass
            ok, err = _concat_attempt(fl, out, args, timeout=600)
            if ok:
                final_ok = True
                break

        try: os.remove(fl)
        except Exception: pass
        for gf in group_files:
            try: os.remove(gf)
            except Exception: pass

    return final_ok


# ═══════════════════════════════════════════════════════════════
# الضغط
# ═══════════════════════════════════════════════════════════════
def _get_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return 0


def _compress_to_target(inp, out, max_size_mb=None):
    if max_size_mb is None:
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
        "-ac", "2", "-ar", "44100",
        "-movflags", "+faststart",
        "-threads", "2", "-y", str(out),
    ]

    try:
        t0 = time.time()
        r = subprocess.run(cmd_crf, capture_output=True, text=True, timeout=1800)
        if r.returncode == 0 and Path(out).exists():
            om = Path(out).stat().st_size / 1048576
            print(f"   ✅ {im:.2f}→{om:.2f}MB في {time.time()-t0:.1f}s")
            if om <= max_size_mb or duration <= 0:
                return True
            print(f"   ⚠️ تجاوز {max_size_mb}MB → two-pass...")
    except Exception as e:
        print(f"   ⚠️ {str(e)[:100]}")

    if duration > 0:
        return _compress_twopass(inp, out, max_size_mb, duration, ff)
    return False


def _compress_twopass(inp, out, max_size_mb, duration, ff):
    audio_bitrate = 32 * 1024
    target_bits = max_size_mb * 1024 * 1024 * 8
    audio_bits = audio_bitrate * duration
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
        r = subprocess.run(
            base + ["-pass", "2", "-c:a", "aac",
                    "-b:a", config.COMPRESS_AUDIO_BITRATE,
                    "-ac", "2", "-ar", "44100",
                    "-movflags", "+faststart", "-threads", "2",
                    "-y", str(out)],
            capture_output=True, timeout=1800,
        )
        if r.returncode == 0 and Path(out).exists():
            om = Path(out).stat().st_size / 1048576
            print(f"   ✅ two-pass: {om:.2f}MB")
            return True
    except Exception:
        pass
    finally:
        for f in [pass_log, pass_log + "-0.log", pass_log + "-0.log.mbtree"]:
            try: os.remove(f)
            except Exception: pass
    return False


# ═══════════════════════════════════════════════════════════════
# yt-dlp
# ═══════════════════════════════════════════════════════════════
def _ytdlp_worker(cmd, out, min_speed_bytes, max_stall):
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, preexec_fn=os.setsid,
        )
    except Exception as e:
        return False, f"spawn: {str(e)[:80]}"

    t0 = time.time()
    last_speed_ok = time.time()
    killed = False

    try:
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip()
            if "PROGRESS:" in line:
                try:
                    parts = line.split("PROGRESS:", 1)[1].strip().split("|")
                    pct = float(parts[0].replace("%", "").strip())
                    speed_b = 0
                    for p in parts:
                        if "speed_bytes:" in p:
                            try:
                                speed_b = float(p.replace("speed_bytes:", "").strip())
                            except Exception:
                                speed_b = 0
                    now = time.time()
                    if speed_b >= min_speed_bytes:
                        last_speed_ok = now
                    elif now - last_speed_ok >= max_stall:
                        killed = True
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except Exception:
                            proc.kill()
                        break
                except Exception:
                    pass
    except Exception:
        pass

    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass

    if killed:
        return False, "slow_speed"
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
        min_speed_bytes = YTDLP_MIN_SPEED_KB * 1024

        for mode in ["native", "ffmpeg"]:
            cmd = [
                "yt-dlp", "--no-warnings", "--no-playlist", "--no-part",
                "--newline", "--progress",
                "--progress-template",
                "download:PROGRESS:%(progress._percent_str)s|"
                "speed_bytes:%(progress.speed)s",
                "--hls-prefer-" + mode,
                "--user-agent", UA,
                "--referer", origin_url + "/",
                "--add-header", f"Origin:{origin_url}",
                "--cookies", cookie_file,
                "--retries", "3", "--fragment-retries", "3",
                "--socket-timeout", "30",
                "--concurrent-fragments", str(YTDLP_CONCURRENT_FRAGMENTS),
                "--throttled-rate", YTDLP_THROTTLED_RATE,
                "-f", "bv*[height<=360]+ba/b[height<=360]/bv*+ba/b",
                "--merge-output-format", "mp4",
                "-o", out, m3u8_url,
            ]
            if ff != "ffmpeg":
                cmd.extend(["--ffmpeg-location", ff])

            try:
                ok, reason = run_with_timeout(
                    _ytdlp_worker,
                    args=(cmd, out, min_speed_bytes, YTDLP_MAX_STALL),
                    timeout=YTDLP_TOTAL_TIMEOUT,
                    default=(False, "outer_timeout"),
                )
                if ok:
                    return True, "ok"
                if reason == "slow_speed":
                    return False, "slow_speed"
                if reason == "outer_timeout":
                    return False, "outer_timeout"
            except TimeoutError_:
                subprocess.run(["pkill", "-9", "-f", "yt-dlp"],
                               capture_output=True, timeout=5)
                return False, "outer_timeout"

        return False, "all_failed"
    finally:
        try: os.remove(cookie_file)
        except Exception: pass


# ═══════════════════════════════════════════════════════════════
# Browser segments
# ═══════════════════════════════════════════════════════════════
def _browser_fetch_batch(sb, urls, timeout=40, with_creds=True):
    if not urls:
        return {}
    creds = "credentials:'include'," if with_creds else ""
    js = """
    (function(){
        window.__br = {}; window.__bd = false;
        var urls = %s; var results = {}; var pending = urls.length;
        if (pending === 0) { window.__br = results; window.__bd = true; return; }
        urls.forEach(function(u, idx){
            var done = function(v){
                results[String(idx)] = v; pending--;
                if (pending === 0) { window.__br = results; window.__bd = true; }
            };
            try {
                fetch(u, {mode:'cors', %s}).then(function(r){
                    if (!r.ok) throw new Error('HTTP '+r.status);
                    return r.arrayBuffer();
                }).then(function(buf){
                    var b = new Uint8Array(buf), s = '', c = 16384;
                    for (var j=0;j<b.length;j+=c) {
                        s += String.fromCharCode.apply(null,
                            b.subarray(j, Math.min(j+c, b.length)));
                    }
                    try { done(btoa(s)); } catch(e){ done(null); }
                }).catch(function(){ done(null); });
            } catch(e) { done(null); }
        });
    })();
    """ % (json.dumps(urls), creds)

    try:
        sb.cdp.execute_script(js)
    except Exception:
        return {}

    start = time.time()
    while time.time() - start < timeout:
        sb.cdp.sleep(0.15)
        try:
            if sb.cdp.execute_script("return window.__bd===true"):
                return sb.cdp.execute_script("return window.__br") or {}
        except Exception:
            pass
    return {}


def _browser_segments(sb, segments, out, cache_key=None):
    if cache_key and cache_key in _SEGMENT_CACHE:
        cached_dir, cached_paths = _SEGMENT_CACHE[cache_key]
        if os.path.exists(cached_dir):
            print(f"      💾 cache ({len(cached_paths)} segment)")
            sorted_p = [cached_paths[k] for k in sorted(cached_paths.keys())]
            if _smart_concat(sorted_p, out):
                ok, msg = _verify_merged(str(out))
                if ok:
                    return (os.path.getsize(out), True)

    batch = 16
    total = len(segments)
    print(f"      🌐 المتصفح: {total} segment...")

    seg_dir = tempfile.mkdtemp(prefix="hls_br_")
    paths, failed, total_bytes = {}, 0, 0

    for i in range(0, total, batch):
        chunk = segments[i:i + batch]
        result = _browser_fetch_batch(sb, chunk, with_creds=True, timeout=25)
        if not result or all(v is None for v in result.values()):
            result = _browser_fetch_batch(sb, chunk, with_creds=False, timeout=25)

        for k, b64 in result.items():
            try:
                li = int(k)
            except Exception:
                continue
            si = i + li
            if b64:
                try:
                    data = base64.b64decode(b64)
                    p = os.path.join(seg_dir, f"s_{si:06d}.ts")
                    with open(p, "wb") as f:
                        f.write(data)
                    paths[si] = p
                    total_bytes += len(data)
                except Exception:
                    failed += 1
            else:
                failed += 1

        done = min(i + batch, total)
        if done % (batch * 4) == 0 or done == total:
            print(f"         📦 {done}/{total} | {total_bytes/1048576:.1f}MB | فشل: {failed}")

    if not paths:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    if cache_key:
        _SEGMENT_CACHE[cache_key] = (seg_dir, dict(paths))

    sorted_p = [paths[k] for k in sorted(paths)]

    if not _smart_concat(sorted_p, out):
        shutil.rmtree(seg_dir, ignore_errors=True)
        if cache_key:
            _SEGMENT_CACHE.pop(cache_key, None)
        return None

    ok, msg = _verify_merged(str(out))
    if not ok:
        print(f"      ⚠️ تالف: {msg}")
        try: os.remove(out)
        except Exception: pass
        if cache_key:
            _SEGMENT_CACHE.pop(cache_key, None)
        return None

    final_size = os.path.getsize(out)
    print(f"      ✅ صالح ({msg}): {final_size/1048576:.1f} MB")
    return (final_size, True)


def _cffi_segments(segments, out, iframe_url, cookies, sb=None, cache_key=None):
    headers = {
        "Referer": iframe_url,
        "Origin": _origin(iframe_url),
        "User-Agent": UA,
        "Accept": "*/*",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    cookies = cookies or {}

    test_ok = False
    for imp in ["chrome124", "chrome120"]:
        try:
            r = cffi_requests.get(segments[0], headers=headers,
                                  cookies=cookies, impersonate=imp,
                                  timeout=8, verify=False)
            if r.status_code == 200 and len(r.content) > 100:
                test_ok = True
                break
        except Exception:
            continue

    if not test_ok:
        print(f"      ⚠️ cffi فشل → المتصفح")
        if sb is not None:
            return _browser_segments(sb, segments, out, cache_key)
        return None

    seg_dir = tempfile.mkdtemp(prefix="hls_cffi_")
    paths, failed, total = {}, 0, 0

    def _dl(iu):
        i, u = iu
        try:
            r = cffi_requests.get(u, headers=headers, cookies=cookies,
                                  impersonate="chrome120", timeout=15, verify=False)
            if r.status_code == 200 and len(r.content) > 100:
                p = os.path.join(seg_dir, f"s_{i:06d}.ts")
                with open(p, "wb") as f:
                    f.write(r.content)
                return (i, p, len(r.content))
        except Exception:
            pass
        return (i, None, 0)

    print(f"      ⚡ {len(segments)} segment cffi...")
    with ThreadPoolExecutor(max_workers=CURL_WORKERS) as ex:
        futs = [ex.submit(_dl, (i, s)) for i, s in enumerate(segments)]
        done = 0
        for f in as_completed(futs):
            i, p, sz = f.result()
            done += 1
            if p:
                paths[i] = p
                total += sz
            else:
                failed += 1
            if done % 40 == 0 or done == len(segments):
                print(f"         📦 {done}/{len(segments)} | {total/1048576:.1f}MB | فشل: {failed}")

    if not paths or failed > len(segments) * 0.15:
        shutil.rmtree(seg_dir, ignore_errors=True)
        if sb is not None:
            print(f"      ⚠️ cffi فشل → المتصفح")
            return _browser_segments(sb, segments, out, cache_key)
        return None

    sorted_p = [paths[k] for k in sorted(paths)]

    if not _smart_concat(sorted_p, out):
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    ok, msg = _verify_merged(str(out))
    if not ok:
        try: os.remove(out)
        except Exception: pass
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    final_size = os.path.getsize(out)
    print(f"      ✅ صالح ({msg}): {final_size/1048576:.1f} MB")
    shutil.rmtree(seg_dir, ignore_errors=True)
    return (final_size, True)


# ═══════════════════════════════════════════════════════════════
# CF bypass
# ═══════════════════════════════════════════════════════════════
def _bypass_cloudflare(sb, iframe_url, timeout_seconds=None):
    if timeout_seconds is None:
        timeout_seconds = CF_TIMEOUT

    print(f"      🔄 CF bypass ({timeout_seconds}s)...")
    start = time.time()

    try:
        try:
            sb.driver.execute_cdp_cmd("Page.navigate", {
                "url": iframe_url, "referrer": "",
            })
        except Exception:
            return False
        sb.cdp.sleep(2)

        for attempt in range(2):
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
                    print(f"      ✅ CF OK ({time.time()-start:.0f}s)")
                    return True
            except Exception:
                pass

        return False
    except Exception:
        return False


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
        except Exception:
            return []

    try:
        with SB(uc=True, xvfb=True, headless=False, incognito=True,
                ad_block_on=False, disable_csp=True,
                page_load_strategy="eager", locale_code="en") as sb:
            try:
                sb.activate_cdp_mode()
                try:
                    import mycdp
                    async def on_req(e):
                        try: _log(e.request.url)
                        except Exception: pass
                    sb.cdp.add_handler(mycdp.network.RequestWillBeSent, on_req)
                except Exception:
                    pass

                print(f"🖥️  فتح: {url[:80]}")
                sb.cdp.open(url)
                sb.cdp.sleep(2)

                cur = sb.cdp.get_current_url() or url
                if "?" in cur:
                    cur = cur.split("?")[0]
                if not cur.endswith("/"):
                    cur += "/"
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
                    except Exception:
                        pass

                if not servers:
                    return None

                # جمع iframes
                print(f"\n   📋 جمع iframes...")
                iframe_map = {}
                seen = set()

                for i in range(15):
                    sb.cdp.sleep(0.5)
                    if sb.cdp.execute_script("return typeof getServer2 === 'function'"):
                        break

                for server in servers:
                    sid = server.get("id")
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
                                return {{x: Math.round(r.left + r.width/2),
                                        y: Math.round(r.top + r.height/2)}};
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
                            iframe_map[f"{server.get('name')}_{sid}"] = u2

                if not iframe_map:
                    return None

                print(f"   ✅ {len(iframe_map)} iframe")

                def _prio(item):
                    k = item[0].lower()
                    for i, s in enumerate([
                        "vids", "vidaraa", "playmate", "firestream",
                        "vidsonic", "vinovo", "bysejikuar",
                        "vidsp", "savefiles", "voe",
                        "luluvdo",
                    ]):
                        if s in k: return i
                    return 99

                ordered = sorted(iframe_map.items(), key=_prio)

                # ★★★ حلقة السيرفرات
                for sname, iframe_url in ordered:
                    print(f"\n   ═══ {sname} ═══")

                    try:
                        # CF bypass
                        if "luluvdo" in sname.lower():
                            if not _bypass_cloudflare(sb, iframe_url, CF_TIMEOUT):
                                print(f"      ⏭️ تخطي (CF)")
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
                                            && ifr.src.indexOf('google') === -1) {
                                            return ifr.src;
                                        }
                                        return null;
                                    })();
                                """)
                                if n and n != iframe_url:
                                    print(f"      🔄 متداخل: {n[:70]}")
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
                        player_ready = False
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
                                    player_ready = True
                                    break
                            except Exception:
                                pass

                        if not player_ready:
                            print(f"      ⚠️ المشغل لم يظهر")

                        # ★★★ نقرات عدوانية + انتظار m3u8 (45s)
                        print(f"      🎬 البحث عن m3u8 (حتى {M3U8_SEARCH_TIMEOUT}s)...")
                        search_start = time.time()
                        m3u8_urls = []

                        while time.time() - search_start < M3U8_SEARCH_TIMEOUT:
                            _aggressive_play(sb)
                            sb.cdp.sleep(1.5)

                            # ابحث كل 5s
                            if int(time.time() - search_start) % 5 == 0:
                                found = _scan_for_m3u8(sb, _read)
                                if found:
                                    m3u8_urls = found
                                    print(f"      ✨ وُجد {len(found)} m3u8 بعد "
                                          f"{time.time()-search_start:.0f}s")
                                    break

                        # إذا لم نجد بعد الوقت، حاول مرة أخيرة
                        if not m3u8_urls:
                            m3u8_urls = _scan_for_m3u8(sb, _read)

                        if not m3u8_urls:
                            print(f"      ❌ لا m3u8 ({M3U8_SEARCH_TIMEOUT}s)")
                            continue

                        print(f"      🎯 {len(m3u8_urls)} مرشح")
                        for u in m3u8_urls[:3]:
                            print(f"         · {u[:100]}")

                        cookies = _get_cookies(sb)

                        # ★★★ yt-dlp
                        for m_url in m3u8_urls[:2]:
                            yt_ok, yt_reason = _ytdlp_hls(
                                m_url, str(out_path), iframe_url, cookies
                            )
                            if yt_ok:
                                ok, msg = _verify_merged(str(out_path))
                                if ok:
                                    return (os.path.getsize(out_path), True)
                                try: os.remove(out_path)
                                except Exception: pass
                            elif yt_reason == "slow_speed":
                                print(f"      ⏭️ yt-dlp بطيء → cffi/browser")
                                break
                            elif yt_reason in ("outer_timeout", "all_failed"):
                                break

                        # ★★★ cffi → browser
                        for m_url in m3u8_urls[:3]:
                            # اجلب m3u8 content
                            content = None
                            for expr in [
                                f"fetch({json.dumps(m_url)},{{mode:'cors',credentials:'include'}})",
                                f"fetch({json.dumps(m_url)},{{mode:'cors'}})",
                            ]:
                                js = f"""
                                (function(){{
                                    window.__dv=false; window.__rv=null;
                                    try{{ {expr}.then(r=>r.text().then(t=>{{
                                        window.__rv={{ok:true,s:r.status,t:t}};
                                        window.__dv=true;
                                    }})).catch(e=>{{window.__dv=true;}});
                                    }}catch(e){{window.__dv=true;}}
                                }})();
                                """
                                try:
                                    sb.cdp.execute_script(js)
                                except Exception:
                                    continue
                                start = time.time()
                                while time.time() - start < 10:
                                    sb.cdp.sleep(0.2)
                                    try:
                                        if sb.cdp.execute_script("return window.__dv===true"):
                                            r = sb.cdp.execute_script("return window.__rv")
                                            if r and r.get("ok") and r.get("s") == 200:
                                                content = r.get("t")
                                            break
                                    except Exception:
                                        pass
                                if content:
                                    break

                            if not content:
                                continue

                            segs, variants = _parse_m3u8(content, m_url.rsplit("/", 1)[0])

                            # variants → master
                            if not segs and variants:
                                for v in variants[:2]:
                                    try:
                                        r = cffi_requests.get(v, headers={
                                            "Referer": iframe_url,
                                            "Origin": _origin(iframe_url),
                                            "User-Agent": UA,
                                        }, cookies=cookies, impersonate="chrome120",
                                            timeout=10, verify=False)
                                        if r.status_code == 200:
                                            s2, _ = _parse_m3u8(r.text, v.rsplit("/", 1)[0])
                                            if s2:
                                                segs = s2
                                                m_url = v
                                                break
                                    except Exception:
                                        continue

                            if segs:
                                print(f"      ⬇️ {len(segs)} segment")
                                ck = _m3u8_cache_key(m_url)
                                res = _cffi_segments(
                                    segs, str(out_path), iframe_url, cookies, sb, ck
                                )
                                if res:
                                    return res

                        print(f"   ⏭️ فشل: {sname}")

                    except Exception as e:
                        print(f"   ❌ خطأ في {sname}: {str(e)[:120]}")
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


# ═══════════════════════════════════════════════════════════════
# الدالة العامة
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
        print(f"    ↳ موجودة: {final.name}")
        return final

    print(f"    ↳ تحميل {file_prefix}...")

    total_timeout = 600
    try:
        res = run_with_timeout(
            _process_with_browser,
            args=(url, raw),
            timeout=total_timeout,
            default=None,
        )
    except TimeoutError_:
        print(f"    ⏰ تجاوز التحميل {total_timeout}s")
        subprocess.run(["pkill", "-9", "-f", "yt-dlp"], capture_output=True, timeout=5)
        subprocess.run(["pkill", "-9", "-f", "chrome"], capture_output=True, timeout=5)
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


if __name__ == "__main__":
    import sys
    test_url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://u.3seq.cam/video/modablaj-muhtemel-ask-episode-51/"
    )
    result = download_episode("test", 51, test_url)
    print(f"\n{'✅ نجح: ' + str(result) if result else '⚠️ فشل'}")