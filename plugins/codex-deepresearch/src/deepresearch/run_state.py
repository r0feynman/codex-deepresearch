"""Run step state machine for resumable DeepResearch stages."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


RUN_STEPS_SCHEMA_VERSION = "codex-deepresearch.run-steps.v0"
RUN_CONTROL_SCHEMA_VERSION = "codex-deepresearch.run-control.v0"
RUN_STEPS_FILENAME = "run_steps.json"
RUN_CONTROL_FILENAME = "run_control.json"
RUN_STATUS_FILENAME = "run_status.json"
_RUN_STATUS_SCHEMA_VERSION = "codex-deepresearch.run-status.v0"
_RUN_TRACE_FILENAME = "run_trace.jsonl"

RUN_STAGE_ORDER = (
    "planning",
    "ingest",
    "fetch_claims",
    "ingest_vision",
    "enforce_guardrails",
    "verify_claims",
    "synthesize",
)
OPTIONAL_RUN_STAGES = ("ingest_manual", "parallel_orchestration")
RUN_STAGES = (
    "planning",
    "parallel_orchestration",
    "ingest",
    "ingest_manual",
    "fetch_claims",
    "ingest_vision",
    "enforce_guardrails",
    "verify_claims",
    "synthesize",
)
RUN_STEP_STATUSES = ("pending", "running", "completed", "failed", "skipped")
TERMINAL_STEP_STATUSES = {"completed", "skipped"}
RETRYABLE_STEP_STATUSES = {"running", "failed"}
RUN_STEP_TRANSITIONS = {
    "pending": ("running", "skipped"),
    "running": ("running", "completed", "failed", "skipped"),
    "failed": ("running", "skipped"),
    "completed": ("running", "completed", "failed", "skipped"),
    "skipped": ("skipped",),
}
_COMPLETED_STAGE_STATUSES = {
    "awaiting_search_results",
    "completed",
    "completed_with_errors",
    "completed_fixture",
    "completed_parallel",
    "completed_partial_parallel",
    "completed_serial_handoff",
    "ingested",
    "ingested_with_rejections",
    "manual_sources_ingested",
    "needs_visual_evidence",
    "visual_evidence_ingested",
}
_SKIPPED_STAGE_STATUSES = {
    "no_visual_tasks",
    "skipped",
}
_STAGE_STATUS_ARTIFACTS = (
    ("planning", "status.json"),
    ("parallel_orchestration", "parallel_orchestration_status.json"),
    ("ingest", "ingest_status.json"),
    ("ingest_manual", "manual_ingest_status.json"),
    ("fetch_claims", "fetch_claims_status.json"),
    ("ingest_vision", "vision_ingest_status.json"),
    ("enforce_guardrails", "guardrails_status.json"),
    ("verify_claims", "verification_matrix_status.json"),
    ("synthesize", "report_status.json"),
)
_DOWNSTREAM_RESET_DEPENDENCIES = {
    "planning": (
        "ingest",
        "fetch_claims",
        "ingest_vision",
        "enforce_guardrails",
        "verify_claims",
        "synthesize",
    ),
    "ingest": (
        "fetch_claims",
        "ingest_vision",
        "enforce_guardrails",
        "verify_claims",
        "synthesize",
    ),
    "fetch_claims": ("enforce_guardrails", "verify_claims", "synthesize"),
    "ingest_vision": ("enforce_guardrails", "verify_claims", "synthesize"),
    "enforce_guardrails": ("verify_claims", "synthesize"),
    "verify_claims": ("synthesize",),
    "synthesize": (),
}
_STAGE_DOWNSTREAM_INPUT_ARTIFACT_KEYS = {
    "planning": ("evidence", "fetch_queue", "search_handoff"),
    "ingest": ("evidence", "fetch_queue"),
    "fetch_claims": ("evidence",),
    "ingest_vision": ("evidence", "visual_observations"),
    "enforce_guardrails": ("evidence",),
    "verify_claims": ("evidence", "verifier_votes"),
    "synthesize": ("evidence", "report"),
}
_EVIDENCE_FINGERPRINT_KEYS = (
    "question",
    "routing",
    "sources",
    "images",
    "claims",
    "quote_candidates",
    "handoff",
)
_TIMESTAMP_KEYS = (
    "updated_at",
    "finished_at",
    "completed_at",
    "created_at",
    "generated_at",
    "fetched_at",
    "ingested_at",
    "verified_at",
    "enforced_at",
    "recorded_at",
    "started_at",
    "timestamp",
)
_FINGERPRINT_VOLATILE_KEYS = frozenset(
    [
        *_TIMESTAMP_KEYS,
        "recorded_at",
        "started_at",
        "finished_at",
        "cache_key",
        "source_cache_key",
        "verification_cache_key",
    ]
)


class RunStepStateError(ValueError):
    """Raised when a run step state artifact or transition is invalid."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        stage: str | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
    ) -> None:
        self.payload = {
            "schema_version": RUN_STEPS_SCHEMA_VERSION,
            "code": code,
            "message": message,
        }
        if stage is not None:
            self.payload["stage"] = stage
        if from_status is not None:
            self.payload["from_status"] = from_status
        if to_status is not None:
            self.payload["to_status"] = to_status
        super().__init__(json.dumps(self.payload, sort_keys=True))

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


@dataclass(frozen=True)
class StageStart:
    """Result of trying to start a stage."""

    stage: str
    status: str
    skipped: bool
    skip_reason: str | None = None


def run_steps_path(run_dir: str | Path) -> Path:
    """Return the canonical run step state artifact path."""

    return Path(run_dir) / RUN_STEPS_FILENAME


def run_control_path(run_dir: str | Path) -> Path:
    """Return the canonical run control artifact path."""

    return Path(run_dir) / RUN_CONTROL_FILENAME


def add_run_steps_artifact(payload: dict[str, Any], run_dir: str | Path) -> None:
    """Add the canonical run step artifact link to a status/result payload."""

    artifacts = payload.setdefault("artifacts", {})
    if isinstance(artifacts, dict):
        artifacts["run_steps"] = str(run_steps_path(run_dir))


def add_run_control_artifact(payload: dict[str, Any], run_dir: str | Path) -> None:
    """Add the canonical run control artifact link to a status/result payload."""

    artifacts = payload.setdefault("artifacts", {})
    if isinstance(artifacts, dict):
        artifacts["run_control"] = str(run_control_path(run_dir))


def initialize_run_steps(
    run_dir: str | Path,
    *,
    run_id: str | None = None,
    created_at: str | None = None,
    reconstruct_missing: bool = True,
) -> dict[str, Any]:
    """Create or update the run step state artifact with pending stages."""

    run_dir = Path(run_dir)
    path = run_steps_path(run_dir)
    now = created_at or _utc_now()
    if path.exists():
        state = _read_state(path)
        changed = _ensure_state_shape(state, run_dir, run_id=run_id, timestamp=now)
    else:
        state = _new_run_steps_state(run_dir, run_id=run_id, timestamp=now)
        if reconstruct_missing:
            _reconstruct_state_from_artifacts(state, run_dir, timestamp=now)
        changed = True
    changed = _overlay_durable_run_artifacts(state, run_dir, timestamp=now) or changed
    state = _with_resume_summary(state)
    if changed:
        _write_state(path, state)
    return state


def begin_stage(
    run_dir: str | Path,
    stage: str,
    *,
    run_id: str | None = None,
    started_at: str | None = None,
    completed_behavior: str = "rerun",
) -> StageStart:
    """Move a pending, failed, or interrupted stage into running state."""

    _validate_stage(stage)
    if completed_behavior not in {"rerun", "skip"}:
        raise RunStepStateError(
            code="invalid_completed_behavior",
            message="completed_behavior must be rerun or skip",
            stage=stage,
        )
    state = initialize_run_steps(run_dir, run_id=run_id, created_at=started_at)
    _raise_if_control_blocks_stage(state, stage)
    record = _stage_record(state, stage)
    current = _status(record)
    if current in TERMINAL_STEP_STATUSES:
        if current == "completed" and completed_behavior == "rerun":
            transition_stage(
                run_dir,
                stage,
                "running",
                reason="rerun_completed_stage",
                timestamp=started_at,
                run_id=run_id,
            )
            return StageStart(stage=stage, status="running", skipped=False)
        reason = "stage_already_completed" if current == "completed" else "stage_already_skipped"
        return StageStart(stage=stage, status=current, skipped=True, skip_reason=reason)

    reason = "stage_started"
    if current == "failed":
        reason = "retry_failed_stage"
    elif current == "running":
        reason = "retry_interrupted_stage"
    transition_stage(
        run_dir,
        stage,
        "running",
        reason=reason,
        timestamp=started_at,
        run_id=run_id,
    )
    return StageStart(stage=stage, status="running", skipped=False)


