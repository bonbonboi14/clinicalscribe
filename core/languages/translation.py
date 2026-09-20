from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence


class TranslationEngine(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def translate(self, texts: Sequence[str], *, source_language: str, target_language: str = "en") -> list[str]:
        raise NotImplementedError


class LocalTranslationEngine(TranslationEngine):
    """Local-only Transformers adapter; it never downloads model files."""

    def __init__(self, model_path: str, *, pipeline: object | None = None) -> None:
        if not model_path:
            raise ValueError("a local translation model path is required")
        self.model_path = model_path
        self._pipeline = pipeline

    @property
    def name(self) -> str:
        return f"local-transformers:{self.model_path}"

    def translate(self, texts: Sequence[str], *, source_language: str, target_language: str = "en") -> list[str]:
        if target_language != "en":
            raise ValueError("Phase 3 supports English as the translation target")
        pipeline = self._get_pipeline(source_language)
        results = pipeline(list(texts))
        translated = [str(item["translation_text"]) for item in results]
        if len(translated) != len(texts):
            raise RuntimeError("translation engine returned a mismatched result count")
        return translated

    def _get_pipeline(self, source_language: str):
        if self._pipeline is None:
            try:
                from transformers import AutoModelForSeq2SeqLM, AutoTokenizer, pipeline
            except ImportError as exc:
                raise RuntimeError("install the 'translation' optional dependency") from exc
            tokenizer = AutoTokenizer.from_pretrained(self.model_path, local_files_only=True)
            model = AutoModelForSeq2SeqLM.from_pretrained(self.model_path, local_files_only=True)
            self._pipeline = pipeline(
                "translation",
                model=model,
                tokenizer=tokenizer,
                device=-1,
                src_lang=source_language,
                tgt_lang="en",
            )
        return self._pipeline
