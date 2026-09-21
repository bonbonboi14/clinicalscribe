from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.main import app
from config import load_config
from config.loader import DiagnosisConfig, get_diagnosis_config
from core.diagnosis import Differential, DifferentialDiagnosisEngine, DifferentialResult, DxLikelihood
from core.models import AssertionState, ClinicalFact
from core.validation import NoteValidator
from models.differential import DIFFERENTIAL_DISABLED_MESSAGE, DIFFERENTIAL_DISCLAIMER
from storage.database import Database
from storage.jobs import enqueue_differential_job
from worker.differential import DifferentialWorker


def _fact(
    session_id: UUID, name: str, assertion: AssertionState = AssertionState.POSITIVE,
) -> ClinicalFact:
    return ClinicalFact(
        session_id=session_id, category="PC", name=name, value=name,
        assertion=assertion, confidence=0.95,
        transcript_segment_ids=[] if assertion == AssertionState.NOT_MENTIONED else [uuid4()],
        original_text=name,
    )


def test_disabled_engine_generates_no_candidates_and_clean_message() -> None:
    session_id = uuid4()
    result = DifferentialDiagnosisEngine(DiagnosisConfig(enabled=False)).generate(
        session_id, [_fact(session_id, "fever"), _fact(session_id, "cough")]
    )
    assert result.enabled is False
    assert result.candidates == []
    assert result.render_text() == DIFFERENTIAL_DISABLED_MESSAGE
    assert result.disclaimer == DIFFERENTIAL_DISCLAIMER


def test_enabled_engine_generates_ranked_three_to_five_candidates_from_facts() -> None:
    session_id = uuid4()
    facts = [
        _fact(session_id, "fever"),
        _fact(session_id, "cough"),
        _fact(session_id, "shortness of breath", AssertionState.NEGATIVE),
        _fact(session_id, "nasal congestion", AssertionState.UNCERTAIN),
        _fact(session_id, "rash", AssertionState.NOT_MENTIONED),
    ]
    result = DifferentialDiagnosisEngine(DiagnosisConfig(enabled=True)).generate(session_id, facts)
    assert result.enabled is True
    assert 3 <= len(result.candidates) <= 5
    assert result.fact_count_used == 3
    eligible_ids = {fact.id for fact in facts[:3]}
    excluded_ids = {facts[3].id, facts[4].id}
    for candidate in result.candidates:
        assert candidate.evidence_refs
        assert set(candidate.evidence_refs) <= eligible_ids
        assert not set(candidate.evidence_refs) & excluded_ids
    assert result.candidates[0].likelihood == DxLikelihood.SUPPORTED
    assert DIFFERENTIAL_DISCLAIMER in result.render_text()
    assert NoteValidator().validate_differential(result, facts).is_safe is True


def test_insufficient_facts_produce_no_fabricated_conditions() -> None:
    session_id = uuid4()
    result = DifferentialDiagnosisEngine(DiagnosisConfig(enabled=True)).generate(
        session_id, [_fact(session_id, "fever")]
    )
    assert result.enabled is True
    assert result.candidates == []
    assert result.fact_count_used == 1


def test_candidate_requires_specific_evidence_reference() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        Differential(
            condition="Viral illness", likelihood=DxLikelihood.POSSIBLE,
            supporting_features=["Documented: fever"], evidence_refs=[],
        )


def test_result_cannot_contain_candidates_while_disabled() -> None:
    candidate = Differential(
        condition="Viral illness", likelihood=DxLikelihood.POSSIBLE,
        supporting_features=["Documented: fever"], evidence_refs=[uuid4()],
    )
    with pytest.raises(ValidationError, match="disabled differential"):
        DifferentialResult(session_id=uuid4(), enabled=False, candidates=[candidate])


def _settings(tmp_path: Path, *, enabled: bool):
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    config["database"]["path"] = str(tmp_path / "scribe.db")
    config["storage"]["audio_directory"] = str(tmp_path / "audio")
    config["storage"]["transcript_directory"] = str(tmp_path / "transcripts")
    config["storage"]["artifact_directory"] = str(tmp_path / "artifacts")
    config["diagnosis"]["enabled"] = enabled
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path, load_config(path)


def _seed_job(database: Database, session_id: UUID, facts: list[ClinicalFact]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO sessions(id, status, diagnosis_enabled, created_at, updated_at) "
            "VALUES (?, 'REVIEW', 1, ?, ?)", (str(session_id), now, now),
        )
        for fact in facts:
            connection.execute(
                "INSERT INTO structured_facts(id, session_id, category, name, value, assertion, confidence, "
                "evidence_json, original_text, version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (str(fact.id), str(session_id), fact.category, fact.name, fact.value, fact.assertion.value,
                 fact.confidence, json.dumps([str(item) for item in fact.transcript_segment_ids]),
                 fact.original_text, now),
            )
        assert enqueue_differential_job(connection, session_id, max_attempts=3, now=now)


