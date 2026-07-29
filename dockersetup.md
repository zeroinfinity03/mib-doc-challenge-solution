# Docker / Linux Setup — build notes for the MIB submission

Everything here is about packaging the pipeline for the judge's box. Nothing in
the pipeline is macOS-specific; only the *performance* differs by architecture,
and a few of the judge's container flags need real work to satisfy.

Source of truth: `mib-doc-challenge/DOCKER_SUBMISSION.md` and `EVALUATION.md`.
Numbers marked "measured" were measured by us on this Mac (ARM) and must be
re-measured on x86 before we trust them.

---

## 1. The contract

Image takes two arguments and writes predictions:

```bash
docker run --rm --network none \
  --mount type=bind,src=/path/to/pdfs,dst=/input,readonly \
  --mount type=bind,src=/path/to/output,dst=/output \
  <image> /input /output/predictions.jsonl
```

Entry point is `run.sh <input_pdf_dir> <output_path>` (see `run.sh.template`).

## 2. Exactly how the judge runs it

```bash
docker run --rm \
  --network none \
  --cpus 4 \
  --memory 8g \
  --pids-limit 512 \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=2g \
  ...
```

| limit | value | what it means for us |
|---|---|---|
| CPU | 4 vCPU | 3 OCR workers (see §5) |
| RAM | 8 GiB | hard ceiling, container gets OOM-killed above it |
| filesystem | **read-only** | only `/tmp` (2 GiB tmpfs) and `/output` are writable |
| pids | 512 | plenty for 3 workers, but do not spawn per-page processes |
| network | none | models must be baked into the image |
| image size | 4 GiB uncompressed | ours will be ~1 GiB, fine |
| model artifact | 250 MiB each, 1 GiB total | ours: 59 + 73 MB = 132 MB ✅ |
| runtime | 6 s/PDF average | hard stop at 30,000 s for 5,000 PDFs |
| output | max 25 MiB | our predictions.jsonl is ~330 KB ✅ |

## 3. THE READ-ONLY TRAP (most likely thing to break the build)

PaddleOCR/PaddleX downloads and caches models under `~/.paddlex/official_models/`
at first use. In the judge's container that path is **not writable** and there is
**no network** — so anything left to download at runtime fails, and it fails on
the very first PDF.

Both models must be COPYed into the image at build time and pointed at
explicitly. Do not rely on the cache being warm.

```dockerfile
# copy the pre-downloaded models into the image
COPY models/PP-OCRv6_medium_det /opt/models/PP-OCRv6_medium_det
COPY models/PP-OCRv6_medium_rec /opt/models/PP-OCRv6_medium_rec
ENV PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
ENV PADDLE_PDX_PDF_RENDER_SCALE=2.78
ENV HOME=/tmp          # anything that still wants a writable HOME lands on tmpfs
```

and pass local paths instead of names in `run_ocr.py`:

```python
PaddleOCR(
    text_detection_model_dir="/opt/models/PP-OCRv6_medium_det",
    text_recognition_model_dir="/opt/models/PP-OCRv6_medium_rec",
    ...
)
```

**Must be verified by actually running with `--read-only --network none`**, not by
reading docs. A build that works locally and dies on the judge's flags is the
single most expensive failure mode here.

Also: write intermediates to `/tmp`, final output to `/output`. Never to the
working directory.

## 4. The two OCR settings that matter (do not silently change)

Both were found by measurement; the reasoning lives in `run_ocr.py` comments.

```python
use_textline_orientation=False   # BUG FIX, not a tuning knob
text_det_limit_side_len=960
text_det_limit_type="max"
```

**`use_textline_orientation`** defaults to `True` in PaddleOCR 3.7.0. Its
classifier decides per cropped line whether the line is upside-down, and on
these packets it misfires in whole batches of 6, handing the recognizer rotated
crops. Same image, same detection boxes:

```
True  -> 'PN0139' 0.91, 'ri ate', '023-21', 'ASGE'
False -> 'SPN-0139' 1.00, 'Arrival Date', '2026-03-21', 'PASSPORT IMAGE'
```

Verified safe to disable: across **25,574 pages** (train 4,159 + validation
21,415) every page has `/Rotate = 0` and **zero** text lines at 180°. The only
off-axis text is decorative watermarks at −11° and +31°.

**`text_det_limit_side_len=960`** — the shipped default is `64` with
`limit_type=min`, which means "never shrink", so the detector ran on the full
1702×2202 render. The detector only locates text; recognition still crops from
the full-resolution image, so character fidelity is untouched.

