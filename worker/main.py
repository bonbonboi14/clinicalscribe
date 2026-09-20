from __future__ import annotations

import logging
import time

from config import load_config
from core.logging import configure_logging
from storage import initialize_database
from worker.transcription import TranscriptionWorker


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
    worker = TranscriptionWorker(database, settings)
    try:
        while True:
            if not worker.process_once():
                time.sleep(poll_seconds)
    except KeyboardInterrupt:
        logger.info("worker_stopped")


if __name__ == "__main__":
    run()
