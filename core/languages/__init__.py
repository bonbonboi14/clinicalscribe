"""Multilingual metadata, source preservation, and optional English translation."""

from core.languages.processor import LanguageProcessor
from core.languages.translation import LocalTranslationEngine, TranslationEngine

__all__ = ["LanguageProcessor", "LocalTranslationEngine", "TranslationEngine"]
