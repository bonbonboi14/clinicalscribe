from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import wave
from array import array
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import yaml
from fastapi.testclient import TestClient

from api.main import app
from config import load_config
from core.audio import PreprocessedAudio
from core.diarization import CpuAcousticDiarizationEngine, DiarizationEngine, SpeakerTurn
from core.languages import LanguageProcessor, TranslationEngine
from core.models import TranscriptSegment
from storage.artefacts import TranscriptArtefactStore
from storage.database import Database
from storage.files import sha256_file
from storage.jobs import enqueue_diarization_job
from worker.diarization import DiarizationWorker


class FakeDiarizer(DiarizationEngine):
    @property
    def name(self) -> str:
        return "fake-diarizer"

    def diarize(self, audio_path: Path, segments, *, session_id: UUID | str):
        return [
            SpeakerTurn(
                segment_id=segment.id,
                diarization_label=f"SPEAKER_{index % 2:02d}",
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                confidence=0.9,
            )
            for index, segment in enumerate(segments)
        ]


class FakeTranslator(TranslationEngine):
    @property
    def name(self) -> str:
        return "fake-local"

    def translate(self, texts, *, source_language: str, target_language: str = "en"):
        assert source_language == "ms"
        return ["Patient has fever." for _ in texts]


class FakePreprocessor:
    def preprocess(self, source: Path, output_directory: Path) -> PreprocessedAudio:
        return PreprocessedAudio(
            path=source,
            source_checksum_sha256=sha256_file(source),
            output_checksum_sha256=sha256_file(source),
            sample_rate_hz=16_000,
            original_duration_ms=2_000,
            processed_duration_ms=2_000,
            gain_db=0.0,
            silence_trimmed=False,
        )


def _settings(tmp_path: Path):
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    config["database"]["path"] = str(tmp_path / "scribe.db")
    config["storage"]["audio_directory"] = str(tmp_path / "audio")
    config["storage"]["transcript_directory"] = str(tmp_path / "transcripts")
    config["storage"]["artifact_directory"] = str(tmp_path / "artifacts")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path, load_config(path)


def _phase_two_state(database: Database, tmp_path: Path) -> UUID:
    session_id = uuid4()
    now = datetime.now(timezone.utc).isoformat()
    audio = tmp_path / "original.wav"
    audio.write_bytes(b"immutable-audio")
    segments = [
        TranscriptSegment(
            session_id=session_id, sequence_number=0, start_ms=0, end_ms=900,
            source_language="ms", original_text="  Pesakit demam. ", confidence=0.9,
        ),
        TranscriptSegment(
            session_id=session_id, sequence_number=1, start_ms=1_000, end_ms=1_900,
            source_language="en", original_text=" No chest pain. ", confidence=0.88,
        ),
    ]
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps([item.model_dump(mode="json") for item in segments]), encoding="utf-8")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO sessions(id, status, created_at, updated_at, assembled_audio_path) VALUES (?, 'PROCESSING', ?, ?, ?)",
            (str(session_id), now, now, str(audio)),
        )
        connection.execute(
            "INSERT INTO transcripts(id, session_id, version, source_language, original_text, segments_json, engine, raw_transcript_path, raw_checksum_sha256, created_at) "
            "VALUES (?, ?, 1, 'ms', ?, ?, 'fake', ?, ?, ?)",
            (str(uuid4()), str(session_id), " Pesakit demam. No chest pain.",
             json.dumps([item.model_dump(mode="json") for item in segments]), str(raw),
             hashlib.sha256(raw.read_bytes()).hexdigest(), now),
        )
        assert enqueue_diarization_job(connection, session_id, max_attempts=3, now=now)
    return session_id


def test_cpu_diarizer_returns_stable_machine_labels_without_role_inference(tmp_path: Path) -> None:
    session_id = uuid4()
    path = tmp_path / "voices.wav"
    rate = 16_000
    samples = array("h")
    for frequency in (120, 640):
        samples.extend(int(12_000 * math.sin(2 * math.pi * frequency * index / rate)) for index in range(rate))
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(samples.tobytes())
    segments = [
        TranscriptSegment(session_id=session_id, sequence_number=0, start_ms=0, end_ms=1_000, source_language="en", original_text="One", confidence=0.9),
        TranscriptSegment(session_id=session_id, sequence_number=1, start_ms=1_000, end_ms=2_000, source_language="en", original_text="Two", confidence=0.9),
    ]
    turns = CpuAcousticDiarizationEngine(min_speakers=2, max_speakers=2).diarize(path, segments, session_id=session_id)
    assert [turn.diarization_label for turn in turns] == ["SPEAKER_00", "SPEAKER_01"]
    assert all(0.5 <= turn.confidence <= 0.99 for turn in turns)


