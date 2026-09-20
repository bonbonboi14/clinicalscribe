# ADR-0004: Manual, constrained GitHub integration

- Status: Accepted
- Date: 2026-09-20

## Context

Approved notes may be pushed to GitHub, but credentials and sensitive source artefacts require strict boundaries.

## Decision

GitHub push is manual only and limited to clinician-approved note artefacts. Audio is never eligible. The personal access token is read only from `CLINICAL_SCRIBE_GITHUB_TOKEN`; it is never written to source, configuration, logs, or Git remotes. The repository URL, branch, and remote name are non-secret configuration. Clipboard and text export remain available offline.

## Consequences

An explicit review gate and audit event are required before every push. Operators configure the destination URL separately and supply the token at runtime. Failures never remove local artefacts.

