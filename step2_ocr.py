"""Run PaddleOCR's native pipeline (PP-OCRv6 medium) over a folder of PDFs.

No custom PDF handling: each PDF path goes straight to PaddleOCR.predict(),
which renders every page to an image internally (pdfium) and OCRs it. The
library's own save_to_json() writes the raw per-page results, one JSON per
page, under <out>/<case_id>/.

Resumable: cases that already have output JSONs are skipped on re-run.

PARALLELISM — how many PDFs run at the same time:
    Controlled by --workers (default 3). Each worker is a separate process
    holding its own copy of the OCR models. Measured, not estimated: three
    workers running concurrently peaked at 1993 / 2117 / 1905 MiB, i.e.
    5.87 GiB total, about 2 GiB each.

        --workers 1   safest; laptop stays cool          (~2 GiB RAM)
        --workers 3   default; fits the judge's 8 GiB    (~5.9 GiB RAM)
        --workers 5   strong desktop only                (~10 GiB RAM)
        --workers 10  workstation with many cores + RAM  (~20 GiB RAM)

    Why 3 and not 4: the challenge box is 4 vCPU / 8 GiB. Four workers need
    ~8.0 GiB before counting the OS and the container itself, which does not
    fit; three leaves ~2.2 GiB of headroom.

    Rule of thumb: workers <= physical cores - 2, and workers * 3 GB must
    fit comfortably in free RAM — if the machine starts swapping, the whole
    system crawls (or worse). When in doubt, stay low: too many workers can
    freeze or crash the machine.

    Two ways to set it:
      1. On the command line each run (recommended):
             uv run python step2_ocr.py --data ../mib-doc-challenge/data/train --out ocr-output --workers 5
      2. Permanently: in main() below, find the "--workers" argument and
         change default=3 to whatever your machine can handle — then plain
         runs use that number automatically.

Usage:
    uv run python step2_ocr.py --data ../mib-doc-challenge/data/train --out ocr-output
    uv run python step2_ocr.py --data ../mib-doc-challenge/data/train --out ocr-output --workers 5
    uv run python step2_ocr.py --data ../mib-doc-challenge/data/train --out ocr-output --limit 2   # quick test
"""

import argparse
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# Must be set before paddleocr is imported (also inherited by workers).
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")  # no phone-home
os.environ.setdefault("PADDLE_PDX_PDF_RENDER_SCALE", "2.78")  # 2.78 x 72 = ~200 DPI
# Parallelism here is per-PDF, not per-tensor. PaddleOCR defaults to 10 CPU
# threads, so three workers ask for 30 threads on a 4-vCPU box and spend their
# time fighting each other. One core each, N PDFs at once.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

# Local model directories, so the image can run under --network none on a
# read-only filesystem (paddlex would otherwise try to download into ~/.paddlex).
DET_MODEL_DIR = os.environ.get("MIB_DET_MODEL_DIR")
REC_MODEL_DIR = os.environ.get("MIB_REC_MODEL_DIR")

# One OCR engine per process, created lazily on first use.
_ocr = None


