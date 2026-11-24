"""Stream lookup, the FFmpeg command and the caption ↔ video timeline.

With video relay on, one FFmpeg process reads the stream once and writes two
outputs from the same input timeline:

    YouTube video + audio HLS ─▶ master.m3u8 ─▶ FFmpeg ─┬─▶ HLS for the browser (copy, no re-encode)
                                                        └─▶ 16 kHz mono PCM for WhisperLive

FFmpeg stamps each HLS segment with #EXT-X-PROGRAM-DATE-TIME, and hls.js
reports ``playingDate`` against the same labels. So a caption that starts
``t`` seconds into the PCM is on screen at ``epoch + t``, where ``epoch`` is
the label of the PCM's first sample (see ``timeline_epoch``).
"""
import logging
import os
import re
import shutil
from datetime import datetime
from urllib.parse import urlparse

logger = logging.getLogger("livecaption.media")
SAMPLE_RATE = 16000
HLS_SEGMENT_SECONDS = 2
HLS_LIST_SIZE = 30            # one minute of DVR window for the browser


def is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


# Muxed live variants to take the audio from, best first: 360p+ carry 128 kb/s AAC (144p/240p
# only 48 kb/s), and the picture costs under 1 Mb/s of extra download.
LIVE_AUDIO_QUALITIES = ("360p", "480p", "720p", "240p", "worst")


def pick_live_audio(streams: dict):
    """URL of the first HLS stream in LIVE_AUDIO_QUALITIES order, else None (e.g. a VOD)."""
    for quality in LIVE_AUDIO_QUALITIES:
        stream = streams.get(quality)
        url = getattr(stream, "url", None)
        if type(stream).__name__ == "HLSStream" and isinstance(url, str):
            return url
    return None


def resolve_live_audio(url: str):
    """Audio from YouTube's short muxed live playlist via streamlink, or None.

    That playlist lists new segments 2-3 s before the per-track DVR playlist yt-dlp returns
    (same segments, same PTS), so captions come out that much sooner.
    """
    from streamlink import Streamlink
    session = Streamlink()
    _name, plugin_class, resolved = session.resolve_url(url)
    plugin = plugin_class(session, resolved)
    details = capture_video_details(plugin)
    audio = pick_live_audio(plugin.streams())
    if audio is None:
        return None
    return {"video": None, "audio": audio, "headers": {}, "title": plugin.get_title() or url,
            "live": True, "bandwidth": 0, "author": plugin.get_author(),
            "category": plugin.get_category(), "description": details.get("shortDescription"),
            "tags": details.get("keywords") or []}


def capture_video_details(plugin) -> dict:
    """Keep the YouTube page's ``videoDetails`` (tags, description) that streamlink reads anyway.

    streamlink parses the page's initial player response but exposes only the title, author and
    category, so its parser is wrapped to see the same dict. No extra request; if streamlink
    changes shape, the dict stays empty and hotwords fall back to the title and channel.
    """
    captured = {}
    original = getattr(plugin, "_get_data_from_regex", None)
    if not callable(original):
        return captured

    def capture(res, regex, descr):
        data = original(res, regex, descr)
        if isinstance(data, dict) and isinstance(data.get("videoDetails"), dict):
            captured.update(data["videoDetails"])
        return data

    plugin._get_data_from_regex = capture
    return captured


def resolve_media(url: str, want_video: bool, max_height: int = 1080) -> dict:
    """Return {"video", "audio", "headers", "title", "live", "bandwidth"} for FFmpeg.

    ``video`` is None when not relaying video. ``audio`` is None when the video
    URL already carries the audio track (one muxed input). YouTube results also
    carry "author", "category", "description" and "tags", the hotword material.
    """
    if not is_youtube(url):
        return {"video": url if want_video else None, "audio": None if want_video else url,
                "headers": {}, "title": url, "live": None, "bandwidth": 0}
    if not want_video:
        try:
            live = resolve_live_audio(url)
        except Exception as error:      # streamlink missing or no plugin/stream: use yt-dlp
            logger.info("streamlink live audio unavailable (%s: %s); using yt-dlp",
                        type(error).__name__, error)
            live = None
        if live:
            logger.info("Live audio from streamlink's muxed playlist")
            return live
    import yt_dlp
    if want_video:
        # Live YouTube offers video-only and audio-only HLS; pair the best of each.
        fmt = (f"bestvideo[height<={max_height}][protocol^=m3u8]+bestaudio[protocol^=m3u8]"
               f"/best[height<={max_height}][protocol^=m3u8]/best")
    else:
        fmt = "bestaudio/best"
    options = {"quiet": True, "no_warnings": True, "noplaylist": True, "format": fmt}
    cookies = os.environ.get("YTDLP_COOKIES")
    if cookies:
        options["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    parts = info.get("requested_formats") or [info]
    # Live HLS formats often report acodec=None, so "the part that is not the video" is the audio.
    video = next((f for f in parts if f.get("vcodec") not in (None, "none")), None)
    audio = next((f for f in parts if f is not video), None)
    result = {"headers": info.get("http_headers") or {}, "title": info.get("title") or url,
              "live": info.get("is_live"),
              "bandwidth": round(sum((f.get("tbr") or 0) for f in parts) * 1000),
              "author": info.get("channel") or info.get("uploader"),
              "category": ", ".join(info.get("categories") or []) or None,
              "description": info.get("description"), "tags": info.get("tags") or []}
    if want_video:
        if video is None:
            raise ValueError("이 스트림에서 영상 트랙을 찾지 못했습니다.")
        result["video"] = video["url"]
        result["audio"] = audio["url"] if audio else None
    else:
        result["video"] = None
        result["audio"] = (audio or video or info)["url"]
    return result


def write_master_playlist(directory: str, video_url: str, audio_url: str, bandwidth: int) -> str:
    """One HLS input for separate video/audio renditions, so FFmpeg keeps them on one clock."""
    path = os.path.join(directory, "source.m3u8")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#EXTM3U\n#EXT-X-INDEPENDENT-SEGMENTS\n"
                     f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="audio",DEFAULT=YES,AUTOSELECT=YES,URI="{audio_url}"\n'
                     f'#EXT-X-STREAM-INF:BANDWIDTH={max(1, bandwidth)},AUDIO="aud"\n{video_url}\n')
    return path


