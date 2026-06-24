#!/bin/bash
# run_union.sh — launch selected OCR stages in parallel for the wuhan project
#
# Stages (2 lang × 3 OCR recipes — total 6, indexed 1-6):
#   Tesseract (default engine, no extra deps):
#     1. chieng_tess         — chi_sim+eng, no preprocess (baseline, matches v1)
#     2. chieng_tess_gauss   — chi_sim+eng, gauss_otsu (recovers char errors)
#     3. chisim_tess         — chi_sim only, no preprocess
#     4. chisim_tess_gauss   — chi_sim only, gauss_otsu (3B-463 attempt)
#   PaddleOCR (optional, needs `pip install -r requirements-paddleocr.txt`):
#     5. chieng_paddle       — chieng → PaddleOCR ch model, no preprocess
#     6. chisim_paddle       — chisim → PaddleOCR en model, no preprocess
#                            (per Q1=B: deliberately different from chieng)
#
# Output dirs (under <WUHAN>/):
#   .stage_chieng_tess/         .stage_chieng_tess_gauss/
#   .stage_chisim_tess/         .stage_chisim_tess_gauss/
#   .stage_chieng_paddle/       .stage_chisim_paddle/
#
# Usage:
#   bash scripts/run_union.sh [workers_per_stage]
#   bash scripts/run_union.sh 4                                  # all 6 stages
#   STAGES_FILTER="1-4" bash scripts/run_union.sh 4              # only Tesseract 4 stages
#   STAGES_FILTER="5,6" bash scripts/run_union.sh 4              # only PaddleOCR 2 stages
#   STAGES_FILTER="1-3,6" bash scripts/run_union.sh 4            # mixed subset
#
# USE_GPU=1 enables GPU for PaddleOCR stages (5, 6). HARD-FAIL if
# paddlepaddle was installed without CUDA support or no CUDA driver is
# reachable. Tesseract stages (1-4) ignore USE_GPU.
#
#   USE_GPU=1 STAGES_FILTER="5-6" bash scripts/run_union.sh 4     # PaddleOCR on GPU
#
# On Win11 NVIDIA box, install paddlepaddle-gpu first (see AGENTS.md or
# run_union.ps1 header for the exact pip lines for cu117/cu118/cu123).
# On macOS, USE_GPU=1 will hard-fail (paddlepaddle macOS wheel has no CUDA).
#
# STAGES_FILTER syntax:
#   "all"        — run all available stages (default)
#   "1-4"        — range, inclusive
#   "1,3,5"      — comma list
#   "1-3,6"      — mixed range + list
#
# Default workers_per_stage = 4 → 24 total when running all 6 on 16-core
# machines (PaddleOCR stages hold ~250MB model each, so 16GB+ RAM recommended).
# Set to 2 on 8-core machines (12 total).
#
# After all stages finish:
#   python scripts/merge_5stage_matches.py <WUHAN_DIR>
# Run merge_5stage_matches.py MULTIPLE times safely:
#   - First run before all stages finish: produces partial union of stages
#     that have a _matches.csv
#   - Second run after all stages finish: produces full union
#   - The merge is idempotent — re-running always reads the current CSVs.
#
# Mac notes:
#   - Tesseract stages use myenv/ (Py3.9).
#   - PaddleOCR stages use myenv312/ (Py3.12 + paddleocr).
#   This script auto-switches venvs. Skip the PaddleOCR stages by passing
#   ENGINE=tesseract.
#
# Win11 notes:
#   - Both engines can live in the same venv (`pip install -r requirements.txt`
#     + `pip install -r requirements-paddleocr.txt`).
#   - Set ENGINE=mixed (default) on Win11 to run all 6 stages.
#   - Set ENGINE=tesseract to skip PaddleOCR stages.

set -e

# === Config ===
WUHAN_DIR="${WUHAN_DIR:-/Users/fangqi-apple/Documents/work/nengzhong/wuhan/pdf}"
CSV="${CSV:-$WUHAN_DIR/filter_result_3hzb_ff.csv}"
REPO="${REPO:-/Users/fangqi-apple/Documents/WebDev/toolspy}"
WORKERS_PER_STAGE="${1:-4}"
STAGES_FILTER="${STAGES_FILTER:-all}"  # "all" | "1-4" | "1,3,5" | "1-3,6"
USE_GPU="${USE_GPU:-0}"  # 1 = enable GPU for PaddleOCR stages (5-6); hard-fail on missing CUDA
LOG_DIR="$WUHAN_DIR/../logs"
mkdir -p "$LOG_DIR"

