#!/bin/bash
# compare_tesseract_vs_paddle.sh — quick A/B test on a small PDF set
#
# Usage:
#   bash scripts/compare_tesseract_vs_paddle.sh <pdf_dir>
#   bash scripts/compare_tesseract_vs_paddle.sh ~/Documents/.../D0202_3号主变压器二次线/PDF
#
# Requires:
#   - myenv/ (Py3.9, Tesseract installed)
#   - myenv312/ (Py3.12, PaddleOCR installed via requirements-paddleocr.txt)

set -e

PDF_DIR="${1:?Usage: $0 <pdf_dir>}"
CSV="${CSV:-$HOME/Documents/work/nengzhong/wuhan/pdf/filter_result_3hzb_ff.csv}"
TEST_DIR="${TEST_DIR:-/tmp/cmp_tp_$$}"
mkdir -p "$TEST_DIR"
WORKERS="${WORKERS:-1}"

echo "=== A/B compare: Tesseract vs PaddleOCR on $PDF_DIR ==="
echo "  CSV:      $CSV"
echo "  workers:  $WORKERS"
echo "  out dir:  $TEST_DIR"
echo ""

# 1. Tesseract run
echo "[1/2] Tesseract run..."
rm -rf "$TEST_DIR/tesseract"
source myenv/bin/activate
python scripts/cable_match.py \
  --csv "$CSV" \
  --input "$PDF_DIR" \
  --output "$TEST_DIR/tesseract" \
  --workers "$WORKERS" \
  --no-state --no-cache 2>&1 | tail -5

# 2. PaddleOCR run (use myenv312)
echo ""
echo "[2/2] PaddleOCR run..."
rm -rf "$TEST_DIR/paddle"
deactivate 2>/dev/null || true
source myenv312/bin/activate
python scripts/cable_match.py \
  --csv "$CSV" \
  --input "$PDF_DIR" \
  --output "$TEST_DIR/paddle" \
  --workers "$WORKERS" \
  --no-state --no-cache --engine paddleocr 2>&1 | tail -5

# 3. Compare
echo ""
echo "=== A/B comparison ==="
python scripts/compare_psm_runs.py \
  "$TEST_DIR/tesseract/_matches.csv" \
  "$TEST_DIR/paddle/_matches.csv" \
  --label-a tesseract --label-b paddleocr

echo ""
echo "Output preserved at: $TEST_DIR/{tesseract,paddle}/_matches.csv"
