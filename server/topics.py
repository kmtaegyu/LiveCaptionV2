"""Topic dictionaries: hotwords and spelling fixes for one subject (dictionaries/<topic>.json).

    {"topic": "politics", "title": "정치",
     "groups": {"윤리·징계": {"triggers": ["윤리위", "징계", "제명"]}},
     "entries": [{"term": "중앙윤리위원회", "category": "기관", "group": "윤리·징계",
                  "hotword": false, "variants": [], "role": "", "confirmed": true}, ...]}

``term`` is the spelling captions should show; every ``variant`` - a misrecognition or
other spelling actually seen in captions - is replaced with it in interim and final
captions (variants under three characters are skipped: they would match inside
unrelated words).

Whisper's prompt only fits a few dozen hotwords, so a large dictionary is not sent
whole. ``Topic.select_hotwords`` picks for one broadcast, in this order:
  1. entries whose ``role`` is named in the broadcast's title, tags or description
     ("행정안전부 장관" -> that minister's name),
  2. the terms of every ``group`` the broadcast touches (one of the group's triggers
     or member terms occurs in that text), taking turns between the groups,
  3. the entries marked ``hotword: true`` (the always-on few).
Entries with ``"confirmed": false`` (e.g. names not yet checked by a person) are
ignored entirely. Pure Python, unit-tested.
"""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("livecaption.topics")
DICTIONARY_DIR = Path(__file__).resolve().parent / "dictionaries"
MIN_VARIANT_CHARS = 3
MIN_MATCH_CHARS = 2          # a group member or trigger this short still marks its group
_NAME = re.compile(r"^[a-z0-9_-]{1,40}$")


@dataclass
class Topic:
    name: str
    title: str = ""
    terms: list[str] = field(default_factory=list)          # every confirmed entry's term
    hotwords: list[str] = field(default_factory=list)       # always-on terms (hotword: true)
    replacements: dict[str, str] = field(default_factory=dict)
    groups: dict[str, dict] = field(default_factory=dict)   # name -> {"triggers": [...], "terms": [...]}
    roles: list[tuple[str, str]] = field(default_factory=list)   # (role, term)
    pending: int = 0                                         # entries waiting for confirmation
    guessed: int = 0                                         # replacements from <topic>.auto.json

    def matched_groups(self, text: str) -> list[str]:
        """Names of the groups ``text`` touches, the one with the most hits first."""
        hits = {}
        for name, group in self.groups.items():
            words = set(group["triggers"]) | set(group["terms"])
            count = sum(1 for word in words if len(word) >= MIN_MATCH_CHARS and word in text)
            if count:
                hits[name] = count
        return sorted(hits, key=lambda name: -hits[name])

    def select_hotwords(self, media: dict | None = None, keywords=None) -> list[str]:
        """Dictionary terms worth sending as hotwords for this broadcast, best first."""
        media = media or {}
        parts = [media.get("title"), media.get("author"), media.get("description"), keywords]
        parts += list(media.get("tags") or [])
        text = " ".join(str(part) for part in parts if part)
        chosen = [term for role, term in self.roles if role in text]
        # Take turns between the matched groups, so the few words that fit come from all of them.
        queues = [list(self.groups[name]["terms"]) for name in self.matched_groups(text)]
        while any(queues):
            chosen += [queue.pop(0) for queue in queues if queue]
        chosen += self.hotwords
        return list(dict.fromkeys(chosen))


def parse_topic(name: str, data: dict) -> Topic:
    topic = Topic(name, str(data.get("title") or name))
    groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}
    for group_name, group in groups.items():
        triggers = group.get("triggers") if isinstance(group, dict) else None
        topic.groups[str(group_name)] = {"triggers": [str(word) for word in triggers or []], "terms": []}
    for entry in data.get("entries") or []:
        term = str(entry.get("term") or "").strip() if isinstance(entry, dict) else ""
        if not term:
            continue
        if entry.get("confirmed") is False:
            topic.pending += 1
            continue
        if term not in topic.terms:
            topic.terms.append(term)
        if entry.get("hotword") and term not in topic.hotwords:
            topic.hotwords.append(term)
        group_name = str(entry.get("group") or "")
        if group_name:
            group = topic.groups.setdefault(group_name, {"triggers": [], "terms": []})
            if term not in group["terms"]:
                group["terms"].append(term)
        role = str(entry.get("role") or "").strip()
        if role:
            topic.roles.append((role, term))
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


def add_guessed_variants(topic: Topic, path: Path) -> int:
    """Add the machine-guessed variants of ``<topic>.auto.json`` (see generate_variants.py).

    A confirmed variant or a dictionary term always wins over a guess, and
    LIVECAPTION_AUTO_VARIANTS=0 leaves the guesses out.
    """
    if os.environ.get("LIVECAPTION_AUTO_VARIANTS", "1") == "0":
        return 0
    try:
        guessed = json.loads(path.read_text(encoding="utf-8")).get("variants") or {}
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, AttributeError) as error:
        logger.warning("Cannot read guessed variants %s: %s", path, error)
        return 0
    known, added = set(topic.terms), 0
    for term, variants in guessed.items():
        if term not in known:
            continue
        for variant in variants or []:
            variant = str(variant).strip()
            if len(variant) < MIN_VARIANT_CHARS or variant in known or variant in topic.replacements:
                continue
            topic.replacements[variant] = term
            added += 1
    return added


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
    topic.guessed = add_guessed_variants(topic, directory / f"{name}.auto.json")
    logger.info("Topic %s: %d guessed variants", name, topic.guessed)
    logger.info("Topic %s: %d terms in %d groups, %d always-on hotwords, %d replacements, "
                "%d unconfirmed entries skipped", name, len(topic.terms), len(topic.groups),
                len(topic.hotwords), len(topic.replacements), topic.pending)
    return topic
