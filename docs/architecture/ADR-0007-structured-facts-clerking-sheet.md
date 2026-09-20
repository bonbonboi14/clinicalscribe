# ADR-0007: Structured facts precede the Medical Clerking Sheet

- Status: Accepted
- Date: 2026-09-21

## Context

Clinical Scribe must not generate a prose note directly from a transcript. Phase 4 needs a complete Medical Clerking Sheet while preserving explicit assertion state and transcript traceability. Missing information must be visible, and generated clinical content must never be completed from general medical knowledge.

## Decision

A separate SQLite `STRUCTURING` job consumes the latest immutable transcript and language artefacts. `ClinicalFactExtractor` first creates immutable `ClinicalFact` rows with assertion state and source segment IDs. `ClerkingSheetGenerator` then maps only those facts into the typed `models.clerking_sheet.ClerkingSheet`.

Every non-placeholder leaf in a sheet carries fact IDs in `evidence_by_field`. `DoNotInferValidator` rejects unknown fact IDs, absent evidence, negated PMH presented as positive history, and any displayed value that is not present in its cited fact. Unmentioned fields display `Not discussed.`; PE displays `Not performed` when there is no positive examination fact.

The worker records `STRUCTURE_COMPLETE` before `CLERKING_SHEET_GENERATED`. Facts and sheets are append-only under SQLite triggers. The session reaches `REVIEW` only after sheet generation.

## Consequences

- Rule-based extraction is intentionally conservative and may leave fields unpopulated.
- Explicit negations remain queryable in structured facts even when a positive-history section excludes them.
- Later extractors may replace the local baseline if they preserve `ClinicalFact`, assertion, provenance, and validation contracts.
- Clinician approval remains outside this phase; the Phase 4 UI is read-only review.
