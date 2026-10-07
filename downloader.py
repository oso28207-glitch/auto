"""
downloader.py — CDP Fetch.enable للالتقاط + XHR داخل iframe
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
M3U8_SEARCH_TIMEOUT = int(os.environ.get("M3U8_SEARCH_TIMEOUT", "40"))
YTDLP_TIMEOUT = int(os.environ.get("YTDLP_TIMEOUT", "120"))
FFMPEG_HLS_TIMEOUT = int(os.environ.get("FFMPEG_HLS_TIMEOUT", "1800"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_REQ_IDS = {}
_RESP_BODIES = {}
_NET_LOCK = threading.Lock()


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


def _cdp_cmd(sb, cmd, params=None):
    if params is None: params = {}
    try:
        return sb.driver.execute_cdp_cmd(cmd, params)
    except Exception:
        pass
    for meth in ("send_cdp_cmd", "execute_cdp_cmd"):
        try:
            fn = getattr(sb.cdp, meth, None)
            if fn:
                return fn(cmd, params)
        except Exception:
            pass
    return None


def _cdp_eval_async(sb, expr, timeout_ms=120000):
    return _cdp_cmd(sb, "Runtime.evaluate", {
        "expression": expr,
        "awaitPromise": True,
        "returnByValue": True,
        "timeout": int(timeout_ms),
    })


# ═══════════════════════════════════════════════════════════════
# ★★★ Fetch.enable — الحل الرئيسي لالتقاط m3u8
# ═══════════════════════════════════════════════════════════════
def _enable_fetch_capture(sb):
    """
    يستخدم CDP Fetch domain لاعتراض استجابات m3u8/mpd.
    عند الاعتراض، يقرأ body فوراً ويحفظه، ثم يمرر الطلب.
    """
    global _REQ_IDS, _RESP_BODIES
    with _NET_LOCK:
        _REQ_IDS.clear()
        _RESP_BODIES.clear()

    print("      🎯 Fetch.enable...")

    enabled = False
    for fn in [
        lambda: sb.driver.execute_cdp_cmd("Fetch.enable", {
            "patterns": [
                {"urlPattern": "*.m3u8*", "requestStage": "Response"},
                {"urlPattern": "*.mpd*", "requestStage": "Response"},
            ],
            "handleAuthRequests": False,
        }),
        lambda: sb.cdp.send_cdp_cmd("Fetch.enable", {
            "patterns": [
                {"urlPattern": "*.m3u8*", "requestStage": "Response"},
                {"urlPattern": "*.mpd*", "requestStage": "Response"},
            ],
        }),
        lambda: sb.cdp.execute_cdp_cmd("Fetch.enable", {
            "patterns": [
                {"urlPattern": "*.m3u8*", "requestStage": "Response"},
                {"urlPattern": "*.mpd*", "requestStage": "Response"},
            ],
        }),
    ]:
        try:
            fn()
            enabled = True
            break
        except Exception:
            pass

    if not enabled:
        print("      ⚠️ Fetch.enable فشل")
        return False

    print("      🎯 Fetch.enable OK")

    # تسجيل handler
    def _handle_paused(params):
        try:
            rid = params.get("requestId", "")
            req = params.get("request") or {}
            url = req.get("url", "") if isinstance(req, dict) else ""
            status = params.get("responseStatusCode")

            if url and (".m3u8" in url or ".mpd" in url) and status == 200:
                try:
                    r = _cdp_cmd(sb, "Fetch.getResponseBody", {"requestId": rid})
                    if r and "body" in r:
                        b = r["body"]
                        if r.get("base64Encoded"):
                            try:
                                b = base64.b64decode(b).decode("utf-8", errors="ignore")
                            except Exception:
                                pass
                        with _NET_LOCK:
                            _RESP_BODIES[url] = b
                        print(f"      💾 m3u8 محفوظ ({len(str(b))}B)")
                except Exception as e:
                    print(f"      ⚠️ getResponseBody: {str(e)[:80]}")

            # مرر الطلب دائماً
            if rid:
                try:
                    _cdp_cmd(sb, "Fetch.continueRequest", {"requestId": rid})
                except Exception:
                    try:
                        _cdp_cmd(sb, "Fetch.continueResponse", {"requestId": rid})
                    except Exception:
                        pass
        except Exception:
            pass

    registered = False

    # محاولة async
    try:
        import mycdp
        cls = getattr(mycdp.fetch, "RequestPaused", None)
        if cls is not None:
            async def _async_handler(params):
                _handle_paused(params if isinstance(params, dict) else {})

            try:
                sb.cdp.add_handler(cls, _async_handler)
                print("      🎯 Fetch handler (async)")
                registered = True
            except Exception:
                pass

            if not registered:
                def _sync_handler(event):
                    try:
                        d = {}
                        for k in ("requestId", "request_id"):
                            v = getattr(event, k, None)
                            if v: d["requestId"] = v
                        req = getattr(event, "request", None)
                        if req is not None:
                            d["request"] = {"url": getattr(req, "url", "")}
                        st = getattr(event, "response_status_code", None)
                        if st is None:
                            st = getattr(event, "responseStatusCode", None)
                        if st is not None: d["responseStatusCode"] = st
                        _handle_paused(d)
                    except Exception:
                        pass

                try:
                    sb.cdp.add_handler(cls, _sync_handler)
                    print("      🎯 Fetch handler (sync)")
                    registered = True
                except Exception as e:
                    print(f"      ⚠️ sync handler: {str(e)[:80]}")
    except Exception as e:
        print(f"      ⚠️ mycdp.fetch: {str(e)[:80]}")

    return registered


def _enable_network_capture(sb):
    """تفعيل Network domain (backup)."""
    for fn in [
        lambda: sb.driver.execute_cdp_cmd("Network.enable", {}),
        lambda: sb.cdp.send_cdp_cmd("Network.enable", {}),
    ]:
        try:
            fn()
            print("      ✅ Network.enable OK")
            return True
        except Exception:
            pass
    print("      ⚠️ Network.enable فشل")
    return False


def _get_response_body(sb, url):
    with _NET_LOCK:
        if url in _RESP_BODIES:
            return _RESP_BODIES[url]
        rid = _REQ_IDS.get(url)
    if not rid:
        return None
    result = _cdp_cmd(sb, "Network.getResponseBody", {"requestId": rid})
    if result and "body" in result:
        body = result["body"]
        if result.get("base64Encoded"):
            try: body = base64.b64decode(body)
            except Exception: pass
        with _NET_LOCK:
            _RESP_BODIES[url] = body
        return body
    return None


def _wait_for_body(sb, url, timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        body = _get_response_body(sb, url)
        if body:
            return body
        sb.cdp.sleep(0.5)
    return None


def _fetch_segments_via_xhr(sb, urls, batch_timeout_ms=120000):
    expr = (
        "(async () => {"
        f"  const urls = {json.dumps(urls)};"
        "  const out = [];"
        "  for (const u of urls) {"
        "    try {"
        "      const xhr = new XMLHttpRequest();"
        "      xhr.open('GET', u, true);"
        "      xhr.responseType = 'arraybuffer';"
        "      xhr.withCredentials = true;"
        "      const resp = await new Promise((resolve) => {"
        "        xhr.onload = () => resolve(xhr.status === 200 ? xhr.response : ('__HTTP_' + xhr.status));"
        "        xhr.onerror = () => resolve('__NETERR__');"
        "        xhr.send();"
        "      });"
        "      if (typeof resp === 'string') { out.push(resp); continue; }"
        "      const bytes = new Uint8Array(resp);"
        "      let bin = '';"
        "      for (let i = 0; i < bytes.length; i += 8192) {"
        "        bin += String.fromCharCode.apply(null, bytes.subarray(i, i+8192));"
        "      }"
        "      out.push('B64:' + btoa(bin));"
        "    } catch (e) {"
        "      out.push('__ERR__:' + e.message);"
        "    }"
        "  }"
        "  return out;"
        "})()"
    )
    result = _cdp_eval_async(sb, expr, timeout_ms=batch_timeout_ms)
    if not result or "result" not in result:
        return None
    val = result["result"].get("value")
    return val if isinstance(val, list) else None


# ═══════════════════════════════════════════════════════════════
# HLS عبر CDP
# ═══════════════════════════════════════════════════════════════
def _hls_via_cdp(sb, m3u8_url, out):
    print(f"         🌐 CDP capture HLS...")

    body = _wait_for_body(sb, m3u8_url, timeout=12)
    if not body:
        print(f"         ⚠️ m3u8 body غير متاح")
        return False

    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="ignore")
        except Exception:
            return False

    if "#EXTM3U" not in body:
        print(f"         ⚠️ ليست m3u8")
        return False

    content = body
    cur_url = m3u8_url

    if "#EXT-X-STREAM-INF" in content:
        variant_url = None
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                variant_url = urljoin(cur_url, line)
                break
        if variant_url:
            print(f"         ↪️ variant...")
            vbody = _wait_for_body(sb, variant_url, timeout=25)
            if vbody:
                if isinstance(vbody, bytes):
                    vbody = vbody.decode("utf-8", errors="ignore")
                if "#EXTINF" in vbody or "#EXT-X-TARGETDURATION" in vbody:
                    content = vbody
                    cur_url = variant_url
                else:
                    return False
            else:
                return False

    segments = []
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            segments.append(urljoin(cur_url, line))

    if not segments:
        print(f"         ⚠️ لا مقاطع")
        return False

    print(f"         📥 {len(segments)} مقطع")

    tmpdir = tempfile.mkdtemp(prefix="hls_cdp_")
    try:
        seg_paths = []
        BATCH = 5
        total = len(segments)
        idx = 0

        while idx < total:
            batch = segments[idx:idx+BATCH]
            results = _fetch_segments_via_xhr(sb, batch, batch_timeout_ms=120000)
            if results is None:
                idx += BATCH
                continue

            for j, r in enumerate(results):
                if not isinstance(r, str) or not r.startswith("B64:"):
                    continue
                try:
                    data = base64.b64decode(r[4:])
                    if len(data) < 100:
                        continue
                    p = os.path.join(tmpdir, f"seg_{idx+j:05d}.ts")
                    with open(p, "wb") as f:
                        f.write(data)
                    seg_paths.append(p)
                except Exception:
                    continue

            idx += BATCH
            if idx % 25 == 0 or idx >= total:
                print(f"         📊 {min(idx, total)}/{total}")

        print(f"         ✅ {len(seg_paths)}/{total} مقطع")

        if len(seg_paths) < 3:
            return False

        concat_file = os.path.join(tmpdir, "concat.txt")
        with open(concat_file, "w") as f:
            for p in seg_paths:
                f.write(f"file '{p}'\n")

        ff = _get_ffmpeg_exe()
        cmd = [
            ff, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-c", "copy", "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart", "-y", str(out),
        ]
        try:
            print(f"         🎬 دمج محلي...")
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if (r.returncode == 0 and os.path.exists(out)
                    and os.path.getsize(out) > MIN_SIZE):
                mb = os.path.getsize(out) / 1048576
                print(f"         ✅ نجح: {mb:.1f}MB")
                return True
            err = (r.stderr or "").strip()[-150:] if r.stderr else "?"
            print(f"         ⚠️ دمج: {err}")
            return False
        except subprocess.TimeoutExpired:
            return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# interceptor JS
# ═══════════════════════════════════════════════════════════════
_INTERCEPTOR_JS = r"""
(function() {
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

    if (window.XMLHttpRequest && !window.__xhr_patched) {
        var origOpen = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(method, url) {
            try { record(url); } catch(e){}
            return origOpen.apply(this, arguments);
        };
        window.__xhr_patched = true;
    }

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

    setInterval(function() {
        window.__patch_jw();
        window.__patch_vjs();
    }, 500);
})();
"""


def _install_interceptor(sb):
    try:
        sb.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": _INTERCEPTOR_JS,
        })
        print("      💉 interceptor")
    except Exception as e:
        print(f"      ⚠️ interceptor: {str(e)[:80]}")


def _read_captured_m3u8(sb):
    try:
        urls = sb.cdp.execute_script("return window.__captured_m3u8 || [];")
        if isinstance(urls, list):
            return [u for u in urls if isinstance(u, str) and
                    (".m3u8" in u or ".mpd" in u)]
    except Exception:
        pass
    return []


def _scan_for_m3u8(sb, netlog_read):
    found = set()

    try:
        for u in _read_captured_m3u8(sb):
            found.add(u)
    except Exception: pass

    try:
        for u in netlog_read():
            if ".m3u8" in u or ".mpd" in u:
                found.add(u)
    except Exception: pass

    # ★ من _RESP_BODIES أيضاً
    with _NET_LOCK:
        for u in list(_RESP_BODIES.keys()):
            if ".m3u8" in u or ".mpd" in u:
                found.add(u)

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
    except Exception: pass

    try:
        html = sb.cdp.get_page_source() or ""
        for m in re.finditer(r'(https?:[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*)', html):
            found.add(m.group(1).replace("\\/", "/"))
        for m in re.finditer(r'(https?:[^\s"\'<>\\]+\.mpd[^\s"\'<>\\]*)', html):
            found.add(m.group(1).replace("\\/", "/"))
    except Exception: pass

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
    except Exception: pass

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
    except Exception: pass

    urls = list(found)
    urls = [u for u in urls if "ping.gif" not in u and "jwpltx" not in u]

    idx = [u for u in urls if "index" in u.lower()]
    mst = [u for u in urls if "master" in u.lower()]
    mpd = [u for u in urls if ".mpd" in u.lower()]
    oth = [u for u in urls if u not in idx and u not in mst and u not in mpd]

    return idx + mst + mpd + oth


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
                ".vjs-big-play-button", "[class*='play']", "#play",
                "button[aria-label*='Play']", ".play-button"]:
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
            for i in range(5):
                sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mousePressed", "x": rect['x'], "y": rect['y'],
                    "button": "left", "clickCount": 1,
                })
                sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mouseReleased", "x": rect['x'], "y": rect['y'],
                    "button": "left", "clickCount": 1,
                })
                sb.cdp.sleep(0.5)
    except Exception: pass

    try:
        sb.cdp.execute_script("""
            (function(){try{
                if(typeof jwplayer!=='undefined'){
                    var p=jwplayer();
                    if(p){
                        if(p.play)p.play(true);
                        if(p.setMute)p.setMute(true);
                        if(p.setVolume)p.setVolume(0);
                    }
                }
                if(typeof videojs!=='undefined'){
                    var p2 = videojs.getPlayers();
                    for (var k in p2) {
                        try { p2[k].play(); p2[k].muted(true); } catch(e){}
                    }
                }
                var v=document.querySelector('video');
                if(v){
                    v.muted=true;
                    if(v.play)v.play().catch(function(){});
                    v.click();
                    setTimeout(function(){ try { v.click(); } catch(e){} }, 300);
                    setTimeout(function(){ try { v.play(); } catch(e){} }, 500);
                }
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
# curl_cffi احتياطي
# ═══════════════════════════════════════════════════════════════
def _hls_via_curl(m3u8_url, out, iframe_url, cookies=None):
    cookies = cookies or {}
    headers = {
        "User-Agent": UA, "Accept": "*/*",
        "Accept-Language": "ar,en;q=0.9",
    }
    if iframe_url:
        headers["Referer"] = iframe_url
        headers["Origin"] = _origin(iframe_url)
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    print(f"         🌐 curl_cffi HLS...")

    tmpdir = tempfile.mkdtemp(prefix="hls_curl_")
    try:
        try:
            r = cffi_requests.get(m3u8_url, headers=headers,
                                  impersonate="chrome120", timeout=30)
        except Exception as e:
            print(f"         ⚠️ m3u8: {str(e)[:80]}")
            return False

        if r.status_code != 200 or not r.text:
            print(f"         ⚠️ m3u8 HTTP {r.status_code}")
            return False

        content = r.text
        cur_url = m3u8_url

        if "#EXT-X-STREAM-INF" in content:
            variant_url = None
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    variant_url = urljoin(cur_url, line)
                    break
            if variant_url:
                try:
                    r2 = cffi_requests.get(variant_url, headers=headers,
                                           impersonate="chrome120", timeout=30)
                    if r2.status_code == 200 and r2.text:
                        content = r2.text
                        cur_url = variant_url
                except Exception:
                    pass

        segments = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            segments.append(urljoin(cur_url, line))

        if not segments:
            return False

        print(f"         📥 {len(segments)} مقطع...")

        from concurrent.futures import ThreadPoolExecutor, as_completed
        seg_paths = [os.path.join(tmpdir, f"seg_{i:05d}.ts")
                     for i in range(len(segments))]

        def _fetch_seg(args):
            idx, seg_url, seg_path = args
            try:
                rr = cffi_requests.get(seg_url, headers=headers,
                                       impersonate="chrome120", timeout=60)
                if rr.status_code == 200 and rr.content:
                    with open(seg_path, "wb") as f:
                        f.write(rr.content)
                    return True
            except Exception:
                pass
            return False

        done_count = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(_fetch_seg, (i, s, seg_paths[i]))
                       for i, s in enumerate(segments)]
            for f in as_completed(futures):
                if f.result(): done_count += 1

        print(f"         ✅ {done_count}/{len(segments)}")

        existing = [p for p in seg_paths
                    if os.path.exists(p) and os.path.getsize(p) > 0]
        if len(existing) < 3:
            return False

        concat_file = os.path.join(tmpdir, "concat.txt")
        with open(concat_file, "w") as f:
            for p in existing:
                f.write(f"file '{p}'\n")

        ff = _get_ffmpeg_exe()
        cmd = [
            ff, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-c", "copy", "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart", "-y", str(out),
        ]
        try:
            r4 = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if (r4.returncode == 0 and os.path.exists(out)
                    and os.path.getsize(out) > MIN_SIZE):
                mb = os.path.getsize(out) / 1048576
                print(f"         ✅ نجح: {mb:.1f}MB")
                return True
        except subprocess.TimeoutExpired:
            pass
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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
    print(f"   🗜️  {im:.2f}MB → {config.COMPRESS_SCALE}p...")

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

                # ★ ترتيب: Network أولاً ثم Fetch (Fetch يحتاج Network مفعل)
                _enable_network_capture(sb)
                _enable_fetch_capture(sb)
                _install_interceptor(sb)

                # تسجيل netlog عبر RequestWillBeSent
                try:
                    import mycdp
                    async def on_req(params):
                        try:
                            req = params.get("request", {}) or {}
                            u = req.get("url", "")
                            rid = params.get("requestId", "")
                            if u:
                                _log(u)
                                if rid:
                                    with _NET_LOCK:
                                        _REQ_IDS[u] = rid
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
                        player_found = False
                        for _ in range(20):
                            sb.cdp.sleep(1)
                            try:
                                p = sb.cdp.execute_script(
                                    "return typeof jwplayer!=='undefined'?'jw':"
                                    "(typeof videojs!=='undefined'?'vjs':"
                                    "(document.querySelector('video')?'h5':'none'))"
                                )
                                if p in ("jw", "vjs", "h5"):
                                    print(f"      ✅ {p}")
                                    player_found = True
                                    break
                            except Exception: pass

                        if not player_found:
                            print(f"      ⚠️ المشغل لم يظهر")

                        _aggressive_play(sb)

                        print(f"      🎬 البحث ({M3U8_SEARCH_TIMEOUT}s)...")
                        search_start = time.time()
                        m3u8_urls = []

                        while time.time() - search_start < M3U8_SEARCH_TIMEOUT:
                            _aggressive_play(sb)
                            sb.cdp.sleep(2)
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

                        sb.cdp.sleep(2)

                        # 1) CDP capture
                        for m_url in m3u8_urls[:2]:
                            if _hls_via_cdp(sb, m_url, str(out_path)):
                                return (os.path.getsize(out_path), True)

                        # 2) curl_cffi
                        cookies = _get_cookies(sb)
                        for m_url in m3u8_urls[:2]:
                            if _hls_via_curl(m_url, str(out_path),
                                             iframe_url, cookies):
                                return (os.path.getsize(out_path), True)

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
                                timeout=900, default=None)
    except TimeoutError_:
        print(f"    ⏰ تجاوز 900s")
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
            print("    ⚠️ فشل الضغط — الأصلي")
            shutil.move(str(raw), str(final))

    if raw.exists(): raw.unlink()
    if not final.exists(): return None

    print(f"    ✅ {final.name} ({final.stat().st_size/1048576:.1f}MB)")
    return final