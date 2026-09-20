from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY,
    external_id TEXT,
    demographics_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    patient_id TEXT REFERENCES patients(id),
    status TEXT NOT NULL,
    source_language TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    original_filename TEXT,
    audio_content_type TEXT,
    final_sequence_number INTEGER CHECK (final_sequence_number IS NULL OR final_sequence_number >= 0),
    assembled_audio_path TEXT,
    assembled_checksum_sha256 TEXT,
    assembled_size_bytes INTEGER CHECK (assembled_size_bytes IS NULL OR assembled_size_bytes >= 0),
    assembled_at TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)
);

CREATE TABLE IF NOT EXISTS audio_chunks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    sequence_number INTEGER NOT NULL CHECK (sequence_number >= 0),
    storage_path TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    uploaded_at TEXT NOT NULL,
    is_final INTEGER NOT NULL DEFAULT 0 CHECK (is_final IN (0, 1)),
    UNIQUE(session_id, sequence_number)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcripts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    source_language TEXT,
    original_text TEXT NOT NULL,
    normalized_text TEXT,
    segments_json TEXT NOT NULL DEFAULT '[]',
    engine TEXT NOT NULL,
    created_at TEXT NOT NULL,
    supersedes_id TEXT REFERENCES transcripts(id),
    UNIQUE(session_id, version)
);

