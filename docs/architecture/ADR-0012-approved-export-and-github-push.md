# ADR 0012 Approved Export and GitHub Push

## Status

Accepted for Phase 10.

## Context

Clinical Scribe needs local TXT, Markdown, DOCX, PDF, print, and JSON export plus an optional manual GitHub archive. Export must preserve the existing immutable note approval boundary. A GitHub personal access token may exist only in the `CLINICAL_SCRIBE_GITHUB_TOKEN` process environment. Audio must never enter a push package, even if configuration is altered.

## Decision

`ExportService` loads one immutable approved-note snapshot and renders every local format from it. TXT and Markdown retain the established note content contract. DOCX uses `python-docx`, PDF uses ReportLab, print returns a dedicated printable HTML document, and JSON includes the approved note with its clerking sheet, optional treatment and differential artefacts, and claim validation.

`GitHubPusher` uses GitPython to clone the configured repository into a temporary directory, writes an allowlisted session package below `sessions/<session_id>/`, commits with the required message, and pushes only after an explicit API request. Authentication is supplied through ephemeral Git environment configuration. It is never embedded in the repository URL or written to disk.

The API records a pending export and `GITHUB_PUSH_REQUESTED` before the push. It records `GITHUB_PUSH_SUCCEEDED` or `GITHUB_PUSH_FAILED` afterward. Approval failures are recorded as `GITHUB_PUSH_BLOCKED`. User-facing failures contain no Git command output or credential material.

## Safety invariants

- Every local export and GitHub push requires an approved immutable clinical-note revision.
- The review UI does not expose the push button until the note is approved and GitHub is enabled.
- `auto_push` must be false.
- `push_audio` must be false.
- The pusher accepts only the Phase 10 session file allowlist and rejects audio paths and audio extensions.
- Differential diagnosis is included only when an enabled result exists, and its mandatory disclaimer is rendered unchanged.
- GitHub token values are never persisted or logged.

## Compatibility migration

The Phase 0 GitHub keys `repository_url`, `manual_push_only`, `approved_notes_only`, and `include_audio` are accepted as input aliases. They normalize to `repo_url`, `auto_push`, the invariant approval gate, and `push_audio`. Unsafe legacy values are rejected.

## Consequences

No schema migration is required. Existing `exports` and `audit_events` rows capture all Phase 10 outcomes, so schema version 9 remains compatible. GitHub pushes execute synchronously after a manual request; future work may move network transfer to a recoverable non-AI worker without weakening the approval or audit boundaries.
