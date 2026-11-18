import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import media  # noqa: E402


class MediaTimelineTests(unittest.TestCase):
    def test_program_date_time_parsing(self):
        self.assertEqual(media.parse_program_date_time("1970-01-01T00:00:01.500+0000"), 1500)
        self.assertEqual(media.parse_program_date_time("1970-01-01T09:00:02.000+0900"), 2000)
        self.assertEqual(media.parse_program_date_time("1970-01-01T00:00:03Z"), 3000)

    def test_first_segment_of_playlist(self):
        playlist = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n"
                    "#EXT-X-PROGRAM-DATE-TIME:1970-01-01T00:00:10.000+0000\n#EXTINF:2.0,\nseg_00000.ts\n"
                    "#EXT-X-PROGRAM-DATE-TIME:1970-01-01T00:00:12.000+0000\n#EXTINF:2.0,\nseg_00001.ts\n")
        self.assertEqual(media.first_segment(playlist), (10000, "seg_00000.ts"))
        self.assertIsNone(media.first_segment("#EXTM3U\n"))
        self.assertIsNone(media.first_segment("#EXTM3U\n#EXTINF:2.0,\nseg_00000.ts\n"))   # no date yet

    def test_epoch_is_label_of_first_audio_sample(self):
        # Segment dates pin to the earliest packet; the PCM starts at the first audio packet.
        self.assertEqual(media.timeline_epoch(10000, {"video": 1.0, "audio": 1.25}), 10250)
        self.assertEqual(media.timeline_epoch(10000, {"video": 1.4, "audio": 1.2}), 10000)

    def test_ffmpeg_command_for_relay_and_audio_only(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = {"video": "https://v/index.m3u8", "audio": "https://a/index.m3u8",
                     "headers": {}, "bandwidth": 4600000}
            command = media.ffmpeg_command(relay, directory)
            source = os.path.join(directory, "source.m3u8")
            self.assertEqual(command[command.index("-i") + 1], source)
            master = Path(source).read_text(encoding="utf-8")
            self.assertIn('URI="https://a/index.m3u8"', master)
            self.assertIn("https://v/index.m3u8", master)
            self.assertIn("program_date_time", " ".join(command))
            self.assertEqual(command[-1], "pipe:1")
            self.assertEqual(command.count("-map"), 3)
            audio_only = media.ffmpeg_command({"video": None, "audio": "rtmp://host/live", "headers": {}})
            self.assertNotIn("-f hls", " ".join(audio_only))
            self.assertIn("rtmp://host/live", audio_only)
            # Source timestamps are kept and frame 0's PTS is logged for the caption clock.
            self.assertLess(audio_only.index("-copyts"), audio_only.index("-i"))
            graph = audio_only[audio_only.index("-filter_complex") + 1]
            self.assertIn(r"aselect=eq(n\,0),ashowinfo", graph)
            # ...but the PCM branch is restamped so the s16le muxer sees monotonic DTS.
            self.assertIn("asetpts=N/SR/TB[pcm]", graph)
            self.assertEqual(audio_only[audio_only.index("-map") + 1], "[pcm]")

    def test_http_input_gives_up_on_stalled_reads(self):
        command = media.ffmpeg_command({"video": None, "audio": "https://m/hls_playlist/index.m3u8", "headers": {}})
        self.assertEqual(command[command.index("-rw_timeout") + 1], "5000000")
        self.assertLess(command.index("-rw_timeout"), command.index("-i"))

    def test_live_audio_prefers_a_muxed_variant_with_full_rate_audio(self):
        HLSStream = type("HLSStream", (), {})
        HTTPStream = type("HTTPStream", (), {})

        def stream(kind, url):
            item = kind()
            item.url = url
            return item

        streams = {"144p": stream(HLSStream, "https://m/91"), "360p": stream(HLSStream, "https://m/93"),
                   "worst": stream(HLSStream, "https://m/91"), "best": stream(HLSStream, "https://m/96")}
        self.assertEqual(media.pick_live_audio(streams), "https://m/93")
        self.assertEqual(media.pick_live_audio({"worst": streams["144p"]}), "https://m/91")
        # A VOD comes back as a progressive HTTP stream: leave it to yt-dlp.
        self.assertIsNone(media.pick_live_audio({"360p": stream(HTTPStream, "https://v/18")}))
        self.assertIsNone(media.pick_live_audio({}))

    def test_first_pts_from_ashowinfo(self):
        line = ("[info] [Parsed_ashowinfo_2 @ 0x7ff218003d80] n:0 pts:77174784 pts_time:1749.995102 "
                "fmt:fltp channels:2 chlayout:stereo rate:44100 nb_samples:1024")
        self.assertAlmostEqual(media.parse_first_pts(line), 1749.995102)
        self.assertIsNone(media.parse_first_pts(line.replace("n:0 ", "n:10 ")))
        self.assertIsNone(media.parse_first_pts("[hls @ 0x1] Opening 'https://x' for reading"))


if __name__ == "__main__":
    unittest.main()
