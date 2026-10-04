#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stream_server.py — خادم MTProto لبث فيديوهات Telegram
★ نسخة محسّنة: asyncio fix + pre-fetch + CORS + FloodWait
"""

# ═══ إصلاح asyncio قبل استيراد pyrogram ═══
import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import os
import re
import time
import traceback
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pyrogram import Client
from pyrogram.errors import FileReferenceExpired, FloodWait
from pyrogram.file_id import FileId
from pyrogram.raw.functions.upload import GetFile
from pyrogram.raw.types import (
    InputDocumentFileLocation,
    InputPhotoFileLocation,
)

# ═══ الإعدادات ═══
API_ID = int(os.environ.get("API_ID", "0") or "0")
API_HASH = os.environ.get("API_HASH", "").strip()
STRING_SESSION = os.environ.get(
    "STRING_SESSION", os.environ.get("SESSION_STRING", "")
).strip()
CHANNEL_ID = os.environ.get(
    "CHANNEL", os.environ.get("CHANNEL_ID", "")
).strip()

_missing = []
if not API_ID: _missing.append("API_ID")
if not API_HASH: _missing.append("API_HASH")
if not STRING_SESSION: _missing.append("STRING_SESSION")
if _missing:
    print(f"❌ متغيرات ناقصة: {', '.join(_missing)}")
    raise SystemExit(1)

CHUNK_SIZE = 1024 * 1024
MAX_REFRESH = 3
STREAM_TIMEOUT = 600

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Range, Content-Type, Accept, Origin",
    "Access-Control-Expose-Headers":
        "Content-Length, Content-Range, Accept-Ranges, Content-Type",
    "Access-Control-Max-Age": "86400",
}

# ═══ العميل ═══
client = Client(
    "stream_session",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=STRING_SESSION,
    in_memory=True,
    workers=8,
    no_updates=True,
)

START_TIME = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 بدء عميل MTProto...")
    try:
        await client.start()
        me = await client.get_me()
        print(f"✅ يعمل كـ: @{me.username or me.id}")
        if CHANNEL_ID:
            print(f"📺 القناة: {CHANNEL_ID}")
    except Exception as e:
        print(f"❌ فشل التشغيل: {e}")
        traceback.print_exc()
        raise
    yield
    print("👋 إيقاف...")
    try: await client.stop()
    except Exception: pass


app = FastAPI(title="Shoof Stream Server", version="2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["Range", "Content-Type", "Accept", "Origin"],
    expose_headers=["Content-Length", "Content-Range", "Accept-Ranges", "Content-Type"],
    max_age=86400,
)


@app.middleware("http")
async def cors_mw(request: Request, call_next):
    try:
        r = await call_next(request)
    except Exception as e:
        r = JSONResponse({"error": str(e)}, 500, headers=CORS_HEADERS)
    for k, v in CORS_HEADERS.items():
        r.headers[k] = v
    return r


# ═══ Endpoints ═══
@app.get("/")
async def root():
    return {"service": "Shoof Stream", "version": "2.0",
            "uptime": int(time.time() - START_TIME)}


@app.get("/health")
async def health():
    try: connected = client.is_connected
    except Exception: connected = False
    return {"status": "ok" if connected else "degraded",
            "mtproto_connected": connected,
            "uptime": int(time.time() - START_TIME)}


# ═══ Helpers ═══
def _extract_file_id(msg):
    for attr in ("video", "document", "audio", "animation", "voice"):
        m = getattr(msg, attr, None)
        if m and getattr(m, "file_id", None):
            return m.file_id
    return ""


async def refresh_file_id(mid):
    if not CHANNEL_ID or not mid:
        return ""
    print(f"🔄 تحديث file_id للرسالة {mid}")
    try:
        msg = await client.get_messages(CHANNEL_ID, message_ids=mid)
        if msg and msg.media:
            fid = _extract_file_id(msg)
            if fid:
                print(f"✅ get_messages: {fid[:40]}")
                return fid
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception as e:
        print(f"⚠️ get_messages: {e}")

    try:
        async for msg in client.get_chat_history(CHANNEL_ID, limit=500):
            if msg.id == mid and msg.media:
                fid = _extract_file_id(msg)
                if fid:
                    print(f"✅ history: {fid[:40]}")
                    return fid
    except Exception as e:
        print(f"⚠️ history: {e}")
    return ""


def build_location(fid):
    ft = str(fid.file_type).lower()
    DOC = {"video", "document", "audio", "animation", "gif", "voice",
           "sticker", "secure", "3","4","5","6","7","8","9"}
    PHOTO = {"photo", "profile_photo", "thumbnail", "0", "1", "2"}
    if ft in PHOTO:
        return InputPhotoFileLocation(
            id=fid.media_id, access_hash=fid.access_hash,
            file_reference=fid.file_reference, thumb_size="")
    return InputDocumentFileLocation(
        id=fid.media_id, access_hash=fid.access_hash,
        file_reference=fid.file_reference, thumb_size="")


def align_down(x, a):
    return (x // a) * a


async def fetch_chunk(location, offset, limit, mid, fid, state):
    try:
        r = await client.invoke(GetFile(location=location, offset=offset, limit=limit))
        return r.bytes, location, fid
    except FileReferenceExpired:
        print(f"🔄 FileReferenceExpired @ {offset}")
        if state["attempts"] >= MAX_REFRESH or not mid:
            return b"", location, fid
        state["attempts"] += 1
        new_fid = await refresh_file_id(mid)
        if not new_fid:
            return b"", location, fid
        try:
            decoded = FileId.decode(new_fid)
            new_loc = build_location(decoded)
            r = await client.invoke(GetFile(location=new_loc, offset=offset, limit=limit))
            return r.bytes, new_loc, new_fid
        except Exception as e:
            print(f"❌ decode: {e}")
            return b"", location, fid
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            r = await client.invoke(GetFile(location=location, offset=offset, limit=limit))
            return r.bytes, location, fid
        except Exception:
            return b"", location, fid
    except Exception as e:
        print(f"⚠️ {offset}: {e}")
        return b"", location, fid


# ═══ Stream ═══
@app.options("/stream")
async def stream_options():
    return Response(200, headers=CORS_HEADERS)


@app.head("/stream")
async def stream_head(fid: str, size: int = 0, mid: int = 0):
    if not fid or not size:
        return Response(400, headers=CORS_HEADERS)
    return Response(200, headers={
        **CORS_HEADERS,
        "Content-Type": "video/mp4",
        "Content-Length": str(size),
        "Accept-Ranges": "bytes",
    })


@app.get("/stream")
async def stream(request: Request, fid: str, size: int = 0, mid: int = 0):
    if not fid:
        raise HTTPException(400, "missing fid")
    try:
        file_id = FileId.decode(fid)
    except Exception as e:
        raise HTTPException(400, f"bad file_id: {e}")

    file_size = int(size)
    if file_size <= 0:
        raise HTTPException(400, "missing size")

    rng = request.headers.get("range")
    start, end = 0, file_size - 1
    if rng:
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else file_size - 1
            if start > end or end >= file_size or start < 0:
                raise HTTPException(416, "range",
                                    headers={"Content-Range": f"bytes */{file_size}"})

    length = end - start + 1
    print(f"📥 stream: {file_id.file_type} | {file_size/1048576:.1f}MB | {start}-{end}")

    location = build_location(file_id)
    aligned = align_down(start, CHUNK_SIZE)
    skip = start - aligned
    state = {"attempts": 0}

    try:
        first, location, new_fid = await fetch_chunk(
            location, aligned, CHUNK_SIZE, mid, fid, state)
    except Exception as e:
        print(f"❌ pre-fetch: {e}")
        first = b""

    if not first:
        return JSONResponse(503, {"error": "stream_unavailable"},
                            headers=CORS_HEADERS)

    if skip > 0:
        first = first[skip:]
    if len(first) > length:
        first = first[:length]

    first_sent = len(first)
    print(f"✅ pre-fetch: {first_sent}")

    async def gen():
        nonlocal location
        try:
            yield first
            if first_sent >= length:
                return
            off = aligned + CHUNK_SIZE
            rem = length - first_sent
            total = first_sent
            t0 = time.time()
            while rem > 0:
                if time.time() - t0 > STREAM_TIMEOUT:
                    break
                try:
                    data, location, _ = await fetch_chunk(
                        location, off, CHUNK_SIZE, mid, fid, state)
                except Exception:
                    break
                if not data:
                    break
                if len(data) > rem:
                    data = data[:rem]
                yield data
                total += len(data)
                off += CHUNK_SIZE
                rem -= len(data)
            print(f"✅ {total/1048576:.1f}MB sent")
        except asyncio.CancelledError:
            raise

    headers = {
        **CORS_HEADERS,
        "Content-Type": "video/mp4",
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=86400",
        "Content-Length": str(length),
    }
    if rng:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        return StreamingResponse(gen(), 206, headers=headers)
    return StreamingResponse(gen(), 200, headers=headers)


@app.exception_handler(Exception)
async def global_err(req, exc):
    traceback.print_exc()
    return JSONResponse({"error": str(exc)}, 500, headers=CORS_HEADERS)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"🚀 Shoof Stream Server on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info",
                timeout_keep_alive=75)