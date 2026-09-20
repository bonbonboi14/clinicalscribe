# ADR-0003: Differential diagnosis is optional and isolated

- Status: Accepted
- Date: 2026-09-20

## Context

Differential suggestions may assist review but create additional clinical risk and must not contaminate the medical note.

## Decision

The differential engine is disabled by default and enabled only through explicit configuration and clinician action. It consumes validated documented facts only, writes a separate versioned artefact, always displays its disclaimer, and cannot change clerking sheets, notes, or treatment plans.

## Consequences

Core documentation works without the feature. The UI and export layer must visually separate differentials and preserve the enabled-at-generation state.

