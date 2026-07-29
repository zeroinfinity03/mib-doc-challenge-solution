# MIB Doc Challenge — offline submission image.
#
# The judge runs this with:
#   docker run --rm --network none --cpus 4 --memory 8g --pids-limit 512 \
#     --read-only --tmpfs /tmp:rw,nosuid,nodev,size=2g \
#     --mount ...,dst=/input,readonly --mount ...,dst=/output \
#     <image> /input /output/predictions.jsonl
#
# Three of those flags shape everything below:
#
#   --network none   nothing may be downloaded at runtime. PaddleOCR normally
#                    fetches its models on first use, so both are COPYed in and
#                    passed by DIRECTORY (MIB_*_MODEL_DIR), not by name.
#   --read-only      the filesystem is not writable. paddlex still wants a HOME
#                    to scribble in, so HOME points at /tmp, the one writable
#                    mount. All intermediates go to /tmp too (see run.sh).
#   --cpus 4         parallelism is per-PDF, 3 workers at ~2 GiB each; the OCR
#                    threads are pinned to 1 so the workers do not fight.
#
# Architecture is PINNED to linux/amd64 because the framework only supports it.
# PaddlePaddle's own install guide is explicit:
#   "The processor architecture is x86_64 (or called x64, Intel 64, AMD64).
#    Currently, PaddlePaddle does not support arm64."
#   https://www.paddlepaddle.org.cn/documentation/docs/en/install/index_en.html
# Supported OS: Windows 10/11, Ubuntu 20.04/22.04/24.04, AlmaLinux 8, macOS.
#
# That matches what PyPI actually ships: paddlepaddle 3.3.1 (which PP-OCRv6
# needs) has no linux-aarch64 wheel at all — 3.2.2 is the newest there — so an
# unpinned build on an ARM host dies at
#   ERROR: No matching distribution found for paddlepaddle==3.3.1
#
# The challenge never states which architecture it scores on. Pinned, an ARM
# host still runs this under emulation; unpinned, it cannot build. Note that
# macOS ARM works only because Python wheels are per-OS *and* per-arch —
# macosx-arm64 exists, linux-aarch64 does not.

FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

# libgomp1 is required by paddle; the rest is what PyMuPDF/OpenCV need to load.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# PP-OCRv6 medium detection + recognition, 59 MB + 73 MB. Well inside the
# challenge's 250 MiB per-artifact and 1 GiB total limits.
COPY models/PP-OCRv6_medium_det /opt/models/PP-OCRv6_medium_det
COPY models/PP-OCRv6_medium_rec /opt/models/PP-OCRv6_medium_rec

ENV MIB_DET_MODEL_DIR=/opt/models/PP-OCRv6_medium_det \
    MIB_REC_MODEL_DIR=/opt/models/PP-OCRv6_medium_rec \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PADDLE_PDX_PDF_RENDER_SCALE=2.78 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY step1_scan_hidden.py step2_ocr.py step3_filter.py \
     step4_extract.py step5_decide.py run.sh /app/
RUN chmod +x /app/run.sh

ENTRYPOINT ["/app/run.sh"]
