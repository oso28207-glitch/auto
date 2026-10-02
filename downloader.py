"""
downloader.py — تحميل الفيديوهات من u.3seq.com
يستخدم SeleniumBase UC Mode + xvfb لتجاوز Cloudflare،
ثم يلتقط m3u8 ويحمّل الـ segments عبر curl_cffi ويدمجها بـ ffmpeg.
"""

import base64
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

import requests
from curl_cffi import requests as cffi_requests

from config import config, MEDIA_DIR
from errors import DownloadError


# ═══════════════════════════════════════════════════════════════
# إعدادات
# ═══════════════════════════════════════════════════════════════
IMPERSONATE_TARGET = os.environ.get("IMPERSONATE_TARGET", "chrome120").strip()
CURL_CFFI_WORKERS = int(os.environ.get("CURL_CFFI_WORKERS", "8"))
SEGMENT_RETRY = 2
M3U8_CANDIDATE_LIMIT = 5
JWPLAYER_WAIT = 25
M3U8_CAPTURE_WAIT = 15

# إعدادات الضغط (كما في الكود القديم)
COMPRESS_PRESET = "veryfast"
COMPRESS_CRF = 28
COMPRESS_THREADS = 2
COMPRESS_SCALE = 144

# الحد الأدنى للمدة (بالثواني) لاعتبار الحلقة صالحة
MIN_EPISODE_DURATION = 900
MIN_VALID_SIZE = 100 * 1024


# ═══════════════════════════════════════════════════════════════
# أدوات مساعدة
# ═══════════════════════════════════════════════════════════════
def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()[:120]


def _origin(url: str) -> str:
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return ""


def _get_referer(url: str) -> str:
    return _origin(url) + "/"


# ═══════════════════════════════════════════════════════════════
# 1) تحليل m3u8
# ═══════════════════════════════════════════════════════════════
def parse_m3u8_content(text: str, base_url: str):
    """يستخرج segments و variants من محتوى m3u8."""
    segs, variants = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ".m3u8" in line:
            variants.append(line if line.startswith("http") else urljoin(base_url + "/", line))
            continue
        if line.endswith(".ts") or ".ts?" in line or "seg" in line.lower() or ".m4s" in line:
            segs.append(line if line.startswith("http") else urljoin(base_url + "/", line))
    return segs, variants


def extract_m3u8_from_text(text: str) -> list:
    """يبحث عن روابط m3u8/mpd في نص HTML."""
    if not text:
        return []
    found, seen = [], set()
    for pat in [
        r'(https?:[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*)',
        r'(https?:[^\s"\'<>\\]+\.mpd[^\s"\'<>\\]*)',
    ]:
        for m in re.finditer(pat, text):
            u = m.group(1).replace("\\/", "/")
            if u not in seen:
                seen.add(u)
                found.append(u)
    return found


