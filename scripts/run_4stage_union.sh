#!/bin/bash
# run_4stage_union.sh — launch all 4 OCR stages in parallel for the wuhan project
#
# Stages:
#   1. chieng+none      — baseline (matches v1's ~245 results)
#   2. chieng+gauss_otsu — finds labels baseline dropped (3B-414 type)
#   3. chisim+none      — Chinese-only baseline
#   4. chisim+gauss_otsu — finds 3B-463 type labels the chieng+gauss misses
#
# Output dirs (under <WUHAN>/):
#   .stage_chieng_none/   .stage_chieng_gauss/
#   .stage_chisim_none/   .stage_chisim_gauss/
#   .stage_chieng_none/_matches.csv  (etc., one per stage)
#
# Usage:
#   bash scripts/run_4stage_union.sh [workers_per_stage]
#
# Default workers_per_stage = 4 → 16 total when running on 16-core machines.
# Set to 2 on 8-core machines (8 total).
#
# After all stages finish:
#   python scripts/merge_4stage_matches.py /Users/.../wuhan/pdf

set -e

# === Config ===
WUHAN_DIR="${WUHAN_DIR:-/Users/fangqi-apple/Documents/work/nengzhong/wuhan/pdf}"
CSV="${CSV:-$WUHAN_DIR/filter_result_3hzb_ff.csv}"
REPO="${REPO:-/Users/fangqi-apple/Documents/WebDev/toolspy}"
WORKERS_PER_STAGE="${1:-4}"
LOG_DIR="$WUHAN_DIR/../logs"
mkdir -p "$LOG_DIR"

# Each stage writes to its own output dir to keep state/cache separate
STAGE_CHIENG_NONE="$WUHAN_DIR/.stage_chieng_none"
STAGE_CHIENG_GAUSS="$WUHAN_DIR/.stage_chieng_gauss"
STAGE_CHISIM_NONE="$WUHAN_DIR/.stage_chisim_none"
STAGE_CHISIM_GAUSS="$WUHAN_DIR/.stage_chisim_gauss"

cd "$REPO"
source myenv/bin/activate

# === Verify deps ===
if [ ! -f "$CSV" ]; then
    echo "ERROR: CSV not found at $CSV" >&2
    exit 1
fi
if ! command -v tesseract >/dev/null; then
    echo "ERROR: tesseract not on PATH. Install: brew install tesseract tesseract-lang" >&2
    exit 1
fi

echo "=== 4-stage union launcher ==="
echo "  WUHAN_DIR:            $WUHAN_DIR"
echo "  CSV:                  $CSV"
echo "  workers_per_stage:    $WORKERS_PER_STAGE"
echo "  total workers:        $((WORKERS_PER_STAGE * 4))"
echo "  logs:                 $LOG_DIR"
echo ""

# === Launch 4 stages in parallel ===
launch() {
    local name="$1"
    local extra_args="$2"
    local outdir="$3"
    local log="$LOG_DIR/stage_$name.log"

    mkdir -p "$outdir"
    nohup python scripts/cable_match.py \
        --csv "$CSV" \
        --input "$WUHAN_DIR" \
        --output "$outdir" \
        --workers "$WORKERS_PER_STAGE" \
        $extra_args \
        > "$log" 2>&1 &
    local pid=$!
    echo "  launched: $name (PID $pid) → $outdir"
    echo "    log: $log"
    echo $pid > "$LOG_DIR/$name.pid"
}

launch chieng_none "--preprocess none" "$STAGE_CHIENG_NONE"
launch chieng_gauss "--preprocess gauss_otsu" "$STAGE_CHIENG_GAUSS"
launch chisim_none "--lang chi_sim --preprocess none" "$STAGE_CHISIM_NONE"
launch chisim_gauss "--lang chi_sim --preprocess gauss_otsu" "$STAGE_CHISIM_GAUSS"

echo ""
echo "=== all 4 stages running in background ==="
echo "  status: bash $REPO/scripts/wuhan_status.sh"
echo "  stop:   pkill -f 'cable_match.py'  (state.json auto-saved every 30s)"
echo "  merge:  python $REPO/scripts/merge_4stage_matches.py $WUHAN_DIR  (after all 4 finish)"
echo ""
sleep 2
ps -eo pid,etime,command | grep cable_match | grep -v grep | awk '{$1=$1; printf "  PID %s  up %s\n", $2, $3}'
