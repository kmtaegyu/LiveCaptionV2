"""SRT parsing, live caption track and time-based lookup, independent of the UI."""
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
import re


TIMING = re.compile(
    r"^(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})\s*-->\s*"
    r"(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})$"
)


@dataclass(frozen=True)
class Cue:
    start: int
    end: int
    text: str
    id: str | None = None
    speaker: str | None = None
    final: bool = True
    carry: str = ""          # unfinished end of the previous sentence, shown in front of ``text``

    @property
    def display(self) -> str:
        return f"{self.carry} {self.text}" if self.carry else self.text


def parse_srt(text: str) -> list[Cue]:
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").strip()
    cues = []
    for number, block in enumerate(re.split(r"\n[ \t]*\n", text), 1):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        match = TIMING.fullmatch(lines[0].strip()) if lines else None
        if not match or len(lines) < 2:
            raise ValueError(f"SRT {number}번째 구간의 시간 또는 내용이 올바르지 않습니다.")
        values = list(map(int, match.groups()))
        start, end = [((h * 60 + m) * 60 + s) * 1000 + ms
                      for h, m, s, ms in (values[:4], values[4:])]
        if end <= start:
            raise ValueError(f"SRT {number}번째 구간의 종료 시간은 시작 시간보다 커야 합니다.")
        cues.append(Cue(start, end, "\n".join(lines[1:])))
    return sorted(cues, key=lambda cue: cue.start)


def load_srt(path: str) -> list[Cue]:
    data = Path(path).read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return parse_srt(data.decode("utf-16"))
    for encoding in ("utf-8-sig", "cp949"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return parse_srt(text)
    raise ValueError("자막 인코딩을 읽을 수 없습니다. UTF-8로 저장해 주세요.")


def active_index(cues: list[Cue], position: int) -> int | None:
    """Index of the active cue that started most recently (latest wins on ties)."""
    best = None
    for index, cue in enumerate(cues):
        if cue.start <= position < cue.end and (best is None or cue.start >= cues[best].start):
            best = index
    return best


def subtitle_at(cues: list[Cue], position: int) -> str:
    index = active_index(cues, position)
    return "" if index is None else " ".join(cues[index].display.split())


# Streaming recognition finalises a caption wherever Whisper put a segment boundary, often
# mid-phrase ("수출과 투자" / "중심으로 ..."). A caption whose predecessor did not end its
# sentence shows that predecessor's last words in front, so the phrase reads as one block.
_SENTENCE_END = re.compile(r"(?:[.?!…。]|[다요죠까네](?:[.?!…]*))[\"'”’)\]]*$")
CARRY_CHARS = 24             # at most about half a line of carried words
CARRY_GAP_MS = 1500          # only across a short pause; a long one ends the thought anyway


def ends_sentence(text: str) -> bool:
    return bool(_SENTENCE_END.search(text.strip()))


def carry_tail(text: str, limit: int = CARRY_CHARS) -> str:
    """Last whole words of ``text`` that fit in ``limit`` characters."""
    words, size = [], 0
    for word in reversed(text.split()):
        size += len(word) + (1 if words else 0)
        if size > limit and words:
            break
        words.insert(0, word)
        if size > limit:
            break
    return " ".join(words)


def live_duration(text: str) -> int:
    """Display time for a caption that arrives without an end time."""
    return max(2000, min(8000, 1200 + 150 * len(text)))


class CaptionTrack:
    """Cues on the media timeline plus one user-controlled sync offset.

    A cue is shown while ``start <= position - offset < end``: a positive offset
    shows captions later, a negative one earlier. File cues and live cues share
    the same list, so the same sync control works for both.
    """

    MAX_CUES = 2000
    ID_LOOKBACK = 50

    def __init__(self, join_sentences: bool = True):
        self.cues: list[Cue] = []
        self.offset = 0
        self.join_sentences = join_sentences

    def set_cues(self, cues):
        self.cues = list(cues)

    def clear(self):
        self.cues = []

    def active_index(self, position: int) -> int | None:
        return active_index(self.cues, position - self.offset)

    def text_at(self, position: int) -> str:
        return subtitle_at(self.cues, position - self.offset)

    def find(self, cue_id):
        oldest = max(-1, len(self.cues) - self.ID_LOOKBACK - 1)
        for index in range(len(self.cues) - 1, oldest, -1):
            if self.cues[index].id == cue_id:
                return index
        return None

    def remove(self, cue_id):
        """Drop a live cue (an interim result the engine withdrew); returns its index."""
        index = self.find(cue_id) if cue_id is not None else None
        if index is not None:
            del self.cues[index]
        return index

    def push(self, text, position, start=None, end=None, duration=None, cue_id=None,
             speaker=None, final=True):
        """Add a live cue stamped at ``position`` unless explicit times are given.

        A repeated ``cue_id`` replaces the text of that cue (interim results).
        Returns ``(index, replaced, trimmed)``.
        """
        text = text.strip()
        if duration is None:
            duration = live_duration(text)
        index = self.find(cue_id) if cue_id is not None else None
        if index is not None:
            old = self.cues[index]
            # An explicit start (auto sync) may refine the cue; arrival-stamped cues keep theirs.
            new_start = old.start if start is None else start
            new_end = end if end is not None else max(old.end, position + duration)
            cue = Cue(new_start, max(new_end, new_start + 1), text, cue_id,
                      speaker or old.speaker, final)
            self.cues[index] = self._with_carry(index, cue)
            return index, True, 0
        if start is None:
            start = position
        if end is None or end <= start:
            end = start + duration
        trimmed = 0
        if len(self.cues) >= self.MAX_CUES:
            del self.cues[0]
            trimmed = 1
        index = bisect_right(self.cues, start, key=lambda cue: cue.start)
        self.cues.insert(index, self._with_carry(index, Cue(start, end, text, cue_id, speaker, final)))
        return index, False, trimmed

    def _with_carry(self, index, cue):
        """``cue`` with the unfinished end of the caption just before it (live captions only)."""
        if not self.join_sentences or cue.id is None or index == 0:
            return cue
        previous = self.cues[index - 1]
        if (previous.id is None or not previous.final or previous.speaker != cue.speaker
                or cue.start - previous.end > CARRY_GAP_MS or ends_sentence(previous.text)):
            return cue
        carry = carry_tail(previous.text)
        return Cue(cue.start, cue.end, cue.text, cue.id, cue.speaker, cue.final, carry) if carry else cue
