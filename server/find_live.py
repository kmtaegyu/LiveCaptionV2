"""Which candidate news channels are live right now, and with what title.

    python find_live.py 2>nul        (PowerShell: 2>$null)

Run it just before a demo or a caption-collecting run: each live channel is listed with its
title and the politics dictionary groups that title touches. One at a time on purpose:
yt-dlp and streamlink are not safe to call from several threads.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from media import resolve_media  # noqa: E402
from topics import load_topic  # noqa: E402

CHANNELS = ["ytnnews24", "yonhapnewstv23", "mbn", "tvchanews", "TVChosunNews", "JTBC_news", "newskbs",
            "sbsnews8", "MBCNEWS11", "KTV_korea", "NATV_korea", "OhmynewsTVorg", "YTN_LIFE"]
topic = load_topic("politics")


def check(handle):
    url = f"https://www.youtube.com/@{handle}/live"
    try:
        media = resolve_media(url, want_video=False)
    except Exception as error:
        return handle, url, None, str(error)[:70]
    text = " ".join([media.get("title") or "", " ".join(media.get("tags") or []), (media.get("description") or "")[:300]])
    return handle, url, media, [g for g in topic.matched_groups(text) if not g.startswith("인물")]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for handle, url, media, extra in map(check, CHANNELS):
        if media is None or not media.get("live"):
            print(f"-- {handle}: 라이브 없음 ({extra if media is None else '라이브 아님'})")
        else:
            print(f"OK {url}\n     {media['title']}\n     정치 묶음: {extra or '없음'}")
