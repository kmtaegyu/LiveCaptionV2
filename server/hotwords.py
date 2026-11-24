"""Hotwords for one broadcast, built from what the stream says about itself.

Whisper is nudged toward rare words (names, products, acronyms) when they sit
in its prompt. Instead of a topic dictionary prepared in advance, the words
come from several sources, in this order: words the presenter typed, the
broadcast's title, tags and channel name, the chosen topic dictionary's
hotword terms (topics.py), and description words that come up at least twice
(the description is mostly sentences and links).

The list stays short (MAX_WORDS, at most about MAX_TOKENS prompt tokens):
faster-whisper keeps only the last half of its 448-token prompt for hotwords,
and a long list makes the model invent those words over silence.
Pure Python, unit-tested.
"""
import re
from collections import Counter

MAX_WORDS = 40
MAX_TOKENS = 200              # under faster-whisper's 223-token hotword window
TOPIC_RESERVE = 70            # tokens kept free for a chosen topic dictionary's terms
DESCRIPTION_CHARS = 1000      # the first lines say what the stream is; the rest is boilerplate

# Words every broadcast carries; they only cost prompt space.
STOPWORDS = {
    "live", "라이브", "생중계", "생방송", "실시간", "방송", "뉴스", "news", "full", "official",
    "youtube", "유튜브", "채널", "channel", "구독", "구독하기", "좋아요", "알림", "알림설정",
    "제보", "문의", "subscribe", "shorts", "the", "and", "for", "with", "현장", "다시보기",
    "오늘", "지금", "속보", "단독", "풀영상", "하이라이트", "hd", "4k", "tv", "관련", "주요",
    "가운데", "이후", "최근", "모두", "여러분", "우리", "그리고", "하지만", "이번", "지난",
}
# Particles cut from title/description words ("이란은" -> "이란"); longest first.
PARTICLES = sorted(["은", "는", "이", "가", "을", "를", "의", "에", "에서", "에게", "도", "로", "으로",
                    "과", "와", "까지", "부터", "만", "이나", "에는", "으로는", "께서"], key=len, reverse=True)
_URL = re.compile(r"(?:https?://|www\.)\S+|\S+@\S+")
_ELLIPSIS = re.compile(r"\.{2,}|…")
# Split on anything that is not a letter, digit or an in-word joiner (A.I, C++, K-pop).
_SPLIT = re.compile(r"[^\w.+&'-]+")
_EDGE = re.compile(r"^[.+&'_-]+|[.&'_-]+$")
_HANGUL = re.compile(r"[가-힣]")
_HANGUL_WORD = re.compile(r"^[가-힣]+$")
# Predicate endings ("움직였다", "있습니다", "할까요"): sentences, not names.
_PREDICATE = re.compile(r"(다|요|죠|니까|는데|지만|면서|며|던|될|겠)$")


def estimate_tokens(word: str) -> int:
    """Upper bound on Whisper tokens for ``word``: Hangul syllables cost up to 2, ASCII about 1/3."""
    hangul = len(_HANGUL.findall(word))
    other = len(word) - hangul
    return 1 + 2 * hangul + (other + 2) // 3


def raw_words(text) -> list[str]:
    if not text:
        return []
    if isinstance(text, (list, tuple)):
        return [word for item in text for word in raw_words(item)]
    text = _ELLIPSIS.sub(" ", _URL.sub(" ", str(text)).replace("#", " "))
    return [word for word in (_EDGE.sub("", raw) for raw in _SPLIT.split(text)) if word]


def strip_particle(word: str, vocabulary) -> str:
    """'이란은' -> '이란' only when '이란' also occurs on its own or with another particle
    ('이란도'), so '국무회의' and '전문가' stay whole."""
    if not _HANGUL_WORD.match(word):
        return word
    for particle in PARTICLES:
        stem = word[:-len(particle)]
        if not word.endswith(particle) or len(stem) < 2:
            continue
        if stem in vocabulary or any(stem + other in vocabulary for other in PARTICLES
                                     if other != particle):
            return stem
    return word


def words_of(text, prose: bool = False, vocabulary=()) -> list[str]:
    """Candidate words of a title, tag, channel name or description, in order.

    ``prose`` text (title, description) also drops predicates and loses particles
    whose stem is in ``vocabulary``.
    """
    found = []
    for word in raw_words(text):
        if prose and _HANGUL_WORD.match(word):
            if len(word) >= 3 and _PREDICATE.search(word):
                continue
            word = strip_particle(word, vocabulary)
        if len(word) < 2 or word[0].isdigit() or word.lower() in STOPWORDS:
            continue
        found.append(word)
    return found


def keyword_list(text) -> list[str]:
    """Words the presenter typed: separated by commas, spaces or new lines, kept as typed."""
    if not text:
        return []
    if isinstance(text, (list, tuple)):
        text = ",".join(str(item) for item in text)
    return [word for word in re.split(r"[,\s;]+", str(text)) if len(word) >= 2]


def build_hotwords(media: dict | None, keywords=None, topic_terms=None,
                   max_words: int = MAX_WORDS, max_tokens: int = MAX_TOKENS) -> str | None:
    """One hotword string for WhisperLive ("A, B, C."), or None when nothing is left.

    Commas and the final period matter: Whisper copies the prompt's style, and a bare
    space-separated list made it drop punctuation (3-minute KTV recording: 13 periods without
    hotwords, 3 with a bare list, 25 with this form).
    """
    media = media or {}
    description_text = str(media.get("description") or "")[:DESCRIPTION_CHARS]
    vocabulary = set(raw_words([media.get("title"), media.get("tags"), description_text]))
    title = words_of(media.get("title"), True, vocabulary)
    description = words_of(description_text, True, vocabulary)
    counts = Counter(word.lower() for word in title + description)
    repeated = [word for word in description if counts[word.lower()] >= 2]
    topic_terms = list(topic_terms or [])
    # (words, tokens that must stay free afterwards): a broadcast with many tags would otherwise
    # leave no room for the topic dictionary the presenter chose.
    reserve = min(TOPIC_RESERVE, sum(estimate_tokens(word) + 1 for word in topic_terms))
    sources = [(keyword_list(keywords), 0), (title, reserve), (words_of(media.get("tags")), reserve),
               (words_of(media.get("author")), reserve), (topic_terms, 0), (repeated, 0)]
    chosen, seen, budget = [], set(), max_tokens
    for words, keep_free in sources:
        for word in words:
            key = word.lower()
            if key in seen:
                continue
            cost = estimate_tokens(word) + 1          # + the comma
            if cost > budget - keep_free:
                continue
            seen.add(key)
            chosen.append(word)
            budget -= cost
            if len(chosen) >= max_words:
                return ", ".join(chosen) + "."
    return ", ".join(chosen) + "." if chosen else None
