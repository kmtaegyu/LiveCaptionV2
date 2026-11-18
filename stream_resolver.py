"""Resolve YouTube pages without blocking the Qt event loop.

Live streams go through streamlink first: it returns YouTube's short muxed HLS
playlist, which lists new segments 2-3 s before the per-track DVR playlists
yt-dlp returns (same segments, same PTS clock). Anything streamlink cannot play
as HLS (VODs, other errors) falls back to yt-dlp.
"""
import shutil
import sys
import logging
import time
import json

from PySide6.QtCore import QObject, QProcess, QTimer, QUrl, Signal

from hls_proxy import HlsProxy


logger = logging.getLogger("livecaption.resolver")
FORMAT_SELECTION = "best[protocol^=m3u8]/bestvideo[protocol^=m3u8]+bestaudio[protocol^=m3u8]/best"


def is_youtube_url(url: QUrl) -> bool:
    host = url.host().lower()
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def parse_stream_url(output: str) -> str:
    if not isinstance(output, str):
        raise ValueError("스트림 주소가 비어 있거나 올바르지 않습니다.")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("영상과 음성이 함께 있는 단일 스트림을 찾지 못했습니다.")
    url = QUrl(lines[0])
    if not url.isValid() or url.scheme() not in {"https", "http"} or not url.host():
        raise ValueError("재생 가능한 HTTP(S) 스트림 주소를 찾지 못했습니다.")
    return lines[0]


def select_playback_url(info: dict) -> str:
    if not isinstance(info, dict):
        raise ValueError("yt-dlp가 올바른 영상 정보를 반환하지 않았습니다.")
    formats = info.get("requested_formats")
    if formats:
        if not isinstance(formats, list) or not all(isinstance(f, dict) for f in formats):
            raise ValueError("영상·음성 형식 정보를 읽을 수 없습니다.")
        if not all(isinstance(f.get("manifest_url"), str) for f in formats):
            raise ValueError("분리된 영상·음성의 HLS 마스터 주소가 없습니다.")
        masters = {f["manifest_url"] for f in formats}
        if (len(masters) == 1 and None not in masters
                and all(str(f.get("protocol", "")).startswith("m3u8") for f in formats)):
            logger.info("Using shared HLS master for %d separate video/audio tracks", len(formats))
            return parse_stream_url(masters.pop())
        raise ValueError("분리된 영상·음성에 공통 HLS 마스터가 없어 재생할 수 없습니다.")
    if info.get("vcodec") == "none" or info.get("acodec") == "none":
        raise ValueError("영상 또는 음성만 있는 스트림입니다.")
    return parse_stream_url(info.get("url", ""))


def parse_streamlink_output(output: str):
    """(playlist URL, YouTube video id or None) from ``streamlink --json``; None if not live HLS."""
    try:
        data = json.loads(output)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") != "hls" or not isinstance(data.get("url"), str):
        return None
    try:
        url = parse_stream_url(data["url"])
    except ValueError:
        return None
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    video_id = metadata.get("id") if isinstance(metadata.get("id"), str) else None
    return url, video_id


def select_live_tracks(info: dict):
    """Return (video, audio) formats when a live stream has separate HLS playlists."""
    formats = info.get("requested_formats") or []
    video = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") == "none"]
    audio = [f for f in formats if f.get("acodec") != "none" and f.get("vcodec") == "none"]
    if len(formats) != 2 or len(video) != 1 or len(audio) != 1:
        return None
    for track in formats:
        if not str(track.get("protocol", "")).startswith("m3u8"):
            return None
        try:
            parse_stream_url(track.get("url", ""))
        except ValueError:
            return None
    return video[0], audio[0]


