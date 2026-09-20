from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

from core.models import ClerkingSheet, ClinicalNote
from core.templates.schema import NoteTemplate


class NoteGenerationEngine(ABC):
    """A swappable engine whose only clinical input is a clerking sheet."""

    def __init__(self, template: NoteTemplate) -> None:
        self.template = template

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def generate(self, clerking_sheet: ClerkingSheet) -> ClinicalNote:
        raise NotImplementedError


class TemplateNoteEngine(NoteGenerationEngine):
    """Offline deterministic renderer; it cannot add facts or paraphrase them."""

    @property
    def name(self) -> str:
        return "structured_template"

    def generate(self, clerking_sheet: ClerkingSheet) -> ClinicalNote:
        markdown = self.render_markdown(clerking_sheet)
        plain_text = self.render_text(clerking_sheet)
        return ClinicalNote(
            session_id=clerking_sheet.session_id,
            clerking_sheet_id=clerking_sheet.id,
            language="en",
            content=markdown,
            plain_text=plain_text,
            template_name=self.template.name,
            engine=self.name,
        )

    def render_markdown(self, sheet: ClerkingSheet) -> str:
        lines = [f"# {self.template.title}"]
        for section in self.template.sections:
            lines.extend(("", f"## {section.heading}"))
            values = [(section.heading, section.static_text)] if section.static_text else list(self._section_values(sheet, section.paths))
            if section.style == "paragraph":
                lines.append(" ".join(value for _, value in values))
            elif section.style == "fields":
                lines.extend(f"- **{label}:** {value}" for label, value in values)
            else:
                lines.extend(f"- {value}" for _, value in values)
        return "\n".join(lines).strip() + "\n"

    def render_text(self, sheet: ClerkingSheet) -> str:
        lines = [self.template.title.upper()]
        for section in self.template.sections:
            lines.extend(("", section.heading.upper()))
            values = [(section.heading, section.static_text)] if section.static_text else list(self._section_values(sheet, section.paths))
            if section.style == "paragraph":
                lines.append(" ".join(value for _, value in values))
            elif section.style == "fields":
                lines.extend(f"{label}: {value}" for label, value in values)
            else:
                lines.extend(f"- {value}" for _, value in values)
        return "\n".join(lines).strip() + "\n"

    @classmethod
    def _section_values(cls, sheet: ClerkingSheet, paths: Iterable[str]):
        data = sheet.model_dump(mode="python")
        for path in paths:
            value: Any = data
            for part in path.split("."):
                if not isinstance(value, dict) or part not in value:
                    raise ValueError(f"template references unknown clerking-sheet field: {path}")
                value = value[part]
            label = path.split(".")[-1].replace("_", " ").title()
            for item in cls._flatten(value):
                yield label, item

    @classmethod
    def _flatten(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for value_item in value for item in cls._flatten(value_item)]
        if isinstance(value, dict):
            return [f"{key.replace('_', ' ').title()}: {item}" for key, child in value.items() for item in cls._flatten(child)]
        raise ValueError("template field has unsupported value type")