def test_worker_does_not_claim_job_when_system_toggle_is_off(tmp_path: Path) -> None:
    _, settings = _settings(tmp_path, enabled=False)
    database = Database(settings.database.path)
    database.initialize()
    session_id = uuid4()
    _seed_job(database, session_id, [_fact(session_id, "fever"), _fact(session_id, "cough")])
    assert DifferentialWorker(database, settings, worker_id="disabled-worker").process_once() is False
    with database.connect() as connection:
        job = connection.execute("SELECT status, stage FROM jobs WHERE job_type = 'DIFFERENTIAL'").fetchone()
        count = connection.execute("SELECT COUNT(*) FROM differentials").fetchone()[0]
    assert (job["status"], job["stage"]) == ("PENDING", None)
    assert count == 0


def test_enabled_worker_validates_and_persists_result_with_disclaimer(tmp_path: Path) -> None:
    _, settings = _settings(tmp_path, enabled=True)
    database = Database(settings.database.path)
    database.initialize()
    session_id = uuid4()
    facts = [_fact(session_id, "fever"), _fact(session_id, "cough")]
    _seed_job(database, session_id, facts)
    assert DifferentialWorker(database, settings, worker_id="enabled-worker").process_once() is True
    with database.connect() as connection:
        job = connection.execute("SELECT status, stage FROM jobs WHERE job_type = 'DIFFERENTIAL'").fetchone()
        row = connection.execute("SELECT * FROM differentials").fetchone()
        states = {item[0] for item in connection.execute(
            "SELECT state FROM validation_findings WHERE artefact_type = 'DIFFERENTIAL'"
        )}
    result = DifferentialResult.model_validate_json(row["result_json"])
    assert (job["status"], job["stage"]) == ("SUCCEEDED", "DIFFERENTIAL_GENERATED")
    assert 3 <= len(result.candidates) <= 5
    assert result.disclaimer == DIFFERENTIAL_DISCLAIMER
    assert states == {"SUPPORTED"}


def test_default_config_keeps_diagnosis_disabled() -> None:
    assert get_diagnosis_config().enabled is False


def test_disabled_review_is_visible_and_system_toggle_cannot_be_bypassed(
    tmp_path: Path, monkeypatch,
) -> None:
    path, settings = _settings(tmp_path, enabled=False)
    database = Database(settings.database.path)
    database.initialize()
    session_id = uuid4()
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO sessions(id, status, created_at, updated_at) VALUES (?, 'REVIEW', ?, ?)",
            (str(session_id), now, now),
        )
    monkeypatch.setenv("CLINICAL_SCRIBE_CONFIG", str(path))
    with TestClient(app) as client:
        page = client.get("/")
        review = client.get(f"/api/v1/sessions/{session_id}/diagnosis-review")
        enable = client.put(
            f"/api/v1/sessions/{session_id}/diagnosis-settings", json={"enabled": True}
        )
    assert 'id="diagnosisEnabled"' in page.text
    assert review.status_code == 200
    assert review.json()["rendered"] == DIFFERENTIAL_DISABLED_MESSAGE
    assert review.json()["disclaimer"] is None
    assert enable.status_code == 409


def test_session_toggle_controls_enabled_review_and_disclaimer(
    tmp_path: Path, monkeypatch,
) -> None:
    path, settings = _settings(tmp_path, enabled=True)
    database = Database(settings.database.path)
    database.initialize()
    session_id = uuid4()
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO sessions(id, status, created_at, updated_at) VALUES (?, 'REVIEW', ?, ?)",
            (str(session_id), now, now),
        )
    monkeypatch.setenv("CLINICAL_SCRIBE_CONFIG", str(path))
    with TestClient(app) as client:
        enabled = client.get(f"/api/v1/sessions/{session_id}/diagnosis-review")
        disabled = client.put(
            f"/api/v1/sessions/{session_id}/diagnosis-settings", json={"enabled": False}
        )
        disabled_review = client.get(f"/api/v1/sessions/{session_id}/diagnosis-review")
    assert enabled.json()["effective_enabled"] is True
    assert enabled.json()["disclaimer"] == DIFFERENTIAL_DISCLAIMER
    assert disabled.json()["effective_enabled"] is False
    assert disabled_review.json()["rendered"] == DIFFERENTIAL_DISABLED_MESSAGE
