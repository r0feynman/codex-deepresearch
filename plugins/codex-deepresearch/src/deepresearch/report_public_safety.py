"""Public-safe string and path helpers for report artifacts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse


REDACTED_FILE_URL = "<redacted-file-url>"
REDACTED_LOCAL_PATH = "<redacted-local-path>"
OUTSIDE_RUN_DIR = "<outside-run-dir>"

FILE_URL_PATTERN = re.compile(r"\bfile:/+[^\s<>'\")]+", flags=re.IGNORECASE)
PRIVATE_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9:/._-])/(?:home|Users)/[^\s<>'\"),;]*")


def public_artifact_ref(value: Any, *, run_dir: Path) -> str:
    """Return a public-safe artifact reference, preserving in-run paths."""

    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    lowered = text.lower()
    if _is_http_url_only(text):
        return text
    if lowered.startswith("file:/"):
        return _public_file_url_ref(text, run_dir=run_dir)

    path = Path(text)
    if path.is_absolute():
        return _public_absolute_path_ref(path, run_dir=run_dir, redact_nonprivate=False)
    if ".." in path.parts:
        return OUTSIDE_RUN_DIR
    return path.as_posix()


def public_url_ref(value: Any, *, run_dir: Path | None = None) -> str:
    """Return a public-safe URL-like reference for report output."""

    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    lowered = text.lower()
    if _is_http_url_only(text):
        return text
    if lowered.startswith("file:/"):
        return _public_file_url_ref(text, run_dir=run_dir)
    path = Path(text)
    if path.is_absolute():
        return _public_absolute_path_ref(path, run_dir=run_dir, redact_nonprivate=True)
    if _looks_like_windows_absolute_path(text):
        return REDACTED_LOCAL_PATH
    return sanitize_public_string(text, run_dir=run_dir)


def sanitize_public_value(value: Any, *, run_dir: Path | None = None) -> Any:
    if isinstance(value, Mapping):
        return {key: sanitize_public_value(item, run_dir=run_dir) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_public_value(item, run_dir=run_dir) for item in value]
    if isinstance(value, tuple):
        return [sanitize_public_value(item, run_dir=run_dir) for item in value]
    if isinstance(value, str):
        return sanitize_public_string(value, run_dir=run_dir)
    return value


def sanitize_public_string(value: str, *, run_dir: Path | None = None) -> str:
    stripped = value.strip()
    lowered = stripped.lower()
    if _is_http_url_only(stripped):
        return value
    if lowered.startswith("file:/") and FILE_URL_PATTERN.fullmatch(stripped):
        return _public_file_url_ref(stripped, run_dir=run_dir)
    if Path(stripped).is_absolute():
        return _public_absolute_path_ref(Path(stripped), run_dir=run_dir, redact_nonprivate=True)
    if _looks_like_windows_absolute_path(stripped):
        return REDACTED_LOCAL_PATH
    if _looks_like_private_absolute_path(stripped):
        return _public_absolute_path_ref(Path(stripped), run_dir=run_dir, redact_nonprivate=False)

    redacted = FILE_URL_PATTERN.sub(
        lambda match: _public_file_url_ref(match.group(0), run_dir=run_dir),
        value,
    )
    redacted = PRIVATE_ABSOLUTE_PATH_PATTERN.sub(
        lambda match: _public_absolute_path_ref(
            Path(match.group(0)),
            run_dir=run_dir,
            redact_nonprivate=False,
        ),
        redacted,
    )
    return redacted


def _public_file_url_ref(value: str, *, run_dir: Path | None) -> str:
    path = _file_url_path(value)
    if path is not None and run_dir is not None:
        relative = _relative_to_run(path, run_dir)
        if relative is not None:
            return relative
    return REDACTED_FILE_URL


def _public_absolute_path_ref(path: Path, *, run_dir: Path | None, redact_nonprivate: bool) -> str:
    if run_dir is not None:
        relative = _relative_to_run(path, run_dir)
        if relative is not None:
            return relative
    if _looks_like_private_absolute_path(str(path)) or redact_nonprivate:
        return REDACTED_LOCAL_PATH
    return OUTSIDE_RUN_DIR


def _relative_to_run(path: Path, run_dir: Path) -> str | None:
    try:
        relative = path.resolve(strict=False).relative_to(run_dir.resolve(strict=False))
    except (OSError, ValueError):
        return None
    return relative.as_posix()


def _file_url_path(value: str) -> Path | None:
    parsed = urlparse(value)
    if parsed.scheme.lower() != "file":
        return None
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        return None
    if not parsed.path:
        return None
    return Path(unquote(parsed.path))


def _is_http_url_only(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith(("http://", "https://")) and not any(char.isspace() for char in value)


def _looks_like_windows_absolute_path(value: str) -> bool:
    return len(value) >= 3 and value[1:3] in {":\\", ":/"} and value[0].isalpha()


def _looks_like_private_absolute_path(value: str) -> bool:
    lowered = value.lower()
    return (
        value.startswith(("/home/", "/Users/"))
        or lowered.startswith("\\users\\")
        or (len(value) >= 9 and value[1:3] in {":\\", ":/"} and lowered[3:].startswith("users"))
    )


__all__ = [
    "OUTSIDE_RUN_DIR",
    "REDACTED_FILE_URL",
    "REDACTED_LOCAL_PATH",
    "public_artifact_ref",
    "public_url_ref",
    "sanitize_public_string",
    "sanitize_public_value",
]
