"""Review caption logs: what the dictionary changed, and words that look like misheard terms.

    python review_captions.py                      # newest log in logs/captions
    python review_captions.py logs/captions/*.jsonl --topic politics --out review.md

caption_api writes every final caption to logs/captions/<date>_<time>_<session>.jsonl.
This script cannot hear the audio, so it only points at suspects: caption words that
differ from a dictionary term by one or two sounds (jamo), e.g. "중소청" next to "중수청".
A person decides which are real misrecognitions; those go into the dictionary's
``variants``. Pure Python.
"""
import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from segments import SPELLINGS  # noqa: E402
from topics import load_topic  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent / "logs" / "captions"
YET, OK = "_check_yet.jsonl", "_check_ok.jsonl"      # log not reviewed yet / reviewed
MIN_SYLLABLES = 3            # shorter terms are one sound away from too many ordinary words
_INITIAL, _MEDIAL, _FINAL = 19, 21, 28


def jamo(text: str) -> tuple:
    """Hangul syllables as (initial, medial, final) numbers; other characters as themselves."""
    sounds = []
    for char in text:
        code = ord(char) - 0xAC00
        if 0 <= code < _INITIAL * _MEDIAL * _FINAL:
            sounds += [("i", code // (_MEDIAL * _FINAL)), ("m", code // _FINAL % _MEDIAL), ("f", code % _FINAL)]
        else:
            sounds.append(("c", char.lower()))
    return tuple(sounds)


def distance(a: tuple, b: tuple, limit: int) -> int:
    """Edit distance between two jamo sequences, cut off above ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[-1] + 1, previous[j - 1] + (x != y)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def allowed_distance(term: str) -> int:
    return 1 if len(term) <= 3 else 2


PARTICLES = set("이가은는을를의에도과와로만")


def suspects(text: str, terms: list[str]) -> list[tuple[str, str]]:
    """(heard, term) pairs: stretches of ``text`` that are close to a dictionary term but not it.

    Single-word terms are compared inside one word at a time ("국민의 일상" is not "국민의힘");
    a word that is just the term's stem plus a particle ("대통령이" vs "대통령실") is skipped.
    """
    found = []
    compact = text.replace(" ", "")
    words = text.split()
    for term in terms:
        target = term.replace(" ", "")
        if len(target) < MIN_SYLLABLES or target in compact:
            continue
        limit = allowed_distance(target)
        goal = jamo(target)
        for chunk in ([compact] if " " in term else words):
            match = None
            for start in range(len(chunk) - len(target) + 1):
                window = chunk[start:start + len(target)]
                if not all("가" <= char <= "힣" or char.isalnum() for char in window):
                    continue
                if len(window) > 3 and window[-1] in PARTICLES and window[:-1] == target[:-1]:
                    continue
                if 0 < distance(jamo(window), goal, limit) <= limit:
                    match = window
                    break
            if match:
                found.append((match, term))
                break
    return found


def read_logs(paths):
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def review(paths, topic_name=None, extra_terms=()) -> str:
    records = list(read_logs(paths))
    headers = [record for record in records if "session" in record]
    captions = [record for record in records if "text" in record]
    topic_name = topic_name or next((h.get("topic") for h in headers if h.get("topic")), None)
    topic = load_topic(topic_name)
    terms = list(dict.fromkeys((topic.terms if topic else []) + list(extra_terms) + list(SPELLINGS.values())))
    known = set(topic.replacements if topic else {}) | set(SPELLINGS)
    fixed, seen_terms = Counter(), Counter()
    suspect_count, suspect_lines = Counter(), defaultdict(list)
    for caption in captions:
        for word, kind in caption.get("marks") or []:
            (fixed if kind == "fixed" else seen_terms)[word] += 1
        heard_text = caption.get("raw") or caption["text"]
        for heard, term in suspects(heard_text, terms):
            # Already handled: a known variant, or this caption's own fix produced the term.
            if heard in known or heard in terms or [term, "fixed"] in (caption.get("marks") or []):
                continue
            suspect_count[(heard, term)] += 1
            if len(suspect_lines[(heard, term)]) < 3:
                suspect_lines[(heard, term)].append(heard_text)
    out = ["# 자막 검토", ""]
    for header in headers:
        out.append(f"- {header.get('started', '')} · {header.get('title', '')} · 주제 사전: {header.get('topic') or '없음'}")
    out += [f"- 확정 자막 {len(captions)}개, 사전 단어 {len(terms)}개와 비교", "",
            "## 사전과 비슷하게 들린 단어 (의심)", "",
            "음성을 듣지 않고 글자만 비교한 결과입니다. 실제 오인식인 것만 사전의 variants에 넣습니다.", ""]
    if suspect_count:
        out += ["| 자막에 나온 말 | 사전 단어 | 횟수 | 자막 |", "| --- | --- | --- | --- |"]
        for (heard, term), count in suspect_count.most_common():
            out.append(f"| {heard} | {term} | {count} | {' / '.join(suspect_lines[(heard, term)])} |")
    else:
        out.append("없음")
    out += ["", "## 사전이 고쳐 쓴 단어", ""]
    out += [f"- {word}: {count}회" for word, count in fixed.most_common()] or ["없음"]
    out += ["", "## 그대로 인식된 사전 단어", ""]
    out += [", ".join(f"{word}({count})" for word, count in seen_terms.most_common()) or "없음", ""]
    return "\n".join(out)


def mark_checked(paths, quiet_seconds: float = 120) -> list:
    """Rename ..._check_yet.jsonl logs to ..._check_ok.jsonl; returns the new paths.

    A log written to in the last ``quiet_seconds`` is left alone: its session may still be
    running, and the server would start a second file under the old name.
    """
    done = []
    for path in map(Path, paths):
        if not path.name.endswith(YET) or time.time() - path.stat().st_mtime < quiet_seconds:
            continue
        target = path.with_name(path.name[:-len(YET)] + OK)
        if not target.exists():
            path.rename(target)
            done.append(target)
    return done


def main():
    parser = argparse.ArgumentParser(description="Review LiveCaption caption logs")
    parser.add_argument("logs", nargs="*", help="caption log files (default: the newest one)")
    parser.add_argument("--topic", help="topic dictionary to compare with (default: the session's)")
    parser.add_argument("--out", help="write the review to this file instead of printing it")
    parser.add_argument("--mark", action="store_true",
                        help="rename the reviewed ..._check_yet.jsonl logs to ..._check_ok.jsonl")
    args = parser.parse_args()
    # Default: every log nobody has reviewed yet, or the newest log when all are reviewed.
    paths = args.logs or sorted(LOG_DIR.glob("*_check_yet.jsonl")) or sorted(LOG_DIR.glob("*.jsonl"))[-1:]
    if not paths:
        sys.exit(f"No caption logs in {LOG_DIR}")
    text = review(paths, args.topic)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    if args.mark:
        print(f"marked {len(mark_checked(paths))} logs as checked")


if __name__ == "__main__":
    main()
