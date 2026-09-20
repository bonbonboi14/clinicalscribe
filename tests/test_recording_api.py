from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from api.main import app
from storage.database import Database


@pytest.fixture
def recording_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    config["database"]["path"] = str(tmp_path / "clinical_scribe.db")
    config["storage"]["audio_directory"] = str(tmp_path / "audio")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("CLINICAL_SCRIBE_CONFIG", str(config_path))
    with TestClient(app) as client:
        yield client, tmp_path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _upload(client: TestClient, session_id: str, sequence: int, data: bytes, *, final: bool = False):
    return client.post(
        f"/api/v1/sessions/{session_id}/chunks",
        params={"sequence_number": sequence, "is_final": str(final).lower()},
        headers={"X-Chunk-SHA256": _sha(data), "Content-Type": "audio/webm"},
        content=data,
    )


def test_session_creation_accepts_no_metadata(recording_client) -> None:
    client, _ = recording_client
    response = client.post("/api/v1/sessions")
    assert response.status_code == 201
    assert response.json()["patient_id"] is None


def test_out_of_order_resume_and_server_assembly(recording_client) -> None:
    client, _ = recording_client
    created = client.post(
        "/api/v1/sessions",
        json={"patient": {"external_id": "local-42"}, "audio_content_type": "audio/webm"},
    )
    session_id = created.json()["id"]

    assert _upload(client, session_id, 0, b"first-").status_code == 200
    final_response = _upload(client, session_id, 2, b"third", final=True)
    assert final_response.status_code == 200
    assert final_response.json()["missing_chunks"] == [1]
    assert final_response.json()["assembled"] is False

    completed = _upload(client, session_id, 1, b"second-")
    assert completed.status_code == 200
    state = completed.json()
    assert state["missing_chunks"] == []
    assert state["assembled"] is True
    assembled = Path(state["assembled_audio_path"])
    assert assembled.read_bytes() == b"first-second-third"
    assert state["assembled_checksum_sha256"] == _sha(b"first-second-third")


def test_chunk_retry_is_idempotent_and_conflicts_are_rejected(recording_client) -> None:
    client, _ = recording_client
    session_id = client.post("/api/v1/sessions", json={}).json()["id"]
    first = _upload(client, session_id, 0, b"same")
    retry = _upload(client, session_id, 0, b"same")
    conflict = _upload(client, session_id, 0, b"changed")
    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json()["already_present"] is True
    assert conflict.status_code == 409


def test_bad_checksum_is_rejected_without_tracking_chunk(recording_client) -> None:
    client, _ = recording_client
    session_id = client.post("/api/v1/sessions", json={}).json()["id"]
    response = client.post(
        f"/api/v1/sessions/{session_id}/chunks",
        params={"sequence_number": 0},
        headers={"X-Chunk-SHA256": "0" * 64},
        content=b"corrupt-in-transit",
    )
    assert response.status_code == 422
    status_response = client.get(f"/api/v1/sessions/{session_id}/upload")
    assert status_response.json()["received_chunks"] == []


def test_ui_is_served(recording_client) -> None:
    client, _ = recording_client
    response = client.get("/")
    assert response.status_code == 200
    assert "Local-first recording" in response.text


def test_phase_zero_database_migrates_without_losing_chunks(tmp_path: Path) -> None:
    path = tmp_path / "phase-zero.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE patients (
            id TEXT PRIMARY KEY, external_id TEXT, demographics_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, patient_id TEXT REFERENCES patients(id), status TEXT NOT NULL,
            source_language TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE audio_chunks (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            sequence_number INTEGER NOT NULL, storage_path TEXT NOT NULL,
            checksum_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
            uploaded_at TEXT NOT NULL, is_final INTEGER NOT NULL DEFAULT 0,
            UNIQUE(session_id, sequence_number), UNIQUE(session_id, checksum_sha256)
        );
        INSERT INTO sessions(id, status, created_at, updated_at)
        VALUES ('session-1', 'UPLOADING', 'now', 'now');
        INSERT INTO audio_chunks
            (id, session_id, sequence_number, storage_path, checksum_sha256, size_bytes, uploaded_at)
        VALUES ('chunk-1', 'session-1', 0, '0.chunk', 'same-checksum', 1, 'now');
        """
    )
    connection.close()

    database = Database(path)
    database.initialize()
    with database.transaction() as migrated:
        assert migrated.execute("SELECT COUNT(*) FROM audio_chunks").fetchone()[0] == 1
        migrated.execute(
            "INSERT INTO audio_chunks(id, session_id, sequence_number, storage_path, "
            "checksum_sha256, size_bytes, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("chunk-2", "session-1", 1, "1.chunk", "same-checksum", 1, "now"),
        )
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(sessions)")}
        assert "assembled_audio_path" in columns
