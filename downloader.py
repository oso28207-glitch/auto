"""تحميل من u.3seq.com عبر SeleniumBase + curl_cffi، مع ضغط ffmpeg."""

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

IMPERSONATE = os.environ.get("IMPERSONATE_TARGET", "chrome120")
CURL_WORKERS = int(os.environ.get("CURL_CFFI_WORKERS", "8"))
JWPLAYER_WAIT = 25
M3U8_WAIT = 15
M3U8_LIMIT = 5
MIN_SIZE = 100 * 1024


def _safe(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()[:120]


def _origin(url):
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return ""


def _parse_m3u8(text, base):
    segs, variants = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ".m3u8" in line:
            variants.append(line if line.startswith("http") else urljoin(base + "/", line))
        elif line.endswith(".ts") or ".ts?" in line or "seg" in line.lower() or ".m4s" in line:
            segs.append(line if line.startswith("http") else urljoin(base + "/", line))
    return segs, variants


def _extract_m3u8_from_html(html):
    if not html:
        return []
    found, seen = [], set()
    for pat in [r'(https?:[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*)',
                r'(https?:[^\s"\'<>\\]+\.mpd[^\s"\'<>\\]*)']:
        for m in re.finditer(pat, html):
            u = m.group(1).replace("\\/", "/")
            if u not in seen:
                seen.add(u); found.append(u)
    return found


# ═══ تحميل segments عبر cffi ═══
def _cffi_segments(segments, out, iframe_url):
    if not segments:
        return None

    headers = {
        "Referer": iframe_url,
        "Origin": _origin(iframe_url),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }

    # اختبار أول segment
    ok = False
    for imp in ["chrome124", "chrome120", "chrome110"]:
        try:
            r = cffi_requests.get(segments[0], headers=headers,
                                  impersonate=imp, timeout=15, verify=False)
            if r.status_code == 200 and len(r.content) > 100:
                ok = True
                print(f"      ⚡ cffi[{imp}] OK")
                break
        except Exception:
            continue
    if not ok:
        return None

    seg_dir = tempfile.mkdtemp(prefix="hls_")
    paths, failed, total = {}, 0, 0

    def _dl(iu):
        i, u = iu
        for imp in ["chrome124", "chrome120"]:
            try:
                r = cffi_requests.get(u, headers=headers, impersonate=imp,
                                      timeout=30, verify=False)
                if r.status_code == 200 and len(r.content) > 100:
                    p = os.path.join(seg_dir, f"s_{i:06d}.ts")
                    with open(p, "wb") as f:
                        f.write(r.content)
                    return (i, p, len(r.content))
            except Exception:
                continue
        return (i, None, 0)

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
                print(f"      📦 {done}/{len(segments)} | {total/1048576:.1f}MB | فشل: {failed}")

    if not paths or failed > len(segments) * 0.15:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    sorted_p = [paths[k] for k in sorted(paths)]
    concat = os.path.join(seg_dir, "list.txt")
    with open(concat, "w") as f:
        for p in sorted_p:
            f.write(f"file '{p}'\n")

    print(f"   🔗 دمج {len(sorted_p)} segment...")
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "warning",
         "-f", "concat", "-safe", "0", "-i", concat,
         "-c", "copy", "-f", "mpegts", "-y", out],
        capture_output=True, text=True, timeout=600,
    )
    shutil.rmtree(seg_dir, ignore_errors=True)

    if r.returncode != 0 or not os.path.exists(out):
        return None
    return (os.path.getsize(out), True)


# ═══ تحميل عبر المتصفح (fallback) ═══
def _browser_segments(sb, segments, out):
    batch = 16
    total = len(segments)
    print(f"   🌐 المتصفح: {total} segment...")
    seg_dir = tempfile.mkdtemp(prefix="hls_br_")
    paths, failed, total_bytes = {}, 0, 0

    for i in range(0, total, batch):
        chunk = segments[i:i + batch]
        js = """
        (function(){
            window.__r = {}; window.__d = false;
            var urls = %s; var res = {}; var pend = urls.length;
            if (!pend) { window.__r = res; window.__d = true; return; }
            urls.forEach(function(u, i){
                var done = function(v){
                    res[String(i)] = v; pend--;
                    if (!pend) { window.__r = res; window.__d = true; }
                };
                try {
                    fetch(u, {mode:'cors'})
                        .then(function(r){ if(!r.ok) throw 0; return r.arrayBuffer(); })
                        .then(function(buf){
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
        """ % json.dumps(chunk)

        try:
            sb.cdp.execute_script(js)
        except Exception:
            failed += len(chunk); continue

        start = time.time()
        result = None
        while time.time() - start < 30:
            sb.cdp.sleep(0.15)
            try:
                if sb.cdp.execute_script("return window.__d===true"):
                    result = sb.cdp.execute_script("return window.__r")
                    break
            except Exception:
                pass

        if not isinstance(result, dict):
            failed += len(chunk); continue

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

    if not paths:
        shutil.rmtree(seg_dir, ignore_errors=True)
        return None

    sorted_p = [paths[k] for k in sorted(paths)]
    concat = os.path.join(seg_dir, "list.txt")
    with open(concat, "w") as f:
        for p in sorted_p:
            f.write(f"file '{p}'\n")

    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "warning",
         "-f", "concat", "-safe", "0", "-i", concat,
         "-c", "copy", "-f", "mpegts", "-y", out],
        capture_output=True, text=True, timeout=600,
    )
    shutil.rmtree(seg_dir, ignore_errors=True)

    if r.returncode != 0 or not os.path.exists(out):
        return None
    return (os.path.getsize(out), True)


