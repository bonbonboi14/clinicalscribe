from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from api.main import app
from config import load_config
from core.examination import ExaminationInterpreter, PhraseToClinicalTermMapper
from models.examination_finding import ExaminationFindingStatus
from storage.database import Database
from storage.jobs import enqueue_examination_interpretation_job
from worker.examination import ExaminationInterpretationWorker


@pytest.mark.parametrize(("phrase", "expected"), [
    ("left nose swollen", "left inferior turbinate hypertrophy"),
    ("right nose blocked", "right inferior turbinate hypertrophy"),
    ("nose swollen on both sides", "bilateral inferior turbinate hypertrophy"),
    ("pus in throat", "tonsillar exudate"),
    ("white stuff on the tonsils", "tonsillar exudate"),
    ("tonsils big", "tonsillar enlargement"),
    ("tonsils grade 3", "tonsillar enlargement (grade 3)"),
    ("eardrums look fine", "bilateral tympanic membranes intact, no perforation"),
    ("eardrums intact", "bilateral tympanic membranes intact, no perforation"),
    ("left ear red", "left acute otitis media"),
    ("fluid behind the right eardrum", "right acute otitis media"),
    ("both ears red", "bilateral acute otitis media"),
    ("fluid in ear", "otitis media with effusion"),
    ("neck glands swollen", "cervical lymphadenopathy"),
    ("throat inflamed", "pharyngeal erythema"),
    ("voice raspy", "dysphonia"),
    ("nasal polyps visible", "nasal polyposis"),
    ("ear discharge", "otorrhoea"),
    ("right ear pus", "right otorrhoea"),
    ("patient looks pale", "pallor present"),
    ("abdomen feels soft", "abdomen soft on palpation"),
    ("pulse irregular", "irregular pulse rhythm"),
    ("no heart murmur", "no cardiac murmur"),
    ("chest sounds clear", "clear breath sounds bilaterally"),
])
def test_phrase_mapping_library(phrase: str, expected: str) -> None:
    finding = ExaminationInterpreter().interpret_phrase(phrase, session_id=uuid4())
    assert finding.interpreted_text == expected
    assert finding.status == ExaminationFindingStatus.CONFIRMED


def test_low_confidence_mapping_requires_review() -> None:
    finding = ExaminationInterpreter().interpret_phrase("ear red", session_id=uuid4())
    assert finding.interpreted_text == "acute otitis media (side not specified)"
    assert finding.status == ExaminationFindingStatus.PENDING_REVIEW
    assert finding.confidence < 0.8

    effusion_finding = ExaminationInterpreter().interpret_phrase(
        "fluid behind eardrum", session_id=uuid4()
    )
    assert effusion_finding.interpreted_text == "acute otitis media (side not specified)"
    assert effusion_finding.status == ExaminationFindingStatus.PENDING_REVIEW


def test_unrecognised_phrase_passes_through_unchanged_and_flagged() -> None:
    source = "unusual clicking near the jaw"
    finding = ExaminationInterpreter().interpret_phrase(source, session_id=uuid4())
    assert finding.raw_text == source
    assert finding.interpreted_text == source
    assert finding.status == ExaminationFindingStatus.UNRECOGNISED
    assert finding.confidence == 0


def test_mapping_library_is_extendable_with_yaml(tmp_path: Path) -> None:
    (tmp_path / "custom.yaml").write_text(yaml.safe_dump({"mappings": [{
        "id": "custom.capillary_refill", "pattern": "^refill under two seconds$",
        "term": "capillary refill time less than 2 seconds", "confidence": 0.93,
    }]}), encoding="utf-8")
    interpreter = ExaminationInterpreter(PhraseToClinicalTermMapper.from_directory(tmp_path))
    finding = interpreter.interpret_phrase("refill under two seconds", session_id=uuid4())
    assert finding.interpreted_text == "capillary refill time less than 2 seconds"