# Per-stage output dirs (each keeps its own cache + state + _matches.csv)
STAGE_CHIENG_TESS="$WUHAN_DIR/.stage_chieng_tess"
STAGE_CHIENG_TESS_GAUSS="$WUHAN_DIR/.stage_chieng_tess_gauss"
STAGE_CHISIM_TESS="$WUHAN_DIR/.stage_chisim_tess"
STAGE_CHISIM_TESS_GAUSS="$WUHAN_DIR/.stage_chisim_tess_gauss"
STAGE_CHIENG_PADDLE="$WUHAN_DIR/.stage_chieng_paddle"
STAGE_CHISIM_PADDLE="$WUHAN_DIR/.stage_chisim_paddle"

# === Engine selection ===
# ENGINE=mixed (default): all 6 stages
# ENGINE=tesseract: only 4 Tesseract stages (skip PaddleOCR)
ENGINE="${ENGINE:-mixed}"
case "$ENGINE" in
    mixed) SKIP_PADDLE=0 ;;
    tesseract) SKIP_PADDLE=1 ;;
    paddleocr)
        echo "ERROR: ENGINE=paddleocr alone is not supported in 6-stage mode." >&2
        echo "       Use ENGINE=mixed (default) for all 6 stages, or ENGINE=tesseract to skip PaddleOCR." >&2
        exit 1
        ;;
    *)
        echo "ERROR: unknown ENGINE=$ENGINE (expected 'mixed' or 'tesseract')" >&2
        exit 1
        ;;
esac

# === Verify deps ===
if [ ! -f "$CSV" ]; then
    echo "ERROR: CSV not found at $CSV" >&2
    exit 1
fi

# Tesseract: always required
if ! command -v tesseract >/dev/null; then
    echo "ERROR: tesseract not on PATH. Install: brew install tesseract tesseract-lang" >&2
    exit 1
fi

# PaddleOCR: required unless SKIP_PADDLE
if [ "$SKIP_PADDLE" = "0" ]; then
    # Find a venv with paddleocr installed. On Mac, this is usually myenv312
    # (Py3.12). On Win11, paddleocr is typically in the same myenv as
    # Tesseract. We try a list of common locations.
    PADDLE_PY=""
    for candidate in \
        "$REPO/myenv/bin/python" \
        "$REPO/myenv312/bin/python"; do
        if [ -f "$candidate" ] && "$candidate" -c "import paddleocr" 2>/dev/null; then
            PADDLE_PY="$candidate"
            break
        fi
    done
    if [ -z "$PADDLE_PY" ]; then
        echo "ERROR: paddleocr not installed in any venv (tried myenv/ and myenv312/)." >&2
        echo "       Run: <myenv>/bin/pip install -r requirements-paddleocr.txt" >&2
        exit 1
    fi
    PADDLE_VENV_DIR=$(dirname "$PADDLE_PY")
fi

# Tesseract venv: myenv/ on Mac, same on Win11
TESS_PY="$REPO/myenv/bin/python"
if [ ! -f "$TESS_PY" ]; then
    echo "ERROR: Tesseract venv not found at myenv/" >&2
    exit 1
fi

# === Build the full stage list (before STAGES_FILTER applies) ===
# Each entry is "name|python|extra_args|output_dir"
declare -a ALL_STAGES=(
    "chieng_tess|$TESS_PY|--preprocess none|$STAGE_CHIENG_TESS"
    "chieng_tess_gauss|$TESS_PY|--preprocess gauss_otsu|$STAGE_CHIENG_TESS_GAUSS"
    "chisim_tess|$TESS_PY|--lang chi_sim --preprocess none|$STAGE_CHISIM_TESS"
    "chisim_tess_gauss|$TESS_PY|--lang chi_sim --preprocess gauss_otsu|$STAGE_CHISIM_TESS_GAUSS"
)

if [ "$SKIP_PADDLE" = "0" ]; then
    ALL_STAGES+=(
        "chieng_paddle|$PADDLE_PY|--engine paddleocr --preprocess none|$STAGE_CHIENG_PADDLE"
        "chisim_paddle|$PADDLE_PY|--engine paddleocr --lang chi_sim --preprocess none|$STAGE_CHISIM_PADDLE"
    )
fi

