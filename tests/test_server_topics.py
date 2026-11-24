import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from hotwords import build_hotwords  # noqa: E402
from segments import SegmentTracker, clean_text  # noqa: E402
from topics import DICTIONARY_DIR, available_topics, load_topic, parse_topic  # noqa: E402

SAMPLE = {"topic": "sample", "title": "예시", "entries": [
    {"term": "국무회의", "category": "제도", "hotword": True, "variants": ["국무 회의", "궁무회의"]},
    {"term": "과방위", "category": "기관", "hotword": False, "variants": ["과학방송위", "과방"]},
    {"term": "", "variants": ["무시됨"]},
    "not an entry",
]}


def seg(start, end, text, completed):
    return {"start": f"{start:.3f}", "end": f"{end:.3f}", "text": text, "completed": completed}


class TopicTests(unittest.TestCase):
    def test_entries_become_hotwords_and_replacements(self):
        topic = parse_topic("sample", SAMPLE)
        self.assertEqual(topic.title, "예시")
        self.assertEqual(topic.hotwords, ["국무회의"])
        # "과방" is under 3 characters: it would match inside other words, so it is skipped.
        self.assertEqual(topic.replacements, {"국무 회의": "국무회의", "궁무회의": "국무회의",
                                              "과학방송위": "과방위"})

    def test_load_by_name_only_from_the_dictionary_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "sample.json").write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
            (folder / "broken.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(load_topic("Sample", folder).hotwords, ["국무회의"])
            self.assertEqual(available_topics(folder), ["broken", "sample"])
            for name in (None, "", "missing", "broken", "../sample", "a/b", "C:\\x"):
                self.assertIsNone(load_topic(name, folder), name)

    def test_shipped_politics_dictionary_loads(self):
        self.assertIn("politics", available_topics(DICTIONARY_DIR))
        topic = load_topic("politics")
        self.assertIsNotNone(topic)
        self.assertEqual(topic.title, "정치")
        self.assertTrue(all(len(key) >= 3 for key in topic.replacements))

    def test_tracker_applies_topic_rules_with_common_spellings(self):
        tracker = SegmentTracker(parse_topic("sample", SAMPLE).replacements)
        interim = tracker.update([seg(0, 1, "궁무회의에서 에이펙", False)])
        self.assertEqual(interim[0]["text"], "국무회의에서 APEC")
        final = tracker.update([seg(0, 2, "국무 회의에서 과학방송위 소관", True)])
        self.assertEqual(final[0]["text"], "국무회의에서 과방위 소관")
        # Without a topic only the common spellings apply.
        self.assertEqual(clean_text("궁무회의에서 에이펙"), "궁무회의에서 APEC")
        self.assertEqual(SegmentTracker().update([seg(0, 1, "궁무회의", True)])[0]["text"], "궁무회의")

    def test_hotword_priority_typed_then_broadcast_then_topic(self):
        media = {"title": "제50회 국무회의", "tags": ["예산안"], "author": "KTV",
                 "description": "트럼프 트럼프"}
        words = build_hotwords(media, "APEC", ["국무회의", "헌법재판소"]).rstrip('.').split(', ')
        self.assertEqual(words, ["APEC", "제50회", "국무회의", "예산안", "KTV", "헌법재판소", "트럼프"])
        self.assertEqual(build_hotwords(None, None, ["헌법재판소"]), "헌법재판소.")


if __name__ == "__main__":
    unittest.main()
