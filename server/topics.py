"""Topic dictionaries: hotwords and spelling fixes for one subject (dictionaries/<topic>.json).

    {"topic": "politics", "title": "정치",
     "entries": [{"term": "국무회의", "category": "제도", "hotword": true,
                  "variants": ["국무 회의"]}, ...]}

``term`` is the spelling captions should show. ``hotword: true`` terms join the
hotwords (after the presenter's words and the broadcast's own), and every
``variant`` - a misrecognition or other spelling actually seen in captions - is
replaced with ``term`` in interim and final captions. Variants shorter than
three characters are skipped: they would match inside unrelated words.
Pure Python, unit-tested.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("livecaption.topics")
DICTIONARY_DIR = Path(__file__).resolve().parent / "dictionaries"
MIN_VARIANT_CHARS = 3
_NAME = re.compile(r"^[a-z0-9_-]{1,40}$")


@dataclass
class Topic:
    name: str
    title: str = ""
    terms: list[str] = field(default_factory=list)          # every entry's term
    hotwords: list[str] = field(default_factory=list)
    replacements: dict[str, str] = field(default_factory=dict)


def parse_topic(name: str, data: dict) -> Topic:
    topic = Topic(name, str(data.get("title") or name))
    for entry in data.get("entries") or []:
        term = str(entry.get("term") or "").strip() if isinstance(entry, dict) else ""
        if not term:
            continue
        if term not in topic.terms:
            topic.terms.append(term)
        if entry.get("hotword") and term not in topic.hotwords:
            topic.hotwords.append(term)
        for variant in entry.get("variants") or []:
            variant = str(variant).strip()
            if variant == term:
                continue
            if len(variant) < MIN_VARIANT_CHARS:
                logger.warning("%s: variant '%s' of '%s' is under %d characters; skipped",
                               name, variant, term, MIN_VARIANT_CHARS)
                continue
            topic.replacements[variant] = term
    return topic


def available_topics(directory: Path = DICTIONARY_DIR) -> list[str]:
    return sorted(path.stem for path in directory.glob("*.json") if _NAME.match(path.stem))


def load_topic(name, directory: Path = DICTIONARY_DIR) -> Topic | None:
    """The topic dictionary called ``name``, or None (no name, unknown name or a bad file)."""
    if not name:
        return None
    name = str(name).strip().lower()
    if not _NAME.match(name):          # a client value never becomes an arbitrary path
        logger.warning("Ignored topic name %r", name)
        return None
    path = directory / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("No topic dictionary %s", path)
        return None
    except (OSError, ValueError) as error:
        logger.warning("Cannot read topic dictionary %s: %s", path, error)
        return None
    topic = parse_topic(name, data if isinstance(data, dict) else {})
    logger.info("Topic %s: %d hotwords, %d replacements", name, len(topic.hotwords),
                len(topic.replacements))
    return topic
