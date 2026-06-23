"""Run step state machine for resumable DeepResearch stages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


RUN_STEPS_SCHEMA_VERSION = "codex-deepresearch.run-steps.v0"
RUN_STEPS_FILENAME = "run_steps.json"
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
OPTIONAL_RUN_STAGES = ("ingest_manual",)
RUN_STAGES = (
    "planning",
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
    ("ingest", "ingest_status.json"),
    ("ingest_manual", "manual_ingest_status.json"),
    ("fetch_claims", "fetch_claims_status.json"),
    ("ingest_vision", "vision_ingest_status.json"),
    ("enforce_guardrails", "guardrails_status.json"),
    ("verify_claims", "verification_matrix_status.json"),
    ("synthesize", "report_status.json"),
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


def add_run_steps_artifact(payload: dict[str, Any], run_dir: str | Path) -> None:
    """Add the canonical run step artifact link to a status/result payload."""

    artifacts = payload.setdefault("artifacts", {})
    if isinstance(artifacts, dict):
        artifacts["run_steps"] = str(run_steps_path(run_dir))


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
    if changed:
        _write_state(path, state)
    return _with_resume_summary(state)


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
    return {
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
    else:
        sources.extend(_replay_status_artifacts(state, run_dir, timestamp=timestamp))
    if sources:
        state["reconstructed_from_artifacts"] = {
            "at": timestamp,
            "sources": sources,
        }


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
        if _status(record) != "pending":
            continue
        status_payload = _read_json_artifact(run_dir / filename)
        if status_payload is None:
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
        raw_status = status_payload.get("status")
        if isinstance(raw_status, str) and raw_status:
            record["stage_status"] = raw_status

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
    if to_status == "running" and from_status in TERMINAL_STEP_STATUSES:
        _reset_downstream_ordered_terminal_stages(
            state,
            upstream_stage=stage,
            timestamp=now,
            trace_event_id=trace_event_id,
        )
    state["updated_at"] = now


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
) -> None:
    if upstream_stage not in RUN_STAGE_ORDER:
        return
    start = RUN_STAGE_ORDER.index(upstream_stage) + 1
    for stage in RUN_STAGE_ORDER[start:]:
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
            "reason": "stale_reset_after_upstream_rerun",
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
                    "reason": "stale_reset_after_upstream_rerun",
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
                    "stale_reset_reason": "stale_reset_after_upstream_rerun",
                    "upstream_stage": upstream_stage,
                }
            )


def _with_resume_summary(state: dict[str, Any]) -> dict[str, Any]:
    stages = state.get("stages")
    if not isinstance(stages, dict):
        return state
    next_stage = _next_retryable_optional_stage(stages) or _next_ordered_stage(stages)
    if next_stage is None:
        state["status"] = "completed"
        state["next_safe_stage"] = None
        state["next_stage_retryable"] = False
        return state
    next_record = stages[next_stage]
    next_status = _status(next_record)
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
    for key in ("stage_status", "started_at", "finished_at", "updated_at", "skip_reason"):
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
    for key in ("updated_at", "created_at", "generated_at", "fetched_at", "ingested_at"):
        value = status_payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


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
