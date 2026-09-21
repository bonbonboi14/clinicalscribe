from __future__ import annotations

import base64
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping
from urllib.parse import urlsplit
from uuid import UUID

from git import Repo
from git.exc import GitCommandError, GitError


TOKEN_ENVIRONMENT_VARIABLE = "CLINICAL_SCRIBE_GITHUB_TOKEN"
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_BASE_FILES = {
    "metadata.json",
    "clerking_sheet.md",
    "clinical_note.md",
    "treatment_plan.md",
    "differential_diagnosis.md",
    "audit_log.json",
    "transcript/raw_transcript.txt",
    "transcript/speaker_labelled_transcript.txt",
}
_AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".webm", ".wma"}


class GitHubPushError(RuntimeError):
    """Safe user-facing GitHub push failure with no credential material."""


class GitHubTokenMissingError(GitHubPushError):
    """Raised when the mandatory environment-only token is unavailable."""


@dataclass(frozen=True)
class PushResult:
    commit_hexsha: str
    branch: str
    session_path: str
    files: tuple[str, ...]


class GitHubPusher:
    """Clone, stage one approved session package, commit, and push manually.

    Authentication is injected through an ephemeral Git configuration environment.
    The token is never written to a remote URL, repository config, source file, or log.
    """

    def __init__(
        self,
        repo_url: str,
        *,
        branch: str = "main",
        remote_name: str = "origin",
        token_env: str = TOKEN_ENVIRONMENT_VARIABLE,
    ) -> None:
        if token_env != TOKEN_ENVIRONMENT_VARIABLE:
            raise ValueError(f"GitHub token must use {TOKEN_ENVIRONMENT_VARIABLE}")
        if not repo_url.strip():
            raise ValueError("GitHub repository URL is not configured")
        parsed = urlsplit(repo_url)
        if parsed.scheme in {"http", "https"} and (parsed.username or parsed.password):
            raise ValueError("GitHub repository URL must not contain credentials")
        if not _BRANCH_PATTERN.fullmatch(branch) or ".." in branch or branch.endswith("/"):
            raise ValueError("invalid Git branch name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", remote_name):
            raise ValueError("invalid Git remote name")
        self.repo_url = repo_url
        self.branch = branch
        self.remote_name = remote_name
        self.token_env = token_env

    def push_session(
        self,
        session_id: UUID | str,
        *,
        specialty: str,
        session_date: str,
        files: Mapping[str, bytes | str],
    ) -> PushResult:
        session_uuid = UUID(str(session_id))
        token = os.getenv(self.token_env)
        if not token:
            raise GitHubTokenMissingError(
                f"GitHub push requires the {TOKEN_ENVIRONMENT_VARIABLE} environment variable"
            )
        validated = self._validate_files(files)
        commit_specialty = " ".join(specialty.split()) or "Clinical"
        commit_specialty = commit_specialty.replace("\r", " ").replace("\n", " ")[:120]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", session_date):
            raise ValueError("session_date must use YYYY-MM-DD")
        commit_message = (
            f"[ClinicalScribe] Session {session_uuid} - {commit_specialty} - {session_date}"
        )
        auth_env = self._authentication_environment(token)
        try:
            with tempfile.TemporaryDirectory(
                prefix="clinical-scribe-github-", ignore_cleanup_errors=True
            ) as temporary:
                checkout = Path(temporary) / "repository"
                repo = Repo.clone_from(
                    self.repo_url,
                    checkout,
                    branch=self.branch,
                    single_branch=True,
                    env=auth_env,
                )
                if self.remote_name != "origin":
                    repo.remote("origin").rename(self.remote_name)
                session_relative = PurePosixPath("sessions") / str(session_uuid)
                session_directory = checkout.joinpath(*session_relative.parts)
                session_directory.mkdir(parents=True, exist_ok=True)
                written: list[str] = []
                for relative, content in validated.items():
                    destination = session_directory.joinpath(*PurePosixPath(relative).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    data = content.encode("utf-8") if isinstance(content, str) else content
                    destination.write_bytes(data)
                    written.append((session_relative / relative).as_posix())
                repo.index.add(written)
                if not repo.is_dirty(index=True, working_tree=True, untracked_files=True):
                    raise GitHubPushError("GitHub push produced no changes to commit")
                with repo.config_writer() as writer:
                    writer.set_value("user", "name", "Clinical Scribe")
                    writer.set_value("user", "email", "clinical-scribe@localhost")
                commit = repo.index.commit(commit_message)
                with repo.git.custom_environment(**auth_env):
                    results = repo.remote(self.remote_name).push(
                        refspec=f"HEAD:refs/heads/{self.branch}"
                    )
                if not results or any(item.flags & item.ERROR for item in results):
                    raise GitHubPushError(
                        "GitHub push failed. Check repository access, network connectivity, and branch permissions."
                    )
                result = PushResult(
                    commit_hexsha=commit.hexsha,
                    branch=self.branch,
                    session_path=session_relative.as_posix(),
                    files=tuple(sorted(written)),
                )
                repo.close()
                return result
        except GitHubPushError:
            raise
        except (GitCommandError, GitError, OSError):
            raise GitHubPushError(
                "GitHub push failed. Check repository access, network connectivity, and branch permissions."
            ) from None

    @staticmethod
    def _authentication_environment(token: str) -> dict[str, str]:
        credential = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}",
            "GIT_TERMINAL_PROMPT": "0",
        }

    @staticmethod
    def _validate_files(files: Mapping[str, bytes | str]) -> dict[str, bytes | str]:
        validated: dict[str, bytes | str] = {}
        for raw_path, content in files.items():
            path = PurePosixPath(raw_path)
            normalized = path.as_posix()
            if path.is_absolute() or ".." in path.parts or normalized not in _BASE_FILES:
                raise ValueError(f"unsupported session push path: {raw_path}")
            if path.suffix.lower() in _AUDIO_SUFFIXES or "audio" in {part.lower() for part in path.parts}:
                raise ValueError("audio files are never permitted in a GitHub push")
            if not isinstance(content, (bytes, str)):
                raise TypeError(f"session push content must be bytes or str: {raw_path}")
            validated[normalized] = content
        required = {"metadata.json", "clerking_sheet.md", "clinical_note.md", "treatment_plan.md", "audit_log.json"}
        missing = required - validated.keys()
        if missing:
            raise ValueError(f"session push package is missing required files: {sorted(missing)}")
        return validated
