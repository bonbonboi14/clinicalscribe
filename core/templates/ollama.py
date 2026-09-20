from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from core.models import ClerkingSheet, ClinicalNote
from core.templates.base import TemplateNoteEngine
from core.templates.schema import NoteTemplate


class OllamaEngine(TemplateNoteEngine):
    """Optional local Ollama renderer constrained to clerking-sheet values."""

    def __init__(self, template: NoteTemplate, *, model: str, base_url: str = "http://127.0.0.1:11434", timeout_seconds: float = 120) -> None:
        super().__init__(template)
        if not model:
            raise ValueError("an Ollama model must be explicitly configured")
        self.model = model
        self.base_url = base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Ollama must use a local loopback HTTP endpoint")
        self.timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return f"ollama:{self.model}"

    def generate(self, clerking_sheet: ClerkingSheet) -> ClinicalNote:
        source = super().generate(clerking_sheet)
        prompt = (
            "Render the supplied validated clerking-sheet note in clear English. "
            "Do not add, infer, diagnose, omit uncertainty, or alter negation. "
            "Return JSON with markdown and plain_text keys only. Every clinical claim must be copied verbatim "
            "from the supplied deterministic draft.\n\nDETERMINISTIC MARKDOWN:\n" + source.content
            + "\nDETERMINISTIC TEXT:\n" + source.plain_text
        )
        request = Request(
            f"{self.base_url}/api/generate",
            data=json.dumps({"model": self.model, "prompt": prompt, "stream": False, "format": "json"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                envelope = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"local Ollama request failed: {exc}") from exc
        result = json.loads(envelope["response"])
        markdown, plain_text = result.get("markdown"), result.get("plain_text")
        if not isinstance(markdown, str) or not isinstance(plain_text, str):
            raise ValueError("Ollama response must contain markdown and plain_text strings")
        self._require_supported(markdown, source.content, clerking_sheet)
        self._require_supported(plain_text, source.plain_text, clerking_sheet)
        return source.model_copy(update={"content": markdown.strip() + "\n", "plain_text": plain_text.strip() + "\n", "engine": self.name})

    @staticmethod
    def _require_supported(candidate: str, deterministic: str, sheet: ClerkingSheet) -> None:
        allowed = {line.strip("# -*:") for line in deterministic.splitlines() if line.strip("# -*:")}
        allowed.update({"Medical Clinical Note", "Clinical Note"})
        for line in candidate.splitlines():
            claim = line.strip("# -*:")
            if claim and claim not in allowed and not any(claim.endswith(value) for value in allowed):
                raise ValueError(f"hallucination firewall rejected unsupported note text: {claim[:120]}")
