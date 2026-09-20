# ADR-0002: Clinical Examination Interpreter

- Status: Accepted
- Date: 2026-09-20

## Context

Spoken examination narration is often informal, multilingual, or ambiguous. Converting it silently to formal terminology could alter clinical meaning.

## Decision

The interpreter is an explicit pipeline stage before fact extraction. Every mapping stores the original phrase, formal term (when available), source evidence, and confidence. Low-confidence mappings require clinician confirmation. Unmappable phrases pass through unchanged and flagged. Original transcript content is immutable.

Mappings are deterministic regular-expression rules loaded from specialty YAML files. A match at or above the configured threshold is `CONFIRMED`; a lower-confidence match is `PENDING_REVIEW`; no match is passed through verbatim as `UNRECOGNISED`. Clinician decisions are append-only revisions and never update the base finding.

The durable worker sequence is `DIARIZATION` -> `EXAMINATION_INTERPRETATION` -> `STRUCTURING`. Completion is persisted as `EXAMINATION_INTERPRETATION_COMPLETE`, so a crash can resume without performing mapping in a FastAPI handler.

## Consequences

Formal clinical language becomes traceable and reviewable. The UI must support confirmations and corrections, and all changes produce new artefact versions plus audit events.

