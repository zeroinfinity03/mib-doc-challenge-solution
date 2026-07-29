#!/usr/bin/env bash
# Entrypoint for the judge: read every PDF in $1, write predictions to $2.
#
# The container runs --read-only with a 2 GiB tmpfs on /tmp, so every
# intermediate lands under /tmp and only the final file is written to the
# mounted output path. No network: the OCR models are baked into the image at
# /opt/models and referenced by directory, never downloaded.
set -euo pipefail

input_dir="${1:?usage: run.sh <input_pdf_dir> <output_path>}"
output_path="${2:?usage: run.sh <input_pdf_dir> <output_path>}"

work=/tmp/mib
mkdir -p "$work" "$(dirname "$output_path")"

export MIB_HIDDEN_DIR="$work/hidden"
export MIB_CLEAN_DIR="$work/clean"

# 1. hidden-text scan (PyMuPDF text layer) - traps, never values.
#    Retries internally: get_texttrace() has a refcount bug that eventually
#    kills a worker outright, so the step resumes itself rather than
#    needing a loop out here.
python3 /app/step1_scan_hidden.py --data "$input_dir" --out "$work/hidden" --workers 4

# 2. OCR every page (PaddleOCR PP-OCRv6 medium). The slow stage.
python3 /app/step2_ocr.py --data "$input_dir" --out "$work/ocr" --workers 3

# 3. mark the OCR lines that sit on injected text
python3 /app/step3_filter.py --ocr "$work/ocr" --hidden "$work/hidden" --out "$work/clean"

# 4. pull the ten fields out
python3 /app/step4_extract.py "$work/ocr" "$work/parsed"

# 5. adjudicate and write the submission file
python3 /app/step5_decide.py --parsed "$work/parsed" --out "$output_path"
