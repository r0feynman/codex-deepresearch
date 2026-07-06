"""Read-only progress and shard monitor for DeepResearch run artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence


RUN_MONITOR_SCHEMA_VERSION = "codex-deepresearch.run-monitor.v0"

_KNOWN_ARTIFACTS = {
    "run_status": "run_status.json",
    "planning_status": "status.json",
    "ingest_status": "ingest_status.json",
    "run_steps": "run_steps.json",
    "run_control": "run_control.json",
    "run_trace": "run_trace.jsonl",
    "evidence": "evidence.json",
    "budget_estimate": "budget_estimate.json",
    "research_tasks": "research_tasks.json",
    "subagent_assignments": "subagent_assignments.jsonl",
    "parallel_orchestration_status": "parallel_orchestration_status.json",
    "merge_status": "merge_status.json",
    "visual_acquisition_status": "visual_acquisition_status.json",
    "visual_provider_status": "visual_provider_status.json",
    "image_fetch_status": "image_fetch_status.jsonl",
    "manual_ingest_status": "manual_ingest_status.json",
    "fetch_claims_status": "fetch_claims_status.json",
    "vision_ingest_status": "vision_ingest_status.json",
    "guardrails_status": "guardrails_status.json",
    "verification_matrix_status": "verification_matrix_status.json",
    "report_status": "report_status.json",
    "report": "report.md",
}

_STAGE_STATUS_ARTIFACTS = {
    "planning": "planning_status",
    "ingest": "ingest_status",
    "ingest_manual": "manual_ingest_status",
    "fetch_claims": "fetch_claims_status",
    "ingest_vision": "vision_ingest_status",
    "parallel_orchestration": "parallel_orchestration_status",
    "enforce_guardrails": "guardrails_status",
    "verify_claims": "verification_matrix_status",
    "synthesize": "report_status",
}

_TERMINAL_STATUS_ARTIFACTS = (
    "report_status",
    "verification_matrix_status",
    "guardrails_status",
    "parallel_orchestration_status",
    "vision_ingest_status",
    "manual_ingest_status",
    "ingest_status",
    "fetch_claims_status",
    "visual_provider_status",
    "visual_acquisition_status",
)

_SUMMARY_KEYS = (
    "run_id",
    "phase",
    "mode",
    "status",
    "shards",
    "evidence_counts",
    "budget",
)


def list_run_monitors(runs_dir: str | Path) -> dict[str, Any]:
    """Return monitor summaries for every run-like child directory."""

    base = Path(runs_dir)
    run_dirs = [
        child
        for child in sorted(base.iterdir(), key=lambda path: path.name)
        if child.is_dir() and _looks_like_run_dir(child)
    ] if base.exists() else []
    summaries = []
    for run_dir in run_dirs:
        detail = inspect_run_monitor(run_dir)
        summary = {key: detail[key] for key in _SUMMARY_KEYS}
        summary["run_path"] = run_dir.name
        summaries.append(summary)
    return {
        "schema_version": RUN_MONITOR_SCHEMA_VERSION,
        "runs_dir": _safe_runs_dir_label(base),
        "run_count": len(summaries),
        "runs": summaries,
    }


def inspect_run_monitor(run_dir: str | Path) -> dict[str, Any]:
    """Aggregate one run directory into a public-safe monitor payload."""

    run_dir = Path(run_dir).resolve()
    artifacts = _load_artifacts(run_dir)
    payloads = artifacts["payloads"]
    errors = artifacts["errors"]

    run_status = _mapping(payloads.get("run_status"))
    planning_status = _mapping(payloads.get("planning_status"))
    run_steps = _mapping(payloads.get("run_steps"))
    run_control = _control_payload(payloads, run_steps)
    evidence = _mapping(payloads.get("evidence"))
    budget_estimate = _mapping(payloads.get("budget_estimate"))
    research_tasks = _mapping(payloads.get("research_tasks"))
    parallel_status = _mapping(payloads.get("parallel_orchestration_status"))
    merge_status = _mapping(payloads.get("merge_status"))
    visual_acquisition = _mapping(payloads.get("visual_acquisition_status"))
    visual_provider = _mapping(payloads.get("visual_provider_status"))
    image_fetch_status = _list(payloads.get("image_fetch_status"))
    run_trace = _list(payloads.get("run_trace"))
    subagent_assignments = _list(payloads.get("subagent_assignments"))
    phase = _phase_summary(run_steps, run_status, planning_status, run_control, run_trace)

    return {
        "schema_version": RUN_MONITOR_SCHEMA_VERSION,
        "run_id": _run_id(run_dir, run_status, planning_status, evidence, parallel_status),
        "run_dir": str(run_dir),
        "status": _monitor_status(run_status, planning_status, parallel_status, run_control, phase, payloads),
        "ok": _monitor_ok(run_status, parallel_status, phase, payloads),
        "terminal": _monitor_terminal(run_status, run_control, phase, payloads),
        "phase": phase,
        "control": _control_summary(run_control, run_steps),
        "mode": _mode_summary(run_status, evidence, parallel_status, merge_status),
        "shards": _shard_summary(
            research_tasks,
            parallel_status,
            merge_status,
            subagent_assignments,
            run_trace,
        ),
        "partial_parallel": _partial_parallel_summary(
            run_status,
            parallel_status,
            merge_status,
        ),
        "child_attempt_probes": _child_attempt_probe_summary(
            research_tasks,
            parallel_status,
            merge_status,
        ),
        "evidence_counts": _evidence_counts(evidence, visual_acquisition, visual_provider, image_fetch_status),
        "budget": _budget_summary(budget_estimate, evidence),
        "visual": _visual_summary(visual_acquisition, visual_provider, image_fetch_status),
        "trace": _trace_summary(run_trace),
        "artifacts": _artifact_summary(run_dir, payloads),
        "artifact_errors": errors,
    }


def render_run_list(payload: Mapping[str, Any]) -> str:
    """Render a compact terminal run list."""

    runs = _list(payload.get("runs"))
    lines = [
        "Codex DeepResearch Run Monitor",
        f"Runs dir: {payload.get('runs_dir') or '<unknown>'}",
        f"Runs: {len(runs)}",
    ]
    if not runs:
        lines.append("No run artifacts found.")
        return "\n".join(lines)

    header = (
        "RUN ID".ljust(28)
        + " "
        + "PHASE".ljust(24)
        + " "
        + "MODE".ljust(16)
        + " "
        + "SHARDS q/a/c/f/ac/m/r/b".ljust(26)
        + " "
        + "SRC IMG"
        + " "
        + "BUDGET"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for run in runs:
        phase = _mapping(run.get("phase"))
        mode = _mapping(run.get("mode"))
        shards = _mapping(run.get("shards"))
        counts = _mapping(run.get("evidence_counts"))
        budget = _mapping(run.get("budget"))
        budget_label = _budget_label(budget)
        shard_label = (
            f"{_int(shards.get('queued'))}/"
            f"{_int(shards.get('active'))}/"
            f"{_int(shards.get('completed'))}/"
            f"{_int(shards.get('failed'))}/"
            f"{_int(shards.get('accepted'))}/"
            f"{_int(shards.get('merged'))}/"
            f"{_int(shards.get('retried'))}/"
            f"{_int(shards.get('blocked'))}"
        )
        lines.append(
            f"{_clip(str(run.get('run_id') or '<unknown>'), 28).ljust(28)} "
            f"{_clip(_phase_label(phase), 24).ljust(24)} "
            f"{_clip(str(mode.get('label') or 'unknown'), 16).ljust(16)} "
            f"{shard_label.ljust(26)} "
            f"{_int(counts.get('sources')):>3} {_int(counts.get('images')):>3} "
            f"{_clip(budget_label, 48)}"
        )
    return "\n".join(lines)


def render_run_detail(payload: Mapping[str, Any]) -> str:
    """Render one run monitor detail view."""

    phase = _mapping(payload.get("phase"))
    control = _mapping(payload.get("control"))
    mode = _mapping(payload.get("mode"))
    shards = _mapping(payload.get("shards"))
    partial_parallel = _mapping(payload.get("partial_parallel"))
    child_attempt_probes = _mapping(payload.get("child_attempt_probes"))
    counts = _mapping(payload.get("evidence_counts"))
    budget = _mapping(payload.get("budget"))
    visual = _mapping(payload.get("visual"))
    trace = _mapping(payload.get("trace"))
    artifacts = _mapping(payload.get("artifacts"))
    errors = _list(payload.get("artifact_errors"))

    lines = [
        "Codex DeepResearch Run Detail",
        f"Run: {payload.get('run_id') or '<unknown>'}",
        f"Run directory: {payload.get('run_dir') or '<unknown>'}",
        f"Status: {payload.get('status') or '<unknown>'}; ok={_display_bool(payload.get('ok'))}; terminal={_display_bool(payload.get('terminal'))}",
        f"Phase: {_phase_label(phase)}; next_safe_stage={phase.get('next_safe_stage') or '<none>'}",
        _control_detail_line(control),
        f"Mode: {mode.get('label') or 'unknown'}; adapter={mode.get('adapter') or '<none>'}; degraded={_display_bool(mode.get('parallel_degraded'))}; serial_handoff={_display_bool(mode.get('needs_serial_handoff'))}",
        (
            "Shards: "
            f"queued={_int(shards.get('queued'))} "
            f"active={_int(shards.get('active'))} "
            f"completed={_int(shards.get('completed'))} "
            f"failed={_int(shards.get('failed'))} "
            f"accepted={_int(shards.get('accepted'))} "
            f"merged={_int(shards.get('merged'))} "
            f"retried={_int(shards.get('retried'))} "
            f"blocked={_int(shards.get('blocked'))} "
            f"planned={_display_optional_int(shards.get('planned'))}"
        ),
        (
            "Partial parallel: "
            f"partial={_display_bool(partial_parallel.get('partial'))} "
            f"reason={partial_parallel.get('reason_category') or 'none'} "
            f"accepted={_int(partial_parallel.get('accepted_shard_count'))} "
            f"omitted={_int(partial_parallel.get('omitted_task_count'))} "
            f"rejected={_int(partial_parallel.get('rejected_shard_count'))} "
            f"failed={_int(partial_parallel.get('failed_task_count'))} "
            f"blocked={_int(partial_parallel.get('blocked_task_count'))} "
            f"final_artifact_gate_passed={_display_bool(partial_parallel.get('final_artifact_gate_passed'))}"
        ),
        (
            "Child probes: "
            f"count={_int(child_attempt_probes.get('count'))} "
            f"timeouts={_int(child_attempt_probes.get('timeout_count'))} "
            f"schema_invalid={_int(child_attempt_probes.get('schema_invalid_count'))} "
            f"recoverable_valid={_int(child_attempt_probes.get('recoverable_valid_shard_count'))}"
        ),
        f"Evidence: sources={_int(counts.get('sources'))}; images={_int(counts.get('images'))}; claims={_int(counts.get('claims'))}",
        _budget_detail_line(budget),
        (
            "Visual: "
            f"status={visual.get('status') or '<none>'}; "
            f"providers={', '.join(_string_list(visual.get('providers'))) or '<none>'}; "
            f"fetches={_int(visual.get('image_fetch_records'))}"
        ),
        (
            "Trace: "
            f"records={_int(trace.get('records'))}; "
            f"last_stage={trace.get('last_stage') or '<none>'}; "
            f"last_status={trace.get('last_status') or '<none>'}"
        ),
        "Artifacts:",
    ]
    for key in sorted(artifacts):
        lines.append(f"  {key}: {artifacts[key]}")
    if errors:
        lines.append("Artifact read errors:")
        for error in errors:
            if isinstance(error, Mapping):
                lines.append(f"  {error.get('artifact')}: {error.get('error')}")
    return "\n".join(lines)


def _load_artifacts(run_dir: Path) -> dict[str, Any]:
    payloads: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    for key, filename in _KNOWN_ARTIFACTS.items():
        path = run_dir / filename
        if not path.exists():
            continue
        if filename.endswith(".json"):
            payload, error = _read_json(path)
        elif filename.endswith(".jsonl"):
            payload, error = _read_jsonl(path)
        else:
            continue
        if error is not None:
            errors.append({"artifact": filename, "error": error})
            continue
        payloads[key] = payload
    return {"payloads": payloads, "errors": errors}


def _read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, exc.__class__.__name__


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if isinstance(record, dict):
                records.append(record)
        return records, None
    except (OSError, json.JSONDecodeError) as exc:
        return [], exc.__class__.__name__


def _looks_like_run_dir(path: Path) -> bool:
    return any((path / filename).exists() for filename in _KNOWN_ARTIFACTS.values())


def _run_id(run_dir: Path, *payloads: Mapping[str, Any]) -> str:
    for payload in payloads:
        value = payload.get("run_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return run_dir.name


def _run_status(*payloads: Mapping[str, Any]) -> str:
    for payload in payloads:
        value = payload.get("status")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _monitor_status(
    run_status: Mapping[str, Any],
    planning_status: Mapping[str, Any],
    parallel_status: Mapping[str, Any],
    run_control: Mapping[str, Any],
    phase: Mapping[str, Any],
    payloads: Mapping[str, Any],
) -> str:
    terminal_status = _terminal_run_status(run_status)
    if terminal_status is not None:
        return terminal_status
    control_status = _status_text(run_control.get("status"))
    if control_status in {"paused", "cancelled"}:
        return control_status

    phase_payload = _phase_status_payload(phase, payloads)
    status = _status_text(phase_payload.get("status"))
    if status is not None:
        return status

    if phase.get("source") != "status":
        status = _status_text(phase.get("status"))
        if status is not None:
            return status

    return _run_status(parallel_status, run_status, planning_status)


def _monitor_ok(
    run_status: Mapping[str, Any],
    parallel_status: Mapping[str, Any],
    phase: Mapping[str, Any],
    payloads: Mapping[str, Any],
) -> bool | None:
    phase_payload = _phase_status_payload(phase, payloads)
    if run_status.get("terminal") is True:
        return _first_present_bool(run_status.get("ok"), phase_payload.get("ok"), parallel_status.get("ok"))
    return _first_present_bool(
        phase_payload.get("ok"),
        parallel_status.get("ok"),
        run_status.get("ok"),
    )


def _monitor_terminal(
    run_status: Mapping[str, Any],
    run_control: Mapping[str, Any],
    phase: Mapping[str, Any],
    payloads: Mapping[str, Any],
) -> bool | None:
    if _terminal_run_status(run_status) is not None:
        return True
    if run_control.get("status") == "cancelled" or run_control.get("terminal") is True:
        return True
    if run_control.get("status") == "paused":
        return False
    phase_payload = _phase_status_payload(phase, payloads)
    terminal = _first_present_bool(phase_payload.get("terminal"))
    if terminal is not None:
        return terminal
    if phase.get("next_safe_stage") is None and phase.get("status") in {
        "completed",
        "failed",
        "skipped",
    }:
        return True
    terminal = _first_present_bool(run_status.get("terminal"))
    if terminal is not None:
        return terminal
    return None


def _phase_status_payload(
    phase: Mapping[str, Any],
    payloads: Mapping[str, Any],
) -> dict[str, Any]:
    stage = str(phase.get("stage") or "")
    artifact_key = _STAGE_STATUS_ARTIFACTS.get(stage)
    if artifact_key:
        payload = _mapping(payloads.get(artifact_key))
        if payload:
            return payload
    if stage == "completed":
        for key in _TERMINAL_STATUS_ARTIFACTS:
            payload = _mapping(payloads.get(key))
            if _status_text(payload.get("status")) is not None:
                return payload
    return {}


def _status_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _terminal_run_status(run_status: Mapping[str, Any]) -> str | None:
    status = _status_text(run_status.get("status"))
    if run_status.get("terminal") is True:
        return status or "terminal"
    if status and status.startswith(("completed", "cancelled", "failed")):
        return status
    return None


def _control_payload(
    payloads: Mapping[str, Any],
    run_steps: Mapping[str, Any],
) -> dict[str, Any]:
    artifact_payload = _mapping(payloads.get("run_control"))
    if artifact_payload:
        return artifact_payload
    return _mapping(run_steps.get("control"))


def _control_summary(
    run_control: Mapping[str, Any],
    run_steps: Mapping[str, Any],
) -> dict[str, Any]:
    history = _list(run_control.get("history"))
    if not history:
        history = _list(run_steps.get("control_history"))
    diagnostics = _mapping(run_control.get("diagnostics"))
    close_records = _list(diagnostics.get("child_context_close_records"))
    known_contexts = _list(run_control.get("known_child_contexts"))
    status = _status_text(run_control.get("status"))
    return {
        "status": status,
        "action": _status_text(run_control.get("action")),
        "terminal": _first_present_bool(run_control.get("terminal")),
        "requested_at": _status_text(run_control.get("requested_at")),
        "requested_by": _status_text(run_control.get("requested_by")),
        "reason": _status_text(run_control.get("reason")),
        "resume_next_safe_stage": _status_text(
            run_control.get("resume_next_safe_stage")
            or run_control.get("next_safe_stage")
        ),
        "history_count": len(history),
        "known_child_contexts": len(known_contexts),
        "child_close_records": len(close_records),
        "actionable_cause": _status_text(diagnostics.get("actionable_cause")),
    }


def _phase_summary(
    run_steps: Mapping[str, Any],
    run_status: Mapping[str, Any],
    planning_status: Mapping[str, Any],
    run_control: Mapping[str, Any],
    run_trace: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    control_status = _status_text(run_control.get("status"))
    if control_status in {"cancelled", "paused"}:
        terminal_phase = _terminal_run_status_phase(run_status)
        if terminal_phase is not None:
            return terminal_phase
    if control_status == "cancelled":
        return {
            "stage": "cancelled",
            "status": "cancelled",
            "next_safe_stage": None,
            "source": "run_control",
        }
    if control_status == "paused":
        next_stage = run_steps.get("next_safe_stage") or run_control.get("resume_next_safe_stage")
        return {
            "stage": "paused",
            "status": str(next_stage or "paused"),
            "next_safe_stage": next_stage,
            "next_stage_retryable": bool(run_steps.get("next_stage_retryable")),
            "source": "run_control",
        }
    stages = run_steps.get("stages")
    if isinstance(stages, Mapping):
        running = _first_stage_with_status(stages, {"running"})
        if running:
            return _phase_record(running, stages, run_steps)
        failed = _first_stage_with_status(stages, {"failed"})
        if failed:
            return _phase_record(failed, stages, run_steps)
        next_stage = run_steps.get("next_safe_stage")
        if isinstance(next_stage, str) and next_stage in stages:
            return _phase_record(next_stage, stages, run_steps)
        if run_steps.get("status") == "completed":
            return {
                "stage": "completed",
                "status": "completed",
                "next_safe_stage": None,
                "source": "run_steps",
            }

    if run_trace:
        last = run_trace[-1]
        stage = last.get("stage")
        if isinstance(stage, str) and stage:
            return {
                "stage": stage,
                "status": str(last.get("status") or "unknown"),
                "next_safe_stage": None,
                "source": "run_trace",
            }

    status = run_status.get("status") or planning_status.get("status") or "unknown"
    return {
        "stage": "run_status" if run_status else "planning",
        "status": str(status),
        "next_safe_stage": None,
        "source": "status",
    }


def _terminal_run_status_phase(run_status: Mapping[str, Any]) -> dict[str, Any] | None:
    status = _terminal_run_status(run_status)
    if status is None:
        return None
    stage = "run_status"
    if status.startswith("completed"):
        stage = "completed"
    elif status.startswith("cancelled"):
        stage = "cancelled"
    elif status.startswith("failed"):
        stage = "failed"
    return {
        "stage": stage,
        "status": status,
        "next_safe_stage": None,
        "source": "run_status",
    }


def _phase_record(
    stage: str,
    stages: Mapping[str, Any],
    run_steps: Mapping[str, Any],
) -> dict[str, Any]:
    record = _mapping(stages.get(stage))
    return {
        "stage": stage,
        "status": str(record.get("status") or "unknown"),
        "stage_status": record.get("stage_status"),
        "next_safe_stage": run_steps.get("next_safe_stage"),
        "next_stage_retryable": bool(run_steps.get("next_stage_retryable")),
        "source": "run_steps",
    }


def _first_stage_with_status(stages: Mapping[str, Any], statuses: set[str]) -> str | None:
    for stage, record in stages.items():
        if isinstance(record, Mapping) and record.get("status") in statuses:
            return str(stage)
    return None


def _mode_summary(
    run_status: Mapping[str, Any],
    evidence: Mapping[str, Any],
    parallel_status: Mapping[str, Any],
    merge_status: Mapping[str, Any],
) -> dict[str, Any]:
    provenance = _mapping(run_status.get("provenance"))
    evidence_source = _mapping(parallel_status.get("evidence_source")) or _mapping(
        merge_status.get("evidence_source")
    )
    fallback = _mapping(run_status.get("fallback"))
    run_parallel = _mapping(run_status.get("parallel"))
    use_run_provenance = run_status.get("terminal") is True or not (parallel_status or merge_status)
    selected_mode = str(run_status.get("selected_mode") or "")
    adapter = str(
        parallel_status.get("adapter")
        or evidence_source.get("adapter")
        or (provenance.get("adapter") if use_run_provenance else "")
        or ""
    )
    parallel_degraded = _bool(parallel_status.get("parallel_degraded")) or _bool(
        merge_status.get("parallel_degraded")
    ) or _bool(
        fallback.get("parallel_degraded")
    ) or _bool(
        run_parallel.get("parallel_degraded")
    )
    needs_serial_handoff = (
        _bool(parallel_status.get("needs_serial_handoff"))
        or _bool(fallback.get("needs_serial_handoff"))
        or _bool(run_parallel.get("needs_serial_handoff"))
    )
    source_type = str(
        evidence_source.get("type")
        or (provenance.get("type") if use_run_provenance else "")
        or ""
    )
    parallel_status_value = str(parallel_status.get("status") or run_status.get("status") or "")

    if use_run_provenance and (
        selected_mode == "manual-handoff" or provenance.get("manual_handoff") is True
    ):
        kind = "manual"
        label = "manual"
    elif source_type == "manual_handoff" or evidence.get("search_provider") == "manual":
        kind = "manual"
        label = "manual"
    elif (
        source_type == "fixture"
        or (use_run_provenance and provenance.get("fixture_only") is True)
        or adapter == "fixture"
    ):
        kind = "fixture"
        label = "fixture"
    elif (
        source_type == "real_child_execution"
        or (use_run_provenance and provenance.get("real_child_execution") is True)
        or parallel_status.get("status") in {"completed_parallel", "completed_partial_parallel"}
    ):
        kind = "real_parallel"
        label = "real parallel"
    elif (
        source_type == "failed_real_child_execution"
        or (
            adapter == "codex-exec"
            and parallel_status_value.startswith("failed")
            and not parallel_degraded
        )
    ):
        kind = "failed_real_parallel"
        label = "failed real parallel"
    elif (
        source_type == "blocked_parallel_execution"
        or (
            adapter == "codex-exec"
            and parallel_status_value.startswith("blocked")
            and not parallel_degraded
        )
    ):
        kind = "blocked_real_parallel"
        label = "blocked real parallel"
    elif parallel_degraded:
        kind = "degraded_parallel"
        label = "degraded parallel"
    elif (
        source_type == "serial_handoff"
        or adapter == "serial-degraded"
    ):
        kind = "serial_fallback"
        label = "serial fallback"
    elif needs_serial_handoff:
        kind = "degraded_parallel"
        label = "degraded parallel"
    elif parallel_status:
        kind = "parallel_pending"
        label = "parallel pending"
    else:
        kind = "prepared"
        label = "prepared"

    return {
        "kind": kind,
        "label": label,
        "adapter": adapter or None,
        "source_type": source_type or None,
        "parallel_degraded": parallel_degraded,
        "needs_serial_handoff": needs_serial_handoff,
        "degraded_reason": (
            parallel_status.get("degraded_reason")
            or merge_status.get("degraded_reason")
            or fallback.get("degraded_reason")
            or run_parallel.get("degraded_reason")
        ),
    }


def _shard_summary(
    research_tasks: Mapping[str, Any],
    parallel_status: Mapping[str, Any],
    merge_status: Mapping[str, Any],
    subagent_assignments: Sequence[Mapping[str, Any]],
    run_trace: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    tasks = [task for task in _list(research_tasks.get("tasks")) if isinstance(task, Mapping)]
    task_records = _overlay_live_task_records(tasks, subagent_assignments, run_trace)
    counts = {
        "queued": 0,
        "active": 0,
        "completed": 0,
        "failed": 0,
        "accepted": 0,
        "merged": 0,
        "retried": 0,
        "blocked": 0,
    }
    for task in task_records:
        state = str(task.get("state") or "")
        if state == "queued":
            counts["queued"] += 1
        elif state in {"assigned", "running", "active"}:
            counts["active"] += 1
        elif state == "completed":
            counts["completed"] += 1
        elif state == "failed":
            counts["failed"] += 1
        elif state == "blocked":
            counts["blocked"] += 1
        elif state == "merged":
            counts["merged"] += 1
        elif state in {"retryable", "retried"}:
            counts["retried"] += 1
        if _int(task.get("attempt")) > 1:
            counts["retried"] += 1

    parallel_merge = _mapping(parallel_status.get("merge"))
    accepted = len(_list(merge_status.get("accepted_shards")))
    if accepted == 0:
        accepted = len(_list(parallel_merge.get("accepted_shards")))
    counts["accepted"] = accepted
    counts["merged"] = max(counts["merged"], accepted)
    failure_counts = _mapping(parallel_status.get("failure_counts")) or _mapping(
        merge_status.get("failure_counts")
    )
    counts["failed"] = max(counts["failed"], _int(failure_counts.get("failed_tasks")))
    counts["blocked"] = max(counts["blocked"], _int(failure_counts.get("blocked_tasks")))
    counts["planned"] = _first_int(
        parallel_status.get("planned_task_count"),
        research_tasks.get("task_count"),
        len(tasks) if tasks else None,
    )
    counts["runnable"] = _first_int(parallel_status.get("runnable_task_count"))
    return counts


def _partial_parallel_summary(
    run_status: Mapping[str, Any],
    parallel_status: Mapping[str, Any],
    merge_status: Mapping[str, Any],
) -> dict[str, Any]:
    source = _mapping(parallel_status.get("partial_parallel_summary")) or _mapping(
        merge_status.get("partial_parallel_summary")
    )
    failure_counts = _mapping(parallel_status.get("failure_counts")) or _mapping(
        merge_status.get("failure_counts")
    )
    retry_summary = _mapping(parallel_status.get("retry_summary")) or _mapping(
        merge_status.get("retry_summary")
    )
    planned = _first_int(
        source.get("planned_task_count"),
        parallel_status.get("planned_task_count"),
        merge_status.get("planned_task_count"),
    ) or 0
    accepted = _first_int(
        source.get("accepted_shard_count"),
        parallel_status.get("accepted_shard_count"),
        merge_status.get("accepted_shard_count"),
        len(_list(merge_status.get("accepted_shards")))
        if _list(merge_status.get("accepted_shards"))
        else None,
        _mapping(parallel_status.get("evidence_source")).get("accepted_shards"),
        _mapping(merge_status.get("evidence_source")).get("accepted_shards"),
    ) or 0
    omitted = _first_int(
        source.get("omitted_task_count"),
        max(0, planned - accepted) if planned else None,
    ) or 0
    failed = _first_int(source.get("failed_task_count"), failure_counts.get("failed_tasks")) or 0
    blocked = _first_int(source.get("blocked_task_count"), failure_counts.get("blocked_tasks")) or 0
    rejected = _first_int(source.get("rejected_shard_count"), failure_counts.get("rejected_shards")) or 0
    discarded = _first_int(source.get("discarded_task_count"), failure_counts.get("discarded_tasks")) or 0
    retried = _first_int(source.get("retried_task_count"), retry_summary.get("retry_count")) or 0
    retry_exhausted = _first_int(
        source.get("retry_exhausted_task_count"),
        retry_summary.get("retry_exhausted_count"),
    ) or 0
    partial = _first_present_bool(source.get("partial"))
    if partial is None:
        status = str(parallel_status.get("status") or run_status.get("status") or "")
        partial = (
            status == "completed_partial_parallel"
            or (accepted > 0 and omitted > 0)
            or any(count > 0 for count in (failed, blocked, rejected, discarded))
            or _bool(parallel_status.get("parallel_degraded"))
            or _bool(merge_status.get("parallel_degraded"))
        )
    reason = str(
        source.get("reason_category")
        or parallel_status.get("partial_reason_category")
        or merge_status.get("partial_reason_category")
        or ""
    )
    if not reason:
        reason = _partial_reason_from_counts(
            partial=partial,
            accepted=accepted,
            failed=failed,
            blocked=blocked,
            rejected=rejected,
            discarded=discarded,
            retry_exhausted=retry_exhausted,
            omitted=omitted,
            parallel_degraded=_bool(parallel_status.get("parallel_degraded"))
            or _bool(merge_status.get("parallel_degraded")),
        )
    final_gate = (
        run_status.get("terminal") is True
        and run_status.get("ok") is True
        and str(run_status.get("status") or "").startswith("completed")
    )
    return {
        "partial": partial,
        "reason_category": reason,
        "planned_task_count": planned,
        "accepted_shard_count": accepted,
        "omitted_task_count": omitted,
        "failed_task_count": failed,
        "blocked_task_count": blocked,
        "rejected_shard_count": rejected,
        "discarded_task_count": discarded,
        "retried_task_count": retried,
        "retry_exhausted_task_count": retry_exhausted,
        "final_artifact_gate_passed": final_gate,
    }


def _partial_reason_from_counts(
    *,
    partial: bool,
    accepted: int,
    failed: int,
    blocked: int,
    rejected: int,
    discarded: int,
    retry_exhausted: int,
    omitted: int,
    parallel_degraded: bool,
) -> str:
    if not partial:
        return "none"
    if accepted == 0:
        return "no_accepted_shards"
    if retry_exhausted > 0:
        return "retry_exhausted"
    if failed > 0:
        return "failed_tasks"
    if blocked > 0:
        return "blocked_tasks"
    if rejected > 0:
        return "rejected_shards"
    if discarded > 0:
        return "discarded_tasks"
    if parallel_degraded:
        return "parallel_degraded"
    if omitted > 0:
        return "omitted_tasks"
    return "partial_unknown"


def _child_attempt_probe_summary(
    research_tasks: Mapping[str, Any],
    parallel_status: Mapping[str, Any],
    merge_status: Mapping[str, Any],
) -> dict[str, Any]:
    probes = _collect_child_attempt_probes(research_tasks, parallel_status, merge_status)
    timeout_count = sum(1 for probe in probes if probe.get("timeout") is True)
    schema_invalid_count = sum(
        1
        for probe in probes
        if probe.get("child_failure_code") == "codex_child_schema_invalid"
    )
    recoverable_count = sum(
        1
        for probe in probes
        if probe.get("runner_recoverable_valid_shard") is True
    )
    return {
        "count": len(probes),
        "timeout_count": timeout_count,
        "schema_invalid_count": schema_invalid_count,
        "recoverable_valid_shard_count": recoverable_count,
        "probes": [_summarize_child_attempt_probe(probe) for probe in probes[:8]],
    }


def _collect_child_attempt_probes(
    research_tasks: Mapping[str, Any],
    parallel_status: Mapping[str, Any],
    merge_status: Mapping[str, Any],
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    for task in _list(research_tasks.get("tasks")):
        if isinstance(task, Mapping):
            probes.extend(_attempt_probes_from_record(task))
    parallel_merge = _mapping(parallel_status.get("merge"))
    for payload in (merge_status, parallel_merge):
        for key in ("failed_tasks", "blocked_tasks", "discarded_tasks"):
            for task in _list(payload.get(key)):
                if isinstance(task, Mapping):
                    probes.extend(_attempt_probes_from_record(task))
        for shard in _list(payload.get("accepted_shards")):
            if not isinstance(shard, Mapping):
                continue
            diagnostics = shard.get("diagnostics")
            if isinstance(diagnostics, Mapping):
                probes.extend(_attempt_probes_from_record(diagnostics))
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str | None]] = set()
    for probe in probes:
        key = (
            str(probe.get("task_id") or ""),
            _int(probe.get("attempt")),
            probe.get("child_failure_code") if isinstance(probe.get("child_failure_code"), str) else None,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(probe)
    return deduped


def _attempt_probes_from_record(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    attempts = record.get("attempt_diagnostics")
    if not isinstance(attempts, list):
        return []
    probes: list[dict[str, Any]] = []
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        probe = attempt.get("attempt_probe")
        if isinstance(probe, Mapping):
            probes.append(dict(probe))
    return probes


def _summarize_child_attempt_probe(probe: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_id": probe.get("task_id"),
        "attempt": probe.get("attempt"),
        "child_failure_code": probe.get("child_failure_code"),
        "timeout": probe.get("timeout"),
        "child_timeout_at": probe.get("child_timeout_at"),
        "child_elapsed_seconds": probe.get("child_elapsed_seconds"),
        "elapsed_seconds": probe.get("elapsed_seconds"),
        "timeout_seconds": probe.get("timeout_seconds"),
        "shard_exists_at_timeout": probe.get("shard_exists_at_timeout"),
        "shard_exists": probe.get("shard_exists"),
        "parent_probe_after_timeout": probe.get("parent_probe_after_timeout"),
        "parent_probe_observed_shard_at": probe.get("parent_probe_observed_shard_at"),
        "parent_probe_validation_attempt_at": probe.get("parent_probe_validation_attempt_at"),
        "parent_probe_validated_shard_at": probe.get("parent_probe_validated_shard_at"),
        "shard_schema_version": probe.get("shard_schema_version"),
        "shard_parent_valid": probe.get("shard_parent_valid"),
        "top_level_missing_fields": probe.get("top_level_missing_fields"),
        "runner_recoverable_valid_shard": probe.get("runner_recoverable_valid_shard"),
        "runner_recoverability": probe.get("runner_recoverability"),
        "last_validation_result": probe.get("last_validation_result"),
        "last_child_event": probe.get("last_child_event"),
        "last_child_event_type": probe.get("last_child_event_type"),
        "last_tool_or_command_call": probe.get("last_tool_or_command_call"),
        "last_tool_or_command_kind": probe.get("last_tool_or_command_kind"),
        "last_tool_or_command_preview": probe.get("last_tool_or_command_preview"),
        "last_child_message_preview": probe.get("last_child_message_preview"),
        "last_message_text_preview": probe.get("last_message_text_preview"),
        "candidate_cause_confidence": probe.get("candidate_cause_confidence"),
        "candidate_cause_basis": probe.get("candidate_cause_basis"),
        "candidate_causes": list(probe.get("candidate_causes") or [])[:3]
        if isinstance(probe.get("candidate_causes"), list)
        else [],
        "unknowns": list(probe.get("unknowns") or [])[:5]
        if isinstance(probe.get("unknowns"), list)
        else [],
    }


def _overlay_live_task_records(
    tasks: Sequence[Mapping[str, Any]],
    subagent_assignments: Sequence[Mapping[str, Any]],
    run_trace: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for task in tasks:
        task_id = _task_id(task)
        if task_id is None:
            continue
        records[task_id] = dict(task)
        order.append(task_id)

    for task_id, live in _live_task_states(subagent_assignments, run_trace).items():
        current = records.get(task_id)
        if current is None:
            records[task_id] = {"id": task_id, **live}
            order.append(task_id)
            continue
        if current.get("state") in {"merged", "discarded"}:
            continue
        current.update(live)
        current["id"] = task_id

    return [records[task_id] for task_id in order]


def _live_task_states(
    subagent_assignments: Sequence[Mapping[str, Any]],
    run_trace: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for assignment in subagent_assignments:
        task_id = _task_id(assignment)
        if task_id is None:
            continue
        attempt = _record_attempt(assignment)
        observed_at = _timestamp_key(assignment.get("timestamp"))
        current = states.get(task_id)
        if current is not None and not _is_newer_live_record(
            attempt=attempt,
            observed_at=observed_at,
            current=current,
        ):
            continue
        states[task_id] = {
            "state": "assigned",
            "attempt": max(_int(current.get("attempt")) if current else 0, attempt),
            "_observed_at": observed_at,
        }

    for event in run_trace:
        task_id = _task_id(event)
        if task_id is None:
            continue
        state = _live_state_from_trace(event)
        if state is None:
            continue
        attempt = _record_attempt(event)
        observed_at = _timestamp_key(event.get("timestamp"))
        record = states.setdefault(task_id, {"state": state, "attempt": 0, "_observed_at": ""})
        if not _is_newer_live_record(
            attempt=attempt,
            observed_at=observed_at,
            current=record,
        ):
            continue
        record["state"] = state
        record["attempt"] = max(_int(record.get("attempt")), attempt)
        if observed_at:
            record["_observed_at"] = observed_at
    return states


def _is_newer_live_record(
    *,
    attempt: int,
    observed_at: str,
    current: Mapping[str, Any],
) -> bool:
    current_attempt = _int(current.get("attempt"))
    if attempt and current_attempt and attempt < current_attempt:
        return False
    if attempt and current_attempt and attempt > current_attempt:
        return True
    current_observed_at = str(current.get("_observed_at") or "")
    if observed_at and current_observed_at and observed_at < current_observed_at:
        return False
    return True


def _record_attempt(record: Mapping[str, Any]) -> int:
    explicit = _int(record.get("attempt"))
    if explicit:
        return explicit
    for key in ("assigned_subagent_id", "child_thread_id"):
        value = record.get(key)
        if isinstance(value, str):
            parsed = _attempt_from_identifier(value)
            if parsed:
                return parsed
    return 0


def _attempt_from_identifier(value: str) -> int:
    marker = "-attempt-"
    if marker not in value:
        return 0
    suffix = value.rsplit(marker, 1)[1]
    digits = []
    for char in suffix:
        if not char.isdigit():
            break
        digits.append(char)
    return int("".join(digits)) if digits else 0


def _timestamp_key(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _live_state_from_trace(event: Mapping[str, Any]) -> str | None:
    status = str(event.get("child_status") or event.get("status") or "").strip()
    if status in {"assigned", "running", "active"}:
        return "running"
    if status == "failed":
        return "failed"
    if status == "blocked":
        return "blocked"
    if status in {"completed", "closed"}:
        return "completed"
    return None


def _task_id(record: Mapping[str, Any]) -> str | None:
    value = record.get("task_id") or record.get("id")
    if isinstance(value, str) and value.strip() and value != "unknown":
        return value.strip()
    return None


def _evidence_counts(
    evidence: Mapping[str, Any],
    visual_acquisition: Mapping[str, Any],
    visual_provider: Mapping[str, Any],
    image_fetch_status: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    return {
        "sources": len(_list(evidence.get("sources"))),
        "images": len(_list(evidence.get("images"))),
        "claims": len(_list(evidence.get("claims"))),
        "visual_candidates": _int(visual_acquisition.get("candidate_records")),
        "selected_visual_observations": _int(visual_acquisition.get("selected_observations")),
        "visual_providers": len(_list(visual_provider.get("providers"))),
        "image_fetch_records": len(image_fetch_status),
    }


def _budget_summary(
    budget_estimate: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    evidence_budget = _mapping(evidence.get("budget"))
    confirmation = _mapping(budget_estimate.get("confirmation"))
    effective_caps = _mapping(budget_estimate.get("effective_caps"))
    estimates = _mapping(budget_estimate.get("estimates"))
    cost_bounds = _mapping(budget_estimate.get("high_water_cost_bounds"))
    preset = str(
        budget_estimate.get("budget_preset")
        or evidence_budget.get("preset")
        or evidence_budget.get("budget_preset")
        or "unknown"
    )
    return {
        "preset": preset,
        "confirmation_required": _maybe_bool(confirmation.get("required")),
        "confirmation_provided": _maybe_bool(confirmation.get("provided")),
        "max_concurrent_codex_subagents": _first_int(
            effective_caps.get("max_concurrent_codex_subagents"),
            evidence_budget.get("max_concurrent_codex_subagents"),
        ),
        "codex_subagent_count": _first_int(estimates.get("codex_subagent_count")),
        "max_cost_usd": _first_number(
            cost_bounds.get("max_cost_usd"),
            effective_caps.get("max_cost_usd"),
            evidence_budget.get("max_cost_usd"),
        ),
        "high_water_cost_usd": _first_number(cost_bounds.get("upper_bound_usd")),
        "within_max_cost": _maybe_bool(cost_bounds.get("within_max_cost")),
        "source": "budget_estimate" if budget_estimate else ("evidence" if evidence_budget else "missing"),
    }


def _visual_summary(
    visual_acquisition: Mapping[str, Any],
    visual_provider: Mapping[str, Any],
    image_fetch_status: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    providers = [
        str(provider.get("provider"))
        for provider in _list(visual_provider.get("providers"))
        if isinstance(provider, Mapping) and provider.get("provider")
    ]
    fetch_counts: dict[str, int] = {}
    for record in image_fetch_status:
        status = str(record.get("fetch_status") or record.get("status") or "unknown")
        fetch_counts[status] = fetch_counts.get(status, 0) + 1
    return {
        "status": visual_provider.get("status") or visual_acquisition.get("status"),
        "providers": providers,
        "candidate_records": _int(visual_acquisition.get("candidate_records")),
        "selected_observations": _int(visual_acquisition.get("selected_observations")),
        "image_fetch_records": len(image_fetch_status),
        "image_fetch_counts": fetch_counts,
    }


def _trace_summary(run_trace: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not run_trace:
        return {"records": 0, "last_stage": None, "last_status": None, "last_event_type": None}
    last = run_trace[-1]
    return {
        "records": len(run_trace),
        "last_stage": last.get("stage"),
        "last_status": last.get("status"),
        "last_event_type": last.get("event_type"),
    }


def _artifact_summary(run_dir: Path, payloads: Mapping[str, Any]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for key, filename in _KNOWN_ARTIFACTS.items():
        if (run_dir / filename).exists():
            artifacts[key] = filename
    for payload in payloads.values():
        if not isinstance(payload, Mapping):
            continue
        raw_artifacts = payload.get("artifacts")
        if not isinstance(raw_artifacts, Mapping):
            continue
        for key, value in raw_artifacts.items():
            if isinstance(value, str):
                artifacts[str(key)] = _safe_artifact_path(run_dir, value)
    return artifacts


def _safe_artifact_path(run_dir: Path, value: str) -> str:
    path = Path(value)
    if not path.is_absolute() and ".." in path.parts:
        return "<outside-run-dir>"
    resolved_run_dir = run_dir.resolve()
    resolved_path = path.resolve() if path.is_absolute() else (resolved_run_dir / path).resolve()
    try:
        return resolved_path.relative_to(resolved_run_dir).as_posix()
    except (OSError, ValueError):
        return "<outside-run-dir>"


def _safe_runs_dir_label(path: Path) -> str:
    if not path.is_absolute():
        return str(path)
    return path.name or "<runs-dir>"


def _phase_label(phase: Mapping[str, Any]) -> str:
    stage = phase.get("stage") or "<unknown>"
    status = phase.get("status") or "unknown"
    return f"{stage}/{status}"


def _control_detail_line(control: Mapping[str, Any]) -> str:
    status = control.get("status") or "active"
    return (
        "Control: "
        f"status={status}; "
        f"action={control.get('action') or '<none>'}; "
        f"resume_next_safe_stage={control.get('resume_next_safe_stage') or '<none>'}; "
        f"child_contexts={_int(control.get('known_child_contexts'))}; "
        f"close_records={_int(control.get('child_close_records'))}"
    )


def _budget_label(budget: Mapping[str, Any]) -> str:
    preset = str(budget.get("preset") or "unknown")
    if preset not in {"deep", "exhaustive"}:
        return preset
    subagents = _display_optional_int(budget.get("max_concurrent_codex_subagents"))
    required = _display_bool(budget.get("confirmation_required"))
    provided = _display_bool(budget.get("confirmation_provided"))
    cap = _display_money(budget.get("max_cost_usd"))
    return f"{preset} sub={subagents} confirm={provided}/{required} cap={cap}"


def _budget_detail_line(budget: Mapping[str, Any]) -> str:
    return (
        "Budget: "
        f"preset={budget.get('preset') or 'unknown'}; "
        f"max_subagents={_display_optional_int(budget.get('max_concurrent_codex_subagents'))}; "
        f"codex_subagents={_display_optional_int(budget.get('codex_subagent_count'))}; "
        f"cost_cap={_display_money(budget.get('max_cost_usd'))}; "
        f"high_water={_display_money(budget.get('high_water_cost_usd'))}; "
        f"confirmation_required={_display_bool(budget.get('confirmation_required'))}; "
        f"confirmation_provided={_display_bool(budget.get('confirmation_provided'))}"
    )


def _display_bool(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _display_optional_int(value: Any) -> str:
    if value is None:
        return "unknown"
    return str(_int(value))


def _display_money(value: Any) -> str:
    number = _first_number(value)
    if number is None:
        return "unset"
    return f"{number:.2f} USD"


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return value[: max(0, limit - 3)] + "..."


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bool(value: Any) -> bool:
    return value is True


def _maybe_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _first_present_bool(*values: Any) -> bool | None:
    for value in values:
        if isinstance(value, bool):
            return value
    return None


def _first_int(*values: Any) -> int | None:
    for value in values:
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_number(*values: Any) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None
