from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Iterable
from uuid import UUID, uuid4


_SAFE_SUFFIX = re.compile(r"^\.[a-zA-Z0-9]{1,10}$")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def recording_suffix(filename: str | None, content_type: str | None) -> str:
    if filename:
        suffix = Path(filename).suffix.lower()
        if _SAFE_SUFFIX.fullmatch(suffix):
            return suffix
    return {
        "audio/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
    }.get((content_type or "").split(";", 1)[0].lower(), ".audio")


class AudioFileStore:
    """Append-only storage for original upload chunks and assembled recordings."""

    def __init__(self, audio_directory: str | Path) -> None:
        self.root = Path(audio_directory).resolve()

    def session_directory(self, session_id: UUID | str) -> Path:
        session_uuid = UUID(str(session_id))
        path = self.root / str(session_uuid)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def chunk_path(self, session_id: UUID | str, sequence_number: int) -> Path:
        return self.session_directory(session_id) / "chunks" / f"{sequence_number:010d}.chunk"

    def store_chunk(
        self,
        session_id: UUID | str,
        sequence_number: int,
        data: bytes,
        expected_sha256: str,
    ) -> Path:
        actual_sha256 = sha256_bytes(data)
        if actual_sha256 != expected_sha256.lower():
            raise ValueError("chunk checksum does not match X-Chunk-SHA256")

        destination = self.chunk_path(session_id, sequence_number)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if sha256_file(destination) == actual_sha256:
                return destination
            raise FileExistsError("chunk position already contains different immutable audio")

        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Hard-link creation is atomic and refuses to replace an existing chunk.
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if sha256_file(destination) != actual_sha256:
                    raise FileExistsError(
                        "chunk position already contains different immutable audio"
                    )
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    def assemble(
        self,
        session_id: UUID | str,
        chunk_paths: Iterable[str | Path],
        *,
        filename: str | None,
        content_type: str | None,
    ) -> tuple[Path, str, int]:
        directory = self.session_directory(session_id)
        destination = directory / f"original{recording_suffix(filename, content_type)}"
        if destination.exists():
            return destination, sha256_file(destination), destination.stat().st_size

        temporary = directory / f".original.{uuid4().hex}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as output:
                for chunk_path_value in chunk_paths:
                    chunk_path = Path(chunk_path_value)
                    with chunk_path.open("rb") as chunk:
                        for block in iter(lambda: chunk.read(1024 * 1024), b""):
                            output.write(block)
                            digest.update(block)
                            size += len(block)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                return destination, sha256_file(destination), destination.stat().st_size
            return destination, digest.hexdigest(), size
        finally:
            temporary.unlink(missing_ok=True)
