"""Keep a caption session running for hours without the player, to fill the caption logs.

    python collect_captions.py https://www.youtube.com/@ytnnews24/live [more urls ...] \
        --server 127.0.0.1:18100 --topic politics --hours 8

caption_api writes the captions to logs/captions itself; this only keeps asking for them.
When a session ends (the broadcast stopped, its stream address expired, an error) or goes
quiet for --quiet-minutes, the next URL in the list is started after a short wait, round
and round. It cannot bring back a dropped SSH tunnel to the GPU server: it then keeps
retrying and says so. Review the result with review_captions.py.
"""
import argparse
import asyncio
import json
import time

import websockets

RETRY_SECONDS = 30


def say(text: str):
    print(f"{time.strftime('%H:%M:%S')}  {text}", flush=True)


async def one_session(server: str, url: str, topic, keywords, quiet_seconds: float, deadline: float) -> int:
    """Run one caption session until it ends, stalls or the deadline passes; return final captions seen."""
    finals = 0
    async with websockets.connect(f"ws://{server}/ws", max_size=None, open_timeout=15) as ws:
        start = {"type": "start", "url": url, "language": "ko", "diarization": False}
        if topic:
            start["topic"] = topic
        if keywords:
            start["keywords"] = keywords
        await ws.send(json.dumps(start, ensure_ascii=False))
        last_caption = time.monotonic()
        while time.monotonic() < deadline:
            try:
                message = json.loads(await asyncio.wait_for(ws.recv(), 30))
            except asyncio.TimeoutError:
                message = {}
            kind = message.get("type")
            if kind == "caption" and message.get("final"):
                finals += 1
                last_caption = time.monotonic()
            elif kind == "session":
                say(f"방송: {message.get('title')}")
            elif kind == "status":
                state = message.get("state")
                if state in {"error", "ended", "stopped"}:
                    say(f"세션 종료({state}): {message.get('detail')}")
                    return finals
            if time.monotonic() - last_caption > quiet_seconds:
                say(f"{quiet_seconds / 60:g}분 동안 자막이 없어 다시 시작합니다.")
                break
        try:
            await ws.send(json.dumps({"type": "stop"}))
        except websockets.ConnectionClosed:
            pass
    return finals


async def collect(args):
    deadline = time.monotonic() + args.hours * 3600
    total, index, failures = 0, 0, 0
    say(f"{args.hours:g}시간 동안 수집합니다. 주소 {len(args.urls)}개, 주제 사전: {args.topic or '없음'}")
    while time.monotonic() < deadline:
        url = args.urls[index % len(args.urls)]
        index += 1
        say(f"시작: {url}")
        try:
            finals = await one_session(args.server, url, args.topic, args.keywords,
                                       args.quiet_minutes * 60, deadline)
        except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as error:
            say(f"자막 API({args.server})에 연결하지 못했습니다: {error}")
            finals = 0
        total += finals
        failures = 0 if finals else failures + 1
        say(f"이번 세션 확정 자막 {finals}개, 누적 {total}개")
        if failures >= 3:
            say("세 번 연속 자막이 없습니다. SSH 터널(PS1)과 방송 주소를 확인하세요. 계속 다시 시도합니다.")
        if time.monotonic() < deadline:
            await asyncio.sleep(min(RETRY_SECONDS * max(1, failures), 300))
    say(f"수집 끝. 확정 자막 누적 {total}개. 기록은 logs/captions 에 있습니다.")


def main():
    parser = argparse.ArgumentParser(description="Collect LiveCaption captions for hours")
    parser.add_argument("urls", nargs="+", help="live stream URLs, tried in turn")
    parser.add_argument("--server", default="127.0.0.1:18100", help="caption API host:port")
    parser.add_argument("--topic", default="politics")
    parser.add_argument("--keywords", default="")
    parser.add_argument("--hours", type=float, default=8)
    parser.add_argument("--quiet-minutes", type=float, default=10,
                        help="restart a session that produced no caption for this long")
    args = parser.parse_args()
    try:
        asyncio.run(collect(args))
    except KeyboardInterrupt:
        say("중지했습니다.")


if __name__ == "__main__":
    main()
