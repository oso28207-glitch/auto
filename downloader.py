"""
downloader.py — تحميل وضغط (حد 45MB) مع إصلاحات شاملة

★ الميزات:
  1. ★ luluvdo CF bypass مع timeout 45s (لا يعلق).
  2. ★ yt-dlp speed watchdog (يوقف عند بطء CDN).
  3. ★ cache للـ segments (لا تكرار بين السيرفرات).
  4. ★ 5 استراتيجيات دمج + chunked fallback.
  5. ★ التحقق من الملف المدموج قبل الرفع.
  6. ضغط two-pass للوصول إلى 45MB.
  7. أولوية: vidaraa → playmate → firestream → vids → vidsonic → vinovo → luluvdo.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as cffi_requests

from config import config, MEDIA_DIR

# ═══════════════════════════════════════════════════════════════
# الإعدادات
# ═══════════════════════════════════════════════════════════════
IMPERSONATE = os.environ.get("IMPERSONATE_TARGET", "chrome120")
CURL_WORKERS = int(os.environ.get("CURL_CFFI_WORKERS", "8"))
JWPLAYER_WAIT = 20
M3U8_WAIT = 20
MIN_SIZE = 100 * 1024
CONCAT_CHUNK = 50
SERVER_TIMEOUT = 300  # ★ 5 دقائق كحد أقصى لكل سيرفر
CF_TIMEOUT = 45       # ★ 45 ثانية لتجاوز Cloudflare

YTDLP_CONCURRENT_FRAGMENTS = int(os.environ.get("YTDLP_CONCURRENT", "16"))
YTDLP_THROTTLED_RATE = os.environ.get("YTDLP_THROTTLED_RATE", "2M")
YTDLP_MIN_SPEED_KB = int(os.environ.get("YTDLP_MIN_SPEED_KB", "800"))
YTDLP_MAX_STALL = int(os.environ.get("YTDLP_MAX_STALL", "30"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ★ Cache عالمي للـ segments
_SEGMENT_CACHE = {}


# ═══════════════════════════════════════════════════════════════
# أدوات
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
        path = imageio_ffmpeg.get_ffmpeg_exe()
        r = subprocess.run([path, "-version"], capture_output=True, timeout=5)
        if r.returncode == 0:
            print(f"   ℹ️  imageio-ffmpeg: {path}")
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
# ★★★ الدمج مع التحقق — 5 استراتيجيات + chunked
# ═══════════════════════════════════════════════════════════════
def _concat_attempt(listfile, out, args, timeout=1800):
    ff = _get_ffmpeg_exe()
    cmd = [ff, "-nostdin", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", listfile] + args + ["-y", out]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        stderr = (r.stderr or "").strip()
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 5000:
            return True, ""
        errs = [l for l in stderr.split("\n") if l.strip()]
        return False, " || ".join(errs[-5:])[:500] if errs else f"code={r.returncode}"
    except subprocess.TimeoutExpired:
        return False, f"timeout {timeout}s"
    except Exception as e:
        return False, str(e)[:200]


def _verify_merged(path):
    """يتحقق من صلاحية الملف المدموج."""
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
            return False, f"المدة قصيرة ({duration}s)"

        r2 = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", str(path), "-vframes", "3", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        if r2.returncode != 0:
            return False, f"الفيديو لا يُقرأ: {(r2.stderr or '')[:150]}"
        return True, f"{duration:.0f}s"
    except Exception as e:
        return False, str(e)[:150]


def _smart_concat(sorted_paths, out):
    """
    ★ 5 استراتيجيات + دمج مباشر أولاً + chunked fallback.
    """
    total = len(sorted_paths)
    if total == 0:
        print(f"      ❌ قائمة فارغة")
        return False

    print(f"      🔗 دمج {total} segment...")

    strategies = [
        (["-c", "copy", "-bsf:a", "aac_adtstoasc",
          "-movflags", "+faststart",
          "-max_muxing_queue_size", "4096",
          "-fflags", "+genpts+igndts",
          "-f", "mp4"], "mp4-copy-aac"),
        (["-c", "copy", "-movflags", "+faststart",
          "-max_muxing_queue_size", "4096",
          "-fflags", "+genpts",
          "-f", "mp4"], "mp4-copy"),
        (["-c", "copy", "-max_muxing_queue_size", "4096",
          "-fflags", "+genpts",
          "-f", "mpegts"], "ts-copy"),
        (["-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
          "-movflags", "+faststart",
          "-max_muxing_queue_size", "4096",
          "-f", "mp4"], "mp4-copy-aac-re"),
        (["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
          "-c:a", "aac", "-b:a", "64k",
          "-movflags", "+faststart",
          "-f", "mp4"], "reencode"),
    ]

    # ═══ 1) محاولة الدمج المباشر ═══
    print(f"      📌 محاولة دمج مباشر لـ {total} segment...")
    listfile = tempfile.mktemp(suffix=".txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for p in sorted_paths:
            sp = str(p).replace("\\", "/").replace("'", r"'\''")
            f.write(f"file '{sp}'\n")

    for args, name in strategies:
        if os.path.exists(out):
            try: os.remove(out)
            except Exception: pass
        ok, err = _concat_attempt(listfile, out, args, timeout=2400)
        if ok:
            print(f"         ✅ نجح: {name} (مباشر)")
            try: os.remove(listfile)
            except Exception: pass
            return True
        print(f"         ❌ {name}: {err[:180]}")

    try: os.remove(listfile)
    except Exception: pass

    # ═══ 2) chunked دمج ═══
    print(f"      📦 محاولة الدمج على دفعات ({CONCAT_CHUNK} segment/دفعة)...")
    groups = [sorted_paths[i:i + CONCAT_CHUNK]
              for i in range(0, total, CONCAT_CHUNK)]
    group_files = []

    for gi, grp in enumerate(groups):
        gpath = out + f".part{gi:04d}.ts"
        if os.path.exists(gpath):
            try: os.remove(gpath)
            except Exception: pass

        listfile = tempfile.mktemp(suffix=".txt")
        with open(listfile, "w", encoding="utf-8") as f:
            for p in grp:
                sp = str(p).replace("\\", "/").replace("'", r"'\''")
                f.write(f"file '{sp}'\n")

        ok = False
        last_err = ""
        for args, name in strategies:
            if os.path.exists(gpath):
                try: os.remove(gpath)
                except Exception: pass
            ok, err = _concat_attempt(listfile, gpath, args, timeout=900)
            if ok:
                break
            last_err = err

        try: os.remove(listfile)
        except Exception: pass

        if ok:
            group_files.append(gpath)
            print(f"         ✅ دفعة {gi+1}/{len(groups)} ({len(grp)} segment)")
        else:
            print(f"         ❌ دفعة {gi+1}: {last_err[:180]}")
            for gf in group_files:
                try: os.remove(gf)
                except Exception: pass
            return False

    # ═══ 3) دمج نهائي ═══
    print(f"      🔗 دمج نهائي ({len(group_files)} دفعة)...")
    final_list = tempfile.mktemp(suffix=".txt")
    with open(final_list, "w", encoding="utf-8") as f:
        for p in group_files:
            sp = str(p).replace("\\", "/").replace("'", r"'\''")
            f.write(f"file '{sp}'\n")

    final_ok = False
    for args, name in strategies:
        if os.path.exists(out):
            try: os.remove(out)
            except Exception: pass
        ok, err = _concat_attempt(final_list, out, args, timeout=1200)
        if ok:
            print(f"         ✅ نجح نهائياً: {name}")
            final_ok = True
            break
        print(f"         ❌ نهائي {name}: {err[:180]}")

    try: os.remove(final_list)
    except Exception: pass
    for gf in group_files:
        try: os.remove(gf)
        except Exception: pass

    return final_ok


# ═══════════════════════════════════════════════════════════════
# الضغط للوصول إلى 45MB
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
            print(f"   ✅ CRF: {im:.2f}→{om:.2f}MB في {time.time()-t0:.1f}s")
            if om <= max_size_mb or duration <= 0:
                return True
            print(f"   ⚠️ تجاوز {max_size_mb}MB → two-pass...")
        else:
            print(f"   ⚠️ CRF فشل: {(r.stderr or '')[-200:]}")
    except Exception as e:
        print(f"   ⚠️ CRF: {str(e)[:100]}")

    if duration > 0:
        return _compress_twopass(inp, out, max_size_mb, duration, ff)
    return False


def _compress_twopass(inp, out, max_size_mb, duration, ff):
    audio_bitrate = 32 * 1024
    target_bits = max_size_mb * 1024 * 1024 * 8
    audio_bits = audio_bitrate * duration
    video_bits = max(0, target_bits - audio_bits)
    video_bitrate = max(100, int(video_bits / duration / 1000))

    print(f"   🔄 two-pass: {video_bitrate}k video + 32k audio")

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
    except Exception as e:
        print(f"   ❌ two-pass: {str(e)[:100]}")
    finally:
        for f in [pass_log, pass_log + "-0.log", pass_log + "-0.log.mbtree"]:
            try: os.remove(f)
            except Exception: pass
    return False


# ═══════════════════════════════════════════════════════════════
# ★★★ yt-dlp مع Speed Watchdog
# ═══════════════════════════════════════════════════════════════
def _ytdlp_hls(m3u8_url, out, iframe_url, cookies=None,
               min_speed_kb=None, max_stall_seconds=None):
    """
    ★ Speed watchdog: يوقف yt-dlp إذا نزلت السرعة.
    يعيد: (نجح, reason)
    """
    if min_speed_kb is None:
        min_speed_kb = YTDLP_MIN_SPEED_KB
    if max_stall_seconds is None:
        max_stall_seconds = YTDLP_MAX_STALL

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
        min_speed_bytes = min_speed_kb * 1024

        for mode in ["native", "ffmpeg"]:
            cmd = [
                "yt-dlp", "--no-warnings", "--no-playlist", "--no-part",
                "--newline", "--progress",
                "--progress-template",
                "download:PROGRESS:%(progress._percent_str)s|"
                "%(progress._speed_str)s|ETA:%(progress._eta_str)s|"
                "speed_bytes:%(progress.speed)s",
                "--hls-prefer-" + mode,
                "--user-agent", UA,
                "--referer", origin_url + "/",
                "--add-header", f"Origin:{origin_url}",
                "--cookies", cookie_file,
                "--retries", "3", "--fragment-retries", "3",
                "--socket-timeout", "30",
                "--concurrent-fragments", str(YTDLP_CONCURRENT_FRAGMENTS),
                "--http-chunk-size", "10M",
                "--throttled-rate", YTDLP_THROTTLED_RATE,
                "--buffer-size", "1M",
                "-f", "bv*[height<=360]+ba/b[height<=360]/bv*+ba/b",
                "--merge-output-format", "mp4",
                "-o", out, m3u8_url,
            ]
            if ff != "ffmpeg":
                cmd.extend(["--ffmpeg-location", ff])

            print(f"      🎬 yt-dlp {mode} → {m3u8_url[:60]}...")
            print(f"         ⚠️ watchdog: min={min_speed_kb}KB/s, "
                  f"stall={max_stall_seconds}s")

            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )

                last_pct = -10
                t0 = time.time()
                last_speed_ok_ts = time.time()
                last_log_ts = time.time()
                killed = False
                reason = ""

                for line in iter(proc.stdout.readline, ""):
                    line = line.rstrip()
                    if not line:
                        continue

                    if "PROGRESS:" in line:
                        try:
                            after = line.split("PROGRESS:", 1)[1].strip()
                            parts = after.split("|")
                            pct = float(parts[0].replace("%", "").strip())

                            speed_b = 0
                            for p in parts:
                                if "speed_bytes:" in p:
                                    try:
                                        speed_b = float(p.replace("speed_bytes:", "").strip())
                                    except (ValueError, TypeError):
                                        speed_b = 0
                                    break

                            if pct >= last_pct + 10 or pct >= 99.5:
                                last_pct = int(pct // 10) * 10
                                speed_mb = speed_b / 1048576
                                elapsed = time.time() - t0
                                print(f"         📊 {pct:5.1f}% | "
                                      f"{speed_mb:.2f}MB/s | ({elapsed:.0f}s)")

                            now = time.time()
                            if speed_b >= min_speed_bytes:
                                last_speed_ok_ts = now
                            else:
                                stalled = now - last_speed_ok_ts
                                if stalled >= max_stall_seconds:
                                    print(f"         ⚠️ السرعة < {min_speed_kb}KB/s "
                                          f"لمدة {stalled:.0f}s — إيقاف yt-dlp")
                                    killed = True
                                    reason = f"slow_speed ({speed_b/1024:.0f}KB/s)"
                                    proc.kill()
                                    break

                            if now - last_log_ts > 20 and pct < last_pct + 10:
                                speed_mb = speed_b / 1048576
                                elapsed = time.time() - t0
                                print(f"         ⏳ {pct:5.1f}% | {speed_mb:.2f}MB/s "
                                      f"| ({elapsed:.0f}s)")
                                last_log_ts = now

                        except (ValueError, IndexError):
                            pass

                if killed:
                    try: proc.wait(timeout=5)
                    except Exception: pass
                    return False, reason

                proc.wait()
                ret_code = proc.returncode
                elapsed = time.time() - t0

                if (ret_code == 0 and os.path.exists(out)
                        and os.path.getsize(out) > MIN_SIZE):
                    print(f"      ✅ yt-dlp {mode}: "
                          f"{os.path.getsize(out)/1048576:.1f}MB في {elapsed:.0f}s")
                    return True, "ok"
                print(f"      ⚠️ yt-dlp {mode}: code={ret_code}, {elapsed:.0f}s")
            except Exception as e:
                print(f"      ⚠️ yt-dlp {mode}: {str(e)[:100]}")

        return False, "all_failed"
    finally:
        try: os.remove(cookie_file)
        except Exception: pass


# ═══════════════════════════════════════════════════════════════
# Browser fetch
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
    # cache check
    if cache_key and cache_key in _SEGMENT_CACHE:
        cached_dir, cached_paths = _SEGMENT_CACHE[cache_key]
        if os.path.exists(cached_dir):
            print(f"      💾 cache ({len(cached_paths)} segment)")
            sorted_p = [cached_paths[k] for k in sorted(cached_paths.keys())]
            if _smart_concat(sorted_p, out):
                ok, msg = _verify_merged(str(out))
                if ok:
                    return (os.path.getsize(out), True)
                print(f"      ⚠️ الملف تالف: {msg}")
            shutil.rmtree(cached_dir, ignore_errors=True)
            _SEGMENT_CACHE.pop(cache_key, None)

    batch = 16
    total = len(segments)
    print(f"      🌐 المتصفح: {total} segment...")

    seg_dir = tempfile.mkdtemp(prefix="hls_br_")
    paths, failed, total_bytes = {}, 0, 0

    for i in range(0, total, batch):
        chunk = segments[i:i + batch]
        result = _browser_fetch_batch(sb, chunk, with_creds=True)
        if not result or all(v is None for v in result.values()):
            result = _browser_fetch_batch(sb, chunk, with_creds=False)

        retry = []
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
                    retry.append((si, chunk[li]))
            else:
                retry.append((si, chunk[li]))

        if retry:
            rr = _browser_fetch_batch(sb, [u for _, u in retry], with_creds=False)
            for k, b64 in rr.items():
                try:
                    li = int(k)
                except Exception:
                    continue
                si, _ = retry[li]
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
        if done % (batch * 2) == 0 or done == total:
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
        print(f"      ⚠️ الملف تالف: {msg}")
        try: os.remove(out)
        except Exception: pass
        if cache_key:
            _SEGMENT_CACHE.pop(cache_key, None)
        return None

    final_size = os.path.getsize(out)
    print(f"      ✅ الملف صالح ({msg}): {final_size/1048576:.1f} MB")
    return (final_size, True)


# ═══════════════════════════════════════════════════════════════
# cffi
# ═══════════════════════════════════════════════════════════════
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
    for imp in ["chrome124", "chrome120", "chrome110"]:
        try:
            r = cffi_requests.get(segments[0], headers=headers,
                                  cookies=cookies, impersonate=imp,
                                  timeout=15, verify=False)
            if r.status_code == 200 and len(r.content) > 100:
                test_ok = True
                break
        except Exception:
            continue

    if not test_ok:
        print(f"      ⚠️ cffi فشل اختبار → المتصفح")
        if sb is not None:
            return _browser_segments(sb, segments, out, cache_key)
        return None

    seg_dir = tempfile.mkdtemp(prefix="hls_cffi_")
    paths, failed, total = {}, 0, 0

    def _dl(iu):
        i, u = iu
        for imp in ["chrome124", "chrome120"]:
            try:
                r = cffi_requests.get(u, headers=headers, cookies=cookies,
                                      impersonate=imp, timeout=30, verify=False)
                if r.status_code == 200 and len(r.content) > 100:
                    p = os.path.join(seg_dir, f"s_{i:06d}.ts")
                    with open(p, "wb") as f:
                        f.write(r.content)
                    return (i, p, len(r.content))
            except Exception:
                continue
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
            print(f"      ⚠️ cffi فشل جزئي → المتصفح")
            return _browser_segments(sb, segments, out, cache_key)
        return None

    sorted_p = [paths[k] for k in sorted(paths)]

    if not _smart_concat(sorted_p, out):
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    ok, msg = _verify_merged(str(out))
    if not ok:
        print(f"      ⚠️ الملف تالف: {msg}")
        try: os.remove(out)
        except Exception: pass
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    final_size = os.path.getsize(out)
    print(f"      ✅ الملف صالح ({msg}): {final_size/1048576:.1f} MB")
    shutil.rmtree(seg_dir, ignore_errors=True)
    return (final_size, True)


# ═══════════════════════════════════════════════════════════════
# أدوات المتصفح
# ═══════════════════════════════════════════════════════════════
def _dismiss_ads(sb):
    try:
        sb.cdp.execute_script("""
            (function(){
                try {
                    ['iframe[id*="ad"]', 'div[class*="popup"]',
                     '[class*="overlay"]', '.ads'].forEach(function(sel){
                        document.querySelectorAll(sel).forEach(function(el){
                            el.style.display = 'none';
                        });
                    });
                    var v = document.querySelector('video');
                    if (v) v.muted = true;
                    document.body.style.overflow = 'auto';
                } catch(e){}
            })();
        """)
    except Exception:
        pass


def _aggressive_play(sb):
    _dismiss_ads(sb)
    for sel in [".jw-icon-playback", ".jw-display-icon-container",
                ".vjs-big-play-button", "[class*='play']", "video"]:
        for _ in range(2):
            try:
                sb.cdp.click_if_visible(sel)
            except Exception:
                pass
    try:
        sb.cdp.execute_script("""
            (function(){try{
                if(typeof jwplayer!=='undefined'){
                    var p=jwplayer(); if(p&&p.play){p.play(true);}
                    if(p&&p.setMute){p.setMute(true);}
                }
                var v=document.querySelector('video');
                if(v){v.muted=true;v.play&&v.play().catch(function(){});}
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
# ★★★ Cloudflare bypass مع timeout
# ═══════════════════════════════════════════════════════════════
def _bypass_cloudflare(sb, iframe_url, timeout_seconds=None):
    """
    ★ تجاوز Cloudflare مع timeout إجباري.
    يعيد True/False.
    """
    if timeout_seconds is None:
        timeout_seconds = CF_TIMEOUT

    print(f"      🔄 محاولة تجاوز CF (حد {timeout_seconds}s)...")
    start = time.time()

    try:
        # انتقل للصفحة أولاً
        try:
            sb.driver.execute_cdp_cmd("Page.navigate", {
                "url": iframe_url,
                "referrer": "",
            })
        except Exception as e:
            print(f"      ⚠️ فشل التنقل: {str(e)[:80]}")
            return False
        sb.cdp.sleep(3)

        # جرّب النقر على captcha مع timeout
        for attempt in range(3):
            elapsed = time.time() - start
            if elapsed > timeout_seconds:
                print(f"      ⏰ تجاوز timeout ({elapsed:.0f}s)")
                return False

            try:
                sb.uc_gui_click_captcha()
            except Exception as e:
                print(f"      ⚠️ محاولة {attempt+1}: {str(e)[:80]}")

            sb.cdp.sleep(2)

            # تحقق من النجاح
            try:
                title = sb.get_page_title() or ""
                html = sb.cdp.get_page_source() or ""

                if ("Just a moment" not in title
                        and "Attention Required" not in title
                        and len(html) > 5000):
                    print(f"      ✅ تم تجاوز CF ({time.time()-start:.0f}s)")
                    return True
            except Exception:
                pass

        print(f"      ❌ فشل تجاوز CF ({time.time()-start:.0f}s)")
        return False
    except Exception as e:
        print(f"      ❌ خطأ في bypass: {str(e)[:100]}")
        return False


# ═══════════════════════════════════════════════════════════════
# ★★★ العملية الكاملة
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

                print(f"🖥️  فتح: {url[:90]}")
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

                # ═══ السيرفرات ═══
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
                            print(f"   ✅ {len(servers)} سيرفر")
                            break
                    except Exception:
                        pass

                if not servers:
                    return None

                # ═══ جمع روابط iframe ═══
                print(f"\n   📋 جمع iframes...")
                iframe_map = {}
                seen = set()

                for i in range(20):
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
                    deadline = time.time() + 5
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

                # ★★★ ترتيب الأولوية — luluvdo في الأخير
                def _prio(item):
                    k = item[0].lower()
                    for i, s in enumerate([
                        "vidaraa", "playmate", "firestream",
                        "vids", "vidsonic", "vinovo", "bysejikuar",
                        "vidsp", "savefiles",
                        "luluvdo",  # ★ الأخير لأنه يعلق مع CF
                    ]):
                        if s in k: return i
                    return 99

                ordered = sorted(iframe_map.items(), key=_prio)

                # ═══ حلقة السيرفرات ═══
                for sname, iframe_url in ordered:
                    server_start = time.time()
                    print(f"\n   ═══ {sname} ═══")

                    try:
                        # ★★★ luluvdo: bypass مع timeout
                        if "luluvdo" in sname.lower():
                            if not _bypass_cloudflare(sb, iframe_url, CF_TIMEOUT):
                                print(f"      ⏭️ تخطي luluvdo (فشل CF أو timeout)")
                                continue
                            sb.cdp.sleep(3)
                        else:
                            # السيرفرات العادية
                            try:
                                sb.driver.execute_cdp_cmd("Page.navigate", {
                                    "url": iframe_url, "referrer": watch_url,
                                })
                            except Exception:
                                continue
                            sb.cdp.sleep(3)

                        # ★ فحص timeout السيرفر
                        if time.time() - server_start > SERVER_TIMEOUT:
                            print(f"      ⏰ تجاوز وقت السيرفر")
                            continue

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
                        for _ in range(JWPLAYER_WAIT):
                            sb.cdp.sleep(1)
                            try:
                                p = sb.cdp.execute_script(
                                    "return typeof jwplayer!=='undefined'?'jw':"
                                    "(document.querySelector('video')?'h5':'none')"
                                )
                                if p in ("jw", "h5"):
                                    break
                            except Exception:
                                pass

                        # تشغيل
                        for cycle in range(15):
                            _aggressive_play(sb)
                            sb.cdp.sleep(1)
                            if any(x in u for u in _read() for x in [".m3u8", ".mpd"]):
                                break

                        if not any(x in u for u in _read() for x in [".m3u8", ".mpd"]):
                            for i in range(M3U8_WAIT):
                                sb.cdp.sleep(1)
                                _dismiss_ads(sb)
                                if any(x in u for u in _read() for x in [".m3u8", ".mpd"]):
                                    break

                        # جمع m3u8
                        m3u8_urls = [u for u in _read() if ".m3u8" in u or ".mpd" in u]
                        try:
                            perf = sb.cdp.execute_script("""
                                (function(){try{
                                    return performance.getEntriesByType('resource')
                                        .map(e=>e.name);
                                }catch(e){return [];}})();
                            """)
                            if isinstance(perf, list):
                                for u in perf:
                                    if (".m3u8" in u or ".mpd" in u) and u not in m3u8_urls:
                                        m3u8_urls.append(u)
                        except Exception:
                            pass

                        m3u8_urls = [u for u in m3u8_urls if ".m3u8" in u or ".mpd" in u]
                        m3u8_urls = [u for u in m3u8_urls
                                     if "ping.gif" not in u and "jwpltx" not in u]

                        if not m3u8_urls:
                            continue

                        idx = [u for u in m3u8_urls if "index" in u.lower()]
                        mst = [u for u in m3u8_urls if "master" in u.lower()]
                        oth = [u for u in m3u8_urls if u not in idx and u not in mst]
                        ordered_m3u8 = idx + mst + oth

                        cookies = _get_cookies(sb)

                        # ★★★ yt-dlp مع watchdog
                        for m_url in ordered_m3u8[:2]:
                            yt_ok, yt_reason = _ytdlp_hls(
                                m_url, str(out_path), iframe_url, cookies
                            )
                            if yt_ok:
                                ok, msg = _verify_merged(str(out_path))
                                if ok:
                                    return (os.path.getsize(out_path), True)
                                else:
                                    print(f"      ⚠️ yt-dlp أنتج ملفاً تالفاً: {msg}")
                                    try: os.remove(out_path)
                                    except Exception: pass
                            else:
                                if yt_reason.startswith("slow_speed"):
                                    print(f"      ⏭️ yt-dlp بطيء → المتصفح فوراً")
                                break

                        # ★★★ cffi → browser
                        for m_url in ordered_m3u8[:2]:
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
                                while time.time() - start < 12:
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

                            if not segs and variants:
                                for v in variants[:2]:
                                    try:
                                        r = cffi_requests.get(v, headers={
                                            "Referer": iframe_url,
                                            "Origin": _origin(iframe_url),
                                            "User-Agent": UA,
                                        }, cookies=cookies, impersonate="chrome120",
                                            timeout=15, verify=False)
                                        if r.status_code == 200:
                                            s2, _ = _parse_m3u8(r.text, v.rsplit("/", 1)[0])
                                            if s2:
                                                segs = s2
                                                m_url = v
                                                break
                                    except Exception:
                                        continue

                            if segs:
                                print(f"         ✅ {len(segs)} segment")
                                ck = _m3u8_cache_key(m_url)
                                res = _cffi_segments(
                                    segs, str(out_path), iframe_url, cookies, sb, ck
                                )
                                if res:
                                    return res

                        # ffmpeg HLS fallback
                        for m_url in ordered_m3u8[:2]:
                            cookies_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                            headers = (f"Referer: {iframe_url}\r\n"
                                       f"Origin: {_origin(iframe_url)}\r\n"
                                       f"User-Agent: {UA}\r\n"
                                       f"Cookie: {cookies_str}\r\n")
                            ff = _get_ffmpeg_exe()
                            cmd = [
                                ff, "-nostdin", "-hide_banner", "-loglevel", "warning",
                                "-headers", headers, "-user_agent", UA,
                                "-reconnect", "1", "-reconnect_streamed", "1",
                                "-i", m_url,
                                "-c", "copy", "-bsf:a", "aac_adtstoasc",
                                "-movflags", "+faststart", "-y", str(out_path),
                            ]
                            try:
                                r = subprocess.run(cmd, capture_output=True,
                                                   text=True, timeout=1800)
                                if (r.returncode == 0 and os.path.exists(out_path)
                                        and os.path.getsize(out_path) > MIN_SIZE):
                                    ok, msg = _verify_merged(str(out_path))
                                    if ok:
                                        return (os.path.getsize(out_path), True)
                            except Exception:
                                pass

                        print(f"   ⏭️ فشل: {sname}")

                    except Exception as e:
                        print(f"   ❌ خطأ في {sname}: {str(e)[:150]}")
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
    try:
        res = _process_with_browser(url, raw)
    except Exception as e:
        print(f"    ⚠️ خطأ: {str(e)[:150]}")
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
        "https://u.3seq.cam/video/modablaj-muhtemel-ask-episode-01/"
    )
    result = download_episode("test", 1, test_url)
    print(f"\n{'✅ نجح: ' + str(result) if result else '⚠️ فشل'}")