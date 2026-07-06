"""Parallel Codex subagent orchestration artifacts and deterministic runner."""

from __future__ import annotations

import copy
import hashlib
import json
import random
import re
import shlex
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence_schema import (
    EVIDENCE_SCHEMA_VERSION,
    SEARCH_RESULT_TYPES,
    SEARCH_ROUTES,
    validate_artifacts,
)
from .execution_mode import BUDGET_PRESETS
from .run_state import add_run_steps_artifact, begin_stage, skip_stage, transition_stage
from .search_handoff import (
    SearchHandoffError,
    apply_release_validation_identity,
    release_validation_identity_from_payload,
    resolve_run_dir,
)
from .semantic_planner import (
    SEMANTIC_PLANNER_VALIDATION_FILENAME,
    write_semantic_planner_validation,
)
from .trace import TRACE_FAILURE_CATEGORIES, TRACE_SCHEMA_VERSION, append_trace_record, trace_path


PARALLEL_SCHEMA_VERSION = "codex-deepresearch.parallel-orchestration.v0"
RESEARCH_TASKS_FILENAME = "research_tasks.json"
ASSIGNMENTS_FILENAME = "subagent_assignments.jsonl"
MERGE_STATUS_FILENAME = "merge_status.json"
RELEASE_SEARCH_RESULT_REQUIRED_FIELDS = (
    "id",
    "task_id",
    "angle_id",
    "route",
    "provider",
    "provider_mode",
    "query",
    "url",
    "title",
    "snippet",
    "result_type",
    "rank",
    "accessed_at",
    "retrieval_status",
    "policy_decision",
    "prompt_id",
    "suite_id",
    "prompt_hash",
    "handoff_artifact",
)
EVIDENCE_SHARDS_DIRNAME = "evidence_shards"
CHILD_EVENTS_DIRNAME = "child_events"
CODEX_EXEC_STDOUT_FILENAME = "codex_exec_stdout.jsonl"
CODEX_EXEC_STDERR_FILENAME = "codex_exec_stderr.txt"
LAST_CHILD_EVENT_FILENAME = "last_child_event.json"
CODEX_CHILD_MODEL_CAPACITY = "codex_child_model_capacity"
CODEX_CHILD_TIMEOUT = "codex_child_timeout"
CODEX_CHILD_SCHEMA_INVALID = "codex_child_schema_invalid"
CODEX_CHILD_QUOTA_EXHAUSTED = "codex_child_quota_exhausted"
CODEX_CHILD_BILLING_DISABLED = "codex_child_billing_disabled"
CODEX_CHILD_AUTH_BLOCKED = "codex_child_auth_blocked"
CODEX_CHILD_SANDBOX_BLOCKED = "codex_child_sandbox_blocked"
CODEX_CHILD_POLICY_BLOCKED = "codex_child_policy_blocked"
CODEX_CHILD_PERMISSION_DENIED = "codex_child_permission_denied"
CODEX_CHILD_MISSING_SHARD = "codex_child_missing_shard"
CODEX_CHILD_EXEC_FAILED = "codex_child_exec_failed"
CODEX_CHILD_RELEASE_HANDOFF_INVALID = "codex_child_release_handoff_invalid"
FAILED_RELEASE_HANDOFF_INVALID = "failed_release_handoff_invalid"
CAPACITY_RETRY_MAX_ATTEMPTS = 3
DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS = 300.0
CAPACITY_RETRY_POLICY_DEFAULTS = {
    "max_attempts": CAPACITY_RETRY_MAX_ATTEMPTS,
    "initial_delay_seconds": 5.0,
    "backoff_multiplier": 2.0,
    "max_delay_seconds": 30.0,
    "jitter_ratio": 0.2,
}
EXPECTED_CHILD_SIDECARS = {
    "search_results": "search_results.jsonl",
    "visual_observations": "visual_observations.jsonl",
    "verifier_votes": "verifier_votes.jsonl",
}
EVIDENCE_SCHEMA_V0_REQUIRED_TOP_LEVEL_FIELDS = (
    "schema_version",
    "run_id",
    "created_at",
    "mode",
    "search_provider",
    "vlm_provider",
)
CHILD_ATTEMPT_PROBE_SCHEMA_VERSION = "codex-deepresearch.child-attempt-probe.v0"
REDACTED_CHILD_PROBE_SECRET = "<redacted-secret>"
CHILD_PROBE_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b("
    r"[A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?token|authorization|bearer|token|"
    r"secret|password|credential|credentials|client[_-]?secret|session[_-]?token)"
    r"[A-Za-z0-9_.-]*"
    r")\b\s*[:=]\s*([^\s,;]+)"
)
CHILD_PROBE_BEARER_PATTERN = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"
)
CHILD_PROBE_SECRET_FLAG_PATTERN = re.compile(
    r"(?i)(--(?:api[_-]?key|access[_-]?token|authorization|bearer|token|"
    r"secret|password|credential|client[_-]?secret|session[_-]?token))"
    r"(?:=|\s+)([^\s,;]+)"
)
RETRY_SAFE_FAILURES = {
    "adapter_unavailable",
    "codex_exec_failed",
    CODEX_CHILD_MODEL_CAPACITY,
    "invalid_shard",
    "invalid_release_search_handoff",
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
    expected_evidence: list[str]
    success_criteria: list[str]
    report_section: str
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
            attempt=int(task.get("attempt") or 1),
        )
        legacy_child_artifacts = _codex_exec_legacy_child_artifact_paths(
            run_dir=run_dir,
            task_id=str(task.get("id") or "unknown"),
        )
        command_context["child_event_artifacts"] = _stringify_paths(legacy_child_artifacts)
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
        child_started_at = _utc_now()
        child_started_monotonic = time.monotonic()
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
            child_timed_out_at = _utc_now()
            elapsed_seconds = _elapsed_seconds(child_started_monotonic)
            stdout = _timeout_stdout(exc)
            stderr = getattr(exc, "stderr", None)
            child_summary = _write_codex_exec_child_diagnostics(
                task=task,
                artifacts=child_artifacts,
                stdout=stdout,
                stderr=stderr,
                timeout=True,
                timeout_seconds=exc.timeout,
                child_started_at=child_started_at,
                child_timed_out_at=child_timed_out_at,
                elapsed_seconds=elapsed_seconds,
                child_thread_id=child_thread_id,
                output_shard_path=shard_path,
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
            child_finished_at = _utc_now()
            elapsed_seconds = _elapsed_seconds(child_started_monotonic)
            child_summary = _write_codex_exec_child_diagnostics(
                task=task,
                artifacts=child_artifacts,
                stdout=getattr(exc, "stdout", None),
                stderr=getattr(exc, "stderr", None),
                os_error=exc.__class__.__name__,
                timeout_seconds=self.timeout_seconds,
                child_started_at=child_started_at,
                child_finished_at=child_finished_at,
                elapsed_seconds=elapsed_seconds,
                child_thread_id=child_thread_id,
                output_shard_path=shard_path,
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

        child_finished_at = _utc_now()
        elapsed_seconds = _elapsed_seconds(child_started_monotonic)
        child_summary = _write_codex_exec_child_diagnostics(
            task=task,
            artifacts=child_artifacts,
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
            timeout_seconds=self.timeout_seconds,
            child_started_at=child_started_at,
            child_finished_at=child_finished_at,
            elapsed_seconds=elapsed_seconds,
            child_thread_id=child_thread_id,
            output_shard_path=shard_path,
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
                raw_event=_with_child_event_artifacts(event, legacy_child_artifacts),
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
                raw_event=command_context,
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
    existing = _read_research_tasks(run_dir)
    if existing:
        write_semantic_planner_validation(
            run_dir=run_dir,
            evidence=evidence,
            tasks=existing,
            report_status=_read_optional_json(run_dir / "report_status.json"),
        )
        return _tasks_payload(run_dir, existing, evidence=evidence, status="already_planned")

    planner_tasks = evidence.get("search_tasks")
    if not isinstance(planner_tasks, list) or not planner_tasks:
        raise ParallelOrchestrationError("evidence.json must include search_tasks before planning")

    semantic_floor = _semantic_task_floor(evidence, planner_tasks)
    hard_task_cap = min(preset.max_codex_handoff_tasks, 100)
    requested_cap = max_tasks if max_tasks is not None else hard_task_cap
    task_count = min(max(min_tasks, semantic_floor, 1), requested_cap, hard_task_cap)
    now = _utc_now()
    tasks: list[dict[str, Any]] = []
    angle_occurrences: dict[str, int] = {}
    for index in range(1, task_count + 1):
        base = planner_tasks[(index - 1) % len(planner_tasks)]
        if not isinstance(base, Mapping):
            continue
        task_id = f"task_research_{index:03d}"
        route = str(base.get("route") or "text_only")
        max_images = _planner_max_images(base, evidence=evidence, route=route)
        angle_id = str(base.get("angle_id") or f"angle_{index:03d}")
        angle_occurrences[angle_id] = int(angle_occurrences.get(angle_id, 0)) + 1
        query = _semantic_task_query(
            base,
            question=str(evidence.get("question") or ""),
            occurrence=angle_occurrences[angle_id],
        )
        task = ResearchTask(
            id=task_id,
            angle_id=angle_id,
            route=route,
            query=query,
            expected_evidence=_expected_evidence_for_task(base, route=route),
            success_criteria=_success_criteria_for_task(base),
            report_section=str(base.get("report_section") or base.get("angle") or "Findings"),
            state="queued",
            assigned_subagent_id=None,
            attempt=0,
            max_attempts=CAPACITY_RETRY_MAX_ATTEMPTS,
            max_sources=max(1, int(base.get("max_results") or 3)),
            max_images=max_images if route != "text_only" else 0,
            source_policy={"decision": "allowed", "flags": []},
            output_shard_path=f"{EVIDENCE_SHARDS_DIRNAME}/{task_id}/evidence_shard.json",
            trace_event_ids=[],
        ).to_dict()
        task["search_task_id"] = str(base.get("id") or task_id)
        apply_release_validation_identity(
            task,
            release_validation_identity_from_payload(evidence),
        )
        tasks.append(task)
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
    apply_release_validation_identity(
        payload,
        release_validation_identity_from_payload(evidence),
    )
    _write_json(run_dir / RESEARCH_TASKS_FILENAME, payload)
    write_semantic_planner_validation(
        run_dir=run_dir,
        evidence=evidence,
        tasks=tasks,
        report_status=_read_optional_json(run_dir / "report_status.json"),
    )
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


def _semantic_task_floor(
    evidence: Mapping[str, Any],
    planner_tasks: Sequence[Any],
) -> int:
    semantic_angles = evidence.get("semantic_angles")
    if isinstance(semantic_angles, list) and semantic_angles:
        return len([angle for angle in semantic_angles if isinstance(angle, Mapping)])
    return len([task for task in planner_tasks if isinstance(task, Mapping)])


_TASK_QUERY_VARIANTS = (
    "evidence map",
    "verification and caveats",
    "decision implications",
    "counterexamples and gaps",
)


def _semantic_task_query(
    task: Mapping[str, Any],
    *,
    question: str,
    occurrence: int,
) -> str:
    question_context = str(task.get("question_context") or question or "").strip()
    research_question = str(
        task.get("research_question")
        or task.get("query")
        or question
        or "research question"
    )
    title = str(task.get("title") or task.get("angle") or task.get("angle_id") or "angle")
    evidence_need = str(task.get("evidence_need") or "primary_source")
    report_section = str(task.get("report_section") or title)
    expected_artifacts = _string_list(task.get("expected_artifacts"))
    artifact_hint = expected_artifacts[(occurrence - 1) % len(expected_artifacts)] if expected_artifacts else "evidence notes"
    normalized = " ".join(research_question.split()) or "research question"
    context_prefix = (
        f"Original question context: {question_context}. "
        if question_context
        else ""
    )
    if occurrence <= 1:
        return f"{title}: {context_prefix}{normalized} Evidence need={evidence_need}; artifact={artifact_hint}."
    variant = _TASK_QUERY_VARIANTS[(occurrence - 2) % len(_TASK_QUERY_VARIANTS)]
    return (
        f"{title} / {variant}: {context_prefix}Angle focus: {normalized}. "
        f"Investigate {artifact_hint} for report section {report_section}. "
        f"Evidence need={evidence_need}; occurrence={occurrence:03d}."
    )


def _expected_evidence_for_task(task: Mapping[str, Any], *, route: str) -> list[str]:
    expected = _string_list(task.get("expected_evidence"))
    evidence_need = str(task.get("evidence_need") or "")
    if evidence_need and evidence_need not in expected:
        expected.insert(0, evidence_need)
    if route != "text_only" and "visual_observation" not in expected:
        expected.append("visual_observation")
    return list(dict.fromkeys(expected or ["primary_source"]))


def _success_criteria_for_task(task: Mapping[str, Any]) -> list[str]:
    criteria = _string_list(task.get("success_criteria"))
    if criteria:
        return criteria
    return [
        "Produce source-backed claims scoped to the parent semantic angle.",
        "Record caveats, counter-evidence, and artifact paths when available.",
    ]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


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
    effective_codex_exec_timeout_seconds = (
        float(adapter.timeout_seconds)
        if isinstance(adapter, CodexExecAdapter)
        else (
            float(codex_exec_timeout_seconds)
            if codex_exec_timeout_seconds is not None
            else None
        )
    )
    capacity_retry_policy = (
        _capacity_retry_policy(effective_codex_exec_timeout_seconds)
        if isinstance(adapter, CodexExecAdapter)
        else None
    )
    if capacity_retry_policy is not None:
        _apply_capacity_retry_policy(tasks, capacity_retry_policy)
        tasks_artifact["codex_exec_retry_policy"] = capacity_retry_policy
        tasks_artifact["codex_exec_timeout_seconds"] = effective_codex_exec_timeout_seconds
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
                codex_exec_timeout_seconds=effective_codex_exec_timeout_seconds,
                codex_exec_retry_policy=capacity_retry_policy,
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
    parallel_degraded, degraded_reason = _execute_task_attempts(
        run_dir=run_dir,
        runnable=runnable,
        adapter=adapter,
        max_concurrent=max_concurrent,
        worker_count=worker_count,
        parallel_degraded=parallel_degraded,
        degraded_reason=degraded_reason,
        allow_degraded=allow_degraded,
        capacity_retry_policy=capacity_retry_policy,
    )

    if parallel_degraded and isinstance(adapter, CodexExecAdapter):
        adapter = SerialFallbackAdapter()
        max_concurrent = 1
        for task in _runnable_tasks(tasks, retry_failed=True):
            if task.get("child_failure_code") == CODEX_CHILD_MODEL_CAPACITY:
                continue
            if not _task_is_retryable(task):
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
    tasks_artifact["retry_summary"] = _retry_summary(
        tasks,
        retry_policy=tasks_artifact.get("codex_exec_retry_policy"),
    )
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
        codex_exec_timeout_seconds=effective_codex_exec_timeout_seconds,
        codex_exec_retry_policy=capacity_retry_policy,
    )
    _write_json(run_dir / "parallel_orchestration_status.json", status)
    write_semantic_planner_validation(
        run_dir=run_dir,
        evidence=_read_json(run_dir / "evidence.json"),
        tasks=tasks,
        report_status=_read_optional_json(run_dir / "report_status.json"),
    )
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
    release_identity = release_validation_identity_from_payload(evidence)
    child_search_handoff_records: list[dict[str, Any]] = []
    child_search_handoff_rejections: list[dict[str, Any]] = []

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
        if release_identity and str(task.get("last_adapter") or "") == "codex-exec":
            sidecar_status = _release_validation_child_search_sidecar_status(
                run_dir=run_dir,
                task=task,
                shard_path=shard_path,
                identity=release_identity,
            )
            if not sidecar_status["valid"]:
                task["state"] = "failed"
                task["failure_category"] = "invalid_release_search_handoff"
                task["child_failure_code"] = CODEX_CHILD_RELEASE_HANDOFF_INVALID
                task["last_error"] = _release_validation_child_search_handoff_message(
                    sidecar_status
                )
                task["release_search_handoff_validation"] = sidecar_status
                child_search_handoff_rejections.extend(
                    dict(rejection)
                    for rejection in sidecar_status.get("rejections", [])
                    if isinstance(rejection, Mapping)
                )
                failed_tasks.append(_task_failure_record(task))
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
            source_copy = _with_task_angle_metadata(source, task)
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
            image_copy = _with_task_angle_metadata(image, task)
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
            claim_copy = _with_task_angle_metadata(claim_copy, task)
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
            accepted_shards.append(_accepted_shard_record(task, shard_path))
            if release_identity and str(task.get("last_adapter") or "") == "codex-exec":
                sidecar_status = _release_validation_child_search_sidecar_status(
                    run_dir=run_dir,
                    task=task,
                    shard_path=shard_path,
                    identity=release_identity,
                )
                child_search_handoff_records.extend(
                    dict(record)
                    for record in sidecar_status.get("records_payload", [])
                    if isinstance(record, Mapping)
                )
                child_search_handoff_rejections.extend(
                    dict(rejection)
                    for rejection in sidecar_status.get("rejections", [])
                    if isinstance(rejection, Mapping)
                )
        else:
            task["state"] = "discarded"
            task["discard_reason"] = "dedupe_or_no_mergeable_claims"
            discarded_tasks.append(_task_status_record(task, "dedupe_or_no_mergeable_claims"))

    if release_identity:
        _write_release_validation_search_results(
            run_dir,
            child_search_handoff_records,
        )
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
    retry_summary = _retry_summary(
        tasks,
        retry_policy=tasks_artifact.get("codex_exec_retry_policy"),
    )
    partial_parallel_summary = _partial_parallel_summary(
        status=None,
        planned_task_count=len(tasks),
        accepted_shard_count=len(accepted_shards),
        failure_counts=failure_counts,
        retry_summary=retry_summary,
        parallel_degraded=bool(tasks_artifact.get("parallel_degraded")),
    )
    merge_status = {
        "schema_version": PARALLEL_SCHEMA_VERSION,
        "run_id": str(evidence.get("run_id") or run_dir.name),
        "generated_at": _utc_now(),
        "status": "completed" if validation.valid else "failed_validation",
        "parallel_degraded": bool(tasks_artifact.get("parallel_degraded")),
        "evidence_source": evidence_source,
        "accepted_shard_count": len(accepted_shards),
        "accepted_shards": accepted_shards,
        "rejected_shards": rejected_shards,
        "blocked_tasks": blocked_tasks,
        "discarded_tasks": discarded_tasks,
        "failed_tasks": failed_tasks,
        "failure_counts": failure_counts,
        "retry_summary": retry_summary,
        "partial_parallel_summary": partial_parallel_summary,
        "partial_reason_category": partial_parallel_summary["reason_category"],
        "codex_native_search_handoff": {
            "search_results_path": str(run_dir / "search_results.jsonl"),
            "records": len(child_search_handoff_records),
            "rejections": child_search_handoff_rejections,
        },
        "diagnostics": _merge_diagnostics(
            accepted_shards=accepted_shards,
            failed_tasks=failed_tasks,
            blocked_tasks=blocked_tasks,
            rejected_shards=rejected_shards,
            discarded_tasks=discarded_tasks,
            retry_summary=retry_summary,
        ),
        "source_dedupe": source_dedupe,
        "image_dedupe": image_dedupe,
        "claim_dedupe": claim_dedupe,
        "conflicts": [],
        "merged_artifact_paths": {
            "evidence": str(evidence_path),
            "research_tasks": str(run_dir / RESEARCH_TASKS_FILENAME),
            "merge_status": str(run_dir / MERGE_STATUS_FILENAME),
            "search_results": str(run_dir / "search_results.jsonl"),
        },
        "validation": validation.to_dict(),
    }
    tasks_artifact["tasks"] = tasks
    tasks_artifact["retry_summary"] = retry_summary
    _write_json(run_dir / RESEARCH_TASKS_FILENAME, tasks_artifact)
    _write_json(run_dir / MERGE_STATUS_FILENAME, merge_status)
    return merge_status


def _release_validation_child_search_results(
    *,
    run_dir: Path,
    task: Mapping[str, Any],
    shard_path: Path,
    identity: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sidecar = shard_path.parent / "search_results.jsonl"
    if not sidecar.exists():
        return [], [
            {
                "task_id": task.get("id"),
                "path": str(sidecar.relative_to(run_dir)),
                "reason": "missing_child_search_results",
            }
        ]
    records = _read_jsonl(sidecar)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            rejected.append(
                {
                    "task_id": task.get("id"),
                    "record_index": index,
                    "reason": "invalid_jsonl_record",
                }
            )
            continue
        normalized, reason = _release_validation_search_result(
            record,
            task=task,
            identity=identity,
            index=index,
        )
        if normalized is None:
            rejected.append(
                {
                    "task_id": task.get("id"),
                    "record_index": index,
                    "reason": reason or "not_release_eligible",
                }
            )
            continue
        accepted.append(normalized)
    return accepted, rejected


def _release_validation_child_search_sidecar_status(
    *,
    run_dir: Path,
    task: Mapping[str, Any],
    shard_path: Path,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    sidecar = shard_path.parent / "search_results.jsonl"
    records, rejections = _release_validation_child_search_results(
        run_dir=run_dir,
        task=task,
        shard_path=shard_path,
        identity=identity,
    )
    normalized_rejections = [
        dict(rejection) for rejection in rejections if isinstance(rejection, Mapping)
    ]
    if not records and not normalized_rejections:
        normalized_rejections.append(
            {
                "task_id": task.get("id"),
                "path": str(sidecar.relative_to(run_dir)),
                "reason": "empty_child_search_results",
            }
        )
    valid = bool(records) and not normalized_rejections
    reason = None
    if not valid:
        first_rejection = normalized_rejections[0] if normalized_rejections else {}
        reason = str(first_rejection.get("reason") or "invalid_child_search_results")
    return {
        "valid": valid,
        "path": str(sidecar.relative_to(run_dir)),
        "records": len(records),
        "records_payload": records,
        "rejections": normalized_rejections,
        "reason": reason,
    }


def _release_validation_child_search_handoff_message(
    sidecar_status: Mapping[str, Any],
) -> str:
    rejections = [
        item
        for item in sidecar_status.get("rejections", [])
        if isinstance(item, Mapping)
    ] if isinstance(sidecar_status.get("rejections"), list) else []
    reasons = list(
        dict.fromkeys(
            str(item.get("reason") or "invalid_child_search_results")
            for item in rejections
        )
    )
    reason_text = ", ".join(reasons) if reasons else str(sidecar_status.get("reason") or "")
    return (
        "release validation child search_results.jsonl is invalid"
        f"; reason={reason_text or 'invalid_child_search_results'}"
        f"; records={int(sidecar_status.get('records') or 0)}"
        f"; rejections={len(rejections)}"
        f"; path={sidecar_status.get('path')}"
    )


def _release_validation_search_result(
    record: Mapping[str, Any],
    *,
    task: Mapping[str, Any],
    identity: Mapping[str, Any],
    index: int,
) -> tuple[dict[str, Any] | None, str | None]:
    missing_fields = [
        field
        for field in RELEASE_SEARCH_RESULT_REQUIRED_FIELDS
        if not _release_child_field_present(record, field)
    ]
    if missing_fields:
        return None, "missing_required_release_field:" + ",".join(missing_fields)
    if any(
        field in record
        for field in (
            "hidden_codex_api_call",
            "codex_native_api_call",
            "hidden_api_call",
        )
    ):
        return None, "hidden_codex_api_marker"
    if _truthy(record.get("post_hoc_patch")) or _truthy(record.get("post_hoc_patched")):
        return None, "post_hoc_patch_marker"

    provider = _normalized_release_string(record, "provider")
    provider_mode = _normalized_release_string(record, "provider_mode")
    retrieval_status = _normalized_release_string(record, "retrieval_status")
    policy_decision = _normalized_release_string(record, "policy_decision")
    handoff_artifact = _string_value(record, "handoff_artifact")
    if provider != "codex-native":
        return None, "non_release_provider"
    if provider_mode != "real":
        return None, "non_release_provider_mode"
    if policy_decision != "allowed":
        return None, "policy_not_allowed"
    if retrieval_status != "fetched":
        return None, "retrieval_not_fetched"
    if handoff_artifact.replace("\\", "/").split("/")[-1] != "search_results.jsonl":
        return None, "invalid_handoff_artifact"

    expected_task_id = str(task.get("search_task_id") or task.get("id") or "")
    task_id = _string_value(record, "task_id")
    angle_id = _string_value(record, "angle_id")
    route = _string_value(record, "route")
    result_type = _string_value(record, "result_type")
    if task_id != expected_task_id:
        return None, "task_id_mismatch"
    if angle_id != str(task.get("angle_id") or ""):
        return None, "angle_id_mismatch"
    if route != str(task.get("route") or "") or route not in SEARCH_ROUTES:
        return None, "route_mismatch"
    if result_type not in SEARCH_RESULT_TYPES:
        return None, "invalid_result_type"
    if _string_value(record, "prompt_id") != identity["prompt_id"]:
        return None, "prompt_id_mismatch"
    if _string_value(record, "suite_id") != identity["suite_id"]:
        return None, "suite_id_mismatch"
    if _string_value(record, "prompt_hash") != identity["prompt_hash"]:
        return None, "prompt_hash_mismatch"

    raw_metadata = record.get("raw_provider_metadata")
    result = {
        "id": _string_value(record, "id"),
        "task_id": task_id,
        "angle_id": angle_id,
        "route": route,
        "provider": provider,
        "provider_mode": provider_mode,
        "query": _string_value(record, "query"),
        "url": _string_value(record, "url"),
        "title": _string_value(record, "title"),
        "snippet": _string_value(record, "snippet"),
        "result_type": result_type,
        "rank": int(record["rank"]),
        "accessed_at": _string_value(record, "accessed_at"),
        "retrieval_status": retrieval_status,
        "policy_decision": policy_decision,
        "prompt_id": _string_value(record, "prompt_id"),
        "suite_id": _string_value(record, "suite_id"),
        "prompt_hash": _string_value(record, "prompt_hash"),
        "handoff_artifact": "search_results.jsonl",
    }
    optional_values = {
        "freshness_requirement": record.get("freshness_requirement"),
        "published_at": record.get("published_at"),
        "language": record.get("language"),
        "region": record.get("region"),
    }
    for key, value in optional_values.items():
        if value is not None:
            result[key] = value
    if "policy_flags" in record:
        result["policy_flags"] = list(record.get("policy_flags") or [])
    if raw_metadata is not None:
        result["raw_provider_metadata"] = (
            dict(raw_metadata) if isinstance(raw_metadata, Mapping) else raw_metadata
        )
    return result, None


def _release_child_field_present(record: Mapping[str, Any], field: str) -> bool:
    if field not in record or record.get(field) is None:
        return False
    value = record.get(field)
    if field == "rank":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _normalized_release_string(record: Mapping[str, Any], field: str) -> str:
    return _string_value(record, field).lower().replace("_", "-")


def _string_value(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    return value.strip() if isinstance(value, str) else str(value)


def _write_release_validation_search_results(
    run_dir: Path,
    records: Sequence[Mapping[str, Any]],
) -> None:
    path = run_dir / "search_results.jsonl"
    if not records:
        if not path.exists():
            path.write_text("", encoding="utf-8")
        return
    path.write_text(
        "".join(json.dumps(dict(record), sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[Any]:
    records: list[Any] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            records.append(None)
    return records


def _truthy(value: Any) -> bool:
    return value is True or (
        isinstance(value, str)
        and value.strip().lower() in {"1", "true", "yes"}
    )


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
            "semantic_planner_validation": str(
                run_dir / SEMANTIC_PLANNER_VALIDATION_FILENAME
            ),
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
    codex_exec_timeout_seconds: float | None = None,
    codex_exec_retry_policy: Mapping[str, Any] | None = None,
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
        "retry_summary": dict(merge_status.get("retry_summary", {})) if merge_status else {},
        "planned_task_count": planned_task_count,
        "runnable_task_count": runnable_task_count,
        "max_scheduled_concurrency": max_scheduled_concurrency,
        "accepted_shard_count": len(accepted_shards),
        "partial_parallel_summary": _partial_parallel_summary_from_merge(
            status=status,
            planned_task_count=planned_task_count,
            merge_status=merge_status,
            parallel_degraded=parallel_degraded,
        ),
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
        merge.setdefault("partial_parallel_summary", payload["partial_parallel_summary"])
        merge.setdefault("partial_reason_category", payload["partial_parallel_summary"]["reason_category"])
        payload["merge"] = merge
    payload["partial_reason_category"] = payload["partial_parallel_summary"]["reason_category"]
    if codex_exec_timeout_seconds is not None:
        payload["codex_exec_timeout_seconds"] = codex_exec_timeout_seconds
    if codex_exec_retry_policy is not None:
        payload["codex_exec_retry_policy"] = dict(codex_exec_retry_policy)
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
        elif accepted_count > 0:
            attempted_real_child_execution = True
            source_type = "real_child_execution"
            if parallel_degraded:
                description = (
                    "accepted evidence shards from real Codex child execution before "
                    "parallel execution degraded"
                )
            else:
                description = "accepted evidence shards from real Codex child execution"
        else:
            attempted_real_child_execution = True
            source_type = "failed_real_child_execution"
            description = "real Codex child execution was attempted, but no evidence shard was accepted"
    elif (adapter_name == "serial-degraded" or parallel_degraded) and accepted_count > 0 and attempted_real_child_execution:
        source_type = "real_child_execution"
        description = (
            "accepted evidence shards from real Codex child execution before "
            "parallel execution degraded"
        )
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
        FAILED_RELEASE_HANDOFF_INVALID,
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
    if accepted_count > 0:
        if _merge_has_exhausted_release_handoff_failure(merge_status):
            return FAILED_RELEASE_HANDOFF_INVALID
        if accepted_count < planned_task_count or has_failures or parallel_degraded:
            return "completed_partial_parallel"
        return "completed_parallel"
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
        FAILED_RELEASE_HANDOFF_INVALID,
        "blocked_parallel_execution",
    }:
        return True
    if status == "completed_serial_handoff":
        return False
    if status == "completed_partial_parallel" and accepted_shards:
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


def _task_has_exhausted_release_handoff_failure(task: Mapping[str, Any]) -> bool:
    if task.get("child_failure_code") != CODEX_CHILD_RELEASE_HANDOFF_INVALID:
        return False
    diagnostics = task.get("attempt_diagnostics")
    if isinstance(diagnostics, list):
        for attempt in diagnostics:
            if (
                isinstance(attempt, Mapping)
                and attempt.get("retry_decision") == "retry_exhausted"
            ):
                return True
    summary = task.get("retry_summary")
    if isinstance(summary, Mapping) and summary.get("retry_exhausted") is True:
        return True
    attempt = int(task.get("attempt") or 0)
    max_attempts = int(task.get("max_attempts") or 0)
    return (
        attempt > 0
        and max_attempts > 0
        and attempt >= max_attempts
        and task.get("retryable") is False
    )


def _merge_has_exhausted_release_handoff_failure(merge_status: Mapping[str, Any]) -> bool:
    for task in _list(merge_status.get("failed_tasks")):
        if _task_has_exhausted_release_handoff_failure(task):
            return True
    retry_summary = merge_status.get("retry_summary")
    if isinstance(retry_summary, Mapping):
        for task in _list(retry_summary.get("tasks")):
            if (
                task.get("final_child_failure_code") == CODEX_CHILD_RELEASE_HANDOFF_INVALID
                and task.get("retry_exhausted") is True
            ):
                return True
    return False


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
        if state == "retryable" and not _task_is_retryable(task):
            task["state"] = "failed"
            continue
        if state == "failed":
            if not retry_failed:
                continue
            if not _task_is_retryable(task):
                continue
            if int(task.get("attempt") or 0) >= int(task.get("max_attempts") or 1):
                continue
            task["state"] = "retryable"
        runnable.append(task)
    return runnable


def _execute_task_attempts(
    *,
    run_dir: Path,
    runnable: Sequence[dict[str, Any]],
    adapter: CodexExecAdapter | FixtureAdapter | SerialFallbackAdapter,
    max_concurrent: int,
    worker_count: int,
    parallel_degraded: bool,
    degraded_reason: str | None,
    allow_degraded: bool,
    capacity_retry_policy: Mapping[str, Any] | None,
) -> tuple[bool, str | None]:
    pending = list(runnable)
    while pending:
        if isinstance(adapter, SerialFallbackAdapter):
            max_concurrent = 1
            worker_count = 1
        retry_pending: list[dict[str, Any]] = []
        retry_traces: list[tuple[dict[str, Any], dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_to_task: dict[Any, dict[str, Any]] = {}
            for task in pending:
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
                if isinstance(adapter, CodexExecAdapter):
                    retry_plan = _maybe_retry_capacity_failure(
                        task,
                        capacity_retry_policy=capacity_retry_policy,
                    )
                    if retry_plan is not None:
                        if retry_plan.get("should_retry") is True:
                            retry_pending.append(task)
                            retry_traces.append((task, retry_plan))
                            continue
                        _append_retry_trace_event(run_dir, task, retry_plan)
                if (
                    isinstance(adapter, CodexExecAdapter)
                    and allow_degraded
                    and result.status == "failed"
                    and _is_missing_capability_failure(result)
                ):
                    parallel_degraded = True
                    degraded_reason = result.failure_category or "codex_exec_unavailable"
                    _preserve_parallel_failure(task)
                    retryable = _task_is_retryable(task)
                    if isinstance(task.get("parallel_failure"), dict):
                        task["parallel_failure"]["retryable"] = retryable
                    if retryable:
                        task["state"] = "retryable"
        if retry_traces:
            batch_sleep_seconds = max(
                float(plan.get("computed_backoff_seconds") or 0.0)
                for _task, plan in retry_traces
            )
            actual_sleep_seconds = _sleep_for_retry(batch_sleep_seconds)
            for task, retry_plan in retry_traces:
                task["capacity_retry_actual_elapsed_seconds"] = (
                    float(task.get("capacity_retry_actual_elapsed_seconds") or 0.0)
                    + actual_sleep_seconds
                )
                _set_latest_attempt_retry_decision(
                    task,
                    str(retry_plan.get("retry_decision") or "retry"),
                    computed_backoff_seconds=retry_plan.get("computed_backoff_seconds"),
                    actual_sleep_seconds=actual_sleep_seconds,
                )
                retry_plan = dict(retry_plan)
                retry_plan["actual_sleep_seconds"] = actual_sleep_seconds
                retry_plan["batch_sleep_seconds"] = batch_sleep_seconds
                _append_retry_trace_event(run_dir, task, retry_plan)
        pending = retry_pending
    return parallel_degraded, degraded_reason


def _capacity_retry_policy(timeout_seconds: float | None) -> dict[str, Any]:
    effective_timeout = (
        float(timeout_seconds)
        if timeout_seconds is not None
        else DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS
    )
    policy = dict(CAPACITY_RETRY_POLICY_DEFAULTS)
    policy["max_retry_elapsed_seconds"] = min(120.0, effective_timeout / 2.0)
    policy["codex_exec_timeout_seconds"] = effective_timeout
    return policy


def _apply_capacity_retry_policy(
    tasks: Sequence[dict[str, Any]],
    policy: Mapping[str, Any],
) -> None:
    max_attempts = int(policy.get("max_attempts") or CAPACITY_RETRY_MAX_ATTEMPTS)
    for task in tasks:
        task["max_attempts"] = max(max_attempts, int(task.get("max_attempts") or 1))


def _maybe_retry_capacity_failure(
    task: dict[str, Any],
    *,
    capacity_retry_policy: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    attempt_record = _latest_attempt_diagnostic(task)
    if not attempt_record:
        return None
    if attempt_record.get("status") == "completed":
        _set_latest_attempt_retry_decision(task, "do_not_retry")
        return {"retry_decision": "do_not_retry", "should_retry": False}
    if capacity_retry_policy is None:
        task["state"] = "failed"
        _set_latest_attempt_retry_decision(task, "do_not_retry")
        return {"retry_decision": "do_not_retry", "should_retry": False}
    retryable_child_failure_codes = {
        CODEX_CHILD_MODEL_CAPACITY,
        CODEX_CHILD_RELEASE_HANDOFF_INVALID,
    }
    if attempt_record.get("child_failure_code") not in retryable_child_failure_codes:
        task["state"] = "failed"
        _set_latest_attempt_retry_decision(task, "do_not_retry")
        return {"retry_decision": "do_not_retry", "should_retry": False}
    if attempt_record.get("timeout") is True:
        task["state"] = "failed"
        _set_latest_attempt_retry_decision(task, "do_not_retry")
        return {"retry_decision": "do_not_retry", "should_retry": False}
    attempt = int(attempt_record.get("attempt") or task.get("attempt") or 0)
    max_attempts = int(
        attempt_record.get("max_attempts")
        or task.get("max_attempts")
        or capacity_retry_policy.get("max_attempts")
        or CAPACITY_RETRY_MAX_ATTEMPTS
    )
    if attempt >= max_attempts:
        task["state"] = "failed"
        task["retry_exhausted"] = True
        task["retry_exhausted_reason"] = "max_attempts_reached"
        _set_latest_attempt_retry_decision(task, "retry_exhausted")
        return {
            "retry_decision": "retry_exhausted",
            "should_retry": False,
            "retry_exhausted_reason": "max_attempts_reached",
        }
    computed_backoff = _capacity_retry_backoff_seconds(attempt, capacity_retry_policy)
    max_elapsed = float(capacity_retry_policy.get("max_retry_elapsed_seconds") or 0.0)
    previous_elapsed = float(task.get("capacity_retry_computed_elapsed_seconds") or 0.0)
    if previous_elapsed + computed_backoff > max_elapsed:
        task["state"] = "failed"
        task["retry_exhausted"] = True
        task["retry_exhausted_reason"] = "max_retry_elapsed_seconds_reached"
        _set_latest_attempt_retry_decision(
            task,
            "retry_exhausted",
            computed_backoff_seconds=computed_backoff,
            actual_sleep_seconds=0.0,
        )
        return {
            "retry_decision": "retry_exhausted",
            "should_retry": False,
            "computed_backoff_seconds": computed_backoff,
            "actual_sleep_seconds": 0.0,
            "retry_exhausted_reason": "max_retry_elapsed_seconds_reached",
        }
    _set_latest_attempt_retry_decision(
        task,
        "retry",
        computed_backoff_seconds=computed_backoff,
    )
    task["capacity_retry_computed_elapsed_seconds"] = previous_elapsed + computed_backoff
    task["state"] = "retryable"
    return {
        "retry_decision": "retry",
        "should_retry": True,
        "computed_backoff_seconds": computed_backoff,
    }


def _capacity_retry_backoff_seconds(
    attempt: int,
    policy: Mapping[str, Any],
) -> float:
    initial = float(policy.get("initial_delay_seconds") or 0.0)
    multiplier = float(policy.get("backoff_multiplier") or 1.0)
    max_delay = float(policy.get("max_delay_seconds") or initial)
    jitter_ratio = max(0.0, float(policy.get("jitter_ratio") or 0.0))
    base_delay = min(max_delay, initial * (multiplier ** max(0, attempt - 1)))
    if jitter_ratio:
        base_delay *= 1.0 + random.uniform(-jitter_ratio, jitter_ratio)
    return max(0.0, base_delay)


def _sleep_for_retry(seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    started = time.monotonic()
    time.sleep(seconds)
    return max(0.0, time.monotonic() - started)


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
        "angle_id": task.get("angle_id"),
        "state": "assigned",
        "assigned_subagent_id": task["assigned_subagent_id"],
        "attempt": task["attempt"],
        "adapter": adapter_name,
        "max_concurrent_codex_subagents": max_concurrent,
        "parallel_degraded": parallel_degraded,
        "task_scope_hash": _task_scope_hash(task),
        "task_scope": {
            "query": task.get("query"),
            "route": task.get("route"),
            "expected_evidence": list(task.get("expected_evidence") or []),
            "report_section": task.get("report_section"),
        },
    }
    _append_jsonl(run_dir / ASSIGNMENTS_FILENAME, record)
    task["state"] = "running"


def _task_scope_hash(task: Mapping[str, Any]) -> str:
    scope = {
        "angle_id": task.get("angle_id"),
        "query": task.get("query"),
        "route": task.get("route"),
        "expected_evidence": list(task.get("expected_evidence") or []),
        "success_criteria": list(task.get("success_criteria") or []),
        "report_section": task.get("report_section"),
        "output_shard_path": task.get("output_shard_path"),
    }
    serialized = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _record_runner_result(run_dir: Path, task: dict[str, Any], result: RunnerResult) -> None:
    trace_ids: list[str] = list(task.get("trace_event_ids") or [])
    task["last_child_thread_id"] = result.child_thread_id
    command_context = _last_command_context(result.events)
    if command_context:
        task["last_command_context"] = command_context
    attempt_record = _append_attempt_diagnostic(task, result, command_context)
    for event in result.events:
        trace_record = _trace_record(run_dir, event)
        append_trace_record(run_dir, trace_record)
        trace_ids.append(trace_record["event_id"])
    task["trace_event_ids"] = trace_ids
    if result.status == "completed" and result.shard_path and Path(result.shard_path).exists():
        shard_path = Path(result.shard_path)
        release_identity = release_validation_identity_from_payload(task)
        if (
            release_identity
            and str(task.get("last_adapter") or "") == "codex-exec"
        ):
            sidecar_status = _release_validation_child_search_sidecar_status(
                run_dir=run_dir,
                task=task,
                shard_path=shard_path,
                identity=release_identity,
            )
            if not sidecar_status["valid"]:
                diagnostic = _release_validation_child_search_handoff_message(
                    sidecar_status
                )
                task["failure_category"] = "invalid_release_search_handoff"
                task["child_failure_code"] = CODEX_CHILD_RELEASE_HANDOFF_INVALID
                task["last_error"] = diagnostic
                task["release_search_handoff_validation"] = sidecar_status
                attempt_record["status"] = "failed"
                attempt_record["failure_category"] = "invalid_release_search_handoff"
                attempt_record["release_search_handoff_validation"] = sidecar_status
                attempt_record["last_message_text_preview"] = _bounded_preview(diagnostic)
                _update_attempt_diagnostic(
                    attempt_record,
                    child_failure_code=CODEX_CHILD_RELEASE_HANDOFF_INVALID,
                    retry_decision="do_not_retry",
                )
                task["state"] = "failed"
                return
        validation = validate_artifacts(evidence_path=shard_path)
        if not validation.valid:
            validation_payload = validation.to_dict()
            task["failure_category"] = "invalid_shard"
            task["child_failure_code"] = CODEX_CHILD_SCHEMA_INVALID
            task["validation"] = validation_payload
            attempt_record["validation"] = validation_payload
            _update_attempt_probe_validation(
                attempt_record,
                validation=validation_payload,
                child_failure_code=CODEX_CHILD_SCHEMA_INVALID,
            )
            _update_attempt_diagnostic(
                attempt_record,
                child_failure_code=CODEX_CHILD_SCHEMA_INVALID,
                retry_decision="do_not_retry",
            )
            task["state"] = "failed"
            return
        validation_payload = validation.to_dict()
        attempt_record["validation"] = validation_payload
        _update_attempt_probe_validation(
            attempt_record,
            validation=validation_payload,
            child_failure_code=attempt_record.get("child_failure_code"),
        )
        task["state"] = "completed"
        task["failure_category"] = None
        task["child_failure_code"] = None
        _update_attempt_diagnostic(attempt_record, retry_decision="do_not_retry")
        task.pop("validation", None)
        task.pop("release_search_handoff_validation", None)
        return
    if result.status == "blocked":
        task["state"] = "blocked"
        task["failure_category"] = result.failure_category or "adapter_unavailable"
        task["child_failure_code"] = attempt_record.get("child_failure_code")
        task["blocked_reason"] = result.message or "parallel execution unavailable"
        _update_attempt_diagnostic(attempt_record, retry_decision="do_not_retry")
        return
    failed_shard_path = _probe_output_shard_path(task, command_context or {}, {})
    if failed_shard_path is not None and failed_shard_path.exists():
        validation = validate_artifacts(evidence_path=failed_shard_path)
        if not validation.valid:
            validation_payload = validation.to_dict()
            task["failure_category"] = "invalid_shard"
            task["child_failure_code"] = CODEX_CHILD_SCHEMA_INVALID
            task["validation"] = validation_payload
            if result.message:
                task["last_error"] = result.message
            attempt_record["status"] = "failed"
            attempt_record["failure_category"] = "invalid_shard"
            attempt_record["validation"] = validation_payload
            _update_attempt_probe_validation(
                attempt_record,
                validation=validation_payload,
                child_failure_code=CODEX_CHILD_SCHEMA_INVALID,
            )
            _update_attempt_diagnostic(
                attempt_record,
                child_failure_code=CODEX_CHILD_SCHEMA_INVALID,
                retry_decision="do_not_retry",
            )
            task["state"] = "failed"
            return
    failure = result.failure_category or "missing_shard"
    child_failure_code = str(attempt_record.get("child_failure_code") or "")
    if not child_failure_code and failure == "missing_shard":
        child_failure_code = CODEX_CHILD_MISSING_SHARD
        _update_attempt_diagnostic(
            attempt_record,
            child_failure_code=child_failure_code,
        )
    if child_failure_code == CODEX_CHILD_MODEL_CAPACITY:
        failure = CODEX_CHILD_MODEL_CAPACITY
    task["failure_category"] = failure
    task["child_failure_code"] = child_failure_code or None
    if result.message:
        task["last_error"] = result.message
    task["state"] = "failed"


def _append_attempt_diagnostic(
    task: dict[str, Any],
    result: RunnerResult,
    command_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    context = command_context if isinstance(command_context, Mapping) else {}
    summary = context.get("last_child_event_summary")
    if not isinstance(summary, Mapping):
        summary = {}
    attempt = int(task.get("attempt") or 0)
    child_failure_code = _classify_child_failure_code(
        result=result,
        command_context=context,
        child_summary=summary,
    )
    probe = _build_child_attempt_probe(
        task=task,
        result=result,
        command_context=context,
        child_summary=summary,
        child_failure_code=child_failure_code,
    )
    if (
        probe.get("timeout") is True
        and probe.get("shard_exists") is True
        and probe.get("shard_parent_valid") is False
    ):
        child_failure_code = CODEX_CHILD_SCHEMA_INVALID
        probe = _with_probe_failure_code(probe, child_failure_code)
    record = {
        "attempt": attempt,
        "max_attempts": int(task.get("max_attempts") or 0),
        "child_thread_id": result.child_thread_id,
        "child_failure_code": child_failure_code,
        "timeout": bool(summary.get("timeout")),
        "returncode": summary.get("returncode"),
        "last_message_text_preview": _attempt_last_message_preview(
            result=result,
            child_summary=summary,
        ),
        "raw_child_event_artifacts": _raw_child_artifacts(context, summary),
        "computed_backoff_seconds": None,
        "actual_sleep_seconds": 0.0,
        "retry_decision": "do_not_retry",
        "status": result.status,
        "failure_category": result.failure_category,
        "attempt_probe": probe,
    }
    diagnostics = task.setdefault("attempt_diagnostics", [])
    if not isinstance(diagnostics, list):
        diagnostics = []
        task["attempt_diagnostics"] = diagnostics
    diagnostics.append(record)
    return record


def _latest_attempt_diagnostic(task: Mapping[str, Any]) -> dict[str, Any] | None:
    diagnostics = task.get("attempt_diagnostics")
    if not isinstance(diagnostics, list) or not diagnostics:
        return None
    latest = diagnostics[-1]
    return latest if isinstance(latest, dict) else None


def _set_latest_attempt_retry_decision(
    task: dict[str, Any],
    retry_decision: str,
    *,
    computed_backoff_seconds: float | None = None,
    actual_sleep_seconds: float | None = None,
) -> None:
    attempt_record = _latest_attempt_diagnostic(task)
    if attempt_record is None:
        return
    _update_attempt_diagnostic(
        attempt_record,
        retry_decision=retry_decision,
        computed_backoff_seconds=computed_backoff_seconds,
        actual_sleep_seconds=actual_sleep_seconds,
    )


def _update_attempt_diagnostic(
    attempt_record: dict[str, Any],
    *,
    child_failure_code: str | None = None,
    retry_decision: str | None = None,
    computed_backoff_seconds: float | None = None,
    actual_sleep_seconds: float | None = None,
) -> None:
    if child_failure_code is not None:
        attempt_record["child_failure_code"] = child_failure_code
        _update_attempt_probe_failure_code(attempt_record, child_failure_code)
    if retry_decision is not None:
        attempt_record["retry_decision"] = retry_decision
    if computed_backoff_seconds is not None:
        attempt_record["computed_backoff_seconds"] = computed_backoff_seconds
    if actual_sleep_seconds is not None:
        attempt_record["actual_sleep_seconds"] = actual_sleep_seconds


def _build_child_attempt_probe(
    *,
    task: Mapping[str, Any],
    result: RunnerResult,
    command_context: Mapping[str, Any],
    child_summary: Mapping[str, Any],
    child_failure_code: str | None,
) -> dict[str, Any]:
    shard_path = (
        Path(result.shard_path)
        if isinstance(result.shard_path, str) and result.shard_path
        else _probe_output_shard_path(task, command_context, child_summary)
    )
    defer_parent_validation = bool(
        release_validation_identity_from_payload(task)
        and str(task.get("last_adapter") or "") == "codex-exec"
        and result.status == "completed"
    )
    timed_out = bool(child_summary.get("timeout"))
    shard_state = _probe_shard_state(
        shard_path,
        defer_parent_validation=defer_parent_validation,
        timed_out=timed_out,
    )
    runner_recoverable_valid_shard = bool(
        shard_state["shard_parent_valid"]
        and (timed_out or result.status == "completed")
    )
    probe: dict[str, Any] = {
        "schema_version": CHILD_ATTEMPT_PROBE_SCHEMA_VERSION,
        "task_id": str(task.get("id") or result.task_id or "unknown"),
        "attempt": int(task.get("attempt") or 0),
        "child_thread_id": result.child_thread_id or child_summary.get("child_thread_id"),
        "child_started_at": _string_or_none(child_summary.get("child_started_at")),
        "child_finished_at": _string_or_none(child_summary.get("child_finished_at")),
        "child_timed_out_at": _string_or_none(child_summary.get("child_timed_out_at")),
        "elapsed_seconds": _number_or_none(child_summary.get("elapsed_seconds")),
        "timeout_seconds": _number_or_none(child_summary.get("timeout_seconds")),
        "timeout": timed_out,
        "output_shard_path": str(shard_path) if shard_path is not None else _string_or_none(
            child_summary.get("output_shard_path")
            or command_context.get("output_shard_path")
            or task.get("output_shard_path")
        ),
        "child_failure_code": child_failure_code,
        "first_shard_observed_at": shard_state["first_shard_observed_at"],
        "first_parent_valid_shard_at": shard_state["first_parent_valid_shard_at"],
        "last_validation_attempt_at": shard_state["last_validation_attempt_at"],
        "last_validation_result": shard_state["last_validation_result"],
        "shard_exists": shard_state["shard_exists"],
        "parent_probe_after_timeout": shard_state["parent_probe_after_timeout"],
        "parent_probe_observed_shard_at": shard_state["parent_probe_observed_shard_at"],
        "parent_probe_validation_attempt_at": shard_state["parent_probe_validation_attempt_at"],
        "parent_probe_validated_shard_at": shard_state["parent_probe_validated_shard_at"],
        "shard_schema_version": shard_state["shard_schema_version"],
        "shard_parent_valid": shard_state["shard_parent_valid"],
        "missing_required_fields": shard_state["missing_required_fields"],
        "validation_error_count": shard_state["validation_error_count"],
        "validation_errors": shard_state["validation_errors"],
        "runner_recoverable_valid_shard": runner_recoverable_valid_shard,
        "sidecars": shard_state["sidecars"],
        "last_child_event_type": _string_or_none(child_summary.get("last_event_type")),
        "last_tool_or_command_kind": _last_tool_or_command_kind(child_summary),
        "last_tool_or_command_preview": _last_tool_or_command_preview(child_summary),
        "last_message_text_preview": _bounded_preview(
            child_summary.get("last_message_text_preview")
        ),
        "facts": [],
        "unknowns": [],
        "candidate_causes": [],
        "root_cause": None,
    }
    probe["unknowns"] = _probe_unknowns(probe, shard_state)
    probe["facts"] = _probe_facts(probe)
    probe["candidate_causes"] = _probe_candidate_causes(probe)
    return _finalize_child_attempt_probe_contract(probe)


def _probe_output_shard_path(
    task: Mapping[str, Any],
    command_context: Mapping[str, Any],
    child_summary: Mapping[str, Any],
) -> Path | None:
    for value in (
        command_context.get("output_shard_path"),
        child_summary.get("output_shard_path"),
    ):
        if isinstance(value, str) and value.strip():
            return Path(value)
    try:
        run_dir_value = command_context.get("run_dir")
        run_dir = Path(str(run_dir_value)) if run_dir_value else None
        if run_dir is not None:
            return _resolve_task_shard_path(task, run_dir)
    except ParallelOrchestrationError:
        return None
    return None


def _probe_shard_state(
    shard_path: Path | None,
    *,
    defer_parent_validation: bool = False,
    timed_out: bool = False,
) -> dict[str, Any]:
    if shard_path is None:
        return _missing_probe_shard_state(
            reason="output_shard_path_unobservable",
            validation_attempt_at=None,
            timed_out=timed_out,
        )
    validation_attempt_at = _utc_now()
    if not shard_path.exists():
        return _missing_probe_shard_state(
            reason="shard_file_not_present_when_parent_probe_ran",
            validation_attempt_at=validation_attempt_at,
            shard_dir=shard_path.parent,
            timed_out=timed_out,
        )
    if defer_parent_validation:
        shard_schema_version = _shard_schema_version(shard_path)
        parent_probe_after_timeout = bool(timed_out)
        return {
            "first_shard_observed_at": None if timed_out else validation_attempt_at,
            "first_parent_valid_shard_at": None,
            "last_validation_attempt_at": None,
            "last_validation_result": {
                "state": "unknown",
                "valid": None,
                "error_count": 0,
                "errors": [],
            },
            "shard_exists": True,
            "parent_probe_after_timeout": parent_probe_after_timeout,
            "parent_probe_observed_shard_at": validation_attempt_at,
            "parent_probe_validation_attempt_at": None,
            "parent_probe_validated_shard_at": None,
            "shard_schema_version": shard_schema_version,
            "shard_parent_valid": None,
            "missing_required_fields": [],
            "validation_error_count": 0,
            "validation_errors": [],
            "sidecars": _expected_sidecar_probe_status(shard_path.parent),
            "unobservable_reasons": {
                **(
                    {
                        "first_shard_observed_at": (
                            "no_pre_timeout_shard_observation_watcher_available"
                        )
                    }
                    if timed_out
                    else {}
                ),
                "last_validation_attempt_at": (
                    "parent_validation_deferred_until_release_sidecar_validation"
                ),
                "first_parent_valid_shard_at": (
                    "parent_validation_deferred_until_release_sidecar_validation"
                ),
            },
        }
    validation = validate_artifacts(evidence_path=shard_path).to_dict()
    validation_errors = _bounded_validation_errors(validation)
    valid = bool(validation.get("valid"))
    shard_schema_version = _shard_schema_version(shard_path)
    parent_probe_after_timeout = bool(timed_out)
    return {
        "first_shard_observed_at": None if timed_out else validation_attempt_at,
        "first_parent_valid_shard_at": None if timed_out else validation_attempt_at if valid else None,
        "last_validation_attempt_at": validation_attempt_at,
        "last_validation_result": {
            "state": "valid" if valid else "invalid",
            "valid": valid,
            "error_count": len(validation.get("errors", []))
            if isinstance(validation.get("errors"), list)
            else 0,
            "errors": validation_errors,
        },
        "shard_exists": True,
        "parent_probe_after_timeout": parent_probe_after_timeout,
        "parent_probe_observed_shard_at": validation_attempt_at,
        "parent_probe_validation_attempt_at": validation_attempt_at,
        "parent_probe_validated_shard_at": validation_attempt_at if valid else None,
        "shard_schema_version": shard_schema_version,
        "shard_parent_valid": valid,
        "missing_required_fields": _missing_required_fields(validation_errors),
        "validation_error_count": len(validation.get("errors", []))
        if isinstance(validation.get("errors"), list)
        else 0,
        "validation_errors": validation_errors,
        "sidecars": _expected_sidecar_probe_status(shard_path.parent),
        "unobservable_reasons": {
            **(
                {
                    "first_shard_observed_at": (
                        "no_pre_timeout_shard_observation_watcher_available"
                    ),
                    "first_parent_valid_shard_at": (
                        "parent_valid_shard_was_observed_only_during_parent_probe_after_timeout"
                        if valid
                        else "shard_was_not_parent_valid_when_parent_probe_ran"
                    ),
                }
                if timed_out
                else {
                    "first_parent_valid_shard_at": None
                    if valid
                    else "shard_was_not_parent_valid_when_parent_probe_ran"
                }
            )
        },
    }


def _missing_probe_shard_state(
    *,
    reason: str,
    validation_attempt_at: str | None,
    shard_dir: Path | None = None,
    timed_out: bool = False,
) -> dict[str, Any]:
    error = {
        "path": "$.evidence",
        "code": "missing_file",
        "message": reason,
    }
    return {
        "first_shard_observed_at": None,
        "first_parent_valid_shard_at": None,
        "last_validation_attempt_at": validation_attempt_at,
        "last_validation_result": {
            "state": "invalid",
            "valid": False,
            "error_count": 1,
            "errors": [error],
        },
        "shard_exists": False,
        "parent_probe_after_timeout": bool(timed_out),
        "parent_probe_observed_shard_at": None,
        "parent_probe_validation_attempt_at": validation_attempt_at,
        "parent_probe_validated_shard_at": None,
        "shard_schema_version": None,
        "shard_parent_valid": False,
        "missing_required_fields": list(EVIDENCE_SCHEMA_V0_REQUIRED_TOP_LEVEL_FIELDS),
        "validation_error_count": 1,
        "validation_errors": [error],
        "sidecars": _expected_sidecar_probe_status(shard_dir) if shard_dir is not None else _unknown_sidecar_probe_status(),
        "unobservable_reasons": {
            "first_shard_observed_at": reason,
            "first_parent_valid_shard_at": "parent_valid_shard_was_not_observed",
            **(
                {"last_validation_attempt_at": "output_shard_path_unobservable"}
                if validation_attempt_at is None
                else {}
            ),
        },
    }


def _shard_schema_version(shard_path: Path) -> str | None:
    try:
        payload = json.loads(shard_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("schema_version")
    return str(value) if value is not None else None


def _expected_sidecar_probe_status(shard_dir: Path) -> dict[str, dict[str, Any]]:
    status: dict[str, dict[str, Any]] = {}
    for key, filename in EXPECTED_CHILD_SIDECARS.items():
        path = shard_dir / filename
        record: dict[str, Any] = {
            "filename": filename,
            "path": str(path),
            "exists": path.exists(),
        }
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                record["read_error"] = exc.__class__.__name__
            else:
                record["size_bytes"] = path.stat().st_size
                record["line_count"] = len([line for line in text.splitlines() if line.strip()])
        status[key] = record
    return status


def _unknown_sidecar_probe_status() -> dict[str, dict[str, Any]]:
    return {
        key: {
            "filename": filename,
            "path": None,
            "exists": None,
            "unobservable_reason": "output_shard_path_unobservable",
        }
        for key, filename in EXPECTED_CHILD_SIDECARS.items()
    }


def _bounded_validation_errors(
    validation: Mapping[str, Any],
    *,
    limit: int = 6,
) -> list[dict[str, str]]:
    errors = validation.get("errors")
    if not isinstance(errors, list):
        return []
    bounded: list[dict[str, str]] = []
    for error in errors[:limit]:
        if not isinstance(error, Mapping):
            continue
        bounded.append(
            {
                "path": str(error.get("path") or "")[:180],
                "code": str(error.get("code") or "")[:120],
                "message": _output_summary(error.get("message"), limit=220),
            }
        )
    return bounded


def _missing_required_fields(validation_errors: Sequence[Mapping[str, Any]]) -> list[str]:
    missing: list[str] = []
    for error in validation_errors:
        if error.get("code") != "missing_required_field":
            continue
        path = str(error.get("path") or "")
        if "." in path:
            missing.append(path.rsplit(".", 1)[-1])
    return list(dict.fromkeys(missing))


def _last_tool_or_command_kind(child_summary: Mapping[str, Any]) -> str | None:
    if child_summary.get("last_command"):
        return "command"
    if child_summary.get("last_tool_name"):
        return "tool"
    if child_summary.get("last_tool_call_id"):
        return "tool_call"
    return None


def _last_tool_or_command_preview(child_summary: Mapping[str, Any]) -> str | None:
    for key in ("last_command", "last_tool_name", "last_tool_call_id"):
        preview = _bounded_preview(child_summary.get(key))
        if preview:
            return preview
    return None


def _probe_facts(probe: Mapping[str, Any]) -> list[dict[str, Any]]:
    facts = [
        {"field": "task_id", "value": probe.get("task_id")},
        {"field": "attempt", "value": probe.get("attempt")},
        {"field": "timeout", "value": probe.get("timeout")},
        {"field": "shard_exists", "value": probe.get("shard_exists")},
        {"field": "shard_parent_valid", "value": probe.get("shard_parent_valid")},
        {"field": "child_failure_code", "value": probe.get("child_failure_code")},
    ]
    return [fact for fact in facts if fact.get("value") is not None]


def _probe_unknowns(
    probe: Mapping[str, Any],
    shard_state: Mapping[str, Any],
) -> list[dict[str, str]]:
    unknowns: list[dict[str, str]] = []
    for field, reason in _mapping_or_empty(shard_state.get("unobservable_reasons")).items():
        if reason:
            unknowns.append({"field": str(field), "reason": str(reason)})
    if not probe.get("child_started_at"):
        unknowns.append(
            {
                "field": "child_started_at",
                "reason": "adapter_did_not_report_child_start_time",
            }
        )
    if not probe.get("child_finished_at") and not probe.get("child_timed_out_at"):
        unknowns.append(
            {
                "field": "child_finished_at",
                "reason": "child_finish_or_timeout_time_unobservable",
            }
        )
    unknowns.append(
        {
            "field": "root_cause",
            "reason": (
                "probe records parent-observed facts only; child-side timeout root "
                "cause requires additional evidence"
            ),
        }
    )
    if probe.get("timeout") is True:
        unknowns.extend(
            [
                {
                    "field": "search_delay_root_cause",
                    "reason": "not directly supported by probe fields",
                },
                {
                    "field": "model_capacity_root_cause",
                    "reason": "not directly supported by probe fields",
                },
                {
                    "field": "runner_recovery_delay_root_cause",
                    "reason": "not directly supported by probe fields",
                },
            ]
        )
    return unknowns


def _probe_candidate_causes(probe: Mapping[str, Any]) -> list[dict[str, Any]]:
    child_failure_code = str(probe.get("child_failure_code") or "")
    timed_out = probe.get("timeout") is True
    parent_observed_shard = probe.get("parent_probe_observed_shard_at") is not None
    shard_parent_valid = probe.get("shard_parent_valid") is True
    if timed_out and not parent_observed_shard:
        return [
            {
                "cause": "no_shard_observed_during_parent_probe_after_timeout",
                "basis": [
                    "timeout",
                    "child_timeout_at",
                    "parent_probe_after_timeout",
                    "parent_probe_observed_shard_at",
                    "last_validation_result",
                ],
                "confidence": "medium",
            }
        ]
    if timed_out and parent_observed_shard and not shard_parent_valid:
        return [
            {
                "cause": "invalid_shard_observed_during_parent_probe_after_timeout",
                "basis": [
                    "timeout",
                    "parent_probe_after_timeout",
                    "parent_probe_observed_shard_at",
                    "parent_probe_validation_attempt_at",
                    "shard_parent_valid",
                    "last_validation_result.errors",
                    "top_level_missing_fields",
                ],
                "confidence": "medium",
            }
        ]
    if timed_out and shard_parent_valid:
        return [
            {
                "cause": "valid_shard_recoverable_during_parent_probe_after_timeout",
                "basis": [
                    "timeout",
                    "parent_probe_after_timeout",
                    "parent_probe_validated_shard_at",
                    "runner_recoverability",
                ],
                "confidence": "medium",
            }
        ]
    if child_failure_code == CODEX_CHILD_SCHEMA_INVALID:
        return [
            {
                "cause": "child_shard_schema_invalid",
                "basis": [
                    "child_failure_code",
                    "shard_schema_version",
                    "last_validation_result.errors",
                ],
                "confidence": "high",
            }
        ]
    if child_failure_code == CODEX_CHILD_MISSING_SHARD:
        return [
            {
                "cause": "missing_child_shard",
                "basis": ["child_failure_code", "shard_exists"],
                "confidence": "high",
            }
        ]
    if child_failure_code:
        return [
            {
                "cause": child_failure_code,
                "basis": ["child_failure_code", "last_message_text_preview"],
                "confidence": "medium",
            }
        ]
    return []


def _finalize_child_attempt_probe_contract(probe: Mapping[str, Any]) -> dict[str, Any]:
    updated = dict(probe)
    updated["child_timeout_at"] = updated.get("child_timed_out_at")
    updated["child_elapsed_seconds"] = updated.get("elapsed_seconds")
    updated["shard_exists_at_timeout"] = None
    updated["top_level_missing_fields"] = list(updated.get("missing_required_fields") or [])
    updated["sidecar_status"] = copy.deepcopy(updated.get("sidecars") or {})
    updated["runner_recoverability"] = _probe_runner_recoverability(updated)
    updated["last_child_event"] = _probe_last_child_event(updated)
    updated["last_tool_or_command_call"] = _probe_last_tool_or_command_call(updated)
    updated["last_child_message_preview"] = updated.get("last_message_text_preview")
    primary_cause = _primary_probe_candidate_cause(updated)
    updated["candidate_cause"] = primary_cause.get("cause") if primary_cause else None
    updated["candidate_cause_confidence"] = (
        primary_cause.get("confidence") if primary_cause else None
    )
    updated["candidate_cause_basis"] = (
        list(primary_cause.get("basis") or []) if primary_cause else []
    )
    updated["unobservable_reasons"] = _probe_unobservable_reasons(updated)
    return updated


def _primary_probe_candidate_cause(probe: Mapping[str, Any]) -> dict[str, Any]:
    candidates = probe.get("candidate_causes")
    if not isinstance(candidates, list):
        return {}
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return dict(candidate)
    return {}


def _probe_runner_recoverability(probe: Mapping[str, Any]) -> dict[str, Any]:
    if probe.get("runner_recoverable_valid_shard") is True:
        basis = (
            ["parent_probe_validated_shard_at", "last_validation_result"]
            if probe.get("parent_probe_after_timeout") is True
            else ["first_parent_valid_shard_at", "last_validation_result"]
        )
        return {
            "state": "recoverable_valid_shard",
            "recoverable": True,
            "basis": basis,
        }
    if probe.get("shard_parent_valid") is False:
        return {
            "state": "not_recoverable_parent_invalid_shard",
            "recoverable": False,
            "basis": ["shard_parent_valid", "last_validation_result"],
        }
    return {
        "state": "unknown",
        "recoverable": None,
        "basis": ["shard_parent_valid"],
    }


def _probe_last_child_event(probe: Mapping[str, Any]) -> dict[str, Any] | None:
    event_type = probe.get("last_child_event_type")
    if not event_type:
        return None
    return {
        "event_type": event_type,
        "message_preview": probe.get("last_message_text_preview"),
    }


def _probe_last_tool_or_command_call(probe: Mapping[str, Any]) -> dict[str, Any] | None:
    kind = probe.get("last_tool_or_command_kind")
    preview = probe.get("last_tool_or_command_preview")
    if not kind and not preview:
        return None
    return {
        "kind": kind,
        "preview": preview,
    }


def _probe_unobservable_reasons(probe: Mapping[str, Any]) -> dict[str, str]:
    reasons: dict[str, str] = {}
    for item in probe.get("unknowns") or []:
        if not isinstance(item, Mapping):
            continue
        field = item.get("field")
        reason = item.get("reason")
        if isinstance(field, str) and field and isinstance(reason, str) and reason:
            reasons.setdefault(field, reason)
    if probe.get("child_timeout_at") is None:
        if probe.get("timeout") is True:
            reasons.setdefault("child_timeout_at", "adapter_did_not_report_child_timeout_time")
        else:
            reasons.setdefault("child_timeout_at", "child_did_not_timeout")
    if probe.get("child_elapsed_seconds") is None:
        reasons.setdefault("child_elapsed_seconds", "adapter_did_not_report_child_elapsed_time")
    if probe.get("shard_exists_at_timeout") is None:
        reasons.setdefault(
            "shard_exists_at_timeout",
            "no_direct_timeout_instant_shard_observation_available"
            if probe.get("timeout") is True
            else "child_did_not_timeout",
        )
    if probe.get("last_child_event") is None:
        reasons.setdefault("last_child_event", "no_child_json_event_observed")
    if probe.get("last_tool_or_command_call") is None:
        reasons.setdefault("last_tool_or_command_call", "no_tool_or_command_call_observed")
    if probe.get("last_child_message_preview") is None:
        reasons.setdefault("last_child_message_preview", "no_child_message_observed")
    if probe.get("candidate_cause_confidence") is None:
        reasons.setdefault("candidate_cause_confidence", "no_candidate_cause_supported_by_probe")
    return reasons


def _with_probe_failure_code(
    probe: Mapping[str, Any],
    child_failure_code: str,
) -> dict[str, Any]:
    updated = dict(probe)
    updated["child_failure_code"] = child_failure_code
    updated["candidate_causes"] = _probe_candidate_causes(updated)
    updated["facts"] = _probe_facts(updated)
    return _finalize_child_attempt_probe_contract(updated)


def _update_attempt_probe_failure_code(
    attempt_record: dict[str, Any],
    child_failure_code: str,
) -> None:
    probe = attempt_record.get("attempt_probe")
    if not isinstance(probe, Mapping):
        return
    attempt_record["attempt_probe"] = _with_probe_failure_code(
        probe,
        child_failure_code,
    )


def _update_attempt_probe_validation(
    attempt_record: dict[str, Any],
    *,
    validation: Mapping[str, Any],
    child_failure_code: Any,
) -> None:
    probe = attempt_record.get("attempt_probe")
    if not isinstance(probe, Mapping):
        return
    updated = dict(probe)
    validation_errors = _bounded_validation_errors(validation)
    valid = bool(validation.get("valid"))
    validation_at = _utc_now()
    updated["last_validation_attempt_at"] = validation_at
    if updated.get("timeout") is True:
        updated["parent_probe_after_timeout"] = True
        if not updated.get("parent_probe_observed_shard_at"):
            updated["parent_probe_observed_shard_at"] = validation_at
        updated["parent_probe_validation_attempt_at"] = validation_at
        updated["parent_probe_validated_shard_at"] = validation_at if valid else None
        updated["first_parent_valid_shard_at"] = None
    elif valid and not updated.get("first_parent_valid_shard_at"):
        updated["first_parent_valid_shard_at"] = validation_at
    updated["last_validation_result"] = {
        "state": "valid" if valid else "invalid",
        "valid": valid,
        "error_count": len(validation.get("errors", []))
        if isinstance(validation.get("errors"), list)
        else 0,
        "errors": validation_errors,
    }
    updated["validation_error_count"] = updated["last_validation_result"]["error_count"]
    updated["validation_errors"] = validation_errors
    updated["missing_required_fields"] = _missing_required_fields(validation_errors)
    updated["shard_parent_valid"] = valid
    if isinstance(child_failure_code, str) and child_failure_code:
        updated["child_failure_code"] = child_failure_code
    updated["candidate_causes"] = _probe_candidate_causes(updated)
    updated["facts"] = _probe_facts(updated)
    attempt_record["attempt_probe"] = _finalize_child_attempt_probe_contract(updated)


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _classify_child_failure_code(
    *,
    result: RunnerResult,
    command_context: Mapping[str, Any],
    child_summary: Mapping[str, Any],
) -> str | None:
    if result.status == "completed":
        return None
    if child_summary.get("timeout") is True:
        return CODEX_CHILD_TIMEOUT
    if result.failure_category == "missing_shard":
        return CODEX_CHILD_MISSING_SHARD
    observed = _child_diagnostic_text(
        result=result,
        command_context=command_context,
        child_summary=child_summary,
    )
    lower = observed.lower()
    non_retry_code = _non_capacity_child_failure_code(lower)
    if non_retry_code:
        return non_retry_code
    if _looks_like_capacity_failure(lower):
        return CODEX_CHILD_MODEL_CAPACITY
    if result.failure_category == "invalid_shard":
        return CODEX_CHILD_SCHEMA_INVALID
    if result.failure_category == "codex_exec_failed":
        return CODEX_CHILD_EXEC_FAILED
    return result.failure_category


def _non_capacity_child_failure_code(observed_lower: str) -> str | None:
    marker_groups = (
        (CODEX_CHILD_BILLING_DISABLED, ("billing", "payment required", "past due")),
        (
            CODEX_CHILD_QUOTA_EXHAUSTED,
            ("quota", "insufficient_quota", "usage limit", "rate limit", "too many requests"),
        ),
        (
            CODEX_CHILD_AUTH_BLOCKED,
            ("auth", "authorization", "unauthorized", "unauthenticated", "credential", "login", "api key"),
        ),
        (
            CODEX_CHILD_POLICY_BLOCKED,
            (
                "policy blocked",
                "content policy",
                "policy violation",
                "request blocked by policy",
                "request denied by policy",
                "safety policy",
                "disallowed",
            ),
        ),
        (
            CODEX_CHILD_SANDBOX_BLOCKED,
            (
                "sandbox",
                "sandbox approval",
                "approval blocked",
                "approval required",
                "trusted directory",
                "not inside a trusted directory",
            ),
        ),
        (CODEX_CHILD_PERMISSION_DENIED, ("permission denied", "access denied", "eacces")),
    )
    for code, markers in marker_groups:
        if any(marker in observed_lower for marker in markers):
            return code
    return None


def _looks_like_capacity_failure(observed_lower: str) -> bool:
    markers = (
        "selected model is at capacity",
        "model is at capacity",
        "model capacity",
        "at capacity. please try a different model",
        "temporarily unavailable",
        "service unavailable",
        "server overloaded",
        "overloaded",
        "try again later",
    )
    return any(marker in observed_lower for marker in markers)


def _child_diagnostic_text(
    *,
    result: RunnerResult,
    command_context: Mapping[str, Any],
    child_summary: Mapping[str, Any],
) -> str:
    stdout_stderr_summary = _stdout_stderr_summary(result.message or "")
    parts: list[str] = [
        result.failure_category or "",
        str(stdout_stderr_summary.get("stderr") or ""),
        str(stdout_stderr_summary.get("stdout") or ""),
        json.dumps(dict(child_summary), sort_keys=True),
        str(child_summary.get("last_message_text_preview") or ""),
        str(child_summary.get("last_command") or ""),
        str(child_summary.get("last_tool_name") or ""),
    ]
    return " ".join(part for part in parts if part)


def _attempt_last_message_preview(
    *,
    result: RunnerResult,
    child_summary: Mapping[str, Any],
) -> str | None:
    preview = child_summary.get("last_message_text_preview")
    if isinstance(preview, str) and preview:
        return preview
    for event in reversed(result.events):
        message = event.get("child_message")
        if isinstance(message, str) and message:
            return _bounded_preview(message)
    return None


def _raw_child_artifacts(
    command_context: Mapping[str, Any],
    child_summary: Mapping[str, Any],
) -> dict[str, Any]:
    summary_artifacts = child_summary.get("artifacts")
    if isinstance(summary_artifacts, Mapping):
        return dict(summary_artifacts)
    artifacts = command_context.get("child_event_artifacts")
    if isinstance(artifacts, Mapping):
        return dict(artifacts)
    return {}


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


def _append_task_trace_event(
    run_dir: Path,
    task: dict[str, Any],
    event: Mapping[str, Any],
) -> None:
    trace_record = _trace_record(run_dir, event)
    append_trace_record(run_dir, trace_record)
    trace_ids: list[str] = list(task.get("trace_event_ids") or [])
    trace_ids.append(trace_record["event_id"])
    task["trace_event_ids"] = trace_ids


def _append_retry_trace_event(
    run_dir: Path,
    task: dict[str, Any],
    retry_plan: Mapping[str, Any],
) -> None:
    attempt_record = _latest_attempt_diagnostic(task) or {}
    retry_decision = str(retry_plan.get("retry_decision") or attempt_record.get("retry_decision") or "do_not_retry")
    event = _codex_event(
        "retry_decision",
        task,
        child_thread_id=str(attempt_record.get("child_thread_id") or task.get("last_child_thread_id") or "unknown"),
        child_status="retrying" if retry_decision == "retry" else "failed",
        child_message=_retry_trace_message(task, retry_plan, attempt_record),
        failure_category=_trace_failure_category(task, attempt_record),
        raw_event=_retry_trace_raw_event(task, retry_plan, attempt_record),
    )
    _append_task_trace_event(run_dir, task, event)


def _retry_trace_message(
    task: Mapping[str, Any],
    retry_plan: Mapping[str, Any],
    attempt_record: Mapping[str, Any],
) -> str:
    decision = str(retry_plan.get("retry_decision") or attempt_record.get("retry_decision") or "do_not_retry")
    child_failure_code = str(attempt_record.get("child_failure_code") or task.get("child_failure_code") or "unknown")
    attempt = int(attempt_record.get("attempt") or task.get("attempt") or 0)
    max_attempts = int(attempt_record.get("max_attempts") or task.get("max_attempts") or 0)
    details = [
        f"retry_decision={decision}",
        f"attempt={attempt}/{max_attempts}",
        f"child_failure_code={child_failure_code}",
    ]
    computed_backoff = retry_plan.get("computed_backoff_seconds")
    if computed_backoff is not None:
        details.append(f"computed_backoff_seconds={computed_backoff}")
    actual_sleep = retry_plan.get("actual_sleep_seconds")
    if actual_sleep is not None:
        details.append(f"actual_sleep_seconds={actual_sleep}")
    exhausted_reason = retry_plan.get("retry_exhausted_reason")
    if exhausted_reason:
        details.append(f"retry_exhausted_reason={exhausted_reason}")
    return "; ".join(details)


def _retry_trace_raw_event(
    task: Mapping[str, Any],
    retry_plan: Mapping[str, Any],
    attempt_record: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "task_id": task.get("id"),
        "attempt": int(attempt_record.get("attempt") or task.get("attempt") or 0),
        "max_attempts": int(attempt_record.get("max_attempts") or task.get("max_attempts") or 0),
        "child_thread_id": attempt_record.get("child_thread_id") or task.get("last_child_thread_id"),
        "child_failure_code": attempt_record.get("child_failure_code") or task.get("child_failure_code"),
        "failure_category": task.get("failure_category"),
        "timeout": bool(attempt_record.get("timeout")),
        "returncode": attempt_record.get("returncode"),
        "retry_decision": retry_plan.get("retry_decision") or attempt_record.get("retry_decision"),
        "computed_backoff_seconds": retry_plan.get("computed_backoff_seconds"),
        "actual_sleep_seconds": retry_plan.get("actual_sleep_seconds"),
        "batch_sleep_seconds": retry_plan.get("batch_sleep_seconds"),
        "retry_exhausted_reason": retry_plan.get("retry_exhausted_reason"),
    }


def _trace_failure_category(
    task: Mapping[str, Any],
    attempt_record: Mapping[str, Any],
) -> str | None:
    failure_category = str(task.get("failure_category") or "")
    if failure_category in TRACE_FAILURE_CATEGORIES:
        return failure_category
    child_failure_code = str(attempt_record.get("child_failure_code") or task.get("child_failure_code") or "")
    if child_failure_code == CODEX_CHILD_MODEL_CAPACITY:
        return "codex_exec_failed"
    return None


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
                "angle_id": task.get("angle_id"),
                "report_section": task.get("report_section"),
                "expected_evidence": list(task.get("expected_evidence") or []),
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
                "promotion_status": "eligible",
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
    release_identity = release_validation_identity_from_payload(task)
    release_instruction = ""
    if release_identity:
        release_instruction = (
            "This is a Public Beta release-validation run. For every SearchResult record, "
            f"set task_id `{task.get('search_task_id') or task.get('id')}`, "
            f"angle_id `{task.get('angle_id')}`, route `{task.get('route')}`, "
            "provider `codex-native`, provider_mode `real`, retrieval_status `fetched`, "
            "policy_decision `allowed`, handoff_artifact `search_results.jsonl`, "
            f"prompt_id `{release_identity['prompt_id']}`, "
            f"suite_id `{release_identity['suite_id']}`, "
            f"prompt_hash `{release_identity['prompt_hash']}`, "
            "and do not set hidden_codex_api_call or codex_native_api_call. "
            "Do not use fixture, manual, user-provided-only, or post-hoc provider_mode values. "
        )
    # Child shard contract: the parent accepts only Evidence Schema v0 envelopes
    # validated by evidence_schema.validate_artifacts; legacy shard-specific
    # schema_version values are intentionally rejected.
    required_top_level_fields = ", ".join(
        f"`{field}`" for field in EVIDENCE_SCHEMA_V0_REQUIRED_TOP_LEVEL_FIELDS
    )
    return (
        "Run this bounded research shard task and write only schema-valid "
        f"evidence to {shard_path}. "
        "The child `evidence_shard.json` contract is an Evidence Schema v0 JSON envelope, "
        f"not a legacy shard-specific payload: set `schema_version` exactly `{EVIDENCE_SCHEMA_VERSION}` "
        f"and include required top-level fields {required_top_level_fields}. "
        f"Set top-level `run_id` exactly `{run_dir.name}`, `mode` exactly `codex-plugin`, "
        "`search_provider` exactly `codex-native`, and `vlm_provider` exactly `codex-interactive` "
        "unless the input evidence specifies another allowed VLM provider. "
        "Do not write `schema_version: \"codex-deepresearch.evidence-shard.v0\"`. "
        "First create the shard directory if needed and write a minimal valid `evidence_shard.json` "
        f"to {shard_path} before any optional sidecars; keep replacing it with richer valid evidence as you proceed. "
        "If you use inline scripts for local JSON or file manipulation, invoke them with `python3`, not `python`. "
        f"Write claim text, caveats, rationales, and synthesized source snippets in {response_language}; "
        "for Korean queries, translate/summarize English source findings into Korean user-facing prose. "
        "Only direct quote_spans.quote values should remain verbatim in the source language. "
        "Prioritize a compact shard with decision-ready claims and do not read repository docs or skills unless the schema is otherwise impossible to satisfy. "
        f"{release_instruction}"
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


def _codex_exec_child_artifact_paths(
    *,
    run_dir: Path,
    task_id: str,
    attempt: int,
) -> dict[str, Path]:
    safe_task_id = _artifact_safe_task_id(task_id)
    safe_attempt = max(1, attempt)
    artifact_dir = run_dir / CHILD_EVENTS_DIRNAME / safe_task_id / f"attempt_{safe_attempt:03d}"
    return {
        "stdout_jsonl_path": artifact_dir / CODEX_EXEC_STDOUT_FILENAME,
        "stderr_path": artifact_dir / CODEX_EXEC_STDERR_FILENAME,
        "last_child_event_path": artifact_dir / LAST_CHILD_EVENT_FILENAME,
    }


def _codex_exec_legacy_child_artifact_paths(
    *,
    run_dir: Path,
    task_id: str,
) -> dict[str, Path]:
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
    child_started_at: str | None = None,
    child_finished_at: str | None = None,
    child_timed_out_at: str | None = None,
    elapsed_seconds: float | None = None,
    child_thread_id: str | None = None,
    output_shard_path: Path | str | None = None,
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
        child_started_at=child_started_at,
        child_finished_at=child_finished_at,
        child_timed_out_at=child_timed_out_at,
        elapsed_seconds=elapsed_seconds,
        child_thread_id=child_thread_id,
        output_shard_path=output_shard_path,
    )
    _write_json(artifacts["last_child_event_path"], summary)
    legacy_artifacts = _codex_exec_legacy_child_artifact_paths(
        run_dir=Path(artifacts["stdout_jsonl_path"]).parents[3],
        task_id=str(task.get("id") or "unknown"),
    )
    _mirror_child_diagnostic_artifacts(
        artifacts=artifacts,
        legacy_artifacts=legacy_artifacts,
    )
    return summary


def _mirror_child_diagnostic_artifacts(
    *,
    artifacts: Mapping[str, Path],
    legacy_artifacts: Mapping[str, Path],
) -> None:
    for key, legacy_path in legacy_artifacts.items():
        source_path = artifacts[key]
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")


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
    child_started_at: str | None,
    child_finished_at: str | None,
    child_timed_out_at: str | None,
    elapsed_seconds: float | None,
    child_thread_id: str | None,
    output_shard_path: Path | str | None,
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
        "child_started_at": child_started_at,
        "child_finished_at": child_finished_at,
        "child_timed_out_at": child_timed_out_at,
        "elapsed_seconds": elapsed_seconds,
        "child_thread_id": child_thread_id,
        "output_shard_path": str(output_shard_path) if output_shard_path is not None else None,
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
    return _redact_child_probe_secret_text(_output_summary(value, limit=limit))


def _redact_child_probe_secret_text(value: str) -> str:
    redacted = CHILD_PROBE_BEARER_PATTERN.sub(
        f"Bearer {REDACTED_CHILD_PROBE_SECRET}",
        value,
    )
    redacted = CHILD_PROBE_SECRET_FLAG_PATTERN.sub(
        lambda match: f"{match.group(1)}={REDACTED_CHILD_PROBE_SECRET}",
        redacted,
    )
    redacted = CHILD_PROBE_SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}={REDACTED_CHILD_PROBE_SECRET}",
        redacted,
    )
    return redacted


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


def _elapsed_seconds(started_monotonic: float) -> float:
    return round(max(0.0, time.monotonic() - started_monotonic), 3)


def _number_or_none(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 3)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return round(parsed, 3)


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


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


def _with_task_angle_metadata(
    record: Mapping[str, Any],
    task: Mapping[str, Any],
) -> dict[str, Any]:
    enriched = dict(record)
    defaults = {
        "angle_id": task.get("angle_id"),
        "route": task.get("route"),
        "source_task_id": task.get("id"),
        "report_section": task.get("report_section"),
        "expected_evidence": list(task.get("expected_evidence") or []),
    }
    for key, value in defaults.items():
        if key in enriched and enriched.get(key):
            continue
        if value:
            enriched[key] = value
    return enriched


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
        retryable = _task_is_retryable(task)
    latest_attempt = _latest_attempt_diagnostic(task) or {}
    record = {
        "task_id": task.get("id"),
        "state": state,
        "reason": reason,
        "output_shard_path": task.get("output_shard_path"),
        "adapter": adapter,
        "failure_category": failure_category,
        "child_failure_code": task.get("child_failure_code"),
        "timeout": bool(latest_attempt.get("timeout")),
        "returncode": latest_attempt.get("returncode"),
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
    _add_attempt_diagnostics(record, task)
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
    latest_attempt = _latest_attempt_diagnostic(task) or {}
    record = {
        "task_id": task.get("id"),
        "state": state,
        "adapter": task.get("last_adapter") or _adapter_from_assignment(task),
        "failure_category": failure_category,
        "child_failure_code": task.get("child_failure_code"),
        "timeout": bool(latest_attempt.get("timeout")),
        "returncode": latest_attempt.get("returncode"),
        "retryable": _task_is_retryable(task),
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
    release_validation = task.get("release_search_handoff_validation")
    if isinstance(release_validation, Mapping):
        record["release_search_handoff_validation"] = copy.deepcopy(release_validation)
    validation = task.get("validation")
    if isinstance(validation, Mapping):
        record["validation"] = copy.deepcopy(validation)
    _add_attempt_diagnostics(record, task)
    return record


def _task_is_retryable(task: Mapping[str, Any]) -> bool:
    latest_attempt = _latest_attempt_diagnostic(task) or {}
    retry_decision = str(latest_attempt.get("retry_decision") or "")
    if retry_decision == "retry":
        return True
    if retry_decision in {"do_not_retry", "retry_exhausted"}:
        return False
    preserved_failure = task.get("parallel_failure")
    if isinstance(preserved_failure, Mapping) and isinstance(preserved_failure.get("retryable"), bool):
        return bool(preserved_failure.get("retryable"))
    failure_category = str(task.get("failure_category") or "")
    return str(task.get("state") or "") == "retryable" or failure_category in RETRY_SAFE_FAILURES


def _accepted_shard_record(task: Mapping[str, Any], shard_path: Path) -> dict[str, Any]:
    record = {"task_id": task["id"], "path": str(shard_path)}
    diagnostics = _accepted_shard_diagnostics(task)
    if diagnostics:
        record["diagnostics"] = diagnostics
    return record


def _accepted_shard_diagnostics(task: Mapping[str, Any]) -> dict[str, Any]:
    command_context = task.get("last_command_context")
    diagnostics: dict[str, Any] = {}
    if isinstance(command_context, Mapping) and command_context.get("timeout_after_valid_shard") is True:
        diagnostics["timeout_after_valid_shard"] = True
        for key in (
            "valid_evidence_shard_exists",
            "valid_evidence_shard_path",
            "expected_sidecars",
            "missing_expected_sidecars",
            "missing_expected_sidecar_paths",
        ):
            if key in command_context:
                diagnostics[key] = copy.deepcopy(command_context[key])
    _add_attempt_diagnostics(diagnostics, task)
    return diagnostics


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


def _add_attempt_diagnostics(
    record: dict[str, Any],
    task: Mapping[str, Any],
) -> None:
    diagnostics = task.get("attempt_diagnostics")
    if not isinstance(diagnostics, list) or not diagnostics:
        return
    copied = [copy.deepcopy(item) for item in diagnostics if isinstance(item, Mapping)]
    if not copied:
        return
    record["attempt_diagnostics"] = copied
    summary = _task_retry_summary(task)
    if summary:
        record["retry_summary"] = summary


def _retry_summary(
    tasks: Sequence[Mapping[str, Any]],
    *,
    retry_policy: Any,
) -> dict[str, Any]:
    task_summaries: list[dict[str, Any]] = []
    child_failure_counts: dict[str, int] = {}
    total_attempts = 0
    retry_count = 0
    retry_exhausted_count = 0
    recovered_after_capacity_count = 0
    capacity_failure_count = 0
    for task in tasks:
        diagnostics = [
            item
            for item in task.get("attempt_diagnostics", [])
            if isinstance(item, Mapping)
        ] if isinstance(task.get("attempt_diagnostics"), list) else []
        if not diagnostics:
            continue
        total_attempts += len(diagnostics)
        for attempt in diagnostics:
            child_failure_code = attempt.get("child_failure_code")
            if child_failure_code:
                key = str(child_failure_code)
                child_failure_counts[key] = child_failure_counts.get(key, 0) + 1
                if key == CODEX_CHILD_MODEL_CAPACITY:
                    capacity_failure_count += 1
            if attempt.get("retry_decision") == "retry":
                retry_count += 1
            if attempt.get("retry_decision") == "retry_exhausted":
                retry_exhausted_count += 1
        task_summary = _task_retry_summary(task)
        if task_summary:
            task_summaries.append(task_summary)
            if task_summary.get("recovered_after_capacity") is True:
                recovered_after_capacity_count += 1
    policy = dict(retry_policy) if isinstance(retry_policy, Mapping) else {}
    return {
        "policy": policy,
        "total_attempts": total_attempts,
        "retry_count": retry_count,
        "capacity_failure_count": capacity_failure_count,
        "retry_exhausted_count": retry_exhausted_count,
        "recovered_after_capacity_count": recovered_after_capacity_count,
        "child_failure_counts": child_failure_counts,
        "tasks": task_summaries,
    }


def _task_retry_summary(task: Mapping[str, Any]) -> dict[str, Any]:
    diagnostics = task.get("attempt_diagnostics")
    if not isinstance(diagnostics, list) or not diagnostics:
        return {}
    attempts = [item for item in diagnostics if isinstance(item, Mapping)]
    if not attempts:
        return {}
    retry_decisions = [str(item.get("retry_decision") or "") for item in attempts]
    capacity_failures = [
        item
        for item in attempts
        if item.get("child_failure_code") == CODEX_CHILD_MODEL_CAPACITY
    ]
    retry_count = sum(1 for decision in retry_decisions if decision == "retry")
    retry_exhausted = any(decision == "retry_exhausted" for decision in retry_decisions)
    recovered_after_capacity = bool(
        capacity_failures
        and str(task.get("state") or "") in {"completed", "merged"}
    )
    return {
        "task_id": task.get("id"),
        "attempts": len(attempts),
        "max_attempts": int(task.get("max_attempts") or 0),
        "retry_count": retry_count,
        "capacity_failure_count": len(capacity_failures),
        "retry_exhausted": retry_exhausted,
        "retry_exhausted_reason": task.get("retry_exhausted_reason"),
        "recovered_after_capacity": recovered_after_capacity,
        "final_state": task.get("state"),
        "final_child_failure_code": task.get("child_failure_code"),
        "retry_decisions": retry_decisions,
        "computed_backoff_seconds": [
            item.get("computed_backoff_seconds")
            for item in attempts
            if item.get("computed_backoff_seconds") is not None
        ],
        "actual_sleep_seconds": [
            item.get("actual_sleep_seconds")
            for item in attempts
            if item.get("actual_sleep_seconds") is not None
        ],
    }


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


def _partial_parallel_summary_from_merge(
    *,
    status: str | None,
    planned_task_count: int,
    merge_status: Mapping[str, Any] | None,
    parallel_degraded: bool,
) -> dict[str, Any]:
    if not merge_status:
        return _partial_parallel_summary(
            status=status,
            planned_task_count=planned_task_count,
            accepted_shard_count=0,
            failure_counts={},
            retry_summary={},
            parallel_degraded=parallel_degraded,
        )
    accepted_count = _accepted_shard_count(merge_status)
    return _partial_parallel_summary(
        status=status,
        planned_task_count=planned_task_count,
        accepted_shard_count=accepted_count,
        failure_counts=_mapping_like(merge_status.get("failure_counts")),
        retry_summary=_mapping_like(merge_status.get("retry_summary")),
        parallel_degraded=parallel_degraded
        or bool(merge_status.get("parallel_degraded")),
    )


def _partial_parallel_summary(
    *,
    status: str | None,
    planned_task_count: int,
    accepted_shard_count: int,
    failure_counts: Mapping[str, Any],
    retry_summary: Mapping[str, Any],
    parallel_degraded: bool,
) -> dict[str, Any]:
    failed_count = _safe_int(failure_counts.get("failed_tasks"))
    blocked_count = _safe_int(failure_counts.get("blocked_tasks"))
    rejected_count = _safe_int(failure_counts.get("rejected_shards"))
    discarded_count = _safe_int(failure_counts.get("discarded_tasks"))
    retried_count = _safe_int(retry_summary.get("retry_count"))
    retry_exhausted_count = _safe_int(retry_summary.get("retry_exhausted_count"))
    omitted_count = max(0, planned_task_count - accepted_shard_count)
    partial = (
        status == "completed_partial_parallel"
        or (accepted_shard_count > 0 and omitted_count > 0)
        or failed_count > 0
        or blocked_count > 0
        or rejected_count > 0
        or discarded_count > 0
        or parallel_degraded
    )
    reason_category = _partial_reason_category(
        partial=partial,
        accepted_shard_count=accepted_shard_count,
        failed_count=failed_count,
        blocked_count=blocked_count,
        rejected_count=rejected_count,
        discarded_count=discarded_count,
        retry_exhausted_count=retry_exhausted_count,
        omitted_count=omitted_count,
        parallel_degraded=parallel_degraded,
    )
    return {
        "partial": partial,
        "reason_category": reason_category,
        "planned_task_count": planned_task_count,
        "accepted_shard_count": accepted_shard_count,
        "omitted_task_count": omitted_count,
        "failed_task_count": failed_count,
        "blocked_task_count": blocked_count,
        "rejected_shard_count": rejected_count,
        "discarded_task_count": discarded_count,
        "retried_task_count": retried_count,
        "retry_exhausted_task_count": retry_exhausted_count,
        "parallel_degraded": parallel_degraded,
        "failure_category_counts": dict(failure_counts.get("by_category", {}))
        if isinstance(failure_counts.get("by_category"), Mapping)
        else {},
    }


def _partial_reason_category(
    *,
    partial: bool,
    accepted_shard_count: int,
    failed_count: int,
    blocked_count: int,
    rejected_count: int,
    discarded_count: int,
    retry_exhausted_count: int,
    omitted_count: int,
    parallel_degraded: bool,
) -> str:
    if not partial:
        return "none"
    if accepted_shard_count == 0:
        return "no_accepted_shards"
    if retry_exhausted_count > 0:
        return "retry_exhausted"
    if failed_count > 0:
        return "failed_tasks"
    if blocked_count > 0:
        return "blocked_tasks"
    if rejected_count > 0:
        return "rejected_shards"
    if discarded_count > 0:
        return "discarded_tasks"
    if parallel_degraded:
        return "parallel_degraded"
    if omitted_count > 0:
        return "omitted_tasks"
    return "partial_unknown"


def _accepted_shard_count(payload: Mapping[str, Any]) -> int:
    explicit = payload.get("accepted_shard_count")
    if isinstance(explicit, int):
        return max(0, explicit)
    accepted = payload.get("accepted_shards")
    if isinstance(accepted, list):
        return len(accepted)
    evidence_source = payload.get("evidence_source")
    if isinstance(evidence_source, Mapping):
        return _safe_int(evidence_source.get("accepted_shards"))
    return 0


def _mapping_like(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(0, value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _merge_diagnostics(
    *,
    accepted_shards: Sequence[Mapping[str, Any]],
    failed_tasks: Sequence[Mapping[str, Any]],
    blocked_tasks: Sequence[Mapping[str, Any]],
    rejected_shards: Sequence[Mapping[str, Any]],
    discarded_tasks: Sequence[Mapping[str, Any]],
    retry_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    retry_summary = retry_summary if isinstance(retry_summary, Mapping) else {}
    shard_counts = _shard_count_diagnostics(
        accepted_shards=accepted_shards,
        failed_tasks=failed_tasks,
        blocked_tasks=blocked_tasks,
        rejected_shards=rejected_shards,
        discarded_tasks=discarded_tasks,
    )
    if accepted_shards:
        accepted_warnings = _accepted_shard_warnings(accepted_shards)
        diagnostics: dict[str, Any] = {"shard_counts": shard_counts}
        if accepted_warnings:
            diagnostics.update({
                "accepted_shard_warning_count": len(accepted_warnings),
                "accepted_shard_warnings": accepted_warnings,
            })
        if int(retry_summary.get("recovered_after_capacity_count") or 0) > 0:
            diagnostics["recovered_after_capacity_count"] = retry_summary.get(
                "recovered_after_capacity_count"
            )
            diagnostics["capacity_retry_count"] = retry_summary.get("retry_count")
        exhausted_release_failures = [
            task for task in failed_tasks if _task_has_exhausted_release_handoff_failure(task)
        ]
        if exhausted_release_failures:
            first = exhausted_release_failures[0]
            diagnostics.update(
                {
                    "actionable_cause": "release validation search handoff retries exhausted",
                    "first_failure_category": first.get("failure_category"),
                    "first_child_failure_code": first.get("child_failure_code"),
                    "first_failed_task_id": first.get("task_id"),
                    "first_failed_adapter": first.get("adapter"),
                    "first_failed_retryable": first.get("retryable"),
                    "first_failed_diagnostic": first.get("diagnostic"),
                    "retry_exhausted": True,
                    "retry_exhausted_count": retry_summary.get("retry_exhausted_count"),
                    "child_failure_counts": retry_summary.get("child_failure_counts"),
                }
            )
        return diagnostics
    if failed_tasks:
        first = failed_tasks[0]
        diagnostics = {
            "shard_counts": shard_counts,
            "actionable_cause": "no evidence shards were accepted because child tasks failed",
            "first_failure_category": first.get("failure_category"),
            "first_child_failure_code": first.get("child_failure_code"),
            "first_failed_task_id": first.get("task_id"),
            "first_failed_adapter": first.get("adapter"),
            "first_failed_retryable": first.get("retryable"),
            "first_failed_diagnostic": first.get("diagnostic"),
        }
        if int(retry_summary.get("retry_exhausted_count") or 0) > 0:
            diagnostics["retry_exhausted"] = True
            diagnostics["retry_exhausted_count"] = retry_summary.get("retry_exhausted_count")
            diagnostics["capacity_failure_count"] = retry_summary.get("capacity_failure_count")
        return diagnostics
    if blocked_tasks:
        first = blocked_tasks[0]
        return {
            "shard_counts": shard_counts,
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
            "shard_counts": shard_counts,
            "actionable_cause": "no evidence shards were accepted because shard validation rejected the output",
            "first_rejected_task_id": first.get("task_id"),
            "first_rejected_reason": first.get("reason"),
        }
    return {
        "shard_counts": shard_counts,
        "actionable_cause": "no evidence shards were accepted",
    }


def _shard_count_diagnostics(
    *,
    accepted_shards: Sequence[Mapping[str, Any]],
    failed_tasks: Sequence[Mapping[str, Any]],
    blocked_tasks: Sequence[Mapping[str, Any]],
    rejected_shards: Sequence[Mapping[str, Any]],
    discarded_tasks: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    return {
        "accepted_shards": len(accepted_shards),
        "rejected_shards": len(rejected_shards),
        "failed_tasks": len(failed_tasks),
        "discarded_tasks": len(discarded_tasks),
        "blocked_tasks": len(blocked_tasks),
    }


def _accepted_shard_warnings(
    accepted_shards: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for shard in accepted_shards:
        diagnostics = shard.get("diagnostics")
        if not isinstance(diagnostics, Mapping):
            continue
        if diagnostics.get("timeout_after_valid_shard") is not True:
            continue
        warnings.append(
            {
                "task_id": shard.get("task_id"),
                "path": shard.get("path"),
                "warning": "timeout_after_valid_shard",
                "missing_expected_sidecars": list(
                    diagnostics.get("missing_expected_sidecars") or []
                ),
                "missing_expected_sidecar_paths": list(
                    diagnostics.get("missing_expected_sidecar_paths") or []
                ),
            }
        )
    return warnings


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
