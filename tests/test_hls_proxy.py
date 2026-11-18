"""Playlist trimming and the loopback proxy against a local upstream server."""
import gzip
import http.server
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QProcess, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from hls_proxy import (KEEP_SEGMENTS, START_SEGMENTS, HlsProxy, consecutive_tail, first_pts,
                       trim_playlist)
from stream_resolver import StreamResolver, parse_streamlink_output, select_live_tracks


def playlist(count, first=100):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1", f"#EXT-X-MEDIA-SEQUENCE:{first}",
             "#EXT-X-PROGRAM-DATE-TIME:2026-11-11T21:54:33.761+00:00", '#EXT-X-MAP:URI="init.mp4"']
    for index in range(count):
        lines += ['#EXT-X-DATERANGE:CLASS="CUEPOINT-AD",ID="x"', "#EXTINF:1.0,", f"seg{first + index}.ts"]
    return "\n".join(lines) + "\n"


def pes_pts(value):
    return bytes([0x21 | ((value >> 29) & 0x0E), (value >> 22) & 0xFF, ((value >> 14) & 0xFE) | 1,
                  (value >> 7) & 0xFF, ((value << 1) & 0xFE) | 1])


def ts_segment(seconds, packets=600):
    """MPEG-TS: a packet without a PES start, then a video PES (after an adaptation field) with the PTS."""
    filler = bytes([0x47, 0x40, 0x00, 0x10]) + bytes(184)
    pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + pes_pts(round(seconds * 90000))
    video = bytes([0x47, 0x41, 0x00, 0x30, 7]) + bytes(7) + pes
    video += bytes(188 - len(video))
    return filler + video + filler * packets


def synchsafe(size):
    return bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])


def packed_audio(seconds):
    """HLS packed audio: ID3v2.4 tag with Apple's transportStreamTimestamp PRIV frame, then ADTS."""
    body = b"com.apple.streaming.transportStreamTimestamp\x00" + round(seconds * 90000).to_bytes(8, "big")
    frame = b"PRIV" + synchsafe(len(body)) + b"\x00\x00" + body
    return b"ID3\x04\x00\x00" + synchsafe(len(frame)) + frame + b"\xff\xf1" + bytes(2000)


SEGMENTS = {"/v100.ts": ts_segment(1749.995), "/a100.aac": packed_audio(1749.98)}
SLOW_SEGMENT = ts_segment(12.5, packets=2000)