# ═══════════════════════════════════════════════════════════════
# 2) تحميل الـ segments عبر curl_cffi (سريع)
# ═══════════════════════════════════════════════════════════════
def try_cffi_segments(segments, out_path, iframe_url):
    """
    يحمّل segments عبر curl_cffi مع TLS fingerprint لـ Chrome.
    يرجع (size, True) عند النجاح، أو None عند الفشل.
    """
    if not segments:
        return None

    ref_origin = _origin(iframe_url) or ""
    headers = {
        "Referer": iframe_url,
        "Origin": ref_origin,
        "User-Agent": config.USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }

    # اختبار سريع على أول segment
    test_data = None
    for imp in ["chrome124", "chrome120", "chrome110"]:
        try:
            r = cffi_requests.get(
                segments[0], headers=headers, impersonate=imp,
                timeout=15, verify=False,
            )
            if r.status_code == 200 and len(r.content) > 100:
                test_data = r.content
                print(f"      ⚡ cffi[{imp}] segment test OK ({len(test_data)}b)")
                break
        except Exception:
            continue

    if test_data is None:
        print("      ⚠️ cffi segment test فشل")
        return None

    # تحميل الكل
    print(f"   ⚡ {len(segments)} segment عبر cffi parallel...")
    seg_dir = tempfile.mkdtemp(prefix="hls_cffi_")
    seg_paths, failed, total_bytes = {}, 0, 0

    def _dl(idx_url):
        idx, url = idx_url
        for imp in ["chrome124", "chrome120"]:
            try:
                r = cffi_requests.get(
                    url, headers=headers, impersonate=imp,
                    timeout=30, verify=False,
                )
                if r.status_code == 200 and len(r.content) > 100:
                    p = os.path.join(seg_dir, f"seg_{idx:06d}.ts")
                    with open(p, "wb") as f:
                        f.write(r.content)
                    return (idx, p, len(r.content))
            except Exception:
                continue
        return (idx, None, 0)

    with ThreadPoolExecutor(max_workers=CURL_CFFI_WORKERS) as ex:
        futures = {ex.submit(_dl, (i, s)): i for i, s in enumerate(segments)}
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
                print(f"      📦 {done}/{len(segments)} | {total_bytes/(1024*1024):.1f} MB | فشل: {failed}")

    if not seg_paths or failed > len(segments) * 0.15:
        shutil.rmtree(seg_dir, ignore_errors=True)
        print(f"      ❌ cffi: نسبة فشل عالية ({failed}/{len(segments)})")
        return None

    # دمج
    sorted_segs = [seg_paths[k] for k in sorted(seg_paths.keys())]
    concat_file = os.path.join(seg_dir, "concat.txt")
    with open(concat_file, "w") as f:
        for p in sorted_segs:
            f.write(f"file '{p}'\n")

    print(f"   🔗 دمج {len(sorted_segs)} segment...")
    concat_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0", "-i", concat_file,
        "-c", "copy", "-f", "mpegts", "-y", out_path,
    ]
    try:
        r = subprocess.run(concat_cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not os.path.exists(out_path):
            shutil.rmtree(seg_dir, ignore_errors=True)
            return None
    except Exception:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    final_size = os.path.getsize(out_path)
    print(f"   ✅ cffi نجح: {final_size/(1024*1024):.1f} MB")
    shutil.rmtree(seg_dir, ignore_errors=True)
    return (final_size, True)


# ═══════════════════════════════════════════════════════════════
# 3) تحميل segments عبر المتصفح (fallback)
# ═══════════════════════════════════════════════════════════════
def download_segments_via_browser(sb, segments, out_path):
    """يحمّل segments عبر المتصفح باستخدام fetch مع base64."""
    total = len(segments)
    batch = 16
    print(f"   🌐 المتصفح: {total} segment (batch={batch})...")
    seg_dir = tempfile.mkdtemp(prefix="hls_br_")
    seg_paths, failed, total_bytes = {}, 0, 0

    for i in range(0, total, batch):
        chunk = segments[i:i + batch]
        urls_json = json.dumps(chunk)
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
            failed += len(chunk)
            continue

        # انتظر النتيجة
        start = time.time()
        result = None
        while time.time() - start < 30:
            sb.cdp.sleep(0.15)
            try:
                if sb.cdp.execute_script("return window.__bd === true"):
                    result = sb.cdp.execute_script("return window.__br")
                    break
            except Exception:
                pass

        if not isinstance(result, dict):
            failed += len(chunk)
            continue

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
                    with open(p, "wb") as f:
                        f.write(data)
                    seg_paths[si] = p
                    total_bytes += len(data)
                except Exception:
                    failed += 1
            else:
                failed += 1

        done = min(i + batch, total)
        if done % (batch * 2) == 0 or done == total:
            print(f"      📦 {done}/{total} | {total_bytes/(1024*1024):.1f} MB | فشل: {failed}")

    if not seg_paths:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    # دمج
    sorted_segs = [seg_paths[k] for k in sorted(seg_paths.keys())]
    concat_file = os.path.join(seg_dir, "concat.txt")
    with open(concat_file, "w") as f:
        for p in sorted_segs:
            f.write(f"file '{p}'\n")

    print(f"   🔗 دمج {len(sorted_segs)} segment...")
    concat_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0", "-i", concat_file,
        "-c", "copy", "-f", "mpegts", "-y", out_path,
    ]
    try:
        r = subprocess.run(concat_cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not os.path.exists(out_path):
            shutil.rmtree(seg_dir, ignore_errors=True)
            return None
    except Exception:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    final_size = os.path.getsize(out_path)
    print(f"   ✅ ملف نهائي: {final_size/(1024*1024):.1f} MB")
    shutil.rmtree(seg_dir, ignore_errors=True)
    return (final_size, True)


# ═══════════════════════════════════════════════════════════════
# 4) العملية الكاملة عبر SeleniumBase
# ═══════════════════════════════════════════════════════════════
def process_episode_via_browser(episode_url: str, out_path: Path):
    """
    يفتح صفحة الحلقة في متصفح حقيقي (UC Mode) لتجاوز Cloudflare،
    يلتقط m3u8، يحمّل segments، ويدمجها.
    """
    from seleniumbase import SB

    iframe_url = None
    download_result = None
    m3u8_urls = []

    netlog = tempfile.mktemp(suffix="_netlog.txt")
    with open(netlog, "w") as f:
        f.write("")

    def _log(u):
        try:
            with open(netlog, "a", encoding="utf-8") as fh:
                fh.write(u + "\n")
                fh.flush()
        except Exception:
            pass

    def _read_log():
        try:
            with open(netlog, encoding="utf-8") as fh:
                return [l.strip() for l in fh if l.strip()]
        except Exception:
            return []

    try:
        with SB(
            uc=True, xvfb=True, headless=False, incognito=True,
            ad_block_on=False, disable_csp=True,
            page_load_strategy="eager", locale_code="en",
        ) as sb:
            try:
                sb.activate_cdp_mode()

                # اعتراض الشبكة
                try:
                    import mycdp

                    async def on_req(e):
                        try:
                            u = e.request.url
                            _log(u)
                            if ".m3u8" in u or ".mpd" in u:
                                print(f"      ✅ [net] {u[:120]}")
                        except Exception:
                            pass

                    sb.cdp.add_handler(mycdp.network.RequestWillBeSent, on_req)
                except Exception:
                    pass

                # ═══ 1) اذهب إلى الصفحة ═══
                print(f"🖥️  فتح: {episode_url}")
                sb.cdp.open(episode_url)
                sb.cdp.sleep(2)

                # ═══ 2) استخرج iframe ═══
                try:
                    sb.wait_for_element("ul.serversList", timeout=12)
                    print("   ✅ السيرفرات ظهرت")
                except Exception:
                    print("   ⚠️ لا سيرفرات — استمر")

                html = sb.get_page_source()
                # جرّب iframe مباشرة
                try:
                    ifr = sb.find_element(".watch iframe")
                    src = ifr.get_attribute("src")
                    if src and src.startswith("http"):
                        iframe_url = src.replace("&amp;", "&")
                except Exception:
                    pass

                if not iframe_url:
                    print("   ❌ لا iframe URL")
                    return None

                print(f"   📋 iframe: {iframe_url[:90]}")

                # ═══ 3) انتقل إلى iframe ═══
                sb.driver.execute_cdp_cmd("Page.navigate", {
                    "url": iframe_url,
                    "referrer": episode_url,
                })
                sb.cdp.sleep(3)

                # ═══ 4) انتظر المشغل ═══
                for tick in range(JWPLAYER_WAIT):
                    sb.cdp.sleep(1)
                    try:
                        player = sb.cdp.execute_script(
                            "return typeof jwplayer !== 'undefined' ? 'jw' : "
                            "(document.querySelector('video') ? 'html5' : 'none')"
                        )
                        if player in ("jw", "html5"):
                            print(f"   ✅ {player} بعد {tick+1}s")
                            break
                    except Exception:
                        pass

                # ═══ 5) شغّل والتقط m3u8 ═══
                for cycle in range(6):
                    # محاولة تشغيل
                    for sel in [
                        "video", ".jw-icon-playback", ".jw-icon-display",
                        ".jw-display-icon-container", ".vjs-big-play-button",
                    ]:
                        try:
                            sb.cdp.click_if_visible(sel)
                        except Exception:
                            pass
                    try:
                        sb.cdp.execute_script("""
                            (function(){
                                try {
                                    if (typeof jwplayer !== 'undefined') {
                                        var p = jwplayer();
                                        if (p && p.play) { p.play(true); return; }
                                    }
                                    var v = document.querySelector('video');
                                    if (v) { v.muted = true; v.play && v.play().catch(function(){}); }
                                } catch(e){}
                            })();
                        """)
                    except Exception:
                        pass

                    sb.cdp.sleep(1)
                    if any(x in u for u in _read_log() for x in [".m3u8", ".mpd"]):
                        print(f"   ✨ m3u8 بعد دورة {cycle+1}")
                        break

                if not any(x in u for u in _read_log() for x in [".m3u8", ".mpd"]):
                    for i in range(M3U8_CAPTURE_WAIT):
                        sb.cdp.sleep(1)
                        if any(x in u for u in _read_log() for x in [".m3u8", ".mpd"]):
                            print(f"   ✨ m3u8 بعد {(i+1)+6}s")
                            break

                # اجمع كل m3u8
                urls = _read_log()
                m3u8_now = [u for u in urls if any(x in u for x in [".m3u8", ".mpd"])]

                try:
                    perf = sb.cdp.execute_script("""
                        (function(){ try {
                            return performance.getEntriesByType('resource').map(e => e.name);
                        } catch(e){ return []; } })();
                    """)
                    if perf and isinstance(perf, list):
                        for u in perf:
                            if any(x in u for x in [".m3u8", ".mpd"]) and u not in m3u8_now:
                                m3u8_now.append(u)
                except Exception:
                    pass

                try:
                    html = sb.cdp.get_page_source() or ""
                    for u in extract_m3u8_from_text(html):
                        if u not in m3u8_now:
                            m3u8_now.append(u)
                except Exception:
                    pass

                idx_files = [u for u in m3u8_now if "index-" in u.lower()]
                master = [u for u in m3u8_now if "master" in u.lower()]
                mpd = [u for u in m3u8_now if ".mpd" in u.lower()]
                others = [u for u in m3u8_now if u not in idx_files and u not in master and u not in mpd]
                m3u8_urls = idx_files + master + mpd + others

                print(f"   🎯 {len(m3u8_urls)} مرشح m3u8")
                for u in m3u8_urls[:3]:
                    print(f"      · {u[:120]}")

                # ═══ 6) حمّل segments ═══
                if m3u8_urls:
                    for m3u8_url in m3u8_urls[:M3U8_CANDIDATE_LIMIT]:
                        if download_result:
                            break

                        print(f"   🎯 محاولة: {m3u8_url[:100]}")
                        content, method = _browser_fetch_text(sb, m3u8_url)
                        if not content:
                            print("      ❌ فشل")
                            continue
                        print(f"      ✅ {method} → {len(content)}b")

                        segments, variants = parse_m3u8_content(
                            content, m3u8_url.rsplit("/", 1)[0]
                        )
                        if not segments and variants:
                            for v in variants[:2]:
                                vc, vm = _browser_fetch_text(sb, v)
                                if vc:
                                    segs2, _ = parse_m3u8_content(vc, v.rsplit("/", 1)[0])
                                    if segs2:
                                        segments = segs2
                                        m3u8_url = v
                                        print(f"      ✅ variant: {len(segments)} seg")
                                        break

                        if segments:
                            # ⚡ cffi أولاً
                            result_cffi = try_cffi_segments(segments, str(out_path), iframe_url)
                            if result_cffi:
                                download_result = result_cffi
                                break

                            # fallback: المتصفح
                            print("      🌐 المتصفح fallback")
                            download_result = download_segments_via_browser(
                                sb, segments, str(out_path)
                            )
                            if download_result:
                                break

            except Exception as e:
                print(f"   ❌ {str(e)[:150]}")
    except Exception as e:
        print(f"   ❌ {str(e)[:150]}")

    try:
        os.remove(netlog)
    except Exception:
        pass

    return download_result


def _browser_fetch_text(sb, url, timeout=12):
    """يجلب نص URL داخل المتصفح (لتجاوز CORS)."""
    url_json = json.dumps(url)
    for idx, expr in enumerate([
        f"fetch({url_json}, {{mode:'cors'}})",
        f"fetch({url_json}, {{mode:'cors', credentials:'include'}})",
    ]):
        dv, rv = f"__d{idx+10}", f"__r{idx+10}"
        js = f"""
        (function(){{
            window.{dv}=false; window.{rv}=null;
            try {{
                {expr}
                    .then(function(r){{ r.text().then(function(t){{
                        window.{rv}={{ok:true,status:r.status,text:t}};
                        window.{dv}=true;
                    }}).catch(function(e){{
                        window.{rv}={{ok:false,error:String(e)}};
                        window.{{dv}}=true;
                    }});
                    }}).catch(function(e){{
                        window.{rv}={{ok:false,error:String(e)}};
                        window.{dv}=true;
                    }});
            }} catch(e) {{
                window.{rv}={{ok:false,error:String(e)}};
                window.{dv}=true;
            }}
        }})();
        """
        try:
            sb.cdp.execute_script(js)
        except Exception:
            continue

        start = time.time()
        while time.time() - start < timeout:
            sb.cdp.sleep(0.15)
            try:
                if sb.cdp.execute_script(f"return window.{dv} === true"):
                    r = sb.cdp.execute_script(f"return window.{rv}")
                    if r and r.get("ok") and r.get("status") == 200 and r.get("text"):
                        return r.get("text"), "fetch"
                    break
            except Exception:
                pass
    return None, None


# ═══════════════════════════════════════════════════════════════
# 5) الضغط
# ═══════════════════════════════════════════════════════════════
def compress_144p(inp: Path, out: Path) -> bool:
    """يضغط الفيديو إلى 144p (كما في الكود القديم)."""
    if not inp.exists():
        return False
    im = inp.stat().st_size / (1024 * 1024)
    print(f"   🗜️ {im:.2f} MB → {COMPRESS_SCALE}p (preset={COMPRESS_PRESET})...")
    cmd = [
        "ffmpeg", "-err_detect", "ignore_err",
        "-fflags", "+discardcorrupt+genpts",
        "-analyzeduration", "50M", "-probesize", "50M",
        "-i", str(inp),
        "-vf", f"scale=-2:{COMPRESS_SCALE}",
        "-c:v", "libx264",
        "-crf", str(COMPRESS_CRF),
        "-preset", COMPRESS_PRESET,
        "-threads", str(COMPRESS_THREADS),
        "-c:a", "aac", "-b:a", "64k",
        "-f", "mp4", "-movflags", "+faststart",
        "-max_muxing_queue_size", "4096",
        "-y", str(out),
    ]
    try:
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not out.exists() or out.stat().st_size < 10 * 1024:
            print(f"   ❌ ffmpeg code={r.returncode}")
            return False
        om = out.stat().st_size / (1024 * 1024)
        dt = time.time() - t0
        print(f"   ✅ {im:.2f}→{om:.2f} MB في {dt:.1f}s")
        return True
    except Exception as e:
        print(f"   ❌ {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# 6) الدالة الرئيسية
# ═══════════════════════════════════════════════════════════════
def download_episode(series_name: str, episode: int, url: str) -> Path:
    """
    يحمّل الحلقة بالكامل:
      1. يفتح الصفحة في متصفح حقيقي (SeleniumBase UC).
      2. يلتقط m3u8 ويحمّل segments.
      3. يدمجها في ملف واحد.
      4. يضغطها إلى 144p (اختياري).
    """
    safe_series = _safe_name(series_name)
    out_dir = MEDIA_DIR / safe_series
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_path = out_dir / f"ep{episode:03d}_raw.ts"
    final_path = out_dir / f"ep{episode:03d}.mp4"

    # ─── موجودة مسبقاً؟ ───
    if final_path.exists() and final_path.stat().st_size > MIN_VALID_SIZE:
        mb = final_path.stat().st_size / 1024 / 1024
        print(f"    ↳ موجودة مسبقاً: {final_path.name} ({mb:.1f}MB)")
        return final_path

    print(f"    ↳ تحميل الحلقة {episode}: {url}")

    # ─── التحميل عبر المتصفح ───
    result = process_episode_via_browser(url, raw_path)

    if not result or not raw_path.exists():
        raise DownloadError(
            f"فشل تحميل {series_name} — الحلقة {episode}\n"
            f"   URL: {url}\n"
            f"   السبب: لم يتمكن المتصفح من التقاط m3u8 أو تحميل الـ segments."
        )

    size = raw_path.stat().st_size
    print(f"    📦 تم التحميل: {size/(1024*1024):.1f} MB")

    # ─── الضغط ───
    if os.environ.get("SKIP_COMPRESS", "false").lower() == "true":
        shutil.move(str(raw_path), str(final_path))
    else:
        if not compress_144p(raw_path, final_path):
            print("    ⚠️ فشل الضغط — استخدام الملف الأصلي")
            shutil.move(str(raw_path), str(final_path))

    # ─── تنظيف ───
    if raw_path.exists():
        raw_path.unlink()

    if not final_path.exists():
        raise DownloadError(f"لم يتم إنتاج ملف نهائي للحلقة {episode}")

    final_mb = final_path.stat().st_size / 1024 / 1024
    print(f"    ✅ اكتمل: {final_path.name} ({final_mb:.1f}MB)")
    return final_path


# ═══════════════════════════════════════════════════════════════
# اختبار سريع
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys

    test_url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://u.3seq.com/video/modablaj-uzak-sehir-episode-141-rkoy/"
    )

    print("🧪 اختبار downloader.py")
    print(f"URL: {test_url}")
    print(f"IMPERSONATE: {IMPERSONATE_TARGET}")
    print()

    try:
        path = download_episode("test", 1, test_url)
        print(f"\n✅ نجح: {path}")
    except DownloadError as e:
        print(f"\n❌ فشل:\n{e}")
        sys.exit(1)
