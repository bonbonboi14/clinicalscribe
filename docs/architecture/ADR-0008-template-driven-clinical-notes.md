# ADR-0008: Template-driven clinical notes

## Status

Accepted for Phase 6.

## Context

Clinical notes must be produced from structured extraction through the Medical Clerking Sheet, never from transcript prose. The CPU-only baseline must work offline, while an explicitly configured local Ollama instance may improve presentation without weakening DO-NOT-INFER controls.

## Decision

`core.templates.base.NoteGenerationEngine` is the canonical engine boundary. Its sole clinical input is a `ClerkingSheet`; template selection is constructor configuration. Strict YAML templates define headings, clerking-sheet paths, and rendering styles without executable expressions. The default engine copies values deterministically into English Markdown and TXT. `OllamaEngine` calls only the loopback Ollama API and rejects response lines outside the deterministic source set.

Every rendered clerking-sheet leaf is classified as `SUPPORTED` or `UNCERTAIN` before the note is inserted. Any `UNSUPPORTED` or `CONTRADICTED` claim blocks persistence. Notes are append-only. Approval creates a new immutable revision, and TXT, Markdown, and clipboard export require that approved revision.

The worker progression is `CLERKING_SHEET_GENERATED` → `NOTE_GENERATION_STARTED` → `NOTE_GENERATED`. The API never invokes either note engine.

## Shared interface migration

- OLD: `core.note_generation.base.NoteGenerationEngine` was a structural protocol with no template contract.
- NEW: `core.templates.base.NoteGenerationEngine` is the canonical abstract base; engines receive their validated template at construction and retain `generate(clerking_sheet)`.
- REASON: YAML formats and local Ollama require explicit renderer configuration while preserving the structured-only clinical boundary.
- MIGRATION: the old module re-exports the new class, and existing `generate(clerking_sheet)` call sites remain source-compatible.
