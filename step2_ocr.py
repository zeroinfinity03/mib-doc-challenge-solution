"""Experimental step 2: RapidOCR (ONNX, PP-OCRv6 small) instead of PaddleOCR.

Why try it (measured on sentinel pages before this existed):
  - reads the sideways MIB-000009 p4 that PaddleOCR medium destroys, WITHOUT
    the doc-orientation pass that cost 5.71 points corpus-wide;
  - reads the ghosted MIB-000003 note as 'DENIEN' (exact-matchable) where
    medium gave 'DENEn';
  - reads MIB-000843 'SPN-0139' at 1.0 with its cls enabled — the PaddleOCR
    textline-cls bug does not reproduce here;
  - onnxruntime ships linux-aarch64 wheels, so the ARM constraint would die;
  - worker init is 0.8s vs paddle's ~12s, models ~10 MB vs 133 MB.

cls stays ON: it occasionally 180-flips a struck value ('XW-2' -> 'Z-MX',
which normalization rejects), but with it OFF the sideways pages garble.

Output is written in step2_ocr.py's exact JSON schema (rec_texts, rec_scores,
rec_boxes, rec_polys, page_index) so steps 3-5 run unchanged.

Usage:
    uv run python step2_rapid.py --data <pdfs> --out ocr-output-rapid --workers 3
"""

import argparse
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")
RENDER_SCALE = 2.78  # 200 DPI, same as the PaddleOCR baseline being compared
BAND_FRACTION = 0.36        # top slice re-detected; every form field sits here
BAND_OVERLAP_REJECT = 0.5   # box overlap above which a line is a re-detection
REREAD_BELOW = 0.75         # recognizer confidence under which a line is re-read
REREAD_PAD_Y = 0.25         # vertical padding as a fraction of box height
REREAD_PAD_X = 60           # px of extra context to the left
REREAD_PAD_R = 250          # px of extra context to the right


def _overlap_frac(a, b):
    x = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    y = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return (x * y) / max(1, (a[2] - a[0]) * (a[3] - a[1]))

_ocr = None


def _get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr import RapidOCR
        # MIB_OCR_TIER=medium switches both models to the PP-OCRv6 medium ONNX
        # conversions (exp 4 tier A/B); default stays the package's small tier.
        # Under --network none RapidOCR cannot fetch anything, so the three
        # models are passed by explicit path. Outside the container the env is
        # unset and the package resolves its own bundled copies.
        params = {}
        for key, var in (("Det", "MIB_DET_MODEL"), ("Rec", "MIB_REC_MODEL"),
                         ("Cls", "MIB_CLS_MODEL")):
            path = os.environ.get(var)
            if path:
                params[f"{key}.model_path"] = path
        tier = os.environ.get("MIB_OCR_TIER")
        if tier:
            from rapidocr.utils.typings import ModelType
            params["Det.model_type"] = ModelType(tier)
            params["Rec.model_type"] = ModelType(tier)
        _ocr = RapidOCR(params=params) if params else RapidOCR()
    return _ocr


