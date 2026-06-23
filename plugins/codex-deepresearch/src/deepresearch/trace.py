"""Public-safe JSONL run tracing for DeepResearch stages."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .run_state import add_run_steps_artifact, record_trace_state


TRACE_SCHEMA_VERSION = "codex-deepresearch.run-trace.v0"
TRACE_FILENAME = "run_trace.jsonl"
TRACE_PREVIEW_LIMIT = 700
TRACE_SUMMARY_LIMIT = 500

TRACE_STAGES = {
    "planning",
    "ingest",
    "fetch_claims",
    "ingest_vision",
    "ingest_manual",
    "enforce_guardrails",
    "verify_claims",
    "synthesize",
}
TRACE_FAILURE_CATEGORIES = {
    "budget_pruned",
    "fetch_failed",
    "guardrail_failed",
    "ingest_rejected",
    "invalid_json",
    "missing_artifact",
    "missing_fetch_queue",
    "missing_search_handoff",
    "policy_blocked",
    "search_handoff_failed",
    "stage_failed",
    "synthesis_failed",
    "validation_failed",
    "verification_disagreement",
    "verification_failed",
    "vision_failed",
}

_PREVIEW_KEYS = (
    "sources_ingested",
    "fetch_queue_count",
    "sources_fetched",
    "sources_partial",
    "sources_failed",
    "quote_candidates_created",
    "claims_created",
    "images_ingested",
    "missing_visual_claims_created",
    "sources_processed",
    "images_processed",
    "claims_processed",
    "claims_budget_pruned",
    "votes_written",
    "claims_seen",
    "claims_included",
    "claims_excluded",
)


class TraceError(ValueError):
    """Raised when a run trace record is invalid."""


@dataclass(frozen=True)
class TraceValidationError:
    """One schema validation problem in a trace artifact."""

    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class TraceValidationResult:
    """Validation result for one trace record or file."""

    valid: bool
    errors: tuple[TraceValidationError, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [error.to_dict() for error in self.errors],
        }


def trace_path(run_dir: str | Path) -> Path:
    """Return the canonical trace artifact path for a run directory."""

    return Path(run_dir) / TRACE_FILENAME


def add_trace_artifact(payload: dict[str, Any], run_dir: str | Path) -> None:
    """Add the canonical trace artifact link to a status/result payload."""

    artifacts = payload.setdefault("artifacts", {})
    if isinstance(artifacts, dict):
        artifacts["run_trace"] = str(trace_path(run_dir))


def record_stage_trace(
    run_dir: str | Path,
    *,
    stage: str,
    agent_role: str,
    status_payload: dict[str, Any],
    prompt_summary: str,
    tool_call_summary: str,
    event_type: str = "stage",
    timestamp: str | None = None,
    output_preview: str | None = None,
    failure_category: str | None = None,
) -> dict[str, Any]:
    """Append one public-safe stage trace record and link it from the status payload."""

    run_dir = Path(run_dir)
    add_trace_artifact(status_payload, run_dir)
    add_run_steps_artifact(status_payload, run_dir)
    record = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "run_id": _run_id(run_dir, status_payload),
        "event_id": _event_id(stage),
        "event_type": event_type,
        "timestamp": timestamp or _utc_now(),
        "stage": stage,
        "agent_role": _truncate(agent_role, TRACE_SUMMARY_LIMIT),
        "status": _string(status_payload.get("status"), "unknown"),
        "prompt_summary": _truncate(prompt_summary, TRACE_SUMMARY_LIMIT),
        "tool_call_summary": _truncate(tool_call_summary, TRACE_SUMMARY_LIMIT),
        "output_preview": _truncate(
            output_preview or _status_preview(status_payload),
            TRACE_PREVIEW_LIMIT,
        ),
        "artifacts": _string_artifacts(status_payload.get("artifacts")),
        "failure_category": failure_category
        if failure_category is not None
        else classify_failure(status_payload),
    }
    validation = validate_trace_record(record)
    if not validation.valid:
        raise TraceError(json.dumps(validation.to_dict(), sort_keys=True))
    append_trace_record(run_dir, record)
    record_trace_state(
        run_dir,
        stage=stage,
        status_payload=status_payload,
        trace_event_id=record["event_id"],
        timestamp=record["timestamp"],
    )
    return record


def record_exception_trace(
    run_dir: str | Path,
    *,
    stage: str,
    agent_role: str,
    exc: BaseException,
    prompt_summary: str,
    tool_call_summary: str,
) -> dict[str, Any]:
    """Append a failed trace event for a CLI-level exception on an existing run."""

    status_payload: dict[str, Any] = {
        "run_id": _run_id(Path(run_dir), {}),
        "status": "failed",
        "errors": [
            {
                "code": exc.__class__.__name__,
                "status": "failed",
            }
        ],
    }
    return record_stage_trace(
        run_dir,
        stage=stage,
        agent_role=agent_role,
        status_payload=status_payload,
        prompt_summary=prompt_summary,
        tool_call_summary=tool_call_summary,
        output_preview=f"status=failed; error={exc.__class__.__name__}",
        failure_category=classify_exception(exc),
    )


def append_trace_record(run_dir: str | Path, record: Mapping[str, Any]) -> None:
    """Append one already validated trace record to run_trace.jsonl."""

    path = trace_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), sort_keys=True) + "\n")


def read_trace_records(path: str | Path) -> list[dict[str, Any]]:
    """Read a trace JSONL artifact into records."""

    trace_file = Path(path)
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(trace_file.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TraceError(f"invalid JSONL in {trace_file} line {line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise TraceError(f"expected JSON object in {trace_file} line {line_number}")
        records.append(record)
    return records


def validate_trace_file(path: str | Path) -> TraceValidationResult:
    """Validate all records in a trace JSONL artifact."""

    errors: list[TraceValidationError] = []
    trace_file = Path(path)
    try:
        lines = trace_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return TraceValidationResult(
            valid=False,
            errors=(
                TraceValidationError(
                    path=str(trace_file),
                    code="missing_file",
                    message="trace file does not exist",
                ),
            ),
        )
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            errors.append(
                TraceValidationError(
                    path=f"{trace_file}:{line_number}",
                    code="invalid_json",
                    message="trace line must be valid JSON",
                )
            )
            continue
        result = validate_trace_record(record, path_prefix=f"{trace_file}:{line_number}")
        errors.extend(result.errors)
    if not any(line.strip() for line in lines):
        errors.append(
            TraceValidationError(
                path=str(trace_file),
                code="empty_trace",
                message="trace file must contain at least one record",
            )
        )
    return TraceValidationResult(valid=not errors, errors=tuple(errors))


def validate_trace_record(
    record: Any,
    *,
    path_prefix: str = "record",
) -> TraceValidationResult:
    """Validate one run trace record."""

    errors: list[TraceValidationError] = []
    if not isinstance(record, Mapping):
        return TraceValidationResult(
            valid=False,
            errors=(
                TraceValidationError(
                    path=path_prefix,
                    code="expected_object",
                    message="trace record must be a JSON object",
                ),
            ),
        )

    required_string_fields = (
        "schema_version",
        "run_id",
        "event_id",
        "timestamp",
        "stage",
        "agent_role",
        "status",
        "prompt_summary",
        "tool_call_summary",
        "output_preview",
    )
    for field in required_string_fields:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(
                TraceValidationError(
                    path=f"{path_prefix}.{field}",
                    code="missing_string",
                    message=f"{field} must be a non-empty string",
                )
            )
    if record.get("schema_version") != TRACE_SCHEMA_VERSION:
        errors.append(
            TraceValidationError(
                path=f"{path_prefix}.schema_version",
                code="invalid_schema_version",
                message=f"schema_version must be {TRACE_SCHEMA_VERSION}",
            )
        )
    if isinstance(record.get("stage"), str) and record["stage"] not in TRACE_STAGES:
        errors.append(
            TraceValidationError(
                path=f"{path_prefix}.stage",
                code="invalid_stage",
                message="stage is not a known DeepResearch stage",
            )
        )
    if isinstance(record.get("timestamp"), str) and not _looks_like_utc_timestamp(record["timestamp"]):
        errors.append(
            TraceValidationError(
                path=f"{path_prefix}.timestamp",
                code="invalid_timestamp",
                message="timestamp must be an ISO-8601 UTC timestamp ending in Z",
            )
        )
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, Mapping):
        errors.append(
            TraceValidationError(
                path=f"{path_prefix}.artifacts",
                code="expected_artifacts_object",
                message="artifacts must be an object",
            )
        )
    else:
        if not isinstance(artifacts.get("run_trace"), str) or not artifacts.get("run_trace"):
            errors.append(
                TraceValidationError(
                    path=f"{path_prefix}.artifacts.run_trace",
                    code="missing_run_trace_artifact",
                    message="artifacts must link to run_trace.jsonl",
                )
            )
        for key, value in artifacts.items():
            if not isinstance(key, str) or not key:
                errors.append(
                    TraceValidationError(
                        path=f"{path_prefix}.artifacts",
                        code="invalid_artifact_key",
                        message="artifact keys must be non-empty strings",
                    )
                )
            if not isinstance(value, str) or not value:
                errors.append(
                    TraceValidationError(
                        path=f"{path_prefix}.artifacts.{key}",
                        code="invalid_artifact_value",
                        message="artifact values must be non-empty strings",
                    )
                )
    failure_category = record.get("failure_category")
    if failure_category is not None and failure_category not in TRACE_FAILURE_CATEGORIES:
        errors.append(
            TraceValidationError(
                path=f"{path_prefix}.failure_category",
                code="invalid_failure_category",
                message="failure_category is not recognized",
            )
        )
    event_type = record.get("event_type")
    if event_type is not None and (not isinstance(event_type, str) or not event_type.strip()):
        errors.append(
            TraceValidationError(
                path=f"{path_prefix}.event_type",
                code="invalid_event_type",
                message="event_type must be a non-empty string when present",
            )
        )
    return TraceValidationResult(valid=not errors, errors=tuple(errors))


def classify_failure(status_payload: Mapping[str, Any]) -> str | None:
    """Return a coarse public-safe failure category for a stage status payload."""

    status = str(status_payload.get("status") or "")
    if status == "failed_validation":
        return "validation_failed"
    if status == "failed_normalization":
        return "vision_failed"
    if status == "blocked_missing_fetch_queue":
        return "missing_fetch_queue"
    if status == "blocked_missing_search_handoff":
        return "missing_search_handoff"
    if status.startswith("failed"):
        return "stage_failed"

    errors = status_payload.get("errors")
    codes = _error_codes(errors)
    if "fetch_failed" in codes:
        return "fetch_failed"
    if "policy_blocked" in codes:
        return "policy_blocked"
    if {"invalid_url", "policy_manual_review"} & codes:
        return "ingest_rejected"
    if "normalization_failed" in codes:
        return "vision_failed"
    if "missing_fetch_queue" in codes:
        return "missing_fetch_queue"
    if any(code in codes for code in ("missing_search_results_file", "empty_search_results_file")):
        return "missing_search_handoff"
    if codes and status.endswith("_with_errors"):
        return "stage_failed"

    if _numeric(status_payload.get("claims_budget_pruned")) > 0:
        return "budget_pruned"
    if _claims_blocked(status_payload):
        return "policy_blocked"
    return None


def classify_exception(exc: BaseException) -> str:
    """Return a coarse public-safe failure category for a CLI exception."""

    name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    if "json" in name or "invalid json" in message or "invalid jsonl" in message:
        return "invalid_json"
    if "missing" in message or "does not exist" in message:
        return "missing_artifact"
    if "vision" in name:
        return "vision_failed"
    if "guardrail" in name:
        return "guardrail_failed"
    if "verification" in name:
        return "verification_failed"
    if "report" in name:
        return "synthesis_failed"
    if "fetch" in name:
        return "fetch_failed"
    if "searchhandoff" in name or "search handoff" in message:
        return "search_handoff_failed"
    if "oserror" in name or "ioerror" in name:
        return "missing_artifact"
    return "stage_failed"


def _status_preview(status_payload: Mapping[str, Any]) -> str:
    pieces = [f"status={_string(status_payload.get('status'), 'unknown')}"]
    for key in _PREVIEW_KEYS:
        value = status_payload.get(key)
        if isinstance(value, (int, float, bool)):
            pieces.append(f"{key}={value}")
    validation = status_payload.get("validation")
    if isinstance(validation, Mapping) and validation.get("valid") is False:
        pieces.append("validation=failed")
    codes = _error_codes(status_payload.get("errors"))
    if codes:
        pieces.append("errors=" + ",".join(sorted(codes)[:5]))
    return "; ".join(pieces)


def _string_artifacts(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if isinstance(key, str) and key and item is not None and str(item)
    }


def _error_codes(errors: Any) -> set[str]:
    if not isinstance(errors, Sequence) or isinstance(errors, (str, bytes, bytearray)):
        return set()
    codes: set[str] = set()
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        code = error.get("code")
        if isinstance(code, str) and code:
            codes.add(code)
    return codes


def _claims_blocked(status_payload: Mapping[str, Any]) -> bool:
    claims = status_payload.get("claims")
    if isinstance(claims, Sequence) and not isinstance(claims, (str, bytes, bytearray)):
        for claim in claims:
            if isinstance(claim, Mapping) and claim.get("verification_status") == "policy_blocked":
                return True
    return False


def _numeric(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _run_id(run_dir: Path, status_payload: Mapping[str, Any]) -> str:
    run_id = status_payload.get("run_id")
    if isinstance(run_id, str) and run_id.strip():
        return run_id
    evidence_path = run_dir / "evidence.json"
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return run_dir.name
    if isinstance(evidence, Mapping) and isinstance(evidence.get("run_id"), str):
        return str(evidence["run_id"])
    return run_dir.name


def _event_id(stage: str) -> str:
    safe_stage = "".join(character if character.isalnum() else "_" for character in stage).strip("_")
    return f"trace_{safe_stage}_{uuid.uuid4().hex[:16]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _looks_like_utc_timestamp(value: str) -> bool:
    if not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


def _string(value: Any, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    return default


def _truncate(value: str, limit: int) -> str:
    compact = " ".join(str(value).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."
