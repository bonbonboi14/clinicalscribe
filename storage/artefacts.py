from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


@dataclass(frozen=True)
class StoredArtefact:
    path: Path
    checksum_sha256: str
    size_bytes: int


class TranscriptArtefactStore:
    """Content-addressed, append-only JSON files for derived transcript views."""

    def __init__(self, artifact_directory: str | Path) -> None:
        self.root = Path(artifact_directory).resolve() / "transcripts"

    def store(self, session_id: UUID | str, kind: str, version: int, payload: dict[str, Any]) -> StoredArtefact:
        session_uuid = UUID(str(session_id))
        if version < 1:
            raise ValueError("artefact version must be positive")
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(data).hexdigest()
        directory = self.root / str(session_uuid)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{kind.lower()}-v{version:04d}-{checksum[:16]}.json"
        if destination.exists():
            if destination.read_bytes() != data:
                raise FileExistsError("immutable artefact path contains different data")
            return StoredArtefact(destination, checksum, len(data))
        temporary = directory / f".{destination.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if destination.read_bytes() != data:
                    raise FileExistsError("immutable artefact path contains different data")
            return StoredArtefact(destination, checksum, len(data))
        finally:
            temporary.unlink(missing_ok=True)
