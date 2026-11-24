"""Measure caption latency of WhisperLive settings on a fixed recording.

Live content changes from minute to minute, so settings are compared on one
recording replayed at real-time pace, exactly as caption_api feeds the model.

    # once: record 3 minutes of a live stream as 16 kHz mono s16le
    python bench_latency.py record https://www.youtube.com/@ytnnews24/live bench/ytn.pcm --seconds 180
    # per setting (WhisperLive started with the beam size under test)
    python bench_latency.py run bench/ytn.pcm --whisper ws://127.0.0.1:9095 --same-output 7 --out bench/b5_t7.json
    python bench_latency.py compare bench/b5_t7.json bench/b1_t3.json ...

Reported per caption:
  first  = audio fed when the caption first appeared - its start  (speech start -> on screen)
  final  = audio fed when it was finalised - its end               (speech end -> final)
  broken = share of final captions that stop mid-sentence (no 다/요/까/. ending)
"""
import argparse
import asyncio
import difflib
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from segments import SegmentTracker, normalize  # noqa: E402

SAMPLE_RATE = 16000
CHUNK = SAMPLE_RATE // 10 * 2          # 100 ms, like caption_api
# Same rule as the player's subtitles.ends_sentence.
SENTENCE_END = re.compile(r"(?:[.?!…。]|[다요죠까네](?:[.?!…]*))[\"'”’)\]]*$")


def broken_share(texts) -> float:
    return sum(not SENTENCE_END.search(text.strip()) for text in texts) / len(texts) if texts else 0.0


def record(url: str, out: str, seconds: int):
    from media import ffmpeg_binary, ffmpeg_env, resolve_media
    media = resolve_media(url, want_video=False)
    command = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", media["audio"],
               "-t", str(seconds), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-y", out]
    subprocess.run(command, check=True, env=ffmpeg_env())
    size = Path(out).stat().st_size
    print(f"recorded {size / 2 / SAMPLE_RATE:.1f}s of '{media['title']}' to {out}")


async def run(pcm: str, whisper_url: str, same_output: int, language: str, out: str, tail: float,
              hotwords: str | None = None):
    import websockets
    audio = Path(pcm).read_bytes()
    tracker = SegmentTracker()
    first, finals, texts = {}, {}, {}
    fed = 0.0
    options = {"uid": f"bench-{time.time():.0f}", "language": language, "task": "transcribe",
               "model": "large-v3-turbo", "use_vad": True, "audio_format": "int16",
               "send_last_n_segments": 10, "no_speech_thresh": 0.45, "same_output_threshold": same_output}
    if hotwords:
        options["hotwords"] = hotwords
    async with websockets.connect(whisper_url, max_size=None, open_timeout=30) as ws:
        await ws.send(json.dumps(options))
        ready = json.loads(await ws.recv())
        assert ready.get("message") == "SERVER_READY", ready

        async def feed():
            nonlocal fed
            started = time.monotonic()
            for offset in range(0, len(audio), CHUNK):
                await ws.send(audio[offset:offset + CHUNK])
                fed = (offset + CHUNK) / 2 / SAMPLE_RATE
                # Real-time pace against the wall clock, so model time is never "free".
                delay = started + fed - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            await asyncio.sleep(tail)
            await ws.send(b"END_OF_AUDIO")

        async def receive():
            async for raw in ws:
                message = json.loads(raw)
                for event in tracker.update(message.get("segments") or []):
                    if event.get("remove"):
                        first.pop(event["id"], None)
                        continue
                    now = fed
                    first.setdefault(event["id"], now - event["stream_start"] / 1000)
                    if event["final"]:
                        finals[event["id"]] = now - event["stream_end"] / 1000
                        texts[event["id"]] = event["text"]

        receiver = asyncio.create_task(receive())
        await feed()
        receiver.cancel()
    ids = [caption for caption in texts]
    result = {"pcm": pcm, "whisper": whisper_url, "same_output": same_output, "hotwords": hotwords,
              "first": [round(first[i], 3) for i in ids if i in first],
              "final": [round(finals[i], 3) for i in ids],
              "text": [texts[i] for i in ids]}
    Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(summary(Path(out).stem, result))


def reference(pcm: str, out: str, model: str, language: str):
    """Offline transcript of the whole recording (full context, beam 5): the accuracy yardstick."""
    import numpy as np
    from faster_whisper import WhisperModel
    audio = np.frombuffer(Path(pcm).read_bytes(), dtype=np.int16).astype(np.float32) / 32768
    whisper = WhisperModel(model, device="cuda", compute_type="float16")
    segments, _info = whisper.transcribe(audio, language=language, beam_size=5, vad_filter=True)
    texts = [segment.text.strip() for segment in segments if segment.text.strip()]
    Path(out).write_text(json.dumps({"pcm": pcm, "first": [], "final": [], "text": texts},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"reference: {len(texts)} segments, {sum(map(len, texts))} characters -> {out}")


def summary(name, result, reference=None):
    def stats(values):
        if not values:
            return "  -  "
        ordered = sorted(values)
        p90 = ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))]
        return f"{statistics.median(values):5.2f} / {p90:5.2f}"
    line = (f"{name:14} captions={len(result['final']):3}  first med/p90 {stats(result['first'])}s"
            f"  final med/p90 {stats(result['final'])}s  broken {broken_share(result['text']):4.0%}")
    if reference is not None:
        a = normalize(" ".join(reference["text"]))
        b = normalize(" ".join(result["text"]))
        line += f"  text match {difflib.SequenceMatcher(None, a, b, autojunk=False).ratio():.3f}"
    return line


def compare(paths):
    results = [(Path(p).stem, json.loads(Path(p).read_text(encoding="utf-8"))) for p in paths]
    reference = results[0][1]
    for name, result in results:
        print(summary(name, result, reference))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("url")
    rec.add_argument("out")
    rec.add_argument("--seconds", type=int, default=180)
    go = sub.add_parser("run")
    go.add_argument("pcm")
    go.add_argument("--whisper", default="ws://127.0.0.1:9095")
    go.add_argument("--same-output", type=int, default=7)
    go.add_argument("--language", default="ko")
    go.add_argument("--tail", type=float, default=8.0, help="seconds to wait for the last captions")
    go.add_argument("--hotwords", help="space-separated hotwords, as caption_api would send")
    go.add_argument("--out", required=True)
    ref = sub.add_parser("reference")
    ref.add_argument("pcm")
    ref.add_argument("--out", required=True)
    ref.add_argument("--model", default="mobiuslabsgmbh/faster-whisper-large-v3-turbo")
    ref.add_argument("--language", default="ko")
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("results", nargs="+")
    args = parser.parse_args()
    if args.command == "record":
        record(args.url, args.out, args.seconds)
    elif args.command == "reference":
        reference(args.pcm, args.out, args.model, args.language)
    elif args.command == "run":
        asyncio.run(run(args.pcm, args.whisper, args.same_output, args.language, args.out, args.tail,
                        args.hotwords))
    else:
        compare(args.results)


if __name__ == "__main__":
    main()
