# ADR-0005: Recoverable CPU transcription worker

- Status: Accepted
- Date: 2026-09-21

## Context

Completed recordings need multilingual, timestamped transcription on a CPU-only Windows baseline. Original audio and raw transcript output are immutable, and heavy decoding or inference cannot run in FastAPI handlers.

## Decision

Successful audio assembly creates one durable `TRANSCRIPTION` job. The separate worker leases the job, records `TRANSCRIPTION_STARTED`, renews its lease during long inference, preprocesses a derived audio copy, and invokes the abstract `TranscriptionEngine`. The first adapter is Faster Whisper with FAST (`tiny`), BALANCED (`small`), and ACCURATE (`medium`) CPU/int8 profiles.

Every segment retains timestamps, detected source language, original text, and confidence. The canonical raw JSON file is content-addressed and append-only; its path and SHA-256 are stored in an immutable transcript row. Completion records `TRANSCRIPTION_COMPLETE`. Failures are retried from SQLite up to the configured limit.

## Shared interface migration

- OLD: `TranscriptionEngine` was a `Protocol` with `transcribe(Path, session_id: str)` and no execution path.
- NEW: `TranscriptionEngine` is an abstract class with a profile and `transcribe(Path, session_id: UUID | str)`; assembly idempotently enqueues a worker job.
- REASON: A concrete typed engine boundary and durable API-to-worker handoff are required for reliable Phase 2 processing.
- MIGRATION: UUID-shaped string callers remain valid. Schema v3 adds nullable `jobs.stage`, `transcripts.raw_transcript_path`, and `transcripts.raw_checksum_sha256` columns. Existing rows and Phase 1 HTTP endpoints are unchanged.

## Consequences

The first local run downloads the selected model weights unless they are already cached. FAST is suitable for rapid drafts, BALANCED is the default, and ACCURATE trades substantially more CPU time and memory for quality. Preprocessed audio is a derived artefact; original recordings are never changed.
