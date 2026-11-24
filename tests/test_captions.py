"""Live caption track, inlet protocol and the player's sync controls."""
import os
import socket
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LIVECAPTION_SETTINGS", os.path.join(
    __import__("tempfile").mkdtemp(prefix="livecaption-test-"), "settings.ini"))

from PySide6.QtCore import QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from caption_server import CaptionServer, parse_caption_message
from remote_captions import http_base, parse_server_event, speaker_name, websocket_url
from main import LIVE_TARGET_MS, PlayerWindow
from subtitles import CaptionTrack, Cue


class CaptionTrackTest(unittest.TestCase):
    def test_offset_shifts_display_both_ways(self):
        track = CaptionTrack()
        track.set_cues([Cue(1000, 2000, "첫 줄")])
        self.assertEqual(track.text_at(1500), "첫 줄")
        track.offset = 3000
        self.assertEqual(track.text_at(1500), "")
        self.assertEqual(track.text_at(4000), "첫 줄")
        track.offset = -500
        self.assertEqual(track.text_at(500), "첫 줄")
        self.assertEqual(track.active_index(1500), None)

    def test_live_push_replace_order_and_trim(self):
        track = CaptionTrack()
        self.assertEqual(track.push("안녕하세요", 5000), (0, False, 0))
        self.assertEqual(track.cues[0].start, 5000)
        self.assertGreaterEqual(track.cues[0].end, 7000)
        # The newest caption wins while the previous one is still active.
        track.push("다음 문장", 5000)
        self.assertEqual(track.text_at(5000), "다음 문장")
        # Interim results with the same id rewrite one cue instead of stacking.
        track.push("실시", 6000, cue_id="a")
        index, replaced, _ = track.push("실시간 자막", 6400, cue_id="a")
        self.assertTrue(replaced)
        self.assertEqual((track.cues[index].start, track.cues[index].text), (6000, "실시간 자막"))
        self.assertEqual(len(track.cues), 3)
        # Explicit media times are kept and sorted into place.
        index, _, _ = track.push("이전", 9000, start=1000, end=1500)
        self.assertEqual(index, 0)
        track.MAX_CUES = 4
        self.assertEqual(track.push("넘침", 9000)[2], 1)
        self.assertEqual(len(track.cues), 4)

    def test_message_parsing(self):
        self.assertEqual(parse_caption_message("  그냥 텍스트 \n"), {"text": "그냥 텍스트"})
        self.assertIsNone(parse_caption_message("   "))
        self.assertIsNone(parse_caption_message('{"text": " "}'))
        self.assertEqual(parse_caption_message('{"text": "a", "id": 7, "duration": 1500.9}'),
                         {"text": "a", "id": "7", "duration": 1500})
        self.assertEqual(parse_caption_message('{"clear": true}'), {"clear": True})
        for line in ("{bad", '{"text": 3}', '{"text": "a", "start": -1}',
                     '{"text": "a", "end": "soon"}', '{"text": "a", "duration": true}'):
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_caption_message(line)


class RemoteProtocolTest(unittest.TestCase):
    def test_server_address_forms(self):
        self.assertEqual(websocket_url("192.168.0.10:8000").toString(), "ws://192.168.0.10:8000/ws")
        self.assertEqual(websocket_url("http://h:1").toString(), "ws://h:1/ws")
        self.assertEqual(websocket_url("https://h/ws?token=t").toString(), "wss://h/ws?token=t")
        self.assertEqual(http_base("h:18000"), "http://h:18000")

    def test_event_validation(self):
        self.assertIsNone(parse_server_event("nope"))
        self.assertIsNone(parse_server_event('{"type": "caption", "text": "a"}'))
        self.assertIsNone(parse_server_event('{"type": "caption", "id": "c1", "text": " "}'))
        self.assertEqual(parse_server_event('{"type": "caption", "id": "c1", "remove": true}'),
                         {"type": "caption", "id": "c1", "remove": True})
        event = parse_server_event('{"type": "caption", "id": "c1", "text": "가", "final": false,'
                                   ' "speaker": "SPEAKER_01", "lag": true}')
        self.assertEqual((event["speaker"], event["final"], event["lag"]), ("화자 2", False, None))
        self.assertEqual(speaker_name("김대리"), "김대리")
        # Stream times survive validation; a bad end is dropped, a bad start drops both.
        event = parse_server_event('{"type": "caption", "id": "c1", "text": "가",'
                                   ' "stream_start": 1640, "stream_end": 6920}')
        self.assertEqual((event["stream_start"], event["stream_end"]), (1640, 6920))
        event = parse_server_event('{"type": "caption", "id": "c1", "text": "가",'
                                   ' "stream_start": 1640, "stream_end": 100}')
        self.assertNotIn("stream_end", event)
        event = parse_server_event('{"type": "caption", "id": "c1", "text": "가", "stream_start": "x"}')
        self.assertNotIn("stream_start", event)
        self.assertEqual(parse_server_event('{"type": "timeline", "pts0": 487.804807}'),
                         {"type": "timeline", "pts0": 487.804807})
        self.assertIsNone(parse_server_event('{"type": "timeline", "epoch": 1}'))


class CaptionWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = PlayerWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertTrue(condition())

    def test_inlet_receives_split_lines_and_ignores_bad_ones(self):
        server = CaptionServer()
        received = []
        server.caption.connect(received.append)
        self.assertTrue(server.start(0))
        with socket.create_connection(("127.0.0.1", server.port())) as client:
            client.sendall("첫 번째\n{bad json}\n{\"text\": \"두 ".encode())
            self.wait_for(lambda: len(received) == 1)
            client.sendall("번째\", \"id\": \"x\"}\n마지막 줄".encode())
            self.wait_for(lambda: len(received) == 2)
        # A final line without a newline is delivered when the client disconnects.
        self.wait_for(lambda: len(received) == 3)
        self.assertEqual(received, [{"text": "첫 번째"}, {"text": "두 번째", "id": "x"},
                                    {"text": "마지막 줄"}])
        server.stop()
        self.assertFalse(server.is_listening())

    def test_live_captions_reach_the_overlay(self):
        panel = self.window.caption_panel
        self.assertTrue(self.window.set_live_captions(True, 0))
        self.assertTrue(panel.live_button.isChecked())
        self.assertIn("로컬", self.window.subtitle_name.text())
        with socket.create_connection(("127.0.0.1", self.window.caption_server.port())) as client:
            client.sendall('{"text": "라이브 자막", "id": "1"}\n'.encode())
            self.wait_for(lambda: self.window.subtitle_label.text() == "라이브 자막")
            client.sendall('{"text": "라이브 자막 수정", "id": "1"}\n'.encode())
            self.wait_for(lambda: self.window.subtitle_label.text() == "라이브 자막 수정")
            self.assertEqual(panel.cue_list.count(), 1)
            client.sendall('{"clear": true}\n'.encode())
            self.wait_for(lambda: self.window.subtitle_label.text() == "")
        self.assertEqual(self.window.subtitles, [])
        self.assertFalse(self.window.set_live_captions(False, 0))
        self.assertFalse(self.window.caption_server.is_listening())
        self.assertEqual(self.window.subtitle_name.text(), "자막 없음")

    def test_typed_caption_offset_and_sync_to_selected_cue(self):
        panel = self.window.caption_panel
        panel.text_input.setText("직접 입력한 자막")
        panel.submit_text()
        self.assertEqual(self.window.subtitle_label.text(), "직접 입력한 자막")
        self.assertEqual(panel.cue_list.count(), 1)
        self.assertEqual(panel.active_row, 0)
        # A positive offset shows the caption later than it arrived.
        panel.set_offset(1500)
        self.assertEqual(self.window.track.offset, 1500)
        self.assertEqual(panel.offset_label.text(), "+1.5초")
        self.assertEqual(self.window.subtitle_label.text(), "")
        self.window.update_position(1600)
        self.assertEqual(self.window.subtitle_label.text(), "직접 입력한 자막")
        self.window.nudge_offset(-100)
        self.assertEqual(self.window.track.offset, 1400)
        # Aligning the selected cue with the current position resets the offset here.
        panel.cue_list.setCurrentRow(0)
        panel.request_sync()
        self.assertEqual(self.window.track.offset, 0)
        panel.visible_box.setChecked(False)
        self.assertFalse(self.window.video_view.subtitle_item.isVisible())
        self.assertFalse(self.window.caption_button.isChecked())
        self.window.caption_button.click()
        self.assertTrue(self.window.video_view.subtitle_item.isVisible())
        panel.font_size.setValue(40)
        self.assertGreaterEqual(self.window.subtitle_label.font().pixelSize(), 40)

    def test_ai_captions_interim_final_remove_and_speaker_color(self):
        panel = self.window.caption_panel
        window = self.window
        window.receive_caption({"type": "caption", "id": "c1", "text": "안녕", "final": False,
                                "speaker": "화자 2", "lag": 900})
        self.assertEqual(window.subtitle_label.text(), "안녕")
        self.assertLess(window.subtitle_label.fill.alphaF(), 1.0)       # interim is dimmed
        self.assertTrue(panel.cue_list.item(0).font().italic())
        self.assertIsNone(panel.measured_lag_ms)                        # only finals report lag
        window.receive_caption({"type": "caption", "id": "c1", "text": "안녕하세요", "final": True,
                                "speaker": "화자 2", "lag": 2300})
        self.assertEqual(panel.cue_list.count(), 1)
        self.assertEqual(window.subtitle_label.fill.name(), "#ffe066")   # 화자 2 colour
        self.assertEqual(window.subtitle_label.fill.alphaF(), 1.0)
        self.assertIn("화자 2 · 안녕하세요", panel.cue_list.item(0).text())
        self.assertEqual(panel.measured_lag_ms, 2300)
        panel.apply_measured_lag()
        self.assertEqual(panel.delay.value(), 2.5)
        window.receive_caption({"type": "caption", "id": "c2", "text": "음", "final": False})
        self.assertEqual(panel.cue_list.count(), 2)
        window.receive_caption({"type": "caption", "id": "c2", "remove": True})
        self.assertEqual(panel.cue_list.count(), 1)
        self.assertEqual([cue.id for cue in window.track.cues], ["c1"])

    def test_ai_captions_use_the_source_clock_when_both_sides_report_it(self):
        window = self.window
        # Without both clocks a caption is stamped at the arrival position.
        window.receive_caption({"type": "caption", "id": "c1", "text": "도착 시점", "final": True,
                                "stream_start": 1000, "stream_end": 2000})
        self.assertEqual(window.track.cues[0].start, window.player.position())
        window.remote.timeline.emit(487.8)
        self.assertIsNone(window.media_position(1000))
        window.stream_resolver.proxy.clock_ready.emit(480.0)
        # Server audio starts 7.8 s after playback position 0.
        self.assertEqual(window.media_position(1000), 8800)
        window.receive_caption({"type": "caption", "id": "c2", "text": "인식 중", "final": False,
                                "stream_start": 1640, "stream_end": 3000})
        window.receive_caption({"type": "caption", "id": "c2", "text": "인식 완료", "final": True,
                                "stream_start": 1600, "stream_end": 6920})
        cue = window.track.cues[window.track.find("c2")]
        self.assertEqual((cue.start, cue.end, cue.text), (9400, 14720, "인식 완료"))
        self.assertEqual(window.track.text_at(10000), "인식 완료")
        # Reopening the stream forgets the playback clock until the proxy reports it again.
        window.open_source(QUrl("http://127.0.0.1:9/x.m3u8"))
        self.assertIsNone(window.playback_pts0)
        self.assertIsNone(window.media_position(1000))
        window.stop_playback()

    def test_unfinished_sentence_is_carried_on_screen_but_not_in_the_list(self):
        window = self.window
        panel = window.caption_panel
        position = window.player.position()
        window.receive_caption({"type": "caption", "id": "c1", "text": "수출과 투자", "final": True,
                                "start": position, "end": position + 1000})
        window.receive_caption({"type": "caption", "id": "c2", "text": "중심으로 회복됐습니다.",
                                "final": True, "start": position + 1000, "end": position + 3000})
        self.assertEqual(window.track.text_at(position + 1500), "수출과 투자 중심으로 회복됐습니다.")
        self.assertNotIn("수출과", panel.cue_list.item(1).text())
        window.refresh_subtitle(position + 1500)
        self.assertEqual(window.video_view.subtitle_label.text(), "수출과 투자 중심으로 회복됐습니다.")
        panel.join_box.setChecked(False)
        self.assertFalse(window.track.join_sentences)
        window.receive_caption({"type": "caption", "id": "c3", "text": "경제정책", "final": True,
                                "start": position + 3000, "end": position + 4000})
        window.receive_caption({"type": "caption", "id": "c4", "text": "방향을", "final": True,
                                "start": position + 4000, "end": position + 5000})
        self.assertEqual(window.track.text_at(position + 4500), "방향을")
        panel.join_box.setChecked(True)

    def test_ai_start_requires_a_stream_url(self):
        window = self.window
        self.assertFalse(window.start_ai_captions())
        self.assertFalse(window.remote.active)
        self.assertIn("URL", window.caption_panel.ai_status.text())
        window.caption_panel.server.setText("127.0.0.1:1")              # nothing listens here
        self.assertTrue(window.start_ai_captions("https://www.youtube.com/live/x"))
        self.assertTrue(window.caption_panel.ai_button.isChecked())
        self.assertFalse(window.caption_panel.server.isEnabled())
        self.wait_for(lambda: not window.remote.active)
        self.assertFalse(window.caption_panel.ai_button.isChecked())
        self.assertIn("연결", window.caption_panel.ai_status.text())

    def test_typed_keywords_go_into_the_start_request(self):
        window = self.window
        panel = window.caption_panel
        panel.server.setText("127.0.0.1:1")
        panel.keywords.setText(" APEC, 머신러닝 ")
        self.assertTrue(window.start_ai_captions("https://www.youtube.com/live/x"))
        self.assertEqual(window.remote.request["keywords"], "APEC, 머신러닝")
        self.assertFalse(panel.keywords.isEnabled())
        self.wait_for(lambda: not window.remote.active)
        self.assertTrue(panel.keywords.isEnabled())
        self.assertEqual(window.settings.value("ai/keywords"), "APEC, 머신러닝")
        panel.keywords.clear()
        self.assertTrue(window.start_ai_captions("https://www.youtube.com/live/x"))
        self.assertNotIn("keywords", window.remote.request)
        self.wait_for(lambda: not window.remote.active)
        # A hand-edited INI line "keywords=APEC, 머신러닝" reads back as a list.
        window.settings.setValue("ai/keywords", ["APEC", "머신러닝"])
        window.load_settings()
        self.assertEqual(panel.keywords.text(), "APEC, 머신러닝")

    def test_ai_captions_wait_for_the_resolved_video(self):
        window = self.window
        started = []
        window.remote.start = lambda server, url, *rest: started.append(url)
        window.stream_resolver.resolve = lambda url: None
        window.url_input.setText("https://www.youtube.com/@ytnnews24/live")
        window.open_url()
        self.assertEqual(started, [])                 # not before we know which video plays
        self.assertTrue(window.ai_after_resolve)
        window.stream_resolver.video_url = "https://www.youtube.com/watch?v=PPZzpgEpgYk"
        window.on_stream_resolved("http://127.0.0.1:1/master.m3u8")
        self.assertEqual(started, ["https://www.youtube.com/watch?v=PPZzpgEpgYk"])
        self.assertEqual(window.page_url, "https://www.youtube.com/watch?v=PPZzpgEpgYk")
        window.stop_all()

    def test_live_pause_accumulates_video_delay(self):
        from PySide6.QtMultimedia import QMediaPlayer
        self.window.live_stream = True
        self.window.update_state(QMediaPlayer.PlaybackState.PausedState)
        QTest.qWait(150)
        self.window.update_state(QMediaPlayer.PlaybackState.PlayingState)
        self.assertGreaterEqual(self.window.video_delay_ms, 100)
        self.assertIn("LIVE −", self.window.live_badge.text())
        self.assertIn("늦게", self.window.caption_panel.delay_status.text())
        self.window.delay_video(2000)  # not playing: must refuse without pausing
        self.assertFalse(self.window.delay_timer.isActive())

    def test_behind_live_is_measured_from_the_source_clock(self):
        window = self.window
        window.live_stream = True
        proxy = window.stream_resolver.proxy
        proxy.windows = {"video.m3u8": {seq: (seq, ["#EXTINF:1.0,"], f"https://x/{seq}.ts", {})
                                        for seq in range(100, 110)}}
        proxy.fetched_at = {"video.m3u8": time.monotonic()}
        proxy.anchors = {"video": (100, 1000.0)}
        window.playback_pts0 = 1000.0
        # Edge = 1000 + 10 one-second segments; position 0 -> about 10 s behind.
        self.assertAlmostEqual(proxy.live_edge_pts(), 1010.0, delta=0.2)
        window.update_delay_status()
        self.assertAlmostEqual(window.behind_live_ms, 10000, delta=200)
        self.assertIn("LIVE −10.", window.live_badge.text())
        self.assertIn("라이브보다 10.", window.caption_panel.delay_status.text())

    def test_auto_delay_holds_the_picture_for_late_captions(self):
        window = self.window
        held = []
        window.delay_video = held.append
        window.live_stream = True
        window.caption_pts0, window.playback_pts0 = 500.0, 500.0
        window.update_sync_mode()
        self.assertTrue(window.caption_panel.sync_automatic)
        self.assertIn("자동 싱크", window.caption_panel.sync_mode.text())
        # Leads -2.0 s, -0.5 s and a one-off -7 s: hold the second-largest lateness + margin.
        for lead in (-2000, -500, -7000):
            window.adjust_auto_delay(lead)
        self.assertEqual(held, [2300])
        self.assertEqual(window.auto_delay_ms, 2300)
        # On-time captions add nothing, and the total hold is capped.
        for lead in (500, 200, 0):
            window.adjust_auto_delay(lead)
        self.assertEqual(held, [2300])
        for lead in (-9000, -9000, -9000):
            window.adjust_auto_delay(lead)
        self.assertEqual(held, [2300, 6000 - 2300])
        self.assertEqual(window.auto_hold_ms, 6000)
        # Captions that keep showing up well early shrink the wanted delay one step (no pause);
        # the recovery tests below cover winning that step back.
        # (The last -9 s sample is still among the recent six until one more caption arrives.)
        for lead in (4000, 4200, 5000, 4500, 4100):
            window.adjust_auto_delay(lead)
        self.assertEqual(window.auto_delay_ms, 6000)
        window.adjust_auto_delay(4300)
        self.assertEqual(window.auto_delay_ms, 6000 - 200)
        self.assertEqual(len(held), 2)
        window.finish_recovery("test")
        # Catch-up: past the live target (plus deliberate delays) the player runs slightly fast.
        from PySide6.QtMultimedia import QMediaPlayer
        rates = []
        window.player.setPlaybackRate = rates.append
        window.player.playbackRate = lambda: rates[-1] if rates else 1.0
        window.player.playbackState = lambda: QMediaPlayer.PlaybackState.PlayingState
        window.video_delay_ms = 2000                    # a viewer pause, kept by catch-up
        window.auto_hold_ms = window.auto_delay_ms = 0
        window.behind_live_ms = LIVE_TARGET_MS + 2000 + 900
        window.adjust_catch_up()
        self.assertEqual(rates, [1.05])
        window.behind_live_ms = LIVE_TARGET_MS + 2000 + 100      # still above target: keep going
        window.adjust_catch_up()
        self.assertEqual(rates, [1.05])
        window.behind_live_ms = LIVE_TARGET_MS + 2000 - 100      # back at target
        window.adjust_catch_up()
        self.assertEqual(rates, [1.05, 1.0])
        window.behind_live_ms = LIVE_TARGET_MS + 2000 + 500      # inside the dead band: stay at 1.0
        window.adjust_catch_up()
        self.assertEqual(rates, [1.05, 1.0])
        # Unticking the option stops automatic holds.
        window.caption_panel.auto_delay.setChecked(False)
        for lead in (-3000, -3000, -3000):
            window.adjust_auto_delay(lead)
        self.assertEqual(len(held), 2)

    # ---- winning eased-off automatic delay back -------------------------------------------

    def recovering_window(self, auto_delay=827, refuse=False):
        """A live window whose 827 ms of automatic hold is all still wanted, with a scripted player."""
        from PySide6.QtMultimedia import QMediaPlayer
        window = self.window
        window.delay_video = lambda ms: self.fail("no hold expected")
        # Settings persist between tests (another test unticks this and closes its window).
        window.caption_panel.auto_delay.setChecked(True)
        window.live_stream = True
        window.video_delay_ms = window.auto_hold_ms = 827
        window.auto_delay_ms = auto_delay
        clock = {"t": 100.0, "pos": 60000, "rate": 1.0, "state": QMediaPlayer.PlaybackState.PlayingState}
        rates = []

        def set_rate(rate):
            rates.append(rate)
            if not refuse:
                clock["rate"] = rate

        window.player.setPlaybackRate = set_rate
        window.player.playbackRate = lambda: clock["rate"]
        window.player.position = lambda: clock["pos"]
        window.player.playbackState = lambda: clock["state"]
        window.monotonic = lambda: clock["t"]
        window.behind_live_ms = None
        return window, clock, rates

    @staticmethod
    def advance(clock, seconds, moving=True):
        """Let ``seconds`` of wall time pass; the position moves only while really playing."""
        from PySide6.QtMultimedia import QMediaPlayer
        clock["t"] += seconds
        if moving and clock["state"] == QMediaPlayer.PlaybackState.PlayingState:
            clock["pos"] += round(seconds * 1000 * clock["rate"])

    def feed(self, window, leads, start=1):
        for number, lead in enumerate(leads, start):
            window.adjust_auto_delay(lead, f"c{number}")

    def test_recovery_needs_five_early_of_six_distinct_sentences(self):
        window, _clock, rates = self.recovering_window()
        self.feed(window, [600, 700, 900, 650, 800])
        # Interim updates and finals of sentences already counted change nothing.
        for number in range(1, 6):
            window.adjust_auto_delay(5000, f"c{number}")
        self.assertEqual(window.first_shown, [5, 0])
        self.assertEqual(window.auto_delay_ms, 827)
        self.assertIsNone(window.recovery)
        # The sixth distinct sentence is on time (not early, not late): 5 of 6 early is enough.
        self.feed(window, [100], start=6)
        self.assertEqual(window.auto_delay_ms, 627)
        self.assertEqual(window.recovery["requested"], 200)
        self.assertEqual(rates, [1.05])                      # fast playback starts right away
        self.assertEqual(window.late_samples, [])            # those six samples are used up
        # Six fresh sentences are needed for the next step, and a step never stacks on a running one.
        self.feed(window, [900] * 6, start=7)
        self.assertEqual(window.auto_delay_ms, 627)

    def test_one_sentence_500_ms_late_blocks_recovery(self):
        window, _clock, rates = self.recovering_window()
        self.feed(window, [900, 900, 900, 900, 900, -600])
        self.assertEqual(window.auto_delay_ms, 827)
        self.assertIsNone(window.recovery)
        self.assertEqual(window.first_shown, [6, 1])
        # 400 ms late is under the 500 ms bar (and one late sentence is no reason to hold).
        self.feed(window, [900, 900, 900, 900, 900, -400], start=7)
        self.assertEqual(window.auto_delay_ms, 627)
        self.assertEqual(rates, [1.05])

    def test_recovery_never_takes_more_than_the_automatic_delay(self):
        window, clock, rates = self.recovering_window(auto_delay=120)
        window.video_delay_ms = window.auto_hold_ms = 120
        self.feed(window, [900] * 6)
        self.assertEqual(window.auto_delay_ms, 0)
        self.assertEqual(window.recovery["requested"], 120)
        for _ in range(3):                                   # 3 s at 1.05x wins 150 ms > 120 ms
            self.advance(clock, 1.0)
            window.adjust_catch_up()
        self.assertIsNone(window.recovery)
        self.assertEqual(rates, [1.05, 1.0])
        # Nothing automatic left: early sentences ask for nothing more.
        self.feed(window, [900] * 6, start=7)
        self.assertEqual(window.auto_delay_ms, 0)
        self.assertIsNone(window.recovery)

    def test_recovery_plays_fast_below_the_catch_up_threshold_until_won_back(self):
        window, clock, rates = self.recovering_window()
        # 300 ms past the target is far below the 800 ms re-buffering threshold...
        window.behind_live_ms = window.catch_up_target() + 300 - 200
        window.adjust_catch_up()
        self.assertEqual(rates, [])
        self.feed(window, [900] * 6)
        self.assertEqual(rates, [1.05])                      # ...but a recovery plays fast anyway
        for second in range(1, 5):
            self.advance(clock, 1.0)
            window.adjust_catch_up()
            if second < 4:
                self.assertAlmostEqual(window.recovery["recovered"], 50 * second, delta=1)
        # 4 s at 1.05x is 200 ms: back to normal speed, and the eased step is kept.
        self.assertIsNone(window.recovery)
        self.assertEqual(rates, [1.05, 1.0])
        self.assertEqual(window.auto_delay_ms, 627)
        self.assertEqual(window.video_delay_ms, 827)         # the pauses themselves stay on record

    def test_pauses_and_stalls_are_not_credited(self):
        from PySide6.QtMultimedia import QMediaPlayer
        window, clock, rates = self.recovering_window()
        self.feed(window, [900] * 6)
        self.advance(clock, 1.0)
        window.adjust_catch_up()                             # 50 ms won
        clock["state"] = QMediaPlayer.PlaybackState.PausedState
        self.advance(clock, 5.0)
        window.adjust_catch_up()
        self.assertEqual(rates, [1.05, 1.0])                 # no fast playback while paused
        clock["state"] = QMediaPlayer.PlaybackState.PlayingState
        window.adjust_catch_up()
        self.assertEqual(rates, [1.05, 1.0, 1.05])
        self.advance(clock, 2.0, moving=False)               # playing but stalled on buffering
        window.adjust_catch_up()
        self.assertAlmostEqual(window.recovery["recovered"], 50, delta=1)
        for _ in range(3):
            self.advance(clock, 1.0)
            window.adjust_catch_up()
        self.assertIsNone(window.recovery)                   # 50 + 150 = 200 ms, only real playback
        self.assertEqual(rates[-1], 1.0)

    def test_refused_rate_change_is_not_recorded_as_recovered(self):
        window, clock, rates = self.recovering_window(refuse=True)
        self.feed(window, [900] * 6)
        self.assertEqual(rates, [1.05])
        self.assertTrue(window.catch_up_refused)
        self.assertIsNone(window.recovery)
        self.assertEqual(window.auto_delay_ms, 827)          # the step comes back: nothing was won
        self.advance(clock, 5.0)
        window.adjust_catch_up()
        self.assertEqual(rates, [1.05])                      # and the player is not asked again

    def test_recovery_end_does_not_stop_needed_rebuffering_catch_up(self):
        window, clock, rates = self.recovering_window()
        self.feed(window, [900] * 6)
        target = window.catch_up_target()
        window.behind_live_ms = target + 900                 # re-buffering drift on top
        window.adjust_catch_up()
        self.assertTrue(window.catch_up_normal)
        self.assertEqual(rates, [1.05])                      # one rate, no flapping
        for _ in range(4):
            self.advance(clock, 1.0)
            window.adjust_catch_up()
        self.assertIsNone(window.recovery)
        self.assertEqual(rates, [1.05])                      # still behind: keeps catching up
        window.behind_live_ms = target - 50
        window.adjust_catch_up()
        self.assertEqual(rates, [1.05, 1.0])

    def test_reaching_the_target_early_ends_recovery_and_reopening_resets_it(self):
        window, clock, rates = self.recovering_window()
        self.feed(window, [900] * 6)
        window.behind_live_ms = window.catch_up_target() - 10
        window.adjust_catch_up()
        self.assertIsNone(window.recovery)
        self.assertEqual(window.auto_delay_ms, 627)          # already there: the step is kept
        self.assertEqual(rates, [1.05, 1.0])
        # A new source (or going back to live) forgets the old broadcast's sentences and recovery.
        self.feed(window, [900] * 5, start=7)
        window.start_recovery(200)
        window.open_source(QUrl("http://127.0.0.1:9/x.m3u8"))
        self.assertIsNone(window.recovery)
        self.assertEqual((window.auto_delay_ms, window.late_samples, window.first_shown), (0, [], [0, 0]))
        self.assertEqual(window.counted_ids, set())
        window.stop_playback()


if __name__ == "__main__":
    unittest.main()
