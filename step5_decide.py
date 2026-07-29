"""Rules engine v1: parsed records -> adjudication + confidence -> predictions.jsonl.

Implements FIELD_MANUAL.md policy in priority order:

  1. Visible adjudicator note finding wins (top of the trust ladder).
  2. Disqualifying risk flags -> DENIED.
  3. Revoked sponsor -> DENIED (public list; more may be learned from training).
  4. Fee: unpaid without waiver -> DENIED; unknown -> NEEDS_REVIEW.
  5. TRANSIT-7 asking for work -> DENIED (manual: "usually").
  6. Review-only flags, missing/contradictory key evidence -> NEEDS_REVIEW.
  7. Everything clean -> APPROVED.

Confidence is per-case: strong single-source rulings score high, murky
evidence scores low; boobytrapped/degraded packets get a haircut. Calibrated
against Brier scoring (20 pts) — honesty beats bravado.

Usage:
    cd solution
    uv run python step5_decide.py --parsed parsed --out predictions.jsonl
"""

import argparse
import json
from pathlib import Path

DENY_FLAGS = {"memory_tampering", "planetary_embargo", "active_warrant", "biohazard_red"}
REVIEW_FLAGS = {"identity_conflict", "sponsor_mismatch", "illegible_biometrics", "rescinded_denial"}
#: Worlds under a standing embargo — every packet from them carries the flag,
#: whether or not the biometric slip that would list it survived.
EMBARGOED_WORLDS = {"TRAPPIST-1e", "Eris Relay"}

REVOKED_SPONSORS = {"SPN-0007", "SPN-0139", "SPN-4040"}  # public manual list
# Learned from training labels: overwhelmingly denied despite clean packets
# (SPN-7331: 14 denied / 5 approved; SPN-9090: 11 / 2). The manual warns
# "other revoked sponsors may appear in examples."
REVOKED_SPONSORS |= {"SPN-7331", "SPN-9090", "SPN-2718"}  # 2718: 13 denied / 3 approved
# Weaker suspects: every training appearance denied, zero approvals
# (2-3 cases each). Deny-by-default is safe: a wrong denial costs 0 points,
# only a wrong approval is catastrophic.
REVOKED_SPONSORS |= {"SPN-1720", "SPN-1934", "SPN-3417", "SPN-4699", "SPN-6368", "SPN-4146"}

# Stale-arrival cutoff: packets were received ~2026-06-29 (PDF creation date);
# manual: stale if arrival >180 days before receipt, DIP-1 exempt. Training
# data confirms: arrival < 2026-01-01 & non-DIP-1 -> 36/36 DENIED.
STALE_BEFORE = "2026-01-01"
WORK_PURPOSES = {"work", "technical work", "reactor maintenance", "field repair", "xenobotany"}

# Confidence = measured accuracy of each decision path on the 1,000 training
# cases (Brier-optimal honesty: confidence should equal P(correct)).
CALIBRATION = {
    "deny_flag": 0.97,
    "transit7_work": 0.96,
    "adjudicator_note": 0.96,
    "unpaid_fee": 0.96,
    "stale_arrival": 0.95,
    "fee_unknown": 0.93,
    "review_flag": 0.91,
    "registry_embargo": 0.90,
    "transit7_unclear": 0.75,
    "waived_no_waiver": 0.38,
    "no_identity_docs": 0.17,
    "med3_no_biometric": 0.14,
    "revoked_sponsor": 0.72,
    "clean": 0.61,
    "redacted_evidence": 0.38,
    "missing_fee": 0.32,
    "missing_visa": 0.22,
    "missing_name": 0.15,
    "missing_sponsor": 0.10,
}

OUTPUT_FIELDS = [
    "case_id", "applicant_name", "species_code", "home_world", "visa_class",
    "sponsor_id", "arrival_date", "declared_purpose", "risk_flags", "fee_status",
]