def transition_stage(
    run_dir: str | Path,
    stage: str,
    to_status: str,
    *,
    reason: str,
    timestamp: str | None = None,
    run_id: str | None = None,
    trace_event_id: str | None = None,
    status_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply one validated state transition and persist run_steps.json."""

    _validate_stage(stage)
    _validate_status(to_status)
    run_dir = Path(run_dir)
    path = run_steps_path(run_dir)
    state = initialize_run_steps(run_dir, run_id=run_id, created_at=timestamp)
    _apply_stage_transition(
        state,
        stage,
        to_status,
        reason=reason,
        timestamp=timestamp,
        trace_event_id=trace_event_id,
        status_payload=status_payload,
    )
    _write_state(path, _with_resume_summary(state))
    return _with_resume_summary(state)


def record_trace_state(
    run_dir: str | Path,
    *,
    stage: str,
    status_payload: Mapping[str, Any],
    trace_event_id: str,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Update run_steps.json from a stage trace/status payload."""

    state = initialize_run_steps(
        run_dir,
        run_id=_payload_run_id(status_payload),
        created_at=timestamp,
        reconstruct_missing=False,
    )
    _apply_status_to_state(
        state,
        stage=stage,
        status_payload=status_payload,
        trace_event_id=trace_event_id,
        timestamp=timestamp,
    )
    _write_state(run_steps_path(run_dir), _with_resume_summary(state))
    return _with_resume_summary(state)


def stage_status_to_step_status(stage: str, status_payload: Mapping[str, Any]) -> str:
    """Map existing stage status strings to the step state enum."""

    _validate_stage(stage)
    raw_status = status_payload.get("status")
    status = raw_status if isinstance(raw_status, str) else ""
    validation = status_payload.get("validation")
    if isinstance(validation, Mapping) and validation.get("valid") is False:
        return "failed"
    if status in _SKIPPED_STAGE_STATUSES:
        return "skipped"
    if status in _COMPLETED_STAGE_STATUSES:
        return "completed"
    if status.startswith("blocked_") or status.startswith("failed"):
        return "failed"
    if status == "running":
        return "running"
    if status == "pending":
        return "pending"
    return "completed"


def skipped_stage_status(
    run_dir: str | Path,
    *,
    stage: str,
    schema_version: str,
    status_artifact_key: str,
    status_filename: str,
    reason: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build a normal stage status payload for an explicit skip."""

    _validate_stage(stage)
    run_dir = Path(run_dir)
    resolved_run_id = _resolve_run_id(run_dir, run_id)
    status = {
        "schema_version": schema_version,
        "run_id": resolved_run_id,
        "run_dir": str(run_dir),
        "status": "skipped",
        "skip_reason": reason,
        "created_at": _utc_now(),
        "artifacts": {
            status_artifact_key: str(run_dir / status_filename),
        },
    }
    evidence_path = run_dir / "evidence.json"
    if evidence_path.exists():
        status["artifacts"]["evidence"] = str(evidence_path)
    add_run_steps_artifact(status, run_dir)
    return status


def skip_stage(
    run_dir: str | Path,
    stage: str,
    *,
    reason: str,
    timestamp: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Mark a pending, failed, running, completed, or skipped stage as skipped."""

    state = initialize_run_steps(run_dir, run_id=run_id, created_at=timestamp)
    current = _status(_stage_record(state, stage))
    if current == "pending":
        return transition_stage(
            run_dir,
            stage,
            "skipped",
            reason=reason,
            timestamp=timestamp,
            run_id=run_id,
        )
    if current in {"failed", "running", "completed", "skipped"}:
        return transition_stage(
            run_dir,
            stage,
            "skipped",
            reason=reason,
            timestamp=timestamp,
            run_id=run_id,
        )
    raise RunStepStateError(
        code="invalid_step_status",
        message=f"stage '{stage}' has invalid status {current}",
        stage=stage,
        from_status=current,
        to_status="skipped",
    )


def inspect_run_state(run_dir: str | Path, *, run_id: str | None = None) -> dict[str, Any]:
    """Return a machine-readable resume summary for a run directory."""

    state = initialize_run_steps(run_dir, run_id=run_id)
    payload = {
        "schema_version": RUN_STEPS_SCHEMA_VERSION,
        "run_id": state["run_id"],
        "run_dir": state["run_dir"],
        "status": state["status"],
        "next_safe_stage": state.get("next_safe_stage"),
        "next_stage_retryable": state.get("next_stage_retryable", False),
        "stages": [
            state["stages"][stage]
            for stage in RUN_STAGES
            if stage in state["stages"]
        ],
        "artifacts": {
            "run_steps": str(run_steps_path(run_dir)),
        },
    }
    control = state.get("control")
    if isinstance(control, Mapping):
        payload["control"] = dict(control)
        payload["artifacts"]["run_control"] = str(run_control_path(run_dir))
    terminal_run_status = state.get("terminal_run_status")
    if isinstance(terminal_run_status, Mapping):
        payload["terminal_run_status"] = dict(terminal_run_status)
        payload["artifacts"]["run_status"] = str(Path(run_dir) / RUN_STATUS_FILENAME)
    return payload


def pause_run(
    run_dir: str | Path,
    *,
    reason: str | None = None,
    requested_by: str = "cli",
    timestamp: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Persist a durable paused control state for an inspectable run."""

    run_dir = Path(run_dir)
    _raise_if_persisted_terminal_run_status_blocks_control(run_dir, action="pause")
    now = timestamp or _utc_now()
    state = initialize_run_steps(run_dir, run_id=run_id, created_at=now)
    control_status = _control_status(state)
    if control_status == "cancelled":
        raise RunStepStateError(
            code="run_cancelled",
            message="cancelled runs cannot be paused",
        )
    if state.get("status") == "completed":
        raise RunStepStateError(
            code="run_completed",
            message="completed runs cannot be paused",
        )
    if control_status == "paused":
        control = _mapping_value(state.get("control"))
        control_artifact = _write_control_artifact(run_dir, state, control)
        return _control_result(state, control_artifact)

    control = _build_control_payload(
        run_dir,
        state,
        status="paused",
        action="pause",
        requested_at=now,
        requested_by=requested_by,
        reason=reason or "user_requested_pause",
        terminal=False,
        diagnostics={
            "actionable_cause": (
                "run pause requested; existing artifacts were left intact and "
                "new stage starts are blocked until resume-run is used"
            )
        },
    )
    _apply_control_payload(state, control)
    _mark_running_stages_interrupted(state, control)
    state = _with_resume_summary(state)
    _write_state(run_steps_path(run_dir), state)
    control_artifact = _write_control_artifact(run_dir, state, control)
    _write_control_run_status(run_dir, control, state)
    return _control_result(state, control_artifact)


def resume_run(
    run_dir: str | Path,
    *,
    reason: str | None = None,
    requested_by: str = "cli",
    timestamp: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Clear a paused control gate and report the next safe stage."""

    run_dir = Path(run_dir)
    _raise_if_persisted_terminal_run_status_blocks_control(run_dir, action="resume")
    now = timestamp or _utc_now()
    state = initialize_run_steps(run_dir, run_id=run_id, created_at=now)
    control_status = _control_status(state)
    if control_status == "cancelled":
        raise RunStepStateError(
            code="run_cancelled",
            message="cancelled runs cannot be resumed",
        )
    _raise_if_resume_blocked_by_terminal_run(run_dir, state, control_status)
    if control_status == "paused":
        reason_value = reason or "user_requested_resume"
        actionable_cause = (
            "run resume requested; continue from next_safe_stage without "
            "rerunning completed stages"
        )
    else:
        reason_value = reason or "resume_requested_for_unpaused_run"
        actionable_cause = "run was not paused; no control gate changed"

    control = _build_control_payload(
        run_dir,
        state,
        status="active",
        action="resume",
        requested_at=now,
        requested_by=requested_by,
        reason=reason_value,
        terminal=False,
        diagnostics={"actionable_cause": actionable_cause},
    )
    _apply_control_payload(state, control)
    state = _with_resume_summary(state)
    _write_state(run_steps_path(run_dir), state)
    control_artifact = _write_control_artifact(run_dir, state, control)
    _write_control_run_status(run_dir, control, state, status_override="resumed")
    return _control_result(state, control_artifact)


def cancel_run(
    run_dir: str | Path,
    *,
    reason: str | None = None,
    requested_by: str = "cli",
    cleanup_children: bool = True,
    timestamp: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Persist terminal cancellation diagnostics without deleting artifacts."""

    run_dir = Path(run_dir)
    _raise_if_persisted_terminal_run_status_blocks_control(run_dir, action="cancel")
    now = timestamp or _utc_now()
    state = initialize_run_steps(run_dir, run_id=run_id, created_at=now)
    if _control_status(state) == "cancelled":
        control = _mapping_value(state.get("control"))
        control_artifact = _write_control_artifact(run_dir, state, control)
        return _control_result(state, control_artifact)
    if state.get("status") == "completed":
        raise RunStepStateError(
            code="run_completed",
            message="completed runs cannot be cancelled",
        )

    child_contexts = _known_child_contexts(run_dir)
    close_records = _child_context_close_records(
        child_contexts,
        requested=cleanup_children,
        timestamp=now,
    )
    diagnostics = {
        "actionable_cause": _cancellation_actionable_cause(
            child_contexts,
            cleanup_children,
        ),
        "child_contexts_known": len(child_contexts),
        "child_context_close_requested": cleanup_children,
        "child_context_close_records": close_records,
    }
    control = _build_control_payload(
        run_dir,
        state,
        status="cancelled",
        action="cancel",
        requested_at=now,
        requested_by=requested_by,
        reason=reason or "user_requested_cancel",
        terminal=True,
        diagnostics=diagnostics,
    )
    control["known_child_contexts"] = child_contexts
    _apply_control_payload(state, control)
    _mark_running_stages_interrupted(state, control)
    state = _with_resume_summary(state)
    _write_state(run_steps_path(run_dir), state)
    control_artifact = _write_control_artifact(run_dir, state, control)
    _write_control_run_status(run_dir, control, state)
    return _control_result(state, control_artifact)


def _ensure_state_shape(
    state: dict[str, Any],
    run_dir: Path,
    *,
    run_id: str | None,
    timestamp: str,
) -> bool:
    changed = False
    if state.get("schema_version") != RUN_STEPS_SCHEMA_VERSION:
        raise RunStepStateError(
            code="invalid_schema_version",
            message=f"run_steps.json schema_version must be {RUN_STEPS_SCHEMA_VERSION}",
        )
    resolved_run_id = _resolve_run_id(run_dir, run_id or _string_value(state.get("run_id")))
    if state.get("run_id") != resolved_run_id:
        state["run_id"] = resolved_run_id
        changed = True
    run_dir_string = str(run_dir.resolve())
    if state.get("run_dir") != run_dir_string:
        state["run_dir"] = run_dir_string
        changed = True
    if state.get("transition_rules") != {
        status: list(targets)
        for status, targets in RUN_STEP_TRANSITIONS.items()
    }:
        state["transition_rules"] = {
            status: list(targets)
            for status, targets in RUN_STEP_TRANSITIONS.items()
        }
        changed = True
    if state.get("stage_order") != list(RUN_STAGE_ORDER):
        state["stage_order"] = list(RUN_STAGE_ORDER)
        changed = True
    if state.get("optional_stages") != list(OPTIONAL_RUN_STAGES):
        state["optional_stages"] = list(OPTIONAL_RUN_STAGES)
        changed = True

    stages = state.setdefault("stages", {})
    if not isinstance(stages, dict):
        raise RunStepStateError(
            code="invalid_stages",
            message="run_steps.json stages must be an object",
        )
    for index, stage in enumerate(RUN_STAGES, start=1):
        if stage not in stages:
            stages[stage] = _new_stage_record(stage, order=index, timestamp=timestamp)
            changed = True
            continue
        record = stages[stage]
        if not isinstance(record, dict):
            raise RunStepStateError(
                code="invalid_stage_record",
                message=f"run_steps.json stage '{stage}' must be an object",
                stage=stage,
            )
        _validate_status(_status(record), stage=stage)
        for key, value in {
            "stage": stage,
            "order": index,
            "retryable": _status(record) in RETRYABLE_STEP_STATUSES,
            "terminal": _status(record) in TERMINAL_STEP_STATUSES,
        }.items():
            if record.get(key) != value:
                record[key] = value
                changed = True
        record.setdefault("attempt", 0)
        record.setdefault("trace_event_ids", [])
        record.setdefault("history", [])
    return changed


def _new_stage_record(stage: str, *, order: int, timestamp: str) -> dict[str, Any]:
    return {
        "stage": stage,
        "order": order,
        "status": "pending",
        "attempt": 0,
        "retryable": False,
        "terminal": False,
        "created_at": timestamp,
        "updated_at": timestamp,
        "trace_event_ids": [],
        "history": [],
        "failure": None,
        "skip_reason": None,
        "artifacts": {},
    }


def _new_run_steps_state(
    run_dir: Path,
    *,
    run_id: str | None,
    timestamp: str,
) -> dict[str, Any]:
    return {
        "schema_version": RUN_STEPS_SCHEMA_VERSION,
        "run_id": _resolve_run_id(run_dir, run_id),
        "run_dir": str(run_dir.resolve()),
        "created_at": timestamp,
        "updated_at": timestamp,
        "transition_rules": {
            status: list(targets)
            for status, targets in RUN_STEP_TRANSITIONS.items()
        },
        "stage_order": list(RUN_STAGE_ORDER),
        "optional_stages": list(OPTIONAL_RUN_STAGES),
        "stages": {
            stage: _new_stage_record(stage, order=index, timestamp=timestamp)
            for index, stage in enumerate(RUN_STAGES, start=1)
        },
    }


def _reconstruct_state_from_artifacts(
    state: dict[str, Any],
    run_dir: Path,
    *,
    timestamp: str,
) -> None:
    sources: list[str] = []
    trace_applied = _replay_trace_artifact(state, run_dir, timestamp=timestamp)
    if trace_applied:
        sources.append(_RUN_TRACE_FILENAME)
    sources.extend(_replay_status_artifacts(state, run_dir, timestamp=timestamp))
    sources.extend(_infer_manual_source_ordered_stage_skips(state, run_dir, timestamp=timestamp))
    if sources:
        state["reconstructed_from_artifacts"] = {
            "at": timestamp,
            "sources": _unique_strings(sources),
        }


def _overlay_durable_run_artifacts(
    state: dict[str, Any],
    run_dir: Path,
    *,
    timestamp: str,
) -> bool:
    changed = False
    changed = (
        _overlay_run_control_artifact(state, run_dir, timestamp=timestamp)
        or changed
    )
    changed = _overlay_terminal_run_status_artifact(state, run_dir) or changed
    return changed


def _overlay_run_control_artifact(
    state: dict[str, Any],
    run_dir: Path,
    *,
    timestamp: str,
) -> bool:
    control = _read_json_artifact(run_control_path(run_dir))
    if control is None:
        return False
    status = _string_value(control.get("status"))
    if status not in {"active", "paused", "cancelled"}:
        return False

    payload = dict(control)
    payload["run_id"] = str(
        payload.get("run_id")
        or state.get("run_id")
        or _resolve_run_id(run_dir)
    )
    payload["run_dir"] = str(run_dir.resolve())
    changed = False
    if state.get("control") != payload:
        state["control"] = payload
        changed = True

    history = control.get("history")
    if isinstance(history, list):
        control_history = [dict(item) for item in history if isinstance(item, Mapping)]
        if control_history and state.get("control_history") != control_history:
            state["control_history"] = control_history
            changed = True

    if changed:
        state["updated_at"] = _string_value(control.get("requested_at")) or timestamp
    return changed


def _overlay_terminal_run_status_artifact(
    state: dict[str, Any],
    run_dir: Path,
) -> bool:
    run_status = _read_json_artifact(run_dir / RUN_STATUS_FILENAME)
    if run_status is None:
        if "terminal_run_status" in state:
            state.pop("terminal_run_status", None)
            return True
        return False

    persisted_status = _string_value(run_status.get("status")) or "terminal"
    if (
        run_status.get("terminal") is not True
        and not _is_terminal_run_status_name(persisted_status)
    ):
        if "terminal_run_status" in state:
            state.pop("terminal_run_status", None)
            return True
        return False

    terminal = {
        "status": persisted_status,
        "ok": run_status.get("ok"),
        "terminal": True,
        "updated_at": _payload_timestamp(run_status),
        "artifacts": _string_artifacts(run_status.get("artifacts")),
    }
    diagnostics = run_status.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        terminal["diagnostics"] = dict(diagnostics)
    control = run_status.get("control")
    if isinstance(control, Mapping):
        terminal["control"] = dict(control)
    provenance = run_status.get("provenance")
    if isinstance(provenance, Mapping):
        terminal["provenance"] = dict(provenance)

    if state.get("terminal_run_status") == terminal:
        return False
    state["terminal_run_status"] = terminal
    updated_at = terminal.get("updated_at")
    if isinstance(updated_at, str) and updated_at:
        state["updated_at"] = updated_at
    return True


def _replay_trace_artifact(
    state: dict[str, Any],
    run_dir: Path,
    *,
    timestamp: str,
) -> bool:
    trace_path = run_dir / _RUN_TRACE_FILENAME
    if not trace_path.exists():
        return False
    applied = False
    for record in _read_jsonl_records(trace_path):
        stage = _string_value(record.get("stage"))
        if stage not in RUN_STAGES:
            continue
        _apply_status_to_state(
            state,
            stage=stage,
            status_payload=record,
            trace_event_id=_string_value(record.get("event_id")),
            timestamp=_string_value(record.get("timestamp")) or timestamp,
            replay_terminal_rerun=True,
        )
        applied = True
    return applied


def _replay_status_artifacts(
    state: dict[str, Any],
    run_dir: Path,
    *,
    timestamp: str,
) -> list[str]:
    applied: list[str] = []
    for stage, filename in _STAGE_STATUS_ARTIFACTS:
        record = _stage_record(state, stage)
        status_payload = _read_json_artifact(run_dir / filename)
        if status_payload is None:
            continue
        if not _should_replay_status_artifact(stage, record, status_payload):
            continue
        stale_upstream = _stale_ordered_status_artifact_upstream(
            state,
            stage,
            record,
            status_payload,
        )
        if stale_upstream is not None:
            _mark_stale_status_artifact(
                state,
                stage=stage,
                record=record,
                status_payload=status_payload,
                upstream=stale_upstream,
            )
            continue
        _apply_status_to_state(
            state,
            stage=stage,
            status_payload=status_payload,
            trace_event_id=None,
            timestamp=_payload_timestamp(status_payload) or timestamp,
        )
        applied.append(filename)
    return applied


def _should_replay_status_artifact(
    stage: str,
    record: Mapping[str, Any],
    status_payload: Mapping[str, Any],
) -> bool:
    target = stage_status_to_step_status(stage, status_payload)
    if target == "pending":
        return False
    if _stale_reset_blocks_status_artifact(record, status_payload):
        return False
    current = _status(record)
    if current == "pending":
        return True
    if current in RETRYABLE_STEP_STATUSES and target in TERMINAL_STEP_STATUSES:
        return True

    payload_timestamp = _payload_timestamp(status_payload)
    record_timestamp = _string_value(record.get("updated_at"))
    if _timestamp_is_after(payload_timestamp, record_timestamp):
        return True

    payload_artifacts = _string_artifacts(status_payload.get("artifacts"))
    record_artifacts = _string_artifacts(record.get("artifacts"))
    if payload_artifacts and any(
        record_artifacts.get(key) != value for key, value in payload_artifacts.items()
    ):
        return True

    raw_status = status_payload.get("status")
    if isinstance(raw_status, str) and raw_status and record.get("stage_status") != raw_status:
        return True
    return False


def _stale_ordered_status_artifact_upstream(
    state: Mapping[str, Any],
    stage: str,
    record: Mapping[str, Any],
    status_payload: Mapping[str, Any],
) -> dict[str, str] | None:
    if stage not in RUN_STAGE_ORDER:
        return None
    if _status(record) in TERMINAL_STEP_STATUSES:
        return None
    target = stage_status_to_step_status(stage, status_payload)
    if target not in TERMINAL_STEP_STATUSES:
        return None
    upstream = _latest_relevant_upstream_ordered_stage(state, stage)
    if upstream is None:
        return None
    payload_timestamp = _payload_timestamp(status_payload)
    if _timestamp_is_demonstrably_after(upstream["timestamp"], payload_timestamp):
        return upstream
    return None


def _latest_relevant_upstream_ordered_stage(
    state: Mapping[str, Any],
    stage: str,
) -> dict[str, str] | None:
    stages = state.get("stages")
    if not isinstance(stages, Mapping) or stage not in RUN_STAGE_ORDER:
        return None
    latest: dict[str, str] | None = None
    for upstream_stage in RUN_STAGE_ORDER[: RUN_STAGE_ORDER.index(stage)]:
        if not _stage_depends_on_upstream(upstream_stage, stage):
            continue
        record = stages.get(upstream_stage)
        if not isinstance(record, Mapping):
            continue
        status = _status(record)
        if status not in TERMINAL_STEP_STATUSES and status not in RETRYABLE_STEP_STATUSES:
            continue
        timestamp = _record_timestamp(record)
        if timestamp is None:
            continue
        if latest is None or _timestamp_is_after(timestamp, latest["timestamp"]):
            latest = {
                "stage": upstream_stage,
                "status": status,
                "timestamp": timestamp,
            }
    return latest


def _mark_stale_status_artifact(
    state: dict[str, Any],
    *,
    stage: str,
    record: dict[str, Any],
    status_payload: Mapping[str, Any],
    upstream: Mapping[str, str],
) -> None:
    current = _status(record)
    target = stage_status_to_step_status(stage, status_payload)
    upstream_stage = upstream["stage"]
    upstream_status = upstream["status"]
    upstream_timestamp = upstream["timestamp"]
    reason = _stale_reconstruction_reason(upstream_status)
    snapshot = _status_payload_terminal_snapshot(record, target, status_payload)
    record["stale_terminal_status"] = snapshot
    record["stale_reset"] = {
        "status": "stale-reset",
        "at": upstream_timestamp,
        "reason": reason,
        "upstream_stage": upstream_stage,
        "upstream_status": upstream_status,
        "previous_terminal_status": snapshot,
    }
    if current == "pending":
        record["updated_at"] = upstream_timestamp
        record["retryable"] = False
        record["terminal"] = False
        record["failure"] = None
        record["skip_reason"] = None
        record["artifacts"] = {}
        history = record.setdefault("history", [])
        if isinstance(history, list):
            history.append(
                {
                    "at": upstream_timestamp,
                    "from": target,
                    "to": "pending",
                    "reason": reason,
                    "upstream_stage": upstream_stage,
                }
            )
        state["updated_at"] = upstream_timestamp


def _stale_reconstruction_reason(upstream_status: str) -> str:
    if upstream_status in TERMINAL_STEP_STATUSES:
        return "stale_reset_after_upstream_completion"
    if upstream_status == "failed":
        return "stale_reset_after_upstream_failure"
    return "stale_reset_after_upstream_running"


def _status_payload_terminal_snapshot(
    record: Mapping[str, Any],
    status: str,
    status_payload: Mapping[str, Any],
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "status": status,
        "attempt": int(record.get("attempt") or 0),
    }
    payload_timestamp = _payload_timestamp(status_payload)
    if payload_timestamp is not None:
        snapshot["updated_at"] = payload_timestamp
        snapshot["finished_at"] = payload_timestamp
    raw_status = status_payload.get("status")
    if isinstance(raw_status, str) and raw_status:
        snapshot["stage_status"] = raw_status
    skip_reason = status_payload.get("skip_reason")
    if isinstance(skip_reason, str) and skip_reason:
        snapshot["skip_reason"] = skip_reason
    artifacts = _string_artifacts(status_payload.get("artifacts"))
    if artifacts:
        snapshot["artifacts"] = artifacts
    return snapshot


def _stale_reset_blocks_status_artifact(
    record: Mapping[str, Any],
    status_payload: Mapping[str, Any],
) -> bool:
    stale_reset = record.get("stale_reset")
    stale_terminal_status = record.get("stale_terminal_status")
    if (
        not isinstance(stale_reset, Mapping)
        and not isinstance(stale_terminal_status, Mapping)
    ):
        return False

    stale_reset_at = None
    if isinstance(stale_reset, Mapping):
        stale_reset_at = _string_value(stale_reset.get("at"))
    if stale_reset_at is None:
        return True

    payload_timestamp = _payload_timestamp(status_payload)
    return not _timestamp_is_demonstrably_after_stale_reset(payload_timestamp, stale_reset_at)


def _timestamp_is_demonstrably_after_stale_reset(
    payload_timestamp: str | None,
    stale_reset_at: str,
) -> bool:
    return _timestamp_is_demonstrably_after(payload_timestamp, stale_reset_at)


def _timestamp_is_demonstrably_after(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    left_datetime = _parse_timestamp(left)
    right_datetime = _parse_timestamp(right)
    if left_datetime is not None and right_datetime is not None:
        if left_datetime <= right_datetime:
            return False
        same_whole_second = (
            left_datetime.replace(microsecond=0)
            == right_datetime.replace(microsecond=0)
        )
        if same_whole_second and (
            not _timestamp_has_subsecond_precision(left)
            or not _timestamp_has_subsecond_precision(right)
        ):
            return False
        return True
    return left > right


def _timestamp_has_subsecond_precision(value: str) -> bool:
    time_part = value.split("T", 1)[-1]
    for separator in ("Z", "+", "-"):
        time_part = time_part.split(separator, 1)[0]
    return "." in time_part


def _infer_manual_source_ordered_stage_skips(
    state: dict[str, Any],
    run_dir: Path,
    *,
    timestamp: str,
) -> list[str]:
    if not _manual_sources_ingested(state, run_dir):
        return []

    skipped: list[str] = []
    artifacts: dict[str, str] = {}
    evidence_path = run_dir / "evidence.json"
    if evidence_path.exists():
        artifacts["evidence"] = str(evidence_path)
    manual_status_path = run_dir / "manual_ingest_status.json"
    if manual_status_path.exists():
        artifacts["manual_ingest_status"] = str(manual_status_path)
    artifacts["run_steps"] = str(run_steps_path(run_dir))

    for stage in ("planning", "ingest", "fetch_claims", "ingest_vision"):
        record = _stage_record(state, stage)
        if _status(record) != "pending":
            continue
        _apply_stage_transition(
            state,
            stage,
            "skipped",
            reason="manual_sources_run",
            timestamp=timestamp,
        )
        record["artifacts"] = dict(artifacts)
        record["stage_status"] = "skipped"
        record["reconstructed_skip"] = {
            "status": "skipped",
            "reason": "manual_sources_run",
            "at": timestamp,
            "source": "manual_source_ingest",
        }
        skipped.append(stage)

    if not skipped:
        return []
    return ["manual_source_skip_inference"]


def _manual_sources_ingested(state: Mapping[str, Any], run_dir: Path) -> bool:
    manual_record = _stage_record(state, "ingest_manual")
    if _status(manual_record) == "completed":
        stage_status = manual_record.get("stage_status")
        if stage_status in {"manual_sources_ingested", "completed"}:
            return True

    manual_status = _read_json_artifact(run_dir / "manual_ingest_status.json")
    if manual_status is not None and manual_status.get("status") == "manual_sources_ingested":
        return True

    evidence = _read_json_artifact(run_dir / "evidence.json")
    if evidence is None:
        return False
    manual_ingest = evidence.get("manual_ingest")
    return (
        isinstance(manual_ingest, Mapping)
        and manual_ingest.get("status") == "manual_sources_ingested"
    )


def _apply_status_to_state(
    state: dict[str, Any],
    *,
    stage: str,
    status_payload: Mapping[str, Any],
    trace_event_id: str | None,
    timestamp: str | None,
    replay_terminal_rerun: bool = False,
) -> None:
    target = stage_status_to_step_status(stage, status_payload)
    if target == "pending":
        return
    record = _stage_record(state, stage)
    current = _status(record)
    if replay_terminal_rerun and current in TERMINAL_STEP_STATUSES and target != "skipped":
        _apply_stage_transition(
            state,
            stage,
            "running",
            reason="replay_terminal_stage_rerun",
            timestamp=timestamp,
            trace_event_id=trace_event_id,
        )
        current = _status(record)
    if current == "completed" and target == "skipped":
        _apply_completed_stage_skip_to_state(
            state,
            stage=stage,
            status_payload=status_payload,
            trace_event_id=trace_event_id,
            timestamp=timestamp,
        )
        return
    if (
        current == "running"
        and target == "skipped"
        and _previous_terminal_status(record) == "completed"
    ):
        _apply_completed_stage_skip_to_state(
            state,
            stage=stage,
            status_payload=status_payload,
            trace_event_id=trace_event_id,
            timestamp=timestamp,
            from_status="running",
        )
        return
    if current == "pending" and target != "running":
        _apply_stage_transition(
            state,
            stage,
            "running",
            reason="implicit_stage_started",
            timestamp=timestamp,
        )
    _apply_stage_transition(
        state,
        stage,
        target,
        reason=_transition_reason(target, status_payload),
        timestamp=timestamp,
        trace_event_id=trace_event_id,
        status_payload=status_payload,
    )


def _apply_stage_transition(
    state: dict[str, Any],
    stage: str,
    to_status: str,
    *,
    reason: str,
    timestamp: str | None,
    trace_event_id: str | None = None,
    status_payload: Mapping[str, Any] | None = None,
) -> None:
    record = _stage_record(state, stage)
    from_status = _status(record)
    allowed = set(RUN_STEP_TRANSITIONS[from_status])
    if to_status not in allowed:
        raise RunStepStateError(
            code="invalid_state_transition",
            message=(
                f"cannot transition stage '{stage}' from {from_status} to {to_status}"
            ),
            stage=stage,
            from_status=from_status,
            to_status=to_status,
        )

    now = timestamp or _utc_now()
    record["status"] = to_status
    record["updated_at"] = now
    record["retryable"] = to_status in RETRYABLE_STEP_STATUSES
    record["terminal"] = to_status in TERMINAL_STEP_STATUSES
    if to_status == "running":
        if from_status in TERMINAL_STEP_STATUSES:
            snapshot = _terminal_snapshot(record, from_status)
            record["previous_terminal_status"] = snapshot
            rerun_start_hash = _stage_downstream_input_hash_from_artifacts(
                state,
                stage,
                _string_artifacts(record.get("artifacts")),
            )
            if rerun_start_hash is not None:
                record["rerun_start_downstream_input_hash"] = rerun_start_hash
            terminal_history = record.setdefault("terminal_history", [])
            if isinstance(terminal_history, list):
                terminal_history.append({**snapshot, "rerun_started_at": now})
            record["last_rerun_status"] = "running"
            record["last_rerun_at"] = now
        record["attempt"] = int(record.get("attempt") or 0) + 1
        record["started_at"] = now
        record.pop("finished_at", None)
    elif from_status == "running" and record.get("previous_terminal_status"):
        record["last_rerun_status"] = to_status
        record["last_rerun_at"] = now
    if to_status in TERMINAL_STEP_STATUSES or to_status == "failed":
        record["finished_at"] = now
    if to_status == "failed" and status_payload is not None:
        record["failure"] = _failure_summary(status_payload)
    elif to_status != "failed":
        record["failure"] = None
    if to_status == "skipped":
        record["skip_reason"] = reason
    elif to_status == "running":
        record["skip_reason"] = None
    if trace_event_id:
        trace_ids = record.setdefault("trace_event_ids", [])
        if isinstance(trace_ids, list) and trace_event_id not in trace_ids:
            trace_ids.append(trace_event_id)
    if status_payload is not None:
        artifacts = _string_artifacts(status_payload.get("artifacts"))
        if artifacts:
            record["artifacts"] = artifacts
        evidence_source = status_payload.get("evidence_source")
        if isinstance(evidence_source, Mapping):
            record["evidence_source"] = dict(evidence_source)
        raw_status = status_payload.get("status")
        if isinstance(raw_status, str) and raw_status:
            record["stage_status"] = raw_status
        if stage == "parallel_orchestration":
            _copy_parallel_status_fields(record, status_payload)
        if to_status in TERMINAL_STEP_STATUSES:
            downstream_input_hash = _stage_downstream_input_hash(
                state,
                stage,
                status_payload,
            )
            if downstream_input_hash is not None:
                record["downstream_input_hash"] = downstream_input_hash

    history = record.setdefault("history", [])
    if isinstance(history, list):
        history.append(
            {
                "at": now,
                "from": from_status,
                "to": to_status,
                "reason": reason,
                **({"trace_event_id": trace_event_id} if trace_event_id else {}),
            }
        )
    if (
        from_status == "running"
        and to_status in TERMINAL_STEP_STATUSES
        and _completed_stage_changed_downstream_inputs(record)
    ):
        reset_reason = "stale_reset_after_upstream_completion"
        if isinstance(record.get("previous_terminal_status"), Mapping):
            reset_reason = "stale_reset_after_upstream_rerun"
        _reset_downstream_ordered_terminal_stages(
            state,
            upstream_stage=stage,
            timestamp=now,
            trace_event_id=trace_event_id,
            reason=reset_reason,
        )
    if to_status in TERMINAL_STEP_STATUSES or to_status == "failed":
        record.pop("rerun_start_downstream_input_hash", None)
    state["updated_at"] = now


def _copy_parallel_status_fields(
    record: dict[str, Any],
    status_payload: Mapping[str, Any],
) -> None:
    for key in (
        "ok",
        "adapter",
        "parallel_degraded",
        "degraded_reason",
        "needs_serial_handoff",
    ):
        if key in status_payload:
            record[key] = status_payload[key]
    for key in ("failure_counts", "diagnostics"):
        value = status_payload.get(key)
        if isinstance(value, Mapping):
            record[key] = dict(value)


def _apply_completed_stage_skip_to_state(
    state: dict[str, Any],
    *,
    stage: str,
    status_payload: Mapping[str, Any],
    trace_event_id: str | None,
    timestamp: str | None,
    from_status: str = "completed",
) -> None:
    record = _stage_record(state, stage)
    now = timestamp or _utc_now()
    snapshot = _previous_terminal_snapshot(record) if from_status == "running" else None
    if snapshot is None:
        snapshot = _terminal_snapshot(record, "completed")
    record["status"] = "completed"
    record["updated_at"] = now
    record["retryable"] = False
    record["terminal"] = True
    record["skip_reason"] = _transition_reason("skipped", status_payload)
    record["previous_terminal_status"] = snapshot
    record["last_rerun_status"] = "skipped"
    record["last_rerun_at"] = now
    for key in ("started_at", "finished_at"):
        value = snapshot.get(key)
        if value is not None:
            record[key] = value
    artifacts = _string_artifacts(status_payload.get("artifacts"))
    if artifacts:
        record["artifacts"] = artifacts
    raw_status = status_payload.get("status")
    if isinstance(raw_status, str) and raw_status:
        record["stage_status"] = raw_status
    trace_ids = record.setdefault("trace_event_ids", [])
    if isinstance(trace_ids, list) and trace_event_id and trace_event_id not in trace_ids:
        trace_ids.append(trace_event_id)
    history = record.setdefault("history", [])
    if isinstance(history, list):
        history.append(
            {
                "at": now,
                "from": from_status,
                "to": "completed",
                "reason": record["skip_reason"],
                "rerun_status": "skipped",
                **({"trace_event_id": trace_event_id} if trace_event_id else {}),
            }
        )
    terminal_history = record.setdefault("terminal_history", [])
    if isinstance(terminal_history, list):
        terminal_history.append({**snapshot, "rerun_status": "skipped", "rerun_at": now})
    state["updated_at"] = now


def _reset_downstream_ordered_terminal_stages(
    state: dict[str, Any],
    *,
    upstream_stage: str,
    timestamp: str,
    trace_event_id: str | None,
    reason: str,
) -> None:
    if upstream_stage not in RUN_STAGE_ORDER:
        return
    start = RUN_STAGE_ORDER.index(upstream_stage) + 1
    for stage in RUN_STAGE_ORDER[start:]:
        if not _stage_depends_on_upstream(upstream_stage, stage):
            continue
        record = _stage_record(state, stage)
        from_status = _status(record)
        if from_status not in TERMINAL_STEP_STATUSES:
            continue
        snapshot = _terminal_snapshot(record, from_status)
        record["status"] = "pending"
        record["updated_at"] = timestamp
        record["retryable"] = False
        record["terminal"] = False
        record["failure"] = None
        record["skip_reason"] = None
        record["artifacts"] = {}
        record["stale_terminal_status"] = snapshot
        record["stale_reset"] = {
            "status": "stale-reset",
            "at": timestamp,
            "reason": reason,
            "upstream_stage": upstream_stage,
            "previous_terminal_status": snapshot,
            **({"trace_event_id": trace_event_id} if trace_event_id else {}),
        }
        for key in (
            "finished_at",
            "last_rerun_at",
            "last_rerun_status",
            "previous_terminal_status",
            "stage_status",
            "started_at",
        ):
            record.pop(key, None)
        history = record.setdefault("history", [])
        if isinstance(history, list):
            history.append(
                {
                    "at": timestamp,
                    "from": from_status,
                    "to": "pending",
                    "reason": reason,
                    "upstream_stage": upstream_stage,
                    **({"trace_event_id": trace_event_id} if trace_event_id else {}),
                }
            )
        terminal_history = record.setdefault("terminal_history", [])
        if isinstance(terminal_history, list):
            terminal_history.append(
                {
                    **snapshot,
                    "stale_reset_at": timestamp,
                    "stale_reset_reason": reason,
                    "upstream_stage": upstream_stage,
                }
            )


def _stage_depends_on_upstream(upstream_stage: str, downstream_stage: str) -> bool:
    return downstream_stage in _DOWNSTREAM_RESET_DEPENDENCIES.get(upstream_stage, ())


def _completed_stage_changed_downstream_inputs(record: Mapping[str, Any]) -> bool:
    current_hash = record.get("downstream_input_hash")
    rerun_start_hash = record.get("rerun_start_downstream_input_hash")
    if (
        isinstance(rerun_start_hash, str)
        and rerun_start_hash
        and isinstance(current_hash, str)
        and current_hash
    ):
        return rerun_start_hash != current_hash

    previous = _previous_terminal_snapshot(record)
    if previous is None:
        return True
    previous_hash = previous.get("downstream_input_hash")
    if (
        isinstance(previous_hash, str)
        and previous_hash
        and isinstance(current_hash, str)
        and current_hash
    ):
        return previous_hash != current_hash
    return True


def _control_status(state: Mapping[str, Any]) -> str | None:
    control = state.get("control")
    if not isinstance(control, Mapping):
        return None
    status = control.get("status")
    return status if status in {"active", "paused", "cancelled"} else None


def _terminal_run_status_payload(state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    terminal = state.get("terminal_run_status")
    if not isinstance(terminal, Mapping):
        return None
    status = _string_value(terminal.get("status"))
    if terminal.get("terminal") is True or _is_terminal_run_status_name(status):
        return terminal
    return None


def _raise_if_control_blocks_stage(state: Mapping[str, Any], stage: str) -> None:
    terminal = _terminal_run_status_payload(state)
    if terminal is not None:
        persisted_status = _string_value(terminal.get("status")) or "terminal"
        raise RunStepStateError(
            code=_terminal_run_status_error_code(persisted_status),
            message="terminal runs cannot start another stage",
            stage=stage,
            from_status=persisted_status,
            to_status="running",
        )
    status = _control_status(state)
    if status == "paused":
        raise RunStepStateError(
            code="run_paused",
            message=(
                "run is paused; use resume-run before starting another stage"
            ),
            stage=stage,
        )
    if status == "cancelled":
        raise RunStepStateError(
            code="run_cancelled",
            message="run is cancelled and cannot start another stage",
            stage=stage,
        )


def _build_control_payload(
    run_dir: Path,
    state: Mapping[str, Any],
    *,
    status: str,
    action: str,
    requested_at: str,
    requested_by: str,
    reason: str,
    terminal: bool,
    diagnostics: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema_version": RUN_CONTROL_SCHEMA_VERSION,
        "run_id": str(state.get("run_id") or _resolve_run_id(run_dir)),
        "run_dir": str(run_dir.resolve()),
        "status": status,
        "action": action,
        "requested_at": requested_at,
        "requested_by": requested_by,
        "reason": reason,
        "terminal": terminal,
        "resume_next_safe_stage": state.get("next_safe_stage"),
        "next_stage_retryable": bool(state.get("next_stage_retryable")),
        "diagnostics": dict(diagnostics),
        "artifacts": _control_artifact_paths(run_dir),
    }
    return payload


def _apply_control_payload(state: dict[str, Any], control: Mapping[str, Any]) -> None:
    state["control"] = dict(control)
    state["updated_at"] = str(control.get("requested_at") or _utc_now())
    history = state.setdefault("control_history", [])
    if isinstance(history, list):
        history.append(
            {
                "at": control.get("requested_at"),
                "action": control.get("action"),
                "status": control.get("status"),
                "reason": control.get("reason"),
                "requested_by": control.get("requested_by"),
                "resume_next_safe_stage": control.get("resume_next_safe_stage"),
                "terminal": bool(control.get("terminal")),
            }
        )


def _mark_running_stages_interrupted(
    state: dict[str, Any],
    control: Mapping[str, Any],
) -> None:
    stages = state.get("stages")
    if not isinstance(stages, Mapping):
        return
    marker = {
        "action": control.get("action"),
        "status": control.get("status"),
        "at": control.get("requested_at"),
        "reason": control.get("reason"),
    }
    for record in stages.values():
        if isinstance(record, dict) and record.get("status") == "running":
            record["interrupted_by_control"] = dict(marker)


def _control_result(
    state: Mapping[str, Any],
    control_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": RUN_CONTROL_SCHEMA_VERSION,
        "run_id": state.get("run_id"),
        "run_dir": state.get("run_dir"),
        "status": control_artifact.get("status"),
        "action": control_artifact.get("action"),
        "terminal": control_artifact.get("terminal"),
        "next_safe_stage": state.get("next_safe_stage"),
        "next_stage_retryable": bool(state.get("next_stage_retryable")),
        "diagnostics": dict(control_artifact.get("diagnostics", {}))
        if isinstance(control_artifact.get("diagnostics"), Mapping)
        else {},
        "artifacts": dict(control_artifact.get("artifacts", {}))
        if isinstance(control_artifact.get("artifacts"), Mapping)
        else {},
    }


def _write_control_artifact(
    run_dir: Path,
    state: Mapping[str, Any],
    control: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(control)
    payload["run_id"] = str(state.get("run_id") or _resolve_run_id(run_dir))
    payload["run_dir"] = str(run_dir.resolve())
    payload["next_safe_stage"] = state.get("next_safe_stage")
    payload["next_stage_retryable"] = bool(state.get("next_stage_retryable"))
    history = state.get("control_history")
    if isinstance(history, list):
        payload["history"] = [item for item in history if isinstance(item, Mapping)]
    payload["artifacts"] = _control_artifact_paths(run_dir)
    _write_json_artifact(run_control_path(run_dir), payload)
    return payload


def _raise_if_persisted_terminal_run_status_blocks_control(
    run_dir: Path,
    *,
    action: str,
) -> None:
    run_status = _read_json_artifact(run_dir / RUN_STATUS_FILENAME) or {}
    persisted_status = _string_value(run_status.get("status")) or "terminal"
    if (
        run_status.get("terminal") is not True
        and not _is_terminal_run_status_name(persisted_status)
    ):
        return
    raise RunStepStateError(
        code=_terminal_run_status_error_code(persisted_status),
        message=f"terminal runs cannot be controlled with {action}-run",
        from_status=persisted_status,
        to_status=_control_action_target_status(action),
    )


def _raise_if_resume_blocked_by_terminal_run(
    run_dir: Path,
    state: Mapping[str, Any],
    control_status: str | None,
) -> None:
    state_status = _string_value(state.get("status"))
    if state_status == "completed":
        raise RunStepStateError(
            code="run_completed",
            message="completed runs cannot be resumed",
            from_status=state_status,
            to_status="resumed",
        )
    if state_status == "cancelled":
        raise RunStepStateError(
            code="run_cancelled",
            message="cancelled runs cannot be resumed",
            from_status=state_status,
            to_status="resumed",
        )

    run_status = _read_json_artifact(run_dir / RUN_STATUS_FILENAME) or {}
    if run_status.get("terminal") is not True:
        if control_status == "paused":
            return
        return
    persisted_status = _string_value(run_status.get("status")) or "terminal"
    raise RunStepStateError(
        code=_terminal_run_status_error_code(persisted_status),
        message="terminal runs cannot be resumed unless they are paused",
        from_status=persisted_status,
        to_status="resumed",
    )


def _is_terminal_run_status_name(status: str) -> bool:
    return status.startswith(("completed", "cancelled", "failed"))


def _terminal_run_status_error_code(status: str) -> str:
    if status.startswith("completed"):
        return "run_completed"
    if status.startswith("cancelled"):
        return "run_cancelled"
    return "run_terminal"


def _control_action_target_status(action: str) -> str:
    return {
        "pause": "paused",
        "resume": "resumed",
        "cancel": "cancelled",
    }.get(action, action)


def _write_control_run_status(
    run_dir: Path,
    control: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    status_override: str | None = None,
) -> None:
    existing = _read_json_artifact(run_dir / RUN_STATUS_FILENAME) or {}
    evidence = _read_json_artifact(run_dir / "evidence.json") or {}
    control_diagnostics = (
        dict(control.get("diagnostics", {}))
        if isinstance(control.get("diagnostics"), Mapping)
        else {}
    )
    diagnostics = (
        dict(existing.get("diagnostics", {}))
        if isinstance(existing.get("diagnostics"), Mapping)
        else {}
    )
    previous_actionable_cause = diagnostics.get("actionable_cause")
    diagnostics.update(control_diagnostics)
    control_actionable_cause = control_diagnostics.get("actionable_cause")
    if (
        isinstance(previous_actionable_cause, str)
        and previous_actionable_cause
        and previous_actionable_cause != control_actionable_cause
    ):
        diagnostics.setdefault("previous_actionable_cause", previous_actionable_cause)

    artifacts = _control_artifact_paths(run_dir)
    if isinstance(existing.get("artifacts"), Mapping):
        artifacts = {str(key): value for key, value in existing["artifacts"].items()}
        artifacts.update(_control_artifact_paths(run_dir))

    status = status_override or str(control.get("status") or "unknown")
    payload = dict(existing)
    payload.update({
        "schema_version": _RUN_STATUS_SCHEMA_VERSION,
        "run_id": str(state.get("run_id") or _resolve_run_id(run_dir)),
        "run_dir": str(run_dir.resolve()),
        "invocation": str(existing.get("invocation") or ""),
        "question": str(existing.get("question") or evidence.get("question") or ""),
        "selected_mode": str(existing.get("selected_mode") or "full-runner"),
        "status": status,
        "ok": False if control.get("status") == "cancelled" else True,
        "terminal": bool(control.get("terminal")),
        "updated_at": str(control.get("requested_at") or _utc_now()),
        "provenance": _control_provenance(existing, control),
        "diagnostics": diagnostics,
        "control": {
            "status": control.get("status"),
            "action": control.get("action"),
            "requested_at": control.get("requested_at"),
            "requested_by": control.get("requested_by"),
            "reason": control.get("reason"),
            "resume_next_safe_stage": control.get("resume_next_safe_stage"),
        },
        "artifacts": artifacts,
    })
    artifact_handoff = (
        dict(existing.get("artifact_handoff", {}))
        if isinstance(existing.get("artifact_handoff"), Mapping)
        else {}
    )
    handoff_artifact_paths = (
        {
            str(key): value
            for key, value in artifact_handoff["artifact_paths"].items()
        }
        if isinstance(artifact_handoff.get("artifact_paths"), Mapping)
        else {}
    )
    handoff_artifact_paths.update(artifacts)
    artifact_handoff.update({
        "run_dir": payload["run_dir"],
        "status": status,
        "ok": payload["ok"],
        "terminal": payload["terminal"],
        "artifact_paths": handoff_artifact_paths,
        "diagnostics": dict(diagnostics),
    })
    payload["artifact_handoff"] = artifact_handoff
    _write_json_artifact(run_dir / RUN_STATUS_FILENAME, payload)


def _control_provenance(
    existing: Mapping[str, Any],
    control: Mapping[str, Any],
) -> dict[str, Any]:
    provenance = (
        dict(existing.get("provenance", {}))
        if isinstance(existing.get("provenance"), Mapping)
        else {}
    )
    provenance["control_action"] = control.get("action")
    provenance["control_status"] = control.get("status")
    if control.get("status") == "cancelled":
        provenance["type"] = "cancelled"
    elif "type" not in provenance:
        provenance["type"] = "full_runner"
    return provenance


def _control_artifact_paths(run_dir: Path) -> dict[str, str]:
    known_files = {
        "run_control": RUN_CONTROL_FILENAME,
        "run_steps": RUN_STEPS_FILENAME,
        "run_status": RUN_STATUS_FILENAME,
        "run_trace": _RUN_TRACE_FILENAME,
        "planning_status": "status.json",
        "evidence": "evidence.json",
        "budget_estimate": "budget_estimate.json",
        "research_tasks": "research_tasks.json",
        "search_tasks": "search_tasks.json",
        "search_results": "search_results.jsonl",
        "fetch_queue": "fetch_queue.json",
        "visual_tasks": "visual_tasks.json",
        "visual_observations": "visual_observations.jsonl",
        "subagent_assignments": "subagent_assignments.jsonl",
        "parallel_orchestration_status": "parallel_orchestration_status.json",
        "merge_status": "merge_status.json",
        "visual_acquisition_status": "visual_acquisition_status.json",
        "visual_provider_status": "visual_provider_status.json",
        "image_fetch_status": "image_fetch_status.jsonl",
        "ingest_status": "ingest_status.json",
        "manual_ingest_status": "manual_ingest_status.json",
        "fetch_claims_status": "fetch_claims_status.json",
        "vision_ingest_status": "vision_ingest_status.json",
        "guardrails_status": "guardrails_status.json",
        "verification_matrix_status": "verification_matrix_status.json",
        "report": "report.md",
        "report_status": "report_status.json",
    }
    artifacts: dict[str, str] = {}
    for key, filename in known_files.items():
        path = run_dir / filename
        if path.exists() or key in {"run_control", "run_steps", "run_status"}:
            artifacts[key] = str(path)
    return artifacts


def _known_child_contexts(run_dir: Path) -> list[dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}

    def add_context(
        child_id: Any,
        *,
        source: str,
        task_id: Any = None,
        status: Any = None,
        stage: Any = None,
        event_type: Any = None,
        observed_at: Any = None,
    ) -> None:
        if not isinstance(child_id, str) or not child_id.strip():
            return
        key = child_id.strip()
        record = contexts.setdefault(
            key,
            {
                "child_context_id": key,
                "sources": [],
                "task_id": None,
                "last_status": None,
                "last_stage": None,
                "last_event_type": None,
                "last_observed_at": None,
                "close_observed": False,
            },
        )
        if source not in record["sources"]:
            record["sources"].append(source)
        if isinstance(task_id, str) and task_id.strip():
            record["task_id"] = task_id.strip()
        if isinstance(status, str) and status.strip():
            record["last_status"] = status.strip()
        if isinstance(stage, str) and stage.strip():
            record["last_stage"] = stage.strip()
        if isinstance(event_type, str) and event_type.strip():
            record["last_event_type"] = event_type.strip()
            if event_type.strip() == "close_agent":
                record["close_observed"] = True
        if isinstance(observed_at, str) and observed_at.strip():
            previous = record.get("last_observed_at")
            if not isinstance(previous, str) or _timestamp_is_after(observed_at, previous):
                record["last_observed_at"] = observed_at.strip()

    for assignment in _read_jsonl_records(run_dir / "subagent_assignments.jsonl"):
        child_id = assignment.get("child_thread_id") or assignment.get("assigned_subagent_id")
        add_context(
            child_id,
            source="subagent_assignments",
            task_id=assignment.get("task_id") or assignment.get("id"),
            status=assignment.get("state") or assignment.get("status"),
            observed_at=assignment.get("timestamp"),
        )

    for event in _read_jsonl_records(run_dir / _RUN_TRACE_FILENAME):
        child_id = event.get("child_thread_id") or event.get("assigned_subagent_id")
        add_context(
            child_id,
            source="run_trace",
            task_id=event.get("task_id"),
            status=event.get("child_status") or event.get("status"),
            stage=event.get("stage"),
            event_type=event.get("event_type"),
            observed_at=event.get("timestamp"),
        )

    research_tasks = _read_json_artifact(run_dir / "research_tasks.json") or {}
    tasks = research_tasks.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, Mapping):
                continue
            add_context(
                task.get("last_child_thread_id"),
                source="research_tasks",
                task_id=task.get("id"),
                status=task.get("state"),
            )

    return [
        contexts[key]
        for key in sorted(contexts)
    ]


def _child_context_close_records(
    child_contexts: list[dict[str, Any]],
    *,
    requested: bool,
    timestamp: str,
) -> list[dict[str, Any]]:
    records = []
    for context in child_contexts:
        already_closed = bool(context.get("close_observed")) or context.get("last_status") in {
            "closed",
            "completed",
            "failed",
            "blocked",
        }
        if already_closed:
            result = "already_closed"
            attempted = False
        elif not requested:
            result = "not_requested"
            attempted = False
        else:
            result = "no_local_child_context_closer"
            attempted = False
        records.append(
            {
                "child_context_id": context["child_context_id"],
                "task_id": context.get("task_id"),
                "requested": requested and not already_closed,
                "attempted": attempted,
                "result": result,
                "recorded_at": timestamp,
            }
        )
    return records


def _cancellation_actionable_cause(
    child_contexts: list[dict[str, Any]],
    cleanup_children: bool,
) -> str:
    if not child_contexts:
        return (
            "run cancellation requested; no known child contexts were recorded "
            "in run artifacts"
        )
    if not cleanup_children:
        return (
            "run cancellation requested; child context cleanup was explicitly "
            "not requested"
        )
    return (
        "run cancellation requested; known child contexts were enumerated, but "
        "the local CLI has no deterministic child-context closer, so close "
        "records were persisted for operator follow-up"
    )


def _mapping_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _with_resume_summary(state: dict[str, Any]) -> dict[str, Any]:
    stages = state.get("stages")
    if not isinstance(stages, dict):
        return state
    next_stage = _next_retryable_optional_stage(stages) or _next_ordered_stage(stages)
    control_status = _control_status(state)
    if control_status == "cancelled":
        state["status"] = "cancelled"
        state["next_safe_stage"] = None
        state["next_stage_retryable"] = False
        return state
    terminal = _terminal_run_status_payload(state)
    if terminal is not None:
        persisted_status = _string_value(terminal.get("status")) or "terminal"
        if persisted_status.startswith("cancelled"):
            state["status"] = "cancelled"
        elif persisted_status.startswith("completed"):
            state["status"] = "completed"
        else:
            state["status"] = persisted_status
        state["next_safe_stage"] = None
        state["next_stage_retryable"] = False
        return state
    if next_stage is None:
        state["status"] = "completed"
        state["next_safe_stage"] = None
        state["next_stage_retryable"] = False
        return state
    next_record = stages[next_stage]
    next_status = _status(next_record)
    if control_status == "paused":
        state["status"] = "paused"
        state["next_safe_stage"] = next_stage
        state["next_stage_retryable"] = next_status in RETRYABLE_STEP_STATUSES
        return state
    state["status"] = "needs_retry" if next_status in RETRYABLE_STEP_STATUSES else "in_progress"
    state["next_safe_stage"] = next_stage
    state["next_stage_retryable"] = next_status in RETRYABLE_STEP_STATUSES
    return state


def _next_retryable_optional_stage(stages: Mapping[str, Any]) -> str | None:
    for stage in OPTIONAL_RUN_STAGES:
        record = stages.get(stage)
        if isinstance(record, Mapping) and _status(record) in RETRYABLE_STEP_STATUSES:
            return stage
    return None


def _next_ordered_stage(stages: Mapping[str, Any]) -> str | None:
    for stage in RUN_STAGE_ORDER:
        record = stages.get(stage)
        if not isinstance(record, Mapping):
            return stage
        if _status(record) not in TERMINAL_STEP_STATUSES:
            return stage
    return None


def _stage_record(state: Mapping[str, Any], stage: str) -> dict[str, Any]:
    stages = state.get("stages")
    if not isinstance(stages, Mapping):
        raise RunStepStateError(
            code="invalid_stages",
            message="run_steps.json stages must be an object",
        )
    record = stages.get(stage)
    if not isinstance(record, dict):
        raise RunStepStateError(
            code="missing_stage",
            message=f"run_steps.json is missing stage '{stage}'",
            stage=stage,
        )
    return record


def _status(record: Mapping[str, Any]) -> str:
    status = record.get("status")
    return status if isinstance(status, str) else "pending"


def _transition_reason(to_status: str, status_payload: Mapping[str, Any]) -> str:
    if to_status == "completed":
        return "stage_completed"
    if to_status == "failed":
        return "stage_failed"
    if to_status == "skipped":
        reason = status_payload.get("skip_reason")
        return reason if isinstance(reason, str) and reason else "stage_skipped"
    if to_status == "running":
        return "stage_running"
    return "stage_pending"


def _failure_summary(status_payload: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    raw_status = status_payload.get("status")
    if isinstance(raw_status, str):
        summary["status"] = raw_status
    errors = status_payload.get("errors")
    if isinstance(errors, list):
        summary["error_codes"] = [
            str(error.get("code"))
            for error in errors
            if isinstance(error, Mapping) and error.get("code")
        ][:10]
    validation = status_payload.get("validation")
    if isinstance(validation, Mapping) and validation.get("valid") is False:
        summary["validation_valid"] = False
    return summary


def _terminal_snapshot(record: Mapping[str, Any], status: str) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "status": status,
        "attempt": int(record.get("attempt") or 0),
    }
    for key in (
        "stage_status",
        "started_at",
        "finished_at",
        "updated_at",
        "skip_reason",
        "downstream_input_hash",
    ):
        value = record.get(key)
        if value is not None:
            snapshot[key] = value
    failure = record.get("failure")
    if isinstance(failure, Mapping):
        snapshot["failure"] = dict(failure)
    artifacts = _string_artifacts(record.get("artifacts"))
    if artifacts:
        snapshot["artifacts"] = artifacts
    trace_ids = record.get("trace_event_ids")
    if isinstance(trace_ids, list):
        snapshot["trace_event_ids"] = [str(trace_id) for trace_id in trace_ids if trace_id]
    return snapshot


def _previous_terminal_snapshot(record: Mapping[str, Any]) -> dict[str, Any] | None:
    previous = record.get("previous_terminal_status")
    return dict(previous) if isinstance(previous, Mapping) else None


def _previous_terminal_status(record: Mapping[str, Any]) -> str | None:
    previous = _previous_terminal_snapshot(record)
    status = previous.get("status") if previous is not None else None
    return status if isinstance(status, str) else None


def _string_artifacts(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if isinstance(key, str) and key and item is not None and str(item)
    }


def _stage_downstream_input_hash(
    state: Mapping[str, Any],
    stage: str,
    status_payload: Mapping[str, Any],
) -> str | None:
    return _stage_downstream_input_hash_from_artifacts(
        state,
        stage,
        _string_artifacts(status_payload.get("artifacts")),
    )


def _stage_downstream_input_hash_from_artifacts(
    state: Mapping[str, Any],
    stage: str,
    artifacts: Mapping[str, str],
) -> str | None:
    artifact_keys = _STAGE_DOWNSTREAM_INPUT_ARTIFACT_KEYS.get(stage, ())
    if not artifact_keys or not artifacts:
        return None
    run_dir = _state_run_dir(state)
    fingerprint_parts: dict[str, Any] = {}
    for artifact_key in artifact_keys:
        artifact_path = artifacts.get(artifact_key)
        if artifact_path is None:
            continue
        path = _resolve_artifact_path(run_dir, artifact_path)
        artifact_fingerprint = _artifact_fingerprint(path)
        if artifact_fingerprint is not None:
            fingerprint_parts[artifact_key] = artifact_fingerprint
    if not fingerprint_parts:
        return None

    encoded = json.dumps(
        fingerprint_parts,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _state_run_dir(state: Mapping[str, Any]) -> Path | None:
    run_dir = state.get("run_dir")
    if isinstance(run_dir, str) and run_dir:
        return Path(run_dir)
    return None


def _resolve_artifact_path(run_dir: Path | None, artifact_path: str) -> Path:
    path = Path(artifact_path)
    if path.is_absolute() or run_dir is None:
        return path
    return run_dir / path


def _artifact_fingerprint(path: Path) -> Any | None:
    try:
        suffix = path.suffix.lower()
        if suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if path.name == "evidence.json" and isinstance(payload, Mapping):
                payload = {
                    key: payload[key]
                    for key in _EVIDENCE_FINGERPRINT_KEYS
                    if key in payload
                }
            return {
                "kind": "json",
                "value": _stable_fingerprint_value(payload),
            }
        if suffix == ".jsonl":
            rows = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(_stable_fingerprint_value(json.loads(line)))
            return {"kind": "jsonl", "value": rows}
        return {
            "kind": "bytes",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _stable_fingerprint_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _stable_fingerprint_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if isinstance(key, str) and key not in _FINGERPRINT_VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_stable_fingerprint_value(item) for item in value]
    return value


def _timestamp_is_after(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    left_datetime = _parse_timestamp(left)
    right_datetime = _parse_timestamp(right)
    if left_datetime is not None and right_datetime is not None:
        return left_datetime > right_datetime
    return left > right


def _parse_timestamp(value: str) -> datetime | None:
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _read_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunStepStateError(
            code="invalid_json",
            message=f"invalid JSON in {path}: {exc}",
        ) from exc
    if not isinstance(state, dict):
        raise RunStepStateError(
            code="expected_object",
            message=f"{path} must contain a JSON object",
        )
    return state


def _read_json_artifact(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise RunStepStateError(
            code="invalid_json",
            message=f"invalid JSON in {path}: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise RunStepStateError(
            code="expected_object",
            message=f"{path} must contain a JSON object",
        )
    return payload


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return records
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RunStepStateError(
                code="invalid_json",
                message=f"invalid JSONL in {path} line {line_number}: {exc}",
            ) from exc
        if not isinstance(record, dict):
            raise RunStepStateError(
                code="expected_object",
                message=f"{path} line {line_number} must contain a JSON object",
            )
        records.append(record)
    return records


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(state), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_json_artifact(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _resolve_run_id(run_dir: Path, run_id: str | None = None) -> str:
    if run_id:
        return run_id
    evidence_path = run_dir / "evidence.json"
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return run_dir.name
    if isinstance(evidence, Mapping) and isinstance(evidence.get("run_id"), str):
        return str(evidence["run_id"])
    return run_dir.name


def _payload_run_id(status_payload: Mapping[str, Any]) -> str | None:
    run_id = status_payload.get("run_id")
    return run_id if isinstance(run_id, str) and run_id else None


def _payload_timestamp(status_payload: Mapping[str, Any]) -> str | None:
    return _latest_mapping_timestamp(status_payload)


def _record_timestamp(record: Mapping[str, Any]) -> str | None:
    for key in (
        "updated_at",
        "finished_at",
        "completed_at",
        "started_at",
        "generated_at",
        "fetched_at",
        "ingested_at",
        "verified_at",
        "enforced_at",
        "timestamp",
        "recorded_at",
        "created_at",
    ):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _latest_mapping_timestamp(payload: Mapping[str, Any]) -> str | None:
    latest: str | None = None
    for key in _TIMESTAMP_KEYS:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            continue
        if latest is None or _timestamp_is_after(value, latest):
            latest = value
    return latest


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _validate_stage(stage: str) -> None:
    if stage not in RUN_STAGES:
        raise RunStepStateError(
            code="invalid_stage",
            message="stage is not a known DeepResearch runner stage",
            stage=stage,
        )


def _validate_status(status: str, *, stage: str | None = None) -> None:
    if status not in RUN_STEP_STATUSES:
        raise RunStepStateError(
            code="invalid_step_status",
            message="step status is not one of pending, running, completed, failed, skipped",
            stage=stage,
            from_status=status,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
