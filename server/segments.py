"""Turn WhisperLive segment updates into caption events for the player.

WhisperLive re-sends the last N completed segments plus one in-progress
segment on every update. ``SegmentTracker`` keeps one caption id for the
in-progress text and hands that id to the segment that completes it, so the
player replaces the interim caption instead of stacking a duplicate.
Pure Python, no dependencies, so it is unit-tested on any machine.
"""
import re
from collections import deque

# Phrases Whisper tends to invent on silence, music or noise (Korean broadcasts).
HALLUCINATIONS = {
    "시청해주셔서감사합니다", "시청해주셔서고맙습니다", "구독과좋아요부탁드립니다",
    "구독과좋아요", "좋아요와구독부탁드립니다", "다음영상에서만나요", "감사합니다", "고맙습니다",
    "mbc뉴스이덕영입니다", "자막제공및자막편집", "한글자막by", "자막by",
    "thankyouforwatching", "thanksforwatching", "pleasesubscribe",
}
_PUNCT = re.compile(r"[\s\.,!?~…·'\"“”‘’()\[\]-]+")
_REPEAT = re.compile(r"(\S+)(?:\s+\1){3,}")


def normalize(text: str) -> str:
    return _PUNCT.sub("", text).lower()


# Spelling, not recognition: Whisper writes these acronyms the way they sound, while
# broadcasters caption them in Latin letters. Topic-independent only; keys are 3+ Hangul
# syllables so they never match inside an unrelated word, and longer keys apply first.
SPELLINGS = {
    "에이펙": "APEC", "지디피": "GDP", "아이엠에프": "IMF", "오이시디": "OECD", "에이아이": "AI",
    "씨이오": "CEO", "이에스지": "ESG", "비티에스": "BTS", "에스엔에스": "SNS", "피씨알": "PCR",
    "시시티비": "CCTV", "지피에스": "GPS", "오티티": "OTT", "엘엔지": "LNG", "에프티에이": "FTA",
    "아이시티": "ICT", "엠오유": "MOU", "디엔에이": "DNA", "지세븐": "G7", "지트웬티": "G20",
}
_SPELLING = re.compile("|".join(sorted(map(re.escape, SPELLINGS), key=len, reverse=True)))


def clean_text(text: str) -> str:
    """Collapse whitespace and runs of 4+ repeated words to two; apply SPELLINGS."""
    text = " ".join(text.split())
    text = _SPELLING.sub(lambda match: SPELLINGS[match.group()], text)
    return _REPEAT.sub(r"\1 \1", text)


def is_hallucination(text: str) -> bool:
    key = normalize(text)
    if not key:
        return True
    if key in HALLUCINATIONS:
        return True
    return any(key.startswith(prefix) for prefix in ("자막제공", "자막by", "한글자막"))


def to_ms(value) -> int:
    try:
        return max(0, round(float(value) * 1000))
    except (TypeError, ValueError):
        return 0


class SegmentTracker:
    SEEN_LIMIT = 500

    def __init__(self):
        self.seen = set()
        self.seen_order = deque()
        self.counter = 0
        self.live_id = None
        self.live_text = ""
        self.last_final = ""
        self.finals = []          # completed captions, for SRT/WebVTT export

    def _new_id(self):
        self.counter += 1
        return f"c{self.counter}"

    def _remember(self, key):
        self.seen.add(key)
        self.seen_order.append(key)
        if len(self.seen_order) > self.SEEN_LIMIT:
            self.seen.discard(self.seen_order.popleft())

    def _drop_live(self, events):
        if self.live_id is not None:
            events.append({"type": "caption", "id": self.live_id, "remove": True})
        self.live_id, self.live_text = None, ""

    def update(self, segments) -> list[dict]:
        events = []
        for segment in segments:
            if not segment.get("completed"):
                continue
            key = (segment.get("start"), segment.get("text"))
            if key in self.seen:
                continue
            self._remember(key)
            text = clean_text(segment.get("text") or "")
            if is_hallucination(text) or normalize(text) == normalize(self.last_final):
                continue
            caption_id = self.live_id or self._new_id()
            self.live_id, self.live_text = None, ""
            self.last_final = text
            event = {"type": "caption", "id": caption_id, "text": text, "final": True,
                     "speaker": segment.get("speaker"),
                     "stream_start": to_ms(segment.get("start")),
                     "stream_end": to_ms(segment.get("end"))}
            self.finals.append(event)
            events.append(event)
        last = segments[-1] if segments else None
        if last is not None and not last.get("completed"):
            text = clean_text(last.get("text") or "")
            if is_hallucination(text) or normalize(text) == normalize(self.last_final):
                if self.live_id is not None and not events:
                    self._drop_live(events)
            elif text != self.live_text:
                if self.live_id is None:
                    self.live_id = self._new_id()
                self.live_text = text
                events.append({"type": "caption", "id": self.live_id, "text": text,
                               "final": False, "speaker": last.get("speaker"),
                               "stream_start": to_ms(last.get("start")),
                               "stream_end": to_ms(last.get("end"))})
        return events


def _stamp(ms: int, sep: str) -> str:
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{sep}{millis:03}"


def _times(event):
    start = event["stream_start"]
    return start, max(event["stream_end"], start + 500)


def to_srt(finals) -> str:
    blocks = []
    for number, event in enumerate(finals, 1):
        start, end = _times(event)
        speaker = f"[{event['speaker']}] " if event.get("speaker") else ""
        blocks.append(f"{number}\n{_stamp(start, ',')} --> {_stamp(end, ',')}\n{speaker}{event['text']}\n")
    return "\n".join(blocks)


def to_webvtt(finals) -> str:
    lines = ["WEBVTT", ""]
    for event in finals:
        start, end = _times(event)
        text = event["text"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if event.get("speaker"):
            text = f"<v {event['speaker']}>{text}"
        lines += [f"{_stamp(start, '.')} --> {_stamp(end, '.')}", text, ""]
    return "\n".join(lines)
