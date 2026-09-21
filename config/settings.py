from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppSection(StrictModel):
    name: str
    environment: str
    version: str
    local_only: bool = True


class ServerSection(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    reload: bool = False


class DatabaseSection(StrictModel):
    path: Path
    busy_timeout_ms: int = Field(default=5000, ge=0)
    journal_mode: str = "WAL"
    foreign_keys: bool = True


class GitHubSection(StrictModel):
    enabled: bool = False
    repository_url: str = ""
    remote_name: str = "origin"
    branch: str = "main"
    token_env: str = "CLINICAL_SCRIBE_GITHUB_TOKEN"
    manual_push_only: bool = True
    approved_notes_only: bool = True
    include_audio: bool = False

    @model_validator(mode="after")
    def enforce_safety(self) -> "GitHubSection":
        if not self.manual_push_only or not self.approved_notes_only or self.include_audio:
            raise ValueError("GitHub safety controls cannot be disabled")
        if self.token_env != "CLINICAL_SCRIBE_GITHUB_TOKEN":
            raise ValueError("GitHub token must use CLINICAL_SCRIBE_GITHUB_TOKEN")
        return self


class DiagnosisSection(StrictModel):
    enabled: bool = False


class AppConfig(StrictModel):
    app: AppSection
    server: ServerSection
    database: DatabaseSection
    storage: dict[str, Any]
    audio: dict[str, Any]
    upload: dict[str, Any]
    worker: dict[str, Any]
    transcription: dict[str, Any]
    diarization: dict[str, Any]
    multilingual: dict[str, Any]
    examination: dict[str, Any]
    extraction: dict[str, Any]
    validation: dict[str, Any]
    clerking: dict[str, Any]
    note_generation: dict[str, Any]
    treatment: dict[str, Any]
    diagnosis: DiagnosisSection
    differential: dict[str, Any]
    review: dict[str, Any]
    export: dict[str, Any]
    github: GitHubSection
    logging: dict[str, Any]


def load_config(path: str | Path | None = None) -> AppConfig:
    configured_path = path or os.getenv("CLINICAL_SCRIBE_CONFIG", "config/config.yaml")
    config_path = Path(configured_path).resolve()
    with config_path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise ValueError(f"Configuration root must be a mapping: {config_path}")
    return AppConfig.model_validate(raw)
