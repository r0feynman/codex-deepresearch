"""Parallel Codex subagent orchestration artifacts and deterministic runner."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence_schema import EVIDENCE_SCHEMA_VERSION, validate_artifacts
from .execution_mode import BUDGET_PRESETS
from .search_handoff import SearchHandoffError, resolve_run_dir
from .trace import TRACE_SCHEMA_VERSION, append_trace_record, trace_path


PARALLEL_SCHEMA_VERSION = "codex-deepresearch.parallel-orchestration.v0"
RESEARCH_TASKS_FILENAME = "research_tasks.json"
ASSIGNMENTS_FILENAME = "subagent_assignments.jsonl"
MERGE_STATUS_FILENAME = "merge_status.json"
EVIDENCE_SHARDS_DIRNAME = "evidence_shards"
RETRY_SAFE_FAILURES = {"adapter_unavailable", "codex_exec_failed", "missing_shard"}
TASK_STATES = (
    "queued",
    "assigned",
    "running",
    "completed",
    "failed",
    "blocked",
    "retryable",
    "merged",
    "discarded",
)


class ParallelOrchestrationError(ValueError):
    """Raised when parallel orchestration cannot proceed."""


class AdapterUnavailable(RuntimeError):
    """Raised when a runner adapter cannot execute in the current environment."""


@dataclass(frozen=True)
class ResearchTask:
    id: str
    angle_id: str
    route: str
    query: str
    state: str
    assigned_subagent_id: str | None
    attempt: int
    max_attempts: int
    max_sources: int
    max_images: int
    source_policy: dict[str, Any]
    output_shard_path: str
    trace_event_ids: list[str]
    failure_category: str | None = None
    blocked_reason: str | None = None
    discard_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


@dataclass(frozen=True)
class RunnerResult:
    task_id: str
    status: str
    child_thread_id: str | None
    events: tuple[dict[str, Any], ...]
    shard_path: str | None = None
    failure_category: str | None = None
    message: str | None = None


class CodexExecAdapter:
    """Runner adapter that invokes Codex CLI JSON mode."""

    name = "codex-exec"

    def __init__(self, *, codex_binary: str = "codex", timeout_seconds: float = 300.0) -> None:
        self.codex_binary = codex_binary
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        return shutil.which(self.codex_binary) is not None

    def build_command(self, task: Mapping[str, Any], *, max_threads: int, run_dir: Path) -> list[str]:
        prompt = _child_prompt(task, run_dir=run_dir)
        return [
            self.codex_binary,
            "exec",
            "--json",
            "-c",
            f"agents.max_threads={max_threads}",
            prompt,
        ]

    def run_task(
        self,
        task: Mapping[str, Any],
        *,
        run_dir: Path,
        max_threads: int,
    ) -> RunnerResult:
        if not self.available():
            raise AdapterUnavailable("codex exec is not available on PATH")
        command = self.build_command(task, max_threads=max_threads, run_dir=run_dir)
        child_thread_id = f"codex-{task['id']}-{uuid.uuid4().hex[:8]}"
        events = [
            _codex_event(
                "spawn_agent",
                task,
                child_thread_id=child_thread_id,
                child_status="running",
                child_message="codex exec JSON runner started",
                raw_event={"command": command[:5] + ["<prompt>"]},
            )
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=run_dir,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            events.append(
                _codex_event(
                    "wait",
                    task,
                    child_thread_id=child_thread_id,
                    child_status="failed",
                    failure_category="codex_exec_failed",
                    child_message=exc.__class__.__name__,
                )
            )
            events.append(
                _codex_event(
                    "close_agent",
                    task,
                    child_thread_id=child_thread_id,
                    child_status="failed",
                )
            )
            return RunnerResult(
                task_id=str(task["id"]),
                status="failed",
                child_thread_id=child_thread_id,
                events=tuple(events),
                failure_category="codex_exec_failed",
                message=exc.__class__.__name__,
            )

        parsed_events = _parse_json_events(completed.stdout)
        events.extend(
            _codex_event(
                event.get("event", event.get("type", "message")),
                task,
                child_thread_id=str(event.get("thread_id") or child_thread_id),
                child_status=str(event.get("status") or "running"),
                child_message=_event_message(event),
                raw_event=event,
                failure_category=_event_failure(event),
            )
            for event in parsed_events
        )
        status = "completed" if completed.returncode == 0 else "failed"
        failure = None if status == "completed" else "codex_exec_failed"
        events.append(
            _codex_event(
                "wait",
                task,
                child_thread_id=child_thread_id,
                child_status=status,
                failure_category=failure,
                child_message=f"codex exec exited {completed.returncode}",
            )
        )
        events.append(
            _codex_event(
                "close_agent",
                task,
                child_thread_id=child_thread_id,
                child_status=status,
                failure_category=failure,
            )
        )
        shard_path = str(run_dir / str(task["output_shard_path"]))
        return RunnerResult(
            task_id=str(task["id"]),
            status=status,
            child_thread_id=child_thread_id,
            events=tuple(events),
            shard_path=shard_path if status == "completed" else None,
            failure_category=failure,
            message=completed.stderr.strip()[:500] or None,
        )


class FixtureAdapter:
    """Deterministic no-network adapter used by unit tests and CLI smoke tests."""

    name = "fixture"

    def run_task(
        self,
        task: Mapping[str, Any],
        *,
        run_dir: Path,
        max_threads: int,
    ) -> RunnerResult:
        child_thread_id = f"fixture-{task['id']}"
        shard_path = run_dir / str(task["output_shard_path"])
        _write_fixture_shard(run_dir, task, shard_path)
        events = (
            _codex_event(
                "spawn_agent",
                task,
                child_thread_id=child_thread_id,
                child_status="running",
                child_message="fixture worker started",
            ),
            _codex_event(
                "message",
                task,
                child_thread_id=child_thread_id,
                child_status="running",
                child_message=f"wrote {task['output_shard_path']}",
            ),
            _codex_event(
                "wait",
                task,
                child_thread_id=child_thread_id,
                child_status="completed",
                child_message="fixture worker completed",
            ),
            _codex_event(
                "close_agent",
                task,
                child_thread_id=child_thread_id,
                child_status="closed",
            ),
        )
        return RunnerResult(
            task_id=str(task["id"]),
            status="completed",
            child_thread_id=child_thread_id,
            events=events,
            shard_path=str(shard_path),
        )


def plan_research_tasks(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
    min_tasks: int = 1,
    max_tasks: int | None = None,
) -> dict[str, Any]:
    """Expand planner search tasks into bounded ResearchTask records."""

    run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    evidence = _read_json(run_dir / "evidence.json")
    budget = evidence.get("budget", {}) if isinstance(evidence.get("budget"), Mapping) else {}
    preset_name = str(budget.get("preset") or "standard")
    preset = BUDGET_PRESETS.get(preset_name, BUDGET_PRESETS["standard"])
    hard_task_cap = min(preset.max_codex_handoff_tasks, 100)
    requested_cap = max_tasks if max_tasks is not None else hard_task_cap
    task_count = min(max(min_tasks, 1), requested_cap, hard_task_cap)
    existing = _read_research_tasks(run_dir)
    if existing:
        return _tasks_payload(run_dir, existing, evidence=evidence, status="already_planned")

    planner_tasks = evidence.get("search_tasks")
    if not isinstance(planner_tasks, list) or not planner_tasks:
        raise ParallelOrchestrationError("evidence.json must include search_tasks before planning")

    now = _utc_now()
    tasks: list[dict[str, Any]] = []
    for index in range(1, task_count + 1):
        base = planner_tasks[(index - 1) % len(planner_tasks)]
        if not isinstance(base, Mapping):
            continue
        task_id = f"task_research_{index:03d}"
        route = str(base.get("route") or "text_only")
        max_images = int(base.get("max_images") or 0)
        query = _bounded_task_query(str(base.get("query") or evidence.get("question") or ""), index)
        tasks.append(
            ResearchTask(
                id=task_id,
                angle_id=str(base.get("angle_id") or f"angle_{index:03d}"),
                route=route,
                query=query,
                state="queued",
                assigned_subagent_id=None,
                attempt=0,
                max_attempts=2,
                max_sources=max(1, int(base.get("max_results") or 3)),
                max_images=max_images if route != "text_only" else 0,
                source_policy={"decision": "allowed", "flags": []},
                output_shard_path=f"{EVIDENCE_SHARDS_DIRNAME}/{task_id}/evidence_shard.json",
                trace_event_ids=[],
            ).to_dict()
        )
    payload = {
        "schema_version": PARALLEL_SCHEMA_VERSION,
        "run_id": str(evidence.get("run_id") or run_dir.name),
        "created_at": now,
        "status": "planned",
        "parallel_degraded": False,
        "budget_preset": preset_name,
        "max_concurrent_codex_subagents": int(
            budget.get("max_concurrent_codex_subagents")
            or preset.max_concurrent_codex_subagents
        ),
        "tasks": tasks,
    }
    _write_json(run_dir / RESEARCH_TASKS_FILENAME, payload)
    return _tasks_payload(run_dir, tasks, evidence=evidence, status="planned")


def run_parallel_orchestration(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
    adapter_name: str = "codex-exec",
    min_tasks: int = 1,
    max_tasks: int | None = None,
    retry_failed: bool = False,
    allow_degraded: bool = True,
) -> dict[str, Any]:
    """Run planned research tasks through a Codex adapter and merge accepted shards."""

    run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    plan_research_tasks(run=run_dir, min_tasks=min_tasks, max_tasks=max_tasks)
    tasks_artifact = _read_json(run_dir / RESEARCH_TASKS_FILENAME)
    evidence = _read_json(run_dir / "evidence.json")
    tasks = _task_list(tasks_artifact)
    max_concurrent = int(tasks_artifact.get("max_concurrent_codex_subagents") or 1)
    adapter = _adapter(adapter_name)
    parallel_degraded = False
    degraded_reason = None
    if isinstance(adapter, CodexExecAdapter) and not adapter.available():
        if not allow_degraded:
            raise AdapterUnavailable("codex exec is not available on PATH")
        parallel_degraded = True
        degraded_reason = "codex_exec_unavailable"
        adapter = FixtureAdapter()
        max_concurrent = 1

    runnable = _runnable_tasks(tasks, retry_failed=retry_failed)
    for task in runnable:
        _assign_task(
            run_dir,
            task,
            adapter_name=adapter.name,
            max_concurrent=max_concurrent,
            parallel_degraded=parallel_degraded,
        )
        result = adapter.run_task(task, run_dir=run_dir, max_threads=max_concurrent)
        _record_runner_result(run_dir, task, result)
        if (
            isinstance(adapter, CodexExecAdapter)
            and allow_degraded
            and result.status == "failed"
            and _is_missing_capability_failure(result)
        ):
            parallel_degraded = True
            degraded_reason = result.failure_category or "codex_exec_unavailable"
            adapter = FixtureAdapter()
            max_concurrent = 1
            task["state"] = "retryable"
            _assign_task(
                run_dir,
                task,
                adapter_name=adapter.name,
                max_concurrent=max_concurrent,
                parallel_degraded=parallel_degraded,
            )
            degraded_result = adapter.run_task(task, run_dir=run_dir, max_threads=max_concurrent)
            _record_runner_result(run_dir, task, degraded_result)

    tasks_artifact["tasks"] = tasks
    tasks_artifact["parallel_degraded"] = parallel_degraded
    if degraded_reason:
        tasks_artifact["degraded_reason"] = degraded_reason
    _write_json(run_dir / RESEARCH_TASKS_FILENAME, tasks_artifact)
    merge_status = merge_evidence_shards(run=run_dir)
    status = {
        "schema_version": PARALLEL_SCHEMA_VERSION,
        "run_id": str(evidence.get("run_id") or run_dir.name),
        "status": "completed",
        "parallel_degraded": parallel_degraded,
        "degraded_reason": degraded_reason,
        "adapter": adapter.name,
        "planned_task_count": len(tasks),
        "runnable_task_count": len(runnable),
        "max_scheduled_concurrency": max_concurrent,
        "artifacts": {
            "research_tasks": str(run_dir / RESEARCH_TASKS_FILENAME),
            "subagent_assignments": str(run_dir / ASSIGNMENTS_FILENAME),
            "merge_status": str(run_dir / MERGE_STATUS_FILENAME),
            "evidence": str(run_dir / "evidence.json"),
            "run_trace": str(trace_path(run_dir)),
        },
        "merge": merge_status,
    }
    _write_json(run_dir / "parallel_orchestration_status.json", status)
    return status


def merge_evidence_shards(*, run: str | Path, runs_dir: str | Path | None = None) -> dict[str, Any]:
    """Validate and deterministically merge completed shard evidence into evidence.json."""

    run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    evidence_path = run_dir / "evidence.json"
    evidence = _read_json(evidence_path)
    tasks_artifact = _read_json(run_dir / RESEARCH_TASKS_FILENAME)
    tasks = _task_list(tasks_artifact)
    source_by_url: dict[str, str] = {}
    image_by_hash: dict[str, str] = {}
    claim_by_text: dict[str, str] = {}
    source_dedupe: list[dict[str, Any]] = []
    image_dedupe: list[dict[str, Any]] = []
    claim_dedupe: list[dict[str, Any]] = []
    accepted_shards: list[dict[str, Any]] = []
    rejected_shards: list[dict[str, Any]] = []
    blocked_tasks: list[dict[str, Any]] = []
    discarded_tasks: list[dict[str, Any]] = []

    evidence.setdefault("sources", [])
    evidence.setdefault("images", [])
    evidence.setdefault("claims", [])
    _index_existing_evidence(evidence, source_by_url, image_by_hash, claim_by_text)

    for task in tasks:
        state = str(task.get("state"))
        if state == "blocked":
            blocked_tasks.append(_task_status_record(task, task.get("blocked_reason")))
            continue
        if state == "discarded":
            discarded_tasks.append(_task_status_record(task, task.get("discard_reason")))
            continue
        if state not in {"completed", "merged"}:
            continue
        shard_path = run_dir / str(task.get("output_shard_path"))
        validation = validate_artifacts(evidence_path=shard_path)
        if not validation.valid:
            task["state"] = "discarded"
            task["discard_reason"] = "invalid_evidence_shard"
            rejected_shards.append(
                {
                    "task_id": task.get("id"),
                    "path": str(shard_path),
                    "reason": "invalid_evidence_shard",
                    "validation": validation.to_dict(),
                }
            )
            discarded_tasks.append(_task_status_record(task, "invalid_evidence_shard"))
            continue
        shard = _read_json(shard_path)
        id_map: dict[str, str] = {}
        new_claims = 0
        for source in _list(shard.get("sources")):
            key = _source_key(source)
            existing_id = source_by_url.get(key)
            if existing_id:
                id_map[str(source.get("id"))] = existing_id
                source_dedupe.append(
                    {"task_id": task["id"], "duplicate_id": source.get("id"), "kept_id": existing_id}
                )
                continue
            source_copy = dict(source)
            source_by_url[key] = str(source_copy.get("id"))
            evidence["sources"].append(source_copy)
            id_map[str(source_copy.get("id"))] = str(source_copy.get("id"))

        for image in _list(shard.get("images")):
            image_copy = dict(image)
            if image_copy.get("source_id") in id_map:
                image_copy["source_id"] = id_map[str(image_copy["source_id"])]
            key = _image_key(image_copy)
            existing_id = image_by_hash.get(key)
            if existing_id:
                id_map[str(image.get("id"))] = existing_id
                image_dedupe.append(
                    {"task_id": task["id"], "duplicate_id": image.get("id"), "kept_id": existing_id}
                )
                continue
            image_by_hash[key] = str(image_copy.get("id"))
            evidence["images"].append(image_copy)
            id_map[str(image_copy.get("id"))] = str(image_copy.get("id"))

        for claim in _list(shard.get("claims")):
            claim_copy = _remap_claim_refs(claim, id_map)
            if not _claim_is_mergeable(claim_copy):
                claim_dedupe.append(
                    {
                        "task_id": task["id"],
                        "duplicate_id": claim.get("id"),
                        "reason": "claim_not_confident_or_policy_safe",
                    }
                )
                continue
            key = _claim_key(claim_copy)
            existing_id = claim_by_text.get(key)
            if existing_id:
                claim_dedupe.append(
                    {"task_id": task["id"], "duplicate_id": claim.get("id"), "kept_id": existing_id}
                )
                continue
            claim_by_text[key] = str(claim_copy.get("id"))
            evidence["claims"].append(claim_copy)
            new_claims += 1

        if new_claims or state == "merged":
            task["state"] = "merged"
            accepted_shards.append({"task_id": task["id"], "path": str(shard_path)})
        else:
            task["state"] = "discarded"
            task["discard_reason"] = "dedupe_or_no_mergeable_claims"
            discarded_tasks.append(_task_status_record(task, "dedupe_or_no_mergeable_claims"))

    _write_json(evidence_path, evidence)
    validation = validate_artifacts(evidence_path=evidence_path)
    merge_status = {
        "schema_version": PARALLEL_SCHEMA_VERSION,
        "run_id": str(evidence.get("run_id") or run_dir.name),
        "generated_at": _utc_now(),
        "status": "completed" if validation.valid else "failed_validation",
        "parallel_degraded": bool(tasks_artifact.get("parallel_degraded")),
        "accepted_shards": accepted_shards,
        "rejected_shards": rejected_shards,
        "blocked_tasks": blocked_tasks,
        "discarded_tasks": discarded_tasks,
        "source_dedupe": source_dedupe,
        "image_dedupe": image_dedupe,
        "claim_dedupe": claim_dedupe,
        "conflicts": [],
        "merged_artifact_paths": {
            "evidence": str(evidence_path),
            "research_tasks": str(run_dir / RESEARCH_TASKS_FILENAME),
            "merge_status": str(run_dir / MERGE_STATUS_FILENAME),
        },
        "validation": validation.to_dict(),
    }
    tasks_artifact["tasks"] = tasks
    _write_json(run_dir / RESEARCH_TASKS_FILENAME, tasks_artifact)
    _write_json(run_dir / MERGE_STATUS_FILENAME, merge_status)
    return merge_status


def _adapter(name: str) -> CodexExecAdapter | FixtureAdapter:
    normalized = name.strip().lower().replace("_", "-")
    if normalized == "codex-exec":
        return CodexExecAdapter()
    if normalized in {"fixture", "fake", "deterministic"}:
        return FixtureAdapter()
    raise ParallelOrchestrationError("adapter must be codex-exec or fixture")


def _tasks_payload(
    run_dir: Path,
    tasks: Sequence[Mapping[str, Any]],
    *,
    evidence: Mapping[str, Any],
    status: str,
) -> dict[str, Any]:
    return {
        "schema_version": PARALLEL_SCHEMA_VERSION,
        "run_id": str(evidence.get("run_id") or run_dir.name),
        "status": status,
        "task_count": len(tasks),
        "tasks": [dict(task) for task in tasks],
        "artifacts": {
            "research_tasks": str(run_dir / RESEARCH_TASKS_FILENAME),
            "subagent_assignments": str(run_dir / ASSIGNMENTS_FILENAME),
            "evidence_shards": str(run_dir / EVIDENCE_SHARDS_DIRNAME),
        },
    }


def _read_research_tasks(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / RESEARCH_TASKS_FILENAME
    if not path.exists():
        return []
    return _task_list(_read_json(path))


def _task_list(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        raise ParallelOrchestrationError("research_tasks.json must contain a tasks list")
    normalized: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            raise ParallelOrchestrationError("each ResearchTask must be a JSON object")
        state = str(task.get("state") or "")
        if state not in TASK_STATES:
            raise ParallelOrchestrationError(f"invalid ResearchTask state: {state}")
        normalized.append(dict(task))
    return normalized


def _runnable_tasks(tasks: list[dict[str, Any]], *, retry_failed: bool) -> list[dict[str, Any]]:
    runnable: list[dict[str, Any]] = []
    for task in tasks:
        state = str(task.get("state"))
        if state in {"completed", "merged", "blocked", "discarded"}:
            continue
        if state == "failed":
            if not retry_failed:
                continue
            if task.get("failure_category") not in RETRY_SAFE_FAILURES:
                continue
            if int(task.get("attempt") or 0) >= int(task.get("max_attempts") or 1):
                continue
            task["state"] = "retryable"
        runnable.append(task)
    return runnable


def _assign_task(
    run_dir: Path,
    task: dict[str, Any],
    *,
    adapter_name: str,
    max_concurrent: int,
    parallel_degraded: bool,
) -> None:
    task["attempt"] = int(task.get("attempt") or 0) + 1
    task["assigned_subagent_id"] = f"{adapter_name}-{task['id']}-attempt-{task['attempt']}"
    task["state"] = "assigned"
    record = {
        "schema_version": PARALLEL_SCHEMA_VERSION,
        "assignment_id": f"assign-{uuid.uuid4().hex[:12]}",
        "timestamp": _utc_now(),
        "task_id": task["id"],
        "state": "assigned",
        "assigned_subagent_id": task["assigned_subagent_id"],
        "attempt": task["attempt"],
        "adapter": adapter_name,
        "max_concurrent_codex_subagents": max_concurrent,
        "parallel_degraded": parallel_degraded,
    }
    _append_jsonl(run_dir / ASSIGNMENTS_FILENAME, record)
    task["state"] = "running"


def _record_runner_result(run_dir: Path, task: dict[str, Any], result: RunnerResult) -> None:
    trace_ids: list[str] = list(task.get("trace_event_ids") or [])
    for event in result.events:
        trace_record = _trace_record(run_dir, event)
        append_trace_record(run_dir, trace_record)
        trace_ids.append(trace_record["event_id"])
    task["trace_event_ids"] = trace_ids
    if result.status == "completed" and result.shard_path and Path(result.shard_path).exists():
        task["state"] = "completed"
        task["failure_category"] = None
        return
    failure = result.failure_category or "missing_shard"
    task["failure_category"] = failure
    if failure in RETRY_SAFE_FAILURES and int(task.get("attempt") or 0) < int(task.get("max_attempts") or 1):
        task["state"] = "retryable"
    else:
        task["state"] = "failed"


def _trace_record(run_dir: Path, event: Mapping[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("event_type") or "message")
    child_status = str(event.get("child_status") or "unknown")
    task_id = str(event.get("task_id") or "unknown")
    child_message = str(event.get("child_message") or event_type)
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "event_id": f"parallel-{event_type}-{uuid.uuid4().hex[:12]}",
        "event_type": event_type,
        "timestamp": str(event.get("timestamp") or _utc_now()),
        "stage": "parallel_orchestration",
        "agent_role": "codex_subagent_orchestrator",
        "status": child_status,
        "prompt_summary": f"Run bounded ResearchTask {task_id} through a Codex subagent adapter.",
        "tool_call_summary": f"adapter event={event_type}; child_thread_id={event.get('child_thread_id')}",
        "output_preview": child_message[:700],
        "artifacts": {"run_trace": str(trace_path(run_dir))},
        "failure_category": event.get("failure_category"),
        "task_id": task_id,
        "child_thread_id": event.get("child_thread_id"),
        "child_status": child_status,
        "child_message": child_message,
        "raw_event": event.get("raw_event", {}),
    }


def _codex_event(
    event_type: str,
    task: Mapping[str, Any],
    *,
    child_thread_id: str,
    child_status: str,
    child_message: str | None = None,
    failure_category: str | None = None,
    raw_event: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": PARALLEL_SCHEMA_VERSION,
        "timestamp": _utc_now(),
        "event_type": event_type,
        "task_id": task["id"],
        "child_thread_id": child_thread_id,
        "child_status": child_status,
        "child_message": child_message or event_type,
        "failure_category": failure_category,
        "raw_event": dict(raw_event or {}),
    }


def _write_fixture_shard(run_dir: Path, task: Mapping[str, Any], shard_path: Path) -> None:
    evidence = _read_json(run_dir / "evidence.json")
    shard_path.parent.mkdir(parents=True, exist_ok=True)
    now = _utc_now()
    source_id = f"src_{task['id']}_001"
    source_url = f"https://example.com/codex-deepresearch/{task['id']}"
    source = {
        "id": source_id,
        "type": "web",
        "url": source_url,
        "title": f"Fixture source for {task['id']}",
        "published_at": None,
        "accessed_at": now,
        "quality": "secondary",
        "retrieval_status": "fetched",
        "local_artifact_path": f"{EVIDENCE_SHARDS_DIRNAME}/{task['id']}/source.json",
        "license_policy": "allowed",
        "robots_policy": "allowed",
        "policy_decision": "allowed",
        "policy_flags": [],
        "route": task.get("route"),
        "angle_id": task.get("angle_id"),
    }
    claim_type = "text"
    supporting_images: list[str] = []
    images: list[dict[str, Any]] = []
    if int(task.get("max_images") or 0) > 0:
        claim_type = "mixed"
        image_id = f"img_{task['id']}_001"
        supporting_images.append(image_id)
        images.append(
            {
                "id": image_id,
                "source_id": source_id,
                "origin": "screenshot",
                "page_url": source_url,
                "image_url": source_url + "/image.png",
                "local_artifact_path": f"{EVIDENCE_SHARDS_DIRNAME}/{task['id']}/image.png",
                "mime_type": "image/png",
                "width": 640,
                "height": 360,
                "observations": [f"Fixture visual observation for {task['query']}"],
                "inferences": [],
                "visual_tasks": [str(task["id"])],
                "analysis_provider": evidence.get("vlm_provider", "codex-interactive"),
                "analysis_status": "analyzed",
                "policy_flags": [],
                "caveats": [],
                "content_hash": f"fixture-image-{task['id']}",
            }
        )
    claim_text = f"Fixture evidence for {task['query']}."
    shard = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "run_id": f"{evidence.get('run_id', run_dir.name)}-{task['id']}",
        "created_at": now,
        "question": evidence.get("question", ""),
        "mode": evidence.get("mode", "codex-plugin"),
        "search_provider": evidence.get("search_provider", "codex-native"),
        "vlm_provider": evidence.get("vlm_provider", "codex-interactive"),
        "sources": [source],
        "images": images,
        "claims": [
            {
                "id": f"claim_{task['id']}_001",
                "text": claim_text,
                "claim_type": claim_type,
                "supporting_sources": [source_id],
                "supporting_images": supporting_images,
                "quote_spans": [
                    {
                        "source_id": source_id,
                        "quote": claim_text,
                        "location": "fixture paragraph 1",
                    }
                ],
                "votes": [],
                "verification_status": "supported",
                "review_status": "human_accepted",
                "promotion_status": "not_eligible",
                "confidence": "medium",
                "caveats": [],
                "source_task_id": task["id"],
            }
        ],
    }
    _write_json(shard_path, shard)
    _write_task_handoffs(shard_path.parent, task, source, images)


def _write_task_handoffs(
    task_dir: Path,
    task: Mapping[str, Any],
    source: Mapping[str, Any],
    images: Sequence[Mapping[str, Any]],
) -> None:
    now = _utc_now()
    search_result = {
        "id": f"sr_{task['id']}_001",
        "task_id": task["id"],
        "angle_id": task["angle_id"],
        "route": task["route"],
        "provider": "manual",
        "query": task["query"],
        "url": source["url"],
        "title": source["title"],
        "snippet": f"Fixture search result for {task['query']}",
        "result_type": "web",
        "rank": 1,
        "freshness_requirement": "any",
        "published_at": None,
        "accessed_at": now,
        "language": "en",
        "region": "US",
        "policy_decision": "allowed",
        "policy_flags": [],
        "raw_provider_metadata": {"fixture": True},
    }
    _append_jsonl(task_dir / "search_results.jsonl", search_result)
    visual_path = task_dir / "visual_observations.jsonl"
    visual_path.write_text("", encoding="utf-8")
    for image in images:
        _append_jsonl(visual_path, image)


def _child_prompt(task: Mapping[str, Any], *, run_dir: Path) -> str:
    return (
        "Run this Codex DeepResearch bounded ResearchTask and write only schema-valid "
        f"evidence to {run_dir / str(task['output_shard_path'])}. "
        "Also write per-task search_results.jsonl and visual_observations.jsonl when applicable. "
        f"Task JSON: {json.dumps(dict(task), sort_keys=True)}"
    )


def _parse_json_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _event_message(event: Mapping[str, Any]) -> str:
    for key in ("message", "text", "content", "summary"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value[:700]
    return json.dumps(dict(event), sort_keys=True)[:700]


def _event_failure(event: Mapping[str, Any]) -> str | None:
    status = str(event.get("status") or "").lower()
    if status in {"failed", "error"}:
        return "codex_exec_failed"
    return None


def _is_missing_capability_failure(result: RunnerResult) -> bool:
    text = " ".join(
        value.lower()
        for value in (
            result.failure_category or "",
            result.message or "",
            " ".join(str(event.get("child_message") or "") for event in result.events),
        )
    )
    markers = (
        "auth",
        "login",
        "credential",
        "sandbox",
        "approval",
        "permission",
        "subagent",
        "max_threads",
        "spawn_agent",
    )
    return any(marker in text for marker in markers)


def _bounded_task_query(query: str, index: int) -> str:
    normalized = " ".join(query.split()) or "research question"
    if index == 1:
        return normalized
    lenses = (
        "official documentation",
        "primary sources",
        "recent changes",
        "counter-evidence",
        "visual evidence",
        "implementation details",
        "pricing and limits",
        "policy constraints",
        "user impact",
        "competitive comparison",
    )
    return f"{normalized} :: {lenses[(index - 2) % len(lenses)]} #{index:03d}"


def _index_existing_evidence(
    evidence: Mapping[str, Any],
    source_by_url: dict[str, str],
    image_by_hash: dict[str, str],
    claim_by_text: dict[str, str],
) -> None:
    for source in _list(evidence.get("sources")):
        source_by_url[_source_key(source)] = str(source.get("id"))
    for image in _list(evidence.get("images")):
        image_by_hash[_image_key(image)] = str(image.get("id"))
    for claim in _list(evidence.get("claims")):
        claim_by_text[_claim_key(claim)] = str(claim.get("id"))


def _source_key(source: Mapping[str, Any]) -> str:
    return str(source.get("url") or "").strip().lower()


def _image_key(image: Mapping[str, Any]) -> str:
    for key in ("content_hash", "sha256", "image_hash"):
        value = image.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return str(image.get("image_url") or image.get("local_artifact_path") or image.get("id")).lower()


def _claim_key(claim: Mapping[str, Any]) -> str:
    return re.sub(r"\s+", " ", str(claim.get("text") or "").strip().lower())


def _remap_claim_refs(claim: Mapping[str, Any], id_map: Mapping[str, str]) -> dict[str, Any]:
    claim_copy = dict(claim)
    claim_copy["supporting_sources"] = [
        id_map.get(str(source_id), str(source_id))
        for source_id in claim_copy.get("supporting_sources", [])
    ]
    claim_copy["supporting_images"] = [
        id_map.get(str(image_id), str(image_id))
        for image_id in claim_copy.get("supporting_images", [])
    ]
    quote_spans = []
    for quote_span in claim_copy.get("quote_spans", []):
        if isinstance(quote_span, Mapping):
            quote_copy = dict(quote_span)
            quote_copy["source_id"] = id_map.get(
                str(quote_copy.get("source_id")),
                str(quote_copy.get("source_id")),
            )
            quote_spans.append(quote_copy)
    claim_copy["quote_spans"] = quote_spans
    return claim_copy


def _claim_is_mergeable(claim: Mapping[str, Any]) -> bool:
    if claim.get("verification_status") in {"policy_blocked", "refuted"}:
        return False
    if claim.get("review_status") == "human_rejected":
        return False
    return bool(claim.get("supporting_sources") or claim.get("supporting_images"))


def _task_status_record(task: Mapping[str, Any], reason: Any) -> dict[str, Any]:
    return {
        "task_id": task.get("id"),
        "state": task.get("state"),
        "reason": reason,
        "output_shard_path": task.get("output_shard_path"),
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SearchHandoffError(f"missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ParallelOrchestrationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ParallelOrchestrationError(f"expected JSON object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(payload), sort_keys=True) + "\n")


def _list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