def adjudicate(parsed: dict):
    """Return (adjudication, confidence, reason)."""
    rec = parsed["record"]
    signals = parsed["signals"]
    flags = set(rec.get("risk_flags", "none").split("|")) - {"none", ""}

    fee = rec.get("fee_status")
    visa = rec.get("visa_class")
    sponsor = rec.get("sponsor_id")
    waiver = rec.get("waiver_code", "")
    has_waiver = bool(waiver) and waiver.upper() not in ("N/A", "NA", "NONE", "")

    # Packet-quality haircut applied at the end.
    murky = signals["boobytrapped"] or signals["redactions"] or signals["foreign_pages"]

    def conf(base):
        return round(max(0.5, base - (0.1 if murky else 0.0)), 2)

    # 1. Adjudicator note is the strongest visible evidence.
    finding = rec.get("adjudicator_finding")
    if finding in ("APPROVED", "DENIED", "NEEDS_REVIEW"):
        return finding, conf(0.93), "adjudicator_note"

    # 2. Disqualifying flags.
    deny_hits = flags & DENY_FLAGS
    if deny_hits:
        return "DENIED", conf(0.92), f"deny_flag:{'|'.join(sorted(deny_hits))}"

    # 3. Revoked sponsor — but DIP-1 needs no sponsor at all (FIELD_MANUAL:
    # "An applicant needs a valid SPN-#### sponsor unless they are applying
    # under DIP-1"), so a revoked one cannot disqualify a diplomat. Training
    # confirms it exactly:
    #     revoked + non-DIP-1 : 79 DENIED, 0 anything else
    #     revoked + DIP-1     : 21 APPROVED, 3 NEEDS_REVIEW, 0 DENIED
    if sponsor in REVOKED_SPONSORS and visa != "DIP-1":
        return "DENIED", conf(0.92), f"revoked_sponsor:{sponsor}"

    # 3a2. Registry embargo: 'EMBARGO REVIEW' status -> 30/32 training cases
    # DENIED, remaining 2 NEEDS_REVIEW, zero approvals. Deny is safe.
    if "EMBARGO" in (rec.get("registry_status") or ""):
        return "DENIED", conf(0.88), "registry_embargo"

    # 3b. Stale arrival (DIP-1 exempt).
    arrival = rec.get("arrival_date")
    if visa != "DIP-1" and arrival and arrival < STALE_BEFORE:
        return "DENIED", conf(0.9), "stale_arrival"

    # 4. Fee rules.
    if fee == "unpaid" and not has_waiver:
        return "DENIED", conf(0.88), "unpaid_fee"
    if fee == "unknown":
        return "NEEDS_REVIEW", conf(0.8), "fee_unknown"
    # Manual: waived acceptable only for DIP-1 or a visible hardship waiver.
    # Training: non-DIP-1 waived without a waiver code -> 3/32 approved only.
    if fee == "waived" and visa != "DIP-1" and not has_waiver:
        return "NEEDS_REVIEW", conf(0.5), "waived_no_waiver"

    # 5. TRANSIT-7 with a work purpose.
    if visa == "TRANSIT-7":
        purpose = rec.get("declared_purpose", "")
        if any(w in purpose for w in ("work", "repair", "maintenance", "botany", "research")):
            return "DENIED", conf(0.8), "transit7_work"
        # Training truth: TRANSIT-7 with unclear purpose is still DENIED 3/4.
        return "DENIED", conf(0.75), "transit7_unclear"

    # 6. Review conditions.
    if flags & REVIEW_FLAGS:
        return "NEEDS_REVIEW", conf(0.82), f"review_flag:{'|'.join(sorted(flags & REVIEW_FLAGS))}"
    if signals["redactions"]:
        return "NEEDS_REVIEW", conf(0.72), "redacted_evidence"

    # Missing key evidence: sponsor required unless DIP-1; core identity needed.
    if visa is None:
        return "NEEDS_REVIEW", conf(0.66), "missing_visa"
    if visa != "DIP-1" and sponsor is None:
        return "NEEDS_REVIEW", conf(0.7), "missing_sponsor"
    if rec.get("applicant_name") is None:
        return "NEEDS_REVIEW", conf(0.66), "missing_name"
    if fee is None:
        return "NEEDS_REVIEW", conf(0.62), "missing_fee"

    # 6b. Structural caution: packets missing their verification documents
    # cannot be confidently approved. Training (clean-path populations):
    #   no biometric AND no registry -> REVIEW beats APPROVE 70 vs 24 raw
    #   MED-3 without biometric (no biohazard check) -> 124 vs 118, and
    #   removes 16 catastrophic false approvals.
    kinds = set(parsed.get("page_kinds", {}).values())
    if "biometric" not in kinds and "registry" not in kinds:
        return "NEEDS_REVIEW", conf(0.17), "no_identity_docs"
    if visa == "MED-3" and "biometric" not in kinds:
        return "NEEDS_REVIEW", conf(0.14), "med3_no_biometric"

    # 7. Clean.
    return "APPROVED", conf(0.88), "clean"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parsed", default="parsed")
    ap.add_argument("--out", default="predictions.jsonl")
    args = ap.parse_args()

    lines = []
    reasons = {}
    for f in sorted(Path(args.parsed).glob("MIB-*.json")):
        parsed = json.loads(f.read_text())
        rec = parsed["record"]
        adjudication, confidence, reason = adjudicate(parsed)
        confidence = CALIBRATION.get(reason.split(":")[0], confidence)
        reasons[reason] = reasons.get(reason, 0) + 1

        row = {k: (rec.get(k) or "") for k in OUTPUT_FIELDS}
        row["risk_flags"] = rec.get("risk_flags") or "none"
        # Two flags are conditions rather than printed text, so they survive even
        # when the biometric slip that would have listed them is missing:
        #   registry status EMBARGO      -> planetary_embargo (72% in training)
        #   an embargoed home world      -> planetary_embargo. The embargo is a
        #     property of the world, and training is unanimous:
        #     TRAPPIST-1e 32/32, Eris Relay 18/18; every other world <= 6.5%.
        derived = set()
        if "EMBARGO" in (rec.get("registry_status") or ""):
            derived.add("planetary_embargo")
        if rec.get("home_world") in EMBARGOED_WORLDS:
            derived.add("planetary_embargo")
        if derived:
            flags = (set(row["risk_flags"].split("|")) - {"none", ""}) | derived
            row["risk_flags"] = "|".join(sorted(flags))
        row["fee_status"] = rec.get("fee_status") or "unknown"
        # Two fields have a required SHAPE but no legal "unknown" the way
        # fee_status does, and scripts/validate_submission.py rejects a blank:
        #   invalid sponsor_id ''      invalid arrival_date ''
        # The challenge's own reference solution answers this by emitting
        # sentinels (examples/offline_baseline/solution.py):
        #   "sponsor_id": "SPN-0000",  "arrival_date": "1900-01-01"
        # Both are unreachable by construction — SPN-0000 appears in 0 of the
        # 1,000 labels, and real arrivals run 2025-05-22 to 2026-07-12 — so a
        # sentinel can never accidentally score. It says "not found" in the only
        # vocabulary the format allows, rather than guessing at a real value.
        row["sponsor_id"] = rec.get("sponsor_id") or "SPN-0000"
        row["arrival_date"] = rec.get("arrival_date") or "1900-01-01"
        row["adjudication"] = adjudication
        row["confidence"] = confidence
        lines.append(json.dumps(row))

    Path(args.out).write_text("\n".join(lines) + "\n")
    print(f"wrote {len(lines)} predictions -> {args.out}")
    print("decision paths:", dict(sorted(reasons.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
