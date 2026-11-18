"""Manual end-to-end probe against the real GPU server (not part of unittest).

    .venv\\Scripts\\python.exe tests\\live_server_probe.py URL [--server HOST:PORT] [--seconds 90]

Prints every status and caption event with its arrival time, then a summary of
time-to-first-caption, interim/final counts and the server-measured lag.
"""
import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QCoreApplication, QTimer  # noqa: E402

from remote_captions import DEFAULT_SERVER, RemoteCaptionClient  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--seconds", type=int, default=90)
    parser.add_argument("--language", default="ko")
    parser.add_argument("--diarization", action="store_true")
    args = parser.parse_args()
    app = QCoreApplication(sys.argv)
    client = RemoteCaptionClient()
    started = time.monotonic()
    stats = {"interim": 0, "final": 0, "removed": 0, "first": None, "running": None, "lags": []}

    def stamp():
        return f"{time.monotonic() - started:6.1f}s"

    def on_status(state, detail):
        print(f"{stamp()} [status] {state}: {detail}", flush=True)
        if state == "running" and stats["running"] is None:
            stats["running"] = time.monotonic() - started
        if state in {"error", "ended"}:
            QTimer.singleShot(500, app.quit)

    def on_caption(event):
        if event.get("remove"):
            stats["removed"] += 1
            return
        kind = "final" if event["final"] else "interim"
        stats[kind] += 1
        if stats["first"] is None:
            stats["first"] = time.monotonic() - started
        if event["final"]:
            if event.get("lag") is not None:
                stats["lags"].append(event["lag"])
            speaker = f"{event['speaker']}: " if event.get("speaker") else ""
            print(f"{stamp()} [final {event['id']} lag={event.get('lag')}ms] {speaker}{event['text']}", flush=True)

    client.status.connect(on_status)
    client.caption.connect(on_caption)
    client.session_started.connect(lambda s: print(f"{stamp()} [session] {s}", flush=True))
    client.start(args.server, args.url, args.language, args.diarization)
    QTimer.singleShot(args.seconds * 1000, app.quit)
    app.exec()
    export = client.export_url("srt")
    client.stop()
    lags = sorted(stats["lags"])
    print("\n--- summary ---")
    print(f"running after {stats['running']}, first caption after {stats['first']}")
    print(f"interim updates {stats['interim']}, finals {stats['final']}, removed {stats['removed']}")
    if lags:
        print(f"lag ms: min {lags[0]}, median {lags[len(lags) // 2]}, max {lags[-1]}")
    print(f"srt: {export}")


if __name__ == "__main__":
    main()