Measured over 14 packets (9 from the audit's hard bucket), 126 fields:

| detector input | s/PDF | correct | wrong | missing |
|---|---|---|---|---|
| full size | 38.3 | 98 | 13 | 15 |
| 1280 | 21.4 | 97 | 11 | 18 |
| **960** | **13.6** | **100** | **9** | 17 |
| 736 | 9.4 | 88 | 9 | **29** |

736 starts dropping real text. 960 mostly drops watermark/speckle noise,
including two planted answer-key rows — a small bonus.

Render scale stays at 2.78 (~200 DPI): dropping it to 2.00 saved no time
(13.3 vs 13.6 s) and cost 2 fields. The detector was the bottleneck, not the
render.

## 5. Workers and RAM

Measured with three workers actually running concurrently:

```
worker 0: 1,992.8 MiB
worker 1: 2,117.2 MiB
worker 2: 1,904.8 MiB
total   : 5,878 MiB (5.74 GiB)
```

- **3 workers** → ~5.9 GiB, leaves ~2.1 GiB headroom under the 8 GiB cap ✅
- **4 workers** → ~8.0 GiB before OS/container overhead ❌ do not

Each worker loads its own copy of the models; there is no shared-memory trick
available here. `13.6 s ÷ 3 ≈ 4.5 s/PDF`, inside the 6 s budget — on ARM.

## 6. x86-only speedups we cannot test on the Mac

These are dependency/config changes, not code changes.

| feature | Mac ARM | Linux x86 | note |
|---|---|---|---|
| MKLDNN / oneDNN | inactive | active | `enable_mkldnn` already defaults to `True`; it simply does nothing on ARM |
| OpenVINO | unavailable | available | PaddleOCR 3.7.0 release notes claim **5.2× CPU speedup**; needs the `openvino` package installed at build time |

So our 13.6 s/PDF is a worst-case number measured on the slowest backend. The
judge's x86 box should be faster, possibly much faster. **Do not assume it —
measure on an AWS x86 Linux instance before submitting.**

### First thing to re-measure on x86: the detector size

`text_det_limit_side_len=960` was forced by the ARM budget, and it costs
accuracy. Measured over 100 packets, identical parser:

| detector | correct / 900 | s/PDF @ 3 workers |
|---|---|---|
| full size | **772** | 12.8 ❌ |
| 1280 | — | 7.1 ❌ |
| 960 | 764 | **5.2** ✅ |

Full-size is 8 fields (+0.9%) better but 2.5× too slow *on ARM*. If x86 brings
the expected speedup, det1280 or full-size may fit inside 6 s/PDF and buy those
fields back. Re-run the A/B there before settling the value.

## 7. `cpu_threads` — open question, likely a real win

`DEFAULT_CPU_THREADS = 10` in `paddleocr/_constants.py`. With 3 workers that is
**30 threads on 4 vCPU** — heavy oversubscription that probably costs us time
rather than saving it. Untested.

Try `cpu_threads` of 1 and 2 with 3 workers and measure end-to-end wall clock.
Do this on x86, since thread behaviour differs by backend.

## 8. Build checklist before submitting

- [ ] `--platform linux/amd64` (build on x86 or with buildx; do not ship an ARM image)
- [ ] Models COPYed into the image, referenced by directory not by name
- [ ] Runs green under `--read-only --network none --cpus 4 --memory 8g`
- [ ] Intermediates under `/tmp`, output under `/output`
- [ ] `docker image inspect` size under 4 GiB
- [ ] Timed on the full 5,000-PDF validation set, under 30,000 s total
- [ ] `scripts/validate_submission.py` passes on the produced predictions.jsonl
- [ ] Score reproduced via `scripts/evaluate.py` inside the same container flags

## 9. Things deliberately NOT done

- **No GPU path.** Not allowed by the rules.
- **No Tesseract.** Tested on a degraded packet page: returned 1 line of footer
  and no data fields, merged table columns into single lines, and misread a
  struck-out `XW-2` as `REZ`. It also gives no per-line confidence, which the
  parser's voting depends on.
- **No tiny/small model tier.** small (31 MB) hit 98/126 fields at 3.7 s/PDF and
  tiny (6 MB) 88/126 at 1.3 s/PDF, versus medium's 100/126 at 13.6 s. We are
  already inside the runtime budget, so there is nothing to buy with accuracy.
  Revisit only if x86 timings come back worse than expected.
