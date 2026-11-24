"""LiveCaption player: live/local video with a synced caption overlay."""
import os
import sys
import time
import logging
import argparse
from collections import deque
from importlib.metadata import version
from pathlib import Path

from subtitles import CaptionTrack, load_srt
from video_view import VideoView
from caption_panel import CaptionPanel, format_offset
from caption_server import CaptionServer
from remote_captions import DEFAULT_SERVER, RemoteCaptionClient
from stream_resolver import StreamResolver, is_youtube_url
from ui_theme import STYLE, dark_palette, make_icon, speaker_color
from app_logging import LOG_PATH, WindowLogHandler, configure_logging, install_diagnostics

from PySide6.QtCore import Qt, QSettings, QSize, QUrl, QTimer, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QMediaMetaData, QPlaybackOptions
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPushButton, QSlider, QStyle, QToolButton, QVBoxLayout, QWidget,
    QDockWidget, QPlainTextEdit)


logger = logging.getLogger("livecaption.player")
AUTO_DELAY_MARGIN_MS = 300     # extra hold so a caption is up just before its first word
# Easing off the automatic delay (experimental values): judge the first captions of the last
# AUTO_EASE_WINDOW distinct sentences; when at least AUTO_EASE_MIN_EARLY of them came
# AUTO_EASE_EARLY_MS or more early and none came AUTO_EASE_LATE_MS or more late, lower the
# wanted delay by AUTO_EASE_STEP_MS (never below zero) and play fast until that much is won back.
AUTO_EASE_WINDOW = 6
AUTO_EASE_MIN_EARLY = 5
AUTO_EASE_EARLY_MS = 500
AUTO_EASE_LATE_MS = 500
AUTO_EASE_STEP_MS = 200
LATE_CAPTION_MS = 500          # a first caption this late counts as late in the stats log
AUTO_DELAY_MAX_MS = 6000       # never hold the picture longer than this in total
AUTO_DELAY_SAMPLES = 3         # finals to look at before deciding on a hold
LIVE_TARGET_MS = 2500          # behind-live the player settles at, before deliberate delays
CATCH_UP_START_MS = 800        # speed up once this far past the target...
CATCH_UP_RATE = 1.05           # ...at this rate (barely audible), until back at the target


def format_time(milliseconds):
    seconds = max(0, milliseconds) // 1000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


class SeekSlider(QSlider):
    """Slider that jumps to the clicked point and reports the seek on release."""
    seek_requested = Signal(int)

    def __init__(self):
        super().__init__(Qt.Orientation.Horizontal)
        self.pressed = False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.pressed = True
            self.move_to(event)
        event.accept()

    def mouseMoveEvent(self, event):
        if self.pressed:
            self.move_to(event)
        event.accept()

    def mouseReleaseEvent(self, event):
        if self.pressed and event.button() == Qt.MouseButton.LeftButton:
            self.pressed = False
            self.seek_requested.emit(self.value())
        event.accept()

    def move_to(self, event):
        self.setValue(QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), round(event.position().x()), self.width()))


class PlayerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LiveCaption")
        self.resize(1180, 700)
        self.setPalette(dark_palette())
        self.setStyleSheet(STYLE)
        self.setup_log_panel()
        logger.info("Player window created; PySide6=%s yt-dlp=%s", version("PySide6"), version("yt-dlp"))
        self.track = CaptionTrack()
        self.subtitle_file = None
        self.source_url = None
        self.page_url = None
        self.live_stream = False
        self.prefer_best_video = False
        self.last_video_size = None
        self.video_delay_ms = 0
        self.paused_at = None
        self.fullscreen_restore = []
        self.stream_resolver = StreamResolver(self)
        self.stream_resolver.resolved.connect(self.on_stream_resolved)
        self.ai_after_resolve = False
        self.stream_resolver.failed.connect(self.show_stream_error)
        self.caption_server = CaptionServer(self)
        self.caption_server.caption.connect(self.receive_caption)
        self.caption_server.clients_changed.connect(lambda _count: self.update_live_status())
        self.remote = RemoteCaptionClient(self)
        self.remote.caption.connect(self.receive_caption)
        self.remote.timeline.connect(self.set_caption_clock)
        self.stream_resolver.proxy.clock_ready.connect(self.set_playback_clock)
        # Both in source PTS seconds: the server audio's first sample and the player's position 0.
        self.caption_pts0 = None
        self.playback_pts0 = None
        self.late_samples = []         # leads (ms) of recent sentences' first captions; negative = late
        self.counted_ids = set()       # sentences whose first caption is already in late_samples
        self.counted_order = deque()
        self.auto_delay_ms = 0         # delay auto sync wants on top of the live target
        self.auto_hold_ms = 0          # pauses auto sync actually made (part of video_delay_ms)
        self.recovery = None           # an eased-off delay being won back by fast playback
        self.catch_up_normal = False   # catch-up started by the re-buffering rule
        self.first_shown = [0, 0]      # [first captions placed, of them LATE_CAPTION_MS+ late]
        self.monotonic = time.monotonic
        self.behind_live_ms = None     # measured distance to the live edge
        self.catch_up_refused = False
        self.behind_smooth_ms = None
        self.remote.status.connect(self.on_ai_status)
        self.remote.active_changed.connect(self.on_ai_active)
        self.remote.session_started.connect(lambda _session: self.caption_panel.set_export_enabled(True))
        self.network = QNetworkAccessManager(self)
        # LIVECAPTION_SETTINGS=<file.ini> keeps tests away from the user's real settings.
        ini = os.environ.get("LIVECAPTION_SETTINGS")
        self.settings = (QSettings(ini, QSettings.Format.IniFormat) if ini
                         else QSettings("LiveCaption", "Player"))
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.5)
        self.player.setAudioOutput(self.audio)
        self.video_view = VideoView()
        self.subtitle_label = self.video_view.subtitle_label
        self.player.setVideoOutput(self.video_view.video_item)
        self.video_view.video_item.videoSink().videoFrameChanged.connect(self.report_video_size)
        self.delay_timer = QTimer(self)
        self.delay_timer.setSingleShot(True)
        self.delay_timer.timeout.connect(self.player.play)
        self.idle_timer = QTimer(self)
        self.idle_timer.setSingleShot(True)
        self.idle_timer.setInterval(2500)
        self.idle_timer.timeout.connect(self.hide_controls)
        self.live_timer = QTimer(self)
        self.live_timer.setInterval(500)
        self.live_timer.timeout.connect(self.update_delay_status)
        self.live_timer.start()

        self.top_bar = self.build_top_bar()
        self.control_bar = self.build_control_bar()
        container = QWidget()
        container.setObjectName("root")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.top_bar)
        layout.addWidget(self.video_view, 1)
        layout.addWidget(self.control_bar)
        self.setCentralWidget(container)
        self.setup_caption_panel()
        self.video_info = QLabel()
        self.statusBar().addPermanentWidget(self.video_info)
        self.statusBar().showMessage("파일 또는 HTTP(S) 동영상 URL을 열어 주세요.")
        self.setup_shortcuts()
        self.load_settings()

        self.video_view.mouse_moved.connect(self.show_controls)
        self.video_view.double_clicked.connect(self.toggle_fullscreen)
        self.player.positionChanged.connect(self.update_position)
        self.player.durationChanged.connect(self.update_duration)
        self.player.seekableChanged.connect(self.seek.setEnabled)
        self.player.playbackStateChanged.connect(self.update_state)
        self.player.errorOccurred.connect(self.show_error)
        self.player.tracksChanged.connect(self.select_video_quality)
        self.player.mediaStatusChanged.connect(lambda status: logger.info("Media status: %s", status.name))
        self.player.seekableChanged.connect(lambda value: logger.info("Seekable: %s", value))
        self.player.hasVideoChanged.connect(lambda value: logger.info("Video track available: %s", value))
        self.player.hasAudioChanged.connect(lambda value: logger.info("Audio track available: %s", value))
        self.player.bufferProgressChanged.connect(lambda value: logger.debug("Buffer: %.0f%%", value * 100))

    @property
    def subtitles(self):
        return self.track.cues

    # ---- layout -----------------------------------------------------------

    def build_top_bar(self):
        bar = QFrame()
        bar.setObjectName("topBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 8, 10, 8)
        file_button = QPushButton("파일 열기")
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("YouTube 라이브 주소 또는 HTTP(S) 동영상 URL")
        self.url_input.setClearButtonEnabled(True)
        url_button = QPushButton("열기")
        url_button.setObjectName("primary")
        for widget in (file_button, self.url_input, url_button):
            layout.addWidget(widget)
        file_button.clicked.connect(self.open_file)
        url_button.clicked.connect(self.open_url)
        self.url_input.returnPressed.connect(self.open_url)
        return bar

    def tool_button(self, icon, tip, checkable=False):
        button = QToolButton()
        button.setIcon(make_icon(icon))
        button.setIconSize(QSize(20, 20))
        button.setToolTip(tip)
        button.setAccessibleName(tip)
        button.setCheckable(checkable)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def build_control_bar(self):
        bar = QFrame()
        bar.setObjectName("controlBar")
        self.seek = SeekSlider()
        self.seek.setRange(0, 0)
        self.seek.setEnabled(False)
        self.seek.setAccessibleName("재생 위치")
        self.play_icons = make_icon("play"), make_icon("pause")
        self.volume_icons = make_icon("volume"), make_icon("mute")
        self.play_button = self.tool_button("play", "재생 / 일시정지 (Space)")
        self.play_button.setEnabled(False)
        stop_button = self.tool_button("stop", "정지")
        self.time_label = QLabel("00:00:00 / 00:00:00")
        self.time_label.setObjectName("time")
        self.live_badge = QLabel("LIVE")
        self.live_badge.setObjectName("badge")
        self.live_badge.hide()
        self.subtitle_name = QLabel("자막 없음")
        self.subtitle_name.setObjectName("hint")
        self.caption_button = self.tool_button("cc", "자막 표시 (C)", checkable=True)
        self.caption_button.setChecked(True)
        self.mute_button = self.tool_button("volume", "음소거 (M)")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(50)
        self.volume.setFixedWidth(110)
        self.volume.setAccessibleName("음량")
        self.volume.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.panel_button = self.tool_button("panel", "자막·싱크 패널", checkable=True)
        fullscreen_button = self.tool_button("fullscreen", "전체 화면 (F, 더블클릭)")
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        for widget in (self.play_button, stop_button):
            buttons.addWidget(widget)
        buttons.addSpacing(6)
        buttons.addWidget(self.time_label)
        buttons.addWidget(self.live_badge)
        buttons.addStretch(1)
        buttons.addWidget(self.subtitle_name)
        buttons.addSpacing(6)
        for widget in (self.caption_button, self.mute_button, self.volume,
                       self.panel_button, fullscreen_button):
            buttons.addWidget(widget)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 6)
        layout.setSpacing(4)
        layout.addWidget(self.seek)
        layout.addLayout(buttons)
        self.play_button.clicked.connect(self.toggle_play)
        stop_button.clicked.connect(self.stop_all)
        self.seek.seek_requested.connect(self.seek_to_position)
        self.volume.valueChanged.connect(lambda value: self.audio.setVolume(value / 100))
        self.mute_button.clicked.connect(self.toggle_mute)
        fullscreen_button.clicked.connect(self.toggle_fullscreen)
        return bar

    def setup_caption_panel(self):
        self.caption_panel = panel = CaptionPanel()
        self.caption_dock = QDockWidget("자막 · 싱크", self)
        self.caption_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable
                                      | QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.caption_dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.caption_dock)
        self.view_menu.addAction(self.caption_dock.toggleViewAction())
        self.panel_button.setChecked(True)
        self.panel_button.toggled.connect(self.caption_dock.setVisible)
        self.caption_dock.toggleViewAction().toggled.connect(self.panel_button.setChecked)
        self.caption_button.toggled.connect(panel.visible_box.setChecked)
        panel.visibility_toggled.connect(self.caption_button.setChecked)
        panel.visibility_toggled.connect(self.video_view.set_subtitles_visible)
        panel.font_size_changed.connect(self.video_view.set_font_size)
        panel.open_srt_requested.connect(self.open_subtitle)
        panel.clear_requested.connect(self.clear_subtitles)
        panel.live_toggled.connect(self.set_live_captions)
        panel.text_entered.connect(lambda text: self.receive_caption({"text": text}))
        panel.offset_changed.connect(self.set_offset)
        panel.sync_cue_requested.connect(self.sync_cue_to_now)
        panel.seek_cue_requested.connect(self.seek_to_cue)
        panel.delay_video_requested.connect(self.delay_video)
        panel.go_live_requested.connect(self.go_live)
        panel.ai_toggled.connect(self.toggle_ai_captions)
        panel.export_requested.connect(self.export_captions)

    def setup_shortcuts(self):
        fullscreen = QAction("전체 화면", self)
        fullscreen.setShortcut(QKeySequence("F"))
        fullscreen.triggered.connect(self.toggle_fullscreen)
        self.view_menu.addAction(fullscreen)
        self.addAction(fullscreen)
        for keys, handler in (
                ("Space", self.toggle_play), ("M", self.toggle_mute),
                ("C", self.caption_button.click), ("Esc", self.exit_fullscreen),
                ("G", lambda: self.nudge_offset(-100)), ("H", lambda: self.nudge_offset(100))):
            QShortcut(QKeySequence(keys), self).activated.connect(handler)

    def setup_log_panel(self):
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(2000)
        self.log_text.setPlaceholderText(str(LOG_PATH))
        self.log_dock = QDockWidget("디버그 로그", self)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        buttons = QHBoxLayout()
        copy_button = QPushButton("로그 복사")
        clear_button = QPushButton("화면 로그 지우기")
        folder_button = QPushButton("로그 폴더 열기")
        for button in (copy_button, clear_button, folder_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addWidget(self.log_text)
        self.log_dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.log_dock.hide()
        self.view_menu = self.menuBar().addMenu("보기")
        self.view_menu.addAction(self.log_dock.toggleViewAction())
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.log_text.toPlainText()))
        clear_button.clicked.connect(self.log_text.clear)
        folder_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_PATH.parent))))
        self.log_handler = WindowLogHandler()
        self.log_handler.signals.message.connect(self.log_text.appendPlainText)
        logging.getLogger("livecaption").addHandler(self.log_handler)

    # ---- settings ---------------------------------------------------------

    def load_settings(self):
        panel = self.caption_panel
        panel.server.setText(str(self.settings.value("ai/server", DEFAULT_SERVER)))
        index = panel.language.findData(self.settings.value("ai/language", "ko") or None)
        panel.language.setCurrentIndex(max(0, index))
        panel.diarization.setChecked(self.settings.value("ai/diarization", False, type=bool))
        panel.auto_start.setChecked(self.settings.value("ai/auto_start", True, type=bool))
        keywords = self.settings.value("ai/keywords", "")
        # An INI value with commas comes back as a list ("APEC, 머신러닝" -> ["APEC", "머신러닝"]).
        panel.keywords.setText(", ".join(map(str, keywords)) if isinstance(keywords, list) else str(keywords))
        panel.font_size.setValue(self.settings.value("view/font_size", 22, type=int))
        panel.delay.setValue(self.settings.value("sync/delay", 2.0, type=float))
        panel.auto_delay.setChecked(self.settings.value("sync/auto_delay", True, type=bool))

    def save_settings(self):
        panel = self.caption_panel
        self.settings.setValue("ai/server", panel.server.text().strip())
        self.settings.setValue("ai/language", panel.language.currentData() or "")
        self.settings.setValue("ai/diarization", panel.diarization.isChecked())
        self.settings.setValue("ai/auto_start", panel.auto_start.isChecked())
        self.settings.setValue("ai/keywords", panel.keywords.text().strip())
        self.settings.setValue("view/font_size", panel.font_size.value())
        self.settings.setValue("sync/delay", panel.delay.value())
        self.settings.setValue("sync/auto_delay", panel.auto_delay.isChecked())

    # ---- AI captions (GPU server) -------------------------------------------

    def toggle_ai_captions(self, enabled):
        if enabled:
            self.start_ai_captions()
        else:
            self.remote.stop()

    def start_ai_captions(self, url=None):
        url = url or self.page_url
        if not url:
            self.caption_panel.set_ai_active(False)
            self.caption_panel.set_ai_status("error", "먼저 YouTube 라이브 또는 스트림 URL을 열어 주세요.")
            return False
        options = self.caption_panel.ai_options()
        if not options["server"]:
            self.caption_panel.set_ai_active(False)
            self.caption_panel.set_ai_status("error", "AI 자막 서버 주소를 입력해 주세요.")
            return False
        self.save_settings()
        logger.info("Starting AI captions for %s via %s", url, options["server"])
        self.caption_panel.set_export_enabled(False)
        self.caption_panel.set_measured_lag(None)
        self.caption_pts0 = None
        self.reset_delay_samples()       # a new caption session numbers sentences from c1 again
        self.update_sync_mode()
        self.remote.start(options["server"], url, options["language"], options["diarization"],
                          options["keywords"])
        return True

    def on_ai_status(self, state, detail):
        self.caption_panel.set_ai_status(state, detail)
        if state == "error":
            self.statusBar().showMessage(f"AI 자막: {detail}", 8000)
        self.update_source_name()

    def on_ai_active(self, active):
        self.caption_panel.set_ai_active(active)
        if not active:
            self.caption_pts0 = None
        self.update_sync_mode()
        self.update_source_name()

    def export_captions(self, fmt):
        url = self.remote.export_url(fmt)
        if not url:
            return
        name = "captions.srt" if fmt == "srt" else "captions.vtt"
        path, _ = QFileDialog.getSaveFileName(self, "자막 저장", name,
                                              "SRT 자막 (*.srt)" if fmt == "srt" else "WebVTT 자막 (*.vtt)")
        if not path:
            return
        reply = self.network.get(QNetworkRequest(QUrl(url)))

        def finished():
            reply.deleteLater()
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self.statusBar().showMessage(f"자막 저장 실패: {reply.errorString()}", 8000)
                return
            Path(path).write_bytes(bytes(reply.readAll()))
            logger.info("Saved %s captions to %s", fmt, path)
            self.statusBar().showMessage(f"자막을 저장했습니다: {path}", 5000)
        reply.finished.connect(finished)

    # ---- captions ---------------------------------------------------------

    def open_subtitle(self):
        path, _ = QFileDialog.getOpenFileName(self, "SRT 자막 선택", "", "SRT 자막 (*.srt)")
        if path:
            self.load_subtitle(path)

    def load_subtitle(self, path):
        logger.info("Loading SRT: %s", path)
        try:
            cues = load_srt(path)
        except (OSError, ValueError) as error:
            logger.warning("SRT load failed: %s", error)
            self.statusBar().showMessage(f"자막 열기 실패: {error}")
            return False
        self.track.set_cues(cues)
        self.subtitle_file = Path(path).name
        logger.info("SRT loaded: %d cues", len(cues))
        self.caption_panel.set_cues(cues)
        self.update_source_name()
        self.refresh_subtitle()
        self.statusBar().showMessage(f"자막 {len(cues)}개 구간을 불러왔습니다.")
        return True

    def clear_subtitles(self):
        logger.debug("Subtitles cleared")
        self.track.clear()
        self.subtitle_file = None
        self.caption_panel.set_cues([])
        self.update_source_name()
        self.video_view.set_subtitle("")

    def set_live_captions(self, enabled, port):
        if enabled and not self.caption_server.start(port):
            self.statusBar().showMessage(f"포트 {port}에서 자막을 수신할 수 없습니다: {self.caption_server.error()}")
            enabled = False
        elif not enabled:
            self.caption_server.stop()
        self.caption_panel.live_button.setChecked(enabled)
        self.caption_panel.port.setEnabled(not enabled)
        self.update_live_status()
        self.update_source_name()
        return enabled

    def update_live_status(self):
        if self.caption_server.is_listening():
            text = f"127.0.0.1:{self.caption_server.port()} 대기 중 · 연결 {len(self.caption_server.buffers)}개"
        else:
            text = "수신 꺼짐"
        self.caption_panel.live_status.setText(text)

    def update_source_name(self):
        names = []
        if self.subtitle_file:
            names.append(f"자막: {self.subtitle_file}")
        if self.remote.active:
            names.append("AI 자막")
        if self.caption_server.is_listening():
            names.append("로컬 자막 수신 중")
        text = " · ".join(names) or "자막 없음"
        self.subtitle_name.setText(text)
        self.caption_panel.source_name.setText(text)

    def receive_caption(self, message):
        """Place one live caption (AI server, local inlet or typed) on the timeline."""
        if message.get("clear"):
            self.track.clear()
            self.caption_panel.set_cues([])
            self.video_view.set_subtitle("")
            return
        if message.get("remove"):
            index = self.track.remove(message.get("id"))
            if index is not None:
                self.caption_panel.remove_row(index)
                self.refresh_subtitle()
            return
        if message.get("lag") is not None and message.get("final"):
            self.caption_panel.set_measured_lag(max(0, int(message["lag"])))
        position = self.player.position()
        start, end = message.get("start"), message.get("end")
        placed = self.media_position(message.get("stream_start"))
        if placed is not None:
            start, end = placed, self.media_position(message.get("stream_end"))
        index, replaced, trimmed = self.track.push(
            message["text"], position, start=start, end=end, duration=message.get("duration"),
            cue_id=message.get("id"), speaker=message.get("speaker"), final=message.get("final", True))
        cue = self.track.cues[index]
        if placed is not None:
            # Negative lead = the scene already played before its caption arrived.
            lead = cue.start + self.track.offset - position
            if message.get("final"):
                logger.info("Caption %s placed at %d ms, lead %+d ms", message.get("id"), cue.start, lead)
            # The hold is sized by when a caption first shows up (its first interim), not by the
            # final, which only comes once the sentence is over. Sentences spoken before playback
            # began say nothing about how late captions run.
            if not replaced and cue.start >= 1000:
                logger.info("Caption %s first shown, lead %+d ms", message.get("id"), lead)
                self.adjust_auto_delay(lead, message.get("id"))
        logger.debug("Live caption at %d ms (replace=%s): %s", cue.start, replaced, message["text"])
        for _ in range(trimmed):
            self.caption_panel.remove_first()
        if replaced:
            self.caption_panel.update_cue(index, self.track.cues[index])
        else:
            self.caption_panel.insert_cue(index, self.track.cues[index])
        self.refresh_subtitle()

    def refresh_subtitle(self, position=None):
        position = self.player.position() if position is None else position
        index = self.track.active_index(position)
        if index is None:
            self.video_view.set_subtitle("")
        else:
            cue = self.track.cues[index]
            self.video_view.set_subtitle(cue.text, speaker_color(cue.speaker), interim=not cue.final)
        self.caption_panel.set_active(index, self.track.cues)

    # ---- sync -------------------------------------------------------------

    def set_caption_clock(self, pts0):
        self.caption_pts0 = pts0
        self.log_clocks()

    def set_playback_clock(self, pts0):
        self.playback_pts0 = pts0
        self.log_clocks()

    def log_clocks(self):
        if self.auto_sync_active():
            logger.info("Auto sync on: caption audio starts %.3f s after playback position 0",
                        self.caption_pts0 - self.playback_pts0)
        self.update_sync_mode()

    def auto_sync_active(self):
        return self.caption_pts0 is not None and self.playback_pts0 is not None

    def update_sync_mode(self):
        if self.auto_sync_active():
            self.caption_panel.set_sync_mode(True, "방송 시각 기준으로 자막을 자동 배치 중")
        elif self.remote.active:
            self.caption_panel.set_sync_mode(False, "방송 시각을 맞추는 중… (그동안은 도착 시점 기준)")
        else:
            self.caption_panel.set_sync_mode(False, "자동 싱크 꺼짐 — 필요하면 수동 조절을 사용하세요")

    def adjust_auto_delay(self, lead, caption_id=None):
        """Keep the picture just late enough for captions to show from their first word.

        ``lead`` is how far ahead of its scene a sentence's first caption appeared (negative:
        the sentence had already started). Each sentence counts once: interim updates and the
        final of an already counted ``caption_id`` are ignored. Captions are already at the
        right place; a hold (pause) only makes late ones visible from the start. The
        second-largest lateness ignores one-off slow sentences. When nearly every recent
        sentence arrives comfortably early, the wanted delay is lowered one small step and
        fast playback wins that step back (see start_recovery). Samples behind a decision are
        used up, so the next decision waits for new sentences.
        """
        if caption_id is not None:
            if caption_id in self.counted_ids:
                return
            self.counted_ids.add(caption_id)
            self.counted_order.append(caption_id)
            while len(self.counted_order) > 500:
                self.counted_ids.discard(self.counted_order.popleft())
        self.first_shown[0] += 1
        if lead <= -LATE_CAPTION_MS:
            self.first_shown[1] += 1
        self.late_samples = (self.late_samples + [lead])[-AUTO_EASE_WINDOW:]
        if (not self.caption_panel.auto_delay.isChecked() or not self.live_stream
                or self.delay_timer.isActive() or len(self.late_samples) < AUTO_DELAY_SAMPLES):
            return
        late = sorted(max(0, -value) for value in self.late_samples)[-2]
        hold = min(late + AUTO_DELAY_MARGIN_MS, AUTO_DELAY_MAX_MS - self.auto_delay_ms)
        if late >= AUTO_DELAY_MARGIN_MS and hold >= AUTO_DELAY_MARGIN_MS:
            self.finish_recovery("hold")
            logger.info("Auto video delay: captions up to %d ms late; holding %d ms (total %d ms)",
                        late, hold, self.auto_delay_ms + hold)
            self.auto_delay_ms += hold
            self.auto_hold_ms += hold
            self.late_samples = []
            self.delay_video(hold)
            return
        if len(self.late_samples) < AUTO_EASE_WINDOW or self.auto_delay_ms <= 0 or self.recovery:
            return
        early = sum(1 for value in self.late_samples if value >= AUTO_EASE_EARLY_MS)
        latest = min(self.late_samples)
        if early < AUTO_EASE_MIN_EARLY or latest <= -AUTO_EASE_LATE_MS:
            return
        step = min(AUTO_EASE_STEP_MS, self.auto_delay_ms)
        logger.info("Auto delay recovery decided: %d/%d sentences >= %d ms early, latest lead %+d ms; "
                    "requesting %d ms (auto delay %d -> %d ms)", early, len(self.late_samples),
                    AUTO_EASE_EARLY_MS, latest, step, self.auto_delay_ms, self.auto_delay_ms - step)
        self.auto_delay_ms -= step
        self.late_samples = []
        self.start_recovery(step)

    # Winning an eased-off step back. Only media actually played faster than real time counts:
    # nothing is credited while paused, stalled or when the source keeps 1.0x.

    def start_recovery(self, milliseconds):
        self.recovery = {"requested": milliseconds, "recovered": 0.0, "mark": None,
                         "started": self.monotonic()}
        if self.catch_up_refused:
            self.finish_recovery("refused")
            return
        self.adjust_catch_up()

    def advance_recovery(self):
        """Credit the recovery with how far playback ran ahead of the wall clock since last time."""
        job = self.recovery
        if job is None:
            return
        now, position = self.monotonic(), self.player.position()
        rate = self.player.playbackRate()
        fast = (rate > 1.001 and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
                and not self.delay_timer.isActive())
        if job["mark"] is not None and fast:
            last_position, last_time = job["mark"]
            played, elapsed = position - last_position, (now - last_time) * 1000
            # A seek or reconnect jumps the position; such an interval proves nothing.
            if 0 <= played <= elapsed * 1.5 + 1000:
                job["recovered"] += max(0.0, min(played - elapsed, played * (1 - 1 / rate)))
        job["mark"] = (position, now) if fast else None
        if job["recovered"] >= job["requested"] - 0.5:
            self.finish_recovery("done")

    def finish_recovery(self, reason):
        """End the recovery. Time not won back stays wanted, unless the target was already met."""
        job, self.recovery = self.recovery, None
        if job is None:
            return
        recovered = min(job["requested"], round(job["recovered"]))
        if reason not in ("done", "at_target"):
            self.auto_delay_ms += job["requested"] - recovered
        logger.info("Auto delay recovery ended: reason=%s requested=%d ms recovered=%d ms after %.1f s "
                    "(auto delay %d ms)", reason, job["requested"], recovered,
                    self.monotonic() - job["started"], self.auto_delay_ms)

    def reset_delay_samples(self):
        """Forget recent sentences (new source, live reconnect or caption session: ids restart)."""
        self.late_samples = []
        self.counted_ids = set()
        self.counted_order = deque()

    def media_position(self, stream_ms):
        """Player position (ms) of a moment ``stream_ms`` into the server audio, if both clocks are known."""
        if stream_ms is None or self.caption_pts0 is None or self.playback_pts0 is None:
            return None
        return round((self.caption_pts0 - self.playback_pts0) * 1000 + stream_ms)

    def set_offset(self, milliseconds):
        self.track.offset = milliseconds
        logger.info("Caption offset: %d ms", milliseconds)
        self.statusBar().showMessage(f"자막 싱크 {format_offset(milliseconds)}", 3000)
        self.refresh_subtitle()

    def nudge_offset(self, step):
        self.caption_panel.set_offset(self.track.offset + step)

    def sync_cue_to_now(self, row):
        if 0 <= row < len(self.track.cues):
            self.caption_panel.set_offset(self.player.position() - self.track.cues[row].start)

    def seek_to_cue(self, row):
        if 0 <= row < len(self.track.cues) and self.player.isSeekable():
            self.player.setPosition(max(0, self.track.cues[row].start + self.track.offset))

    def delay_video(self, milliseconds):
        """Hold the picture so late live captions can catch up with it."""
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.statusBar().showMessage("재생 중일 때만 영상을 지연할 수 있습니다.")
            return
        logger.info("Delaying video by %d ms", milliseconds)
        self.player.pause()
        self.delay_timer.start(milliseconds)
        self.statusBar().showMessage(f"영상을 {milliseconds / 1000:.1f}초 지연하는 중…")

    def go_live(self):
        if self.source_url is None:
            self.statusBar().showMessage("먼저 영상을 열어 주세요.")
            return
        logger.info("Reconnecting to the live edge")
        self.open_source(self.source_url, live=self.live_stream)

    def measure_behind_live(self):
        """Milliseconds the picture is behind the newest published moment, if measurable."""
        if not self.live_stream or self.playback_pts0 is None:
            return None
        edge = self.stream_resolver.proxy.live_edge_pts()
        if edge is None:
            return None
        return max(0, round((edge - self.playback_pts0) * 1000 - self.player.position()))

    def update_delay_status(self):
        self.behind_live_ms = self.measure_behind_live()
        # The edge estimate steps by a segment (1 s); average ~5 s so catch-up does not flap.
        if self.behind_live_ms is None:
            self.behind_smooth_ms = None
        elif self.behind_smooth_ms is None:
            self.behind_smooth_ms = float(self.behind_live_ms)
        else:
            self.behind_smooth_ms += 0.1 * (self.behind_live_ms - self.behind_smooth_ms)
        measured = self.behind_live_ms is not None
        if measured and time.monotonic() - getattr(self, "behind_logged_at", 0) >= 10:
            self.behind_logged_at = time.monotonic()
            shown, late = self.first_shown
            logger.info("Behind live: %.1f s (auto delay %.1f s, recovery %s); first captions >= %d ms "
                        "late: %d/%d (%.0f%%)", self.behind_live_ms / 1000, self.auto_delay_ms / 1000,
                        "active" if self.recovery else "idle", LATE_CAPTION_MS, late, shown,
                        100 * late / shown if shown else 0)
        behind = self.behind_live_ms if measured else self.video_delay_ms
        delayed = self.live_stream and (measured or behind >= 100)
        seconds = behind / 1000
        self.live_badge.setVisible(self.live_stream)
        self.live_badge.setText(f"LIVE −{seconds:.1f}초" if delayed else "LIVE")
        self.live_badge.setToolTip("YouTube에 올라온 최신 조각보다 늦은 정도 (방송 시각으로 측정)" if measured
                                   else "일시정지로 쌓인 지연 (측정 전 추정값)")
        if self.live_badge.property("delayed") != delayed:
            self.live_badge.setProperty("delayed", delayed)
            self.live_badge.style().unpolish(self.live_badge)
            self.live_badge.style().polish(self.live_badge)
        if not delayed:
            text = "영상 지연 없음"
        elif measured:
            extra = f" (자동 지연 {self.auto_delay_ms / 1000:.1f}초 포함)" if self.auto_delay_ms else ""
            text = f"라이브보다 {seconds:.1f}초 늦게 재생 중{extra}"
        else:
            text = f"영상이 라이브보다 약 {seconds:.1f}초 늦게 재생 중"
        self.caption_panel.delay_status.setText(text)
        self.adjust_catch_up()

    def catch_up_target(self):
        # Viewer pauses stay; automatic holds are replaced by what auto sync wants now.
        return LIVE_TARGET_MS + self.video_delay_ms - self.auto_hold_ms + self.auto_delay_ms

    def adjust_catch_up(self):
        """Choose the playback rate: 1.05x for re-buffering drift or for an eased-off delay.

        Two reasons share one rate. The re-buffering rule starts once the picture is
        CATCH_UP_START_MS past the target and runs until it is back at the target; the target
        includes every deliberate delay (pauses, automatic holds), so it never undoes a delay
        the viewer or auto sync chose. A recovery (an eased-off step of automatic delay) plays
        fast below that threshold until the step is won back. Either one keeps the rate up,
        so neither ends the other early.
        """
        self.advance_recovery()
        target = self.catch_up_target()
        behind = self.behind_smooth_ms if self.behind_smooth_ms is not None else self.behind_live_ms
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        enabled = self.caption_panel.auto_delay.isChecked()
        free = playing and not self.delay_timer.isActive() and enabled
        if not enabled:
            self.finish_recovery("disabled")
        elif self.recovery is not None and behind is not None and behind <= target:
            self.finish_recovery("at_target")
        if behind is None or not free or behind <= target:
            self.catch_up_normal = False
        elif behind > target + CATCH_UP_START_MS:
            self.catch_up_normal = True
        recovering = self.recovery is not None and free
        wanted = CATCH_UP_RATE if (self.catch_up_normal or recovering) else 1.0
        # Qt stores the rate as a float, so 1.05 comes back as 1.0499999.
        if abs(wanted - self.player.playbackRate()) < 0.001 or (wanted > 1.0 and self.catch_up_refused):
            if wanted > 1.0 and self.catch_up_refused:
                self.finish_recovery("refused")
            return
        if wanted > 1.0:
            reason = "re-buffering" + (" + recovery" if recovering else "") if self.catch_up_normal else "recovery"
        elif not enabled:
            reason = "auto delay off"
        elif not free:
            reason = "paused"
        else:
            reason = "target reached"
        logger.info("Catch-up %s (%s): %s behind live, target %.1f s", "on" if wanted > 1.0 else "off",
                    reason, "?" if behind is None else f"{behind / 1000:.1f} s", target / 1000)
        self.player.setPlaybackRate(wanted)
        if wanted > 1.0 and self.player.playbackRate() <= 1.001:
            # Qt's FFmpeg backend refuses rate changes on some live sources; stop asking.
            logger.info("Catch-up unavailable: the player keeps 1.0x on this source")
            self.catch_up_refused = True
            self.catch_up_normal = False
            self.finish_recovery("refused")
        elif wanted > 1.0 and self.recovery is not None:
            # Credit starts from this moment, not from when the decision was made.
            self.recovery["mark"] = (self.player.position(), self.monotonic())

    # ---- playback ---------------------------------------------------------

    def seek_to_position(self, position=None):
        position = self.seek.value() if position is None else position
        logger.info("Seek requested: %d ms -> %d ms", self.player.position(), position)
        self.player.setPosition(position)

    def show_stream_error(self, message):
        logger.error("Stream resolution: %s", message)
        self.statusBar().showMessage(message)
        if self.ai_after_resolve:
            self.ai_after_resolve = False
            self.caption_panel.set_ai_status("error", "영상을 열지 못해 AI 자막을 시작하지 않았습니다.")

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "미디어 파일 선택")
        if path:
            self.page_url = None
            self.remote.stop()
            self.open_source(QUrl.fromLocalFile(path))

    def open_url(self):
        url = QUrl(self.url_input.text().strip())
        logger.info("URL requested: %s", url.toString())
        if not url.isValid() or url.scheme().lower() not in {"http", "https"} or not url.host():
            self.statusBar().showMessage("유효한 HTTP(S) URL을 입력해 주세요.")
            logger.warning("Invalid URL input")
            return
        self.page_url = url.toString()
        if is_youtube_url(url):
            self.stop_playback()
            self.statusBar().showMessage("YouTube 스트림 주소 확인 중… (정지로 취소)")
            self.stream_resolver.resolve(url)
            # Start captions only once we know which exact video is playing (see on_stream_resolved).
            self.ai_after_resolve = self.caption_panel.auto_start.isChecked()
            if self.ai_after_resolve:
                self.remote.stop(silent=True)
                self.caption_panel.set_ai_status("connecting", "영상 확인 후 AI 자막을 시작합니다…")
            return
        self.open_source(url)

    def on_stream_resolved(self, stream_url):
        if self.stream_resolver.video_url:
            logger.info("Pinned video for captions: %s", self.stream_resolver.video_url)
            self.page_url = self.stream_resolver.video_url
        self.open_source(QUrl(stream_url), live=self.stream_resolver.is_live)
        if self.ai_after_resolve:
            self.ai_after_resolve = False
            self.start_ai_captions(self.page_url)

    def open_source(self, url, live=False):
        logger.info("Opening media source: %s", url.toString())
        self.stream_resolver.cancel()
        self.delay_timer.stop()
        self.player.stop()
        self.source_url = url
        self.live_stream = live
        # The previous broadcast's recovery (and its bookkeeping) ends here.
        self.finish_recovery("source changed")
        self.player.setPlaybackRate(1.0)
        self.catch_up_refused = False
        self.behind_smooth_ms = None
        self.video_delay_ms = 0
        self.auto_delay_ms = 0
        self.auto_hold_ms = 0
        self.reset_delay_samples()
        self.catch_up_normal = False
        self.first_shown = [0, 0]
        self.paused_at = None
        # The proxy reports position 0 again from the first segments of this load.
        self.playback_pts0 = None
        self.stream_resolver.proxy.reset_clock()
        self.update_sync_mode()
        host = url.host().lower()
        self.prefer_best_video = host == "googlevideo.com" or host.endswith(".googlevideo.com")
        self.last_video_size = None
        self.video_info.clear()
        options = QPlaybackOptions()
        if live:
            options.setPlaybackIntent(QPlaybackOptions.PlaybackIntent.LowLatencyStreaming)
        self.player.setPlaybackOptions(options)
        logger.info("Playback profile: %s", "low latency live" if live else "standard")
        self.clear_subtitles()
        self.update_delay_status()
        self.video_view.placeholder.hide()
        self.seek.setEnabled(False)
        if self.player.source() == url:
            # Qt ignores an unchanged source; clear it so a reconnect really reloads the stream.
            self.player.setSource(QUrl())
        self.player.setSource(url)
        self.play_button.setEnabled(True)
        self.statusBar().showMessage("미디어를 불러오는 중…")
        self.player.play()

    def select_video_quality(self):
        if not self.prefer_best_video:
            return
        tracks = self.player.videoTracks()
        ranked = []
        for index, track in enumerate(tracks):
            size = track.value(QMediaMetaData.Key.Resolution)
            pixels = size.width() * size.height() if size and size.isValid() else 0
            fps = track.value(QMediaMetaData.Key.VideoFrameRate) or 0
            ranked.append((pixels, fps, index))
            logger.info("Video track %d: size=%s fps=%s", index, size, fps)
        if ranked:
            _, _, best = max(ranked)
            logger.info("Selecting highest resolution video track %d (previous=%d)", best, self.player.activeVideoTrack())
            self.player.setActiveVideoTrack(best)

    def report_video_size(self, frame):
        if not frame.isValid() or frame.size() == self.last_video_size:
            return
        self.last_video_size = frame.size()
        width, height = frame.width(), frame.height()
        logger.info("Decoded video resolution: %dx%d", width, height)
        profile = " · 저지연" if self.live_stream else ""
        self.video_info.setText(f"{width}×{height}{profile}")

    def stop_all(self):
        self.ai_after_resolve = False
        self.stop_playback()
        self.remote.stop()

    def stop_playback(self):
        logger.info("Stop requested; position=%d ms", self.player.position())
        self.stream_resolver.cancel()
        self.delay_timer.stop()
        self.player.stop()

    def toggle_play(self):
        logger.info("Play/pause requested; state=%s", self.player.playbackState().name)
        if not self.play_button.isEnabled():
            return
        self.delay_timer.stop()
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def toggle_mute(self):
        self.audio.setMuted(not self.audio.isMuted())
        self.mute_button.setIcon(self.volume_icons[self.audio.isMuted()])

    def update_state(self, state):
        logger.info("Playback state: %s; position=%d ms", state.name, self.player.position())
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setIcon(self.play_icons[playing])
        # Every pause on a live stream pushes the picture further behind the broadcast.
        if state == QMediaPlayer.PlaybackState.PausedState and self.live_stream:
            self.paused_at = time.monotonic()
        elif self.paused_at is not None:
            if playing:
                self.video_delay_ms += round((time.monotonic() - self.paused_at) * 1000)
                self.update_delay_status()
            self.paused_at = None
        if playing:
            self.statusBar().showMessage("재생 중")

    def update_position(self, position):
        self.refresh_subtitle(position)
        if not self.seek.pressed:
            self.seek.setValue(position)
        if self.live_stream:
            self.time_label.setText(format_time(position))
        else:
            self.time_label.setText(f"{format_time(position)} / {format_time(self.player.duration())}")

    def update_duration(self, duration):
        logger.info("Media duration: %d ms", duration)
        self.seek.setRange(0, duration)
        self.update_position(self.player.position())

    def show_error(self, _error, message):
        logger.error("Playback error %s at %d ms: %s", _error, self.player.position(), message)
        self.statusBar().showMessage(f"재생 오류: {message}")

    # ---- window -----------------------------------------------------------

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.exit_fullscreen()
            return
        chrome = (self.menuBar(), self.top_bar, self.statusBar(), self.caption_dock, self.log_dock)
        self.fullscreen_restore = [widget for widget in chrome if not widget.isHidden()]
        for widget in self.fullscreen_restore:
            widget.hide()
        self.showFullScreen()
        self.idle_timer.start()

    def exit_fullscreen(self):
        if not self.isFullScreen():
            return
        self.idle_timer.stop()
        self.showNormal()
        for widget in self.fullscreen_restore:
            widget.show()
        self.fullscreen_restore = []
        self.control_bar.show()
        self.video_view.viewport().unsetCursor()

    def show_controls(self):
        if self.isFullScreen():
            self.control_bar.show()
            self.video_view.viewport().unsetCursor()
            self.idle_timer.start()

    def hide_controls(self):
        if self.isFullScreen() and not self.control_bar.underMouse():
            self.control_bar.hide()
            self.video_view.viewport().setCursor(Qt.CursorShape.BlankCursor)

    def closeEvent(self, event):
        logger.info("Closing player window")
        self.stop_playback()
        self.caption_server.stop()
        self.remote.stop(silent=True)
        self.save_settings()
        logging.getLogger("livecaption").removeHandler(self.log_handler)
        self.log_handler.close()
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description="LiveCaption media player")
    parser.add_argument("url", nargs="?", help="YouTube page or HTTP(S) media URL")
    parser.add_argument("--caption-port", type=int, metavar="PORT",
                        help="start receiving live captions on 127.0.0.1:PORT")
    parser.add_argument("--server", metavar="HOST:PORT",
                        help="AI caption server address (saved for next time)")
    args = parser.parse_args()
    configure_logging()
    install_diagnostics()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = PlayerWindow()
    window.show()
    if args.caption_port:
        window.caption_panel.port.setValue(args.caption_port)
        window.set_live_captions(True, args.caption_port)
    if args.server:
        window.caption_panel.server.setText(args.server)
    if args.url:
        window.url_input.setText(args.url)
        QTimer.singleShot(0, window.open_url)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
