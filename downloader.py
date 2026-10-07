def _concat_segments(seg_paths, out_path, seg_dir):
    """
    v54: binary concat أولاً (أسرع 100x من ffmpeg concat demuxer).
    ffmpeg يستخدم فقط للـ remux النهائي (TS → MP4).
    """
    if not seg_paths:
        return False

    total = len(seg_paths)

    # ═══ 1) دمج ثنائي (binary concat) — سريع جداً ═══
    print(f"      🔗 دمج {total} مقطع (binary)...", flush=True)
    t0 = time.time()
    raw_ts = os.path.join(seg_dir, "combined.ts")

    try:
        with open(raw_ts, "wb") as out:
            for i, p in enumerate(seg_paths, 1):
                if not os.path.exists(p):
                    continue
                with open(p, "rb") as seg:
                    shutil.copyfileobj(seg, out, length=1024 * 1024)
                if i % 100 == 0:
                    print(f"         ↳ {i}/{total}", flush=True)
        size_mb = os.path.getsize(raw_ts) / 1048576
        print(f"      ✅ دمج ثنائي: {size_mb:.1f}MB في {time.time()-t0:.1f}s",
              flush=True)
    except Exception as e:
        print(f"      ❌ فشل الدمج الثنائي: {str(e)[:100]}", flush=True)
        return False

    # ═══ 2) remux: TS → MP4 (مع timeout صارم) ═══
    print(f"      📼 تحويل TS → MP4...", flush=True)
    t0 = time.time()
    cmd = [
        _ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+igndts",
        "-i", raw_ts,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        "-y", out_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if (r.returncode == 0 and os.path.exists(out_path)
                and os.path.getsize(out_path) > MIN_SIZE):
            size_mb = os.path.getsize(out_path) / 1048576
            print(f"      ✅ remux: {size_mb:.1f}MB في {time.time()-t0:.1f}s",
                  flush=True)
            try:
                os.unlink(raw_ts)
            except Exception:
                pass
            return True
        err = (r.stderr or "").strip().split("\n")[-1] if r.stderr else "?"
        print(f"      ⚠️ remux: {err[:150]}", flush=True)
    except subprocess.TimeoutExpired:
        print(f"      ❌ remux timeout (300s) — إيقاف ffmpeg", flush=True)
        try:
            subprocess.run(["pkill", "-9", "-f", "ffmpeg"],
                           capture_output=True, timeout=3)
        except Exception:
            pass
    except Exception as e:
        print(f"      ❌ remux: {str(e)[:100]}", flush=True)

    # ═══ 3) fallback: استخدم ملف TS مباشرة كـ mp4 ═══
    print(f"      🔄 fallback: استخدام TS مباشرة", flush=True)
    try:
        shutil.move(raw_ts, out_path)
        if os.path.exists(out_path) and os.path.getsize(out_path) > MIN_SIZE:
            size_mb = os.path.getsize(out_path) / 1048576
            print(f"      ✅ TS مباشر: {size_mb:.1f}MB", flush=True)
            return True
    except Exception as e:
        print(f"      ❌ fallback: {str(e)[:100]}", flush=True)

    return False