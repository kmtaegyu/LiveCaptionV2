"""Local TCP inlet where an external caption engine pushes live captions.

Protocol: UTF-8, one message per line. A line is either plain caption text or
a JSON object: {"text": "...", "id": "...", "duration": ms, "start": ms, "end": ms}
or {"clear": true}. Only ``text`` is required; times are media positions.
"""
import json
import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer


logger = logging.getLogger("livecaption.captions")
DEFAULT_PORT = 8765
MAX_LINE = 64 * 1024


def parse_caption_message(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    if not line.startswith("{"):
        return {"text": line}
    try:
        data = json.loads(line)
    except ValueError as error:
        raise ValueError(f"JSON 형식이 올바르지 않습니다: {error}") from None
    if not isinstance(data, dict):
        raise ValueError("자막 메시지는 JSON 객체여야 합니다.")
    if data.get("clear") is True:
        return {"clear": True}
    text = data.get("text")
    if not isinstance(text, str):
        raise ValueError("text 필드(문자열)가 필요합니다.")
    if not text.strip():
        return None
    message = {"text": text.strip()}
    for key in ("start", "end", "duration"):
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"{key} 필드는 0 이상의 밀리초 숫자여야 합니다.")
        message[key] = int(value)
    if data.get("id") is not None:
        message["id"] = str(data["id"])
    return message


class CaptionServer(QObject):
    caption = Signal(dict)
    clients_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._accept)
        self.buffers = {}

    def is_listening(self):
        return self.server.isListening()

    def port(self):
        return self.server.serverPort()

    def error(self):
        return self.server.errorString()

    def start(self, port=DEFAULT_PORT):
        self.stop()
        # Loopback only: captions come from a process on this computer.
        if not self.server.listen(QHostAddress(QHostAddress.SpecialAddress.LocalHost), port):
            logger.error("Caption inlet failed on port %d: %s", port, self.server.errorString())
            return False
        logger.info("Caption inlet listening on 127.0.0.1:%d", self.server.serverPort())
        return True

    def stop(self):
        if self.server.isListening():
            logger.info("Caption inlet stopped")
        self.server.close()
        sockets, self.buffers = list(self.buffers), {}
        for socket in sockets:
            socket.abort()
            socket.deleteLater()
        self.clients_changed.emit(0)

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self.buffers[socket] = b""
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(lambda s=socket: self._closed(s))
            logger.info("Caption client connected (%d total)", len(self.buffers))
            self.clients_changed.emit(len(self.buffers))
            self._read(socket)

    def _closed(self, socket):
        if socket not in self.buffers:
            return
        self._read(socket, final=True)
        del self.buffers[socket]
        logger.info("Caption client disconnected (%d left)", len(self.buffers))
        self.clients_changed.emit(len(self.buffers))
        socket.deleteLater()

    def _read(self, socket, final=False):
        if socket not in self.buffers:
            return
        data = self.buffers[socket] + bytes(socket.readAll())
        *lines, rest = data.split(b"\n")
        if final and rest:
            lines, rest = [*lines, rest], b""
        if len(rest) > MAX_LINE:
            logger.warning("Caption line exceeds %d bytes; dropped", MAX_LINE)
            rest = b""
        self.buffers[socket] = rest
        for line in lines:
            try:
                message = parse_caption_message(line.decode("utf-8", errors="replace"))
            except ValueError as error:
                logger.warning("Caption message ignored: %s", error)
                continue
            if message:
                self.caption.emit(message)
