from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml

from config import load_config
from core.models import AssertionState, ClinicalFact, TranscriptSegment
from core.structuring import ClinicalFactExtractor
from core.treatment import FabricatedTreatmentError, TreatmentDoNotInferValidator, TreatmentPlanGenerator
from models.treatment_plan import NOT_MENTIONED
from storage.database import Database
from storage.jobs import enqueue_treatment_plan_job
from worker.treatment import TreatmentPlanWorker


def _segments(session_id: UUID, *texts: str) -> list[TranscriptSegment]:
    return [TranscriptSegment(
        session_id=session_id, sequence_number=index, start_ms=index * 1000, end_ms=index * 1000 + 900,
        source_language="en", original_text=text, confidence=0.99,
    ) for index, text in enumerate(texts)]


def _generate(*texts: str):
    session_id = uuid4()
    segments = _segments(session_id, *texts)
    facts = ClinicalFactExtractor().extract(segments)
    return segments, facts, TreatmentPlanGenerator().generate(session_id, facts)


def test_explicit_paracetamol_order_is_structured_exactly() -> None:
    segments, facts, plan = _generate("Give paracetamol 1 gram three times a day.")
    medication = plan.pharmacological[0]
    assert medication.drug.casefold() == "paracetamol"
    assert medication.dose.casefold() == "1 gram"
    assert medication.frequency.casefold() == "three times a day"
    assert medication.route == NOT_MENTIONED
    assert medication.duration == NOT_MENTIONED
    assert medication.source_transcript_ref == str(segments[0].id)
    assert {fact.category for fact in facts} == {"TREATMENT"}


def test_no_medications_discussed_is_explicitly_not_mentioned() -> None:
    _, _, plan = _generate("The patient has a headache.")
    assert len(plan.pharmacological) == 1
    assert plan.pharmacological[0].drug == NOT_MENTIONED
    assert "PHARMACOLOGICAL\n- Not mentioned." in plan.render_text()


def test_hypertension_diagnosis_with_no_medications_does_not_invent_antihypertensive() -> None:
    _, facts, plan = _generate("The diagnosis is hypertension. No medications yet.")
    assert any(fact.category == "PMH" and fact.name.casefold() == "hypertension" for fact in facts)
    assert plan.pharmacological[0].drug == NOT_MENTIONED
    serialised = plan.model_dump_json().casefold()
    assert "amlodipine" not in serialised
    assert "losartan" not in serialised


def test_spoken_referral_has_transcript_reference() -> None:
    segments, facts, plan = _generate("Refer to ENT.")
    assert plan.referrals == ["ENT"]
    evidence = plan.evidence_by_field["referrals.0"]
    assert len(evidence) == 1
    supporting = next(fact for fact in facts if fact.id == evidence[0])
    assert supporting.category == "PLAN"
    assert supporting.transcript_segment_ids == [segments[0].id]


def test_patient_speech_cannot_create_a_treatment_order() -> None:
    session_id = uuid4()
    facts = ClinicalFactExtractor().extract([{
        "id": str(uuid4()), "session_id": str(session_id), "speaker_role": "PATIENT",
        "original_text": "Give paracetamol 1 gram three times a day.",
    }])
    plan = TreatmentPlanGenerator().generate(session_id, facts)
    assert not any(fact.category in {"TREATMENT", "PLAN"} for fact in facts)
    assert plan.pharmacological[0].drug == NOT_MENTIONED


def test_do_not_infer_rejects_treatment_backed_only_by_diagnosis_fact() -> None:
    session_id = uuid4()
    diagnosis = ClinicalFact(
        session_id=session_id, category="ASSESSMENT", name="hypertension", value="hypertension",
        assertion=AssertionState.POSITIVE, confidence=1.0, transcript_segment_ids=[uuid4()],
        original_text="Diagnosis is hypertension.",
    )
    plan = TreatmentPlanGenerator().generate(session_id, [])
    forged = plan.model_copy(update={
        "non_pharmacological": ["salt restriction"],
        "fact_ids": [diagnosis.id],
        "evidence_by_field": {"non_pharmacological.0": [diagnosis.id]},
    })
    with pytest.raises(FabricatedTreatmentError, match="non-treatment fact"):
        TreatmentDoNotInferValidator().validate(forged, [diagnosis])


def _settings(tmp_path: Path):
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    config["database"]["path"] = str(tmp_path / "scribe.db")
    config["storage"]["audio_directory"] = str(tmp_path / "audio")
    config["storage"]["transcript_directory"] = str(tmp_path / "transcripts")
    config["storage"]["artifact_directory"] = str(tmp_path / "artifacts")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return load_config(path)


def test_worker_persists_validated_immutable_treatment_plan(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database.path)
    database.initialize()
    session_id = uuid4()
    segment = _segments(session_id, "Give paracetamol 1 gram three times a day.")[0]
    facts = ClinicalFactExtractor().extract([segment])
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO sessions(id, status, created_at, updated_at) VALUES (?, 'PROCESSING', ?, ?)",
            (str(session_id), now, now),
        )
        connection.execute(
            "INSERT INTO transcripts(id, session_id, version, source_language, original_text, segments_json, engine, created_at) "
            "VALUES (?, ?, 1, 'en', ?, ?, 'test', ?)",
            (str(uuid4()), str(session_id), segment.original_text,
             json.dumps([segment.model_dump(mode="json")]), now),
        )
        for fact in facts:
            connection.execute(
                "INSERT INTO structured_facts(id, session_id, category, name, value, assertion, confidence, "
                "evidence_json, original_text, version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (str(fact.id), str(session_id), fact.category, fact.name, fact.value, fact.assertion.value,
                 fact.confidence, json.dumps([str(item) for item in fact.transcript_segment_ids]),
                 fact.original_text, now),
            )
        assert enqueue_treatment_plan_job(connection, session_id, max_attempts=3, now=now)

    worker = TreatmentPlanWorker(database, settings, worker_id="test-treatment-worker")
    assert worker.process_once() is True
    with database.connect() as connection:
        job = connection.execute(
            "SELECT status, stage FROM jobs WHERE job_type = 'TREATMENT_PLAN'"
        ).fetchone()
        row = connection.execute("SELECT * FROM treatment_plans").fetchone()
        states = {item[0] for item in connection.execute(
            "SELECT state FROM validation_findings WHERE artefact_type = 'TREATMENT_PLAN'"
        )}
        action = connection.execute(
            "SELECT action FROM audit_events WHERE artefact_type = 'treatment_plan'"
        ).fetchone()[0]
    assert (job["status"], job["stage"]) == ("SUCCEEDED", "TREATMENT_PLAN_GENERATED")
    assert json.loads(row["structured_json"])["pharmacological"][0]["drug"].casefold() == "paracetamol"
    assert states == {"SUPPORTED"}
    assert action == "TREATMENT_PLAN_GENERATED"
    with database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE treatment_plans SET status = 'APPROVED'")
