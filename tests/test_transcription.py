from __future__ import annotations

import json
import sqlite3
import wave
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import yaml
import numpy as np

from config import load_config
from core.audio import AudioPreprocessor, PreprocessedAudio
from core.models import TranscriptSegment
from core.transcription import TranscriptionEngine, TranscriptionProfile
from core.transcription.faster_whisper import FasterWhisperEngine, PROFILES
from storage.database import Database
from storage.files import sha256_file
from storage.jobs import enqueue_transcription_job
from storage.transcripts import TranscriptFileStore
from worker.transcription import TranscriptionWorker


class FakeEngine(TranscriptionEngine):
    @property
    def name(self) -> str:
        return "fake:multilingual"

    @property
    def profile(self) -> TranscriptionProfile:
        return TranscriptionProfile.FAST

    def transcribe(
        self, audio_path: Path, *, session_id: UUID | str
    ) -> list[TranscriptSegment]:
        session_uuid = UUID(str(session_id))
        return [
            TranscriptSegment(
                session_id=session_uuid,
                sequence_number=0,
                start_ms=0,
                end_ms=900,
                source_language="ms",
                original_text=" Pesakit demam.",
                confidence=0.91,
            ),
            TranscriptSegment(
                session_id=session_uuid,
                sequence_number=1,
                start_ms=950,
                end_ms=1800,
                source_language="en",
                original_text=" No chest pain.",
                confidence=0.88,
            ),
        ]


class FakePreprocessor:
    def preprocess(self, source: Path, output_directory: Path) -> PreprocessedAudio:
        output_directory.mkdir(parents=True, exist_ok=True)
        output = output_directory / "preprocessed.wav"
        output.write_bytes(source.read_bytes())
        checksum = sha256_file(source)
        return PreprocessedAudio(
            path=output,
            source_checksum_sha256=checksum,
            output_checksum_sha256=checksum,
            sample_rate_hz=16_000,
            original_duration_ms=1800,
            processed_duration_ms=1800,
            gain_db=0.0,
            silence_trimmed=False,
        )


def _settings(tmp_path: Path):
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    config["database"]["path"] = str(tmp_path / "scribe.db")
    config["storage"]["audio_directory"] = str(tmp_path / "audio")
    config["storage"]["transcript_directory"] = str(tmp_path / "transcripts")
    config["worker"]["lease_seconds"] = 30
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return load_config(path)


def test_profiles_are_cpu_sized_and_named() -> None:
    assert PROFILES[TranscriptionProfile.FAST].model_size == "tiny"
    assert PROFILES[TranscriptionProfile.BALANCED].model_size == "small"
    assert PROFILES[TranscriptionProfile.ACCURATE].model_size == "medium"
    engine = FasterWhisperEngine(profile="fast", model=object())
    assert engine.profile is TranscriptionProfile.FAST
    assert engine.name == "faster-whisper:tiny:fast"
    assert engine._language_from_detection(("ms", 0.9, []), "und") == "ms"
    assert engine._language_from_detection([("en", 0.8)], "und") == "en"


def test_preprocessor_trims_silence_and_normalises_without_touching_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "original.webm"
    source.write_bytes(b"source-remains-immutable")
    source_checksum = sha256_file(source)
    audio = np.concatenate(
        [np.zeros(1600, dtype=np.float32), np.full(1600, 0.1, dtype=np.float32), np.zeros(1600)]
    )
    preprocessor = AudioPreprocessor(
        silence_padding_ms=0,
        decoder=lambda *_args, **_kwargs: audio,
    )
    result = preprocessor.preprocess(source, tmp_path / "processed")
    assert result.silence_trimmed is True
    assert result.original_duration_ms == 300
    assert result.processed_duration_ms == 100
    assert result.gain_db > 0
    assert sha256_file(source) == source_checksum
    with wave.open(str(result.path), "rb") as processed:
        assert processed.getframerate() == 16_000
        assert processed.getnchannels() == 1
        assert processed.getnframes() == 1600


