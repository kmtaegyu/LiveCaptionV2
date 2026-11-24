"""Side panel: AI caption server, sync controls, the cue list and other sources."""
import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QPushButton, QSpinBox, QToolButton, QVBoxLayout, QWidget)

from caption_server import DEFAULT_PORT
from remote_captions import DEFAULT_SERVER, LANGUAGES, TOPICS
from ui_theme import ACCENT, MUTED, speaker_color

STATE_COLORS = {"running": "#3ccf6e", "connecting": "#e3b341", "resolving": "#e3b341",
                "error": "#ef5350", "ended": MUTED, "stopped": MUTED, "idle": MUTED}


def format_offset(milliseconds):
    return f"{milliseconds / 1000:+.1f}초"


def cue_label(cue):
    seconds = max(0, cue.start) // 1000
    stamp = f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"
    speaker = f"{cue.speaker} · " if getattr(cue, "speaker", None) else ""
    interim = " …" if not getattr(cue, "final", True) else ""
    marks = getattr(cue, "marks", ())
    found = ""
    if marks:
        found = "   [" + ", ".join(("고침 " if kind == "fixed" else "사전 ") + word for word, kind in marks) + "]"
    return f"{stamp}   {speaker}{' '.join(cue.text.split())}{interim}{found}"


class CaptionPanel(QWidget):
    open_srt_requested = Signal()
    clear_requested = Signal()
    live_toggled = Signal(bool, int)
    text_entered = Signal(str)
    offset_changed = Signal(int)
    sync_cue_requested = Signal(int)
    seek_cue_requested = Signal(int)
    delay_video_requested = Signal(int)
    go_live_requested = Signal()
    visibility_toggled = Signal(bool)
    font_size_changed = Signal(int)
    ai_toggled = Signal(bool)
    recognition_changed = Signal()     # topic or hotwords edited: a running session restarts with them
    export_requested = Signal(str)

    def __init__(self):
        super().__init__()
        self.active_row = None
        self.measured_lag_ms = None
        self.setMinimumWidth(320)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 10)
        layout.setSpacing(6)
        layout.addWidget(self.build_ai_group())
        layout.addWidget(self.build_sync_group())
        layout.addWidget(self.build_display_row())
        self.cue_list = QListWidget()
        self.cue_list.setWordWrap(True)
        self.cue_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.cue_list.setToolTip("자막을 선택한 뒤 '선택한 자막을 지금 장면에 맞추기'로 싱크를 맞춥니다.\n"
                                 "더블클릭: 해당 자막 위치로 이동 (탐색 가능한 영상)")
        self.cue_list.setMinimumHeight(80)
        self.cue_list.itemDoubleClicked.connect(
            lambda item: self.seek_cue_requested.emit(self.cue_list.row(item)))
        layout.addWidget(self.cue_list, 1)
        layout.addWidget(self.build_other_sources())

    # ---- AI captions ----------------------------------------------------

    def build_ai_group(self):
        group = QGroupBox("AI 자막")
        layout = QVBoxLayout(group)
        layout.setSpacing(6)
        status = QHBoxLayout()
        self.ai_dot = QLabel("●")
        self.ai_status = QLabel("꺼짐")
        self.ai_status.setWordWrap(True)
        status.addWidget(self.ai_dot)
        status.addWidget(self.ai_status, 1)
        layout.addLayout(status)
        self.server = QLineEdit(DEFAULT_SERVER)
        self.server.setPlaceholderText("서버 주소 (예: 192.168.0.10:8000)")
        self.server.setAccessibleName("AI 자막 서버 주소")
        self.server.setToolTip("음성 인식 서버 주소. 영상 주소만 서버로 보내고 자막을 받아옵니다.")
        layout.addWidget(self.server)
        options = QHBoxLayout()
        self.language = QComboBox()
        for label, code in LANGUAGES:
            self.language.addItem(label, code)
        self.language.setAccessibleName("인식 언어")
        self.diarization = QCheckBox("화자 구분")
        self.diarization.setToolTip("말하는 사람마다 다른 색으로 자막을 표시합니다.")
        options.addWidget(self.language, 1)
        options.addWidget(self.diarization)
        layout.addLayout(options)
        self.keywords = QLineEdit()
        self.keywords.setPlaceholderText("핫워드 (쉼표로 구분, 예: APEC, 머신러닝)")
        self.keywords.setAccessibleName("인식 핫워드")
        self.keywords.setToolTip("방송에 나올 고유명사·약어를 넣으면 인식이 그 단어 쪽으로 기웁니다.\n"
                                 "방송 제목·태그·채널명에서 뽑은 단어와 합쳐 서버로 보냅니다. "
                                 "Enter를 누르면 바로 적용됩니다.")
        self.keywords.setClearButtonEnabled(True)
        self.keywords.editingFinished.connect(self.recognition_changed)
        layout.addWidget(self.keywords)
        self.topic = QComboBox()
        for label, code in TOPICS:
            self.topic.addItem(label, code)
        self.topic.setAccessibleName("주제 사전")
        self.topic.setToolTip("서버의 주제 사전(server/dictionaries)을 씁니다. 사전의 용어를 핫워드에 더하고\n"
                              "알려진 오인식을 바로잡습니다. 고르면 바로 적용됩니다.")
        self.topic.currentIndexChanged.connect(lambda _index: self.recognition_changed.emit())
        layout.addWidget(self.topic)
        self.mark_box = QCheckBox("사전 단어 색 표시 (빨강: 고침, 파랑: 사전 단어)")
        self.mark_box.setChecked(True)
        self.mark_box.setToolTip("사전이 고쳐 쓴 단어는 빨강, 사전에 있는 단어가 그대로 인식되면 파랑으로 보여 줍니다.")
        layout.addWidget(self.mark_box)
        self.auto_start = QCheckBox("YouTube 영상을 열면 자동으로 시작")
        self.auto_start.setChecked(True)
        layout.addWidget(self.auto_start)
        self.ai_button = QPushButton("AI 자막 시작")
        self.ai_button.setObjectName("primary")
        self.ai_button.setCheckable(True)
        self.ai_button.setMinimumHeight(32)
        layout.addWidget(self.ai_button)
        exports = QHBoxLayout()
        self.srt_export = QPushButton("SRT 저장")
        self.vtt_export = QPushButton("WebVTT 저장")
        for button, fmt in ((self.srt_export, "srt"), (self.vtt_export, "vtt")):
            button.setEnabled(False)
            button.setToolTip("지금까지 확정된 자막을 파일로 저장합니다.")
            button.clicked.connect(lambda _checked=False, f=fmt: self.export_requested.emit(f))
            exports.addWidget(button)
        layout.addLayout(exports)
        self.ai_button.clicked.connect(self.ai_toggled)
        self.set_ai_status("idle", "꺼짐")
        return group

    def ai_options(self):
        return {"server": self.server.text().strip(), "language": self.language.currentData(),
                "diarization": self.diarization.isChecked(), "keywords": self.keywords.text().strip(),
                "topic": self.topic.currentData()}

    def set_ai_active(self, active):
        self.ai_button.setChecked(active)
        self.ai_button.setText("AI 자막 중지" if active else "AI 자막 시작")
        # Topic and hotwords stay editable: changing them restarts the session (recognition_changed).
        for widget in (self.server, self.language, self.diarization):
            widget.setEnabled(not active)

    def set_ai_status(self, state, detail):
        self.ai_dot.setStyleSheet(f"color: {STATE_COLORS.get(state, MUTED)}; font-size: 14px;")
        self.ai_status.setText(detail or state)

    def set_export_enabled(self, enabled):
        self.srt_export.setEnabled(enabled)
        self.vtt_export.setEnabled(enabled)

    def set_measured_lag(self, milliseconds):
        self.measured_lag_ms = milliseconds
        self.use_lag.setEnabled(milliseconds is not None)
        if milliseconds is None:
            self.lag_label.setText("인식 지연: 측정 전")
        else:
            self.lag_label.setText(f"인식 지연: 약 {milliseconds / 1000:.1f}초")

    # ---- sync -----------------------------------------------------------

    def build_sync_group(self):
        group = QGroupBox("싱크")
        outer = QVBoxLayout(group)
        outer.setSpacing(6)
        mode = QHBoxLayout()
        self.sync_dot = QLabel("●")
        self.sync_mode = QLabel()
        self.sync_mode.setWordWrap(True)
        mode.addWidget(self.sync_dot)
        mode.addWidget(self.sync_mode, 1)
        outer.addLayout(mode)
        self.auto_delay = QCheckBox("영상 지연 자동 조절")
        self.auto_delay.setChecked(True)
        self.auto_delay.setToolTip("자막이 장면보다 늦게 도착하면 영상을 그만큼 자동으로 늦춰\n"
                                   "자막이 첫 마디부터 보이게 합니다. '라이브로 이동'으로 되돌립니다.")
        outer.addWidget(self.auto_delay)
        live_row = QHBoxLayout()
        self.delay_status = QLabel("영상 지연 없음")
        self.delay_status.setObjectName("hint")
        self.delay_status.setWordWrap(True)
        live_button = QPushButton("라이브로 이동")
        live_button.setToolTip("스트림에 다시 연결해 지연 없이 최신 지점부터 재생합니다.")
        live_row.addWidget(self.delay_status, 1)
        live_row.addWidget(live_button)
        outer.addLayout(live_row)
        self.manual_toggle = QToolButton()
        self.manual_toggle.setText("수동 조절 (오프셋 · 영상 지연)")
        self.manual_toggle.setCheckable(True)
        self.manual_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.manual_toggle.setArrowType(Qt.ArrowType.RightArrow)
        outer.addWidget(self.manual_toggle)
        self.manual_box = QWidget()
        layout = QVBoxLayout(self.manual_box)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(6)
        outer.addWidget(self.manual_box)
        self.manual_box.hide()
        self.manual_toggle.toggled.connect(self.set_manual_visible)
        top = QHBoxLayout()
        self.offset_label = QLabel(format_offset(0))
        self.offset_label.setObjectName("offset")
        self.offset = QDoubleSpinBox()
        self.offset.setRange(-3600, 3600)
        self.offset.setDecimals(1)
        self.offset.setSingleStep(0.1)
        self.offset.setSuffix(" 초")
        self.offset.setAccessibleName("자막 싱크 오프셋")
        reset = QPushButton("초기화")
        top.addWidget(self.offset_label, 1)
        top.addWidget(self.offset)
        top.addWidget(reset)
        layout.addLayout(top)
        nudges = QHBoxLayout()
        for step in (-1000, -100, 100, 1000):
            button = QPushButton(format_offset(step))
            button.setToolTip("자막을 더 빨리 표시" if step < 0 else "자막을 더 늦게 표시")
            button.clicked.connect(lambda _checked=False, s=step: self.set_offset(self.offset_ms() + s))
            nudges.addWidget(button)
        layout.addLayout(nudges)
        hint = QLabel("− 자막을 빠르게 · ＋ 자막을 늦게 (단축키 G / H)")
        hint.setObjectName("hint")
        layout.addWidget(hint)
        sync_button = QPushButton("선택한 자막을 지금 장면에 맞추기")
        sync_button.setToolTip("목록에서 지금 들리는 대사를 고른 뒤 누르면 오프셋을 자동으로 계산합니다.")
        layout.addWidget(sync_button)
        delay = QHBoxLayout()
        self.delay = QDoubleSpinBox()
        self.delay.setRange(0.5, 60)
        self.delay.setDecimals(1)
        self.delay.setSingleStep(0.5)
        self.delay.setValue(2)
        self.delay.setSuffix(" 초")
        self.delay.setAccessibleName("영상 지연 시간")
        delay_button = QPushButton("영상 지연")
        delay_button.setToolTip("자막이 영상보다 늦게 도착할 때 영상을 잠시 멈춰 그만큼 뒤로 미룹니다.")
        delay.addWidget(self.delay)
        delay.addWidget(delay_button, 1)
        layout.addLayout(delay)
        lag = QHBoxLayout()
        self.lag_label = QLabel()
        self.lag_label.setObjectName("hint")
        self.use_lag = QPushButton("지연 값으로 사용")
        self.use_lag.setToolTip("서버가 측정한 인식 지연을 위 영상 지연 칸에 넣습니다.")
        lag.addWidget(self.lag_label, 1)
        lag.addWidget(self.use_lag)
        layout.addLayout(lag)
        self.set_measured_lag(None)
        self.set_sync_mode(False, "자동 싱크 꺼짐 — 필요하면 수동 조절을 사용하세요")
        self.offset.valueChanged.connect(self.on_offset_changed)
        reset.clicked.connect(lambda: self.set_offset(0))
        sync_button.clicked.connect(self.request_sync)
        delay_button.clicked.connect(
            lambda: self.delay_video_requested.emit(round(self.delay.value() * 1000)))
        live_button.clicked.connect(self.go_live_requested)
        self.use_lag.clicked.connect(self.apply_measured_lag)
        return group

    def set_sync_mode(self, automatic, detail):
        self.sync_automatic = automatic
        self.sync_dot.setStyleSheet(f"color: {STATE_COLORS['running'] if automatic else MUTED}; font-size: 14px;")
        self.sync_mode.setText(f"자동 싱크 · {detail}" if automatic else detail)

    def set_manual_visible(self, visible):
        self.manual_box.setVisible(visible)
        self.manual_toggle.setChecked(visible)
        self.manual_toggle.setArrowType(Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow)

    def apply_measured_lag(self):
        if self.measured_lag_ms is not None:
            # Round up to the spin box step so the picture never runs ahead of the text.
            self.delay.setValue(max(0.5, math.ceil(self.measured_lag_ms / 500) * 0.5))

    def build_display_row(self):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(2, 0, 2, 0)
        self.visible_box = QCheckBox("자막 표시")
        self.visible_box.setChecked(True)
        self.font_size = QSpinBox()
        self.font_size.setRange(12, 64)
        self.font_size.setValue(22)
        self.font_size.setPrefix("크기 ")
        self.font_size.setAccessibleName("자막 글자 크기")
        self.join_box = QCheckBox("문장 이어 보기")
        self.join_box.setChecked(True)
        self.join_box.setToolTip("문장 중간에서 끊겨 확정된 AI 자막이면, 다음 자막 앞에 그 끝부분을\n"
                                 "이어 붙여 한 구절로 보여 줍니다. 새로 들어오는 자막부터 적용됩니다.")
        layout.addWidget(self.visible_box, 1)
        layout.addWidget(self.join_box)
        layout.addWidget(self.font_size)
        self.visible_box.toggled.connect(self.visibility_toggled)
        self.font_size.valueChanged.connect(self.font_size_changed)
        return row

    # ---- other sources (collapsed) ---------------------------------------

    def build_other_sources(self):
        wrapper = QWidget()
        outer = QVBoxLayout(wrapper)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        self.other_toggle = QToolButton()
        self.other_toggle.setText("기타 자막 소스 (SRT 파일 · 로컬 입력)")
        self.other_toggle.setCheckable(True)
        self.other_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.other_toggle.setArrowType(Qt.ArrowType.RightArrow)
        outer.addWidget(self.other_toggle)
        self.other_box = QWidget()
        layout = QVBoxLayout(self.other_box)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(6)
        self.source_name = QLabel("자막 없음")
        self.source_name.setObjectName("hint")
        self.source_name.setWordWrap(True)
        layout.addWidget(self.source_name)
        files = QHBoxLayout()
        srt_button = QPushButton("SRT 파일 열기")
        clear_button = QPushButton("자막 지우기")
        files.addWidget(srt_button)
        files.addWidget(clear_button)
        layout.addLayout(files)
        live = QHBoxLayout()
        self.live_button = QPushButton("로컬 자막 수신")
        self.live_button.setCheckable(True)
        self.live_button.setToolTip("같은 PC의 외부 자막 엔진이 127.0.0.1의 이 포트로 한 줄씩 자막을 보냅니다.")
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(DEFAULT_PORT)
        self.port.setPrefix("포트 ")
        self.port.setAccessibleName("로컬 자막 수신 포트")
        live.addWidget(self.live_button, 1)
        live.addWidget(self.port)
        layout.addLayout(live)
        self.live_status = QLabel("수신 꺼짐")
        self.live_status.setObjectName("hint")
        layout.addWidget(self.live_status)
        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("자막 직접 입력 후 Enter")
        self.text_input.setAccessibleName("자막 직접 입력")
        layout.addWidget(self.text_input)
        outer.addWidget(self.other_box)
        self.other_box.hide()
        self.other_toggle.toggled.connect(self.set_other_visible)
        srt_button.clicked.connect(self.open_srt_requested)
        clear_button.clicked.connect(self.clear_requested)
        self.live_button.clicked.connect(
            lambda checked: self.live_toggled.emit(checked, self.port.value()))
        self.text_input.returnPressed.connect(self.submit_text)
        return wrapper

    def set_other_visible(self, visible):
        self.other_box.setVisible(visible)
        self.other_toggle.setChecked(visible)
        self.other_toggle.setArrowType(Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow)

    # ---- behaviour ------------------------------------------------------

    def submit_text(self):
        text = self.text_input.text().strip()
        if text:
            self.text_input.clear()
            self.text_entered.emit(text)

    def offset_ms(self):
        return round(self.offset.value() * 1000)

    def set_offset(self, milliseconds):
        self.offset.setValue(milliseconds / 1000)

    def on_offset_changed(self, _value):
        self.offset_label.setText(format_offset(self.offset_ms()))
        self.offset_changed.emit(self.offset_ms())

    def request_sync(self):
        row = self.cue_list.currentRow()
        if row >= 0:
            self.sync_cue_requested.emit(row)

    def style_item(self, item, cue, active=False):
        item.setText(cue_label(cue))
        color = ACCENT if active else speaker_color(getattr(cue, "speaker", None), default="#e6e8ec")
        item.setForeground(QBrush(QColor(color)))
        font = item.font()
        font.setItalic(not getattr(cue, "final", True))
        item.setFont(font)

    def set_cues(self, cues):
        self.active_row = None
        self.cue_list.clear()
        for cue in cues:
            self.cue_list.addItem("")
            self.style_item(self.cue_list.item(self.cue_list.count() - 1), cue)

    def insert_cue(self, row, cue):
        at_bottom = self._at_bottom()
        self.cue_list.insertItem(row, "")
        self.style_item(self.cue_list.item(row), cue)
        if self.active_row is not None and row <= self.active_row:
            self.active_row += 1
        if row == self.cue_list.count() - 1 and at_bottom:
            self.cue_list.scrollToBottom()

    def update_cue(self, row, cue):
        item = self.cue_list.item(row)
        if item:
            self.style_item(item, cue, active=row == self.active_row)

    def remove_row(self, row):
        self.cue_list.takeItem(row)
        if self.active_row is not None:
            if row == self.active_row:
                self.active_row = None
            elif row < self.active_row:
                self.active_row -= 1

    def remove_first(self):
        self.remove_row(0)

    def _at_bottom(self):
        bar = self.cue_list.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def set_active(self, row, cues=None):
        """Mark the cue currently on screen without touching the user's selection."""
        if row == self.active_row:
            return
        previous, self.active_row = self.active_row, row
        for index, active in ((previous, False), (row, True)):
            item = self.cue_list.item(index) if index is not None else None
            if item and cues is not None and index < len(cues):
                self.style_item(item, cues[index], active)
            elif item:
                item.setForeground(QBrush(QColor(ACCENT if active else "#e6e8ec")))
        if row is not None and self.cue_list.item(row) and self._at_bottom():
            self.cue_list.scrollToItem(self.cue_list.item(row))
