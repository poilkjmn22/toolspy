#!/bin/bash
# wuhan_status.sh — show progress of both union runs
# Usage: bash scripts/wuhan_status.sh

set -e

chieng_state=/Users/fangqi-apple/Documents/work/nengzhong/wuhan/pdf/.cable_match_state.json
chieng_csv=/Users/fangqi-apple/Documents/work/nengzhong/wuhan/pdf/_matches.csv
chieng_cache=/Users/fangqi-apple/Documents/work/nengzhong/wuhan/pdf/.cable_match_cache.db

chisim_state=/Users/fangqi-apple/Documents/work/nengzhong/wuhan_chisim/.cable_match_state.json
chisim_csv=/Users/fangqi-apple/Documents/work/nengzhong/wuhan_chisim/_matches.csv
chisim_cache=/Users/fangqi-apple/Documents/work/nengzhong/wuhan_chisim/.cable_match_cache.db

for label in "chieng wuhan/pdf" "chisim /Users/fangqi-apple/Documents/work/nengzhong/wuhan_chisim"; do
    name=$(echo $label | cut -d' ' -f1)
    dir=$(echo $label | cut -d' ' -f2)
    case $name in
        chieng) state=$chieng_state; csv=$chieng_csv; cache=$chieng_cache ;;
        chisim) state=$chisim_state; csv=$chisim_csv; cache=$chisim_cache ;;
    esac
    echo "=== $name ($dir) ==="
    if [ -f $state ]; then
        python3 -c "
import json, sqlite3, os
s = json.load(open('$state'))
total = s.get('total', 0)
done = len(s.get('processed', []))
matches = sum(len(v) for v in s.get('matches', {}).values())
print(f'  state: {done}/{total} PDFs ({100*done/max(total,1):.1f}%), {matches} matches')
if os.path.exists('$cache'):
    n_cache = sqlite3.connect('$cache').execute('SELECT COUNT(*) FROM ocr_cache').fetchone()[0]
    print(f'  cache: {n_cache} OCR rows (expected ~{2*done} for both mode)')
if os.path.exists('$csv'):
    n_csv = sum(1 for _ in open('$csv')) - 1
    print(f'  _matches.csv: {n_csv} rows')
"
    else
        echo "  state.json not yet created (still starting up)"
    fi
    # last log line
    if [ -f /tmp/wuhan_${name}.log ]; then
        last=$(tail -1 /tmp/wuhan_${name}.log | cut -c1-100)
        echo "  last log: $last"
    fi
    echo ""
done

echo "=== processes ==="
ps -eo pid,pcpu,etime,command | grep cable_match | grep -v grep | awk '{$1=$1; print "  " $0}'
echo ""
echo "=== free disk space ==="
df -h /Users/fangqi-apple/Documents/work/nengzhong/wuhan/pdf 2>/dev/null | tail -1
df -h /tmp 2>/dev/null | tail -1
