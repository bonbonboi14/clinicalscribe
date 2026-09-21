# ADR-0010: Optional differential diagnosis engine

## Status

Accepted — Phase 8, implemented after the local Phase 9 commit and verified against Phases 7 and 9.

## Decision

Differential diagnosis is a separate, immutable decision-support artefact. It cannot modify the clerking sheet, clinical note, or treatment plan.

The system-level `diagnosis.enabled` setting is the master switch and defaults to `false`. A nullable per-session setting inherits the system setting; when the system switch is off, the session UI remains visible but its toggle is disabled. Disabling a session cancels pending differential work without deleting previously generated versions.

The engine consumes only `POSITIVE` and `NEGATIVE` `ClinicalFact` records. `UNCERTAIN` and `NOT_MENTIONED` facts are excluded before matching. Negative findings may rank an already supported candidate down but cannot introduce a condition by themselves.

Every candidate must contain:

- condition
- likelihood: `SUPPORTED`, `POSSIBLE`, or `UNLIKELY`
- documented supporting features
- documented features against
- at least one specific `ClinicalFact` evidence reference

Generation requires enough matching evidence to return a ranked list of three to five candidates. Otherwise, the engine returns an enabled result with no candidates and an explicit insufficient-evidence note.

Before persistence, `NoteValidator.validate_differential()` and the hallucination firewall validate the cited features. The worker writes `DIFFERENTIAL_GENERATED` only after validation succeeds.

## Mandatory presentation

When enabled, the review interface always displays:

> This is an AI-generated differential for clinician reference only. It does not replace clinical judgment.

When disabled, it displays:

> Differential diagnosis: [Disabled — enable in settings]

## Shared interface migration

**OLD**: `core.models.Differential` is the Phase 7 loose safety-test artefact using dictionary candidates.

**NEW**: `models.differential.Differential` is a typed candidate and `DifferentialResult` is the generated artefact.

**REASON**: enforce likelihood values, evidence references, toggle state, and mandatory presentation text at the model boundary.

**MIGRATION**: the legacy Phase 7 type remains exported from `core.models`. `ClaimExtractor` and `NoteValidator` accept both shapes. `DifferentialCandidate` aliases the new typed candidate for compatibility with the original Phase 8 handoff vocabulary.

## Consequences

- Default-off behavior is enforced in configuration, job claiming, session settings, and review rendering.
- Differential jobs are never enqueued by the treatment pipeline unless the global and effective session toggles are enabled.
- The initial local rules are deliberately small and conservative; expansion requires clinical review.
