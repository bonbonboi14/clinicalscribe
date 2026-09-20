from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from api.main import app
from config import load_config
from core.clerking import ClerkingSheetGenerator, DoNotInferValidator, FabricatedClerkingFieldError
from core.models import TranscriptSegment
from core.structuring import ClinicalFactExtractor
from storage.database import Database
from storage.jobs import enqueue_structuring_job
from worker.structuring import StructuringWorker


def _segments(session_id: UUID, texts: list[str]) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(
            session_id=session_id, sequence_number=index, start_ms=index * 1_000,
            end_ms=index * 1_000 + 900, source_language="en", original_text=text, confidence=0.95,
        )
        for index, text in enumerate(texts)
    ]


def _generate(*texts: str):
    session_id = uuid4()
    facts = ClinicalFactExtractor().extract(_segments(session_id, list(texts)))
    return facts, ClerkingSheetGenerator().generate(session_id, facts)


def test_denied_diabetes_is_preserved_as_negative_fact_but_not_added_to_pmh() -> None:
    facts, sheet = _generate("Patient denies diabetes.")
    assert [(fact.name, fact.assertion.value) for fact in facts] == [("diabetes", "NEGATIVE")]
    assert sheet.past_medical_history == ["Not discussed."]
    assert "diabetes" not in json.dumps(sheet.past_medical_history).casefold()


def test_unnamed_tablet_never_acquires_an_invented_drug_name() -> None:
    _, sheet = _generate("I take a tablet once a day.")
    assert sheet.drug_history[0].drug == "Unnamed tablet"
    assert sheet.drug_history[0].frequency == "once a day"
    assert sheet.drug_history[0].dose == "Not discussed."
    assert sheet.drug_history[0].duration == "Not discussed."


def test_no_examination_defaults_to_not_performed_without_normal_findings() -> None:
    _, sheet = _generate("No examination was performed.")
    assert sheet.physical_examination == ["Not performed"]
    assert "normal" not in json.dumps(sheet.physical_examination).casefold()


def test_do_not_infer_validator_rejects_fabricated_value_even_with_fact_id() -> None:
    facts, sheet = _generate("Patient has fever.")
    forged = sheet.model_copy(update={"presenting_complaint": "pneumonia"})
    with pytest.raises(FabricatedClerkingFieldError, match="text not present"):
        DoNotInferValidator().validate(forged, facts)


def _settings(tmp_path: Path):
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    config["database"]["path"] = str(tmp_path / "scribe.db")
    config["storage"]["audio_directory"] = str(tmp_path / "audio")
    config["storage"]["transcript_directory"] = str(tmp_path / "transcripts")
    config["storage"]["artifact_directory"] = str(tmp_path / "artifacts")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path, load_config(path)


def test_worker_persists_facts_then_clerking_sheet_and_review_api_returns_it(tmp_path: Path, monkeypatch) -> None:
    config_path, settings = _settings(tmp_path)
    database = Database(settings.database.path)
    database.initialize()
    session_id = uuid4()
    segments = _segments(session_id, ["Patient has fever.", "I take a tablet once a day."])
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO sessions(id, status, created_at, updated_at) VALUES (?, 'PROCESSING', ?, ?)",
            (str(session_id), now, now),
        )
        connection.execute(
            "INSERT INTO transcripts(id, session_id, version, source_language, original_text, segments_json, engine, created_at) "
            "VALUES (?, ?, 1, 'en', ?, ?, 'test', ?)",
            (str(uuid4()), str(session_id), " ".join(item.original_text for item in segments),
             json.dumps([item.model_dump(mode="json") for item in segments]), now),
        )
        assert enqueue_structuring_job(connection, session_id, max_attempts=3, now=now)

    worker = StructuringWorker(database, settings, worker_id="test-structuring-worker")
    assert worker.process_once() is True
    with database.connect() as connection:
        job = connection.execute("SELECT status, stage FROM jobs WHERE job_type = 'STRUCTURING'").fetchone()
        fact_count = connection.execute("SELECT COUNT(*) FROM structured_facts").fetchone()[0]
        sheet_count = connection.execute("SELECT COUNT(*) FROM clerking_sheets").fetchone()[0]
        validation_states = {row[0] for row in connection.execute("SELECT state FROM validation_findings")}
        audit_actions = {row[0] for row in connection.execute("SELECT action FROM audit_events")}
    assert (job["status"], job["stage"]) == ("SUCCEEDED", "CLERKING_SHEET_GENERATED")
    assert fact_count >= 2
    assert sheet_count == 1
    assert validation_states == {"SUPPORTED"}
    assert {"STRUCTURE_COMPLETE", "CLERKING_SHEET_GENERATED"} <= audit_actions
    with database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE clerking_sheets SET status = 'APPROVED'")

    monkeypatch.setenv("CLINICAL_SCRIBE_CONFIG", str(config_path))
    with TestClient(app) as client:
        response = client.get(f"/api/v1/sessions/{session_id}/clerking-sheet")
    assert response.status_code == 200, response.text
    assert response.json()["presenting_complaint"] == "fever"
    assert response.json()["drug_history"][0]["drug"] == "Unnamed tablet"