def ffmpeg_binary() -> str:
    """System ffmpeg if present, else the static build shipped with imageio-ffmpeg."""
    found = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def ffmpeg_env() -> dict:
    """The static imageio ffmpeg cannot find Rocky Linux's CA store; point it at certifi."""
    env = os.environ.copy()
    if "SSL_CERT_FILE" not in env:
        try:
            import certifi
            env["SSL_CERT_FILE"] = certifi.where()
        except ImportError:
            pass
    return env


def ffmpeg_command(media: dict, hls_dir: str | None = None) -> list[str]:
    """Read the stream once; write HLS (when ``hls_dir``) and 16 kHz mono PCM to stdout.

    Audio-only runs keep the source timestamps (-copyts) and log the first audio frame's
    PTS (see ``parse_first_pts``): for YouTube that is the broadcast clock the viewer's
    own player also sees, so captions can be placed without relaying the video.
    """
    command = [ffmpeg_binary(), "-hide_banner", "-loglevel", "level+warning" if hls_dir else "level+info",
               "-nostdin"]
    if hls_dir and media.get("video") and media.get("audio"):
        source = write_master_playlist(hls_dir, media["video"], media["audio"], media.get("bandwidth", 0))
        command += ["-protocol_whitelist", "file,http,https,tcp,tls,crypto"]
    else:
        source = media["video"] if hls_dir else media["audio"]
        if source.startswith(("http://", "https://")):
            command += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                        # A stalled segment read (seen: a TLS connection hanging 60 s twice) gives
                        # up after 5 s so the HLS demuxer retries instead of starving the captions.
                        "-rw_timeout", "5000000"]
            if media.get("headers"):
                command += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in media["headers"].items())]
        elif source.startswith("rtmp://"):
            command += ["-rw_timeout", "10000000"]
    command += ["-i", source]
    if hls_dir:
        command += ["-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-f", "hls",
                    "-hls_time", str(HLS_SEGMENT_SECONDS), "-hls_list_size", str(HLS_LIST_SIZE),
                    "-hls_flags", "delete_segments+program_date_time+independent_segments+omit_endlist",
                    "-hls_segment_filename", os.path.join(hls_dir, "seg_%05d.ts"),
                    os.path.join(hls_dir, "index.m3u8")]
        command += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"]
        return command
    command[command.index("-i"):command.index("-i")] = ["-copyts"]
    # The PCM branch is restamped from 0 so the s16le muxer never sees -copyts's jumpy DTS;
    # the probe branch keeps the source PTS and logs only frame 0 (comma escaped for the parser).
    command += ["-filter_complex", r"[0:a:0]asplit[pcm0][probe];[pcm0]asetpts=N/SR/TB[pcm];[probe]aselect=eq(n\,0),ashowinfo,anullsink",
                "-map", "[pcm]", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"]
    return command


_FIRST_PTS = re.compile(r"Parsed_ashowinfo.*\bn:0\b.*\bpts_time:(-?[\d.]+)")


def parse_first_pts(line: str):
    """PTS in seconds from FFmpeg's ashowinfo line for the first audio frame, else None."""
    match = _FIRST_PTS.search(line)
    return float(match.group(1)) if match else None


_PDT = re.compile(r"#EXT-X-PROGRAM-DATE-TIME:(\S+)")


def parse_program_date_time(value: str) -> float:
    """'2026-11-17T23:20:01.123+0000' -> epoch milliseconds."""
    value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value.strip()).replace("Z", "+00:00")
    return datetime.fromisoformat(value).timestamp() * 1000


def first_segment(playlist: str):
    """(program date time in ms, segment file name) of the first entry, or None."""
    pdt = None
    for line in playlist.splitlines():
        line = line.strip()
        match = _PDT.match(line)
        if match:
            pdt = parse_program_date_time(match.group(1))
        elif line and not line.startswith("#"):
            return (pdt, line) if pdt is not None else None
    return None


def first_timestamps(segment_path: str) -> dict:
    """First PTS in seconds of each stream type in an MPEG-TS segment."""
    import av
    found = {}
    with av.open(segment_path) as container:
        for packet in container.demux():
            kind = packet.stream.type
            if packet.pts is not None and kind in ("video", "audio") and kind not in found:
                found[kind] = float(packet.pts * packet.time_base)
            if len(found) == 2:
                break
    return found


def timeline_epoch(segment_pdt_ms: float, first: dict) -> float:
    """Program-date-time label of the PCM's first sample.

    FFmpeg's HLS muxer and hls.js both pin a segment's date to its earliest
    packet, while the PCM starts at the first audio packet.
    """
    earliest = min(first.values())
    return segment_pdt_ms + (first["audio"] - earliest) * 1000
