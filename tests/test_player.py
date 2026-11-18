"""Headless integration checks using generated PCM audio, no media download."""
import functools
import http.server
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave
import logging
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LIVECAPTION_SETTINGS", os.path.join(
    __import__("tempfile").mkdtemp(prefix="livecaption-test-"), "settings.ini"))

from PySide6.QtCore import QUrl, QSize
from PySide6.QtGui import QFontMetrics, QImage, QColor, QFontDatabase
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QMediaMetaData, QPlaybackOptions
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from main import PlayerWindow, format_time


class PlayerSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # Offscreen Qt does not discover Windows font fallback automatically.
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/malgun.ttf"
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        self.window = PlayerWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_initial_state_and_invalid_url(self):
        self.assertFalse(self.window.play_button.isEnabled())
        self.window.url_input.setText("file:///untrusted.mp4")
        self.window.open_url()
        self.assertTrue(self.window.player.source().isEmpty())
        self.assertIn("HTTP(S)", self.window.statusBar().currentMessage())
        self.assertEqual(format_time(3661000), "01:01:01")

    def check_playback(self, url):
        self.window.open_source(url)
        deadline = time.monotonic() + 8
        while self.window.player.position() == 0 and time.monotonic() < deadline:
            QTest.qWait(50)
        self.assertEqual(self.window.player.error(), QMediaPlayer.Error.NoError)
        self.assertGreater(self.window.player.position(), 0)
        self.window.toggle_play()
        self.assertEqual(self.window.player.playbackState(), QMediaPlayer.PlaybackState.PausedState)
        self.window.volume.setValue(25)
        self.assertAlmostEqual(self.window.audio.volume(), 0.25)
        self.window.player.setPosition(1000)
        self.assertEqual(self.window.player.position(), 1000)
        self.window.player.stop()
        self.assertEqual(self.window.player.playbackState(), QMediaPlayer.PlaybackState.StoppedState)

    def test_youtube_routing_and_stop(self):
        with patch.object(self.window.stream_resolver, "resolve") as resolve:
            self.window.url_input.setText("https://www.youtube.com/watch?v=test")
            self.window.open_url()
            resolve.assert_called_once_with(QUrl("https://www.youtube.com/watch?v=test"))
            self.assertIn("확인 중", self.window.statusBar().currentMessage())
        with patch.object(self.window, "open_source") as open_source:
            self.window.stream_resolver.resolved.emit("https://example.com/live.m3u8")
            open_source.assert_called_once_with(QUrl("https://example.com/live.m3u8"), live=False)
        with patch.object(self.window.stream_resolver, "cancel") as cancel:
            self.window.stop_playback()
            cancel.assert_called_once()

    def test_highest_quality_track_and_live_options(self):
        tracks = []
        for size, fps in ((QSize(256, 144), 15), (QSize(1920, 1080), 30), (QSize(1920, 1080), 60)):
            metadata = QMediaMetaData()
            metadata.insert(QMediaMetaData.Key.Resolution, size)
            metadata.insert(QMediaMetaData.Key.VideoFrameRate, fps)
            tracks.append(metadata)
        self.window.prefer_best_video = True
        with patch.object(self.window.player, "videoTracks", return_value=tracks), patch.object(self.window.player, "setActiveVideoTrack") as select:
            self.window.select_video_quality()
            select.assert_called_once_with(2)
        with patch.object(self.window.player, "setSource"), patch.object(self.window.player, "play"):
            self.window.open_source(QUrl("https://manifest.googlevideo.com/master.m3u8"), live=True)
            self.assertTrue(self.window.prefer_best_video)
            self.assertEqual(self.window.player.playbackOptions().playbackIntent(), QPlaybackOptions.PlaybackIntent.LowLatencyStreaming)
            self.window.open_source(QUrl.fromLocalFile("C:/video.mp4"))
            self.assertFalse(self.window.prefer_best_video)
            self.assertEqual(self.window.player.playbackOptions().playbackIntent(), QPlaybackOptions.PlaybackIntent.Playback)

    def test_log_panel_redaction_and_handler_cleanup(self):
        logger = logging.getLogger("livecaption")
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            logger.info("Test request https://example.com/stream?token=private")
            self.app.processEvents()
            text = self.window.log_text.toPlainText()
            self.assertIn("Test request", text)
            self.assertNotIn("private", text)
            self.assertIn("REDACTED", text)
            self.window.log_dock.show()
            self.assertFalse(self.window.log_dock.isHidden())
            self.window.close()
            self.assertNotIn(self.window.log_handler, logger.handlers)
        finally:
            logger.setLevel(previous_level)

    def test_subtitle_load_sync_clear_and_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.srt"
            path.write_text("1\n00:00:01,000 --> 00:00:02,000\n한글 자막\n두 줄", encoding="utf-8")
            self.assertTrue(self.window.load_subtitle(str(path)))
            self.window.update_position(1000)
            self.assertEqual(self.window.subtitle_label.text(), "한글 자막 두 줄")
            self.window.update_position(2000)
            self.assertEqual(self.window.subtitle_label.text(), "")
            self.window.update_position(1500)
            self.assertEqual(self.window.subtitle_label.text(), "한글 자막 두 줄")
            path.write_text("invalid", encoding="utf-8")
            self.assertFalse(self.window.load_subtitle(str(path)))
            self.assertEqual(len(self.window.subtitles), 1)
            self.window.clear_subtitles()
            self.assertEqual(self.window.subtitle_label.text(), "")
            self.assertEqual(self.window.subtitles, [])

    def test_subtitle_overlay_render_and_resize(self):
        self.window.show()
        QTest.qWait(50)
        view = self.window.video_view
        image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32)
        image.fill(QColor("#2878b8"))
        view.video_item.videoSink().setVideoFrame(QVideoFrame(image))
        QTest.qWait(50)
        before = view.grab().toImage()
        view.set_subtitle("영상 위 오버레이 자막\n두 번째 줄")
        QTest.qWait(50)
        after = view.grab().toImage()
        rect = view.subtitle_item.sceneBoundingRect()
        self.assertTrue(view.sceneRect().contains(rect))
        x, y = int(rect.left() + 12), int(rect.top() + 12)
        self.assertEqual(after.pixelColor(x, y), before.pixelColor(x, y))
        self.assertEqual(view.subtitle_label.text(), "영상 위 오버레이 자막 두 번째 줄")
        self.assertFalse(view.subtitle_label.wordWrap())
        self.assertTrue(any(after.pixelColor(px, py).red() > 220
                            for px in range(int(rect.left()), int(rect.right()))
                            for py in range(int(rect.top()), int(rect.bottom()))))
        # Both the white glyph fill and black outline must render on the video.
        self.assertTrue(any(after.pixelColor(px, py).red() < 20
                            and after.pixelColor(px, py).green() < 20
                            and after.pixelColor(px, py).blue() < 20
                            for px in range(int(rect.left()), int(rect.right()))
                            for py in range(int(rect.top()), int(rect.bottom()))))
        after.save(str(Path(tempfile.gettempdir()) / "LiveCaption-overlay-preview.png"))
        self.window.resize(800, 480)
        QTest.qWait(50)
        self.assertTrue(view.sceneRect().contains(view.subtitle_item.sceneBoundingRect()))
        self.assertAlmostEqual(view.subtitle_item.sceneBoundingRect().bottom(), view.sceneRect().bottom() - 24)
        # A sentence too wide for one line wraps into two balanced lines at full size.
        sentence = ("AI를 이용해 부정행위를 저질렀다고 자수한 세무사 시험 수석합격자 한모 씨가 "
                    "14시간 가까운 경찰 조사를 마치고 또다시 허리를 숙였습니다.")
        self.window.resize(1280, 720)
        QTest.qWait(50)
        view.set_subtitle(sentence)
        lines = view.subtitle_label.lines
        self.assertEqual(len(lines), 2)
        self.assertEqual(" ".join(lines), sentence)
        base = round(22 * max(1.0, view.viewport().height() / 540))
        self.assertEqual(view.subtitle_label.font().pixelSize(), base)   # wrapped, not shrunk
        metrics = QFontMetrics(view.subtitle_label.font())
        widths = [metrics.horizontalAdvance(line) for line in lines]
        self.assertLess(max(widths) - min(widths), max(widths) * 0.35)    # balanced, not line + stub
        self.assertLessEqual(max(widths), view.viewport().width() * 0.88)
        self.assertTrue(view.sceneRect().contains(view.subtitle_item.sceneBoundingRect()))
        # Absurdly long text shrinks and stays within three lines on screen.
        view.set_subtitle("아주 긴 한 줄 자막 " * 30)
        self.assertLess(view.subtitle_label.font().pixelSize(), 22)
        self.assertLessEqual(len(view.subtitle_label.lines), 3)
        self.assertTrue(view.sceneRect().contains(view.subtitle_item.sceneBoundingRect()))
        view.set_subtitle("")
        self.assertFalse(view.subtitle_item.isVisible())

    def test_local_and_http_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.wav"
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(b"\x00\x00" * 8000 * 3)
            self.check_playback(QUrl.fromLocalFile(str(path)))
            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.check_playback(QUrl(f"http://127.0.0.1:{server.server_port}/sample.wav"))
            finally:
                self.window.player.setSource(QUrl())
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
