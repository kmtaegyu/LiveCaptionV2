# LiveCaption

라이브 방송에 실시간 한국어 자막을 붙이는 시스템입니다. 캡스톤 디자인 프로젝트(팀 레몬).

YouTube 라이브 주소를 넣으면, 시청자 PC가 영상을 YouTube에서 직접 재생하고 GPU 서버가 음성을 인식해
자막만 보냅니다. 자막은 도착한 순간이 아니라 **그 말이 나온 방송 시각(PTS)** 에 맞춰 표시됩니다.

- 시청자가 켭니다. 방송자의 설정이나 협조가 필요 없습니다.
- 한국어 라이브를 인식합니다. 방송의 지연 설정(일반·저지연)과 상관없이 동작합니다.
- 인식 중인 문장은 흐리게, 확정된 문장은 진하게 표시하고, 라이브보다 몇 초 늦게 재생 중인지 화면에 보여 줍니다.

발표 자료: [presentation/livecaption-capstone.html](presentation/livecaption-capstone.html)

## 구조

```
YouTube 라이브 ──영상+음성 HLS──▶ 시청자 PC  (streamlink → 로컬 HLS 프록시 → Qt 플레이어 + 자막 오버레이)
             └─음성 HLS──────▶ GPU 서버   (streamlink → FFmpeg → WhisperLive → faster-whisper)
                                   │
시청자 PC ◀──WebSocket: 자막 텍스트 + 방송 시각──┘
```

| 위치 | 파일 | 역할 |
| --- | --- | --- |
| PC | `main.py` | 플레이어 창, 재생, 자막 배치, 자동 싱크·자동 영상 지연·따라잡기 |
| PC | `stream_resolver.py` | streamlink로 저지연 재생목록 확인(실패하면 yt-dlp) |
| PC | `hls_proxy.py` | 로컬 HLS 프록시: 조각 미리 받기, 최신 조각에서 시작, PTS 읽기, 라이브 지연 측정 |
| PC | `remote_captions.py` | 자막 서버 WebSocket 클라이언트 |
| PC | `caption_panel.py`, `video_view.py`, `subtitles.py` | 패널, 자막 오버레이, 자막 타임라인 |
| 서버 | `server/` | 자막 서버(FastAPI + WhisperLive). [server/README.md](server/README.md) |

## 데스크톱 플레이어 실행 (Windows, Python 3.12)

```powershell
setup.bat          # .venv를 만들고 requirements.txt 설치
run.bat            # 플레이어 실행
```

직접 실행할 때:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --server <서버주소:포트> "https://www.youtube.com/@채널/live"
```

- 자막 서버 주소는 오른쪽 **AI 자막** 패널에 입력합니다(다음 실행에도 기억). 기본값은 `localhost:8000`이며
  환경변수 `LIVECAPTION_SERVER`로 바꿀 수 있습니다.
- YouTube 주소 확인에 JavaScript 런타임이 필요할 수 있습니다. Node.js LTS 설치를 권장합니다.
- 패널의 **영상 지연 자동 조절**을 켜 두면, 자막이 장면보다 늦게 오는 동안 영상을 잠깐 늦추고
  여유가 생기면 1.05배속으로 다시 따라잡습니다. 수동 오프셋은 **수동 조절**을 펼쳐 씁니다.

| 단축키 | 동작 |
| --- | --- |
| Space | 재생 / 일시정지 |
| F, 영상 더블클릭 | 전체 화면 |
| M | 음소거 |
| C | 자막 표시 켜기/끄기 |
| G / H | 자막 싱크 −0.1초 / +0.1초 |

## 그 밖의 자막 소스

- **SRT 파일**: 패널의 **기타 자막 소스 → SRT 파일 열기**. UTF-8, UTF-16, CP949를 지원합니다.
- **로컬 자막 입력**: **로컬 자막 수신**을 켜면 `127.0.0.1:8765`에서 TCP로 한 줄에 자막 하나를 받습니다.
  JSON 한 줄도 됩니다: `{"text": "자막", "id": "7", "duration": 3000}` (같은 id는 그 자막을 교체).

## 테스트

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Windows에서 `discover`가 멈추면 모듈별로 실행합니다(예: `-m unittest tests.test_captions`).
`tests/test_server_media.py`는 ffmpeg가 없는 PC에서 `FFMPEG=ffmpeg` 환경변수를 주고 실행합니다.
`tests/live_server_probe.py`는 실제 서버 점검용 스크립트입니다(unittest 아님).

## 로그

실행하면 `logs/livecaption.log`에 기록합니다(최대 2MB × 4개). URL의 쿼리·사용자 정보는 가립니다.
**보기 → 디버그 로그**에서 실시간으로 볼 수 있습니다. 10초마다 남는 `Behind live` 줄에서
라이브 대비 지연, 자동 지연, 장면보다 500ms 이상 늦게 뜬 첫 자막의 비율을 확인할 수 있습니다.

## 참고

- [WhisperLive](https://github.com/collabora/WhisperLive), [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [streamlink](https://streamlink.github.io/), [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [Qt for Python](https://doc.qt.io/qtforpython-6/)