def test_diarization_worker_preserves_languages_and_creates_separate_artefacts(tmp_path: Path) -> None:
    _, settings = _settings(tmp_path)
    database = Database(settings.database.path)
    database.initialize()
    session_id = _phase_two_state(database, tmp_path)
    worker = DiarizationWorker(
        database, settings, engine=FakeDiarizer(),
        language_processor=LanguageProcessor(FakeTranslator()),
        preprocessor=FakePreprocessor(),
        artefact_store=TranscriptArtefactStore(settings.storage["artifact_directory"]),
        worker_id="test-worker",
    )
    assert worker.process_once() is True
    with database.connect() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE job_type = 'DIARIZATION'").fetchone()
        kinds = {row[0] for row in connection.execute("SELECT kind FROM transcript_artifacts")}
        speakers = connection.execute("SELECT diarization_label FROM speakers ORDER BY diarization_label").fetchall()
        revisions = connection.execute("SELECT role, is_manual FROM speaker_revisions").fetchall()
        original = connection.execute("SELECT original_text, segments_json FROM transcripts").fetchone()
    assert job["stage"] == "DIARIZATION_COMPLETE"
    assert kinds == {"RAW_TRANSCRIPT", "CLEAN_TRANSCRIPT", "TRANSLATED_TRANSCRIPT", "SPEAKER_LABELLED_TRANSCRIPT"}
    assert [row[0] for row in speakers] == ["SPEAKER_00", "SPEAKER_01"]
    assert {(row["role"], row["is_manual"]) for row in revisions} == {("UNKNOWN", 0)}
    assert original["original_text"] == " Pesakit demam. No chest pain."
    translated = next((tmp_path / "artifacts" / "transcripts" / str(session_id)).glob("translated_transcript-*.json"))
    payload = json.loads(translated.read_text(encoding="utf-8"))
    assert payload["segments"][0]["original_text"] == "  Pesakit demam. "
    assert payload["segments"][0]["translated_text"] == "Patient has fever."
    assert payload["segments"][1]["translated_text"] == "No chest pain."
    with database.transaction() as connection:
        try:
            connection.execute("UPDATE speaker_revisions SET role = 'DOCTOR'")
        except sqlite3.IntegrityError as exc:
            assert "immutable" in str(exc)
        else:
            raise AssertionError("speaker revision update should be rejected")


def test_phase_three_migration_queues_existing_latest_transcript(tmp_path: Path) -> None:
    database = Database(tmp_path / "migration.db")
    database.initialize()
    session_id = uuid4()
    now = datetime.now(timezone.utc).isoformat()
    segment = TranscriptSegment(
        session_id=session_id, sequence_number=0, start_ms=0, end_ms=100,
        source_language="en", original_text="Hello", confidence=0.9,
    )
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO sessions(id, status, created_at, updated_at, assembled_audio_path) VALUES (?, 'PROCESSING', ?, ?, 'audio.wav')",
            (str(session_id), now, now),
        )
        connection.execute(
            "INSERT INTO transcripts(id, session_id, version, original_text, segments_json, engine, created_at) VALUES (?, ?, 1, 'Hello', ?, 'fake', ?)",
            (str(uuid4()), str(session_id), json.dumps([segment.model_dump(mode="json")]), now),
        )
    database.initialize()
    with database.connect() as connection:
        jobs = connection.execute(
            "SELECT job_type, status FROM jobs WHERE session_id = ? ORDER BY job_type", (str(session_id),)
        ).fetchall()
    assert [(row["job_type"], row["status"]) for row in jobs] == [("DIARIZATION", "PENDING")]


def test_review_api_appends_manual_role_and_assignment_revisions(tmp_path: Path, monkeypatch) -> None:
    config_path, settings = _settings(tmp_path)
    database = Database(settings.database.path)
    database.initialize()
    session_id = _phase_two_state(database, tmp_path)
    worker = DiarizationWorker(
        database, settings, engine=FakeDiarizer(), language_processor=LanguageProcessor(),
        preprocessor=FakePreprocessor(), artefact_store=TranscriptArtefactStore(settings.storage["artifact_directory"]),
        worker_id="test-worker",
    )
    worker.process_once()
    monkeypatch.setenv("CLINICAL_SCRIBE_CONFIG", str(config_path))
    with TestClient(app) as client:
        initial = client.get(f"/api/v1/sessions/{session_id}/speaker-review").json()
        first, second = initial["speakers"]
        response = client.post(
            f"/api/v1/sessions/{session_id}/speaker-corrections",
            json={
                "actor": "clinician-test",
                "speakers": [
                    {"speaker_id": first["id"], "display_name": "Dr Lee", "role": "DOCTOR"},
                    {"speaker_id": second["id"], "display_name": "Patient", "role": "PATIENT"},
                ],
                "segment_assignments": [
                    {"segment_id": initial["segments"][0]["segment_id"], "speaker_id": second["id"]}
                ],
            },
        )
        assert response.status_code == 200, response.text
        reviewed = response.json()
    assert {item["role"] for item in reviewed["speakers"]} == {"DOCTOR", "PATIENT"}
    assert reviewed["segments"][0]["speaker_id"] == second["id"]
    assert reviewed["segments"][0]["assignment_revision"] == 2
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM speaker_revisions").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM segment_speaker_assignments").fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM transcript_artifacts WHERE kind = 'SPEAKER_LABELLED_TRANSCRIPT'"
        ).fetchone()[0] == 2
