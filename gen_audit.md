# Generality audit — what this pipeline rests on

Measured by switching each element off and rescoring the 1,000 packets.

## Rules that come from the FIELD MANUAL (a contract, not a sample)

    visa classes (5)          XW-1 XW-2 DIP-1 MED-3 TRANSIT-7
    fee statuses (4)          paid waived unpaid unknown
    risk flags (8)            all eight, deny set and review set
    revoked sponsors (3)      SPN-0007 SPN-0139 SPN-4040
    embargo worlds            TRAPPIST-1e, Eris Relay
    trust ladder order        adjudicator note > intake > biometric > ...
    hidden text is untrusted  the whole of step 1's contract

These carry no regeneration risk: a new batch drawn by the same generator is
bound by the same manual.

## Rules that come from OCR PHYSICS or document structure

    band pass             the detector shrinks a tall page and enlarges a band
    low-confidence reread a too-tall box swallows the rule under the line
    rn -> m repair        the two render near-identically at these sizes
    fuzzy label matching  the label is small and repeated, so it chews first
    injection quarantine  wording and CSV shape, not specific strings
    soft suspect flags    keep the line, rank it last
    variant by confidence a garble reads low, a decoy reads high

These describe how the rasteriser and the recogniser behave, not what this
particular corpus happens to contain.

## Rules MINED from the training labels — the actual exposure

    field priors            +1.25   modal value where nothing was read
    5 weak revoked sponsors +0.32   two non-DIP-1 cases each; noise floor
    TRUST_BY_FIELD          in the mix, measured on conflict cases only
    STALE_BEFORE            2026-01-01
    per-path confidences    calibration table
    HOME_WORLDS /
    SPECIES_CODES /
    DECLARED_PURPOSES       the manual does not enumerate these

  About 1.6 points of 126.48 -- 1.3% -- would move if a regenerated batch
  broke these assumptions. Nothing else in the pipeline is fitted.

## Unknown values survive rather than being forced

    home_world        'Kepler-442b'         kept as itself
    species_code      'NEW_SPECIES_X'       kept as itself
    declared_purpose  'terraforming survey' kept as itself
    sponsor_id        'SPN-9999'            kept as itself
    applicant_name    any well-formed pair  kept as itself
    arrival_date      any valid ISO date    kept as itself
    visa_class        'XW-9'                DROPPED -- one edit from both
                                            XW-1 and XW-2, so tie-rejection
                                            refuses to guess. Correct: a
                                            silent wrong snap is worse.
    fee_status        'deferred'            DROPPED -- the manual enumerates
                                            exactly four, so an unknown fifth
                                            is genuinely unknown.

## Nothing is keyed to a case

    Case IDs in the runtime: 3, all inside comments as evidence citations.
    Zero trained model artifacts. Four float thresholds in the whole pipeline
    (the competitor's has 36 and eight trained artifacts).
