"""Loopback HLS proxy: starts playback near the live edge and knows the source clock.

YouTube offers two kinds of live playlists. The short muxed one (what streamlink
returns: video + audio in each segment, three segments listed) is 2-3 s fresher
than the long per-track DVR playlists yt-dlp returns (an hour of one-second
segments, several megabytes, reloaded every second). The proxy serves either:

- Each upstream reload is merged into a rolling window, so a short upstream list
  still leaves the player older segments to fall back on after a pause or an
  automatic video delay, and a long one is cut to the newest entries.
- The first load after (re)opening lists only the newest ``START_SEGMENTS``, so
  FFmpeg (which starts three segments from the end) begins next to the live edge.

Segment URIs point back at the proxy, which serves them itself: each new
segment is downloaded the moment a reload lists it, so the player usually gets
it from memory (or streamed while it still arrives) instead of opening its own
origin connection. Serving segments also shows which ones the player starts
from: the proxy reads the first PTS of the first segment of each playlist
requested after (re)opening and reports the smallest as ``clock_ready``. Qt's position 0 is that source time,
and YouTube's source PTS is the same clock the caption server reports as
``pts0``, so captions can be placed without relaying the video. The window's
segment durations also give the live edge, so the player can show how far
behind live it is.
"""
import logging
import secrets
import struct
import time
from urllib.parse import urljoin, urlsplit

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import (QAbstractSocket, QHostAddress, QNetworkAccessManager,
    QNetworkReply, QNetworkRequest, QTcpServer)


logger = logging.getLogger("livecaption.hls")
KEEP_SEGMENTS = 60           # listed to the player: room for pauses and automatic delay
START_SEGMENTS = 1           # listed on the first load: start this close to the live edge
MAX_AGE = 0.8
POLL_INTERVAL_MS = 1000      # own reloads of live playlists, well inside a 3 s upstream window
POLL_IDLE_STOP = 30          # stop polling once no player has asked for a playlist this long
HEADER_TAGS = ("#EXTM3U", "#EXT-X-VERSION", "#EXT-X-TARGETDURATION", "#EXT-X-INDEPENDENT-SEGMENTS")
# Ad markers and wall-clock tags are meaningless once older segments are cut.
DROPPED_TAGS = ("#EXT-X-DATERANGE", "#EXT-X-CUEPOINT", "#EXT-X-PROGRAM-DATE-TIME",
                "#EXT-X-DISCONTINUITY-SEQUENCE")
STICKY_TAGS = ("#EXT-X-MAP", "#EXT-X-KEY")
PLAYLIST_TYPE = "application/vnd.apple.mpegurl"
SEGMENT_TYPE = "video/mp2t"
DOWNLOAD_MEMORY = 40         # segments kept in memory per playlist (about 20 MB at 1080p)
PROBE_BYTES = 65536          # enough for the first PES headers or the ID3 tag
SEGMENT_MEMORY = 300         # window entries remembered per playlist
TS_PACKET = 188
ID3_TIMESTAMP_OWNER = b"com.apple.streaming.transportStreamTimestamp\x00"


# ---- first PTS of a segment -------------------------------------------------

def _pes_pts(raw: bytes) -> int:
    return (((raw[0] >> 1) & 0x07) << 30 | raw[1] << 22 | (raw[2] >> 1) << 15
            | raw[3] << 7 | raw[4] >> 1)


def _ts_first_pts(data: bytes):
    """Earliest of the first video PTS and the first audio PTS (muxed segments carry both)."""
    found = {}
    for offset in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
        packet = data[offset:offset + TS_PACKET]
        if packet[0] != 0x47 or not packet[1] & 0x40:     # sync byte, payload unit start
            continue
        control = (packet[3] >> 4) & 0x03
        start = 4
        if control & 0x02:                                # adaptation field present
            start += 1 + packet[4]
        if not control & 0x01 or start + 14 > TS_PACKET:
            continue
        pes = packet[start:]
        stream_id = pes[3]
        if pes[:3] == b"\x00\x00\x01" and (0xC0 <= stream_id <= 0xEF) and pes[7] & 0x80:
            kind = "video" if stream_id >= 0xE0 else "audio"
            found.setdefault(kind, _pes_pts(pes[9:14]) / 90000)
            if len(found) == 2:
                break
    return min(found.values()) if found else None


