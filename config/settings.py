from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


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
    repo_url: str = Field(
        default="",
        validation_alias=AliasChoices("repo_url", "repository_url"),
    )
    remote_name: str = "origin"
    branch: str = "main"
    token_env: str = "CLINICAL_SCRIBE_GITHUB_TOKEN"
    auto_push: bool = False
    push_transcripts: bool = True
    push_audio: bool = Field(
        default=False,
        validation_alias=AliasChoices("push_audio", "include_audio"),
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_phase_zero_keys(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if "repo_url" in migrated and "repository_url" in migrated:
            if migrated["repo_url"] != migrated["repository_url"]:
                raise ValueError("conflicting repo_url and repository_url values")
            migrated.pop("repository_url")
        if "push_audio" in migrated and "include_audio" in migrated:
            if bool(migrated["push_audio"]) != bool(migrated["include_audio"]):
                raise ValueError("conflicting push_audio and include_audio values")
            migrated.pop("include_audio")
        if "manual_push_only" in migrated:
            if not bool(migrated.pop("manual_push_only")):
                raise ValueError("GitHub manual_push_only cannot be false")
            migrated.setdefault("auto_push", False)
        if "approved_notes_only" in migrated and not bool(migrated.pop("approved_notes_only")):
            raise ValueError("GitHub approved_notes_only cannot be false")
        return migrated

    @model_validator(mode="after")
    def enforce_safety(self) -> "GitHubSection":
        if self.auto_push:
            raise ValueError("GitHub auto_push must remain false; pushes are manual only")
        if self.push_audio:
            raise ValueError("GitHub push_audio must remain false; audio is never pushed")
        if self.token_env != "CLINICAL_SCRIBE_GITHUB_TOKEN":
            raise ValueError("GitHub token must use CLINICAL_SCRIBE_GITHUB_TOKEN")
        return self

    @property
    def repository_url(self) -> str:
        """Phase 0 compatibility alias; new code uses repo_url."""
        return self.repo_url

    @property
    def manual_push_only(self) -> bool:
        """Compatibility view of the enforced manual-only policy."""
        return not self.auto_push

    @property
    def approved_notes_only(self) -> bool:
        """Approval is an invariant of the push service and API."""
        return True

    @property
    def include_audio(self) -> bool:
        """Compatibility view of the enforced no-audio policy."""
        return self.push_audio


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
