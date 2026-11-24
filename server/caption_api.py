"""LiveCaption server: pull a live stream, transcribe it, push captions (and video).

    player ──WS /ws {"type":"start","url":...,"video":true}──▶ this API (:8000)
        yt-dlp → stream URLs → FFmpeg ─┬─▶ 16 kHz mono PCM ──▶ WhisperLive (:9095)
                                       └─▶ /hls/{id}/index.m3u8 (video relay, optional)
    player ◀── {"type":"caption", id, text, final, speaker, stream_start, ...}
    player ◀── {"type":"timeline", pts0}   caption spoken at source PTS pts0 + stream_start/1000
    player ◀── {"type":"timeline", epoch}  (video relay) caption on screen at epoch + stream_start

The web player (web/) is served at "/". Finished captions stay available as
/sessions/{id}.srt and /sessions/{id}.vtt until the server restarts.
"""
import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from media import (SAMPLE_RATE, ffmpeg_command, ffmpeg_env, first_segment, first_timestamps,
                   parse_first_pts, resolve_media, timeline_epoch)
from hotwords import build_hotwords
from segments import SegmentTracker, to_srt, to_webvtt
from topics import load_topic

WHISPER_URL = os.environ.get("WHISPER_URL", "ws://127.0.0.1:9095")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
# How many identical passes finalise an in-progress sentence. Each pass is one decode plus
# 0.1 s, so this sets most of the delay between the end of speech and the final caption.
SAME_OUTPUT_THRESHOLD = int(os.environ.get("WHISPER_SAME_OUTPUT", "3"))
# Hotwords from the broadcast's title, tags and channel (hotwords.py); 0 turns them off.
USE_HOTWORDS = os.environ.get("LIVECAPTION_HOTWORDS", "1") != "0"
API_TOKEN = os.environ.get("LIVECAPTION_TOKEN", "")
HF_TOKEN = os.environ.get("HF_TOKEN")
APP_DIR = Path(__file__).resolve().parent
HLS_ROOT = Path(os.environ.get("LIVECAPTION_HLS_DIR", APP_DIR / "hls"))
WEB_DIR = Path(os.environ.get("LIVECAPTION_WEB_DIR", APP_DIR / "web"))
CHUNK_BYTES = SAMPLE_RATE // 10 * 2          # 100 ms of s16le mono
MODEL_READY_TIMEOUT = 300                    # first load downloads the model
TIMELINE_TIMEOUT = 60
KEPT_SESSIONS = 50
# Every final caption is appended to <dir>/<date>_<time>_<session>.jsonl as it comes out, so an
# hour of captions survives a restart and can be reviewed later (review_captions.py). "" = off.
CAPTION_LOG_DIR = os.environ.get("LIVECAPTION_CAPTION_LOG_DIR", str(APP_DIR / "logs" / "captions"))
MAX_KEYWORD_CHARS = 300                      # typed hotwords; build_hotwords caps the count anyway
ALLOWED_SCHEMES = {"http", "https", "rtmp", "rtmps", "srt", "rtp"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("livecaption.api")
app = FastAPI(title="LiveCaption")
# The page may also be hosted elsewhere (e.g. GitHub Pages behind HTTPS); allow any origin.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
sessions: "OrderedDict[str, Session]" = OrderedDict()
shutil.rmtree(HLS_ROOT, ignore_errors=True)
HLS_ROOT.mkdir(parents=True, exist_ok=True)


class Session:
    def __init__(self, url: str, language, diarization: bool, max_speakers: int, video: bool,
                 keywords: str = "", topic=None):
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.language = language
        self.diarization = diarization
        self.max_speakers = max_speakers
        self.video = video
        self.hls_dir = HLS_ROOT / self.id if video else None
        self.title = url
        self.keywords = keywords
        # Topic dictionary (dictionaries/<topic>.json): extra hotwords and spelling fixes.
        self.topic = load_topic(topic)
        self.hotwords = None
        self.tracker = SegmentTracker(self.topic.replacements if self.topic else None,
                                      self.topic.terms if self.topic else ())
        self.started = time.time()
        self.audio_seconds = 0.0
        self.epoch = None
        self.pts0 = None
        self.state = "created"
        self.log_path = None

    def log(self, record: dict):
        """Append one line to this session's caption log; logging never breaks captioning."""
        if not CAPTION_LOG_DIR:
            return
        try:
            if self.log_path is None:
                folder = Path(CAPTION_LOG_DIR)
                folder.mkdir(parents=True, exist_ok=True)
                self.log_path = folder / f"{time.strftime('%Y%m%d_%H%M%S', time.localtime(self.started))}_{self.id}.jsonl"
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as error:
            logger.warning("[%s] cannot write caption log: %s", self.id, error)

    def export(self):
        return self.tracker.finals


async def send(ws: WebSocket, message: dict):
    await ws.send_text(json.dumps(message, ensure_ascii=False))


async def status(ws: WebSocket, session: Session, state: str, detail: str = ""):
    session.state = state
    logger.info("[%s] %s %s", session.id, state, detail)
    await send(ws, {"type": "status", "state": state, "detail": detail})


async def pump_audio(proc, whisper, session: Session):
    while True:
        chunk = await proc.stdout.read(CHUNK_BYTES)
        if not chunk:
            break
        session.audio_seconds += len(chunk) / 2 / SAMPLE_RATE
        await whisper.send(chunk)
    try:
        await whisper.send(b"END_OF_AUDIO")
    except websockets.ConnectionClosed:
        pass


async def relay_segments(whisper, ws: WebSocket, session: Session):
    async for raw in whisper:
        message = json.loads(raw)
        if "segments" in message:
            for event in session.tracker.update(message["segments"]):
                if "stream_end" in event:
                    event["lag"] = round(session.audio_seconds * 1000) - event["stream_end"]
                if event.get("final"):
                    session.log({key: event[key] for key in ("id", "stream_start", "stream_end", "text",
                                                             "raw", "marks", "speaker") if event.get(key)})
                await send(ws, event)
        elif message.get("message") == "DISCONNECT":
            return "음성 인식 서버가 연결을 끊었습니다."
        elif message.get("status") in {"ERROR", "WAIT"}:
            detail = message.get("message")
            if message["status"] == "WAIT":
                detail = f"인식 서버가 가득 찼습니다. 약 {float(detail or 0):.0f}분 후 다시 시도하세요."
            return str(detail)
    return "음성 인식 연결이 종료되었습니다."


async def watch_timeline(ws: WebSocket, session: Session):
    """Once the first HLS segment exists, tell the player where caption time 0 sits."""
    playlist = session.hls_dir / "index.m3u8"
    deadline = time.monotonic() + TIMELINE_TIMEOUT
    while time.monotonic() < deadline:
        await asyncio.sleep(0.3)
        try:
            entry = first_segment(playlist.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not entry:
            continue
        pdt, name = entry
        try:
            first = await asyncio.to_thread(first_timestamps, str(session.hls_dir / name))
        except Exception as error:  # PyAV raises its own error types
            logger.warning("[%s] cannot read %s: %s", session.id, name, error)
            return
        if "audio" not in first:
            logger.warning("[%s] no audio in %s", session.id, name)
            return
        session.epoch = timeline_epoch(pdt, first)
        logger.info("[%s] timeline epoch %.0f (first pts %s)", session.id, session.epoch, first)
        await send(ws, {"type": "timeline", "epoch": round(session.epoch)})
        return
    logger.warning("[%s] no HLS segment after %ds", session.id, TIMELINE_TIMEOUT)


async def log_stderr(proc, ws: WebSocket, session: Session):
    async for raw in proc.stderr:
        line = raw.decode(errors="replace").rstrip()
        if session.pts0 is None:
            pts = parse_first_pts(line)
            if pts is not None:
                # Caption at stream_start ms was spoken at source PTS pts0 + stream_start / 1000.
                session.pts0 = pts
                logger.info("[%s] first audio pts %.3f", session.id, pts)
                await send(ws, {"type": "timeline", "pts0": pts})
                continue
        if "[warning]" in line or "[error]" in line or "[fatal]" in line:
            logger.warning("[%s] ffmpeg: %s", session.id, line)


async def run_session(ws: WebSocket, session: Session):
    await status(ws, session, "resolving", "스트림 주소 확인 중")
    try:
        media = await asyncio.to_thread(resolve_media, session.url, session.video)
    except Exception as error:  # yt-dlp raises many types
        await status(ws, session, "error", f"스트림 주소 확인 실패: {error}")
        return
    session.title = media["title"]
    # Words typed in the player come first, then the broadcast's own (unless turned off),
    # then the topic dictionary's terms picked for this broadcast (topics.select_hotwords).
    topic_terms = None
    if session.topic:
        topic_terms = session.topic.select_hotwords(media, session.keywords)
        logger.info("[%s] topic %s groups for this broadcast: %s", session.id, session.topic.name,
                    session.topic.matched_groups(" ".join(str(media.get(key) or "") for key in
                                                          ("title", "author", "description", "tags"))
                                                 + " " + session.keywords))
    session.hotwords = build_hotwords(media if USE_HOTWORDS else None, session.keywords, topic_terms)
    logger.info("[%s] hotwords: %s", session.id, session.hotwords)
    session.log({"session": session.id, "url": session.url, "title": session.title,
                 "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(session.started)),
                 "topic": session.topic.name if session.topic else None, "hotwords": session.hotwords})
    if session.log_path:
        logger.info("[%s] caption log: %s", session.id, session.log_path)
    announce = {"type": "session", "id": session.id, "title": session.title, "live": media["live"],
                "hotwords": session.hotwords,
                "topic": session.topic.name if session.topic else None}
    if session.video:
        session.hls_dir.mkdir(parents=True, exist_ok=True)
        announce["hls"] = f"/hls/{session.id}/index.m3u8"
    await send(ws, announce)
    await status(ws, session, "connecting", f"음성 인식 모델 준비 중 ({WHISPER_MODEL})")
    proc = None
    tasks = []
    try:
        async with websockets.connect(WHISPER_URL, max_size=None, ping_interval=20,
                                      ping_timeout=60, open_timeout=30) as whisper:
            options = {"uid": session.id, "language": session.language, "task": "transcribe",
                       "model": WHISPER_MODEL, "use_vad": True, "audio_format": "int16",
                       "send_last_n_segments": 10, "no_speech_thresh": 0.45,
                       "same_output_threshold": SAME_OUTPUT_THRESHOLD}
            if session.hotwords:
                options["hotwords"] = session.hotwords
            if session.diarization:
                options.update(enable_diarization=True, max_speakers=session.max_speakers,
                               diarization_threshold=0.55)
                if HF_TOKEN:
                    options["hf_token"] = HF_TOKEN
            await whisper.send(json.dumps(options))
            ready = json.loads(await asyncio.wait_for(whisper.recv(), MODEL_READY_TIMEOUT))
            if ready.get("message") != "SERVER_READY":
                await status(ws, session, "error", f"음성 인식 서버 응답: {ready.get('message') or ready}")
                return
            command = ffmpeg_command(media, str(session.hls_dir) if session.video else None)
            proc = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=ffmpeg_env())
            await status(ws, session, "running", "음성 인식 중")
            tasks = [asyncio.create_task(pump_audio(proc, whisper, session)),
                     asyncio.create_task(relay_segments(whisper, ws, session)),
                     asyncio.create_task(log_stderr(proc, ws, session))]
            if session.video:
                tasks.append(asyncio.create_task(watch_timeline(ws, session)))
            done, _ = await asyncio.wait(tasks[:2], return_when=asyncio.FIRST_COMPLETED)
            finished = done.pop()
            if finished is tasks[0]:
                # Audio ended: let WhisperLive flush the last segments briefly.
                try:
                    await asyncio.wait_for(tasks[1], 10)
                except asyncio.TimeoutError:
                    pass
                await status(ws, session, "ended", "스트림이 끝났습니다.")
            else:
                await status(ws, session, "error", finished.result() or "인식이 중단되었습니다.")
    except (OSError, websockets.InvalidHandshake) as error:
        await status(ws, session, "error", f"음성 인식 서버({WHISPER_URL})에 연결할 수 없습니다: {error}")
    except asyncio.TimeoutError:
        await status(ws, session, "error", "음성 인식 모델 준비 시간이 초과되었습니다.")
    finally:
        for task in tasks:
            task.cancel()
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        if session.hls_dir:
            shutil.rmtree(session.hls_dir, ignore_errors=True)


def keep(session: Session):
    sessions[session.id] = session
    while len(sessions) > KEPT_SESSIONS:
        sessions.popitem(last=False)


@app.middleware("http")
async def no_cache_playlists(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.endswith(".m3u8"):
        response.headers["Cache-Control"] = "no-cache, no-store"
    return response


@app.get("/health")
async def health():
    try:
        async with websockets.connect(WHISPER_URL, open_timeout=3):
            whisper = "up"
    except Exception as error:
        whisper = f"down: {error}"
    return {"ok": True, "whisper": whisper, "model": WHISPER_MODEL, "sessions": len(sessions)}


@app.get("/sessions/{session_id}.{fmt}", response_class=PlainTextResponse)
async def export(session_id: str, fmt: str):
    session = sessions.get(session_id)
    if session is None or fmt not in {"srt", "vtt"}:
        raise HTTPException(404)
    body = to_srt(session.export()) if fmt == "srt" else to_webvtt(session.export())
    return PlainTextResponse(body, media_type="text/vtt" if fmt == "vtt" else "application/x-subrip")


@app.websocket("/ws")
async def captions(ws: WebSocket):
    if API_TOKEN and ws.query_params.get("token") != API_TOKEN:
        await ws.close(code=4401)
        return
    await ws.accept()
    runner = None
    try:
        while True:
            message = json.loads(await ws.receive_text())
            if message.get("type") == "start":
                if runner and not runner.done():
                    runner.cancel()
                url = str(message.get("url") or "").strip()
                if urlparse(url).scheme.lower() not in ALLOWED_SCHEMES:
                    # Never let a remote client point FFmpeg at local files or devices.
                    await send(ws, {"type": "status", "state": "error",
                                    "detail": "http(s), rtmp, srt, rtp 스트림 주소만 사용할 수 있습니다."})
                    continue
                # language None = Whisper auto-detect
                language = message["language"] if "language" in message else "ko"
                keywords = message.get("keywords")
                keywords = keywords[:MAX_KEYWORD_CHARS] if isinstance(keywords, str) else ""
                session = Session(url, language, bool(message.get("diarization")),
                                  int(message.get("max_speakers") or 4), bool(message.get("video")),
                                  keywords, message.get("topic"))
                keep(session)
                runner = asyncio.create_task(run_session(ws, session))
            elif message.get("type") == "stop" and runner:
                runner.cancel()
                await send(ws, {"type": "status", "state": "stopped", "detail": "중지했습니다."})
            elif message.get("type") == "ping":
                await send(ws, {"type": "pong", "t": message.get("t")})
    except (WebSocketDisconnect, json.JSONDecodeError):
        pass
    finally:
        if runner and not runner.done():
            runner.cancel()


app.mount("/hls", StaticFiles(directory=HLS_ROOT, check_dir=False), name="hls")
if WEB_DIR.is_dir():
    # Registered last so /ws, /health, /sessions and /hls win over the page's catch-all.
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