class StreamResolver(QObject):
    resolved = Signal(str)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = None
        self.is_live = False
        self.video_url = None   # canonical watch URL of the video actually resolved
        self.proxy = HlsProxy(self)

    def resolve(self, url: QUrl):
        self.cancel()
        self.is_live = False
        self.video_url = None
        self.started_at = time.monotonic()
        logger.info("Resolving YouTube page: %s", url.toString())
        self._resolve_live(url)

    def _resolve_live(self, url: QUrl):
        """Ask streamlink for the low-latency muxed live playlist; fall back to yt-dlp."""
        process = QProcess(self)
        self.process = process
        timer = QTimer(process)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._streamlink_failed(process, url, "timed out"))
        process.finished.connect(lambda code, _status: self._streamlink_finished(process, url, code))
        process.finished.connect(timer.stop)
        process.finished.connect(process.deleteLater)
        process.errorOccurred.connect(
            lambda error: error == QProcess.ProcessError.FailedToStart
            and self._streamlink_failed(process, url, "could not start"))
        process.setProgram(sys.executable)
        process.setArguments(["-m", "streamlink_cli", "--json", "--no-config", "--http-timeout", "15",
                              "--", url.toString(), "best"])
        process.start()
        timer.start(30000)

    def _streamlink_failed(self, process, url, reason):
        if self.process is not process:
            return
        logger.info("streamlink gave no live HLS (%s); falling back to yt-dlp", reason)
        self.process = None
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill()
        self._resolve_ytdlp(url)

    def _streamlink_finished(self, process, url, code):
        if self.process is not process:
            return
        output = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        result = parse_streamlink_output(output) if code == 0 else None
        if result is None:
            detail = output.strip().replace("\n", " ")[:300]
            self._streamlink_failed(process, url, f"exit={code} {detail}")
            return
        self.process = None
        stream, video_id = result
        logger.info("streamlink resolved a live muxed playlist in %.2fs (video %s)",
                    time.monotonic() - self.started_at, video_id)
        self.is_live = True
        # A channel /live URL can point at a different broadcast on every lookup; pin this one.
        self.video_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else url.toString()
        local = self.proxy.serve(stream)
        logger.info("Resolved playback stream: %s", local or stream)
        self.resolved.emit(local or stream)

    def _resolve_ytdlp(self, url: QUrl):
        process = QProcess(self)
        self.process = process
        timer = QTimer(process)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._timeout(process))
        diagnostics = []
        process.readyReadStandardError.connect(lambda: self._read_diagnostics(process, diagnostics))
        process.finished.connect(lambda code, _status: self._finished(process, code, diagnostics))
        process.finished.connect(timer.stop)
        process.finished.connect(process.deleteLater)
        process.errorOccurred.connect(lambda error: self._error(process, error))
        args = ["-m", "yt_dlp", "--verbose", "--ignore-config", "--no-playlist", "--no-progress",
                "--socket-timeout", "15", "--retries", "1", "--extractor-retries", "1",
                "--format", FORMAT_SELECTION, "--dump-single-json", "--skip-download", "--encoding", "utf-8"]
        # Deno is enabled by yt-dlp by default; explicitly enable installed Node too.
        if shutil.which("node"):
            args.extend(["--js-runtimes", "node"])
        logger.debug("Extractor interpreter=%s; Node=%s; Deno=%s", sys.executable,
                     shutil.which("node"), shutil.which("deno"))
        process.setProgram(sys.executable)
        process.setArguments([*args, "--", url.toString()])
        process.start()
        timer.start(60000)

    def cancel(self):
        process, self.process = self.process, None
        if process is not None:
            logger.info("Cancelling YouTube resolution")
            process.kill()
            # Ensure child processes are reaped before window destruction.
            process.waitForFinished(1000)

    def _timeout(self, process):
        if self.process is process:
            logger.error("YouTube resolution timed out after 60 seconds")
            self.cancel()
            self.failed.emit("YouTube 주소 확인 시간이 초과되었습니다. 다시 시도해 주세요.")

    def _error(self, process, error):
        logger.debug("Extractor process event: %s", error)
        if self.process is process and error == QProcess.ProcessError.FailedToStart:
            self.process = None
            self.failed.emit("yt-dlp를 시작하지 못했습니다. 실행 환경을 확인해 주세요.")
            process.deleteLater()

    def _read_diagnostics(self, process, diagnostics):
        text = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
        if text:
            diagnostics.append(text[-4096:])
            del diagnostics[:-32]
            logger.debug("yt-dlp: %s", text.strip())

    def _finished(self, process, code, diagnostics):
        if self.process is not process:
            return
        self.process = None
        self._read_diagnostics(process, diagnostics)
        logger.info("Extractor finished: exit=%d elapsed=%.2fs", code, time.monotonic() - self.started_at)
        if code != 0:
            error = "".join(diagnostics)
            errors = [line for line in error.splitlines() if "ERROR" in line]
            detail = (errors[-1] if errors else error.strip())[-1200:]
            self.failed.emit(f"YouTube 연결 실패: {detail or '스트림을 찾지 못했습니다.'}")
            return
        try:
            info = json.loads(bytes(process.readAllStandardOutput()).decode("utf-8"))
            stream = select_playback_url(info)
            self.is_live = info.get("is_live") is True or info.get("live_status") == "is_live"
            # A channel /live URL can point at a different broadcast on every lookup; pin the one
            # we play so the caption server transcribes the same video.
            if info.get("id") and info.get("extractor_key", "").lower().startswith("youtube"):
                self.video_url = f"https://www.youtube.com/watch?v={info['id']}"
            else:
                self.video_url = info.get("webpage_url")
            tracks = select_live_tracks(info) if self.is_live else None
            if tracks:
                # Long live playlists starve FFmpeg; play one variant through the local proxy.
                video, audio = tracks
                stream = self.proxy.serve(
                    video["url"], audio["url"], video.get("width") or 0, video.get("height") or 0,
                    (video.get("tbr") or 0) * 1000) or stream
        except ValueError as error:
            logger.error("Invalid extractor output: %s", error)
            self.failed.emit(str(error))
        else:
            logger.info("Resolved playback stream: %s", stream)
            self.resolved.emit(stream)
