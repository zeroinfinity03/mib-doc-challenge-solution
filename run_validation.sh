#!/usr/bin/env bash
# Full pipeline over the 5,000-PDF validation set, natively (no container).
# The container is what the judge runs on their own box; this produces the
# predictions.jsonl that goes into the pull request.
set -uo pipefail
DATA=../mib-doc-challenge/data/validation
W=val
export MIB_HIDDEN_DIR="$W/hidden" MIB_CLEAN_DIR="$W/clean"
mkdir -p "$W"
PY=.venv/bin/python

# Step 1 retries: get_texttrace() takes the MuPDF C layer down after a few
# hundred documents — a hard crash, not an exception, so the loop is the
# handler. Each pass skips what is already written.
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    $PY step1_scan_hidden.py --data "$DATA" --out "$W/hidden" --workers 4 && break
    echo "step1 crashed, retry $attempt"
done

$PY step2_ocr.py --data "$DATA" --out "$W/ocr" --workers 3
$PY step3_filter.py --ocr "$W/ocr" --hidden "$W/hidden" --out "$W/clean"
$PY step4_extract.py "$W/ocr" "$W/parsed"
$PY step5_decide.py --parsed "$W/parsed" --out "$W/predictions.jsonl"
