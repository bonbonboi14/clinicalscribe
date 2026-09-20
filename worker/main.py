from __future__ import annotations

import logging
import time

from config import load_config
from core.logging import configure_logging
from storage import initialize_database


def run() -> None:
    settings = load_config()
    configure_logging(str(settings.logging.get("level", "INFO")))
    initialize_database(settings.database.path)
    logger = logging.getLogger(__name__)
    logger.info("worker_started")
    poll_seconds = float(settings.worker.get("poll_interval_seconds", 1.0))
    try:
        while True:
            # Job claiming and AI pipeline orchestration arrive in later phases.
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        logger.info("worker_stopped")


if __name__ == "__main__":
    run()

