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
