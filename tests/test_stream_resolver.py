import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QProcess, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from stream_resolver import StreamResolver, is_youtube_url, parse_stream_url
from stream_resolver import FORMAT_SELECTION, select_playback_url


class ScriptProcess(QProcess):
    """Run a local script to exercise real asynchronous process lifecycle."""
    script = "import json; print(json.dumps({'url':'https://example.com/live.m3u8','vcodec':'h264','acodec':'aac'}))"

    calls = []

    def setArguments(self, arguments):
        self.requested_arguments = arguments
        ScriptProcess.calls.append(arguments)
        super().setArguments(["-c", self.script])


class StreamResolverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.resolver = StreamResolver()
        self.results = []
        self.errors = []
        self.resolver.resolved.connect(self.results.append)
        self.resolver.failed.connect(self.errors.append)

    def tearDown(self):
        self.resolver.cancel()
        self.resolver.deleteLater()
        self.app.processEvents()

    def wait_for_result(self):
        deadline = time.monotonic() + 5
        while not self.results and not self.errors and time.monotonic() < deadline:
            QTest.qWait(20)

    def test_host_and_stream_validation(self):
        for host in ("www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be"):
            self.assertTrue(is_youtube_url(QUrl(f"https://{host}/watch?v=test")))
        self.assertFalse(is_youtube_url(QUrl("https://youtube.com.example.com/test")))
        self.assertEqual(parse_stream_url("https://example.com/live.m3u8\n"), "https://example.com/live.m3u8")
        for output in ("", "file:///tmp/video", "https://a.com/video\nhttps://a.com/audio"):
            with self.assertRaises(ValueError):
                parse_stream_url(output)

    def test_success_and_command(self):
        ScriptProcess.calls = []
        with patch("stream_resolver.QProcess", ScriptProcess):
            self.resolver.resolve(QUrl("https://youtu.be/test"))
            # streamlink is asked first for a live playlist...
            first = self.resolver.process.requested_arguments
            self.assertEqual(first[:2], ["-m", "streamlink_cli"])
            self.assertIn("--no-config", first)
            self.assertEqual(first[-3:], ["--", "https://youtu.be/test", "best"])
            self.wait_for_result()
        # ...and its non-HLS answer falls back to yt-dlp.
        args = ScriptProcess.calls[-1]
        self.assertEqual(len(ScriptProcess.calls), 2)
        self.assertIn("--ignore-config", args)
        self.assertIn(FORMAT_SELECTION, args)
        self.assertIn("--dump-single-json", args)
        self.assertEqual(args[-2:], ["--", "https://youtu.be/test"])
        self.assertEqual(self.results, ["https://example.com/live.m3u8"])
        self.assertFalse(self.errors)

    def test_channel_live_url_is_pinned_to_the_resolved_video(self):
        script = ("import json; print(json.dumps({'url':'https://example.com/live.m3u8','vcodec':'h264',"
                  "'acodec':'aac','id':'PPZzpgEpgYk','extractor_key':'Youtube'}))")
        with patch("stream_resolver.QProcess", ScriptProcess), patch.object(ScriptProcess, "script", script):
            self.resolver.resolve(QUrl("https://www.youtube.com/@ytnnews24/live"))
            self.wait_for_result()
        self.assertEqual(self.resolver.video_url, "https://www.youtube.com/watch?v=PPZzpgEpgYk")

    def test_extractor_error(self):
        script = "import sys; print('ERROR: Live stream is offline', file=sys.stderr); sys.exit(1)"
        with patch("stream_resolver.QProcess", ScriptProcess), patch.object(ScriptProcess, "script", script):
            self.resolver.resolve(QUrl("https://youtu.be/test"))
            self.wait_for_result()
        self.assertFalse(self.results)
        self.assertIn("offline", self.errors[0])

    def test_separate_tracks_use_shared_master(self):
        tracks = [
            {"url": "https://example.com/video.m3u8", "manifest_url": "https://example.com/master.m3u8", "protocol": "m3u8_native", "acodec": "none"},
            {"url": "https://example.com/audio.m3u8", "manifest_url": "https://example.com/master.m3u8", "protocol": "m3u8_native", "vcodec": "none"},
        ]
        self.assertEqual(select_playback_url({"requested_formats": tracks}), "https://example.com/master.m3u8")
        import json
        script = f"print({json.dumps({'requested_formats': tracks})!r})"
        with patch("stream_resolver.QProcess", ScriptProcess), patch.object(ScriptProcess, "script", script):
            self.resolver.resolve(QUrl("https://youtu.be/test"))
            self.wait_for_result()
        self.assertEqual(self.results, ["https://example.com/master.m3u8"])
        tracks[1]["manifest_url"] = "https://example.com/other.m3u8"
        with self.assertRaises(ValueError):
            select_playback_url({"requested_formats": tracks})

    def test_invalid_or_incomplete_metadata(self):
        for info in ([], {}, {"requested_formats": [None]}, {"url": "https://example.com/video", "acodec": "none"}):
            with self.subTest(info=info), self.assertRaises(ValueError):
                select_playback_url(info)

    def test_cancel_and_timeout(self):
        with patch("stream_resolver.QProcess", ScriptProcess), patch.object(ScriptProcess, "script", "import time; time.sleep(10)"):
            self.resolver.resolve(QUrl("https://youtu.be/test"))
            self.resolver.cancel()
            QTest.qWait(50)
            self.assertFalse(self.results or self.errors)
            self.resolver.resolve(QUrl("https://youtu.be/test"))
            self.resolver._timeout(self.resolver.process)
            self.assertIsNone(self.resolver.process)
            self.assertIn("초과", self.errors[0])
