#!/bin/bash
# run_union.sh — launch all 6 OCR stages in parallel for the wuhan project
#
# Stages (2 lang × 3 OCR recipes — total 6):
#   Tesseract (default engine, no extra deps):
#     1. chieng+none      — chi_sim+eng, no preprocess (baseline, matches v1)
#     2. chieng+gauss      — chi_sim+eng, gauss_otsu (recovers char errors)
#     3. chisim+none       — chi_sim only, no preprocess
#     4. chisim+gauss      — chi_sim only, gauss_otsu (3B-463 attempt)
#   PaddleOCR (optional, needs `pip install -r requirements-paddleocr.txt`):
#     5. chieng+paddle     — chieng → PaddleOCR ch model, no preprocess
#     6. chisim+paddle     — chisim → PaddleOCR en model, no preprocess
#                            (per Q1=B: deliberately different from chieng)
#
# Output dirs (under <WUHAN>/):
#   .stage_chieng_tess/         .stage_chieng_tess_gauss/
#   .stage_chisim_tess/         .stage_chisim_tess_gauss/
#   .stage_chieng_paddle/       .stage_chisim_paddle/
#
# Usage:
#   bash scripts/run_union.sh [workers_per_stage]
#
# Default workers_per_stage = 4 → 24 total when running on 16-core machines
# (PaddleOCR stages hold ~250MB model each, so 16GB+ RAM recommended).
# Set to 2 on 8-core machines (12 total).
#
# After all stages finish:
#   python scripts/merge_5stage_matches.py <WUHAN_DIR>
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

cd "$REPO"
echo "=== 6-stage union launcher ==="
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

# === Launch stages in parallel ===
# Each stage: (stage_name, venv_python, extra_args, output_dir)
declare -a STAGES=(
    "chieng_tess|$TESS_PY|--preprocess none|$STAGE_CHIENG_TESS"
    "chieng_tess_gauss|$TESS_PY|--preprocess gauss_otsu|$STAGE_CHIENG_TESS_GAUSS"
    "chisim_tess|$TESS_PY|--lang chi_sim --preprocess none|$STAGE_CHISIM_TESS"
    "chisim_tess_gauss|$TESS_PY|--lang chi_sim --preprocess gauss_otsu|$STAGE_CHISIM_TESS_GAUSS"
)

if [ "$SKIP_PADDLE" = "0" ]; then
    STAGES+=(
        "chieng_paddle|$PADDLE_PY|--engine paddleocr --preprocess none|$STAGE_CHIENG_PADDLE"
        "chisim_paddle|$PADDLE_PY|--engine paddleocr --lang chi_sim --preprocess none|$STAGE_CHISIM_PADDLE"
    )
fi

N_STAGES=${#STAGES[@]}
TOTAL_WORKERS=$((WORKERS_PER_STAGE * N_STAGES))
echo "  total stages:         $N_STAGES"
echo "  total workers:        $TOTAL_WORKERS"
echo ""

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

for stage in "${STAGES[@]}"; do
    IFS='|' read -r name py args outdir <<< "$stage"
    launch "$name" "$py" "$args" "$outdir"
done

echo ""
echo "=== all $N_STAGES stages running in background ==="
echo "  status: bash $REPO/scripts/wuhan_status.sh"
echo "  stop:   pkill -f 'cable_match.py'  (state.json auto-saved every 30s)"
echo "  merge:  python $REPO/scripts/merge_5stage_matches.py $WUHAN_DIR  (after all finish)"
echo ""
sleep 2
ps -eo pid,etime,command | grep cable_match | grep -v grep | awk '{$1=$1; printf "  PID %s  up %s\n", $2, $3}'