# ═══ جلسة المتصفح الكاملة ═══
def _process_with_browser(url, out_path):
    from seleniumbase import SB

    netlog = tempfile.mktemp(suffix=".txt")
    open(netlog, "w").close()
    iframe_url = None
    result = None

    def _log(u):
        try:
            with open(netlog, "a", encoding="utf-8") as fh:
                fh.write(u + "\n"); fh.flush()
        except Exception:
            pass

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

                print(f"🖥️  فتح: {url}")
                sb.cdp.open(url)
                sb.cdp.sleep(2)

                try:
                    sb.wait_for_element("ul.serversList", timeout=12)
                    print("   ✅ السيرفرات ظهرت")
                except Exception:
                    pass

                try:
                    ifr = sb.find_element(".watch iframe")
                    src = ifr.get_attribute("src")
                    if src and src.startswith("http"):
                        iframe_url = src.replace("&amp;", "&")
                except Exception:
                    pass

                if not iframe_url:
                    print("   ❌ لا iframe")
                    return None

                print(f"   📋 {iframe_url[:90]}")
                sb.driver.execute_cdp_cmd("Page.navigate",
                                         {"url": iframe_url, "referrer": url})
                sb.cdp.sleep(3)

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

                # تشغيل والتقاط
                for cycle in range(6):
                    for sel in ["video", ".jw-icon-playback", ".jw-icon-display",
                                ".vjs-big-play-button"]:
                        try: sb.cdp.click_if_visible(sel)
                        except Exception: pass
                    try:
                        sb.cdp.execute_script("""
                            (function(){try{
                                if(typeof jwplayer!=='undefined'){
                                    var p=jwplayer(); if(p&&p.play){p.play(true);return;}
                                }
                                var v=document.querySelector('video');
                                if(v){v.muted=true;v.play&&v.play().catch(function(){});}
                            }catch(e){}})();
                        """)
                    except Exception:
                        pass
                    sb.cdp.sleep(1)
                    if any(x in u for u in _read() for x in [".m3u8", ".mpd"]):
                        break

                # جمع m3u8
                urls = _read()
                m3u8 = [u for u in urls if ".m3u8" in u or ".mpd" in u]

                try:
                    perf = sb.cdp.execute_script("""
                        (function(){try{
                            return performance.getEntriesByType('resource').map(e=>e.name);
                        }catch(e){return [];}})();
                    """)
                    if isinstance(perf, list):
                        for u in perf:
                            if (".m3u8" in u or ".mpd" in u) and u not in m3u8:
                                m3u8.append(u)
                except Exception:
                    pass

                try:
                    html = sb.cdp.get_page_source() or ""
                    for u in _extract_m3u8_from_html(html):
                        if u not in m3u8:
                            m3u8.append(u)
                except Exception:
                    pass

                # ترتيب
                idx = [u for u in m3u8 if "index-" in u.lower()]
                mst = [u for u in m3u8 if "master" in u.lower()]
                mpd = [u for u in m3u8 if ".mpd" in u.lower()]
                oth = [u for u in m3u8 if u not in idx and u not in mst and u not in mpd]
                ordered = idx + mst + mpd + oth

                print(f"   🎯 {len(ordered)} مرشح")
                for m_url in ordered[:M3U8_LIMIT]:
                    if result: break
                    print(f"   🎯 {m_url[:100]}")

                    # جلب المحتوى عبر المتصفح
                    content = None
                    for expr in [f"fetch({json.dumps(m_url)},{{mode:'cors'}})",
                                 f"fetch({json.dumps(m_url)},{{mode:'cors',credentials:'include'}})"]:
                        js = f"""
                        (function(){{
                            window.__dv=false; window.__rv=null;
                            try{{ {expr}.then(r=>r.text().then(t=>{{
                                window.__rv={{ok:true,s:r.status,t:t}};
                                window.__dv=true;
                            }})).catch(e=>{{window.__rv={{ok:false}};window.__dv=true;}});
                            }}catch(e){{window.__rv={{ok:false}};window.__dv=true;}}
                        }})();
                        """
                        try: sb.cdp.execute_script(js)
                        except Exception: continue
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
                        if content: break

                    if not content:
                        print("      ❌ فشل الجلب")
                        continue

                    segs, variants = _parse_m3u8(content, m_url.rsplit("/", 1)[0])
                    if not segs and variants:
                        for v in variants[:2]:
                            # جرب variant
                            js = f"""
                            (function(){{
                                window.__dv2=false;window.__rv2=null;
                                fetch({json.dumps(v)},{{mode:'cors'}}).then(r=>r.text().then(t=>{{
                                    window.__rv2={{ok:true,t:t}};window.__dv2=true;
                                }})).catch(e=>{{window.__dv2=true;}});
                            }})();
                            """
                            try: sb.cdp.execute_script(js)
                            except Exception: continue
                            start = time.time()
                            while time.time() - start < 12:
                                sb.cdp.sleep(0.2)
                                try:
                                    if sb.cdp.execute_script("return window.__dv2===true"):
                                        r = sb.cdp.execute_script("return window.__rv2")
                                        if r and r.get("ok"):
                                            s2, _ = _parse_m3u8(r["t"], v.rsplit("/", 1)[0])
                                            if s2:
                                                segs = s2; break
                                        break
                                except Exception: pass
                            if segs: break

                    if segs:
                        # cffi أولاً
                        res = _cffi_segments(segs, str(out_path), iframe_url)
                        if res:
                            result = res; break
                        print("      🌐 fallback للمتصفح")
                        res = _browser_segments(sb, segs, str(out_path))
                        if res:
                            result = res; break

            except Exception as e:
                print(f"   ❌ {str(e)[:150]}")
    except Exception as e:
        print(f"   ❌ {str(e)[:150]}")
    finally:
        try: os.remove(netlog)
        except Exception: pass

    return result


