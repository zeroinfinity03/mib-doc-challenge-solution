"""Trap detector: scan every PDF's embedded text layer for hidden content.

The OCR pass (step2_ocr.py) reads what is VISIBLE — trusted evidence. This
scanner reads what is EMBEDDED — the machine text layer — and flags content
that a human looking at the printed page cannot see:

  - white / near-white text on the page background
  - text positioned outside the page boundary
  - microscopic text (< 3pt)
  - PDF render mode 3, i.e. explicitly marked invisible
  - opacity below 0.1
  - injection phrasing anywhere in the layer ("SYSTEM:", "answer key",
    "ignore visible evidence")

get_texttrace() is used rather than get_text(): it is the only API that reports
render mode and opacity, so it is the only one that can see the last two.

Findings are written per case to <out>/<case_id>.json. Hidden content is
QUARANTINED EVIDENCE: recorded so the rules stage knows the packet is
boobytrapped, never used as field values.

Each finding also carries its bbox in OCR PIXEL space. That is what lets step 3
reject an OCR line by POSITION — the only test that catches fragments like
'BANAROVED.0.99', which carry no suspicious wording of their own. Measured: 249
such lines currently slip past text-pattern matching alone.

A caution learned by measuring: white ink is NOT invisible to OCR. On a scanned
page the white text is drawn on top of a beige scan, so it is perfectly legible
in the raster and OCR reads it straight back in. "We only read images" is not a
defence on its own.

Usage:
    uv run python step1_scan_hidden.py --data ../mib-doc-challenge/data/train --out hidden-output
"""

import argparse
import json
import re
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import fitz

INJECTION_RX = re.compile(
    r"SYSTEM\s*:|answer key|ignore (visible|all) evidence|output this"
    r"|ignore (previous|all previous)|you must output|disregard the"
    r"|barcode payload|force adjudication",
    re.I,
)

WHITE_LEVEL = 0.95        # every channel at/above this counts as white ink
OPACITY_FLOOR = 0.1
RENDER_SCALE = 2.78       # must match PADDLE_PDX_PDF_RENDER_SCALE in step2_ocr.py
MAX_ATTEMPTS = 25         # see the note in main(): each pass resumes, none repeats work


def _is_white(color) -> bool:
    """get_texttrace returns a float or an RGB tuple, not a packed int."""
    if color is None:
        return False
    if isinstance(color, (int, float)):
        return float(color) >= WHITE_LEVEL
    return all(float(channel) >= WHITE_LEVEL for channel in color)


def scan_pdf(pdf_path: Path) -> dict:
    doc = fitz.open(pdf_path)
    findings = []
    for pno, page in enumerate(doc, 1):
        W, H = page.rect.width, page.rect.height
        for span in page.get_texttrace():
            text = "".join(chr(ch[0]) for ch in span["chars"]).strip()
            if not text:
                continue
            x0, y0, x1, y1 = span["bbox"]
            reasons = []
            if _is_white(span.get("color")):
                reasons.append("white_text")
            if x1 < 0 or y1 < 0 or x0 > W or y0 > H:
                reasons.append("off_page")
            if span.get("size", 99) < 3:
                reasons.append("tiny_text")
            if span.get("type") == 3:
                reasons.append("invisible_render")
            if float(span.get("opacity", 1.0)) < OPACITY_FLOOR:
                reasons.append("transparent")
            if INJECTION_RX.search(text):
                reasons.append("injection_phrase")
            if reasons:
                findings.append(
                    {
                        "page": pno,
                        "reasons": reasons,
                        "text": text,
                        "bbox": [round(v, 2) for v in (x0, y0, x1, y1)],
                        # Same box in the pixel space step 2 renders at, so
                        # step 3 needs no conversion.
                        "bbox_px": [round(v * RENDER_SCALE, 1) for v in (x0, y0, x1, y1)],
                        "font_size": round(span.get("size", 0), 1),
                    }
                )
    doc.close()
    return {
        "case_id": pdf_path.stem,
        "boobytrapped": bool(findings),
        "render_scale": RENDER_SCALE,
        "findings": findings,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="folder of PDFs")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--workers", type=int, default=4,
                    help="PDFs in parallel — containment for the refcount bug, not speed")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(Path(args.data).glob("*.pdf"))

    # Resumable, and it has to be. get_texttrace() has a refcount bug in
    # PyMuPDF's C extension: it corrupts CPython's reference counting until the
    # interpreter aborts outright with
    #     Fatal Python error: none_dealloc: deallocating None:
    #     bug likely caused by a refcount error in a C extension
    # That is not an exception, so nothing can catch it — the process simply
    # dies. It is cumulative, not packet-specific: every PDF is fine on its own,
    # and it took 0 crashes over 1,000 documents but 2 over 5,000.
    #
    # get_text("dict") does not have the bug, and was tried as a replacement.
    # It cannot do the job: get_text() clips to the page, so it never sees the
    # off-page injections (3,782 of them in the validation set), and its packed
    # RGB colour compares numerically, which makes a red 0xff0000 stamp read as
    # "white" and flags 85 packets that carry nothing hidden at all.
    #
    # So the bug is worked around rather than avoided: skip what is already
    # written, and let the caller re-run until it completes.
    done = {p.stem for p in out.glob("*.json")
            if "render_scale" in p.read_text(errors="ignore")}
    todo = [p for p in pdfs if p.stem not in done]
    print(f"{len(todo)} of {len(pdfs)} to scan ({len(done)} already done)", flush=True)

    # Workers are not for speed here — the whole scan is seconds either way —
    # they are the containment. The refcount damage is per-process, so N workers
    # means each process only opens 1/N of the corpus and dies less often:
    #     1 worker,  1,000 PDFs -> crashed at 354
    #     4 workers, 1,000 PDFs -> clean
    #     1 process, 5,000 PDFs -> crashed
    #     4 workers, 5,000 PDFs -> 2 crashes, both resumed
    #
    # The retry lives here rather than in the caller so the judge's container
    # gets it too. A dying worker surfaces in the parent as BrokenProcessPool,
    # which the parent survives, so it is enough to rebuild the pool and carry
    # on with whatever is still missing.
    trapped = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        pending = [p for p in todo if not (out / f"{p.stem}.json").exists()]
        if not pending:
            break
        try:
            if args.workers > 1:
                with ProcessPoolExecutor(max_workers=args.workers) as pool:
                    for result in pool.map(scan_pdf, pending, chunksize=8):
                        trapped += result["boobytrapped"]
                        (out / f"{result['case_id']}.json").write_text(
                            json.dumps(result, indent=1))
            else:
                for pdf in pending:
                    result = scan_pdf(pdf)
                    trapped += result["boobytrapped"]
                    (out / f"{pdf.stem}.json").write_text(json.dumps(result, indent=1))
        except BrokenProcessPool:
            print(f"  worker died (PyMuPDF refcount bug), attempt {attempt}; "
                  f"{len(pending)} were pending — resuming", flush=True)
            continue

    missing = [p for p in todo if not (out / f"{p.stem}.json").exists()]
    if missing:
        raise SystemExit(
            f"gave up after {MAX_ATTEMPTS} attempts, {len(missing)} PDFs unscanned")

    print(f"finished: {len(todo)} PDFs scanned, {trapped} contain hidden content -> {out}/")


if __name__ == "__main__":
    main()
