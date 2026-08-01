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
#   --network none   nothing may be downloaded at runtime. RapidOCR fetches its
#                    models on first use, so all three are COPYed in and passed
#                    by explicit path (MIB_*_MODEL), never by name.
#   --read-only      the filesystem is not writable. HOME points at /tmp, the
#                    one writable mount, and every intermediate goes there too.
#   --cpus 4         parallelism is per-PDF, 3 workers at ~0.5 GiB each; the
#                    ONNX Runtime threads are pinned to 1 so they do not fight.
#
# NO --platform PIN, and that is a deliberate change from the previous image.
# The pipeline used to run PaddleOCR, and PaddlePaddle ships no linux-aarch64
# wheel, which forced --platform=linux/amd64 and left an ARM judge running the
# whole batch under emulation. onnxruntime publishes manylinux wheels for both
# x86_64 and aarch64, so this image now builds and runs natively on either.

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# PP-OCRv6 small detection + recognition + the mobile text-line classifier.
# 30 MB all in, against the challenge's 250 MiB per-artifact and 1 GiB total.
COPY models/ /opt/models/

ENV MIB_DET_MODEL=/opt/models/PP-OCRv6_det_small.onnx \
    MIB_REC_MODEL=/opt/models/PP-OCRv6_rec_small.onnx \
    MIB_CLS_MODEL=/opt/models/ch_ppocr_mobile_v2.0_cls_mobile.onnx \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY step1_scan_hidden.py step2_ocr.py step3_filter.py \
     step4_extract.py step5_decide.py run.sh /app/
RUN chmod +x /app/run.sh

ENTRYPOINT ["/app/run.sh"]
