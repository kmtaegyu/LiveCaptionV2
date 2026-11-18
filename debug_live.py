"""Opt-in real network probe: python debug_live.py <YouTube live URL>."""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtMultimedia import QAudioBufferOutput
from PySide6.QtWidgets import QApplication

from app_logging import LOG_PATH, configure_logging, install_diagnostics
from main import PlayerWindow


def main():
    if len(sys.argv) != 2:
        print("Usage: python debug_live.py <YouTube live URL>")
        return 2
    configure_logging(LOG_PATH.with_name("live-probe.log"))
    install_diagnostics()
    app = QApplication([])
    window = PlayerWindow()
    window.audio.setMuted(True)
    counts = {"video": 0, "audio": 0}
    audio_output = QAudioBufferOutput(window)
    window.player.setAudioBufferOutput(audio_output)

    def video_frame(frame):
        if frame.isValid():
            counts["video"] += 1

    def audio_buffer(buffer):
        if buffer.isValid():
            counts["audio"] += 1

    window.video_view.video_item.videoSink().videoFrameChanged.connect(video_frame)
    audio_output.audioBufferReceived.connect(audio_buffer)
    window.stream_resolver.failed.connect(lambda error: finish(1, error))
    window.player.errorOccurred.connect(lambda _code, error: finish(1, error))
    started = time.monotonic()
    done = False

    def finish(code, message):
        nonlocal done
        if done:
            return
        done = True
        print(f"exit={code} video_frames={counts['video']} audio_buffers={counts['audio']} "
              f"position_ms={window.player.position()} size={window.last_video_size} "
              f"elapsed={time.monotonic() - started:.1f}s")
        if code:
            from app_logging import redact_urls
            print(redact_urls(message))
        window.close()
        app.exit(code)

    def check():
        if counts["video"] >= 10 and counts["audio"] >= 2 and window.player.position() >= 1000:
            finish(0, "Video and audio decoded")
        elif time.monotonic() - started > 75:
            finish(1, "Timed out waiting for decoded video and audio")

    timer = QTimer()
    timer.timeout.connect(check)
    timer.start(250)
    window.url_input.setText(sys.argv[1])
    QTimer.singleShot(0, window.open_url)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
