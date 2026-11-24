"""Client for the LiveCaption GPU server: send a stream URL, receive captions.

The server pulls the stream's audio itself, so only the URL goes up and only
caption events come down. See server/caption_api.py for the other side.
"""
import json
import logging
import os

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWebSockets import QWebSocket


logger = logging.getLogger("livecaption.remote")
# Caption server address; set LIVECAPTION_SERVER or type it in the panel.
DEFAULT_SERVER = os.environ.get("LIVECAPTION_SERVER", "localhost:8000")
LANGUAGES = [("한국어", "ko"), ("자동 감지", None), ("영어", "en"), ("일본어", "ja"), ("중국어", "zh")]


def websocket_url(server: str) -> QUrl:
    """Accept 'host:port', 'http://host:port' or a full ws:// URL; return the /ws endpoint."""
    text = server.strip()
    if "://" not in text:
        text = "ws://" + text
    url = QUrl(text)
    scheme = url.scheme().lower()
    url.setScheme({"http": "ws", "https": "wss"}.get(scheme, scheme))
    if url.path() in ("", "/"):
        url.setPath("/ws")
    return url


def http_base(server: str) -> str:
    url = websocket_url(server)
    return f"{'https' if url.scheme() == 'wss' else 'http'}://{url.authority()}"


def speaker_name(label):
    """'SPEAKER_00' -> '화자 1'; other labels (enrolled names) are kept."""
    if not label:
        return None
    if label.startswith("SPEAKER_") and label[8:].isdigit():
        return f"화자 {int(label[8:]) + 1}"
    return label


def parse_server_event(raw: str) -> dict | None:
    """Validate one server message; drop anything malformed."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("type"), str):
        return None
    if data["type"] == "caption":
        if not isinstance(data.get("id"), str):
            return None
        if data.get("remove"):
            return {"type": "caption", "id": data["id"], "remove": True}
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        event = {"type": "caption", "id": data["id"], "text": text.strip(),
                 "final": bool(data.get("final", True)),
                 "speaker": speaker_name(data.get("speaker")) if isinstance(data.get("speaker"), str) else None,
                 "lag": _number(data.get("lag"))}
        # Milliseconds into the server's audio; placed on the video via the timeline event.
        start, end = _number(data.get("stream_start")), _number(data.get("stream_end"))
        if start is not None and start >= 0:
            event["stream_start"] = start
            if end is not None and end > start:
                event["stream_end"] = end
        return event
    if data["type"] == "timeline":
        pts0 = _number(data.get("pts0"))
        return {"type": "timeline", "pts0": float(pts0)} if pts0 is not None else None
    return data


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class RemoteCaptionClient(QObject):
    caption = Signal(dict)
    timeline = Signal(float)           # source PTS (seconds) of the server audio's first sample
    status = Signal(str, str)          # state, human readable detail
    session_started = Signal(dict)     # {"id", "title", "live"}
    active_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket = QWebSocket(parent=self)
        self.socket.connected.connect(self._on_connected)
        self.socket.disconnected.connect(self._on_disconnected)
        self.socket.textMessageReceived.connect(self._on_message)
        self.socket.errorOccurred.connect(self._on_error)
        self.keepalive = QTimer(self)
        self.keepalive.setInterval(20000)
        self.keepalive.timeout.connect(lambda: self.socket.ping())
        self.server = DEFAULT_SERVER
        self.request = None
        self.session = None
        self.active = False
        self.state = "idle"

    def start(self, server: str, url: str, language=None, diarization=False, keywords=""):
        self.stop(silent=True)
        self.server = server
        self.request = {"type": "start", "url": url, "language": language,
                        "diarization": diarization, "max_speakers": 4}
        if keywords:
            # Hotwords typed in the panel; the server merges them with the broadcast's own.
            self.request["keywords"] = keywords
        self.session = None
        self._set_active(True)
        target = websocket_url(server)
        logger.info("Connecting to caption server %s", target.toString())
        self._emit_status("connecting", f"서버 연결 중 ({target.host()}:{target.port()})")
        self.socket.open(target)

    def stop(self, silent=False):
        was_active = self.active
        self.request = None
        self.keepalive.stop()
        if self.socket.state() == QAbstractSocket.SocketState.ConnectedState:
            self.socket.sendTextMessage(json.dumps({"type": "stop"}))
        if self.socket.state() != QAbstractSocket.SocketState.UnconnectedState:
            self.socket.close()
        self._set_active(False)
        if was_active and not silent:
            self._emit_status("idle", "AI 자막 꺼짐")

    def export_url(self, fmt: str) -> str | None:
        if not self.session:
            return None
        return f"{http_base(self.server)}/sessions/{self.session['id']}.{fmt}"

    # ---- socket ---------------------------------------------------------

    def _on_connected(self):
        logger.info("Caption server connected")
        self.keepalive.start()
        if self.request:
            self.socket.sendTextMessage(json.dumps(self.request, ensure_ascii=False))

    def _on_disconnected(self):
        self.keepalive.stop()
        if self.active:
            logger.warning("Caption server disconnected: %s", self.socket.closeReason())
            self._set_active(False)
            if self.state not in {"error", "ended"}:
                self._emit_status("error", "서버 연결이 끊겼습니다.")

    def _on_error(self, _error):
        if self.active:
            logger.error("Caption server socket error: %s", self.socket.errorString())
            self._emit_status("error", f"서버에 연결할 수 없습니다: {self.socket.errorString()}")
            self._set_active(False)

    def _on_message(self, raw):
        event = parse_server_event(raw)
        if event is None:
            logger.warning("Ignored malformed server message: %.200s", raw)
            return
        kind = event["type"]
        if kind == "caption":
            self.caption.emit(event)
        elif kind == "timeline":
            logger.info("Caption timeline: server audio starts at source PTS %.3f", event["pts0"])
            self.timeline.emit(event["pts0"])
        elif kind == "status":
            state, detail = str(event.get("state", "")), str(event.get("detail", ""))
            self._emit_status(state, detail)
            if state in {"error", "ended", "stopped"}:
                self.request = None
                self._set_active(False)
                self.socket.close()
        elif kind == "session":
            self.session = {"id": str(event.get("id", "")), "title": event.get("title"),
                            "live": event.get("live"), "hotwords": event.get("hotwords")}
            logger.info("Caption session %s: %s", self.session["id"], self.session["title"])
            logger.info("Caption hotwords: %s", self.session["hotwords"])
            self.session_started.emit(self.session)

    def _emit_status(self, state, detail):
        self.state = state
        logger.info("Caption server status: %s %s", state, detail)
        self.status.emit(state, detail)

    def _set_active(self, active):
        if active != self.active:
            self.active = active
            self.active_changed.emit(active)
