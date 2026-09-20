from __future__ import annotations

import json
import logging
import socket
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Sequence
from uuid import UUID, uuid4

from config.settings import AppConfig
from core.audio import AudioPreprocessor
from core.models import JobStatus, SessionStatus, TranscriptSegment
from core.transcription import FasterWhisperEngine, TranscriptionEngine, TranscriptionProfile
from storage.database import Database
from storage.jobs import (
    ClaimedJob,
    TranscriptionStage,
    claim_transcription_job,
    enqueue_diarization_job,
    renew_lease,
)
from storage.transcripts import TranscriptFileStore


logger = logging.getLogger(__name__)


class TranscriptionWorker:
    def __init__(
        self,
        database: Database,
        settings: AppConfig,
        *,
        engine: TranscriptionEngine | None = None,
        preprocessor: AudioPreprocessor | None = None,
        transcript_store: TranscriptFileStore | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        transcription = settings.transcription
        profile = TranscriptionProfile(str(transcription.get("profile", "BALANCED")).upper())
        self.engine = engine or FasterWhisperEngine(
            profile=profile,
            model_size=transcription.get("model"),
            device=str(transcription.get("device", "cpu")),
            compute_type=str(transcription.get("compute_type", "int8")),
            cpu_threads=int(transcription.get("cpu_threads", 0)),
        )
        self.preprocessor = preprocessor or AudioPreprocessor(
            sample_rate_hz=int(settings.audio.get("sample_rate_hz", 16_000)),
            silence_threshold_dbfs=float(
                transcription.get("silence_threshold_dbfs", -40.0)
            ),
            silence_padding_ms=int(transcription.get("silence_padding_ms", 150)),
            target_peak_dbfs=float(transcription.get("target_peak_dbfs", -1.0)),
        )
        self.transcript_store = transcript_store or TranscriptFileStore(
            settings.storage["transcript_directory"]
        )
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid4()}"
        self.lease_seconds = int(settings.worker.get("lease_seconds", 300))

    def process_once(self) -> bool:
        with self.database.transaction() as connection:
            job = claim_transcription_job(
                connection,
                lease_owner=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
        if job is None:
            return False

        try:
            with self._lease_heartbeat(job):
                self._process(job)
        except Exception as exc:
            logger.exception("transcription_job_failed", extra={"job_id": str(job.id)})
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
                        if not renew_lease(
                            connection, job, lease_seconds=self.lease_seconds
                        ):
                            logger.error("transcription_lease_lost", extra={"job_id": str(job.id)})
                            return
                except Exception:
                    logger.exception(
                        "transcription_lease_renewal_failed",
                        extra={"job_id": str(job.id)},
                    )

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=min(interval, 2.0))

    @staticmethod
    def _validate_segments(
        segments: Sequence[TranscriptSegment], session_id: UUID
    ) -> None:
        previous_start = 0
        for expected_sequence, segment in enumerate(segments):
            if segment.session_id != session_id:
                raise ValueError("engine returned a segment for a different session")
            if segment.sequence_number != expected_sequence:
                raise ValueError("engine returned non-contiguous segment sequence numbers")
            if segment.start_ms < previous_start:
                raise ValueError("engine returned out-of-order segments")
            previous_start = segment.start_ms

    def _process(self, job: ClaimedJob) -> None:
        with self.database.connect() as connection:
            session = connection.execute(
                "SELECT assembled_audio_path FROM sessions WHERE id = ?",
                (str(job.session_id),),
            ).fetchone()
        if session is None or not session["assembled_audio_path"]:
            raise ValueError("session has no completed assembled audio")

        original_path = Path(session["assembled_audio_path"])
        processed = self.preprocessor.preprocess(
            original_path, original_path.parent / "processed"
        )
        segments = self.engine.transcribe(processed.path, session_id=job.session_id)
        self._validate_segments(segments, job.session_id)

        with self.database.connect() as connection:
            next_version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM transcripts WHERE session_id = ?",
                (str(job.session_id),),
            ).fetchone()[0]
        stored = self.transcript_store.store(
            job.session_id, next_version, segments, engine=self.engine.name
        )
        now = datetime.now(timezone.utc).isoformat()
        source_language = self._primary_language(segments)
        original_text = "".join(segment.original_text for segment in segments)
        segments_json = json.dumps(
            [segment.model_dump(mode="json") for segment in segments],
            ensure_ascii=False,
            separators=(",", ":"),
        )

        with self.database.transaction() as connection:
            owned = connection.execute(
                "SELECT status, lease_owner FROM jobs WHERE id = ?",
                (str(job.id),),
            ).fetchone()
            if (
                owned is None
                or owned["lease_owner"] != job.lease_owner
                or owned["status"] not in (JobStatus.LEASED.value, JobStatus.RUNNING.value)
            ):
                raise RuntimeError("transcription job lease was lost before persistence")
            transcript_id = uuid4()
            connection.execute(
                "INSERT INTO transcripts(id, session_id, version, source_language, "
                "original_text, normalized_text, segments_json, engine, raw_transcript_path, "
                "raw_checksum_sha256, created_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (
                    str(transcript_id),
                    str(job.session_id),
                    next_version,
                    source_language,
                    original_text,
                    segments_json,
                    self.engine.name,
                    str(stored.path),
                    stored.checksum_sha256,
                    now,
                ),
            )
            connection.execute(
                "UPDATE jobs SET status = ?, stage = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE id = ? AND lease_owner = ?",
                (
                    JobStatus.SUCCEEDED.value,
                    TranscriptionStage.COMPLETE.value,
                    now,
                    str(job.id),
                    job.lease_owner,
                ),
            )
            connection.execute(
                "UPDATE sessions SET source_language = COALESCE(source_language, ?), "
                "updated_at = ?, version = version + 1 WHERE id = ?",
                (source_language, now, str(job.session_id)),
            )
            connection.execute(
                "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, "
                "actor, after_json, created_at) VALUES (?, 'transcript', ?, ?, ?, ?, ?)",
                (
                    str(job.session_id),
                    str(transcript_id),
                    TranscriptionStage.COMPLETE.value,
                    self.worker_id,
                    json.dumps(
                        {
                            "engine": self.engine.name,
                            "raw_checksum_sha256": stored.checksum_sha256,
                            "segment_count": len(segments),
                            "version": next_version,
                        }
                    ),
                    now,
                ),
            )
            if enqueue_diarization_job(
                connection,
                job.session_id,
                max_attempts=int(self.settings.worker.get("max_attempts", 3)),
                now=now,
            ):
                connection.execute(
                    "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, "
                    "actor, after_json, created_at) VALUES (?, 'job', NULL, "
                    "'DIARIZATION_QUEUED', ?, '{}', ?)",
                    (str(job.session_id), self.worker_id, now),
                )

    @staticmethod
    def _primary_language(segments: Sequence[TranscriptSegment]) -> str | None:
        if not segments:
            return None
        weights: Counter[str] = Counter()
        for segment in segments:
            weights[segment.source_language] += max(1, len(segment.original_text.strip()))
        return weights.most_common(1)[0][0]

    def _record_failure(self, job: ClaimedJob, exc: Exception) -> None:
        now = datetime.now(timezone.utc)
        terminal = job.attempts >= job.max_attempts
        status = JobStatus.FAILED if terminal else JobStatus.PENDING
        available_at = (now + timedelta(seconds=min(60, 2**job.attempts))).isoformat()
        error = f"{type(exc).__name__}: {exc}"[:2000]
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = ?, available_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, error = ?, updated_at = ? "
                "WHERE id = ? AND lease_owner = ?",
                (
                    status.value,
                    available_at,
                    error,
                    now.isoformat(),
                    str(job.id),
                    job.lease_owner,
                ),
            )
            if cursor.rowcount == 0:
                return
            if terminal:
                connection.execute(
                    "UPDATE sessions SET status = ?, updated_at = ?, version = version + 1 "
                    "WHERE id = ?",
                    (SessionStatus.FAILED.value, now.isoformat(), str(job.session_id)),
                )
            connection.execute(
                "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, "
                "actor, after_json, created_at) VALUES (?, 'job', ?, 'TRANSCRIPTION_FAILED', "
                "?, ?, ?)",
                (
                    str(job.session_id),
                    str(job.id),
                    self.worker_id,
                    json.dumps({"error": error, "terminal": terminal}),
                    now.isoformat(),
                ),
            )
