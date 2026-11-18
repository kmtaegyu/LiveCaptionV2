#!/usr/bin/env bash
# Start (or restart) WhisperLive and the caption API in the background.
# Logs: ./logs/{whisper,api}.log
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
mkdir -p logs
# cuDNN/cuBLAS from pip wheels for CTranslate2 (faster-whisper)
SITE=$(python -c 'import site; print(site.getsitepackages()[0])')
export LD_LIBRARY_PATH="$SITE/nvidia/cublas/lib:$SITE/nvidia/cudnn/lib:${LD_LIBRARY_PATH:-}"
[ -f .env ] && set -a && source .env && set +a

pkill -f run_whisper.py 2>/dev/null || true
pkill -f "uvicorn caption_api" 2>/dev/null || true
sleep 1
nohup python run_whisper.py > logs/whisper.log 2>&1 &
nohup uvicorn caption_api:app --host 0.0.0.0 --port 8000 > logs/api.log 2>&1 &
sleep 3
echo "whisper pid: $(pgrep -f run_whisper.py || echo none)"
echo "api pid:     $(pgrep -f 'uvicorn caption_api' || echo none)"
