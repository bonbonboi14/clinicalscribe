# ADR-0006: Versioned speaker and language artefacts

## Status

Accepted for Phase 3.

## Context

Phase 2 made transcript rows and raw JSON immutable. Phase 3 must add automatic diarization, clinician correction, multilingual preservation, and optional English translation without writing speaker IDs, cleaned text, or translations back into that source.

## Decision

- `DiarizationEngine` is a worker-only interface. `CpuAcousticDiarizationEngine` is the CPU baseline and may be replaced by pyannote or another implementation.
- Machine labels (`SPEAKER_00`, `SPEAKER_01`) never imply Doctor or Patient. Clinical roles are explicit clinician-reviewed `SpeakerRole` values.
- `diarization_runs` records the engine and source transcript.
- `speakers` holds stable automatic identities. `speaker_revisions` holds append-only names and roles.
- `segment_speaker_assignments` is append-only. Corrections and merges add a revision and preserve the automatic assignment.
- `transcript_artifacts` records content-addressed immutable `RAW_TRANSCRIPT`, `CLEAN_TRANSCRIPT`, `TRANSLATED_TRANSCRIPT`, and `SPEAKER_LABELLED_TRANSCRIPT` files.
- Per-segment language and original text are copied into derived views for provenance. Source transcript rows and raw files are never changed.
- Translation is optional, local-only, and explicitly configured. Untranslated non-English segments remain source-language text with a status; they are never falsely labelled as English.
- FastAPI only reads review projections and appends clinician corrections. Acoustic diarization and translation run in the separate worker.

## Shared interface migration

OLD: `core/diarization` and `core/multilingual` were placeholder packages; the mutable `Speaker` model/table was the only speaker contract and transcript segments had no derived artefact boundary.

NEW: `DiarizationEngine.diarize(audio_path, segments, *, session_id) -> list[SpeakerTurn]`; `LanguageProcessor.process(segments) -> list[SegmentLanguageMetadata]`; append-only speaker and assignment revision tables; four typed transcript artefact streams. `core.multilingual` re-exports `core.languages` for compatibility.

REASON: automatic labels, clinician corrections, translation, and cleaned text must evolve independently while original transcript content remains immutable and auditable.

MIGRATION: existing `TranscriptSegment`, `TranscriptionEngine`, transcript rows, and raw JSON remain unchanged. Schema migration v4 backfills one `DIARIZATION` job for the latest existing transcript per session. Existing imports from `core.multilingual` continue to work. New code should use `core.languages`. Callers must treat `speakers` as stable identities and append `speaker_revisions` instead of updating speaker rows.

## Consequences

The lightweight acoustic baseline is suitable for an offline CPU deployment but is not equivalent to a production speaker-embedding model. Clinician review remains mandatory. A local translation model is an optional installation and must be provisioned separately.
