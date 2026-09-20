"""YAML clinical-note templates and local note-generation engines."""

from core.templates.base import NoteGenerationEngine, TemplateNoteEngine
from core.templates.ollama import OllamaEngine
from core.templates.schema import NoteTemplate, TemplateRegistry
from core.templates.validation import NoteHallucinationFirewall, NoteValidationError

__all__ = ["NoteGenerationEngine", "TemplateNoteEngine", "OllamaEngine", "NoteTemplate", "TemplateRegistry", "NoteHallucinationFirewall", "NoteValidationError"]
