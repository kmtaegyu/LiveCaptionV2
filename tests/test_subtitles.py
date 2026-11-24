import tempfile
from pathlib import Path
import unittest

from subtitles import load_srt, parse_srt, subtitle_at


SRT = "1\n00:00:01,000 --> 00:00:02,000\n안녕하세요\n두 번째 줄\n\n2\n00:00:01,500 --> 00:00:03,000\n다음 자막\n"


class SubtitleTest(unittest.TestCase):
    def test_boundaries_overlap_and_backward_seek(self):
        cues = parse_srt(SRT.replace("\n", "\r\n"))
        self.assertEqual(subtitle_at(cues, 999), "")
        self.assertEqual(subtitle_at(cues, 1000), "안녕하세요 두 번째 줄")
        self.assertEqual(subtitle_at(cues, 1500), "다음 자막")
        self.assertEqual(subtitle_at(cues, 2000), "다음 자막")
        self.assertEqual(subtitle_at(cues, 3000), "")
        self.assertEqual(subtitle_at(cues, 1000), "안녕하세요 두 번째 줄")

    def test_encodings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.srt"
            for encoding in ("utf-8", "utf-8-sig", "utf-16", "cp949"):
                with self.subTest(encoding=encoding):
                    path.write_bytes(SRT.encode(encoding))
                    self.assertEqual(len(load_srt(str(path))), 2)

    def test_invalid_input(self):
        for text in ("", "invalid", "1\n00:00:03,000 --> 00:00:01,000\ntext",
                     "1\n00:61:00,000 --> 00:62:00,000\ntext"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_srt(text)


class WrapCaptionTest(unittest.TestCase):
    def test_balanced_wrap_by_character_width(self):
        from video_view import wrap_caption, wrap_by_characters
        measure = len
        self.assertEqual(wrap_caption("짧은 자막", measure, 20), ["짧은 자막"])
        self.assertEqual(wrap_caption("aaaa bb cc dddd", measure, 10), ["aaaa bb", "cc dddd"])
        self.assertIsNone(wrap_caption("aaaaaaaa bbbbbbbb cccccccc", measure, 10))
        self.assertEqual(wrap_caption("aaaaaaaa bbbbbbbb cccccccc", measure, 10, max_lines=3),
                         ["aaaaaaaa", "bbbbbbbb", "cccccccc"])
        self.assertEqual(wrap_by_characters("abcdefghij", measure, 4), ["abcd", "efgh", "ij"])


class SentenceCarryTest(unittest.TestCase):
    def test_sentence_ends(self):
        from subtitles import ends_sentence
        for text in ("국회의원입니다.", "상황입니다", "그렇죠?", "정말요!", "\"끝났다.\"", "Yes."):
            self.assertTrue(ends_sentence(text), text)
        for text in ("수출과 투자", "언론인 출신 재선", "고르게", "경제정책 방향을", ""):
            self.assertFalse(ends_sentence(text), text)

    def test_unfinished_caption_carries_into_the_next(self):
        from subtitles import CaptionTrack
        track = CaptionTrack()
        track.push("반도체 수출과 투자", 0, start=1000, end=3000, cue_id="c1")
        index, _, _ = track.push("중심으로 회복되고 있습니다.", 0, start=3000, end=5000, cue_id="c2",
                                 final=False)
        self.assertEqual(track.cues[index].carry, "반도체 수출과 투자")
        self.assertEqual(track.text_at(3500), "반도체 수출과 투자 중심으로 회복되고 있습니다.")
        self.assertEqual(track.text_at(1500), "반도체 수출과 투자")
        # The interim -> final replacement keeps the carry; the panel row text stays its own.
        index, replaced, _ = track.push("중심으로 회복되고 있습니다.", 0, start=3000, end=5200, cue_id="c2")
        self.assertTrue(replaced)
        self.assertEqual(track.cues[index].text, "중심으로 회복되고 있습니다.")
        self.assertEqual(track.cues[index].carry, "반도체 수출과 투자")
        # A finished sentence carries nothing.
        index, _, _ = track.push("다음 소식입니다.", 0, start=5300, end=7000, cue_id="c3")
        self.assertEqual(track.cues[index].carry, "")

    def test_no_carry_across_long_pause_speaker_change_or_file_cues(self):
        from subtitles import CaptionTrack, Cue
        track = CaptionTrack()
        track.push("경제정책", 0, start=1000, end=2000, cue_id="c1")
        index, _, _ = track.push("방향을", 0, start=4000, end=5000, cue_id="c2")
        self.assertEqual(track.cues[index].carry, "")                       # 2 s pause
        index, _, _ = track.push("말씀하셨는데", 0, start=5100, end=6000, cue_id="c3", speaker="화자 2")
        self.assertEqual(track.cues[index].carry, "")                       # other speaker
        track.set_cues([Cue(0, 1000, "파일 자막")])
        index, _, _ = track.push("이어서", 0, start=1000, end=2000, cue_id="c4")
        self.assertEqual(track.cues[index].carry, "")                       # SRT cue has no id
        off = CaptionTrack(join_sentences=False)
        off.push("수출과 투자", 0, start=0, end=1000, cue_id="c1")
        index, _, _ = off.push("중심으로", 0, start=1000, end=2000, cue_id="c2")
        self.assertEqual(off.cues[index].carry, "")

    def test_carry_is_short_and_does_not_snowball(self):
        from subtitles import CARRY_CHARS, CaptionTrack
        track = CaptionTrack()
        track.push("국토 교통 AI 등 다양한 분야에서 의정 경험을 두루 쌓았으며 국정 철학에 대한 이해와",
                   0, start=0, end=1000, cue_id="c1")
        index, _, _ = track.push("실행력을 갖추고", 0, start=1000, end=2000, cue_id="c2")
        self.assertLessEqual(len(track.cues[index].carry), CARRY_CHARS)
        self.assertTrue(track.cues[index].carry.endswith("이해와"))
        index, _, _ = track.push("있습니다.", 0, start=2000, end=3000, cue_id="c3")
        self.assertLessEqual(len(track.cues[index].carry), CARRY_CHARS)
        self.assertTrue(track.cues[index].carry.endswith("실행력을 갖추고"))
