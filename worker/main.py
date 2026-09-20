from __future__ import annotations

import logging
import time

from config import load_config
from core.logging import configure_logging
from storage import initialize_database
from worker.transcription import TranscriptionWorker
from worker.diarization import DiarizationWorker
from worker.examination import ExaminationInterpretationWorker
from worker.structuring import StructuringWorker
from worker.notes import NoteGenerationWorker


def run() -> None:
    settings = load_config()
    configure_logging(str(settings.logging.get("level", "INFO")))
    database = initialize_database(
        settings.database.path,
        busy_timeout_ms=settings.database.busy_timeout_ms,
        journal_mode=settings.database.journal_mode,
        foreign_keys=settings.database.foreign_keys,
    )
    logger = logging.getLogger(__name__)
    logger.info("worker_started")
    poll_seconds = float(settings.worker.get("poll_interval_seconds", 1.0))
    transcription_worker = TranscriptionWorker(database, settings)
    diarization_worker = DiarizationWorker(database, settings)
    examination_worker = ExaminationInterpretationWorker(database, settings)
    structuring_worker = StructuringWorker(database, settings)
    note_worker = NoteGenerationWorker(database, settings)
    try:
        while True:
            processed = transcription_worker.process_once()
            processed = diarization_worker.process_once() or processed
            processed = examination_worker.process_once() or processed
            processed = structuring_worker.process_once() or processed
            processed = note_worker.process_once() or processed
            if not processed:
                time.sleep(poll_seconds)
    except KeyboardInterrupt:
        logger.info("worker_stopped")


if __name__ == "__main__":
    run()
