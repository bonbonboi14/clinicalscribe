from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MappingResult:
    interpreted_text: str
    confidence: float
    mapping_key: str


@dataclass(frozen=True)
class _MappingRule:
    key: str
    pattern: re.Pattern[str]
    term: str
    confidence: float


class PhraseToClinicalTermMapper:
    """Deterministic YAML-backed phrase mapper with no clinical inference."""

    def __init__(self, rules: list[_MappingRule]) -> None:
        self._rules = tuple(rules)

    @classmethod
    def from_directory(cls, directory: str | Path | None = None) -> "PhraseToClinicalTermMapper":
        root = Path(directory) if directory else Path(__file__).resolve().parents[2] / "config" / "examination_mappings"
        if not root.is_dir():
            raise ValueError(f"examination mapping directory does not exist: {root}")
        rules: list[_MappingRule] = []
        for path in sorted(root.glob("*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            entries = payload.get("mappings", [])
            if not isinstance(entries, list):
                raise ValueError(f"mappings must be a list: {path}")
            for index, entry in enumerate(entries):
                try:
                    key = str(entry.get("id") or f"{path.stem}:{index}")
                    pattern = re.compile(str(entry["pattern"]), re.IGNORECASE)
                    term = str(entry["term"])
                    confidence = float(entry.get("confidence", 0.9))
                except (KeyError, TypeError, ValueError, re.error) as exc:
                    raise ValueError(f"invalid examination mapping {path}:{index + 1}: {exc}") from exc
                if not 0 <= confidence <= 1:
                    raise ValueError(f"mapping confidence must be between 0 and 1: {key}")
                rules.append(_MappingRule(key=key, pattern=pattern, term=term, confidence=confidence))
        if not rules:
            raise ValueError(f"no examination mappings found in: {root}")
        return cls(rules)

    def map_phrase(self, phrase: str) -> MappingResult | None:
        normalized = " ".join(phrase.strip().split())
        for rule in self._rules:
            match = rule.pattern.fullmatch(normalized)
            if match is None:
                continue
            replacements = {key: (value or "").casefold() for key, value in match.groupdict().items()}
            try:
                interpreted = rule.term.format(**replacements)
            except KeyError as exc:
                raise ValueError(f"mapping {rule.key} references missing capture {exc}") from exc
            return MappingResult(
                interpreted_text=" ".join(interpreted.split()),
                confidence=rule.confidence,
                mapping_key=rule.key,
            )
        return None


# Concise compatibility name for callers that imported the planned Phase 5 type.
ExaminationMapper = PhraseToClinicalTermMapper
