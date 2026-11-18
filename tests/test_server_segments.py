import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from segments import SegmentTracker, clean_text, is_hallucination, to_srt, to_webvtt  # noqa: E402


def seg(start, end, text, completed, speaker=None):
    data = {"start": f"{start:.3f}", "end": f"{end:.3f}", "text": text, "completed": completed}
    if speaker:
        data["speaker"] = speaker
    return data


class SegmentTrackerTests(unittest.TestCase):
    def test_interim_then_final_reuses_id(self):
        tracker = SegmentTracker()
        first = tracker.update([seg(0, 1, "안녕", False)])
        self.assertEqual(first[0]["id"], "c1")
        self.assertFalse(first[0]["final"])
        grown = tracker.update([seg(0, 2, "안녕하세요", False)])
        self.assertEqual(grown[0]["id"], "c1")
        final = tracker.update([seg(0, 2, "안녕하세요 여러분", True), seg(2, 3, "오늘은", False)])
        self.assertEqual([(e["id"], e["final"]) for e in final], [("c1", True), ("c2", False)])
        self.assertEqual(final[0]["stream_start"], 0)
        self.assertEqual(final[0]["stream_end"], 2000)

    def test_resent_completed_segments_are_ignored(self):
        tracker = SegmentTracker()
        tracker.update([seg(0, 2, "첫 문장", True), seg(2, 3, "둘", False)])
        events = tracker.update([seg(0, 2, "첫 문장", True), seg(2, 3, "둘", False)])
        self.assertEqual(events, [])
        self.assertEqual(len(tracker.finals), 1)

    def test_unchanged_interim_text_is_not_resent(self):
        tracker = SegmentTracker()
        tracker.update([seg(0, 1, "네", False)])
        self.assertEqual(tracker.update([seg(0, 1, "네", False)]), [])

    def test_hallucination_and_duplicate_finals_dropped(self):
        tracker = SegmentTracker()
        self.assertEqual(tracker.update([seg(0, 1, "시청해주셔서 감사합니다.", True)]), [])
        tracker.update([seg(1, 2, "좋습니다", True)])
        self.assertEqual(tracker.update([seg(2, 3, "좋습니다.", True)]), [])

    def test_interim_that_turns_into_noise_is_removed(self):
        tracker = SegmentTracker()
        tracker.update([seg(0, 1, "음", False)])
        events = tracker.update([seg(0, 1, "구독과 좋아요", False)])
        self.assertEqual(events, [{"type": "caption", "id": "c1", "remove": True}])
        self.assertIsNone(tracker.live_id)

    def test_speaker_is_forwarded(self):
        tracker = SegmentTracker()
        events = tracker.update([seg(0, 1, "반갑습니다", True, "SPEAKER_01")])
        self.assertEqual(events[0]["speaker"], "SPEAKER_01")

    def test_clean_text_collapses_repeats(self):
        self.assertEqual(clean_text("네  네 네 네 네 알겠습니다"), "네 네 알겠습니다")
        self.assertTrue(is_hallucination("  ...  "))
        self.assertFalse(is_hallucination("감사합니다 여러분 오늘 회의를 시작하겠습니다"))

    def test_export_formats(self):
        finals = [{"text": "a<b", "speaker": "S1", "stream_start": 1500, "stream_end": 3200},
                  {"text": "둘", "speaker": None, "stream_start": 3600, "stream_end": 3600}]
        srt = to_srt(finals)
        self.assertIn("1\n00:00:01,500 --> 00:00:03,200\n[S1] a<b", srt)
        self.assertIn("00:00:03,600 --> 00:00:04,100", srt)
        vtt = to_webvtt(finals)
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn("<v S1>a&lt;b", vtt)


if __name__ == "__main__":
    unittest.main()
