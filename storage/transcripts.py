from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from uuid import UUID, uuid4

from core.models import TranscriptSegment


@dataclass(frozen=True)
class StoredTranscript:
    path: Path
    checksum_sha256: str
    size_bytes: int


class TranscriptFileStore:
    """Append-only canonical JSON storage for raw engine transcript output."""

    def __init__(self, transcript_directory: str | Path) -> None:
        self.root = Path(transcript_directory).resolve()

    def store(
        self,
        session_id: UUID | str,
        version: int,
        segments: Sequence[TranscriptSegment],
        *,
        engine: str,
    ) -> StoredTranscript:
        session_uuid = UUID(str(session_id))
        if version < 1:
            raise ValueError("transcript version must be positive")
        payload = {
            "engine": engine,
            "segments": [segment.model_dump(mode="json") for segment in segments],
            "session_id": str(session_uuid),
            "version": version,
        }
        data = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        checksum = hashlib.sha256(data).hexdigest()
        directory = self.root / str(session_uuid)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"raw-v{version:04d}-{checksum[:16]}.json"
        if destination.exists():
            if destination.read_bytes() != data:
                raise FileExistsError("immutable transcript path contains different data")
            return StoredTranscript(destination, checksum, len(data))

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
                    raise FileExistsError("immutable transcript path contains different data")
            return StoredTranscript(destination, checksum, len(data))
        finally:
            temporary.unlink(missing_ok=True)
