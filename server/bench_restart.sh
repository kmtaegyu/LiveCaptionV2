#!/usr/bin/env bash
# Restart the benchmark WhisperLive on :9096 with the given run_whisper.py options, then run
# bench_latency.py for each --same-output value. Leaves the production server (:9095) alone.
#   bench_restart.sh "<run_whisper options>" <tag> <same_output>...
#   e.g. bench_restart.sh "--beam-size 1 --min-chunk 0.5" b1_m05 3 2
# Results: bench/<tag>_t<N>.json, log: bench/<tag>.log
set -uo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
SITE=$(python -c 'import site; print(site.getsitepackages()[0])')
export LD_LIBRARY_PATH="$SITE/nvidia/cublas/lib:$SITE/nvidia/cudnn/lib:${LD_LIBRARY_PATH:-}"
options=$1; tag=$2; shift 2
for pid in $(pgrep -f "run_whisper.py --port 9096"); do
    [ "$pid" != "$$" ] && kill "$pid"
done
sleep 2
nohup python run_whisper.py --port 9096 --max-clients 1 $options > logs/whisper-bench.log 2>&1 < /dev/null &
for _ in $(seq 60); do
    grep -q "listening" logs/whisper-bench.log && break
    sleep 1
done
grep -E "beam|minimum|listening" logs/whisper-bench.log
for threshold in "$@"; do
    python bench_latency.py run bench/ytn.pcm --whisper ws://127.0.0.1:9096 \
        --same-output "$threshold" --out "bench/${tag}_t${threshold}.json"
done
