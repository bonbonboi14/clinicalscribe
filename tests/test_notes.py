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
from core.clerking import ClerkingSheetGenerator
from core.models import AssertionState, ClinicalFact, TranscriptSegment
from core.templates import NoteHallucinationFirewall, NoteValidationError, OllamaEngine, TemplateNoteEngine, TemplateRegistry
from storage.database import Database
from storage.jobs import enqueue_note_generation_job
from worker.notes import NoteGenerationWorker


def _settings(tmp_path: Path):
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    config["database"]["path"] = str(tmp_path / "scribe.db")
    config["storage"]["audio_directory"] = str(tmp_path / "audio")
    config["storage"]["transcript_directory"] = str(tmp_path / "transcripts")
    config["storage"]["artifact_directory"] = str(tmp_path / "artifacts")
    config["note_generation"]["template_directory"] = str(Path("templates").resolve())
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path, load_config(path)


def test_all_note_templates_are_strict_english_yaml_and_render_both_formats() -> None:
    registry = TemplateRegistry("templates")
    assert registry.names() == ["ent", "primary_care", "soap"]
    session_id = uuid4()
    fact = ClinicalFact(
        session_id=session_id, category="PC", name="fever", value="fever",
        assertion=AssertionState.POSITIVE, confidence=0.95,
    )
    sheet = ClerkingSheetGenerator().generate(session_id, [fact])
    for name in registry.names():
        note = TemplateNoteEngine(registry.load(name)).generate(sheet)
        assert note.language == "en"
        assert "fever" in note.content
        assert "fever" in note.plain_text
        assert note.template_name == name


def test_hallucination_firewall_rejects_changed_or_added_clinical_text() -> None:
    session_id = uuid4()
    fact = ClinicalFact(
        session_id=session_id, category="PC", name="fever", value="fever",
        assertion=AssertionState.POSITIVE, confidence=0.95,
    )
    sheet = ClerkingSheetGenerator().generate(session_id, [fact])
    note = TemplateNoteEngine(TemplateRegistry("templates").load("primary_care")).generate(sheet)
    fabricated = note.model_copy(update={"content": note.content.replace("fever", "pneumonia")})
    with pytest.raises(NoteValidationError, match="omitted or changed"):
        NoteHallucinationFirewall().validate(fabricated, sheet, [fact])


def test_ollama_is_loopback_only_and_rejects_unsupported_rewording(monkeypatch) -> None:
    template = TemplateRegistry("templates").load("primary_care")
    with pytest.raises(ValueError, match="loopback"):
        OllamaEngine(template, model="local-model", base_url="https://example.com")
    session_id = uuid4()
    fact = ClinicalFact(
        session_id=session_id, category="PC", name="fever", value="fever",
        assertion=AssertionState.POSITIVE, confidence=0.95,
    )
    sheet = ClerkingSheetGenerator().generate(session_id, [fact])

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self):
            return json.dumps({"response": json.dumps({"markdown": "# Clinical Note\n- pneumonia", "plain_text": "pneumonia"})}).encode()

    monkeypatch.setattr("core.templates.ollama.urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(ValueError, match="hallucination firewall"):
        OllamaEngine(template, model="local-model").generate(sheet)


def test_note_worker_review_approval_and_exports_are_recoverable_and_gated(tmp_path: Path, monkeypatch) -> None:
    config_path, settings = _settings(tmp_path)
    database = Database(settings.database.path)
    database.initialize()
    session_id = uuid4()
    segment = TranscriptSegment(
        session_id=session_id, sequence_number=0, start_ms=0, end_ms=900,
        source_language="ms", original_text="Pesakit demam", normalized_text="Patient has fever", confidence=0.95,
    )
    fact = ClinicalFact(
        session_id=session_id, category="PC", name="fever", value="fever",
        assertion=AssertionState.POSITIVE, confidence=0.95, transcript_segment_ids=[segment.id],
        original_text="Pesakit demam",
    )
    sheet = ClerkingSheetGenerator().generate(session_id, [fact])
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO sessions(id, status, created_at, updated_at) VALUES (?, 'PROCESSING', ?, ?)",
            (str(session_id), now, now),
        )
        connection.execute(
            "INSERT INTO transcripts(id, session_id, version, source_language, original_text, segments_json, engine, created_at) "
            "VALUES (?, ?, 1, 'ms', ?, ?, 'test', ?)",
            (str(uuid4()), str(session_id), segment.original_text, json.dumps([segment.model_dump(mode="json")]), now),
        )
        connection.execute(
            "INSERT INTO structured_facts(id, session_id, category, name, value, assertion, confidence, evidence_json, "
            "original_text, version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (str(fact.id), str(session_id), fact.category, fact.name, fact.value, fact.assertion.value, fact.confidence,
             json.dumps([str(segment.id)]), fact.original_text, now),
        )
        connection.execute(
            "INSERT INTO clerking_sheets(id, session_id, version, structured_json, fact_ids_json, status, created_at) "
            "VALUES (?, ?, 1, ?, ?, 'DRAFT', ?)",
            (str(sheet.id), str(session_id), sheet.model_dump_json(), json.dumps([str(fact.id)]), now),
        )
        assert enqueue_note_generation_job(connection, session_id, max_attempts=3, now=now)

    assert NoteGenerationWorker(database, settings, worker_id="test-note-worker").process_once() is True
    with database.connect() as connection:
        job = connection.execute("SELECT status, stage FROM jobs WHERE job_type = 'NOTE_GENERATION'").fetchone()
        note = connection.execute("SELECT * FROM notes").fetchone()
        states = {row[0] for row in connection.execute("SELECT state FROM validation_findings WHERE artefact_type = 'CLINICAL_NOTE'")}
    assert (job["status"], job["stage"]) == ("SUCCEEDED", "NOTE_GENERATED")
    assert note["status"] == "DRAFT"
    assert "fever" in note["content"] and "fever" in note["plain_text"]
    assert states == {"SUPPORTED"}
    with database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE notes SET content = 'changed'")

    monkeypatch.setenv("CLINICAL_SCRIBE_CONFIG", str(config_path))
    with TestClient(app) as client:
        ui = client.get("/")
        assert 'class="note-review-grid"' in ui.text and 'id="copyNote"' in ui.text
        review = client.get(f"/api/v1/sessions/{session_id}/note-review")
        assert review.status_code == 200, review.text
        assert review.json()["transcript"]["original_text"] == "Pesakit demam"
        assert review.json()["clerking_sheet"]["presenting_complaint"] == "fever"
        assert review.json()["clinical_note"]["status"] == "DRAFT"
        blocked = client.get(f"/api/v1/sessions/{session_id}/clinical-note/export?format=txt")
        assert blocked.status_code == 409
        approved = client.post(
            f"/api/v1/sessions/{session_id}/clinical-note/approval", json={"actor": "test-clinician"}
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "APPROVED"
        txt = client.get(f"/api/v1/sessions/{session_id}/clinical-note/export?format=txt")
        markdown = client.get(f"/api/v1/sessions/{session_id}/clinical-note/export?format=md")
        assert txt.status_code == markdown.status_code == 200
        assert "fever" in txt.text and "# Primary Care Clinical Note" in markdown.text
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM audit_events WHERE action = 'NOTE_EXPORTED'").fetchone()[0] == 2
