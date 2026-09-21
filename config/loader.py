from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config.settings import load_config


@dataclass(frozen=True)
class DiagnosisConfig:
    enabled: bool = False


def get_diagnosis_config(path: str | Path | None = None) -> DiagnosisConfig:
    return DiagnosisConfig(enabled=load_config(path).diagnosis.enabled)