N_ALL_STAGES=${#ALL_STAGES[@]}

# === Parse STAGES_FILTER into FILTERED_STAGES array (1-based indices) ===
FILTERED_STAGES=()
parse_stage_filter() {
    local filter="$1"
    local max="$2"
    FILTERED_STAGES=()
    if [ "$filter" = "all" ] || [ -z "$filter" ]; then
        FILTERED_STAGES=( $(seq 1 "$max") )
        return 0
    fi
    local IFS=','
    local tokens=( $filter )
    for token in "${tokens[@]}"; do
        # Trim whitespace
        token="${token// /}"
        if [[ "$token" =~ ^([0-9]+)-([0-9]+)$ ]]; then
            local s="${BASH_REMATCH[1]}"
            local e="${BASH_REMATCH[2]}"
            if [ "$s" -gt "$e" ]; then
                echo "ERROR: invalid range '$token' (start > end)" >&2
                return 1
            fi
            for ((i=s; i<=e; i++)); do
                if [ "$i" -lt 1 ] || [ "$i" -gt "$max" ]; then
                    echo "ERROR: stage index $i out of range (1-$max)" >&2
                    return 1
                fi
                FILTERED_STAGES+=( "$i" )
            done
        elif [[ "$token" =~ ^[0-9]+$ ]]; then
            if [ "$token" -lt 1 ] || [ "$token" -gt "$max" ]; then
                echo "ERROR: stage index $token out of range (1-$max)" >&2
                return 1
            fi
            FILTERED_STAGES+=( "$token" )
        else
            echo "ERROR: invalid stage token: '$token' (expected integer or N-M range)" >&2
            return 1
        fi
    done
    return 0
}

parse_stage_filter "$STAGES_FILTER" "$N_ALL_STAGES" || exit 1
N_FILTERED=${#FILTERED_STAGES[@]}
TOTAL_WORKERS=$((WORKERS_PER_STAGE * N_FILTERED))

# === GPU precheck (only when USE_GPU=1 AND at least one PaddleOCR stage runs) ===
# HARD-FAIL: user explicitly asked for GPU; if paddlepaddle isn't CUDA-enabled
# or no CUDA driver is reachable, we refuse to silently fall back to CPU.
if [ "$USE_GPU" = "1" ] && [ "$SKIP_PADDLE" = "0" ]; then
    has_paddle_stage=0
    for s in "${FILTERED_STAGES[@]}"; do
        # PaddleOCR stages are indices 5 and 6
        if [ "$s" = "5" ] || [ "$s" = "6" ]; then
            has_paddle_stage=1
            break
        fi
    done
    if [ "$has_paddle_stage" = "1" ]; then
        echo "  GPU precheck: running paddle.utils.run_check() ..."
        gpu_out_file="$(mktemp)"
        # Run precheck; disable set -e around it because the python process
        # intentionally exits non-zero to signal failure categories.
        set +e
        "$PADDLE_PY" - > "$gpu_out_file" 2>&1 <<'PYEOF'
import sys
try:
    import paddle
    paddle.utils.run_check()
    if not paddle.device.is_compiled_with_cuda():
        print('PADDLE_NOT_COMPILED_WITH_CUDA')
        sys.exit(2)
    n = paddle.device.cuda.device_count()
    if n < 1:
        print('NO_CUDA_DEVICE_VISIBLE')
        sys.exit(4)
    print(f'CUDA_OK n={n}')
except SystemExit:
    raise
except Exception as e:
    print(f'GPU_CHECK_FAILED: {type(e).__name__}: {e}')
    sys.exit(3)
PYEOF
        gpu_exit=$?
        set -e
        # Filter paddle's verbose GLOG noise; keep last 5 lines (real status).
        gpu_out="$(tail -5 "$gpu_out_file")"
        rm -f "$gpu_out_file"
        if [ "$gpu_exit" -ne 0 ]; then
            cat >&2 <<EOF

GPU precheck failed (exit code $gpu_exit). Last lines of output:
$gpu_out

This usually means paddlepaddle was installed without CUDA support, or your
CUDA driver / toolkit version doesn't match the wheel you grabbed.

Fix on Linux / WSL2 NVIDIA box:

  1. Check your CUDA driver version:
       nvidia-smi
     (top-right "CUDA Version" line is the *driver*-supported max; the
      *toolkit* version installed on the box is what the wheel must match.)

  2. Uninstall the CPU-only paddlepaddle:
       $PADDLE_VENV_DIR/pip uninstall -y paddlepaddle

  3. Install paddlepaddle-gpu==2.6.2 from the matching wheel index:
       # CUDA 11.7 (most Win11 boxes shipped 2022-2024):
       $PADDLE_VENV_DIR/pip install paddlepaddle-gpu==2.6.2 -f https://www.paddlepaddle.org.cn/whl/linux/cu117
       # CUDA 11.8:
       #   .../whl/linux/cu118
       # CUDA 12.x:
       #   .../whl/linux/cu123

  4. Verify:
       $PADDLE_PY -c "import paddle; print(paddle.device.is_compiled_with_cuda(), paddle.device.cuda.device_count())"

  5. Re-run with USE_GPU=1.

If you can't install CUDA-enabled paddlepaddle on this machine, drop USE_GPU
and PaddleOCR stages will run on CPU (still works, ~5-10x slower than GPU).

On macOS, USE_GPU=1 always fails (paddlepaddle macOS wheel has no CUDA).
EOF
            exit 1
        fi
        echo "  GPU precheck: OK ($gpu_out)"
    fi
fi

cd "$REPO"
echo "=== 6-stage union launcher (filtered) ==="
echo "  WUHAN_DIR:            $WUHAN_DIR"
echo "  CSV:                  $CSV"
echo "  engine:               $ENGINE  (override: ENGINE=tesseract bash ...)"
echo "  workers_per_stage:    $WORKERS_PER_STAGE"
if [ "$SKIP_PADDLE" = "0" ]; then
    echo "  paddle python:        $PADDLE_PY"
fi
echo "  tess python:          $TESS_PY"
echo "  logs:                 $LOG_DIR"
echo ""
echo "  stages filter:        $STAGES_FILTER  ($N_FILTERED / $N_ALL_STAGES stages)"
echo "  total workers:        $TOTAL_WORKERS"
if [ "$SKIP_PADDLE" = "0" ]; then
    if [ "$USE_GPU" = "1" ]; then
        echo "  gpu:                  enabled (PaddleOCR stages 5-6 only; Tesseract stages ignore)"
    else
        echo "  gpu:                  disabled (PaddleOCR stages will run on CPU; set USE_GPU=1 to enable)"
    fi
fi
echo ""
echo "  all stages (index → name):"
for ((i=0; i<N_ALL_STAGES; i++)); do
    stage="${ALL_STAGES[$i]}"
    IFS='|' read -r name _ <<< "$stage"
    idx=$((i+1))
    marker=""
    for s in "${FILTERED_STAGES[@]}"; do
        if [ "$s" = "$idx" ]; then
            marker="  ← will run"
            break
        fi
    done
    echo "    $idx. $name$marker"
done
echo ""

# === Launch selected stages in parallel ===
launch() {
    local name="$1"
    local py="$2"
    local extra_args="$3"
    local outdir="$4"
    local log="$LOG_DIR/stage_$name.log"

    mkdir -p "$outdir"
    nohup "$py" scripts/cable_match.py \
        --csv "$CSV" \
        --input "$WUHAN_DIR" \
        --output "$outdir" \
        --workers "$WORKERS_PER_STAGE" \
        $extra_args \
        > "$log" 2>&1 &
    local pid=$!
    echo "  launched: $name (PID $pid, py=$(basename $(dirname $py))) → $outdir"
    echo "    log: $log"
    echo $pid > "$LOG_DIR/$name.pid"
}

for idx in "${FILTERED_STAGES[@]}"; do
    array_idx=$((idx-1))
    stage="${ALL_STAGES[$array_idx]}"
    IFS='|' read -r name py args outdir <<< "$stage"
    # Inject --use-gpu into PaddleOCR stages when USE_GPU=1
    if [ "$USE_GPU" = "1" ] && [[ "$name" == *_paddle ]]; then
        args="$args --use-gpu"
    fi
    launch "$name" "$py" "$args" "$outdir"
done

echo ""
echo "=== $N_FILTERED stage(s) running in background ==="
echo "  status: bash $REPO/scripts/wuhan_status.sh"
echo "  stop:   pkill -f 'cable_match.py'  (state.json auto-saved every 30s)"
echo "  merge:  python $REPO/scripts/merge_5stage_matches.py $WUHAN_DIR  (idempotent; run any time)"
if [ "$USE_GPU" = "1" ]; then
    echo "  gpu:    nvidia-smi  (check PaddleOCR stages are actually using GPU; util should be >0%)"
fi
echo ""
sleep 2
ps -eo pid,etime,command | grep cable_match | grep -v grep | awk '{$1=$1; printf "  PID %s  up %s\n", $2, $3}'