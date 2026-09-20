from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import load_config
from core.logging import configure_logging
from storage import initialize_database


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_config()
    configure_logging(str(settings.logging.get("level", "INFO")))
    initialize_database(
        settings.database.path,
        busy_timeout_ms=settings.database.busy_timeout_ms,
        journal_mode=settings.database.journal_mode,
        foreign_keys=settings.database.foreign_keys,
    )
    app.state.settings = settings
    logging.getLogger(__name__).info("application_started", extra={"version": settings.app.version})
    yield
    logging.getLogger(__name__).info("application_stopped")


app = FastAPI(title="Clinical Scribe", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

