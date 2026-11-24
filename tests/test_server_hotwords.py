import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from hotwords import MAX_TOKENS, build_hotwords, estimate_tokens, keyword_list  # noqa: E402
from media import capture_video_details  # noqa: E402

KTV = {"title": "(26.11.24. KTV LIVE) 이재명 대통령 제50회 국무회의",
       "tags": ["이재명대통령", "국무회의", "예산안"], "author": "KTV 국민방송",
       "description": "⠀\n✔연간 수출액이 처음 1조 달러 돌파\n#국무회의 https://www.ktv.go.kr 구독하기"}


class HotwordTests(unittest.TestCase):
    def test_title_tags_and_channel_in_priority_order(self):
        words = build_hotwords(KTV).rstrip('.').split(', ')
        self.assertEqual(words[:5], ["KTV", "이재명", "대통령", "제50회", "국무회의"])
        self.assertIn("예산안", words)
        self.assertIn("국민방송", words)
        # Dates, "LIVE", links and boilerplate never make it in; duplicates only once.
        for word in ("26.11.24", "LIVE", "구독하기", "www.ktv.go.kr"):
            self.assertNotIn(word, words)
        self.assertEqual(words.count("국무회의"), 1)

    def test_typed_keywords_come_first(self):
        words = build_hotwords(KTV, "APEC, 머신러닝\n엔비디아").rstrip('.').split(', ')
        self.assertEqual(words[:3], ["APEC", "머신러닝", "엔비디아"])
        self.assertEqual(keyword_list(["a", "GPT-5", "  ", "딥시크"]), ["GPT-5", "딥시크"])

    def test_description_only_adds_repeated_words_and_drops_predicates(self):
        media = {"title": "호르무즈 유조선 '쾅'…이란도 움직였다",
                 "description": "이란은 지하 기지를 공개했습니다. 트럼프 대통령은 이란에 경고했습니다. "
                                "트럼프의 경고가 이어졌다."}
        words = build_hotwords(media).rstrip('.').split(', ')
        self.assertIn("이란", words)           # 이란도 / 이란은 / 이란에 -> one word
        self.assertIn("트럼프", words)         # said twice in the description
        self.assertNotIn("움직였다", words)
        self.assertNotIn("기지", words)        # said once
        self.assertNotIn("공개했습니다", words)

    def test_particle_is_kept_when_the_stem_never_stands_alone(self):
        words = build_hotwords({"title": "국무회의 전문가 토론"}).rstrip('.').split(', ')
        self.assertEqual(words, ["국무회의", "전문가", "토론"])

    def test_length_is_bounded(self):
        many = {"tags": [f"단어{i}번째" for i in range(200)]}
        words = build_hotwords(many).rstrip('.').split(', ')
        self.assertLessEqual(len(words), 40)
        self.assertLessEqual(sum(map(estimate_tokens, words)) + len(words), MAX_TOKENS)

    def test_nothing_to_say(self):
        self.assertIsNone(build_hotwords({"title": "https://example.com/live.m3u8"}))
        self.assertIsNone(build_hotwords(None))


class VideoDetailsTests(unittest.TestCase):
    def test_streamlink_page_data_is_captured(self):
        class Plugin:
            @staticmethod
            def _get_data_from_regex(res, regex, descr):
                return {"videoDetails": {"keywords": ["국무회의"], "shortDescription": "설명"}}

        plugin = Plugin()
        details = capture_video_details(plugin)
        plugin._get_data_from_regex(None, None, "initial player response")
        self.assertEqual(details, {"keywords": ["국무회의"], "shortDescription": "설명"})


if __name__ == "__main__":
    unittest.main()
