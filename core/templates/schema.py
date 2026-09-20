from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TemplateSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    heading: str = Field(min_length=1)
    paths: list[str] = Field(default_factory=list)
    static_text: str | None = None
    style: Literal["paragraph", "list", "fields"] = "list"

    @field_validator("paths")
    @classmethod
    def safe_paths(cls, paths: list[str]) -> list[str]:
        for path in paths:
            if not path or any(part.startswith("_") for part in path.split(".")):
                raise ValueError(f"unsafe clerking-sheet path: {path!r}")
        return paths

    @model_validator(mode="after")
    def require_one_source(self) -> "TemplateSection":
        if bool(self.paths) == bool(self.static_text):
            raise ValueError("a template section must define paths or non-clinical static_text, but not both")
        return self


class NoteTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    language: Literal["en"] = "en"
    title: str = Field(min_length=1)
    sections: list[TemplateSection] = Field(min_length=1)


class TemplateRegistry:
    """Loads strict templates from YAML without executing template expressions."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()

    def load(self, name: str) -> NoteTemplate:
        if not name or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in name):
            raise ValueError("invalid template name")
        path = self.directory / f"{name}.yaml"
        if path.parent != self.directory or not path.is_file():
            raise FileNotFoundError(f"note template not found: {name}")
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        template = NoteTemplate.model_validate(payload)
        if template.name != name:
            raise ValueError("template filename and declared name differ")
        return template

    def names(self) -> list[str]:
        return sorted(path.stem for path in self.directory.glob("*.yaml") if path.is_file())
