# ADR-0009: Claim-level safety engine

## Status

Accepted for Phase 7.

## Context

Phase 6 verified that rendered note values retained clerking-sheet provenance, but it did not independently extract added claims or expose one validation contract for all clinical artefacts. The safety boundary must distinguish absent, uncertain, negative, and positive evidence and must block fabricated or contradicted claims before clinician presentation.

## Decision

`ClinicalFact.assertion` remains mandatory and uses exactly `POSITIVE`, `NEGATIVE`, `NOT_MENTIONED`, or `UNCERTAIN`. `FactValidator` checks that contract. `ClaimExtractor` extracts classifiable claims without supplying medical knowledge. `ContradictionChecker` compares claim polarity and terminology only with structured facts and explicit aliases. `NoteValidator` returns an immutable `NoteValidationResult` containing a `ValidationFinding` for every extracted claim.

`HallucinationFirewall` rejects any result containing `UNSUPPORTED` or `CONTRADICTED`. It provides validation entry points for clerking sheets, treatment plans, and differentials. `NoteHallucinationFirewall` remains the Phase 6 compatibility facade and delegates to the new validator. The structuring and note workers run the firewall before inserting their artefacts.

Differential candidates are rejected while the feature is disabled. Treatment claims receive no support from diagnosis; they must match documented facts. Examination interpretations are supported only by the interpreter's retained formal mapping and provenance, so a mapped turbinate finding cannot support an added sinusitis claim.

## Shared interface migration

- OLD: `core.templates.validation.NoteHallucinationFirewall.validate(...)` returned a raw list of findings and checked only values copied from the clerking sheet.
- NEW: `core.validation.NoteValidator.validate(...)` returns `NoteValidationResult`; `HallucinationFirewall` supplies common artefact gates. The old note firewall remains available and still returns the findings list.
- REASON: all clinical artefacts need one claim-level classification and blocking contract.
- MIGRATION: existing Phase 6 callers require no changes. New workers and generators should call the relevant `HallucinationFirewall.validate_*` method and persist `result.findings` only after it returns successfully.

## Consequences

- Unsupported or contradicted claims cannot be persisted by integrated generation workers.
- Uncertain claims remain visible with their uncertainty classification.
- The rule-based baseline is deterministic, local, and conservative; synonym expansion requires an explicit reviewed alias rather than implicit medical inference.
