"""Compatibility import for the Phase 0 multilingual package name.

New code should import from :mod:`core.languages`.
"""

from core.languages import LanguageProcessor, LocalTranslationEngine, TranslationEngine

__all__ = ["LanguageProcessor", "LocalTranslationEngine", "TranslationEngine"]
