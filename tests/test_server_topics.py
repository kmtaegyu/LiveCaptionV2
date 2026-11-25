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

    def test_topic_terms_keep_room_next_to_a_tag_heavy_broadcast(self):
        media = {"title": "국무회의", "tags": [f"태그{i}번째단어" for i in range(60)]}
        terms = ["중수청", "공소청", "인사청문회", "필리버스터"]
        words = build_hotwords(media, None, terms).rstrip(".").split(", ")
        for term in terms:
            self.assertIn(term, words)
        self.assertEqual(words[0], "국무회의")
        self.assertLess(words.index("태그0번째단어"), words.index("중수청"))

    def test_politics_draft_fixes_misrecognitions_seen_in_caption_logs(self):
        topic = load_topic("politics")
        tracker = SegmentTracker(topic.replacements)
        text = tracker.update([seg(0, 2, "중소청 중심의 검찰개혁, 강금식 후보자", True)])[0]["text"]
        self.assertEqual(text, "중수청 중심의 검찰개혁, 강금실 후보자")
        self.assertEqual(topic.hotwords[0], "중수청")

    def test_captions_report_dictionary_words_for_colouring(self):
        topic = load_topic("politics")
        tracker = SegmentTracker(topic.replacements, topic.terms)
        interim = tracker.update([seg(0, 1, "에이펙 뒤 중소청과 공소청", False)])[0]
        self.assertEqual(interim["text"], "APEC 뒤 중수청과 공소청")
        # Rewritten words are "fixed"; a dictionary term recognised as it is is a "term".
        self.assertEqual(interim["marks"], [["APEC", "fixed"], ["중수청", "fixed"], ["공소청", "term"]])
        final = tracker.update([seg(0, 2, "중수청과 공소청 그리고 인사청문회", True)])[0]
        self.assertEqual(final["marks"], [["중수청", "term"], ["공소청", "term"], ["인사청문회", "term"]])
        plain = tracker.update([seg(3, 4, "날씨가 맑겠습니다", True)])[0]
        self.assertNotIn("marks", plain)
        # Without a topic only the common spellings are reported.
        event = SegmentTracker().update([seg(0, 1, "에이펙과 중수청", True)])[0]
        self.assertEqual(event["marks"], [["APEC", "fixed"]])

    GROUPED = {"topic": "grouped", "groups": {
        "윤리·징계": {"triggers": ["윤리위", "징계"]},
        "예산": {"triggers": ["예산안", "예결위"]}},
        "entries": [
            {"term": "중수청", "hotword": True},
            {"term": "중앙윤리위원회", "group": "윤리·징계"},
            {"term": "당원권 정지", "group": "윤리·징계"},
            {"term": "제명", "group": "윤리·징계"},
            {"term": "감액 심사", "group": "예산"},
            {"term": "계수조정", "group": "예산"},
            {"term": "홍길동", "category": "인물", "group": "내각", "role": "행정안전부 장관"},
            {"term": "임꺽정", "category": "인물", "group": "내각", "role": "법무부 장관", "confirmed": False},
        ]}

    def test_hotwords_are_picked_for_the_broadcast(self):
        topic = parse_topic("grouped", self.GROUPED)
        # Nothing in the broadcast touches a group: only the always-on terms.
        self.assertEqual(topic.select_hotwords({"title": "오늘의 날씨"}), ["중수청"])
        # A trigger or a member term brings in the whole group, ahead of the always-on terms.
        picked = topic.select_hotwords({"title": "국민의힘 윤리위, 징계 수위 논의"})
        self.assertEqual(picked, ["중앙윤리위원회", "당원권 정지", "제명", "중수청"])
        self.assertEqual(topic.select_hotwords({"tags": ["제명"]})[:3],
                         ["중앙윤리위원회", "당원권 정지", "제명"])
        # The group with more hits comes first; a role named in the text brings that person.
        picked = topic.select_hotwords({"title": "예산안 처리 시한", "description":
                                        "예결위 심사 중 윤리위 소집. 행정안전부 장관 출석"})
        self.assertEqual(picked[0], "홍길동")
        self.assertLess(picked.index("감액 심사"), picked.index("중앙윤리위원회"))
        self.assertEqual(topic.select_hotwords(None, "징계")[0], "중앙윤리위원회")   # typed keyword

    def test_unconfirmed_entries_are_ignored(self):
        topic = parse_topic("grouped", self.GROUPED)
        self.assertEqual(topic.pending, 1)
        self.assertNotIn("임꺽정", topic.terms)
        self.assertNotIn("임꺽정", topic.select_hotwords({"title": "법무부 장관 내각 개편"}))
        self.assertIn("홍길동", topic.terms)

    def test_selected_terms_fit_next_to_broadcast_words(self):
        topic = parse_topic("grouped", self.GROUPED)
        media = {"title": "윤리위 징계 논의", "tags": [f"태그{i}번째단어" for i in range(60)]}
        words = build_hotwords(media, None, topic.select_hotwords(media)).rstrip(".").split(", ")
        for term in ("중앙윤리위원회", "당원권 정지", "제명", "중수청"):
            self.assertIn(term, words)

    def test_guessed_variants_come_from_seen_jamo_swaps(self):
        from generate_variants import generate
        entries = [{"term": "조희대", "category": "인물", "variants": ["조의대"]},      # ㅎ -> ㅇ
                   {"term": "탄핵안", "category": "제도", "variants": ["단핵안"]},      # ㅌ -> ㄷ
                   {"term": "한미동맹", "category": "외교", "variants": []},
                   {"term": "통일교", "category": "정당", "variants": []},
                   {"term": "한덕수", "category": "인물", "variants": []}]
        auto, seen = generate(entries, corpus="동일교 신자", blocked=["안미동맹"])
        self.assertEqual(auto, {"탄핵안": ["탄액안"]})     # 한미동맹 -> 안미동맹 is blocked
        self.assertEqual(seen, [("동일교", "통일교", 1)])  # in the logs: left for a person
        self.assertNotIn("한덕수", auto)                    # names get no guesses

    def test_guessed_variants_load_beside_the_dictionary_and_can_be_turned_off(self):
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "sample.json").write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
            Path(folder, "sample.auto.json").write_text(json.dumps(
                {"variants": {"국무회의": ["국무위의", "궁무회의", "과방위"], "없는말": ["아무거나"]}},
                ensure_ascii=False), encoding="utf-8")
            topic = load_topic("sample", Path(folder))
            self.assertEqual(topic.replacements["국무위의"], "국무회의")
            self.assertEqual(topic.guessed, 1)              # a confirmed variant and a term are kept as they are
            self.assertNotIn("아무거나", topic.replacements)
            self.assertEqual(available_topics(Path(folder)), ["sample"])
            with mock.patch.dict(os.environ, {"LIVECAPTION_AUTO_VARIANTS": "0"}):
                self.assertNotIn("국무위의", load_topic("sample", Path(folder)).replacements)


if __name__ == "__main__":
    unittest.main()