def _get_ocr():
    global _ocr
    if _ocr is None:
        from paddleocr import PaddleOCR

        local = {}
        if DET_MODEL_DIR and REC_MODEL_DIR:
            local["text_detection_model_dir"] = DET_MODEL_DIR
            local["text_recognition_model_dir"] = REC_MODEL_DIR

        _ocr = PaddleOCR(
            **local,
            text_detection_model_name="PP-OCRv6_medium_det",
            text_recognition_model_name="PP-OCRv6_medium_rec",
            # OFF. This is the whole-page rotation model (a DIFFERENT model from
            # the textline classifier below), and it was tried on all 1,000
            # packets. It loses.
            #
            # It does what it claims on genuinely sideways pages — MIB-000009
            # page 4 went from 'EGIAGE', 'peRO', 'Arivl -11' to
            # 'Home World: Luyten-b' / 'Species Code: KAIJU_MICRO' /
            # 'Arrival Date: 2026-07-11' — and it is not slow (3 PDFs at
            # --workers 3: 35s on, 58s off; full run 117.5 min vs 113).
            #
            # But it also misfires on upright pages and destroys them. Measured
            # over the full corpus, 187 packets lost their adjudicator note:
            #   MIB-000005 p2  off -> 'Manual Adjudicator Note',
            #                         'Finding: APPROVED. Reason: Clean ...'
            #                  on  -> 'Syntent', 'PacK K1 e 2', 'APPED', 'Maute'
            # The adjudicator note is the top of the trust ladder and alone
            # drives 324 decisions at 99% accuracy, so:
            #     off  123.30 / 150   (classification 64.78)
            #     on   117.59 / 150   (classification 60.21)
            #
            # METHOD NOTE: the first evaluation of this flag sampled only pages
            # that were ALREADY empty, found 6 of 40 recovered, and called it a
            # win. It never checked whether the flag BREAKS pages that were fine.
            # Measure both directions before adopting anything.
            # MIB_DOC_ORI=1 turns it back on for re-measurement.
            use_doc_orientation_classify=os.environ.get("MIB_DOC_ORI", "0") != "0",
            use_doc_unwarping=False,
            # OFF, and this one only shows up inside the container. PaddleOCR
            # defaults it to True; on Apple Silicon oneDNN is not built so the
            # flag is silently a no-op, but in the linux/amd64 image it really
            # engages and every predict() dies with
            #   NotImplementedError: ConvertPirAttribute2RuntimeAttribute not
            #   support [pir::ArrayAttribute<pir::DoubleAttribute>]
            #   (onednn_instruction.cc)
            # Measured in-container on the same PDF: True -> crash,
            # False -> 49 lines. A local run can never surface this.
            enable_mkldnn=False,
            # MUST stay False. This classifier decides whether each cropped
            # text line is upside-down; on these packets it misfires and hands
            # the recognizer rotated crops, which silently eats the leading
            # characters. Measured on MIB-000843 page 3, identical image:
            #   True  -> 'PN0139' 0.91, 'ri ate', '023-21', 'ASGE'
            #   False -> 'SPN-0139' 1.00, 'Arrival Date', '2026-03-21', ...
            # It also loads a third model we do not need.
            use_textline_orientation=False,
            # The detector only has to find WHERE text sits, not read it, so it
            # runs on a shrunken copy; recognition still crops from the full
            # 1702x2202 render, leaving character fidelity untouched. The
            # shipped default (limit_side_len=64, limit_type=min) means "never
            # shrink" and is what made this stage 38s/PDF.
            # Measured over 14 packets (9 from the audit's hard bucket), same
            # parser, scoring correct fields out of 126:
            #   full size  38.3s   98 correct   13 wrong   15 missing
            #   1280       21.4s   97 correct   11 wrong   18 missing
            #   960        13.6s  100 correct    9 wrong   17 missing  <- chosen
            #   736         9.4s   88 correct    9 wrong   29 missing
            # 736 starts dropping real text; 960 mostly drops watermark and
            # speckle noise (including two planted answer-key rows).
            # Overridable so the choice can be re-measured without editing code:
            #   MIB_DET_SIDE=0 restores the library default (never shrink).
            text_det_limit_side_len=int(os.environ.get("MIB_DET_SIDE", "960")) or 64,
            text_det_limit_type="max" if os.environ.get("MIB_DET_SIDE", "960") != "0" else "min",
        )
    return _ocr


def process_pdf(pdf_path: str, out_root: str) -> str:
    """OCR one PDF into <out_root>/<case_id>/, skipping if already done."""
    pdf = Path(pdf_path)
    case_dir = Path(out_root) / pdf.stem
    if any(case_dir.glob("*.json")):
        return "skipped"
    case_dir.mkdir(parents=True, exist_ok=True)

    results = _get_ocr().predict(str(pdf))  # library renders + OCRs every page
    for res in results:
        res.save_to_json(str(case_dir))  # raw per-page JSON, library-named
    return "done"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="folder of PDFs")
    ap.add_argument("--out", required=True, help="output root folder")
    ap.add_argument("--limit", type=int, default=0, help="stop after N PDFs (0 = all)")
    ap.add_argument(
        "--workers",
        type=int,
        default=3,
        help="how many PDFs to process in parallel (see PARALLELISM note above)",
    )
    args = ap.parse_args()

    pdfs = sorted(str(p) for p in Path(args.data).glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"no PDFs found in {args.data}")
    if args.limit:
        pdfs = pdfs[: args.limit]
    Path(args.out).mkdir(parents=True, exist_ok=True)

    print(f"{len(pdfs)} PDFs -> {args.out}  ({args.workers} worker(s))", flush=True)
    t0 = time.time()
    done = skipped = failed = 0

    def tally(status: str, i: int):
        nonlocal done, skipped
        if status == "skipped":
            skipped += 1
        else:
            done += 1
        if done and done % 10 == 0:
            rate = done / (time.time() - t0)
            eta_min = (len(pdfs) - i) / rate / 60
            print(
                f"{i}/{len(pdfs)} done ({skipped} skipped)  "
                f"{rate:.2f} pdf/s  eta {eta_min:.0f} min",
                flush=True,
            )

    # One unreadable packet must not take the whole run down with it — this is
    # a two-hour job over 1,000 files, and ex.map() propagates the first
    # exception and abandons the rest. Failures are counted and reported; the
    # run is resumable, so a re-run picks up whatever is missing.
    if args.workers <= 1:
        for i, pdf in enumerate(pdfs, 1):
            try:
                tally(process_pdf(pdf, args.out), i)
            except Exception as exc:
                failed += 1
                print(f"  FAIL {Path(pdf).name}: {type(exc).__name__}: {exc}", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(process_pdf, p, args.out): p for p in pdfs}
            for i, future in enumerate(as_completed(futures), 1):
                try:
                    tally(future.result(), i)
                except Exception as exc:
                    failed += 1
                    print(f"  FAIL {Path(futures[future]).name}: "
                          f"{type(exc).__name__}: {exc}", flush=True)

    print(f"finished: {done} processed, {skipped} skipped, {failed} failed, {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