def _id3_first_pts(data: bytes):
    """HLS packed audio carries its PTS in an ID3 PRIV frame (RFC 8216 §3.4)."""
    size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
    version, position, end = data[3], 10, min(len(data), 10 + size)
    while position + 10 <= end:
        frame_id = data[position:position + 4]
        raw_size = data[position + 4:position + 8]
        if version >= 4:
            frame_size = ((raw_size[0] & 0x7F) << 21 | (raw_size[1] & 0x7F) << 14
                          | (raw_size[2] & 0x7F) << 7 | (raw_size[3] & 0x7F))
        else:
            frame_size = struct.unpack(">I", raw_size)[0]
        body = data[position + 10:position + 10 + frame_size]
        if frame_id == b"PRIV" and body.startswith(ID3_TIMESTAMP_OWNER):
            stamp = body[len(ID3_TIMESTAMP_OWNER):len(ID3_TIMESTAMP_OWNER) + 8]
            if len(stamp) == 8:
                return (struct.unpack(">Q", stamp)[0] & ((1 << 33) - 1)) / 90000
        if frame_size <= 0 or frame_id == b"\x00\x00\x00\x00":
            break
        position += 10 + frame_size
    return None


def first_pts(data: bytes):
    """First presentation time in seconds of an MPEG-TS or packed-audio segment, else None."""
    if data[:3] == b"ID3" and len(data) >= 10:
        return _id3_first_pts(data)
    if data[:1] == b"\x47":
        return _ts_first_pts(data)
    return None


# ---- playlists --------------------------------------------------------------

def segment_extension(url: str) -> str:
    """FFmpeg's HLS demuxer only accepts segment URLs with a media extension; keep the origin's."""
    tail = urlsplit(url).path.rsplit("/", 1)[-1]
    extension = tail.rsplit(".", 1)[-1].lower() if "." in tail else ""
    return f".{extension}" if extension.isalnum() and len(extension) <= 5 else ".ts"


def segment_duration(tags) -> float | None:
    for tag in tags:
        if tag.startswith("#EXTINF:"):
            try:
                return float(tag[8:].split(",", 1)[0])
            except ValueError:
                return None
    return None


def parse_media_playlist(text: str, base_url: str = ""):
    """(header tags, [(sequence, tags, absolute url, sticky tags)], ended)."""
    header, segments, pending, sticky = [], [], [], {}
    sequence, ended = 0, False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(DROPPED_TAGS):
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif line.startswith(HEADER_TAGS):
            header.append(line)
        elif line.startswith("#"):
            if line.startswith(STICKY_TAGS):
                sticky[line.split(":", 1)[0]] = line
            pending.append(line)
        else:
            segments.append((sequence + len(segments), pending, urljoin(base_url, line), dict(sticky)))
            pending = []
    if header[:1] != ["#EXTM3U"]:
        raise ValueError("HLS 재생목록 형식이 아닙니다.")
    return header, segments, ended


def render_playlist(header, segments, ended=False, segment_uri=None) -> str:
    """Media playlist text for consecutive ``(sequence, tags, url, sticky)`` entries."""
    lines = list(header) + [f"#EXT-X-MEDIA-SEQUENCE:{segments[0][0] if segments else 0}"]
    if segments:
        # Init segments and keys declared earlier still apply to the first listed segment.
        lines += [tag for tag in segments[0][3].values() if tag not in segments[0][1]]
    for sequence, tags, uri, _sticky in segments:
        lines += [*tags, segment_uri(sequence, uri, segment_duration(tags)) if segment_uri else uri]
    if ended:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def trim_playlist(text: str, base_url: str = "", keep: int = KEEP_SEGMENTS, segment_uri=None) -> str:
    """Return a media playlist holding only its last ``keep`` segments (all of a finished one).

    ``segment_uri(sequence, url, duration)`` may replace each kept segment's absolute URL.
    """
    header, segments, ended = parse_media_playlist(text, base_url)
    return render_playlist(header, segments if ended else segments[-keep:], ended, segment_uri)