def process_pdf(pdf_path: str, out_root: str) -> str:
    import fitz
    import numpy as np

    pdf = Path(pdf_path)
    case_dir = Path(out_root) / pdf.stem
    if any(case_dir.glob("*.json")):
        return "skipped"
    case_dir.mkdir(parents=True, exist_ok=True)

    ocr = _get_ocr()
    doc = fitz.open(pdf)
    for i, page in enumerate(doc):
        pm = page.get_pixmap(matrix=fitz.Matrix(RENDER_SCALE, RENDER_SCALE))
        img = np.frombuffer(pm.samples, dtype=np.uint8).reshape(pm.height, pm.width, 3)
        texts, scores, polys, boxes = [], [], [], []

        def _absorb(result, seen_text, seen_box):
            for text, score, poly in zip(result.txts or [], result.scores or [],
                                         result.boxes if result.boxes is not None else []):
                text = (text or "").strip()
                if not text or text in seen_text:
                    continue
                pts = [[int(p[0]), int(p[1])] for p in poly]
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                box = [min(xs), min(ys), max(xs), max(ys)]
                if any(_overlap_frac(box, b) > BAND_OVERLAP_REJECT for b in seen_box):
                    continue
                texts.append(text)
                scores.append(float(score))
                polys.append(pts)
                boxes.append(box)
                seen_text.add(text)
                seen_box.append(box)

        seen_text, seen_box = set(), []
        _absorb(ocr(img), seen_text, seen_box)

        # Second detection pass over the top of the page.
        #
        # The recognizer is not what loses faint lines -- the DETECTOR is. On
        # MIB-000012 page 3 "Observed flags: biohazard_red" is plainly legible
        # and OCRs cleanly when handed over on its own, yet the whole-page pass
        # returns the four lines above it and omits that one. Dropping
        # box_thresh to 0.1 or det thresh to 0.08 changes nothing, so it is not
        # a score cutoff -- it is the resize. RapidOCR scales the short side up
        # to at least 736 and caps the long side at 2000, so a 1702x2202 render
        # is SHRUNK by 0.909 while a 1702x790 band is ENLARGED by 1.05. That
        # 1.16x swing decides whether a faint line survives segmentation.
        #
        # Every field on these forms lives in the top third, and the band
        # starts at y=0 so its boxes need no offset. Measured on the 1,000
        # training packets: 509 lines added, 309 field values fixed against 44
        # broken, 8 adjudications changed and all 8 correct, no new false
        # approvals. The page is already rendered here, so the extra cost is
        # one OCR call on a third of a page.
        if os.environ.get("MIB_BAND_PASS", "1") != "0":
            band = img[: int(img.shape[0] * BAND_FRACTION), :]
            if band.shape[0] >= 60:
                _absorb(ocr(band), seen_text, seen_box)

        # Third pass: re-read the lines the recognizer was unsure about, with
        # more of the page around them.
        #
        # A detector box that is a few pixels too tall swallows the table rule
        # under the line and the recognizer chokes on it. MIB-000896 page 4 is
        # the clean example: "Species Match: ORION_GRAYS" gets a 30px box and
        # reads at 1.00, the line directly below it gets a 36px box and reads
        # as 'Oese  sd' at 0.55 -- while the same pixels handed over with a
        # wider crop read 'Observed flags: biohazard_red' exactly. That one
        # line is a deny-relevant flag, and losing it cost a false approval.
        #
        # Only 2.8% of lines fall below the threshold, and each re-read is a
        # recognition call on a small crop, so the pass is nearly free. The
        # replacement is accepted only when it comes back MORE confident than
        # the original -- a longer string is not evidence of a better read.
        if os.environ.get("MIB_REREAD_PASS", "1") != "0":
            for k, score in enumerate(scores):
                if score >= REREAD_BELOW or len(texts[k]) < 4:
                    continue
                x0, y0, x1, y1 = boxes[k]
                pad = max(4, int((y1 - y0) * REREAD_PAD_Y))
                crop = img[max(0, y0 - pad):min(img.shape[0], y1 + pad),
                           max(0, x0 - REREAD_PAD_X):min(img.shape[1], x1 + REREAD_PAD_R)]
                if crop.size == 0 or crop.shape[0] < 8:
                    continue
                rr = ocr(crop)
                if not (rr.txts and rr.scores):
                    continue
                best = max(zip(rr.scores, rr.txts))
                if best[0] > score and best[1].strip():
                    texts[k] = best[1].strip()
                    scores[k] = float(best[0])
        (case_dir / f"{pdf.stem}_{i}_res.json").write_text(json.dumps({
            "page_index": i,
            "rec_texts": texts,
            "rec_scores": scores,
            "rec_boxes": boxes,
            "rec_polys": polys,
        }))
    doc.close()
    return "done"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    pdfs = sorted(str(p) for p in Path(args.data).glob("*.pdf"))
    if args.limit:
        pdfs = pdfs[: args.limit]
    Path(args.out).mkdir(parents=True, exist_ok=True)
    print(f"{len(pdfs)} PDFs -> {args.out} ({args.workers} workers, RapidOCR v6-small)", flush=True)

    t0 = time.time()
    done = skipped = failed = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_pdf, p, args.out): p for p in pdfs}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                status = fut.result()
            except Exception as exc:
                failed += 1
                print(f"  FAIL {Path(futures[fut]).name}: {type(exc).__name__}: {exc}", flush=True)
                continue
            done += status == "done"
            skipped += status == "skipped"
            if done and done % 50 == 0:
                rate = done / (time.time() - t0)
                print(f"{i}/{len(pdfs)}  {rate:.2f} pdf/s  eta {(len(pdfs)-i)/rate/60:.0f} min", flush=True)

    print(f"finished: {done} done, {skipped} skipped, {failed} failed, "
          f"{(time.time()-t0)/60:.1f} min")


if __name__ == "__main__":
    main()
