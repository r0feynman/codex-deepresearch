"""Parallel Codex subagent orchestration artifacts and deterministic runner."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence_schema import EVIDENCE_SCHEMA_VERSION, validate_artifacts
from .execution_mode import BUDGET_PRESETS
from .run_state import add_run_steps_artifact, begin_stage, skip_stage, transition_stage
from .search_handoff import SearchHandoffError, resolve_run_dir
from .trace import TRACE_SCHEMA_VERSION, append_trace_record, trace_path


PARALLEL_SCHEMA_VERSION = "codex-deepresearch.parallel-orchestration.v0"
RESEARCH_TASKS_FILENAME = "research_tasks.json"
ASSIGNMENTS_FILENAME = "subagent_assignments.jsonl"
MERGE_STATUS_FILENAME = "merge_status.json"
EVIDENCE_SHARDS_DIRNAME = "evidence_shards"
CHILD_EVENTS_DIRNAME = "child_events"
CODEX_EXEC_STDOUT_FILENAME = "codex_exec_stdout.jsonl"
CODEX_EXEC_STDERR_FILENAME = "codex_exec_stderr.txt"
LAST_CHILD_EVENT_FILENAME = "last_child_event.json"
EXPECTED_CHILD_SIDECARS = {
    "search_results": "search_results.jsonl",
    "visual_observations": "visual_observations.jsonl",
    "verifier_votes": "verifier_votes.jsonl",
}
RETRY_SAFE_FAILURES = {
    "adapter_unavailable",
    "codex_exec_failed",
    "invalid_shard",
    "missing_shard",
}
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

    def __init__(
        self,
        *,
        codex_binary: str = "codex",
        timeout_seconds: float = 300.0,
        project_root: str | Path | None = None,
    ) -> None:
        self.codex_binary = codex_binary
        self.timeout_seconds = timeout_seconds
        self.project_root = Path(project_root).resolve() if project_root else _default_project_root()

    def available(self) -> bool:
        return shutil.which(self.codex_binary) is not None

    def build_command(
        self,
        task: Mapping[str, Any],
        *,
        max_threads: int,
        run_dir: Path,
        sandbox_mode: str = "workspace-write",
        approval_policy: str = "never",
    ) -> list[str]:
        run_dir = run_dir.resolve()
        shard_path = _resolve_task_shard_path(task, run_dir)
        prompt = _child_prompt(task, run_dir=run_dir, shard_path=shard_path)
        return [
            self.codex_binary,
            "exec",
            "--json",
            "--ignore-user-config",
            "--ignore-rules",
            "-C",
            str(self.project_root),
            "--add-dir",
            str(run_dir),
            "-c",
            f"agents.max_threads={max_threads}",
            "-c",
            f"sandbox_mode={sandbox_mode}",
            "-c",
            f"approval_policy={approval_policy}",
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
        run_dir = run_dir.resolve()
        child_thread_id = f"codex-{task['id']}-{uuid.uuid4().hex[:8]}"
        try:
            shard_path = _resolve_task_shard_path(task, run_dir)
            command = self.build_command(task, max_threads=max_threads, run_dir=run_dir)
        except ParallelOrchestrationError as exc:
            return _invalid_output_shard_result(
                adapter_name=self.name,
                task=task,
                child_thread_id=child_thread_id,
                message=str(exc),
            )
        command_context = _codex_exec_command_context(
            adapter_name=self.name,
            task=task,
            command=command,
            cwd=self.project_root,
            run_dir=run_dir,
            shard_path=shard_path,
        )
        child_artifacts = _codex_exec_child_artifact_paths(
            run_dir=run_dir,
            task_id=str(task.get("id") or "unknown"),
        )
        command_context["child_event_artifacts"] = _stringify_paths(child_artifacts)
        events = [
            _codex_event(
                "spawn_agent",
                task,
                child_thread_id=child_thread_id,
                child_status="running",
                child_message="codex exec JSON runner started",
                raw_event=command_context,
            )
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self.project_root,
                check=False,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _timeout_stdout(exc)
            stderr = getattr(exc, "stderr", None)
            child_summary = _write_codex_exec_child_diagnostics(
                task=task,
                artifacts=child_artifacts,
                stdout=stdout,
                stderr=stderr,
                timeout=True,
                timeout_seconds=exc.timeout,
            )
            command_context["last_child_event_summary"] = child_summary
            diagnostic = _codex_exec_failure_message(
                command_context=command_context,
                cause=exc.__class__.__name__,
                stdout=stdout,
                stderr=stderr,
            )
            if shard_path.exists() and validate_artifacts(evidence_path=shard_path).valid:
                valid_shard_context = dict(command_context)
                valid_shard_context.update(
                    _timeout_after_valid_shard_context(shard_path=shard_path)
                )
                diagnostic = _append_timeout_after_valid_shard_context(
                    diagnostic,
                    valid_shard_context,
                )
                events.append(
                    _codex_event(
                        "wait",
                        task,
                        child_thread_id=child_thread_id,
                        child_status="completed",
                        child_message=f"codex exec timed out after writing a valid shard; {diagnostic}",
                        raw_event=valid_shard_context,
                    )
                )
                events.append(
                    _codex_event(
                        "close_agent",
                        task,
                        child_thread_id=child_thread_id,
                        child_status="completed",
                    )
                )
                return RunnerResult(
                    task_id=str(task["id"]),
                    status="completed",
                    child_thread_id=child_thread_id,
                    events=tuple(events),
                    shard_path=str(shard_path),
                    message=diagnostic,
                )
            diagnostic = _append_shard_validation_context(diagnostic, shard_path)
            events.append(
                _codex_event(
                    "wait",
                    task,
                    child_thread_id=child_thread_id,
                    child_status="failed",
                    failure_category="codex_exec_failed",
                    child_message=diagnostic,
                    raw_event=command_context,
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
                message=diagnostic,
            )
        except OSError as exc:
            child_summary = _write_codex_exec_child_diagnostics(
                task=task,
                artifacts=child_artifacts,
                stdout=getattr(exc, "stdout", None),
                stderr=getattr(exc, "stderr", None),
                os_error=exc.__class__.__name__,
            )
            command_context["last_child_event_summary"] = child_summary
            diagnostic = _codex_exec_failure_message(
                command_context=command_context,
                cause=exc.__class__.__name__,
                stdout=getattr(exc, "stdout", None),
                stderr=getattr(exc, "stderr", None),
            )
            events.append(
                _codex_event(
                    "wait",
                    task,
                    child_thread_id=child_thread_id,
                    child_status="failed",
                    failure_category="codex_exec_failed",
                    child_message=diagnostic,
                    raw_event=command_context,
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
                message=diagnostic,
            )

        child_summary = _write_codex_exec_child_diagnostics(
            task=task,
            artifacts=child_artifacts,
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )
        command_context["last_child_event_summary"] = child_summary
        parsed_events = _parse_json_events(completed.stdout)
        events.extend(
            _codex_event(
                event.get("event", event.get("type", "message")),
                task,
                child_thread_id=str(event.get("thread_id") or child_thread_id),
                child_status=str(event.get("status") or "running"),
                child_message=_event_message(event),
                raw_event=_with_child_event_artifacts(event, child_artifacts),
                failure_category=_event_failure(event),
            )
            for event in parsed_events
        )
        status = "completed" if completed.returncode == 0 else "failed"
        failure = None if status == "completed" else "codex_exec_failed"
        diagnostic = None
        if status == "failed":
            diagnostic = _codex_exec_failure_message(
                command_context=command_context,
                cause=f"codex exec exited {completed.returncode}",
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        events.append(
            _codex_event(
                "wait",
                task,
                child_thread_id=child_thread_id,
                child_status=status,
                failure_category=failure,
                child_message=diagnostic or f"codex exec exited {completed.returncode}",
                raw_event=command_context if diagnostic else None,
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
        return RunnerResult(
            task_id=str(task["id"]),
            status=status,
            child_thread_id=child_thread_id,
            events=tuple(events),
            shard_path=str(shard_path) if status == "completed" else None,
            failure_category=failure,
            message=diagnostic,
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
        try:
            shard_path = _resolve_task_shard_path(task, run_dir)
        except ParallelOrchestrationError as exc:
            return _invalid_output_shard_result(
                adapter_name=self.name,
                task=task,
                child_thread_id=child_thread_id,
                message=str(exc),
            )
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


class SerialFallbackAdapter:
    """Honest degraded adapter that records blocked work without fabricating evidence."""

    name = "serial-degraded"

    def run_task(
        self,
        task: Mapping[str, Any],
        *,
        run_dir: Path,
        max_threads: int,
    ) -> RunnerResult:
        child_thread_id = f"serial-degraded-{task['id']}"
        events = (
            _codex_event(
                "spawn_agent",
                task,
                child_thread_id=child_thread_id,
                child_status="blocked",
                child_message="parallel execution capability unavailable; serial fallback recorded blocked task",
                failure_category="adapter_unavailable",
            ),
            _codex_event(
                "wait",
                task,
                child_thread_id=child_thread_id,
                child_status="blocked",
                child_message="no production Codex subagent evidence was fabricated",
                failure_category="adapter_unavailable",
            ),
            _codex_event(
                "close_agent",
                task,
                child_thread_id=child_thread_id,
                child_status="blocked",
                failure_category="adapter_unavailable",
            ),
        )
        return RunnerResult(
            task_id=str(task["id"]),
            status="blocked",
            child_thread_id=child_thread_id,
            events=events,
            failure_category="adapter_unavailable",
            message="codex execution unavailable; task preserved as blocked",
        )


def plan_research_tasks(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
    min_tasks: int = 1,
    max_tasks: int | None = None,
    confirm_exhaustive: bool = False,
    max_cost_usd: float | None = None,
) -> dict[str, Any]:
    """Expand planner search tasks into bounded ResearchTask records."""

    run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    evidence = _read_json(run_dir / "evidence.json")
    budget = evidence.get("budget", {}) if isinstance(evidence.get("budget"), Mapping) else {}
    preset_name = str(budget.get("preset") or "standard")
    preset = BUDGET_PRESETS.get(preset_name, BUDGET_PRESETS["standard"])
    _enforce_parallel_budget_gate(
        run_dir=run_dir,
        preset_name=preset_name,
        budget=budget,
        confirm_exhaustive=confirm_exhaustive,
        max_cost_usd=max_cost_usd,
    )
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
        max_images = _planner_max_images(base, evidence=evidence, route=route)
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


def _planner_max_images(
    task: Mapping[str, Any],
    *,
    evidence: Mapping[str, Any],
    route: str,
) -> int:
    if route == "text_only":
        return 0
    direct = _nonnegative_int(task.get("max_images"))
    if direct > 0:
        return direct
    angle_id = str(task.get("angle_id") or "")
    routing = evidence.get("routing")
    if isinstance(routing, list):
        for record in routing:
            if not isinstance(record, Mapping):
                continue
            same_angle = bool(angle_id and str(record.get("id") or "") == angle_id)
            same_route = str(record.get("modality") or record.get("route") or "") == route
            if same_angle or same_route:
                routed = _nonnegative_int(record.get("max_images"))
                if routed > 0:
                    return routed
    budget = evidence.get("budget")
    if isinstance(budget, Mapping):
        return _nonnegative_int(budget.get("max_images"))
    return 0


def _nonnegative_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def run_parallel_orchestration(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
    adapter_name: str = "codex-exec",
    codex_exec_timeout_seconds: float | None = None,
    min_tasks: int = 1,
    max_tasks: int | None = None,
    retry_failed: bool = False,
    allow_degraded: bool = True,
    confirm_exhaustive: bool = False,
    max_cost_usd: float | None = None,
) -> dict[str, Any]:
    """Run planned research tasks through a Codex adapter and merge accepted shards."""

    run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    stage_start = begin_stage(run_dir, "parallel_orchestration")
    if stage_start.skipped:
        status = _parallel_status(
            run_dir,
            evidence=_read_json(run_dir / "evidence.json"),
            status="skipped",
            parallel_degraded=False,
            degraded_reason=None,
            adapter_name=adapter_name,
            planned_task_count=len(_read_research_tasks(run_dir)),
            runnable_task_count=0,
            max_scheduled_concurrency=0,
            merge_status=_read_optional_json(run_dir / MERGE_STATUS_FILENAME),
            skip_reason=stage_start.skip_reason or "stage_already_completed",
        )
        _write_json(run_dir / "parallel_orchestration_status.json", status)
        return status

    try:
        result = _run_parallel_orchestration_started(
            run_dir=run_dir,
            adapter_name=adapter_name,
            codex_exec_timeout_seconds=codex_exec_timeout_seconds,
            min_tasks=min_tasks,
            max_tasks=max_tasks,
            retry_failed=retry_failed,
            allow_degraded=allow_degraded,
            confirm_exhaustive=confirm_exhaustive,
            max_cost_usd=max_cost_usd,
        )
    except Exception as exc:
        status = _parallel_status(
            run_dir,
            evidence=_read_optional_json(run_dir / "evidence.json") or {"run_id": run_dir.name},
            status="failed",
            parallel_degraded=False,
            degraded_reason=None,
            adapter_name=adapter_name,
            planned_task_count=0,
            runnable_task_count=0,
            max_scheduled_concurrency=0,
            merge_status=None,
            errors=[{"code": exc.__class__.__name__, "message": str(exc)[:500]}],
        )
        _write_json(run_dir / "parallel_orchestration_status.json", status)
        transition_stage(
            run_dir,
            "parallel_orchestration",
            "failed",
            reason="stage_failed",
            status_payload=status,
        )
        raise
    return result


def _run_parallel_orchestration_started(
    *,
    run_dir: Path,
    adapter_name: str,
    codex_exec_timeout_seconds: float | None,
    min_tasks: int,
    max_tasks: int | None,
    retry_failed: bool,
    allow_degraded: bool,
    confirm_exhaustive: bool,
    max_cost_usd: float | None,
) -> dict[str, Any]:
    plan_research_tasks(
        run=run_dir,
        min_tasks=min_tasks,
        max_tasks=max_tasks,
        confirm_exhaustive=confirm_exhaustive,
        max_cost_usd=max_cost_usd,
    )
    tasks_artifact = _read_json(run_dir / RESEARCH_TASKS_FILENAME)
    evidence = _read_json(run_dir / "evidence.json")
    tasks = _task_list(tasks_artifact)
    max_concurrent = int(tasks_artifact.get("max_concurrent_codex_subagents") or 1)
    requested_adapter_name = _normalize_adapter_name(adapter_name)
    adapter = _adapter(adapter_name, codex_exec_timeout_seconds=codex_exec_timeout_seconds)
    parallel_degraded = False
    degraded_reason = None
    if isinstance(adapter, CodexExecAdapter) and not adapter.available():
        if not allow_degraded:
            blocked_reason = "codex exec is not available on PATH"
            for task in tasks:
                task["state"] = "blocked"
                task["last_adapter"] = adapter.name
                task["failure_category"] = "adapter_unavailable"
                task["blocked_reason"] = blocked_reason
            tasks_artifact["tasks"] = tasks
            tasks_artifact["parallel_degraded"] = False
            tasks_artifact["last_adapter"] = adapter.name
            tasks_artifact["attempted_real_child_execution"] = False
            tasks_artifact["degraded_reason"] = "codex_exec_unavailable"
            _write_json(run_dir / RESEARCH_TASKS_FILENAME, tasks_artifact)
            merge_status = merge_evidence_shards(run=run_dir)
            status = _parallel_status(
                run_dir,
                evidence=evidence,
                status="blocked_parallel_execution",
                parallel_degraded=False,
                degraded_reason="codex_exec_unavailable",
                adapter_name=adapter.name,
                planned_task_count=len(tasks),
                runnable_task_count=0,
                max_scheduled_concurrency=0,
                merge_status=merge_status,
                needs_serial_handoff=True,
            )
            _write_json(run_dir / "parallel_orchestration_status.json", status)
            transition_stage(
                run_dir,
                "parallel_orchestration",
                "failed",
                reason="stage_failed",
                status_payload=status,
            )
            return status
        parallel_degraded = True
        degraded_reason = "codex_exec_unavailable"
        adapter = SerialFallbackAdapter()
        max_concurrent = 1

    runnable = _runnable_tasks(tasks, retry_failed=retry_failed)
    worker_count = max(1, min(max_concurrent, len(runnable) or 1))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_task: dict[Any, dict[str, Any]] = {}
        for task in runnable:
            if isinstance(adapter, SerialFallbackAdapter):
                max_concurrent = 1
                worker_count = 1
            _assign_task(
                run_dir,
                task,
                adapter_name=adapter.name,
                max_concurrent=max_concurrent,
                parallel_degraded=parallel_degraded,
            )
            future = executor.submit(
                adapter.run_task,
                dict(task),
                run_dir=run_dir,
                max_threads=max_concurrent,
            )
            future_to_task[future] = task

        for future in as_completed(future_to_task):
            task = future_to_task[future]
            result = future.result()
            _record_runner_result(run_dir, task, result)
            if (
                isinstance(adapter, CodexExecAdapter)
                and allow_degraded
                and result.status == "failed"
                and _is_missing_capability_failure(result)
            ):
                parallel_degraded = True
                degraded_reason = result.failure_category or "codex_exec_unavailable"
                _preserve_parallel_failure(task)
                task["state"] = "retryable"

    if parallel_degraded and isinstance(adapter, CodexExecAdapter):
        adapter = SerialFallbackAdapter()
        max_concurrent = 1
        for task in _runnable_tasks(tasks, retry_failed=True):
            if task.get("failure_category") not in RETRY_SAFE_FAILURES:
                continue
            if task.get("state") in {"completed", "merged", "blocked", "discarded"}:
                continue
            _preserve_parallel_failure(task)
            _assign_task(
                run_dir,
                task,
                adapter_name=adapter.name,
                max_concurrent=max_concurrent,
                parallel_degraded=parallel_degraded,
            )
            result = adapter.run_task(dict(task), run_dir=run_dir, max_threads=max_concurrent)
            _record_runner_result(run_dir, task, result)

    attempted_real_child_execution = any(
        str(task.get("last_adapter") or "") == "codex-exec"
        for task in tasks
    )
    tasks_artifact["tasks"] = tasks
    tasks_artifact["parallel_degraded"] = parallel_degraded
    tasks_artifact["last_adapter"] = adapter.name
    tasks_artifact["attempted_real_child_execution"] = attempted_real_child_execution
    if degraded_reason:
        tasks_artifact["degraded_reason"] = degraded_reason
    _write_json(run_dir / RESEARCH_TASKS_FILENAME, tasks_artifact)
    merge_status = merge_evidence_shards(run=run_dir)
    accepted_shards = _list(merge_status.get("accepted_shards"))
    status_value = _parallel_status_value(
        requested_adapter_name=requested_adapter_name,
        final_adapter_name=adapter.name,
        merge_status=merge_status,
        planned_task_count=len(tasks),
        parallel_degraded=parallel_degraded,
        attempted_real_child_execution=attempted_real_child_execution,
    )
    needs_serial_handoff = _needs_serial_handoff(
        status=status_value,
        accepted_shards=accepted_shards,
        parallel_degraded=parallel_degraded,
    )
    status = _parallel_status(
        run_dir,
        evidence=evidence,
        status=status_value,
        parallel_degraded=parallel_degraded,
        degraded_reason=degraded_reason,
        adapter_name=adapter.name,
        planned_task_count=len(tasks),
        runnable_task_count=len(runnable),
        max_scheduled_concurrency=max_concurrent,
        merge_status=merge_status,
        needs_serial_handoff=needs_serial_handoff,
    )
    _write_json(run_dir / "parallel_orchestration_status.json", status)
    if _parallel_status_ok(status_value):
        transition_stage(
            run_dir,
            "parallel_orchestration",
            "completed",
            reason="stage_completed",
            status_payload=status,
        )
        if status_value in {"completed_fixture", "completed_parallel", "completed_partial_parallel"}:
            _skip_serial_handoff_after_parallel(run_dir, status)
    else:
        transition_stage(
            run_dir,
            "parallel_orchestration",
            "failed",
            reason="stage_failed",
            status_payload=status,
        )
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
    used_source_ids: set[str] = set()
    used_image_ids: set[str] = set()
    used_claim_ids: set[str] = set()
    source_dedupe: list[dict[str, Any]] = []
    image_dedupe: list[dict[str, Any]] = []
    claim_dedupe: list[dict[str, Any]] = []
    accepted_shards: list[dict[str, Any]] = []
    rejected_shards: list[dict[str, Any]] = []
    blocked_tasks: list[dict[str, Any]] = []
    discarded_tasks: list[dict[str, Any]] = []
    failed_tasks: list[dict[str, Any]] = []

    evidence.setdefault("sources", [])
    evidence.setdefault("images", [])
    evidence.setdefault("claims", [])
    _index_existing_evidence(
        evidence,
        source_by_url,
        image_by_hash,
        claim_by_text,
        used_source_ids,
        used_image_ids,
        used_claim_ids,
    )

    for task in tasks:
        state = str(task.get("state"))
        if state == "blocked":
            blocked_tasks.append(_task_status_record(task, task.get("blocked_reason")))
            continue
        if state == "discarded":
            discarded_tasks.append(_task_status_record(task, task.get("discard_reason")))
            continue
        if state in {"failed", "retryable"}:
            failed_tasks.append(_task_failure_record(task))
            continue
        if state not in {"completed", "merged"}:
            continue
        try:
            shard_path = _resolve_task_shard_path(task, run_dir)
        except ParallelOrchestrationError as exc:
            task["state"] = "discarded"
            task["discard_reason"] = "invalid_output_shard_path"
            rejected_shards.append(
                {
                    "task_id": task.get("id"),
                    "path": str(task.get("output_shard_path") or ""),
                    "reason": "invalid_output_shard_path",
                    "diagnostic": str(exc),
                }
            )
            discarded_tasks.append(_task_status_record(task, "invalid_output_shard_path"))
            continue
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
            old_id = str(source.get("id") or "")
            existing_id = source_by_url.get(key)
            if existing_id:
                id_map[old_id] = existing_id
                source_dedupe.append(
                    {"task_id": task["id"], "duplicate_id": source.get("id"), "kept_id": existing_id}
                )
                continue
            source_copy = dict(source)
            new_id = _canonical_artifact_id(
                old_id,
                used_source_ids,
                prefix="src",
                task_id=str(task["id"]),
            )
            source_copy["id"] = new_id
            source_by_url[key] = new_id
            evidence["sources"].append(source_copy)
            id_map[old_id] = new_id

        for image in _list(shard.get("images")):
            image_copy = dict(image)
            old_id = str(image_copy.get("id") or "")
            if image_copy.get("source_id") in id_map:
                image_copy["source_id"] = id_map[str(image_copy["source_id"])]
            key = _image_key(image_copy)
            existing_id = image_by_hash.get(key)
            if existing_id:
                id_map[old_id] = existing_id
                image_dedupe.append(
                    {"task_id": task["id"], "duplicate_id": image.get("id"), "kept_id": existing_id}
                )
                continue
            new_id = _canonical_artifact_id(
                old_id,
                used_image_ids,
                prefix="img",
                task_id=str(task["id"]),
            )
            image_copy["id"] = new_id
            image_by_hash[key] = new_id
            evidence["images"].append(image_copy)
            id_map[old_id] = new_id

        for claim in _list(shard.get("claims")):
            claim_copy = _remap_claim_refs(claim, id_map)
            old_id = str(claim.get("id") or "")
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
            new_id = _canonical_artifact_id(
                old_id,
                used_claim_ids,
                prefix="claim",
                task_id=str(task["id"]),
            )
            claim_copy["id"] = new_id
            claim_by_text[key] = new_id
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
    tasks_artifact["attempted_real_child_execution"] = any(
        str(task.get("last_adapter") or "") == "codex-exec"
        for task in tasks
    )
    evidence_source = _parallel_evidence_source(
        adapter_name=str(tasks_artifact.get("last_adapter") or ""),
        parallel_degraded=bool(tasks_artifact.get("parallel_degraded")),
        accepted_shards=accepted_shards,
        attempted_real_child_execution=bool(tasks_artifact.get("attempted_real_child_execution")),
    )
    failure_counts = _failure_counts(
        failed_tasks=failed_tasks,
        blocked_tasks=blocked_tasks,
        rejected_shards=rejected_shards,
        discarded_tasks=discarded_tasks,
    )
    merge_status = {
        "schema_version": PARALLEL_SCHEMA_VERSION,
        "run_id": str(evidence.get("run_id") or run_dir.name),
        "generated_at": _utc_now(),
        "status": "completed" if validation.valid else "failed_validation",
        "parallel_degraded": bool(tasks_artifact.get("parallel_degraded")),
        "evidence_source": evidence_source,
        "accepted_shards": accepted_shards,
        "rejected_shards": rejected_shards,
        "blocked_tasks": blocked_tasks,
        "discarded_tasks": discarded_tasks,
        "failed_tasks": failed_tasks,
        "failure_counts": failure_counts,
        "diagnostics": _merge_diagnostics(
            accepted_shards=accepted_shards,
            failed_tasks=failed_tasks,
            blocked_tasks=blocked_tasks,
            rejected_shards=rejected_shards,
        ),
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


def _adapter(
    name: str,
    *,
    codex_exec_timeout_seconds: float | None = None,
) -> CodexExecAdapter | FixtureAdapter | SerialFallbackAdapter:
    normalized = _normalize_adapter_name(name)
    if normalized == "codex-exec":
        if codex_exec_timeout_seconds is not None and codex_exec_timeout_seconds <= 0:
            raise ParallelOrchestrationError("codex_exec_timeout_seconds must be positive")
        if codex_exec_timeout_seconds is None:
            return CodexExecAdapter()
        return CodexExecAdapter(timeout_seconds=codex_exec_timeout_seconds)
    if normalized in {"fixture", "fake", "deterministic"}:
        return FixtureAdapter()
    if normalized in {"serial-degraded", "serial-fallback"}:
        return SerialFallbackAdapter()
    raise ParallelOrchestrationError("adapter must be codex-exec, fixture, or serial-degraded")


def _normalize_adapter_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


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


def _parallel_status(
    run_dir: Path,
    *,
    evidence: Mapping[str, Any],
    status: str,
    parallel_degraded: bool,
    degraded_reason: str | None,
    adapter_name: str,
    planned_task_count: int,
    runnable_task_count: int,
    max_scheduled_concurrency: int,
    merge_status: Mapping[str, Any] | None,
    needs_serial_handoff: bool = False,
    skip_reason: str | None = None,
    errors: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    accepted_shards = _list(merge_status.get("accepted_shards")) if merge_status else []
    existing_source = merge_status.get("evidence_source") if merge_status else None
    attempted_real_child_execution = (
        bool(existing_source.get("attempted_real_child_execution"))
        if isinstance(existing_source, Mapping)
        else adapter_name == "codex-exec"
    )
    evidence_source = _parallel_evidence_source(
        adapter_name=adapter_name,
        parallel_degraded=parallel_degraded,
        accepted_shards=accepted_shards,
        attempted_real_child_execution=attempted_real_child_execution,
    )
    payload: dict[str, Any] = {
        "schema_version": PARALLEL_SCHEMA_VERSION,
        "run_id": str(evidence.get("run_id") or run_dir.name),
        "run_dir": str(run_dir),
        "ok": _parallel_status_ok(status),
        "status": status,
        "created_at": _utc_now(),
        "parallel_degraded": parallel_degraded,
        "degraded_reason": degraded_reason,
        "adapter": adapter_name,
        "repo_check_bypass_used": False,
        "evidence_source": evidence_source,
        "failure_counts": dict(merge_status.get("failure_counts", {})) if merge_status else {},
        "diagnostics": dict(merge_status.get("diagnostics", {})) if merge_status else {},
        "planned_task_count": planned_task_count,
        "runnable_task_count": runnable_task_count,
        "max_scheduled_concurrency": max_scheduled_concurrency,
        "needs_serial_handoff": needs_serial_handoff,
        "artifacts": {
            "parallel_orchestration_status": str(run_dir / "parallel_orchestration_status.json"),
            "research_tasks": str(run_dir / RESEARCH_TASKS_FILENAME),
            "subagent_assignments": str(run_dir / ASSIGNMENTS_FILENAME),
            "merge_status": str(run_dir / MERGE_STATUS_FILENAME),
            "evidence": str(run_dir / "evidence.json"),
            "run_trace": str(trace_path(run_dir)),
        },
    }
    if merge_status is not None:
        merge = dict(merge_status)
        merge.setdefault("evidence_source", evidence_source)
        payload["merge"] = merge
    if skip_reason:
        payload["skip_reason"] = skip_reason
    if errors:
        payload["errors"] = [dict(error) for error in errors]
    add_run_steps_artifact(payload, run_dir)
    return payload


def _parallel_evidence_source(
    *,
    adapter_name: str,
    parallel_degraded: bool,
    accepted_shards: Sequence[Mapping[str, Any]],
    attempted_real_child_execution: bool = False,
) -> dict[str, Any]:
    accepted_count = len(accepted_shards)
    if adapter_name == "fixture":
        source_type = "fixture"
        description = "deterministic no-network fixture shards"
    elif adapter_name == "codex-exec":
        if accepted_count == 0 and not attempted_real_child_execution:
            source_type = "blocked_parallel_execution"
            description = "real Codex child execution was blocked before launch"
        elif accepted_count > 0 and not parallel_degraded:
            attempted_real_child_execution = True
            source_type = "real_child_execution"
            description = "accepted evidence shards from real Codex child execution"
        else:
            attempted_real_child_execution = True
            source_type = "failed_real_child_execution"
            description = "real Codex child execution was attempted, but no evidence shard was accepted"
    elif adapter_name == "serial-degraded" or parallel_degraded:
        source_type = "serial_handoff"
        description = "serial degraded handoff after parallel execution could not provide accepted shards"
    else:
        source_type = "unknown"
        description = "parallel evidence source could not be classified"
    return {
        "type": source_type,
        "adapter": adapter_name,
        "accepted_shards": accepted_count,
        "fixture_only": source_type == "fixture",
        "manual_handoff": False,
        "attempted_real_child_execution": attempted_real_child_execution,
        "real_child_execution": source_type == "real_child_execution",
        "real_use_e2e_eligible": (
            source_type == "real_child_execution"
            and accepted_count > 0
            and not parallel_degraded
        ),
        "description": description,
    }


def _parallel_status_ok(status: str) -> bool:
    return status not in {
        "failed",
        "failed_validation",
        "failed_parallel_no_accepted_shards",
        "blocked_parallel_execution",
    }


def _parallel_status_value(
    *,
    requested_adapter_name: str,
    final_adapter_name: str,
    merge_status: Mapping[str, Any],
    planned_task_count: int,
    parallel_degraded: bool,
    attempted_real_child_execution: bool,
) -> str:
    if merge_status.get("status") != "completed":
        return "failed_validation"
    accepted_shards = _list(merge_status.get("accepted_shards"))
    accepted_count = len(accepted_shards)
    failure_counts = merge_status.get("failure_counts")
    has_failures = False
    if isinstance(failure_counts, Mapping):
        has_failures = any(
            int(failure_counts.get(key) or 0) > 0
            for key in ("failed_tasks", "blocked_tasks", "rejected_shards", "discarded_tasks")
        )
    if final_adapter_name == "fixture":
        return "completed_fixture" if accepted_count > 0 else "failed_parallel_no_accepted_shards"
    if final_adapter_name == "serial-degraded" or parallel_degraded:
        if requested_adapter_name in {"serial-degraded", "serial-fallback"} and accepted_count == 0:
            return "blocked_parallel_execution"
        return "degraded_serial_handoff_required"
    if (
        final_adapter_name == "codex-exec"
        and attempted_real_child_execution
        and accepted_count == 0
    ):
        if _merge_has_parallel_execution_blocker(merge_status):
            return "blocked_parallel_execution"
        return "failed_parallel_no_accepted_shards"
    if final_adapter_name == "codex-exec" and accepted_count > 0:
        if accepted_count < planned_task_count or has_failures:
            return "completed_partial_parallel"
        return "completed_parallel"
    return "completed_partial_parallel" if accepted_count > 0 else "failed_parallel_no_accepted_shards"


def _needs_serial_handoff(
    *,
    status: str,
    accepted_shards: Sequence[Mapping[str, Any]],
    parallel_degraded: bool,
) -> bool:
    if status in {
        "degraded_serial_handoff_required",
        "failed_parallel_no_accepted_shards",
        "blocked_parallel_execution",
    }:
        return True
    if status == "completed_serial_handoff":
        return False
    return bool(parallel_degraded or not accepted_shards)


def _merge_has_parallel_execution_blocker(merge_status: Mapping[str, Any]) -> bool:
    failed_tasks = _list(merge_status.get("failed_tasks"))
    blocked_tasks = _list(merge_status.get("blocked_tasks"))
    observed_parts: list[str] = []
    for task in failed_tasks:
        observed_parts.append(str(task.get("failure_category") or ""))
        summary = task.get("stdout_stderr_summary")
        if isinstance(summary, Mapping):
            observed_parts.extend(str(value) for value in summary.values())
    for task in blocked_tasks:
        observed_parts.append(str(task.get("failure_category") or ""))
        observed_parts.append(str(task.get("reason") or ""))
    blocker_text = " ".join(observed_parts).lower()
    markers = (
        "adapter_unavailable",
        "auth",
        "login",
        "credential",
        "sandbox",
        "approval",
        "permission",
        "not inside a trusted directory",
    )
    return any(marker in blocker_text for marker in markers)


def _skip_serial_handoff_after_parallel(
    run_dir: Path,
    status_payload: Mapping[str, Any],
) -> None:
    stages = ["ingest", "fetch_claims"]
    adapter = str(status_payload.get("adapter") or "").strip().lower().replace("_", "-")
    if adapter != "codex-exec" or not _run_has_visual_routes(run_dir):
        stages.append("ingest_vision")
    for stage in stages:
        try:
            skip_stage(
                run_dir,
                stage,
                reason="parallel_orchestration_completed",
            )
        except Exception:
            continue


def _run_has_visual_routes(run_dir: Path) -> bool:
    evidence = _read_optional_json(run_dir / "evidence.json")
    routing = evidence.get("routing") if isinstance(evidence, Mapping) else None
    if not isinstance(routing, list):
        return False
    for route in routing:
        if not isinstance(route, Mapping) or route.get("modality") == "text_only":
            continue
        try:
            max_images = int(route.get("max_images") or 0)
        except (TypeError, ValueError):
            max_images = 0
        if max_images > 0:
            return True
    return False


def _enforce_parallel_budget_gate(
    *,
    run_dir: Path,
    preset_name: str,
    budget: Mapping[str, Any],
    confirm_exhaustive: bool,
    max_cost_usd: float | None,
) -> None:
    if preset_name != "exhaustive":
        return
    budget_estimate = _read_optional_json(run_dir / "budget_estimate.json") or {}
    confirmation_provided = confirm_exhaustive or bool(
        _nested_get(budget_estimate, ("confirmation", "provided"))
    )
    effective_cost_cap = max_cost_usd
    if effective_cost_cap is None:
        budget_cost = budget.get("max_cost_usd")
        if isinstance(budget_cost, (int, float)):
            effective_cost_cap = float(budget_cost)
    if effective_cost_cap is None:
        estimate_cost = _nested_get(budget_estimate, ("effective_caps", "max_cost_usd"))
        if isinstance(estimate_cost, (int, float)):
            effective_cost_cap = float(estimate_cost)
    if not confirmation_provided:
        raise ParallelOrchestrationError(
            "exhaustive parallel orchestration requires explicit confirmation"
        )
    if effective_cost_cap is None or effective_cost_cap <= 0:
        raise ParallelOrchestrationError(
            "exhaustive parallel orchestration requires a positive max_cost_usd cap"
        )


def _nested_get(payload: Mapping[str, Any], keys: Sequence[str]) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


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
    task["last_adapter"] = adapter_name
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
    task["last_child_thread_id"] = result.child_thread_id
    command_context = _last_command_context(result.events)
    if command_context:
        task["last_command_context"] = command_context
    for event in result.events:
        trace_record = _trace_record(run_dir, event)
        append_trace_record(run_dir, trace_record)
        trace_ids.append(trace_record["event_id"])
    task["trace_event_ids"] = trace_ids
    if result.status == "completed" and result.shard_path and Path(result.shard_path).exists():
        validation = validate_artifacts(evidence_path=result.shard_path)
        if not validation.valid:
            task["failure_category"] = "invalid_shard"
            task["validation"] = validation.to_dict()
            if int(task.get("attempt") or 0) < int(task.get("max_attempts") or 1):
                task["state"] = "retryable"
            else:
                task["state"] = "failed"
            return
        task["state"] = "completed"
        task["failure_category"] = None
        task.pop("validation", None)
        return
    if result.status == "blocked":
        task["state"] = "blocked"
        task["failure_category"] = result.failure_category or "adapter_unavailable"
        task["blocked_reason"] = result.message or "parallel execution unavailable"
        return
    failure = result.failure_category or "missing_shard"
    task["failure_category"] = failure
    if result.message:
        task["last_error"] = result.message
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
    visual_supports: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    if int(task.get("max_images") or 0) > 0:
        claim_type = "mixed"
        image_id = f"img_{task['id']}_001"
        observation_text = f"Fixture visual observation for {task['query']}"
        supporting_images.append(image_id)
        visual_supports.append(
            {
                "image_id": image_id,
                "observation_ref": f"images.{image_id}.observations[0]",
                "observation_index": 0,
                "observation_text": observation_text,
                "relation_type": "screenshot_support",
                "provider": str(evidence.get("vlm_provider", "codex-interactive")),
                "rationale": "Linked because shard claim and image cite the same fixture source.",
                "confidence": 0.74,
            }
        )
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
                "observations": [observation_text],
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
                "visual_supports": visual_supports,
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


def _resolve_task_shard_path(task: Mapping[str, Any], run_dir: Path) -> Path:
    run_root = run_dir.resolve()
    raw_path = task.get("output_shard_path")
    task_id = str(task.get("id") or "unknown")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ParallelOrchestrationError(
            f"invalid output_shard_path for task_id={task_id}: missing; "
            f"run_dir={run_root}; output shard paths must be relative and stay under run_dir"
        )
    relative_path = Path(raw_path)
    if relative_path.is_absolute():
        raise ParallelOrchestrationError(
            f"invalid output_shard_path for task_id={task_id}: {raw_path}; "
            f"run_dir={run_root}; output shard paths must be relative and stay under run_dir"
        )
    resolved_path = (run_root / relative_path).resolve()
    try:
        resolved_path.relative_to(run_root)
    except ValueError as exc:
        raise ParallelOrchestrationError(
            f"invalid output_shard_path for task_id={task_id}: {raw_path}; "
            f"resolved_path={resolved_path}; run_dir={run_root}; "
            "output shard paths must be relative and stay under run_dir"
        ) from exc
    return resolved_path


def _invalid_output_shard_result(
    *,
    adapter_name: str,
    task: Mapping[str, Any],
    child_thread_id: str,
    message: str,
) -> RunnerResult:
    event = _codex_event(
        "wait",
        task,
        child_thread_id=child_thread_id,
        child_status="failed",
        child_message=message,
        failure_category="missing_shard",
        raw_event={
            "adapter": adapter_name,
            "task_id": str(task.get("id") or "unknown"),
            "output_shard_path": task.get("output_shard_path"),
            "diagnostic": message,
        },
    )
    return RunnerResult(
        task_id=str(task.get("id") or "unknown"),
        status="failed",
        child_thread_id=child_thread_id,
        events=(event,),
        failure_category="missing_shard",
        message=message,
    )


def _child_prompt(task: Mapping[str, Any], *, run_dir: Path, shard_path: Path | None = None) -> str:
    shard_path = shard_path or _resolve_task_shard_path(task, run_dir)
    shard_dir = shard_path.parent
    search_results_path = shard_dir / "search_results.jsonl"
    visual_observations_path = shard_dir / "visual_observations.jsonl"
    verifier_votes_path = shard_dir / "verifier_votes.jsonl"
    query = str(task.get("query") or "")
    response_language = "Korean" if _contains_korean(query) else "English"
    try:
        max_images = max(0, int(task.get("max_images") or 0))
    except (TypeError, ValueError):
        max_images = 0
    visual_instruction = ""
    if max_images > 0:
        visual_instruction = (
            f"Because this visual task requests up to {max_images} images, discover and write as many "
            f"public HTTP(S) image_url records as the task supports, targeting {max_images} when available. "
            "Prefer direct NASA, Wikimedia Commons, or other source-hosted image URLs over pages that only "
            "describe images. Each image record must reference a source_id present in the same shard, use "
            "origin `image_search` or `page_image`, set local_artifact_path to a placeholder under "
            f"`{EVIDENCE_SHARDS_DIRNAME}/<task_id>/` when the child has not downloaded the binary, set "
            "analysis_provider `codex-interactive`, analysis_status `skipped`, and leave observations and "
            "inferences empty for later runner VLM analysis. Do not use user-uploaded, manual, login-walled, "
            "paywalled, CAPTCHA-gated, DRM-restricted, or robots-disallowed images. "
        )
    return (
        "Run this bounded research shard task and write only schema-valid "
        f"evidence to {shard_path}. "
        "First create the shard directory if needed and write a minimal valid `evidence_shard.json` "
        f"to {shard_path} before any optional sidecars; keep replacing it with richer valid evidence as you proceed. "
        "If you use inline scripts for local JSON or file manipulation, invoke them with `python3`, not `python`. "
        f"Write claim text, caveats, rationales, and synthesized source snippets in {response_language}; "
        "for Korean queries, translate/summarize English source findings into Korean user-facing prose. "
        "Only direct quote_spans.quote values should remain verbatim in the source language. "
        "Prioritize a compact shard with decision-ready claims and do not read repository docs or skills unless the schema is otherwise impossible to satisfy. "
        "Use `vlm_provider` exactly `codex-interactive` unless the input evidence specifies another allowed value. "
        f"{visual_instruction}"
        "Every source must include a non-empty `local_artifact_path`, such as `evidence_shards/<task_id>/source_001.html`. "
        "Verifier vote `method` must be one of `codex-subagent`, `runner-agent`, `model-call`, or `manual-review`; "
        "`vote` must be one of `support`, `refute`, `uncertain`, or `blocked`; "
        "`evidence_refs` must reference only source or image IDs present in the same shard. "
        f"Write per-task search results exactly to {search_results_path}. "
        f"Write visual observations exactly to {visual_observations_path} when applicable. "
        f"Write verifier votes exactly to {verifier_votes_path} when applicable. "
        f"Do not write sidecars outside {shard_dir}. "
        f"Task JSON: {json.dumps(dict(task), sort_keys=True)}"
    )


def _timeout_after_valid_shard_context(*, shard_path: Path) -> dict[str, Any]:
    sidecars = _expected_sidecar_status(shard_path.parent)
    missing = [
        record["filename"]
        for record in sidecars.values()
        if record.get("exists") is not True
    ]
    return {
        "timeout_after_valid_shard": True,
        "valid_evidence_shard_exists": True,
        "valid_evidence_shard_path": str(shard_path),
        "expected_sidecars": sidecars,
        "missing_expected_sidecars": missing,
        "missing_expected_sidecar_paths": [
            record["path"]
            for record in sidecars.values()
            if record.get("exists") is not True
        ],
    }


def _expected_sidecar_status(shard_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "filename": filename,
            "path": str(shard_dir / filename),
            "exists": (shard_dir / filename).exists(),
        }
        for key, filename in EXPECTED_CHILD_SIDECARS.items()
    }


def _contains_korean(value: str) -> bool:
    return any("\uac00" <= char <= "\ud7a3" for char in value)


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _codex_exec_child_artifact_paths(*, run_dir: Path, task_id: str) -> dict[str, Path]:
    safe_task_id = _artifact_safe_task_id(task_id)
    artifact_dir = run_dir / CHILD_EVENTS_DIRNAME / safe_task_id
    return {
        "stdout_jsonl_path": artifact_dir / CODEX_EXEC_STDOUT_FILENAME,
        "stderr_path": artifact_dir / CODEX_EXEC_STDERR_FILENAME,
        "last_child_event_path": artifact_dir / LAST_CHILD_EVENT_FILENAME,
    }


def _artifact_safe_task_id(task_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("._")
    return safe or "unknown"


def _stringify_paths(paths: Mapping[str, Path]) -> dict[str, str]:
    return {key: str(path) for key, path in paths.items()}


def _timeout_stdout(exc: subprocess.TimeoutExpired) -> Any:
    stdout = getattr(exc, "stdout", None)
    if stdout is not None:
        return stdout
    return getattr(exc, "output", None)


def _write_codex_exec_child_diagnostics(
    *,
    task: Mapping[str, Any],
    artifacts: Mapping[str, Path],
    stdout: Any = None,
    stderr: Any = None,
    timeout: bool = False,
    timeout_seconds: float | None = None,
    returncode: int | None = None,
    os_error: str | None = None,
) -> dict[str, Any]:
    stdout_text = _coerce_output_text(stdout)
    stderr_text = _coerce_output_text(stderr)
    for path in artifacts.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    artifacts["stdout_jsonl_path"].write_text(stdout_text, encoding="utf-8")
    artifacts["stderr_path"].write_text(stderr_text, encoding="utf-8")
    events, parse_errors = _parse_json_events_with_errors(stdout_text)
    summary = _last_child_event_summary(
        task=task,
        events=events,
        parse_errors=parse_errors,
        artifacts=artifacts,
        timeout=timeout,
        timeout_seconds=timeout_seconds,
        returncode=returncode,
        os_error=os_error,
    )
    _write_json(artifacts["last_child_event_path"], summary)
    return summary


def _last_child_event_summary(
    *,
    task: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    parse_errors: int,
    artifacts: Mapping[str, Path],
    timeout: bool,
    timeout_seconds: float | None,
    returncode: int | None,
    os_error: str | None,
) -> dict[str, Any]:
    last_event = events[-1] if events else {}
    command_event, last_command = _last_extracted_value(events, _extract_command)
    tool_event, last_tool_name = _last_extracted_value(events, _extract_tool_name)
    tool_call_event, last_tool_call_id = _last_extracted_value(events, _extract_tool_call_id)
    message_event, last_message_text = _last_extracted_value(events, _extract_message_text)
    command_status = _extract_status(command_event or {}) or _extract_status(last_event)
    return {
        "schema_version": PARALLEL_SCHEMA_VERSION,
        "task_id": str(task.get("id") or "unknown"),
        "artifact_kind": "codex_exec_child_diagnostics",
        "artifacts": _stringify_paths(artifacts),
        "total_json_events": len(events),
        "parse_errors": parse_errors,
        "last_event_type": _extract_event_type(last_event),
        "last_item_type": _extract_item_type(last_event),
        "last_command": _bounded_preview(last_command),
        "last_command_status": _bounded_preview(command_status),
        "last_tool_name": _bounded_preview(last_tool_name),
        "last_tool_call_id": _bounded_preview(last_tool_call_id),
        "last_message_text_preview": _bounded_preview(last_message_text),
        "last_message_event_type": _extract_event_type(message_event or {}),
        "last_tool_event_type": _extract_event_type(tool_event or {}),
        "last_tool_call_event_type": _extract_event_type(tool_call_event or {}),
        "timeout": timeout,
        "timeout_seconds": timeout_seconds,
        "returncode": returncode,
        "os_error": os_error,
    }


def _last_extracted_value(
    events: Sequence[Mapping[str, Any]],
    extractor: Any,
) -> tuple[Mapping[str, Any] | None, Any]:
    for event in reversed(events):
        value = extractor(event)
        if value not in (None, ""):
            return event, value
    return None, None


def _extract_event_type(event: Mapping[str, Any]) -> str | None:
    value = event.get("event_type") or event.get("event") or event.get("type")
    if value:
        return str(value)
    msg = event.get("msg")
    if isinstance(msg, Mapping):
        msg_type = msg.get("type")
        if msg_type:
            return str(msg_type)
    return None


def _extract_item_type(event: Mapping[str, Any]) -> str | None:
    item = event.get("item")
    if isinstance(item, Mapping) and item.get("type"):
        return str(item["type"])
    value = event.get("item_type")
    if value:
        return str(value)
    msg = event.get("msg")
    if isinstance(msg, Mapping):
        return _extract_item_type(msg)
    return None


def _extract_command(event: Mapping[str, Any]) -> Any:
    direct = _first_mapping_value(event, ("command", "cmd"))
    if direct:
        return direct
    arguments = event.get("arguments")
    command = _command_from_arguments(arguments)
    if command:
        return command
    for key in ("item", "tool_call", "call", "function", "msg"):
        value = event.get(key)
        if isinstance(value, Mapping):
            command = _extract_command(value)
            if command:
                return command
    return None


def _command_from_arguments(arguments: Any) -> Any:
    if isinstance(arguments, Mapping):
        return _first_mapping_value(arguments, ("command", "cmd"))
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, Mapping):
            return _first_mapping_value(parsed, ("command", "cmd"))
    return None


def _extract_status(event: Mapping[str, Any]) -> str | None:
    status = _first_mapping_value(event, ("status", "command_status", "result_status", "outcome"))
    if status:
        return str(status)
    for key in ("item", "tool_call", "call", "msg"):
        value = event.get(key)
        if isinstance(value, Mapping):
            nested = _extract_status(value)
            if nested:
                return nested
    return None


def _extract_tool_name(event: Mapping[str, Any]) -> str | None:
    value = _first_mapping_value(event, ("tool_name", "tool"))
    if isinstance(value, Mapping):
        name = value.get("name")
        return str(name) if name else None
    if value:
        return str(value)
    event_type = str(event.get("type") or event.get("event") or "").lower()
    name = event.get("name")
    if name and _event_type_has_tool_name(event_type):
        return str(name)
    for key in ("item", "tool_call", "call", "function", "msg"):
        nested = event.get(key)
        if isinstance(nested, Mapping):
            value = _extract_tool_name(nested)
            if value:
                return value
    return None


def _event_type_has_tool_name(event_type: str) -> bool:
    return "tool" in event_type or "function_call" in event_type


def _extract_tool_call_id(event: Mapping[str, Any]) -> str | None:
    value = _first_mapping_value(event, ("tool_call_id", "call_id"))
    if value:
        return str(value)
    event_type = str(event.get("type") or event.get("event") or "").lower()
    identifier = event.get("id")
    if identifier and "tool" in event_type:
        return str(identifier)
    for key in ("item", "tool_call", "call", "function", "msg"):
        nested = event.get(key)
        if isinstance(nested, Mapping):
            value = _extract_tool_call_id(nested)
            if value:
                return value
    return None


def _extract_message_text(event: Mapping[str, Any]) -> Any:
    value = _first_mapping_value(event, ("message", "text", "content", "summary"))
    if value:
        return value
    for key in ("item", "msg", "delta"):
        nested = event.get(key)
        if isinstance(nested, Mapping):
            value = _extract_message_text(nested)
            if value:
                return value
    return None


def _first_mapping_value(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _bounded_preview(value: Any, *, limit: int = 300) -> str | None:
    if value is None:
        return None
    return _output_summary(value, limit=limit)


def _with_child_event_artifacts(
    event: Mapping[str, Any],
    artifacts: Mapping[str, Path],
) -> dict[str, Any]:
    raw_event = dict(event)
    raw_event["child_event_artifacts"] = _stringify_paths(artifacts)
    return raw_event


def _codex_exec_command_context(
    *,
    adapter_name: str,
    task: Mapping[str, Any],
    command: Sequence[str],
    cwd: Path,
    run_dir: Path,
    shard_path: Path,
) -> dict[str, Any]:
    command_without_prompt = list(command[:-1]) + ["<prompt>"]
    return {
        "adapter": adapter_name,
        "task_id": str(task.get("id") or "unknown"),
        "cwd": str(cwd),
        "trusted_project_root": str(cwd),
        "run_dir": str(run_dir),
        "output_shard_path": str(shard_path),
        "command": command_without_prompt,
        "command_string": shlex.join(command_without_prompt),
        "repo_check_bypass_used": False,
        "retryable": True,
    }


def _codex_exec_failure_message(
    *,
    command_context: Mapping[str, Any],
    cause: str,
    stdout: Any = None,
    stderr: Any = None,
) -> str:
    stdout_summary = _output_summary(stdout)
    stderr_summary = _output_summary(stderr)
    observed = " ".join(part for part in (stderr_summary, stdout_summary) if part)
    remediation = (
        "Run codex-exec from a trusted project root with -C and keep --add-dir pointed at "
        "the selected run directory; --skip-git-repo-check is diagnostic-only and cannot "
        "satisfy real-use E2E acceptance."
    )
    if "not inside a trusted directory" in observed.lower():
        remediation = (
            "Trust the project root or pass -C to a trusted git checkout; do not count "
            "--skip-git-repo-check bypass runs as passing real-use E2E."
        )
    return (
        f"{cause}; adapter={command_context.get('adapter')}; "
        f"task_id={command_context.get('task_id')}; "
        f"cwd={command_context.get('cwd')}; "
        f"trusted_project_root={command_context.get('trusted_project_root')}; "
        f"run_dir={command_context.get('run_dir')}; "
        f"output_shard_path={command_context.get('output_shard_path')}; "
        f"child_event_artifacts={json.dumps(command_context.get('child_event_artifacts') or {}, sort_keys=True)}; "
        f"command={command_context.get('command_string')}; "
        f"repo_check_bypass_used={command_context.get('repo_check_bypass_used')}; "
        f"retryable={command_context.get('retryable')}; "
        f"stderr={stderr_summary or '<empty>'}; stdout={stdout_summary or '<empty>'}; "
        f"remediation={remediation}"
    )


def _append_shard_validation_context(message: str, shard_path: Path) -> str:
    if not shard_path.exists():
        return f"{message}; shard_status=missing; shard_path={shard_path}"
    validation = validate_artifacts(evidence_path=shard_path)
    return (
        f"{message}; shard_status=invalid; shard_path={shard_path}; "
        f"shard_validation={json.dumps(validation.to_dict(), sort_keys=True)}"
    )


def _append_timeout_after_valid_shard_context(
    message: str,
    context: Mapping[str, Any],
) -> str:
    return (
        f"{message}; timeout_after_valid_shard=True; "
        f"valid_evidence_shard_exists=True; "
        "missing_expected_sidecars="
        f"{json.dumps(context.get('missing_expected_sidecars') or [], sort_keys=True)}; "
        "missing_expected_sidecar_paths="
        f"{json.dumps(context.get('missing_expected_sidecar_paths') or [], sort_keys=True)}"
    )


def _output_summary(value: Any, *, limit: int = 700) -> str:
    text = _coerce_output_text(value)
    return " ".join(text.split())[:limit]


def _coerce_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _parse_json_events(stdout: str) -> list[dict[str, Any]]:
    events, _parse_errors = _parse_json_events_with_errors(stdout)
    return events


def _parse_json_events_with_errors(stdout: Any) -> tuple[list[dict[str, Any]], int]:
    text = _coerce_output_text(stdout)
    events: list[dict[str, Any]] = []
    parse_errors = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            parse_errors += 1
    return events, parse_errors


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
        "not inside a trusted directory",
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
    used_source_ids: set[str],
    used_image_ids: set[str],
    used_claim_ids: set[str],
) -> None:
    for source in _list(evidence.get("sources")):
        source_id = str(source.get("id") or "")
        if source_id:
            source_by_url[_source_key(source)] = source_id
            used_source_ids.add(source_id)
    for image in _list(evidence.get("images")):
        image_id = str(image.get("id") or "")
        if image_id:
            image_by_hash[_image_key(image)] = image_id
            used_image_ids.add(image_id)
    for claim in _list(evidence.get("claims")):
        claim_id = str(claim.get("id") or "")
        if claim_id:
            claim_by_text[_claim_key(claim)] = claim_id
            used_claim_ids.add(claim_id)


def _canonical_artifact_id(
    preferred_id: str,
    used_ids: set[str],
    *,
    prefix: str,
    task_id: str,
) -> str:
    preferred = _sanitize_id(preferred_id)
    if preferred and preferred not in used_ids:
        used_ids.add(preferred)
        return preferred
    task_part = _sanitize_id(task_id) or "task"
    local_part = preferred or "artifact"
    base = f"{prefix}_{task_part}_{local_part}"
    candidate = base
    suffix = 2
    while candidate in used_ids:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def _sanitize_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


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
    visual_supports = []
    for visual_support in claim_copy.get("visual_supports", []):
        if not isinstance(visual_support, Mapping):
            continue
        support_copy = dict(visual_support)
        image_id = str(support_copy.get("image_id"))
        remapped_image_id = id_map.get(image_id, image_id)
        support_copy["image_id"] = remapped_image_id
        observation_index = support_copy.get("observation_index")
        if isinstance(observation_index, int):
            support_copy["observation_ref"] = (
                f"images.{remapped_image_id}.observations[{observation_index}]"
            )
        visual_supports.append(support_copy)
    if visual_supports:
        claim_copy["visual_supports"] = visual_supports
    votes = []
    for vote in claim_copy.get("votes", []):
        if not isinstance(vote, Mapping):
            continue
        vote_copy = dict(vote)
        vote_copy["evidence_refs"] = [
            id_map.get(str(evidence_ref), str(evidence_ref))
            for evidence_ref in vote_copy.get("evidence_refs", [])
        ]
        votes.append(vote_copy)
    claim_copy["votes"] = votes
    return claim_copy


def _claim_is_mergeable(claim: Mapping[str, Any]) -> bool:
    if claim.get("verification_status") in {"policy_blocked", "refuted"}:
        return False
    if claim.get("review_status") == "human_rejected":
        return False
    return bool(claim.get("supporting_sources") or claim.get("supporting_images"))


def _task_status_record(task: Mapping[str, Any], reason: Any) -> dict[str, Any]:
    preserved_failure = task.get("parallel_failure")
    if not isinstance(preserved_failure, Mapping):
        preserved_failure = {}
    command_context = preserved_failure.get("command_context") or task.get("last_command_context")
    if not isinstance(command_context, Mapping):
        command_context = {}
    diagnostic = str(
        preserved_failure.get("diagnostic")
        or reason
        or task.get("last_error")
        or task.get("blocked_reason")
        or ""
    )
    failure_category = str(
        preserved_failure.get("failure_category")
        or task.get("failure_category")
        or reason
        or "blocked"
    )
    state = str(task.get("state") or "blocked")
    stdout_stderr_summary = preserved_failure.get("stdout_stderr_summary")
    if not isinstance(stdout_stderr_summary, Mapping):
        stdout_stderr_summary = _stdout_stderr_summary(diagnostic)
    adapter = preserved_failure.get("adapter") or task.get("last_adapter") or _adapter_from_assignment(task)
    retryable = preserved_failure.get("retryable")
    if not isinstance(retryable, bool):
        retryable = failure_category in RETRY_SAFE_FAILURES
    record = {
        "task_id": task.get("id"),
        "state": state,
        "reason": reason,
        "output_shard_path": task.get("output_shard_path"),
        "adapter": adapter,
        "failure_category": failure_category,
        "retryable": retryable,
        "attempt": int(task.get("attempt") or 0),
        "max_attempts": int(task.get("max_attempts") or 0),
        "child_thread_id": preserved_failure.get("child_thread_id") or task.get("last_child_thread_id"),
        "assigned_subagent_id": (
            preserved_failure.get("assigned_subagent_id") or task.get("assigned_subagent_id")
        ),
        "working_dir": str(preserved_failure.get("working_dir") or command_context.get("cwd") or ""),
        "diagnostic": diagnostic,
        "stdout_stderr_summary": dict(stdout_stderr_summary),
        "command_context": dict(command_context),
    }
    if preserved_failure:
        record["serial_fallback"] = {
            "adapter": task.get("last_adapter") or _adapter_from_assignment(task),
            "assigned_subagent_id": task.get("assigned_subagent_id"),
            "blocked_reason": task.get("blocked_reason"),
        }
    return record


def _task_failure_record(task: Mapping[str, Any]) -> dict[str, Any]:
    diagnostic = str(task.get("last_error") or task.get("blocked_reason") or "")
    command_context = task.get("last_command_context")
    if not isinstance(command_context, Mapping):
        command_context = {}
    failure_category = str(task.get("failure_category") or "unknown_failure")
    state = str(task.get("state") or "failed")
    return {
        "task_id": task.get("id"),
        "state": state,
        "adapter": task.get("last_adapter") or _adapter_from_assignment(task),
        "failure_category": failure_category,
        "retryable": state == "retryable" or failure_category in RETRY_SAFE_FAILURES,
        "attempt": int(task.get("attempt") or 0),
        "max_attempts": int(task.get("max_attempts") or 0),
        "child_thread_id": task.get("last_child_thread_id"),
        "assigned_subagent_id": task.get("assigned_subagent_id"),
        "output_shard_path": task.get("output_shard_path"),
        "working_dir": str(command_context.get("cwd") or ""),
        "diagnostic": diagnostic,
        "stdout_stderr_summary": _stdout_stderr_summary(diagnostic),
        "command_context": dict(command_context),
    }


def _preserve_parallel_failure(task: dict[str, Any]) -> None:
    if isinstance(task.get("parallel_failure"), Mapping):
        return
    task["parallel_failure"] = _task_failure_record(task)


def _adapter_from_assignment(task: Mapping[str, Any]) -> str | None:
    assigned = task.get("assigned_subagent_id")
    if not isinstance(assigned, str) or not assigned:
        return None
    marker = f"-{task.get('id')}-attempt-"
    if marker in assigned:
        return assigned.split(marker, 1)[0]
    return assigned.split("-", 1)[0]


def _last_command_context(events: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        raw_event = event.get("raw_event")
        if isinstance(raw_event, Mapping) and raw_event.get("command"):
            return dict(raw_event)
    return None


def _stdout_stderr_summary(diagnostic: str) -> dict[str, str]:
    summary: dict[str, str] = {}
    for key in ("stderr", "stdout"):
        match = re.search(rf"{key}=([^;]*)", diagnostic)
        if match:
            summary[key] = match.group(1).strip()
    return summary


def _failure_counts(
    *,
    failed_tasks: Sequence[Mapping[str, Any]],
    blocked_tasks: Sequence[Mapping[str, Any]],
    rejected_shards: Sequence[Mapping[str, Any]],
    discarded_tasks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_category: dict[str, int] = {}
    for task in failed_tasks:
        category = str(task.get("failure_category") or "unknown_failure")
        by_category[category] = by_category.get(category, 0) + 1
    for task in blocked_tasks:
        category = str(task.get("failure_category") or task.get("reason") or "blocked")
        by_category[category] = by_category.get(category, 0) + 1
    for shard in rejected_shards:
        category = str(shard.get("reason") or "rejected_shard")
        by_category[category] = by_category.get(category, 0) + 1
    for task in discarded_tasks:
        category = str(task.get("reason") or "discarded")
        by_category[category] = by_category.get(category, 0) + 1
    return {
        "failed_tasks": len(failed_tasks),
        "blocked_tasks": len(blocked_tasks),
        "rejected_shards": len(rejected_shards),
        "discarded_tasks": len(discarded_tasks),
        "by_category": by_category,
    }


def _merge_diagnostics(
    *,
    accepted_shards: Sequence[Mapping[str, Any]],
    failed_tasks: Sequence[Mapping[str, Any]],
    blocked_tasks: Sequence[Mapping[str, Any]],
    rejected_shards: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if accepted_shards:
        return {}
    if failed_tasks:
        first = failed_tasks[0]
        return {
            "actionable_cause": "no evidence shards were accepted because child tasks failed",
            "first_failure_category": first.get("failure_category"),
            "first_failed_task_id": first.get("task_id"),
            "first_failed_adapter": first.get("adapter"),
            "first_failed_retryable": first.get("retryable"),
            "first_failed_diagnostic": first.get("diagnostic"),
        }
    if blocked_tasks:
        first = blocked_tasks[0]
        return {
            "actionable_cause": "no evidence shards were accepted because child tasks were blocked",
            "first_blocked_task_id": first.get("task_id"),
            "first_blocked_reason": first.get("reason"),
            "first_blocked_failure_category": first.get("failure_category"),
            "first_blocked_adapter": first.get("adapter"),
            "first_blocked_retryable": first.get("retryable"),
            "first_blocked_diagnostic": first.get("diagnostic"),
            "first_blocked_stdout_stderr_summary": first.get("stdout_stderr_summary"),
        }
    if rejected_shards:
        first = rejected_shards[0]
        return {
            "actionable_cause": "no evidence shards were accepted because shard validation rejected the output",
            "first_rejected_task_id": first.get("task_id"),
            "first_rejected_reason": first.get("reason"),
        }
    return {"actionable_cause": "no evidence shards were accepted"}


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


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


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
