from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml
import pytest
from docx import Document
from fastapi.testclient import TestClient
from git import Repo

from api.main import app
from config.settings import GitHubSection
from core.clerking import ClerkingSheetGenerator
from core.models import AssertionState, ClinicalFact, TranscriptSegment
from core.github import GitHubPusher
from models.differential import Differential, DifferentialResult, DxLikelihood
from models.treatment_plan import TreatmentPlan
from storage.database import Database


def _remote_repository(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    remote = tmp_path / "remote.git"
    repo = Repo.init(seed, initial_branch="main")
    (seed / "README.md").write_text("# Session archive\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("initial")
    Repo.clone_from(seed, remote, bare=True)
    return remote


def _settings_path(tmp_path: Path, remote: Path) -> Path:
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    config["database"]["path"] = str(tmp_path / "scribe.db")
    config["storage"]["audio_directory"] = str(tmp_path / "audio")
    config["storage"]["transcript_directory"] = str(tmp_path / "transcripts")
    config["storage"]["artifact_directory"] = str(tmp_path / "artifacts")
    config["github"].update({"enabled": True, "repo_url": str(remote), "branch": "main"})
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _seed_session(database: Database) -> tuple[str, str]:
    session_id = uuid4()
    segment = TranscriptSegment(
        session_id=session_id,
        sequence_number=0,
        start_ms=0,
        end_ms=900,
        source_language="ms",
        original_text="Pesakit demam",
        normalized_text="Patient has fever",
        confidence=0.96,
    )
    fact = ClinicalFact(
        session_id=session_id,
        category="PC",
        name="fever",
        value="fever",
        assertion=AssertionState.POSITIVE,
        confidence=0.96,
        transcript_segment_ids=[segment.id],
        original_text=segment.original_text,
    )
    sheet = ClerkingSheetGenerator().generate(session_id, [fact])
    note_id = uuid4()
    plan = TreatmentPlan(session_id=session_id, note_id=note_id)
    differential = DifferentialResult(
        session_id=session_id,
        enabled=True,
        candidates=[
            Differential(
                condition="Viral syndrome",
                likelihood=DxLikelihood.POSSIBLE,
                supporting_features=["fever"],
                evidence_refs=[fact.id],
            )
        ],
        fact_count_used=1,
    )
    now = datetime.now(timezone.utc).isoformat()
    artefact_path = database.path.parent / "speaker-labelled.json"
    artefact_path.write_text(
        json.dumps({
            "segments": [{
                "start_ms": 0,
                "end_ms": 900,
                "speaker_role": "PATIENT",
                "original_text": segment.original_text,
            }]
        }),
        encoding="utf-8",
    )
    transcript_id = uuid4()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO sessions(id, status, source_language, created_at, updated_at) "
            "VALUES (?, 'REVIEW', 'ms', ?, ?)",
            (str(session_id), now, now),
        )
        connection.execute(
            "INSERT INTO transcripts(id, session_id, version, source_language, original_text, segments_json, engine, created_at) "
            "VALUES (?, ?, 1, 'ms', ?, ?, 'test', ?)",
            (str(transcript_id), str(session_id), segment.original_text, json.dumps([segment.model_dump(mode="json")]), now),
        )
        connection.execute(
            "INSERT INTO transcript_artifacts(id, session_id, transcript_id, kind, version, language, storage_path, checksum_sha256, created_at) "
            "VALUES (?, ?, ?, 'SPEAKER_LABELLED_TRANSCRIPT', 1, 'ms', ?, ?, ?)",
            (str(uuid4()), str(session_id), str(transcript_id), str(artefact_path), "0" * 64, now),
        )
        connection.execute(
            "INSERT INTO structured_facts(id, session_id, category, name, value, assertion, confidence, evidence_json, original_text, version, created_at) "
            "VALUES (?, ?, 'PC', 'fever', 'fever', 'POSITIVE', 0.96, ?, ?, 1, ?)",
            (str(fact.id), str(session_id), json.dumps([str(segment.id)]), segment.original_text, now),
        )
        connection.execute(
            "INSERT INTO clerking_sheets(id, session_id, version, structured_json, fact_ids_json, status, created_at) "
            "VALUES (?, ?, 1, ?, ?, 'DRAFT', ?)",
            (str(sheet.id), str(session_id), sheet.model_dump_json(), json.dumps([str(fact.id)]), now),
        )
        connection.execute(
            "INSERT INTO notes(id, session_id, clerking_sheet_id, version, language, content, plain_text, template_name, engine, status, created_at) "
            "VALUES (?, ?, ?, 1, 'en', '# Primary Care Clinical Note\n\n## Presenting Complaint\n- fever', "
            "'PRIMARY CARE CLINICAL NOTE\n\nPRESENTING COMPLAINT\nfever', 'primary_care', 'test', 'DRAFT', ?)",
            (str(note_id), str(session_id), str(sheet.id), now),
        )
        connection.execute(
            "INSERT INTO validation_findings(id, session_id, artefact_type, artefact_id, claim_text, state, evidence_json, created_at) "
            "VALUES (?, ?, 'CLINICAL_NOTE', ?, 'fever', 'SUPPORTED', ?, ?)",
            (str(uuid4()), str(session_id), str(note_id), json.dumps([str(segment.id)]), now),
        )
        connection.execute(
            "INSERT INTO treatment_plans(id, session_id, note_id, version, structured_json, fact_ids_json, status, created_at) "
            "VALUES (?, ?, ?, 1, ?, '[]', 'DRAFT', ?)",
            (str(plan.id), str(session_id), str(note_id), plan.model_dump_json(), now),
        )
        connection.execute(
            "INSERT INTO differentials(id, session_id, version, enabled_at_generation, candidates_json, fact_ids_json, disclaimer, result_json, created_at) "
            "VALUES (?, ?, 1, 1, ?, ?, ?, ?, ?)",
            (
                str(differential.id), str(session_id),
                json.dumps([item.model_dump(mode="json") for item in differential.candidates]),
                json.dumps([str(fact.id)]), differential.disclaimer,
                differential.model_dump_json(), now,
            ),
        )
    return str(session_id), str(note_id)


def _client(tmp_path: Path, monkeypatch, remote: Path) -> tuple[TestClient, Database, str]:
    config_path = _settings_path(tmp_path, remote)
    database = Database(tmp_path / "scribe.db")
    database.initialize()
    session_id, _ = _seed_session(database)
    monkeypatch.setenv("CLINICAL_SCRIBE_CONFIG", str(config_path))
    return TestClient(app), database, session_id


def _approve(client: TestClient, session_id: str) -> None:
    response = client.post(
        f"/api/v1/sessions/{session_id}/clinical-note/approval",
        json={"actor": "test-clinician"},
    )
    assert response.status_code == 200, response.text


def test_phase_zero_github_keys_remain_compatible_and_safe() -> None:
    settings = GitHubSection.model_validate({
        "enabled": False,
        "repository_url": "https://github.com/example/archive.git",
        "manual_push_only": True,
        "approved_notes_only": True,
        "include_audio": False,
    })
    assert settings.repo_url.endswith("archive.git")
    assert settings.auto_push is False
    assert settings.push_audio is False


def test_pusher_rejects_audio_even_when_called_directly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLINICAL_SCRIBE_GITHUB_TOKEN", "test-token")
    pusher = GitHubPusher(str(tmp_path / "unused.git"))
    files = {
        "metadata.json": "{}",
        "clerking_sheet.md": "# Clerking",
        "clinical_note.md": "# Note",
        "treatment_plan.md": "# Treatment",
        "audit_log.json": "[]",
        "audio.wav": b"not-audio",
    }
    with pytest.raises(ValueError, match="unsupported session push path|audio files"):
        pusher.push_session(
            uuid4(), specialty="Primary Care", session_date="2026-09-21", files=files
        )


def test_all_export_formats_require_approval_and_render(tmp_path: Path, monkeypatch) -> None:
    remote = _remote_repository(tmp_path)
    client_context, database, session_id = _client(tmp_path, monkeypatch, remote)
    with client_context as client:
        for export_format in ("txt", "md", "docx", "pdf", "print", "json"):
            blocked = client.get(
                f"/api/v1/sessions/{session_id}/clinical-note/export?format={export_format}"
            )
            assert blocked.status_code == 409
        _approve(client, session_id)
        responses = {
            export_format: client.get(
                f"/api/v1/sessions/{session_id}/clinical-note/export?format={export_format}"
            )
            for export_format in ("txt", "md", "docx", "pdf", "print", "json")
        }
    assert all(response.status_code == 200 for response in responses.values())
    assert "fever" in responses["txt"].text
    assert "# Primary Care Clinical Note" in responses["md"].text
    assert "fever" in "\n".join(p.text for p in Document(io.BytesIO(responses["docx"].content)).paragraphs)
    assert responses["pdf"].content.startswith(b"%PDF")
    assert "window.print" in responses["print"].text
    assert responses["json"].json()["metadata"]["approved_note"]["approved_by"] == "test-clinician"
    with database.connect() as connection:
        formats = {row[0] for row in connection.execute("SELECT format FROM exports")}
    assert formats == {"TXT", "MD", "DOCX", "PDF", "PRINT", "JSON"}


def test_successful_manual_push_commits_exact_session_package(tmp_path: Path, monkeypatch) -> None:
    remote = _remote_repository(tmp_path)
    client_context, database, session_id = _client(tmp_path, monkeypatch, remote)
    token = "test-token-that-must-not-be-persisted"
    monkeypatch.setenv("CLINICAL_SCRIBE_GITHUB_TOKEN", token)
    with client_context as client:
        review = client.get(f"/api/v1/sessions/{session_id}/note-review").json()
        assert review["github_push"]["available"] is False
        assert 'id="pushGitHub" hidden' in client.get("/").text
        _approve(client, session_id)
        response = client.post(
            f"/api/v1/sessions/{session_id}/github/push",
            json={"actor": "test-clinician"},
        )
    assert response.status_code == 200, response.text
    remote_repo = Repo(remote)
    commit = remote_repo.commit("main")
    assert commit.message.strip() == f"[ClinicalScribe] Session {session_id} - Primary Care - {datetime.now(timezone.utc).date().isoformat()}"
    names = {item.path for item in commit.tree.traverse() if item.type == "blob"}
    prefix = f"sessions/{session_id}/"
    expected = {
        "metadata.json", "clerking_sheet.md", "clinical_note.md", "treatment_plan.md",
        "differential_diagnosis.md", "transcript/raw_transcript.txt",
        "transcript/speaker_labelled_transcript.txt", "audit_log.json",
    }
    assert {name.removeprefix(prefix) for name in names if name.startswith(prefix)} == expected
    assert not any("audio" in name.lower() for name in names)
    for name in names:
        assert token.encode("utf-8") not in (commit.tree / name).data_stream.read()
    with database.connect() as connection:
        exported = connection.execute("SELECT status FROM exports WHERE format = 'GITHUB'").fetchone()
        actions = {row[0] for row in connection.execute("SELECT action FROM audit_events")}
    assert exported["status"] == "COMPLETED"
    assert "GITHUB_PUSH_SUCCEEDED" in actions


def test_missing_token_returns_clear_error_and_is_audited(tmp_path: Path, monkeypatch) -> None:
    remote = _remote_repository(tmp_path)
    client_context, database, session_id = _client(tmp_path, monkeypatch, remote)
    monkeypatch.delenv("CLINICAL_SCRIBE_GITHUB_TOKEN", raising=False)
    with client_context as client:
        _approve(client, session_id)
        response = client.post(
            f"/api/v1/sessions/{session_id}/github/push",
            json={"actor": "test-clinician"},
        )
    assert response.status_code == 409
    assert "CLINICAL_SCRIBE_GITHUB_TOKEN" in response.json()["detail"]
    with database.connect() as connection:
        event = connection.execute(
            "SELECT after_json FROM audit_events WHERE action = 'GITHUB_PUSH_FAILED'"
        ).fetchone()
    assert event is not None and "CLINICAL_SCRIBE_GITHUB_TOKEN" in event["after_json"]


def test_unapproved_push_is_blocked_and_network_failure_is_audited(tmp_path: Path, monkeypatch) -> None:
    remote = _remote_repository(tmp_path)
    client_context, database, session_id = _client(tmp_path, monkeypatch, remote)
    monkeypatch.setenv("CLINICAL_SCRIBE_GITHUB_TOKEN", "test-token")
    with client_context as client:
        blocked = client.post(
            f"/api/v1/sessions/{session_id}/github/push",
            json={"actor": "test-clinician"},
        )
        assert blocked.status_code == 409
        assert "approval" in blocked.json()["detail"]
        _approve(client, session_id)
        client.app.state.settings.github.repo_url = str(tmp_path / "unreachable.git")
        failed = client.post(
            f"/api/v1/sessions/{session_id}/github/push",
            json={"actor": "test-clinician"},
        )
    assert failed.status_code == 502
    assert "GitHub push failed" in failed.json()["detail"]
    with database.connect() as connection:
        actions = [row[0] for row in connection.execute(
            "SELECT action FROM audit_events WHERE artefact_type = 'github_push' ORDER BY id"
        )]
        export = connection.execute(
            "SELECT status FROM exports WHERE format = 'GITHUB' ORDER BY requested_at DESC LIMIT 1"
        ).fetchone()
    assert "GITHUB_PUSH_BLOCKED" in actions
    assert "GITHUB_PUSH_FAILED" in actions
    assert export["status"] == "FAILED"
