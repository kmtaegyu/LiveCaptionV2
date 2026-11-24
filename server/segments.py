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


def replacement_pattern(replacements: dict):
    """One regex over all keys, longest first, so a long key wins over a key inside it."""
    if not replacements:
        return None
    return re.compile("|".join(sorted(map(re.escape, replacements), key=len, reverse=True)))


_SPELLING = replacement_pattern(SPELLINGS)


def clean_text(text: str, replacements: dict | None = None, pattern=None) -> str:
    """Collapse whitespace and runs of 4+ repeated words to two; apply SPELLINGS
    (or ``replacements``, e.g. SPELLINGS plus a topic dictionary, with its ``pattern``)."""
    text = " ".join(text.split())
    if replacements is None:
        replacements, pattern = SPELLINGS, _SPELLING
    elif pattern is None:
        pattern = replacement_pattern(replacements)
    if pattern is not None:
        text = pattern.sub(lambda match: replacements[match.group()], text)
    return _REPEAT.sub(r"\1 \1", text)


def dictionary_marks(raw: str, text: str, replacements: dict, pattern, terms_pattern) -> list:
    """Dictionary words in ``text`` for the player to colour: [[word, kind], ...].

    kind "fixed" = a rule rewrote the recognised text into this word (``raw`` held a variant);
    kind "term"  = a topic dictionary term that was recognised as it is.
    """
    marks, seen = [], set()
    if pattern is not None:
        for match in pattern.finditer(" ".join(raw.split())):
            word = replacements[match.group()]
            if word not in seen and word in text:
                seen.add(word)
                marks.append([word, "fixed"])
    if terms_pattern is not None:
        for match in terms_pattern.finditer(text):
            if match.group() not in seen:
                seen.add(match.group())
                marks.append([match.group(), "term"])
    return marks


def prompt_echo_pattern(hotwords):
    """Regex for the model reciting its hotword prompt: 3+ hotwords in a row, comma-separated.

    Seen live (2026-11-24, a parliamentary hearing): over pauses Whisper wrote the prompt itself,
    "KBS사장, 후보자, 인사청문준비단, 배경훈, 무제한 토론 종결." Speech does not come out as a
    comma list of exactly those words, so such a run is removed from the caption.
    """
    words = sorted({word.strip() for word in hotwords or () if word and word.strip()}, key=len, reverse=True)
    if len(words) < 3:
        return None
    one = "(?:" + "|".join(map(re.escape, words)) + ")"
    return re.compile(rf"(?:{one}\s*,\s*){{2,}}(?:{one}[.,]?)?")


# A word with a letter that is neither Hangul nor basic Latin (or U+FFFD, a broken byte):
# decoding noise such as "납tem,ն" or "��iß이란".
_FOREIGN_WORD = re.compile(r"\S*[^\W\d_a-zA-Z\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]\S*|\S*\ufffd\S*")
_HANGUL_CHAR = re.compile(r"[\uac00-\ud7a3]")


_LATIN_WORD = re.compile(r"(?<![A-Za-z])[a-z]{3,}(?![A-Za-z])")


def strip_noise(text: str, echo=None) -> str:
    """Remove a recited hotword prompt; "" for decoding noise or when little real text is left.

    A caption with broken or foreign-script letters, or two or more lower-case foreign words
    ("continu하면은 habits arg wine europe"), is a garbage decode: dropping one caption reads
    better than showing it.
    """
    if _FOREIGN_WORD.search(text) or len(_LATIN_WORD.findall(text)) >= 2:
        return ""
    if echo is None:
        return text
    cleaned = echo.sub(" ", text)
    if cleaned == text:
        return text
    cleaned = re.sub(r"\s+([,.])", r"\1", " ".join(cleaned.split())).strip(" ,")
    # What is left around a recited prompt is usually more of the same list.
    if cleaned.count(",") >= 3 or len(_HANGUL_CHAR.findall(cleaned)) < 4:
        return ""
    return cleaned


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

    def __init__(self, replacements: dict | None = None, terms=()):
        # Topic dictionary terms, reported in each caption's "marks" when they come up.
        self.terms_pattern = replacement_pattern({term: term for term in terms})
        # SPELLINGS plus any topic dictionary's variant -> term rules; topic rules win on a tie.
        self.replacements = {**SPELLINGS, **(replacements or {})}
        self.pattern = replacement_pattern(self.replacements)
        self.echo = None             # set_prompt(): the hotwords sent to the model, if any
        self.seen = set()
        self.seen_order = deque()
        self.counter = 0
        self.live_id = None
        self.live_text = ""
        self.last_final = ""
        self.finals = []          # completed captions, for SRT/WebVTT export

    def set_prompt(self, hotwords):
        """Tell the tracker which hotwords the model was given, so a recited prompt is removed."""
        if isinstance(hotwords, str):
            hotwords = hotwords.rstrip(".").split(",")
        self.echo = prompt_echo_pattern(hotwords)

    def _clean(self, raw: str) -> str:
        return strip_noise(clean_text(raw, self.replacements, self.pattern), self.echo)

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

    def _marked(self, event, raw):
        marks = dictionary_marks(raw, event["text"], self.replacements, self.pattern, self.terms_pattern)
        if marks:
            event["marks"] = marks
        raw = " ".join(raw.split())
        if raw != event["text"]:
            event["raw"] = raw          # what the model wrote, before the dictionary (for review logs)
        return event

    def update(self, segments) -> list[dict]:
        events = []
        for segment in segments:
            if not segment.get("completed"):
                continue
            key = (segment.get("start"), segment.get("text"))
            if key in self.seen:
                continue
            self._remember(key)
            text = self._clean(segment.get("text") or "")
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
            events.append(self._marked(event, segment.get("text") or ""))
        last = segments[-1] if segments else None
        if last is not None and not last.get("completed"):
            text = self._clean(last.get("text") or "")
            if is_hallucination(text) or normalize(text) == normalize(self.last_final):
                if self.live_id is not None and not events:
                    self._drop_live(events)
            elif text != self.live_text:
                if self.live_id is None:
                    self.live_id = self._new_id()
                self.live_text = text
                events.append(self._marked(
                    {"type": "caption", "id": self.live_id, "text": text,
                     "final": False, "speaker": last.get("speaker"),
                     "stream_start": to_ms(last.get("start")),
                     "stream_end": to_ms(last.get("end"))}, last.get("text") or ""))
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
