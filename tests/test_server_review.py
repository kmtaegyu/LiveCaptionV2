import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from review_captions import distance, jamo, review, suspects  # noqa: E402
from segments import SegmentTracker  # noqa: E402

TERMS = ["중수청", "위성락", "원내대표", "대통령실", "국민의힘", "북미 정상회담", "친명"]


class SuspectTests(unittest.TestCase):
    def test_jamo_distance(self):
        self.assertEqual(distance(jamo("중소청"), jamo("중수청"), 2), 1)       # one vowel
        self.assertEqual(distance(jamo("강금식"), jamo("강금실"), 2), 1)       # one final consonant
        self.assertGreater(distance(jamo("대한민국"), jamo("원내대표"), 2), 2)

    def test_words_one_or_two_sounds_from_a_term(self):
        self.assertEqual(suspects("이성락 국가안보실장을 중심으로", TERMS), [("이성락", "위성락")])
        self.assertEqual(suspects("민주당 원내대회 지냈고", TERMS), [("원내대회", "원내대표")])
        self.assertEqual(suspects("중소청을 중심으로", TERMS), [("중소청", "중수청")])
        self.assertEqual(suspects("북미 장상회담이 무산", TERMS), [("북미장상회담", "북미 정상회담")])

    def test_ordinary_text_is_left_alone(self):
        for text in ("이 대통령이 당대표였을 땐", "국민의 일상에 닿는 실용행정", "대통령 비서실장의 후임",
                     "중수청과 위성락 그리고 원내대표", "치명적인 실수", "오늘 날씨가 맑습니다"):
            self.assertEqual(suspects(text, TERMS), [], text)

    def test_review_of_a_caption_log(self):
        tracker = SegmentTracker({"중소청": "중수청"}, TERMS)
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "log.jsonl"
            lines = [{"session": "abc", "title": "뉴스", "started": "2026-11-24 10:00:00", "topic": None}]
            for n, text in enumerate(["중소청 출범", "이성락 실장", "이성락 실장은 원내대표를"]):
                event = tracker.update([{"start": n, "end": n + 1, "text": text, "completed": True}])[0]
                lines.append({key: event[key] for key in ("id", "text", "raw", "marks") if event.get(key)})
            log.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8")
            self.assertEqual(lines[1]["raw"], "중소청 출범")            # what the model wrote is kept
            report = review([log], None, TERMS)
        self.assertIn("| 이성락 | 위성락 | 2 |", report)
        self.assertIn("- 중수청: 1회", report)
        self.assertNotIn("| 중소청 |", report)                         # already a known variant

    def test_reviewed_logs_are_renamed_unless_still_being_written(self):
        import os
        import tempfile
        import time
        from review_captions import mark_checked
        with tempfile.TemporaryDirectory() as folder:
            old, live, other = (Path(folder) / name for name in
                                ("a_check_yet.jsonl", "b_check_yet.jsonl", "c.jsonl"))
            for path in (old, live, other):
                path.write_text("{}", encoding="utf-8")
            os.utime(old, (time.time() - 600, time.time() - 600))
            done = mark_checked([old, live, other])
            self.assertEqual([path.name for path in done], ["a_check_ok.jsonl"])
            self.assertTrue(live.exists() and other.exists() and not old.exists())


if __name__ == "__main__":
    unittest.main()
