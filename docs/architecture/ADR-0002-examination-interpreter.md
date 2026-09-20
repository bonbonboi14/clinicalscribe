# ADR-0002: Clinical Examination Interpreter

- Status: Accepted
- Date: 2026-09-20

## Context

Spoken examination narration is often informal, multilingual, or ambiguous. Converting it silently to formal terminology could alter clinical meaning.

## Decision

The interpreter is an explicit pipeline stage before fact extraction. Every mapping stores the original phrase, formal term (when available), source evidence, and confidence. Low-confidence mappings require clinician confirmation. Unmappable phrases pass through unchanged and flagged. Original transcript content is immutable.

## Consequences

Formal clinical language becomes traceable and reviewable. The UI must support confirmations and corrections, and all changes produce new artefact versions plus audit events.

