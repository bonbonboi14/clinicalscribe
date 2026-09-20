# ADR-0001: SQLite-backed recoverable job queue

- Status: Accepted
- Date: 2026-09-20

## Context

Clinical Scribe must operate offline on a modest Windows CPU host. Heavy AI work must never execute inside FastAPI request handlers, and interrupted work must be recoverable.

## Decision

SQLite is the authoritative store for sessions and queued work. API handlers create durable `jobs` rows and return promptly. A separate worker claims jobs using short transactions, a lease owner, and an expiry time. WAL mode, foreign keys, busy timeout, bounded retries, and idempotent pipeline stages are required.

## Consequences

The system has no external queue dependency and survives process restarts. Worker concurrency remains deliberately conservative. Later schema evolution must use forward migrations and preserve audit history.