def _settings(tmp_path: Path):
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    config["database"]["path"] = str(tmp_path / "scribe.db")
    config["storage"]["audio_directory"] = str(tmp_path / "audio")
    config["storage"]["transcript_directory"] = str(tmp_path / "transcripts")
    config["storage"]["artifact_directory"] = str(tmp_path / "artifacts")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path, load_config(path)


def test_worker_persists_findings_completes_stage_and_confirmation_is_append_only(
    tmp_path: Path, monkeypatch,
) -> None:
    config_path, settings = _settings(tmp_path)
    database = Database(settings.database.path)
    database.initialize()
    session_id, transcript_id, segment_id, run_id = uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime.now(timezone.utc).isoformat()
    segments = [{
        "id": str(segment_id), "session_id": str(session_id), "sequence_number": 0,
        "start_ms": 0, "end_ms": 1000, "source_language": "en",
        "original_text": "On examination, ear red. Unusual clicking near the jaw.",
        "confidence": 0.95,
    }]
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO sessions(id, status, created_at, updated_at) VALUES (?, 'PROCESSING', ?, ?)",
            (str(session_id), now, now),
        )
        connection.execute(
            "INSERT INTO transcripts(id, session_id, version, source_language, original_text, segments_json, engine, created_at) "
            "VALUES (?, ?, 1, 'en', ?, ?, 'test', ?)",
            (str(transcript_id), str(session_id), segments[0]["original_text"], json.dumps(segments), now),
        )
        connection.execute(
            "INSERT INTO diarization_runs(id, session_id, transcript_id, engine, created_at) VALUES (?, ?, ?, 'test', ?)",
            (str(run_id), str(session_id), str(transcript_id), now),
        )
        assert enqueue_examination_interpretation_job(connection, session_id, max_attempts=3, now=now)

    worker = ExaminationInterpretationWorker(database, settings, worker_id="test-examination-worker")
    assert worker.process_once() is True
    with database.connect() as connection:
        job = connection.execute(
            "SELECT status, stage FROM jobs WHERE job_type = 'EXAMINATION_INTERPRETATION'"
        ).fetchone()
        findings = connection.execute("SELECT * FROM examination_findings ORDER BY created_at, id").fetchall()
        structuring = connection.execute(
            "SELECT status FROM jobs WHERE job_type = 'STRUCTURING'"
        ).fetchone()
    assert (job["status"], job["stage"]) == ("SUCCEEDED", "EXAMINATION_INTERPRETATION_COMPLETE")
    assert len(findings) == 2
    assert {row["status"] for row in findings} == {"PENDING_REVIEW", "UNRECOGNISED"}
    assert structuring["status"] == "PENDING"
    with database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE examination_findings SET raw_text = 'changed'")

    pending_id = next(row["id"] for row in findings if row["status"] == "PENDING_REVIEW")
    monkeypatch.setenv("CLINICAL_SCRIBE_CONFIG", str(config_path))
    with TestClient(app) as client:
        response = client.get(f"/api/v1/sessions/{session_id}/examination-findings")
        assert response.status_code == 200, response.text
        confirmation = client.post(
            f"/api/v1/sessions/{session_id}/examination-findings/{pending_id}/confirmation",
            json={"interpreted_text": "right acute otitis media", "actor": "test-clinician"},
        )
    assert confirmation.status_code == 200, confirmation.text
    effective = next(item for item in confirmation.json() if item["id"] == pending_id)
    assert effective["status"] == "CONFIRMED"
    assert effective["interpreted_text"] == "right acute otitis media"
    with database.connect() as connection:
        revision = connection.execute("SELECT * FROM examination_finding_revisions").fetchone()
        original = connection.execute(
            "SELECT raw_text, status FROM examination_findings WHERE id = ?", (pending_id,)
        ).fetchone()
        audit = connection.execute(
            "SELECT action FROM audit_events WHERE action = 'EXAMINATION_FINDING_CONFIRMED'"
        ).fetchone()
    assert (original["raw_text"], original["status"]) == ("ear red", "PENDING_REVIEW")
    assert revision["version"] == 1
    assert audit["action"] == "EXAMINATION_FINDING_CONFIRMED"