# ═══ الضغط ═══
def _compress(inp, out):
    if not Path(inp).exists():
        return False
    im = Path(inp).stat().st_size / 1048576
    print(f"   🗜️  {im:.2f}MB → {config.COMPRESS_SCALE}p...")
    cmd = [
        "ffmpeg", "-err_detect", "ignore_err",
        "-fflags", "+discardcorrupt+genpts",
        "-analyzeduration", "50M", "-probesize", "50M",
        "-i", str(inp),
        "-vf", f"scale=-2:{config.COMPRESS_SCALE}",
        "-c:v", "libx264",
        "-crf", str(config.COMPRESS_CRF),
        "-preset", config.COMPRESS_PRESET,
        "-threads", "2",
        "-c:a", "aac", "-b:a", "64k",
        "-f", "mp4", "-movflags", "+faststart",
        "-max_muxing_queue_size", "4096",
        "-y", str(out),
    ]
    try:
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not Path(out).exists():
            return False
        om = Path(out).stat().st_size / 1048576
        print(f"   ✅ {im:.2f}→{om:.2f}MB في {time.time()-t0:.1f}s")
        return True
    except Exception as e:
        print(f"   ❌ {e}")
        return False


# ═══ الدالة الرئيسية ═══
def download_episode(series_name, episode, url):
    safe = _safe(series_name)
    out_dir = MEDIA_DIR / safe
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = out_dir / f"ep{episode:03d}_raw.ts"
    final = out_dir / f"ep{episode:03d}.mp4"

    if final.exists() and final.stat().st_size > MIN_SIZE:
        print(f"    ↳ موجودة: {final.name}")
        return final

    print(f"    ↳ تحميل الحلقة {episode}...")
    res = _process_with_browser(url, raw)

    if not res or not raw.exists():
        raise DownloadError(
            f"فشل تحميل {series_name} — حلقة {episode}\n"
            f"   URL: {url}"
        )

    size = raw.stat().st_size
    print(f"    📦 {size/1048576:.1f}MB")

    if config.SKIP_COMPRESS:
        shutil.move(str(raw), str(final))
    else:
        if not _compress(raw, final):
            print("    ⚠️ فشل الضغط — استخدام الأصلي")
            shutil.move(str(raw), str(final))

    if raw.exists():
        raw.unlink()

    if not final.exists():
        raise DownloadError(f"لا ملف نهائي للحلقة {episode}")

    print(f"    ✅ {final.name} ({final.stat().st_size/1048576:.1f}MB)")
    return final