class Upstream(http.server.BaseHTTPRequestHandler):
    requests = []
    short_first = 500

    def do_GET(self):
        Upstream.requests.append((self.path, self.headers.get("Accept-Encoding", "")))
        if self.path == "/missing.m3u8":
            self.send_error(404)
            return
        if self.path == "/slow_video.m3u8":
            body = b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:100\n#EXTINF:1.0,\nslow.ts\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/slow.ts":
            # Half now, half later: the proxy must stream what it has while the rest arrives.
            half = len(SLOW_SEGMENT) // 2
            self.send_response(200)
            self.send_header("Content-Length", str(len(SLOW_SEGMENT)))
            self.end_headers()
            self.wfile.write(SLOW_SEGMENT[:half])
            self.wfile.flush()
            time.sleep(0.6)
            self.wfile.write(SLOW_SEGMENT[half:])
            return
        if self.path == "/short.m3u8":
            # A short live playlist (three segments) that moves on by one segment per load.
            first, Upstream.short_first = Upstream.short_first, Upstream.short_first + 1
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:1", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
            for number in range(first, first + 3):
                lines += ["#EXTINF:1.0,", f"sq/{number}/seg.ts"]
            body = ("\n".join(lines) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in SEGMENTS or self.path in ("/clock_video.m3u8", "/clock_audio.m3u8"):
            name = "v100.ts" if "video" in self.path else "a100.aac"
            body = SEGMENTS.get(self.path) or (
                f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:100\n#EXTINF:1.0,\n{name}\n").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = gzip.compress(playlist(3600).encode())
        self.send_response(200)
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class HlsProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.origin = f"http://127.0.0.1:{cls.upstream.server_port}"
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def fetch(self, url):
        """Blocking client in a thread while the Qt loop runs the proxy."""
        result = []

        def run():
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    result.append((response.status, response.read().decode("latin-1")))
            except urllib.error.HTTPError as error:
                result.append((error.code, ""))
            except OSError as error:
                result.append((0, str(error)))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 8
        while not result and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(result, "proxy did not answer")
        return result[0]

    def test_trim_keeps_tail_sequence_and_init_segment(self):
        trimmed = trim_playlist(playlist(3600), "https://cdn.example.com/live/index.m3u8", keep=30)
        lines = trimmed.splitlines()
        self.assertEqual(lines[0], "#EXTM3U")
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:3670", lines)
        self.assertIn('#EXT-X-MAP:URI="init.mp4"', lines)
        self.assertEqual(trimmed.count("#EXTINF"), 30)
        self.assertEqual(lines[-1], "https://cdn.example.com/live/seg3699.ts")
        self.assertNotIn("DATERANGE", trimmed)
        self.assertNotIn("PROGRAM-DATE-TIME", trimmed)
        self.assertLess(len(trimmed), 3000)
        # Short and finished playlists are passed through whole.
        self.assertEqual(trim_playlist(playlist(5)).count("#EXTINF"), 5)
        ended = trim_playlist(playlist(100) + "#EXT-X-ENDLIST\n", keep=30)
        self.assertEqual(ended.count("#EXTINF"), 100)
        self.assertTrue(ended.endswith("#EXT-X-ENDLIST\n"))
        with self.assertRaises(ValueError):
            trim_playlist("<html>error</html>")

    def test_proxy_serves_trimmed_playlists_and_rejects_stale_tokens(self):
        proxy = HlsProxy()
        Upstream.requests.clear()
        master_url = proxy.serve(f"{self.origin}/video.m3u8", f"{self.origin}/missing.m3u8", 1920, 1080, 5500000)
        base = master_url.rsplit("/", 1)[0]
        status, master = self.fetch(master_url)
        self.assertEqual(status, 200)
        self.assertIn("RESOLUTION=1920x1080", master)
        self.assertIn('URI="audio.m3u8"', master)
        self.assertTrue(master.rstrip().endswith("video.m3u8"))
        status, video = self.fetch(f"{base}/video.m3u8")
        self.assertEqual(status, 200)
        # The first load lists only the newest segments so FFmpeg starts at the live edge.
        self.assertEqual(video.count("#EXTINF"), START_SEGMENTS)
        self.assertIn(f"#EXT-X-MEDIA-SEQUENCE:{3700 - START_SEGMENTS}", video.splitlines())
        self.assertIn("seg/video/3699.ts", video.splitlines())
        self.assertEqual(proxy.windows["video.m3u8"][3699][2], f"{self.origin}/seg3699.ts")
        self.assertIn("gzip", Upstream.requests[0][1])
        # A reload inside the freshness window is answered from memory, with the full window.
        status, video = self.fetch(f"{base}/video.m3u8")
        self.assertEqual(video.count("#EXTINF"), KEEP_SEGMENTS)
        self.assertIn(f"#EXT-X-MEDIA-SEQUENCE:{3700 - KEEP_SEGMENTS}", video.splitlines())
        self.assertEqual(len([path for path, _ in Upstream.requests if path == "/video.m3u8"]), 1)
        self.assertEqual(self.fetch(f"{base}/audio.m3u8")[0], 502)
        self.assertEqual(self.fetch(f"{base}/other.m3u8")[0], 404)
        proxy.serve(f"{self.origin}/video.m3u8", f"{self.origin}/video.m3u8")
        self.assertEqual(self.fetch(master_url)[0], 404)
        proxy.deleteLater()

    def test_short_muxed_playlist_grows_into_a_rolling_window(self):
        proxy = HlsProxy()
        Upstream.short_first = 500
        master_url = proxy.serve(f"{self.origin}/short.m3u8")
        proxy.poller.stop()                 # drive reloads by hand so the counts are exact
        base = master_url.rsplit("/", 1)[0]
        status, master = self.fetch(master_url)
        self.assertNotIn("AUDIO", master)                            # muxed: one playlist only
        self.assertTrue(master.rstrip().endswith("video.m3u8"))
        self.assertEqual(self.fetch(f"{base}/audio.m3u8")[0], 404)
        with patch("hls_proxy.MAX_AGE", 0):
            listed = []
            for _ in range(5):
                text = self.fetch(f"{base}/video.m3u8")[1]
                listed.append([line for line in text.splitlines() if line.startswith("seg/")])
        # Upstream lists three segments and moves on by one per load; the proxy keeps them all.
        self.assertEqual(listed[0], [f"seg/video/{n}.ts" for n in range(503 - START_SEGMENTS, 503)])
        self.assertEqual(listed[-1][0], "seg/video/500.ts")
        self.assertEqual(listed[-1][-1], "seg/video/506.ts")
        self.assertEqual(len(listed[-1]), 7)
        # Reopening starts near the edge again.
        proxy.reset_clock()
        with patch("hls_proxy.MAX_AGE", 0):
            text = self.fetch(f"{base}/video.m3u8")[1]
        self.assertEqual([line for line in text.splitlines() if line.startswith("seg/")],
                         [f"seg/video/{n}.ts" for n in range(508 - START_SEGMENTS, 508)])
        # Background polls keep loading while the player is busy, so no segment is missed.
        with patch("hls_proxy.MAX_AGE", 0):
            for _ in range(4):
                before = proxy.fetched_at["video.m3u8"]
                proxy._poll()
                self.wait(lambda: proxy.fetched_at["video.m3u8"] != before)
        self.assertEqual(max(proxy.windows["video.m3u8"]), 511)
        self.assertEqual(len(consecutive_tail(proxy.windows["video.m3u8"])), 12)   # 500..511
        # Polling stops once no player has asked for a while.
        proxy.requested_at -= 60
        before = proxy.fetched_at["video.m3u8"]
        proxy._poll()
        QTest.qWait(200)
        self.assertEqual(proxy.fetched_at["video.m3u8"], before)
        proxy.deleteLater()

    def test_keep_alive_serves_several_requests_on_one_connection(self):
        import http.client
        proxy = HlsProxy()
        master_url = proxy.serve(f"{self.origin}/clock_video.m3u8", f"{self.origin}/clock_audio.m3u8")
        port = int(master_url.split(":")[2].split("/")[0])
        path = "/" + master_url.split("/", 3)[3].rsplit("/", 1)[0]
        result = []

        def run():
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            for name in ("video.m3u8", "seg/video/100.ts", "seg/video/100.ts"):
                connection.request("GET", f"{path}/{name}")
                response = connection.getresponse()
                body = response.read()
                result.append((response.status, response.getheader("Connection"), len(body)))
            connection.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.wait(lambda: len(result) == 3)
        size = len(SEGMENTS["/v100.ts"])
        self.assertEqual([item[:2] for item in result], [(200, "keep-alive")] * 3)
        self.assertEqual([item[2] for item in result[1:]], [size, size])
        proxy.deleteLater()

    def test_segment_still_downloading_is_streamed(self):
        proxy = HlsProxy()
        proxy.prefetching = False          # make the player's request start the download
        master_url = proxy.serve(f"{self.origin}/slow_video.m3u8")
        base = master_url.rsplit("/", 1)[0]
        self.assertIn("seg/video/100.ts", self.fetch(f"{base}/video.m3u8")[1].splitlines())
        status, body = self.fetch(f"{base}/seg/video/100.ts")
        self.assertEqual(status, 200)
        self.assertEqual(body, SLOW_SEGMENT.decode("latin-1"))
        proxy.deleteLater()

    def test_window_stops_at_a_gap(self):
        entry = lambda number: (number, ["#EXTINF:1.0,"], f"https://x/{number}.ts", {})
        window = {number: entry(number) for number in (1, 2, 5, 6, 7)}
        self.assertEqual([item[0] for item in consecutive_tail(window)], [5, 6, 7])

    def test_first_pts_of_ts_and_packed_audio(self):
        self.assertAlmostEqual(first_pts(ts_segment(1749.995, packets=2)), 1749.995, places=4)
        self.assertAlmostEqual(first_pts(packed_audio(487.804807)), 487.804807, places=4)
        self.assertIsNone(first_pts(b"<html>"))
        self.assertIsNone(first_pts(bytes([0x47, 0x40, 0x00, 0x10]) + bytes(184)))
        rewritten = trim_playlist(playlist(40), "https://cdn/x.m3u8", keep=30,
                                  segment_uri=lambda sequence, url, duration: f"seg/video/{sequence}")
        self.assertIn("seg/video/139", rewritten.splitlines())
        self.assertNotIn("https://cdn/seg139.ts", rewritten)

    def test_proxy_relays_prefetched_segments_and_reports_the_start_clock(self):
        Upstream.requests.clear()
        proxy = HlsProxy()
        clocks = []
        proxy.clock_ready.connect(clocks.append)
        master_url = proxy.serve(f"{self.origin}/clock_video.m3u8", f"{self.origin}/clock_audio.m3u8")
        base = master_url.rsplit("/", 1)[0]
        for kind in ("video", "audio"):
            status, text = self.fetch(f"{base}/{kind}.m3u8")
            self.assertEqual(status, 200)
            self.assertIn(f"seg/{kind}/100.{'ts' if kind == 'video' else 'aac'}", text.splitlines())
        # The newest listed segments were downloaded before anyone asked for them...
        self.wait(lambda: ("/v100.ts", "") in [(path, "") for path, _ in Upstream.requests])
        status, body = self.fetch(f"{base}/seg/video/100.ts")
        self.assertEqual(status, 200)
        self.assertEqual(body, SEGMENTS["/v100.ts"].decode("latin-1"))
        # ...and served from memory, again and again.
        self.fetch(f"{base}/seg/video/100.ts")
        self.assertEqual([path for path, _ in Upstream.requests].count("/v100.ts"), 1)
        self.assertEqual(self.fetch(f"{base}/seg/video/99")[0], 404)
        self.assertEqual(self.fetch(f"{base}/seg/other/100")[0], 404)
        self.fetch(f"{base}/seg/audio/100")
        self.wait(lambda: clocks)
        self.assertEqual(len(clocks), 1)
        self.assertAlmostEqual(clocks[0], 1749.98, places=3)        # the earlier of the two streams
        # A reset waits for the next first segments instead of reusing the old clock.
        proxy.reset_clock()
        self.assertIsNone(proxy.clock)
        # Segments requested before the reopened playlists are served do not set the clock.
        self.fetch(f"{base}/seg/video/100")
        self.fetch(f"{base}/seg/audio/100")
        QTest.qWait(300)
        self.assertEqual(len(clocks), 1)
        for kind in ("video", "audio"):
            self.fetch(f"{base}/{kind}.m3u8")
        self.fetch(f"{base}/seg/video/100")
        self.fetch(f"{base}/seg/audio/100")
        self.wait(lambda: len(clocks) == 2)
        self.assertEqual(len(clocks), 2)
        proxy.deleteLater()

    def wait(self, condition):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(20)

    def test_resolver_routes_live_separate_tracks_through_proxy(self):
        tracks = [
            {"url": "https://example.com/video.m3u8", "manifest_url": "https://example.com/master.m3u8",
             "protocol": "m3u8_native", "acodec": "none", "vcodec": "avc1", "width": 1920, "height": 1080, "tbr": 5500},
            {"url": "https://example.com/audio.m3u8", "manifest_url": "https://example.com/master.m3u8",
             "protocol": "m3u8_native", "vcodec": "none", "acodec": "mp4a"},
        ]
        self.assertEqual(select_live_tracks({"requested_formats": tracks}), (tracks[0], tracks[1]))
        self.assertIsNone(select_live_tracks({"requested_formats": tracks[:1]}))
        self.assertIsNone(select_live_tracks({"url": "https://example.com/live.m3u8"}))
        self.assertIsNone(select_live_tracks({"requested_formats": [tracks[0], {**tracks[1], "protocol": "https"}]}))

        class ScriptProcess(QProcess):
            def setArguments(self, arguments):
                script = f"print({json.dumps({'requested_formats': tracks, 'is_live': True})!r})"
                super().setArguments(["-c", script])

        resolver = StreamResolver()
        results = []
        resolver.resolved.connect(results.append)
        with patch("stream_resolver.QProcess", ScriptProcess):
            resolver.resolve(QUrl("https://youtu.be/test"))
            deadline = time.monotonic() + 5
            while not results and time.monotonic() < deadline:
                QTest.qWait(20)
        self.assertTrue(resolver.is_live)
        self.assertRegex(results[0], r"^http://127\.0\.0\.1:\d+/[0-9a-f]{16}/master\.m3u8$")
        self.assertEqual(resolver.proxy.sources["video.m3u8"], "https://example.com/video.m3u8")

    def test_resolver_prefers_the_streamlink_live_playlist(self):
        live = {"type": "hls", "url": "https://manifest.example.com/hls_playlist/itag/96/index.m3u8",
                "metadata": {"id": "3v7IWa3OWfU", "author": "YTN"}}
        self.assertEqual(parse_streamlink_output(json.dumps(live)), (live["url"], "3v7IWa3OWfU"))
        self.assertIsNone(parse_streamlink_output(json.dumps({**live, "type": "http"})))   # a VOD
        self.assertIsNone(parse_streamlink_output('{"error": "No playable streams found"}'))
        self.assertIsNone(parse_streamlink_output("not json"))
        calls = []

        class ScriptProcess(QProcess):
            def setArguments(self, arguments):
                calls.append(arguments[1])
                super().setArguments(["-c", f"print({json.dumps(live)!r})"])

        resolver = StreamResolver()
        results = []
        resolver.resolved.connect(results.append)
        with patch("stream_resolver.QProcess", ScriptProcess):
            resolver.resolve(QUrl("https://www.youtube.com/@ytnnews24/live"))
            deadline = time.monotonic() + 5
            while not results and time.monotonic() < deadline:
                QTest.qWait(20)
        self.assertEqual(calls, ["streamlink_cli"])                   # yt-dlp was not needed
        self.assertTrue(resolver.is_live)
        self.assertEqual(resolver.video_url, "https://www.youtube.com/watch?v=3v7IWa3OWfU")
        self.assertEqual(resolver.proxy.sources, {"video.m3u8": live["url"]})
        self.assertRegex(results[0], r"^http://127\.0\.0\.1:\d+/[0-9a-f]{16}/master\.m3u8$")
        resolver.deleteLater()
        resolver.deleteLater()


if __name__ == "__main__":
    unittest.main()