CREATE TABLE IF NOT EXISTS speakers (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    diarization_label TEXT NOT NULL,
    display_name TEXT,
    role TEXT,
    manually_corrected INTEGER NOT NULL DEFAULT 0 CHECK (manually_corrected IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(session_id, diarization_label)
);

CREATE TABLE IF NOT EXISTS structured_facts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT,
    assertion TEXT NOT NULL CHECK (assertion IN ('POSITIVE','NEGATIVE','NOT_MENTIONED','UNCERTAIN')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence_json TEXT NOT NULL DEFAULT '[]',
    original_text TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS examination_mappings (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    original_phrase TEXT NOT NULL,
    formal_term TEXT,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence_json TEXT NOT NULL DEFAULT '[]',
    requires_confirmation INTEGER NOT NULL DEFAULT 1 CHECK (requires_confirmation IN (0, 1)),
    confirmed_by_clinician INTEGER NOT NULL DEFAULT 0 CHECK (confirmed_by_clinician IN (0, 1)),
    flagged_unmappable INTEGER NOT NULL DEFAULT 0 CHECK (flagged_unmappable IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clerking_sheets (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    version INTEGER NOT NULL CHECK (version >= 1),
    structured_json TEXT NOT NULL,
    fact_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    supersedes_id TEXT REFERENCES clerking_sheets(id),
    UNIQUE(session_id, version)
);

CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    clerking_sheet_id TEXT NOT NULL REFERENCES clerking_sheets(id),
    version INTEGER NOT NULL CHECK (version >= 1),
    language TEXT NOT NULL DEFAULT 'en',
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    supersedes_id TEXT REFERENCES notes(id),
    UNIQUE(session_id, version)
);

CREATE TABLE IF NOT EXISTS treatment_plans (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    note_id TEXT REFERENCES notes(id),
    version INTEGER NOT NULL DEFAULT 1,
    items_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    supersedes_id TEXT REFERENCES treatment_plans(id),
    UNIQUE(session_id, version)
);

CREATE TABLE IF NOT EXISTS differentials (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    version INTEGER NOT NULL DEFAULT 1,
    enabled_at_generation INTEGER NOT NULL DEFAULT 0 CHECK (enabled_at_generation IN (0, 1)),
    candidates_json TEXT NOT NULL DEFAULT '[]',
    fact_ids_json TEXT NOT NULL DEFAULT '[]',
    disclaimer TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, version)
);

CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    artefact_type TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    content TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(name, artefact_type, version)
);

CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    note_id TEXT NOT NULL REFERENCES notes(id),
    format TEXT NOT NULL,
    destination TEXT,
    status TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    checksum_sha256 TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    artefact_type TEXT NOT NULL,
    artefact_id TEXT,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS validation_findings (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    artefact_type TEXT NOT NULL,
    artefact_id TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('SUPPORTED','UNSUPPORTED','CONTRADICTED','UNCERTAIN')),
    evidence_json TEXT NOT NULL DEFAULT '[]',
    explanation TEXT,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS prevent_audio_chunk_update
BEFORE UPDATE ON audio_chunks
BEGIN
    SELECT RAISE(ABORT, 'audio chunks are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_audio_chunk_delete
BEFORE DELETE ON audio_chunks
BEGIN
    SELECT RAISE(ABORT, 'audio chunks are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_transcript_update
BEFORE UPDATE ON transcripts
BEGIN
    SELECT RAISE(ABORT, 'transcripts are immutable; create a new version');
END;

CREATE TRIGGER IF NOT EXISTS prevent_transcript_delete
BEFORE DELETE ON transcripts
BEGIN
    SELECT RAISE(ABORT, 'transcripts are immutable');
END;

CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, available_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_audio_chunks_session ON audio_chunks(session_id, sequence_number);
CREATE INDEX IF NOT EXISTS idx_transcripts_session ON transcripts(session_id, version);
CREATE INDEX IF NOT EXISTS idx_facts_session ON structured_facts(session_id, category);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_events(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_validation_artefact ON validation_findings(artefact_type, artefact_id);
"""


class Database:
    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
        journal_mode: str = "WAL",
        foreign_keys: bool = True,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.journal_mode = journal_mode
        self.foreign_keys = foreign_keys

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        connection.execute(f"PRAGMA journal_mode = {self.journal_mode}")
        connection.execute(f"PRAGMA foreign_keys = {'ON' if self.foreign_keys else 'OFF'}")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(DDL)
            self._migrate_v2(connection)
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _migrate_v2(connection: sqlite3.Connection) -> None:
        """Add resumable-upload state while preserving all existing audio rows."""
        session_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(sessions)")
        }
        additions = {
            "original_filename": "TEXT",
            "audio_content_type": "TEXT",
            "final_sequence_number": "INTEGER CHECK (final_sequence_number IS NULL OR final_sequence_number >= 0)",
            "assembled_audio_path": "TEXT",
            "assembled_checksum_sha256": "TEXT",
            "assembled_size_bytes": "INTEGER CHECK (assembled_size_bytes IS NULL OR assembled_size_bytes >= 0)",
            "assembled_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in session_columns:
                connection.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")

        table_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'audio_chunks'"
        ).fetchone()
        table_sql = table_sql_row["sql"] if table_sql_row else ""
        normalized_sql = "".join(table_sql.lower().split())
        if "unique(session_id,checksum_sha256)" not in normalized_sql:
            return

        # A repeated byte sequence at two positions is valid audio. Phase 0's checksum
        # uniqueness constraint prevented that, so rebuild the table without it.
        connection.executescript(
            """
            DROP TRIGGER IF EXISTS prevent_audio_chunk_update;
            DROP TRIGGER IF EXISTS prevent_audio_chunk_delete;
            DROP INDEX IF EXISTS idx_audio_chunks_session;
            CREATE TABLE audio_chunks_v2 (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                sequence_number INTEGER NOT NULL CHECK (sequence_number >= 0),
                storage_path TEXT NOT NULL,
                checksum_sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                uploaded_at TEXT NOT NULL,
                is_final INTEGER NOT NULL DEFAULT 0 CHECK (is_final IN (0, 1)),
                UNIQUE(session_id, sequence_number)
            );
            INSERT INTO audio_chunks_v2
                (id, session_id, sequence_number, storage_path, checksum_sha256,
                 size_bytes, uploaded_at, is_final)
            SELECT id, session_id, sequence_number, storage_path, checksum_sha256,
                   size_bytes, uploaded_at, is_final
            FROM audio_chunks;
            DROP TABLE audio_chunks;
            ALTER TABLE audio_chunks_v2 RENAME TO audio_chunks;
            CREATE TRIGGER prevent_audio_chunk_update
            BEFORE UPDATE ON audio_chunks
            BEGIN
                SELECT RAISE(ABORT, 'audio chunks are immutable');
            END;
            CREATE TRIGGER prevent_audio_chunk_delete
            BEFORE DELETE ON audio_chunks
            BEGIN
                SELECT RAISE(ABORT, 'audio chunks are immutable');
            END;
            CREATE INDEX idx_audio_chunks_session
                ON audio_chunks(session_id, sequence_number);
            """
        )


def initialize_database(path: str | Path, **kwargs: object) -> Database:
    database = Database(path, **kwargs)
    database.initialize()
    return database