def test_faster_whisper_detects_language_for_each_segment(
    tmp_path: Path, monkeypatch
) -> None:
    audio = np.concatenate(
        [np.full(1600, 0.1, dtype=np.float32), np.full(1600, 0.6, dtype=np.float32)]
    )

    class FakeModel:
        def transcribe(self, _audio, **_kwargs):
            return iter(
                [
                    SimpleNamespace(start=0.0, end=0.1, text=" demam", avg_logprob=-0.1),
                    SimpleNamespace(start=0.1, end=0.2, text=" fever", avg_logprob=-0.2),
                ]
            ), SimpleNamespace(language="ms")

        def detect_language(self, clip):
            language = "ms" if float(np.mean(clip)) < 0.3 else "en"
            return language, 0.9, [(language, 0.9)]

    monkeypatch.setattr("faster_whisper.audio.decode_audio", lambda *_args, **_kwargs: audio)
    source = tmp_path / "audio.wav"
    source.write_bytes(b"decoder-is-mocked")
    engine = FasterWhisperEngine(profile="fast", model=FakeModel())
    segments = engine.transcribe(source, session_id=uuid4())
    assert [segment.source_language for segment in segments] == ["ms", "en"]
    assert [(segment.start_ms, segment.end_ms) for segment in segments] == [
        (0, 100),
        (100, 200),
    ]


def test_worker_stores_timestamped_multilingual_transcript_immutably(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database.path)
    database.initialize()
    session_id = uuid4()
    original = tmp_path / "audio" / str(session_id) / "original.webm"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"immutable-original-audio")
    original_checksum = sha256_file(original)
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO sessions(id, status, created_at, updated_at, assembled_audio_path) "
            "VALUES (?, 'UPLOADING', ?, ?, ?)",
            (str(session_id), now, now, str(original)),
        )
        assert enqueue_transcription_job(
            connection, session_id, max_attempts=3, now=now
        )

    worker = TranscriptionWorker(
        database,
        settings,
        engine=FakeEngine(),
        preprocessor=FakePreprocessor(),
        transcript_store=TranscriptFileStore(settings.storage["transcript_directory"]),
        worker_id="test-worker",
    )
    assert worker.process_once() is True
    assert worker.process_once() is False
    assert sha256_file(original) == original_checksum

    with database.connect() as connection:
        job = connection.execute("SELECT * FROM jobs").fetchone()
        transcript = connection.execute("SELECT * FROM transcripts").fetchone()
        actions = {
            row[0]
            for row in connection.execute(
                "SELECT action FROM audit_events WHERE session_id = ?", (str(session_id),)
            )
        }
        assert job["status"] == "SUCCEEDED"
        assert job["stage"] == "TRANSCRIPTION_COMPLETE"
        assert transcript["source_language"] == "ms"
        segments = json.loads(transcript["segments_json"])
        assert [(s["start_ms"], s["end_ms"], s["source_language"]) for s in segments] == [
            (0, 900, "ms"),
            (950, 1800, "en"),
        ]
        raw_path = Path(transcript["raw_transcript_path"])
        assert raw_path.exists()
        assert sha256_file(raw_path) == transcript["raw_checksum_sha256"]
        assert {"TRANSCRIPTION_STARTED", "TRANSCRIPTION_COMPLETE"} <= actions

    with database.transaction() as connection:
        try:
            connection.execute(
                "UPDATE transcripts SET original_text = 'changed' WHERE session_id = ?",
                (str(session_id),),
            )
        except sqlite3.IntegrityError as exc:
            assert "immutable" in str(exc)
        else:
            raise AssertionError("transcript update should be rejected")


def test_database_migration_retains_phase_two_provenance_columns(tmp_path: Path) -> None:
    database = Database(tmp_path / "phase-two.db")
    database.initialize()
    with database.connect() as connection:
        job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
        transcript_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(transcripts)")
        }
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert "stage" in job_columns
    assert {"raw_transcript_path", "raw_checksum_sha256"} <= transcript_columns
    assert version == "8"


def test_phase_two_migration_backfills_assembled_sessions(tmp_path: Path) -> None:
    database = Database(tmp_path / "backfill.db")
    database.initialize()
    session_id = uuid4()
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO sessions(id, status, created_at, updated_at, assembled_audio_path) "
            "VALUES (?, 'UPLOADING', ?, ?, 'existing-original.webm')",
            (str(session_id), now, now),
        )
    database.initialize()
    with database.connect() as connection:
        jobs = connection.execute(
            "SELECT job_type, status FROM jobs WHERE session_id = ?", (str(session_id),)
        ).fetchall()
    assert [(row["job_type"], row["status"]) for row in jobs] == [
        ("TRANSCRIPTION", "PENDING")
    ]
