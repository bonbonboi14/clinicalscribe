from __future__ import annotations

import json
import logging
import socket
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import UUID, uuid4

from config.settings import AppConfig
from core.audio import AudioPreprocessor
from core.diarization import CpuAcousticDiarizationEngine, DiarizationEngine
from core.languages import LanguageProcessor, LocalTranslationEngine
from core.models import JobStatus, SessionStatus, TranscriptSegment
from storage.artefacts import TranscriptArtefactStore
from storage.database import Database
from storage.jobs import (
    ClaimedJob, DiarizationStage, claim_diarization_job,
    enqueue_examination_interpretation_job, renew_lease,
)


logger = logging.getLogger(__name__)


class DiarizationWorker:
    def __init__(
        self,
        database: Database,
        settings: AppConfig,
        *,
        engine: DiarizationEngine | None = None,
        language_processor: LanguageProcessor | None = None,
        preprocessor: AudioPreprocessor | None = None,
        artefact_store: TranscriptArtefactStore | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        config = settings.diarization
        self.engine = engine or CpuAcousticDiarizationEngine(
            min_speakers=int(config.get("min_speakers", 1)),
            max_speakers=min(2, int(config.get("max_speakers", 2))),
        )
        if language_processor is None:
            multilingual = settings.multilingual
            translator = None
            if multilingual.get("translation_enabled", False):
                translator = LocalTranslationEngine(str(multilingual.get("local_model_path") or ""))
            language_processor = LanguageProcessor(translator)
        self.language_processor = language_processor
        transcription = settings.transcription
        self.preprocessor = preprocessor or AudioPreprocessor(
            sample_rate_hz=int(settings.audio.get("sample_rate_hz", 16_000)),
            silence_threshold_dbfs=float(transcription.get("silence_threshold_dbfs", -40.0)),
            silence_padding_ms=int(transcription.get("silence_padding_ms", 150)),
            target_peak_dbfs=float(transcription.get("target_peak_dbfs", -1.0)),
        )
        self.artefact_store = artefact_store or TranscriptArtefactStore(settings.storage["artifact_directory"])
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid4()}"
        self.lease_seconds = int(settings.worker.get("lease_seconds", 300))

    def process_once(self) -> bool:
        with self.database.transaction() as connection:
            job = claim_diarization_job(connection, lease_owner=self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return False
        try:
            with self._lease_heartbeat(job):
                self._process(job)
        except Exception as exc:
            logger.exception("diarization_job_failed", extra={"job_id": str(job.id)})
            self._record_failure(job, exc)
        return True

    @contextmanager
    def _lease_heartbeat(self, job: ClaimedJob) -> Iterator[None]:
        stop = threading.Event()
        interval = max(1.0, self.lease_seconds / 3)

        def heartbeat() -> None:
            while not stop.wait(interval):
                try:
                    with self.database.transaction() as connection:
                        if not renew_lease(connection, job, lease_seconds=self.lease_seconds):
                            return
                except Exception:
                    logger.exception("diarization_lease_renewal_failed", extra={"job_id": str(job.id)})

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=min(interval, 2.0))

    def _process(self, job: ClaimedJob) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT t.*, s.assembled_audio_path FROM transcripts t JOIN sessions s ON s.id = t.session_id "
                "WHERE t.session_id = ? ORDER BY t.version DESC LIMIT 1",
                (str(job.session_id),),
            ).fetchone()
        if row is None or not row["assembled_audio_path"]:
            raise ValueError("session has no transcript or assembled audio")
        transcript_id = UUID(row["id"])
        segments = [TranscriptSegment.model_validate(item) for item in json.loads(row["segments_json"])]
        processed = self.preprocessor.preprocess(
            Path(row["assembled_audio_path"]), Path(row["assembled_audio_path"]).parent / "processed"
        )
        turns = self.engine.diarize(processed.path, segments, session_id=job.session_id)
        if {turn.segment_id for turn in turns} != {segment.id for segment in segments}:
            raise ValueError("diarization engine must assign every transcript segment exactly once")
        language = self.language_processor.process(segments)
        language_by_id = {item.segment_id: item for item in language}
        turn_by_id = {item.segment_id: item for item in turns}
        version = 1
        raw_payload = {
            "session_id": str(job.session_id), "transcript_id": str(transcript_id),
            "kind": "RAW_TRANSCRIPT", "segments": [item.model_dump(mode="json") for item in segments],
        }
        clean_payload = {
            "session_id": str(job.session_id), "transcript_id": str(transcript_id),
            "kind": "CLEAN_TRANSCRIPT",
            "segments": [
                {
                    "segment_id": str(item.segment_id),
                    "source_language": item.source_language,
                    "original_text": item.original_text,
                    "clean_text": item.clean_text,
                    "confidence": item.confidence,
                }
                for item in language
            ],
        }
        translated_payload = {
            "session_id": str(job.session_id), "transcript_id": str(transcript_id), "target_language": "en",
            "kind": "TRANSLATED_TRANSCRIPT", "segments": [item.model_dump(mode="json") for item in language],
        }
        labelled_payload = {
            "session_id": str(job.session_id), "transcript_id": str(transcript_id),
            "kind": "SPEAKER_LABELLED_TRANSCRIPT",
            "segments": [
                {
                    **segment.model_dump(mode="json"),
                    "diarization_label": turn_by_id[segment.id].diarization_label,
                    "diarization_confidence": turn_by_id[segment.id].confidence,
                    "clean_text": language_by_id[segment.id].clean_text,
                    "english_text": language_by_id[segment.id].translated_text,
                    "translation_status": language_by_id[segment.id].translation_status.value,
                }
                for segment in segments
            ],
        }
        stored = {
            "RAW_TRANSCRIPT": self.artefact_store.store(job.session_id, "raw_transcript", version, raw_payload),
            "CLEAN_TRANSCRIPT": self.artefact_store.store(job.session_id, "clean_transcript", version, clean_payload),
            "TRANSLATED_TRANSCRIPT": self.artefact_store.store(job.session_id, "translated_transcript", version, translated_payload),
            "SPEAKER_LABELLED_TRANSCRIPT": self.artefact_store.store(job.session_id, "speaker_labelled_transcript", version, labelled_payload),
        }
        now = datetime.now(timezone.utc).isoformat()
        run_id = uuid4()
        with self.database.transaction() as connection:
            owned = connection.execute("SELECT status, lease_owner FROM jobs WHERE id = ?", (str(job.id),)).fetchone()
            if owned is None or owned["lease_owner"] != job.lease_owner or owned["status"] not in (JobStatus.LEASED.value, JobStatus.RUNNING.value):
                raise RuntimeError("diarization job lease was lost before persistence")
            connection.execute(
                "INSERT INTO diarization_runs(id, session_id, transcript_id, engine, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(run_id), str(job.session_id), str(transcript_id), self.engine.name, now),
            )
            speaker_ids: dict[str, UUID] = {}
            for label in sorted({turn.diarization_label for turn in turns}):
                speaker_id = uuid4()
                speaker_ids[label] = speaker_id
                connection.execute(
                    "INSERT INTO speakers(id, session_id, diarization_label, display_name, role, manually_corrected, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'UNKNOWN', 0, ?, ?)",
                    (str(speaker_id), str(job.session_id), label, label, now, now),
                )
                revision_id = uuid4()
                connection.execute(
                    "INSERT INTO speaker_revisions(id, speaker_id, session_id, version, display_name, role, is_manual, actor, created_at) "
                    "VALUES (?, ?, ?, 1, ?, 'UNKNOWN', 0, ?, ?)",
                    (str(revision_id), str(speaker_id), str(job.session_id), label, self.worker_id, now),
                )
            for turn in turns:
                connection.execute(
                    "INSERT INTO segment_speaker_assignments(id, diarization_run_id, session_id, segment_id, speaker_id, version, confidence, is_manual, actor, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?, 0, ?, ?)",
                    (str(uuid4()), str(run_id), str(job.session_id), str(turn.segment_id), str(speaker_ids[turn.diarization_label]), turn.confidence, self.worker_id, now),
                )
            for kind, item in stored.items():
                language_code = "en" if kind == "TRANSLATED_TRANSCRIPT" else None
                connection.execute(
                    "INSERT INTO transcript_artifacts(id, session_id, transcript_id, diarization_run_id, kind, version, language, storage_path, checksum_sha256, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid4()), str(job.session_id), str(transcript_id), str(run_id), kind, version, language_code, str(item.path), item.checksum_sha256, now),
                )
            connection.execute(
                "UPDATE jobs SET status = ?, stage = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE id = ? AND lease_owner = ?",
                (JobStatus.SUCCEEDED.value, DiarizationStage.COMPLETE.value, now, str(job.id), job.lease_owner),
            )
            connection.execute(
                "UPDATE sessions SET status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                (SessionStatus.PROCESSING.value, now, str(job.session_id)),
            )
            enqueue_examination_interpretation_job(
                connection, job.session_id,
                max_attempts=int(self.settings.worker.get("max_attempts", 3)), now=now,
            )
            connection.execute(
                "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, after_json, created_at) "
                "VALUES (?, 'diarization_run', ?, ?, ?, ?, ?)",
                (str(job.session_id), str(run_id), DiarizationStage.COMPLETE.value, self.worker_id, json.dumps({"engine": self.engine.name, "speaker_count": len(speaker_ids), "segment_count": len(turns)}), now),
            )

    def _record_failure(self, job: ClaimedJob, exc: Exception) -> None:
        now = datetime.now(timezone.utc)
        terminal = job.attempts >= job.max_attempts
        status = JobStatus.FAILED if terminal else JobStatus.PENDING
        error = f"{type(exc).__name__}: {exc}"[:2000]
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = ?, available_at = ?, lease_owner = NULL, lease_expires_at = NULL, error = ?, updated_at = ? "
                "WHERE id = ? AND lease_owner = ?",
                (status.value, (now + timedelta(seconds=min(60, 2**job.attempts))).isoformat(), error, now.isoformat(), str(job.id), job.lease_owner),
            )
            if cursor.rowcount == 0:
                return
            if terminal:
                connection.execute(
                    "UPDATE sessions SET status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                    (SessionStatus.FAILED.value, now.isoformat(), str(job.session_id)),
                )
            connection.execute(
                "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, after_json, created_at) "
                "VALUES (?, 'job', ?, 'DIARIZATION_FAILED', ?, ?, ?)",
                (str(job.session_id), str(job.id), self.worker_id, json.dumps({"error": error, "terminal": terminal}), now.isoformat()),
            )