def consecutive_tail(window: dict) -> list:
    """Entries of ``window`` (sequence -> entry) ending at the newest, stopping at the first gap."""
    tail = []
    for sequence in sorted(window, reverse=True):
        if tail and sequence != tail[-1][0] - 1:
            break
        tail.append(window[sequence])
    return tail[::-1]


class SegmentDownload:
    """One segment fetched from the origin, shared by every player request for it."""

    def __init__(self, kind, sequence):
        self.kind, self.sequence = kind, sequence
        self.data = bytearray()
        self.done = False
        self.error = None
        self.reply = None
        self.listeners = []            # sockets receiving it while it downloads
        self.probe = None              # clock generation waiting for this segment's first PTS


class HlsProxy(QObject):
    clock_ready = Signal(float)        # source PTS (seconds) at player position 0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._accept)
        self.network = QNetworkAccessManager(self)
        self.buffers = {}
        self.keep_alive = {}
        self.token = None
        self.master = b""
        self.sources = {}
        self.waiting = {}
        self.fetching = set()
        self.windows = {}              # "video.m3u8" -> {sequence: (sequence, tags, origin, sticky)}
        self.headers = {}              # "video.m3u8" -> header tags of the last load
        self.ended = {}
        self.fetched_at = {}           # "video.m3u8" -> monotonic time of the last upstream load
        self.downloads = {}            # (kind, sequence) -> SegmentDownload
        self.requested_at = 0.0        # last time the player asked for a playlist
        self.prefetching = True
        self.poller = QTimer(self)
        self.poller.setInterval(POLL_INTERVAL_MS)
        self.poller.timeout.connect(self._poll)
        self.clock = None
        self.reset_clock()

    def reset_clock(self):
        """Forget the start point; the next loads start near the live edge and define the clock."""
        self.clock_generation = getattr(self, "clock_generation", 0) + 1
        self.started = set()           # playlists already served since the reset
        self.probing = set()
        self.probed = {}
        self.anchors = {}              # kind -> (sequence, PTS of that segment's start)
        self.clock = None

    def serve(self, video_url, audio_url=None, width=0, height=0, bandwidth=0):
        """Publish a muxed playlist, or a video/audio pair; return the local master playlist URL."""
        if not self.server.isListening() and not self.server.listen(
                QHostAddress(QHostAddress.SpecialAddress.LocalHost), 0):
            logger.error("HLS proxy could not listen: %s", self.server.errorString())
            return None
        self.token = secrets.token_hex(8)
        self.sources = {"video.m3u8": video_url}
        if audio_url:
            self.sources["audio.m3u8"] = audio_url
        self.waiting, self.fetching = {}, set()
        self.windows = {name: {} for name in self.sources}
        self.headers, self.ended, self.fetched_at = {}, {}, {}
        self.downloads = {}
        self.requested_at = time.monotonic()
        self.poller.start()
        self.reset_clock()
        resolution = f",RESOLUTION={width}x{height}" if width and height else ""
        audio_group = (
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="audio",DEFAULT=YES,AUTOSELECT=YES,'
            'URI="audio.m3u8"\n') if audio_url else ""
        self.master = (
            "#EXTM3U\n#EXT-X-INDEPENDENT-SEGMENTS\n" + audio_group
            + f'#EXT-X-STREAM-INF:BANDWIDTH={int(bandwidth) or 5000000}{resolution}'
            + (',AUDIO="audio"' if audio_url else "") + "\nvideo.m3u8\n").encode()
        url = f"http://127.0.0.1:{self.server.serverPort()}/{self.token}/master.m3u8"
        logger.info("HLS proxy serving %s %sx%s at %s", "video+audio" if audio_url else "muxed",
                    width, height, url)
        return url

    # ---- HTTP ---------------------------------------------------------------

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self.buffers[socket] = b""
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(lambda s=socket: self._closed(s))
            self._read(socket)

    def _closed(self, socket):
        self.keep_alive.pop(socket, None)
        if self.buffers.pop(socket, None) is not None:
            socket.deleteLater()

    def _read(self, socket):
        if socket not in self.buffers:
            return
        self.buffers[socket] += bytes(socket.readAll())
        if b"\r\n\r\n" not in self.buffers[socket]:
            if len(self.buffers[socket]) > 16384:
                socket.abort()
            return
        head, _, self.buffers[socket] = self.buffers[socket].partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        request = lines[0].split()
        # FFmpeg reuses one connection for every segment; closing it costs a failed retry each time.
        connection = next((line.split(":", 1)[1].strip().lower() for line in lines[1:]
                           if line.lower().startswith("connection:")), "")
        self.keep_alive[socket] = (len(request) > 2 and request[2] == "HTTP/1.1"
                                   and connection != "close")
        if len(request) < 2 or request[0] != "GET":
            self._respond(socket, "405 Method Not Allowed")
            return
        token, _, name = request[1].split("?", 1)[0].lstrip("/").partition("/")
        if token != self.token:
            self._respond(socket, "404 Not Found")
        elif name == "master.m3u8":
            self._respond(socket, "200 OK", self.master)
        elif name in self.sources:
            self._playlist(socket, name)
        elif name.startswith("seg/"):
            self._segment(socket, name)
        else:
            self._respond(socket, "404 Not Found")

    def _respond(self, socket, status, body=b"", location=None, content_type=PLAYLIST_TYPE):
        if socket.state() != QAbstractSocket.SocketState.ConnectedState:
            return
        keep = self.keep_alive.get(socket, False)
        extra = f"Location: {location}\r\n" if location else ""
        head = (f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n{extra}"
                f"Content-Length: {len(body)}\r\nCache-Control: no-cache\r\n"
                f"Connection: {'keep-alive' if keep else 'close'}\r\n\r\n")
        socket.write(head.encode() + body)
        if not keep:
            socket.disconnectFromHost()
        elif b"\r\n\r\n" in self.buffers.get(socket, b""):
            self._read(socket)          # a pipelined request is already waiting

    # ---- playlists ------------------------------------------------------------

    def _playlist(self, socket, name):
        self.requested_at = time.monotonic()
        loaded = self.fetched_at.get(name)
        if loaded is not None and time.monotonic() - loaded < MAX_AGE:
            self._respond(socket, "200 OK", self._render(name))
            return
        self.waiting.setdefault(name, []).append(socket)
        self._fetch(name)

    def _poll(self):
        """Reload live playlists on our own while a player is using them.

        FFmpeg only reloads once it has played what it knows, which with a rolling window can
        be many seconds; a three-segment upstream list moves on in three, leaving a gap the
        player cannot cross (it skips ahead). Polling keeps the window gap-free.
        """
        if time.monotonic() - self.requested_at > POLL_IDLE_STOP:
            return
        for name in self.sources:
            loaded = self.fetched_at.get(name)
            if not self.ended.get(name) and (loaded is None or time.monotonic() - loaded >= MAX_AGE):
                self._fetch(name)

    def _fetch(self, name):
        if name in self.fetching:
            return
        self.fetching.add(name)
        token, url = self.token, self.sources[name]
        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(15000)
        # Qt asks for gzip and decompresses transparently; long DVR playlists need that.
        reply = self.network.get(request)
        reply.finished.connect(lambda: self._fetched(reply, token, name, url))

    def _fetched(self, reply, token, name, url):
        reply.deleteLater()
        if token != self.token:
            return
        self.fetching.discard(name)
        waiting = self.waiting.pop(name, [])
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                raise ValueError(reply.errorString())
            text = bytes(reply.readAll()).decode("utf-8", errors="replace")
            header, segments, ended = parse_media_playlist(text, url)
        except ValueError as error:
            # Background polls retry every second; only a failure a player is waiting on is news.
            (logger.warning if waiting else logger.debug)(
                "HLS proxy upstream failed for %s: %s", name, error)
            for socket in waiting:
                self._respond(socket, "502 Bad Gateway")
            return
        window = self.windows.setdefault(name, {})
        if ended:
            window.clear()
        newest = max(window) if window else None
        for entry in segments[-SEGMENT_MEMORY:]:
            window[entry[0]] = entry
        if not ended and self.prefetching:
            # Download each new segment the moment it is listed; on the first load only the ones
            # the player will start from.
            fresh = [entry[0] for entry in segments if newest is None or entry[0] > newest]
            self._prefetch(name, fresh[-START_SEGMENTS:] if newest is None else fresh)
        for sequence in sorted(window)[:-SEGMENT_MEMORY]:
            del window[sequence]
        self.headers[name], self.ended[name] = header, ended
        self.fetched_at[name] = time.monotonic()
        for socket in waiting:
            self._respond(socket, "200 OK", self._render(name))

    def _render(self, name):
        kind = name.split(".")[0]
        entries = consecutive_tail(self.windows.get(name, {}))
        if self.ended.get(name):
            keep = len(entries)
        elif name in self.started:
            keep = KEEP_SEGMENTS
        else:
            # First load since (re)opening: FFmpeg starts at the oldest listed segment here.
            keep = START_SEGMENTS
            self.started.add(name)
            if entries:
                logger.info("HLS proxy: %s starts at seq %d (%d newest of %d known)",
                            kind, entries[-min(keep, len(entries))][0], keep, len(entries))
        return render_playlist(
            self.headers.get(name, ["#EXTM3U"]), entries[-keep:], self.ended.get(name, False),
            lambda sequence, origin, _duration: f"seg/{kind}/{sequence}{segment_extension(origin)}").encode()

    # ---- segments and the clocks ------------------------------------------------

    def _segment(self, socket, name):
        """Serve a segment from the prefetch cache, streaming it while it still downloads."""
        _, kind, sequence = (name.split("/") + ["", ""])[:3]
        sequence = sequence.split(".", 1)[0]
        entry = self.windows.get(f"{kind}.m3u8", {}).get(int(sequence)) if sequence.isdigit() else None
        if entry is None:
            self._respond(socket, "404 Not Found")
            return
        fetch = self._download(kind, entry[0], entry[2])
        # Only segments listed after the (re)open define the clock; a request from a playlist the
        # player loaded before the reset would pin position 0 to the wrong segment.
        if self.clock is None and kind not in self.probing and f"{kind}.m3u8" in self.started:
            self.probing.add(kind)
            fetch.probe = self.clock_generation
            self._check_probe(fetch)
        if fetch.done and fetch.error is None:
            self._respond(socket, "200 OK", bytes(fetch.data), content_type=SEGMENT_TYPE)
        elif fetch.done:
            self._respond(socket, "502 Bad Gateway")
        else:
            self._stream_start(socket, fetch)

    def _download(self, kind, sequence, origin):
        """The cached (or newly started) download of one segment."""
        key = (kind, sequence)
        fetch = self.downloads.get(key)
        if fetch is not None:
            return fetch
        fetch = SegmentDownload(kind, sequence)
        self.downloads[key] = fetch
        request = QNetworkRequest(QUrl(origin))
        request.setTransferTimeout(15000)
        reply = self.network.get(request)
        fetch.reply = reply
        reply.readyRead.connect(lambda: self._download_data(fetch))
        reply.finished.connect(lambda: self._download_done(fetch))
        # Keep the newest segments of each kind; older ones are fetched again if ever asked for.
        mine = sorted(number for k, number in self.downloads if k == kind)
        for number in mine[:-DOWNLOAD_MEMORY]:
            old = self.downloads[(kind, number)]
            if old.done and not old.listeners:
                del self.downloads[(kind, number)]
        return fetch

    def _download_data(self, fetch):
        if fetch.reply is None or not fetch.reply.isOpen():
            return
        chunk = bytes(fetch.reply.readAll())
        if not chunk:
            return
        fetch.data.extend(chunk)
        for socket in list(fetch.listeners):
            self._stream_write(socket, fetch, chunk)
        self._check_probe(fetch)

    def _download_done(self, fetch):
        self._download_data(fetch)
        reply, fetch.reply = fetch.reply, None
        if reply.error() != QNetworkReply.NetworkError.NoError:
            fetch.error = reply.errorString()
            logger.warning("HLS proxy could not fetch %s seq %d: %s", fetch.kind, fetch.sequence, fetch.error)
            self.downloads.pop((fetch.kind, fetch.sequence), None)
        reply.deleteLater()
        fetch.done = True
        for socket in list(fetch.listeners):
            self._stream_end(socket, fetch)
        fetch.listeners.clear()
        self._check_probe(fetch)

    def _prefetch(self, name, sequences):
        """Start downloading newly listed segments right away, before the player asks."""
        kind = name.split(".")[0]
        window = self.windows.get(name, {})
        for sequence in sequences:
            if sequence in window:
                self._download(kind, sequence, window[sequence][2])

    # Streaming a segment that is still downloading: chunked transfer, then keep-alive.

    def _stream_start(self, socket, fetch):
        if socket.state() != QAbstractSocket.SocketState.ConnectedState:
            return
        keep = self.keep_alive.get(socket, False)
        socket.write((f"HTTP/1.1 200 OK\r\nContent-Type: {SEGMENT_TYPE}\r\nTransfer-Encoding: chunked\r\n"
                      f"Cache-Control: no-cache\r\nConnection: {'keep-alive' if keep else 'close'}\r\n\r\n").encode())
        fetch.listeners.append(socket)
        if fetch.data:
            self._stream_write(socket, fetch, bytes(fetch.data))

    def _stream_write(self, socket, fetch, chunk):
        if socket.state() != QAbstractSocket.SocketState.ConnectedState:
            if socket in fetch.listeners:
                fetch.listeners.remove(socket)
            return
        socket.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")

    def _stream_end(self, socket, fetch):
        if socket.state() != QAbstractSocket.SocketState.ConnectedState:
            return
        if fetch.error is not None:
            socket.abort()          # the player sees a broken segment and moves on
            return
        socket.write(b"0\r\n\r\n")
        if not self.keep_alive.get(socket, False):
            socket.disconnectFromHost()
        elif b"\r\n\r\n" in self.buffers.get(socket, b""):
            self._read(socket)

    def _check_probe(self, fetch):
        """Read the first PTS of a probed segment as soon as its head has arrived."""
        if fetch.probe is None or fetch.probe != self.clock_generation:
            return
        if len(fetch.data) < PROBE_BYTES and not fetch.done:
            return
        fetch.probe = None
        try:
            pts = first_pts(bytes(fetch.data[:PROBE_BYTES]))
        except (IndexError, struct.error):
            pts = None
        if pts is None:
            logger.warning("HLS proxy could not read the first %s PTS (seq %d, %d bytes)",
                           fetch.kind, fetch.sequence, len(fetch.data))
            self.probing.discard(fetch.kind)
            return
        logger.info("HLS proxy first %s segment seq %d starts at PTS %.3f", fetch.kind, fetch.sequence, pts)
        self.probed[fetch.kind] = pts
        self.anchors[fetch.kind] = (fetch.sequence, pts)
        if len(self.probed) == len(self.sources) and self.clock is None:
            # Qt's position 0 is the earliest timestamp across the streams it opened.
            self.clock = min(self.probed.values())
            logger.info("Playback clock: position 0 = source PTS %.3f", self.clock)
            self.clock_ready.emit(self.clock)

    def live_edge_pts(self):
        """Estimated source PTS of the newest moment the origin has published, or None.

        The probed segment's PTS plus the listed durations up to the newest segment gives the
        edge at the last playlist load; the live edge keeps moving in real time after that.
        """
        for kind in ("video", "audio"):
            name = f"{kind}.m3u8"
            window = self.windows.get(name) or {}
            if kind not in self.anchors or not window or name not in self.fetched_at:
                continue
            sequence, pts = self.anchors[kind]
            newest = max(window)
            if newest < sequence:
                continue
            lengths = {number: segment_duration(entry[1]) for number, entry in window.items()}
            known = [value for value in lengths.values() if value]
            typical = sum(known) / len(known) if known else 1.0
            listed = sum(lengths.get(number) or typical for number in range(sequence, newest + 1))
            return pts + listed + (time.monotonic() - self.fetched_at[name])
        return None
