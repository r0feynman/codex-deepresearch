"""Skill invocation router for the Codex DeepResearch runner."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .budget_estimator import BudgetEstimateError
from .execution_mode import ConfigResolutionError
from .guardrails import GuardrailsError, enforce_guardrails
from .manual_sources import ManualSourcesError, ingest_manual_sources
from .parallel_orchestrator import (
    AdapterUnavailable,
    ParallelOrchestrationError,
    run_parallel_orchestration,
)
from .report_generation import ReportGenerationError, synthesize_report
from .search_handoff import SearchHandoffError, prepare_run
from .verification_matrix import VerificationMatrixError, verify_claims
from .visual_artifacts import (
    VISUAL_PROVIDER_STATUS_FILENAME,
    VISUAL_PROVIDER_STATUS_SCHEMA_VERSION,
    build_visual_provider_status,
)


RUN_STATUS_SCHEMA_VERSION = "codex-deepresearch.run-status.v0"
RUN_STATUS_FILENAME = "run_status.json"
INVOCATION_MODES = ("full-runner", "quick-chat", "manual-handoff", "blocked")

_QUICK_CHAT_MARKERS = (
    "quick answer",
    "quick chat",
    "quick-chat",
    "chat only",
    "chat-only",
)
_FULL_RUNNER_INTENT_MARKERS = (
    "instead run full pipeline",
    "instead run the full pipeline",
    "run full pipeline",
    "run the full pipeline",
    "full pipeline",
    "full runner",
    "full-runner",
    "produce evidence",
    "produce an evidence",
    "produce evidence bundle",
    "create evidence bundle",
    "create an evidence bundle",
    "evidence bundle",
    "durable artifacts",
    "run_status.json",
)
_NO_FULL_RUNNER_PATTERNS = (
    r"\bdo\s+not\s+run\s+(?:the\s+)?full[-\s]?pipeline\b",
    r"\bdon't\s+run\s+(?:the\s+)?full[-\s]?pipeline\b",
    r"\bno\s+full[-\s]?pipeline\b",
    r"\bwithout\s+(?:the\s+)?full[-\s]?pipeline\b",
    r"\bdo\s+not\s+use\s+(?:the\s+)?full[-\s]?runner\b",
    r"\bdon't\s+use\s+(?:the\s+)?full[-\s]?runner\b",
    r"\bno\s+full[-\s]?runner\b",
    r"\bwithout\s+(?:the\s+)?full[-\s]?runner\b",
)
_NEGATED_QUICK_CHAT_PATTERNS = (
    r"\bdo\s+not\s+(?:give\s+me\s+|provide\s+|return\s+|use\s+)?(?:a\s+)?quick\s+(?:answer|chat)\b",
    r"\bdon't\s+(?:give\s+me\s+|provide\s+|return\s+|use\s+)?(?:a\s+)?quick\s+(?:answer|chat)\b",
    r"\bnot\s+(?:a\s+)?quick\s+(?:answer|chat)\b",
    r"\bnever\s+(?:give\s+me\s+|provide\s+|return\s+|use\s+)?(?:a\s+)?quick\s+(?:answer|chat)\b",
    r"\bwithout\s+(?:a\s+)?quick\s+(?:answer|chat)\b",
    r"\bwithout\s+quick\b",
    r"\bno\s+quick\s+(?:answer|chat)\b",
    r"\bdo\s+not\s+(?:give\s+me\s+|provide\s+|return\s+|use\s+)?chat[-\s]?only\b",
    r"\bdon't\s+(?:give\s+me\s+|provide\s+|return\s+|use\s+)?chat[-\s]?only\b",
    r"\bnot\s+chat[-\s]?only\b",
    r"\bno\s+chat[-\s]?only\b",
)
_PARALLEL_SYNTHESIS_STATUSES = {
    "completed_parallel",
    "completed_partial_parallel",
    "completed_fixture",
}
_SYNTHESIZED_SUCCESS_STATUSES = {
    "completed_parallel",
    "completed_partial_parallel",
    "completed_serial_handoff",
    "completed_auto_visual",
    "partial_auto_visual",
    "completed_fixture",
}
_REQUIRED_SYNTHESIZED_ARTIFACTS = (
    "report",
    "evidence",
    "run_status",
    "report_status",
)
_STAGE_OK_STATUSES = {
    "completed",
    "completed_fixture",
    "completed_parallel",
    "completed_partial_parallel",
    "manual_sources_ingested",
    "skipped",
    "visual_candidates_collected",
    "visual_evidence_ingested",
}


def run_skill_invocation(
    invocation: str,
    *,
    runs_dir: str | Path,
    quick_chat: bool = False,
    manual_handoff: bool = False,
    urls: Sequence[str] = (),
    pdfs: Sequence[str | Path] = (),
    image_urls: Sequence[str] = (),
    local_images: Sequence[str | Path] = (),
    labels: Sequence[str] = (),
    route: str | None = None,
    angles: Sequence[str] | None = None,
    budget_preset: str = "standard",
    max_results: int = 8,
    max_sources: int | None = None,
    max_images: int | None = None,
    max_subagents: int | None = None,
    max_agents: int | None = None,
    max_cost_usd: float | None = None,
    confirm_budget: bool = False,
    adapter_name: str = "codex-exec",
    min_tasks: int = 1,
    max_tasks: int | None = None,
    allow_degraded: bool = True,
    require_codex_exec: bool = False,
) -> dict[str, Any]:
    """Route one ``$deep-research`` skill invocation to an explicit mode."""

    normalized_invocation = " ".join(invocation.strip().split())
    question = _question_from_invocation(normalized_invocation)
    if not question:
        return _blocked_without_run(
            invocation=normalized_invocation,
            question=question,
            status="blocked_preflight",
            actionable_cause="invocation did not include a research question",
        )

    selected_mode = select_invocation_mode(
        normalized_invocation,
        quick_chat=quick_chat,
        manual_handoff=manual_handoff,
        urls=urls,
        pdfs=pdfs,
        image_urls=image_urls,
        local_images=local_images,
    )
    if selected_mode == "quick-chat":
        return _quick_chat_status(normalized_invocation, question)
    if selected_mode == "manual-handoff":
        return _run_manual_handoff(
            invocation=normalized_invocation,
            question=question,
            runs_dir=runs_dir,
            urls=urls,
            pdfs=pdfs,
            image_urls=image_urls,
            local_images=local_images,
            labels=labels,
            budget_preset=budget_preset,
        )

    preflight = _preflight_full_runner(
        invocation=normalized_invocation,
        question=question,
        runs_dir=runs_dir,
        adapter_name=adapter_name,
        require_codex_exec=require_codex_exec,
    )
    if preflight is not None:
        return preflight

    try:
        prepared = prepare_run(
            question=question,
            runs_dir=Path(runs_dir),
            mode="codex-plugin",
            search_provider="codex-native",
            budget_preset=budget_preset,
            route=route,
            angles=angles,
            max_results=max_results,
            max_sources=max_sources,
            max_images=max_images,
            max_subagents=max_subagents,
            max_agents=max_agents,
            max_cost_usd=max_cost_usd,
            codex_runner="codex-exec",
            confirm_budget=confirm_budget,
        )
    except (BudgetEstimateError, ConfigResolutionError, SearchHandoffError, OSError) as exc:
        return _blocked_preflight_with_status_dir(
            invocation=normalized_invocation,
            question=question,
            runs_dir=Path(runs_dir),
            actionable_cause=str(exc) or exc.__class__.__name__,
        )

    run_dir = Path(prepared["run_dir"])
    _write_run_status(
        run_dir,
        _base_run_status(
            invocation=normalized_invocation,
            question=question,
            selected_mode="full-runner",
            run_dir=run_dir,
            status="prepared",
            ok=True,
            terminal=False,
            provenance={"type": "full_runner", "adapter": adapter_name},
            diagnostics={
                "actionable_cause": "full-runner prepared; parallel orchestration pending"
            },
            artifacts=_artifact_paths(run_dir, prepared.get("artifacts")),
        ),
    )
    visual_provider_status = _visual_provider_preflight_status(
        run_dir=run_dir,
        adapter_name=adapter_name,
    )
    if visual_provider_status is not None:
        _write_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME, visual_provider_status)
        if visual_provider_status.get("ok") is not True:
            status = _base_run_status(
                invocation=normalized_invocation,
                question=question,
                selected_mode="full-runner",
                run_dir=run_dir,
                status="blocked_missing_visual_provider",
                ok=False,
                terminal=True,
                provenance={
                    "type": "blocked_missing_visual_provider",
                    "adapter": adapter_name,
                    "fixture_only": False,
                    "manual_handoff": False,
                    "real_child_execution": False,
                    "real_use_e2e_eligible": False,
                },
                diagnostics={
                    "actionable_cause": str(
                        visual_provider_status["diagnostics"]["actionable_cause"]
                    )
                },
                artifacts=_artifact_paths(run_dir),
            )
            status["visual_provider"] = _stage_summary(visual_provider_status)
            return _write_run_status(run_dir, status)

    try:
        parallel_status = run_parallel_orchestration(
            run=run_dir,
            adapter_name=adapter_name,
            min_tasks=min_tasks,
            max_tasks=max_tasks,
            allow_degraded=allow_degraded,
            confirm_exhaustive=confirm_budget,
            max_cost_usd=max_cost_usd,
        )
    except (AdapterUnavailable, ParallelOrchestrationError, SearchHandoffError, OSError) as exc:
        status = _base_run_status(
            invocation=normalized_invocation,
            question=question,
            selected_mode="blocked",
            run_dir=run_dir,
            status="blocked_parallel_execution",
            ok=False,
            terminal=True,
            provenance={"type": "blocked_parallel_execution", "adapter": adapter_name},
            diagnostics={"actionable_cause": str(exc) or exc.__class__.__name__},
            artifacts=_artifact_paths(run_dir),
        )
        return _write_run_status(run_dir, status)

    if parallel_status.get("status") not in _PARALLEL_SYNTHESIS_STATUSES:
        status = _terminal_from_parallel(
            invocation=normalized_invocation,
            question=question,
            run_dir=run_dir,
            parallel_status=parallel_status,
        )
        return _write_run_status(run_dir, status)

    guardrails_status: Mapping[str, Any] | None = None
    verify_status: Mapping[str, Any] | None = None
    report_status: Mapping[str, Any] | None = None
    try:
        guardrails_status = enforce_guardrails(run=run_dir)
        verify_status = verify_claims(run=run_dir)
        report_status = synthesize_report(run=run_dir)
    except (
        GuardrailsError,
        VerificationMatrixError,
        ReportGenerationError,
        SearchHandoffError,
        OSError,
    ) as exc:
        status = _base_run_status(
            invocation=normalized_invocation,
            question=question,
            selected_mode="full-runner",
            run_dir=run_dir,
            status="failed_synthesis",
            ok=False,
            terminal=True,
            provenance=_provenance_from_parallel(parallel_status),
            diagnostics={"actionable_cause": str(exc) or exc.__class__.__name__},
            artifacts=_artifact_paths(run_dir),
        )
        status["parallel"] = _parallel_summary(parallel_status)
        return _write_run_status(run_dir, status)

    report_completed = isinstance(report_status, Mapping) and report_status.get("status") == "completed"
    final_status = str(parallel_status.get("status") or "completed_parallel")
    status = _base_run_status(
        invocation=normalized_invocation,
        question=question,
        selected_mode="full-runner",
        run_dir=run_dir,
        status=final_status if report_completed else "failed_synthesis",
        ok=report_completed,
        terminal=True,
        provenance=_provenance_from_parallel(parallel_status),
        diagnostics={
            "actionable_cause": (
                "full-runner completed through synthesis"
                if report_completed
                else "report synthesis did not complete"
            )
        },
        artifacts=_artifact_paths(run_dir),
    )
    status["parallel"] = _parallel_summary(parallel_status)
    status["stages"] = {
        "guardrails": _stage_summary(guardrails_status),
        "verify_claims": _stage_summary(verify_status),
        "synthesize": _stage_summary(report_status),
    }
    return _write_run_status(run_dir, status)


def select_invocation_mode(
    invocation: str,
    *,
    quick_chat: bool = False,
    manual_handoff: bool = False,
    urls: Sequence[str] = (),
    pdfs: Sequence[str | Path] = (),
    image_urls: Sequence[str] = (),
    local_images: Sequence[str | Path] = (),
) -> str:
    """Select the explicit skill invocation mode."""

    if quick_chat or _has_quick_chat_marker(invocation):
        return "quick-chat"
    if manual_handoff or urls or pdfs or image_urls or local_images:
        return "manual-handoff"
    return "full-runner"


def _run_manual_handoff(
    *,
    invocation: str,
    question: str,
    runs_dir: str | Path,
    urls: Sequence[str],
    pdfs: Sequence[str | Path],
    image_urls: Sequence[str],
    local_images: Sequence[str | Path],
    labels: Sequence[str],
    budget_preset: str,
) -> dict[str, Any]:
    try:
        manual_status = ingest_manual_sources(
            runs_dir=runs_dir,
            question=question,
            urls=urls,
            pdfs=pdfs,
            image_urls=image_urls,
            local_images=local_images,
            labels=labels,
            budget_preset=budget_preset,
        )
    except (ConfigResolutionError, ManualSourcesError, OSError) as exc:
        return _blocked_preflight_with_status_dir(
            invocation=invocation,
            question=question,
            runs_dir=Path(runs_dir),
            actionable_cause=str(exc) or exc.__class__.__name__,
        )

    run_dir = Path(str(manual_status["run_dir"]))
    ok = str(manual_status.get("status")) != "failed_validation"
    status = _base_run_status(
        invocation=invocation,
        question=question,
        selected_mode="manual-handoff",
        run_dir=run_dir,
        status=str(manual_status.get("status") or "manual_sources_ingested"),
        ok=ok,
        terminal=True,
        provenance=_manual_provenance(manual_status),
        diagnostics={
            "actionable_cause": (
                "manual handoff recorded user-provided sources; no external search was run"
            )
        },
        artifacts=_artifact_paths(run_dir, manual_status.get("artifacts")),
    )
    status["manual_handoff"] = _stage_summary(manual_status)
    return _write_run_status(run_dir, status)


def _preflight_full_runner(
    *,
    invocation: str,
    question: str,
    runs_dir: str | Path,
    adapter_name: str,
    require_codex_exec: bool,
) -> dict[str, Any] | None:
    runs_path = Path(runs_dir)
    try:
        runs_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _blocked_without_run(
            invocation=invocation,
            question=question,
            status="blocked_preflight",
            actionable_cause=f"cannot create runs directory {runs_path}: {exc}",
        )

    runner_path = Path(__file__).resolve().parents[2] / "scripts" / "codex-deepresearch"
    if not runner_path.is_file():
        return _blocked_preflight_with_status_dir(
            invocation=invocation,
            question=question,
            runs_dir=runs_path,
            actionable_cause=f"runner executable is missing: {runner_path}",
        )
    normalized_adapter = adapter_name.strip().lower().replace("_", "-")
    if require_codex_exec and normalized_adapter == "codex-exec" and shutil.which("codex") is None:
        return _blocked_preflight_with_status_dir(
            invocation=invocation,
            question=question,
            runs_dir=runs_path,
            actionable_cause="codex exec is not available on PATH",
            diagnostics_extra={
                "required_capability": "codex-exec",
                "retry": "Install/authenticate Codex CLI or rerun with an explicit fixture/manual mode.",
            },
        )
    return None


def _blocked_preflight_with_status_dir(
    *,
    invocation: str,
    question: str,
    runs_dir: Path,
    actionable_cause: str,
    diagnostics_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        run_dir = _create_status_run_dir(runs_dir)
    except OSError as exc:
        return _blocked_without_run(
            invocation=invocation,
            question=question,
            status="blocked_preflight",
            actionable_cause=f"{actionable_cause}; additionally could not create run_status.json: {exc}",
        )
    diagnostics = {"actionable_cause": actionable_cause}
    if diagnostics_extra:
        diagnostics.update(dict(diagnostics_extra))
    status = _base_run_status(
        invocation=invocation,
        question=question,
        selected_mode="blocked",
        run_dir=run_dir,
        status="blocked_preflight",
        ok=False,
        terminal=True,
        provenance={"type": "blocked_preflight"},
        diagnostics=diagnostics,
        artifacts=_artifact_paths(run_dir),
    )
    return _write_run_status(run_dir, status)


def _terminal_from_parallel(
    *,
    invocation: str,
    question: str,
    run_dir: Path,
    parallel_status: Mapping[str, Any],
) -> dict[str, Any]:
    status_value = str(parallel_status.get("status") or "blocked_parallel_execution")
    if status_value == "degraded_serial_handoff_required":
        terminal_status = "blocked_parallel_execution"
        actionable = "parallel execution degraded to serial handoff and produced no accepted shards"
    else:
        terminal_status = status_value
        actionable = _actionable_cause_from_parallel(parallel_status)
    status = _base_run_status(
        invocation=invocation,
        question=question,
        selected_mode="full-runner",
        run_dir=run_dir,
        status=terminal_status,
        ok=False,
        terminal=True,
        provenance=_provenance_from_parallel(parallel_status),
        diagnostics={"actionable_cause": actionable},
        artifacts=_artifact_paths(run_dir, parallel_status.get("artifacts")),
    )
    status["parallel"] = _parallel_summary(parallel_status)
    return status


def _visual_provider_preflight_status(
    *,
    run_dir: Path,
    adapter_name: str,
) -> dict[str, Any] | None:
    if not _run_requires_visual_provider(run_dir):
        return None
    normalized_adapter = adapter_name.strip().lower().replace("_", "-")
    if normalized_adapter == "fixture":
        return _visual_provider_status(
            run_dir=run_dir,
            status="fixture_visual_provider",
            ok=True,
            terminal=False,
            metric_classification="fixture_only_not_release_eligible",
            provider="fixture",
            provider_kind="fixture",
            provider_mode="fixture",
            configured=True,
            available=True,
            blocked_reason=None,
            actionable_cause=(
                "visual-required route is using deterministic fixture visual evidence; "
                "this is not eligible for real-use release metrics"
            ),
        )
    actionable_cause = (
        "visual_required route needs an explicit real visual acquisition provider; "
        "none is configured for the invocation router"
    )
    return _visual_provider_status(
        run_dir=run_dir,
        status="blocked_missing_visual_provider",
        ok=False,
        terminal=True,
        metric_classification="excluded_blocked",
        provider="automatic-web-visual",
        provider_kind="visual_acquisition",
        provider_mode="real",
        configured=False,
        available=False,
        blocked_reason="missing_real_visual_acquisition_provider",
        actionable_cause=actionable_cause,
    )


def _visual_provider_status(
    *,
    run_dir: Path,
    status: str,
    ok: bool,
    terminal: bool,
    metric_classification: str,
    provider: str,
    provider_kind: str,
    provider_mode: str,
    configured: bool,
    available: bool,
    blocked_reason: str | None,
    actionable_cause: str,
) -> dict[str, Any]:
    return build_visual_provider_status(
        run_dir=run_dir,
        status=status,
        ok=ok,
        terminal=terminal,
        metric_classification=metric_classification,
        provider=provider,
        provider_kind=provider_kind,
        provider_mode=provider_mode,
        configured=configured,
        available=available,
        blocked_reason=blocked_reason,
        actionable_cause=actionable_cause,
        created_at=_utc_now(),
    )


def _run_requires_visual_provider(run_dir: Path) -> bool:
    visual_tasks_path = run_dir / "visual_tasks.json"
    if visual_tasks_path.exists():
        try:
            visual_tasks = _read_json(visual_tasks_path)
        except (OSError, json.JSONDecodeError):
            visual_tasks = {}
        tasks = visual_tasks.get("tasks") if isinstance(visual_tasks, Mapping) else None
        if isinstance(tasks, list):
            return any(
                isinstance(task, Mapping) and task.get("route") == "visual_required"
                for task in tasks
            )
    try:
        evidence = _read_json(run_dir / "evidence.json")
    except (OSError, json.JSONDecodeError):
        return False
    routing = evidence.get("routing")
    if not isinstance(routing, list):
        return False
    return any(
        isinstance(route, Mapping) and route.get("modality") == "visual_required"
        for route in routing
    )


def _quick_chat_status(invocation: str, question: str) -> dict[str, Any]:
    return {
        "schema_version": RUN_STATUS_SCHEMA_VERSION,
        "run_id": None,
        "run_dir": None,
        "invocation": invocation,
        "question": question,
        "selected_mode": "quick-chat",
        "status": "quick_chat_only",
        "ok": True,
        "terminal": True,
        "created_at": _utc_now(),
        "provenance": {
            "type": "quick_chat",
            "evidence_bundle_produced": False,
            "fixture_only": False,
            "manual_handoff": False,
            "real_child_execution": False,
            "real_use_e2e_eligible": False,
        },
        "diagnostics": {
            "actionable_cause": "quick-chat was explicitly requested; no DeepResearch evidence bundle was produced"
        },
        "no_evidence_bundle": True,
        "response_notice": "Quick-chat requested explicitly; no DeepResearch evidence bundle was produced.",
        "artifacts": {},
    }


def _blocked_without_run(
    *,
    invocation: str,
    question: str,
    status: str,
    actionable_cause: str,
) -> dict[str, Any]:
    return {
        "schema_version": RUN_STATUS_SCHEMA_VERSION,
        "run_id": None,
        "run_dir": None,
        "invocation": invocation,
        "question": question,
        "selected_mode": "blocked",
        "status": status,
        "ok": False,
        "terminal": True,
        "created_at": _utc_now(),
        "provenance": {"type": status},
        "diagnostics": {"actionable_cause": actionable_cause},
        "artifacts": {},
    }


def _base_run_status(
    *,
    invocation: str,
    question: str,
    selected_mode: str,
    run_dir: Path,
    status: str,
    ok: bool,
    terminal: bool,
    provenance: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
    artifacts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if selected_mode not in INVOCATION_MODES:
        raise ValueError(f"unknown invocation mode: {selected_mode}")
    return {
        "schema_version": RUN_STATUS_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "invocation": invocation,
        "question": question,
        "selected_mode": selected_mode,
        "status": status,
        "ok": ok,
        "terminal": terminal,
        "updated_at": _utc_now(),
        "provenance": dict(provenance),
        "diagnostics": dict(diagnostics),
        "artifacts": _artifact_paths(run_dir, artifacts),
    }


def _artifact_paths(run_dir: Path, extra_artifacts: Mapping[str, Any] | None = None) -> dict[str, Any]:
    artifacts: dict[str, Any] = {"run_status": str(run_dir / RUN_STATUS_FILENAME)}
    if extra_artifacts:
        artifacts.update({str(key): value for key, value in extra_artifacts.items()})
    known_files = {
        "planning_status": "status.json",
        "evidence": "evidence.json",
        "research_tasks": "research_tasks.json",
        "search_tasks": "search_tasks.json",
        "search_results": "search_results.jsonl",
        "visual_tasks": "visual_tasks.json",
        "visual_observations": "visual_observations.jsonl",
        "parallel_orchestration_status": "parallel_orchestration_status.json",
        "merge_status": "merge_status.json",
        "manual_ingest_status": "manual_ingest_status.json",
        "guardrails_status": "guardrails_status.json",
        "verification_matrix_status": "verification_matrix_status.json",
        "report": "report.md",
        "report_status": "report_status.json",
        "run_trace": "run_trace.jsonl",
        "run_steps": "run_steps.json",
        "budget_estimate": "budget_estimate.json",
        "visual_acquisition_status": "visual_acquisition_status.json",
        "visual_provider_status": "visual_provider_status.json",
    }
    for key, filename in known_files.items():
        path = run_dir / filename
        if path.exists():
            artifacts[key] = str(path)
    return artifacts


def _write_run_status(run_dir: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    output = _finalize_handoff_payload(run_dir, payload)
    _write_json(run_dir / RUN_STATUS_FILENAME, output)
    return output


def _finalize_handoff_payload(run_dir: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(payload)
    output["artifacts"] = _artifact_paths(
        run_dir,
        output.get("artifacts") if isinstance(output.get("artifacts"), Mapping) else None,
    )
    missing_required = _missing_required_synthesized_artifacts(run_dir, output)
    if missing_required:
        diagnostics = (
            dict(output.get("diagnostics", {}))
            if isinstance(output.get("diagnostics"), Mapping)
            else {}
        )
        diagnostics["actionable_cause"] = (
            "successful synthesized run is missing required artifact paths: "
            + ", ".join(missing_required)
        )
        diagnostics["missing_required_artifacts"] = missing_required
        output["status"] = "failed_synthesis"
        output["ok"] = False
        output["terminal"] = True
        output["diagnostics"] = diagnostics
    output["shard_summary"] = _shard_summary(output)
    output["fallback"] = _fallback_summary(output)
    output["artifact_handoff"] = {
        "run_dir": output.get("run_dir"),
        "status": output.get("status"),
        "ok": output.get("ok"),
        "terminal": output.get("terminal"),
        "artifact_paths": dict(output["artifacts"]),
        "missing_required_artifacts": list(
            output.get("diagnostics", {}).get("missing_required_artifacts", [])
        )
        if isinstance(output.get("diagnostics"), Mapping)
        else [],
        "shards": dict(output["shard_summary"]),
        "fallback": dict(output["fallback"]),
        "diagnostics": dict(output.get("diagnostics", {}))
        if isinstance(output.get("diagnostics"), Mapping)
        else {},
    }
    return output


def _missing_required_synthesized_artifacts(
    run_dir: Path, payload: Mapping[str, Any]
) -> list[str]:
    if not _requires_synthesized_artifact_validation(payload):
        return []
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping):
        artifacts = {}
    missing: list[str] = []
    for key in _REQUIRED_SYNTHESIZED_ARTIFACTS:
        artifact_path = artifacts.get(key)
        if not isinstance(artifact_path, str) or not artifact_path.strip():
            missing.append(key)
            continue
        if key == "run_status":
            continue
        if not _resolve_artifact_path(run_dir, artifact_path).is_file():
            missing.append(key)
    return missing


def _requires_synthesized_artifact_validation(payload: Mapping[str, Any]) -> bool:
    if payload.get("selected_mode") != "full-runner":
        return False
    if payload.get("ok") is not True or payload.get("terminal") is not True:
        return False
    if str(payload.get("status") or "") not in _SYNTHESIZED_SUCCESS_STATUSES:
        return False
    stages = payload.get("stages")
    if not isinstance(stages, Mapping):
        return False
    synthesize_stage = stages.get("synthesize")
    return (
        isinstance(synthesize_stage, Mapping)
        and synthesize_stage.get("status") == "completed"
    )


def _shard_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    parallel = payload.get("parallel")
    if not isinstance(parallel, Mapping):
        return {
            "planned_task_count": None,
            "accepted_shard_count": 0,
            "merged_shard_count": 0,
            "failed_task_count": 0,
            "blocked_task_count": 0,
            "rejected_shard_count": 0,
            "discarded_task_count": 0,
        }
    return {
        "planned_task_count": parallel.get("planned_task_count"),
        "accepted_shard_count": _int_or_zero(parallel.get("accepted_shard_count")),
        "merged_shard_count": _int_or_zero(parallel.get("merged_shard_count")),
        "failed_task_count": _int_or_zero(parallel.get("failed_task_count")),
        "blocked_task_count": _int_or_zero(parallel.get("blocked_task_count")),
        "rejected_shard_count": _int_or_zero(parallel.get("rejected_shard_count")),
        "discarded_task_count": _int_or_zero(parallel.get("discarded_task_count")),
    }


def _fallback_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    parallel = payload.get("parallel")
    if not isinstance(parallel, Mapping):
        return {
            "parallel_degraded": None,
            "needs_serial_handoff": None,
            "degraded_reason": None,
        }
    return {
        "parallel_degraded": parallel.get("parallel_degraded"),
        "needs_serial_handoff": parallel.get("needs_serial_handoff"),
        "degraded_reason": parallel.get("degraded_reason"),
    }


def _resolve_artifact_path(run_dir: Path, artifact_path: str) -> Path:
    path = Path(artifact_path)
    if path.is_absolute():
        return path
    return run_dir / path


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _question_from_invocation(invocation: str) -> str:
    if invocation.lower().startswith("$deep-research:"):
        return invocation.split(":", 1)[1].strip()
    return invocation.strip()


def _has_quick_chat_marker(invocation: str) -> bool:
    normalized = invocation.strip().lower()
    if _negates_quick_chat(normalized):
        return False
    if _has_no_full_runner_intent(normalized):
        return True
    if _has_full_runner_intent(normalized):
        return False
    return any(marker in normalized for marker in _QUICK_CHAT_MARKERS)


def _has_no_full_runner_intent(normalized_invocation: str) -> bool:
    return any(re.search(pattern, normalized_invocation) for pattern in _NO_FULL_RUNNER_PATTERNS)


def _has_full_runner_intent(normalized_invocation: str) -> bool:
    return any(marker in normalized_invocation for marker in _FULL_RUNNER_INTENT_MARKERS)


def _negates_quick_chat(normalized_invocation: str) -> bool:
    return any(re.search(pattern, normalized_invocation) for pattern in _NEGATED_QUICK_CHAT_PATTERNS)


def _manual_provenance(manual_status: Mapping[str, Any]) -> dict[str, Any]:
    source = manual_status.get("evidence_source")
    if isinstance(source, Mapping):
        provenance = dict(source)
    else:
        provenance = {"type": "manual_handoff"}
    provenance.setdefault("adapter", "manual-sources")
    provenance.setdefault("manual_handoff", True)
    provenance.setdefault("fixture_only", False)
    provenance.setdefault("real_child_execution", False)
    provenance.setdefault("real_use_e2e_eligible", False)
    return provenance


def _provenance_from_parallel(parallel_status: Mapping[str, Any]) -> dict[str, Any]:
    source = parallel_status.get("evidence_source")
    if isinstance(source, Mapping):
        provenance = dict(source)
    else:
        provenance = {"type": "unknown"}
    provenance.setdefault("adapter", parallel_status.get("adapter"))
    provenance.setdefault("fixture_only", False)
    provenance.setdefault("manual_handoff", False)
    provenance.setdefault("attempted_real_child_execution", False)
    provenance.setdefault("real_child_execution", False)
    provenance.setdefault("real_use_e2e_eligible", False)
    return provenance


def _parallel_summary(parallel_status: Mapping[str, Any]) -> dict[str, Any]:
    merge = parallel_status.get("merge")
    accepted_shards = []
    if isinstance(merge, Mapping):
        accepted = merge.get("accepted_shards")
        if isinstance(accepted, list):
            accepted_shards = accepted
    failure_counts = (
        dict(parallel_status.get("failure_counts", {}))
        if isinstance(parallel_status.get("failure_counts"), Mapping)
        else {}
    )
    return {
        "status": parallel_status.get("status"),
        "ok": parallel_status.get("ok"),
        "adapter": parallel_status.get("adapter"),
        "parallel_degraded": parallel_status.get("parallel_degraded"),
        "degraded_reason": parallel_status.get("degraded_reason"),
        "needs_serial_handoff": parallel_status.get("needs_serial_handoff"),
        "planned_task_count": parallel_status.get("planned_task_count"),
        "accepted_shard_count": len(accepted_shards),
        "merged_shard_count": len(accepted_shards),
        "failed_task_count": _int_or_zero(failure_counts.get("failed_tasks")),
        "blocked_task_count": _int_or_zero(failure_counts.get("blocked_tasks")),
        "rejected_shard_count": _int_or_zero(failure_counts.get("rejected_shards")),
        "discarded_task_count": _int_or_zero(failure_counts.get("discarded_tasks")),
        "failure_counts": failure_counts,
    }


def _stage_summary(stage_status: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(stage_status, Mapping):
        return {"status": None}
    raw_status = stage_status.get("status")
    return {
        "status": raw_status,
        "ok": stage_status.get("ok", raw_status in _STAGE_OK_STATUSES),
        "artifacts": dict(stage_status.get("artifacts", {}))
        if isinstance(stage_status.get("artifacts"), Mapping)
        else {},
    }


def _actionable_cause_from_parallel(parallel_status: Mapping[str, Any]) -> str:
    diagnostics = parallel_status.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        cause = diagnostics.get("actionable_cause")
        if isinstance(cause, str) and cause:
            return cause
        first = diagnostics.get("first_blocked_reason") or diagnostics.get("first_blocked_diagnostic")
        if isinstance(first, str) and first:
            return first
    errors = parallel_status.get("errors")
    if isinstance(errors, list) and errors:
        first_error = errors[0]
        if isinstance(first_error, Mapping):
            return str(first_error.get("message") or first_error.get("code") or "parallel execution failed")
    return "parallel execution did not produce a synthesizable evidence bundle"


def _create_status_run_dir(runs_dir: Path) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = _utc_now().replace("-", "").replace(":", "").rstrip("Z")
    run_dir = runs_dir / f"dr_preflight_{timestamp}"
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = runs_dir / f"dr_preflight_{timestamp}_{suffix}"
    run_dir.mkdir()
    return run_dir.resolve()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
