from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from api.main import app
from config import load_config
from storage.database import Database


EXPECTED_TABLES = {
    "sessions",
    "patients",
    "audio_chunks",
    "jobs",
    "transcripts",
    "speakers",
    "structured_facts",
    "clerking_sheets",
    "notes",
    "treatment_plans",
    "differentials",
    "examination_mappings",
    "templates",
    "exports",
    "audit_events",
    "validation_findings",
}


def test_config_loads() -> None:
    settings = load_config("config/config.yaml")
    assert settings.app.local_only is True
    assert settings.differential["enabled"] is False
    assert settings.github.manual_push_only is True
    assert settings.github.approved_notes_only is True
    assert settings.github.include_audio is False


def test_database_initializes_all_required_tables(tmp_path: Path) -> None:
    database = Database(tmp_path / "smoke.db")
    database.initialize()
    with database.connect() as connection:
        actual = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert EXPECTED_TABLES <= actual


def test_app_starts_and_health_is_ok(tmp_path: Path, monkeypatch) -> None:
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    config["database"]["path"] = str(tmp_path / "smoke.db")
    config["storage"]["audio_directory"] = str(tmp_path / "audio")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("CLINICAL_SCRIBE_CONFIG", str(config_path))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
