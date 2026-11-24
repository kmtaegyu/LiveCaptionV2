# LiveCaption 자막 서버

플레이어는 영상 주소만 보내고, 서버가 음성을 직접 받아 인식한 뒤 자막만 돌려줍니다.

```
플레이어 ──WS /ws {"type":"start","url":...}──▶ caption_api.py (FastAPI, :8000)
    streamlink(실패 시 yt-dlp) → 음성 HLS → FFmpeg → 16kHz 모노 PCM ──▶ WhisperLive (127.0.0.1:9095)
                                                     faster-whisper large-v3-turbo (CUDA FP16)
플레이어 ◀── {"type":"timeline","pts0"} · {"type":"caption", id, text, final, stream_start, stream_end, lag} ──
```

| 파일 | 역할 |
| --- | --- |
| `caption_api.py` | 세션, FFmpeg 실행, WhisperLive 연결, 자막 전송, SRT/VTT 내보내기 |
| `media.py` | 스트림 주소 확인, FFmpeg 명령, 첫 음성 PTS 읽기 |
| `hotwords.py` | 방송 제목·태그·채널명·설명에서 인식 핫워드 만들기(최대 40단어, 약 200토큰) |
| `topics.py`, `dictionaries/<주제>.json` | 주제 사전: 핫워드 용어와 오인식 → 정답 치환 규칙 |
| `segments.py` | WhisperLive 결과 → 자막 이벤트(인식 중과 확정을 같은 id로 잇고, 중복·환각 문구 제거) |
| `run_whisper.py` | WhisperLive 실행(`--beam-size`, `--min-chunk`) |
| `start.sh` | 두 서버를 백그라운드로 (재)시작, 로그는 `logs/` |
| `bench_latency.py`, `bench_restart.sh` | 녹음을 실시간 속도로 재생해 인식 설정별 지연·정확도 비교 |
| `web/` | 실험용 웹 플레이어(서버가 영상도 중계하는 방식) |

## 설치 (Linux, NVIDIA GPU)

```bash
python3.11 -m venv .venv && .venv/bin/pip install uv
.venv/bin/uv pip install --python .venv/bin/python -r requirements.txt \
    --index-strategy unsafe-best-match --extra-index-url https://download.pytorch.org/whl/cu128
.venv/bin/uv pip install --python .venv/bin/python --no-deps whisper-live==0.10.0
./start.sh
curl localhost:8000/health        # {"ok":true,"whisper":"up",...}
```

- 시스템 ffmpeg가 없으면 `imageio-ffmpeg`에 들어 있는 ffmpeg를 씁니다(`SSL_CERT_FILE`은 certifi로 자동 설정).
- whisper-live는 PyAudio·OpenVINO가 필요 없어서 의존성 없이 설치합니다.
- 화자 구분을 쓰려면 `pyannote.audio`를 추가로 설치하고 `HF_TOKEN`을 설정합니다.

## 환경변수 (`.env`에 두면 `start.sh`가 읽음)

| 변수 | 기본값 | 의미 |
| --- | --- | --- |
| `LIVECAPTION_TOKEN` | (없음) | 설정하면 `ws://서버/ws?token=…`이 필요. **외부에 열 때는 꼭 설정** |
| `WHISPER_BEAM_SIZE` | `1` | 디코딩 beam 크기 |
| `WHISPER_SAME_OUTPUT` | `3` | 같은 결과가 몇 번 반복되면 문장을 확정할지 |
| `WHISPER_MIN_CHUNK` | `1.0` | 인식을 시작하기 전에 모을 음성(초) |
| `LIVECAPTION_HOTWORDS` | `1` | `0`이면 방송 정보에서 뽑는 자동 핫워드를 끔 |
| `HF_TOKEN` | (없음) | 화자 구분(pyannote) 모델 다운로드 |

## 프로토콜

플레이어 → 서버

- `{"type":"start","url":"https://www.youtube.com/watch?v=...","language":"ko","diarization":false,"keywords":"APEC, 머신러닝"}`
  `topic`(선택)은 주제 사전 이름(`dictionaries/<topic>.json`, 예: `politics`). 없거나 모르는 이름이면 사전 없이 동작합니다.
  `keywords`(선택)는 패널에 입력한 핫워드. 쉼표·공백으로 구분하며 방송 정보에서 뽑은 핫워드보다 앞에 둡니다.
  `language`가 `null`이면 자동 감지. URL은 http(s)·rtmp·srt·rtp만 허용합니다(서버 파일 접근 차단).
- `{"type":"stop"}`

서버 → 플레이어

- `{"type":"status","state":"resolving|connecting|running|ended|stopped|error","detail":"..."}`
- `{"type":"session","id":"...","title":"...","live":true,"hotwords":"KTV 국무회의 ..."}`
  `hotwords`는 이 세션에 WhisperLive로 보낸 핫워드(없으면 `null`)
- `{"type":"timeline","pts0":초}`: 서버가 받은 첫 음성의 방송 시각. 자막의 방송 시각 = `pts0 + stream_start/1000`
- `{"type":"caption","id":"c3","text":"...","final":false,"speaker":null,"stream_start":ms,"stream_end":ms,"lag":ms}`
  같은 id는 그 자막을 교체하고, `final`이면 확정입니다. `{"id":"c3","remove":true}`는 철회입니다.

확정 자막은 `GET /sessions/{id}.srt`, `GET /sessions/{id}.vtt`로 받습니다(서버 재시작 전까지, 최근 50개 세션).

## 인식 설정 비교 (YTN 라이브 3분 녹음, RTX 5080)

| 설정 | 말 시작 → 처음 표시 | 말 끝 → 확정 | 정확도* |
| --- | --- | --- | --- |
| beam 5 · 반복 7 | 1.73초 | 2.31초 | 0.914 |
| **beam 1 · 반복 3 (기본값)** | 1.14~1.23초 | 1.20~1.28초 | 0.900~0.905 |
| beam 1 · 반복 2 | 1.10초 | 0.90초 | 0.879 |

\*녹음 전체를 문맥을 다 보고 인식한 결과와의 글자 일치도. 중앙값.

## 주제 사전 (`dictionaries/<주제>.json`)

```json
{"topic": "politics", "title": "정치",
 "entries": [{"term": "국무회의", "category": "제도", "hotword": true, "variants": ["궁무회의"]}]}
```

- `hotword: true`인 `term`은 핫워드에 더해집니다. 순서는 패널 입력 > 방송 제목·태그·채널명 > 주제 사전 >
  설명문이고, 개수·토큰 상한(40단어, 약 200토큰)은 같습니다.
- `variants`는 인식 중·확정 자막 모두에서 `term`으로 바뀝니다. 3글자 미만은 무시하고 긴 것부터 적용합니다.
  실제 자막 로그에서 확인된 오인식만 넣습니다.
- 플레이어 패널의 **주제 사전**에서 고릅니다(`remote_captions.TOPICS`). 측정: `bench_latency.py run ... --topic politics`.
