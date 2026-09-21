from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.sessions import router as sessions_router
from api.speakers import router as speakers_router
from api.clerking import router as clerking_router
from api.examination import router as examination_router
from api.notes import router as notes_router
from api.treatment import router as treatment_router
from config import load_config
from core.logging import configure_logging
from storage import initialize_database
from storage.files import AudioFileStore
from storage.artefacts import TranscriptArtefactStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_config()
    configure_logging(str(settings.logging.get("level", "INFO")))
    database = initialize_database(
        settings.database.path,
        busy_timeout_ms=settings.database.busy_timeout_ms,
        journal_mode=settings.database.journal_mode,
        foreign_keys=settings.database.foreign_keys,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.audio_file_store = AudioFileStore(settings.storage["audio_directory"])
    app.state.transcript_artefact_store = TranscriptArtefactStore(settings.storage["artifact_directory"])
    logging.getLogger(__name__).info("application_started", extra={"version": settings.app.version})
    yield
    logging.getLogger(__name__).info("application_stopped")


app = FastAPI(title="Clinical Scribe", version="0.1.0", lifespan=lifespan)
app.include_router(sessions_router)
app.include_router(speakers_router)
app.include_router(clerking_router)
app.include_router(examination_router)
app.include_router(notes_router)
app.include_router(treatment_router)

_STATIC_DIRECTORY = Path(__file__).with_name("static")
app.mount("/static", StaticFiles(directory=_STATIC_DIRECTORY), name="static")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def desktop_ui() -> FileResponse:
    return FileResponse(_STATIC_DIRECTORY / "index.html")
