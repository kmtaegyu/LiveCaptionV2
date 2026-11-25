"""Guess misrecognitions of dictionary terms from the ones already seen.

    python generate_variants.py politics            # writes dictionaries/politics.auto.json

Every confirmed ``variants`` entry differs from its term by a few jamo (조희대 -> 조의대 is
first sound ㅎ -> ㅇ). Those jamo swaps, applied one at a time to every other term of three
or more syllables, give spellings the model may produce for them. A guess is dropped when it
could be a real word: it is another dictionary word, sits inside one, occurs anywhere in
the caption logs (those are listed for a person to judge instead), only turns the last
syllable into a particle or suffix (탄핵안 -> 탄핵만, 투표록 -> 투표로), or is on the
``blocked`` list kept in the output file (real words a person spotted: 정교유착 -> 종교유착).
People's names get no guesses: one jamo away from a name is usually somebody else's name
(이진수 -> 이진숙, 최인호 -> 최민호), and those cannot be told apart.

Measured 2026-11-25: with one term's variants hidden, the swaps learnt from the other terms
reproduced 12 of 39 seen misrecognitions, so this catches roughly a third of new ones. The
guesses are kept apart from the confirmed variants (``<topic>.auto.json``) and the server
skips them with LIVECAPTION_AUTO_VARIANTS=0.
"""
import argparse
import glob
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
JONG = ["", *"ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"]
MIN_SYLLABLES = 3
# A guess whose only change makes the last syllable one of these reads as "word + particle".
ENDINGS = set("은는이가을를만도로의에와과서시식상부반단지기고")


def split(char: str):
    """[first sound, vowel, final] of a Hangul syllable, or None."""
    code = ord(char) - 0xAC00
    if not 0 <= code < 11172:
        return None
    return [CHO[code // 588], JUNG[code % 588 // 28], JONG[code % 28]]


def join(parts) -> str:
    return chr(0xAC00 + CHO.index(parts[0]) * 588 + JUNG.index(parts[1]) * 28 + JONG.index(parts[2]))


def swaps(term: str, variant: str):
    """The jamo swaps that turn ``term`` into ``variant``: {(slot, from, to)}; None if not comparable."""
    a, b = term.replace(" ", ""), variant.replace(" ", "")
    if len(a) != len(b):
        return None
    found = set()
    for x, y in zip(a, b):
        if x == y:
            continue
        dx, dy = split(x), split(y)
        if not dx or not dy:
            return None
        found |= {(slot, dx[slot], dy[slot]) for slot in range(3) if dx[slot] != dy[slot]}
    return found


def learn(entries) -> set:
    table = set()
    for entry in entries:
        for variant in entry.get("variants") or []:
            table |= swaps(entry["term"], variant) or set()
    return table


def guesses(term: str, table) -> set:
    """``term`` with one jamo swapped, for every swap in ``table`` that applies."""
    out = set()
    for index, char in enumerate(term):
        parts = split(char)
        if not parts:
            continue
        for slot, old, new in table:
            if parts[slot] != old:
                continue
            changed = list(parts)
            changed[slot] = new
            out.add(term[:index] + join(changed) + term[index + 1:])
    out.discard(term)
    return out


def syllables(term: str) -> int:
    return sum(1 for char in term if split(char))


def generate(entries, corpus: str, blocked=()):
    """({term: [guess, ...]}, [(guess, term, count)] seen in the corpus and left for a person)."""
    entries = [entry for entry in entries if entry.get("term") and entry.get("confirmed") is not False]
    table = learn(entries)
    terms = [entry["term"] for entry in entries]
    taken = set(terms) | {variant for entry in entries for variant in entry.get("variants") or []}
    taken |= set(blocked)
    squeezed = [term.replace(" ", "") for term in terms]
    auto, seen, owner = {}, [], {}
    for entry in entries:
        term = entry["term"]
        if entry.get("category") == "인물":
            continue
        if syllables(term) < MIN_SYLLABLES or any(not split(char) and char != " " for char in term):
            continue
        for guess in sorted(guesses(term, table)):
            flat = guess.replace(" ", "")
            if guess in taken or any(flat in other for other in squeezed):
                continue
            if guess[:-1] == term[:-1] and guess[-1] in ENDINGS:
                continue
            if guess in owner:                      # two terms claim it: nobody gets it
                auto[owner[guess]].remove(guess)
                taken.add(guess)
                continue
            count = corpus.count(guess)
            if count:
                seen.append((guess, term, count))
                continue
            owner[guess] = term
            auto.setdefault(term, []).append(guess)
    return {term: words for term, words in auto.items() if words}, seen


def read_corpus(pattern: str) -> str:
    lines = []
    for path in glob.glob(pattern):
        for line in Path(path).read_text(encoding="utf-8").splitlines()[1:]:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            lines.append(record.get("raw") or record.get("text") or "")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Guess misrecognitions of a topic dictionary's terms")
    parser.add_argument("topic")
    parser.add_argument("--logs", default=str(HERE / "logs" / "captions" / "*.jsonl"))
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    data = json.loads((HERE / "dictionaries" / f"{args.topic}.json").read_text(encoding="utf-8"))
    corpus = read_corpus(args.logs)
    out = HERE / "dictionaries" / f"{args.topic}.auto.json"
    try:
        blocked = json.loads(out.read_text(encoding="utf-8")).get("blocked") or []
    except (OSError, ValueError):
        blocked = []
    auto, seen = generate(data.get("entries") or [], corpus, blocked)
    note = ("generate_variants.py가 만든 추측 변형. 사람이 확인한 것이 아님. "
            "잘못 고치는 것이 보이면 그 말을 blocked에 넣고 다시 만들거나, "
            "LIVECAPTION_AUTO_VARIANTS=0으로 전부 끔. blocked = 실제로 쓰는 말이라 뺀 것.")
    out.write_text(json.dumps({"note": note, "blocked": blocked, "variants": auto}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{out.name}: {sum(map(len, auto.values()))} guesses for {len(auto)} terms")
    for guess, term, count in sorted(seen, key=lambda item: -item[2]):
        print(f"  left out, in the logs {count}x: {guess} -> {term}")


if __name__ == "__main__":
    main()
