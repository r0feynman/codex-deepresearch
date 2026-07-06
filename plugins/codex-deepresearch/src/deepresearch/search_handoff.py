"""Codex-native search handoff preparation and ingestion."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .budget_estimator import (
    BudgetCaps,
    BudgetEstimateError,
    add_budget_estimate_artifact,
    estimate_budget,
    write_budget_estimate,
)
from .cache_keys import source_cache_key
from .evidence_schema import (
    EVIDENCE_SCHEMA_VERSION,
    FRESHNESS_REQUIREMENTS,
    POLICY_DECISIONS,
    SEARCH_ROUTES,
    validate_artifacts,
)
from .execution_mode import BudgetPreset, resolve_config
from .modality_router import route_angles
from .public_beta_validation import public_beta_prompt_hash
from .run_state import begin_stage, skipped_stage_status
from .semantic_planner import (
    BLOCKED_SEMANTIC_PLANNER_UNAVAILABLE,
    PLANNER_MODE_MANUAL_ANGLES,
    SEMANTIC_PLANNER_SCHEMA_VERSION,
    SEMANTIC_PLANNER_VALIDATION_FILENAME,
    plan_semantic_angles,
    write_semantic_integrity_artifacts,
    write_semantic_planner_validation,
)
from .trace import record_stage_trace


HANDOFF_SCHEMA_VERSION = "codex-deepresearch.search-handoff.v0"
INGEST_STATUS_SCHEMA_VERSION = "codex-deepresearch.search-ingest.v0"
FETCH_QUEUE_SCHEMA_VERSION = "codex-deepresearch.fetch-queue.v0"
RELEASE_VALIDATION_IDENTITY_FIELDS = (
    "prompt_id",
    "suite_id",
    "prompt_hash",
    "original_question",
    "execution_mode",
    "runner_mode",
)


class SearchHandoffError(ValueError):
    """Raised when search handoff preparation or ingestion cannot continue."""


def prepare_run(
    *,
    question: str,
    runs_dir: str | Path,
    mode: str = "codex-plugin",
    search_provider: str | None = "codex-native",
    vlm_provider: str | None = None,
    budget_preset: str = "standard",
    freshness_requirement: str = "any",
    route: str | None = None,
    angles: Sequence[str] | None = None,
    max_results: int = 8,
    max_sources: int | None = None,
    max_images: int | None = None,
    max_subagents: int | None = None,
    max_agents: int | None = None,
    max_cost_usd: float | None = None,
    codex_runner: str = "codex-exec",
    confirm_budget: bool = False,
    prompt_id: str | None = None,
    suite_id: str | None = None,
    prompt_hash: str | None = None,
    original_question: str | None = None,
) -> dict[str, Any]:
    """Create a run directory and the Codex search handoff artifacts."""

    normalized_question = question.strip()
    if not normalized_question:
        raise SearchHandoffError("question cannot be empty")
    if freshness_requirement not in FRESHNESS_REQUIREMENTS:
        raise SearchHandoffError(
            "freshness_requirement must be one of: "
            + ", ".join(FRESHNESS_REQUIREMENTS)
        )
    if route is not None and route not in SEARCH_ROUTES:
        raise SearchHandoffError("route must be one of: " + ", ".join(SEARCH_ROUTES))
    if max_results < 1:
        raise BudgetEstimateError(
            code="invalid_cap",
            message="max_results must be at least 1",
            field="max_results",
            value=max_results,
            minimum_supported=1,
        )

    release_identity = build_release_validation_identity(
        question=normalized_question,
        prompt_id=prompt_id,
        suite_id=suite_id,
        prompt_hash=prompt_hash,
        original_question=original_question,
    )

    config = resolve_config(
        mode=mode,
        search_provider=search_provider,
        vlm_provider=vlm_provider,
        budget_preset=budget_preset,
    )
    if config.mode != "codex-plugin" or config.search_provider != "codex-native":
        raise SearchHandoffError(
            "search handoff prepare is only implemented for codex-plugin mode "
            "with codex-native search"
        )

    semantic_plan = plan_semantic_angles(
        question=normalized_question,
        explicit_angles=_effective_explicit_angles_for_route(
            angles=angles,
            route=route,
        ),
    )
    if semantic_plan.status == BLOCKED_SEMANTIC_PLANNER_UNAVAILABLE:
        return _prepare_blocked_semantic_planner_run(
            normalized_question=normalized_question,
            config=config,
            semantic_plan=semantic_plan,
            runs_dir=Path(runs_dir),
            release_identity=release_identity,
        )
    decisions = []
    for semantic_angle in semantic_plan.angles:
        route_override = route
        if route_override is None and semantic_plan.planner_mode != PLANNER_MODE_MANUAL_ANGLES:
            route_override = semantic_angle.route
        decisions.extend(
            route_angles(
                question=normalized_question,
                angles=[semantic_angle.title],
                max_images=config.budget.max_images,
                route_override=route_override,
            )
        )
    routing = [
        _routing_record(decision, index, semantic_angle=semantic_plan.angles[index - 1])
        for index, decision in enumerate(decisions, start=1)
    ]
    now = _utc_now()
    budget_estimate = estimate_budget(
        question=normalized_question,
        config=config,
        routing=routing,
        max_results=max_results,
        codex_runner=codex_runner,
        caps=BudgetCaps(
            max_sources=max_sources,
            max_images=max_images,
            max_subagents=max_subagents,
            max_agents=max_agents,
            max_cost_usd=max_cost_usd,
        ),
        confirmation_provided=confirm_budget,
        generated_at=now,
    )
    run_dir = _create_unique_run_dir(Path(runs_dir), now)
    run_id = run_dir.name
    begin_stage(run_dir, "planning", run_id=run_id, started_at=now)
    routing = _apply_budget_image_allocations(
        routing,
        budget_estimate["planned_search"]["route_image_allocations"],
    )
    effective_max_results = int(budget_estimate["planned_search"]["max_results_per_task"])
    search_tasks = [
        _search_task(
            normalized_question,
            route_record,
            index,
            freshness_requirement=freshness_requirement,
            max_results=effective_max_results,
            use_angle_in_query=len(routing) > 1,
        )
        for index, route_record in enumerate(routing, start=1)
    ]
    budget = _budget_to_evidence(config.budget_preset, config.budget)
    budget.update(
        {
            "max_sources": budget_estimate["effective_caps"]["max_sources"],
            "max_images": budget_estimate["effective_caps"]["max_images"],
            "max_verifier_invocations": budget_estimate["effective_caps"][
                "max_verifier_invocations"
            ],
            "max_model_api_calls": budget_estimate["effective_caps"][
                "max_model_api_calls"
            ],
            "max_concurrent_codex_subagents": budget_estimate["effective_caps"][
                "max_concurrent_codex_subagents"
            ],
            "max_concurrent_runner_agents": budget_estimate["effective_caps"][
                "max_concurrent_runner_agents"
            ],
            "max_cost_usd": budget_estimate["effective_caps"]["max_cost_usd"],
        }
    )
    evidence = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now,
        "question": normalized_question,
        "mode": config.mode,
        "search_provider": config.search_provider,
        "vlm_provider": config.vlm_provider,
        "routing": routing,
        "semantic_planner": {
            "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
            "question_class": semantic_plan.question_class,
            "broad_question": semantic_plan.broad_question,
            "source": semantic_plan.source,
            "expected_evidence_needs": list(semantic_plan.expected_evidence_needs),
            "planner_mode": semantic_plan.planner_mode,
            "semantic_release_eligible": semantic_plan.semantic_release_eligible,
            "status": semantic_plan.status,
            "diagnostics": dict(semantic_plan.diagnostics or {}),
        },
        "semantic_angles": [
            _semantic_angle_evidence_record(route_record)
            for route_record in routing
        ],
        "search_tasks": search_tasks,
        "budget": budget,
        "sources": [],
        "images": [],
        "claims": [],
        "handoff": {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "status": "awaiting_search_results",
            "search_results_path": "search_results.jsonl",
            "visual_observations_path": "visual_observations.jsonl",
        },
    }
    apply_release_validation_identity(evidence, release_identity)

    search_tasks_artifact = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now,
        "question": normalized_question,
        "mode": config.mode,
        "search_provider": config.search_provider,
        "tasks": search_tasks,
    }
    apply_release_validation_identity(search_tasks_artifact, release_identity)
    visual_tasks_artifact = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now,
        "tasks": [
            _visual_task(route_record, index)
            for index, route_record in enumerate(routing, start=1)
            if route_record["modality"] != "text_only"
        ],
    }
    apply_release_validation_identity(visual_tasks_artifact, release_identity)
    status = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "run_id": run_id,
        "status": "awaiting_search_results",
        "semantic_planning_status": semantic_plan.status,
        "planner_mode": semantic_plan.planner_mode,
        "semantic_release_eligible": semantic_plan.semantic_release_eligible,
        "created_at": now,
        "question": normalized_question,
        "mode": config.mode,
        "search_provider": config.search_provider,
        "vlm_provider": config.vlm_provider,
        "next_step": (
            "Fill search_results.jsonl with SearchResult records, then run "
            "codex-deepresearch ingest --run <run_id_or_path>."
        ),
        "artifacts": {
            "evidence": str(run_dir / "evidence.json"),
            "search_tasks": str(run_dir / "search_tasks.json"),
            "search_results": str(run_dir / "search_results.jsonl"),
            "visual_tasks": str(run_dir / "visual_tasks.json"),
            "visual_observations": str(run_dir / "visual_observations.jsonl"),
            "status": str(run_dir / "status.json"),
            "semantic_planner_validation": str(
                run_dir / SEMANTIC_PLANNER_VALIDATION_FILENAME
            ),
        },
        "budget_estimate": _budget_estimate_summary(budget_estimate),
    }
    apply_release_validation_identity(status, release_identity)
    add_budget_estimate_artifact(status, run_dir)

    _write_json(run_dir / "evidence.json", evidence)
    _write_json(run_dir / "search_tasks.json", search_tasks_artifact)
    (run_dir / "search_results.jsonl").write_text("", encoding="utf-8")
    _write_json(run_dir / "visual_tasks.json", visual_tasks_artifact)
    (run_dir / "visual_observations.jsonl").write_text("", encoding="utf-8")
    write_budget_estimate(run_dir, budget_estimate)
    semantic_integrity_artifacts = write_semantic_integrity_artifacts(
        run_dir=run_dir,
        question=normalized_question,
        plan=semantic_plan,
        routing=routing,
        search_tasks=search_tasks,
        created_at=now,
    )
    status["artifacts"].update(semantic_integrity_artifacts)
    semantic_validation = write_semantic_planner_validation(run_dir=run_dir, evidence=evidence)
    semantic_planning = _prepare_semantic_planning_summary(
        semantic_plan=semantic_plan,
        validation=semantic_validation,
    )
    status["semantic_planning"] = semantic_planning
    if semantic_planning["semantic_release_eligible"] is not True:
        status["diagnostics"] = {
            "semantic_planning": semantic_planning["user_visible_diagnostic"],
        }

    validation = validate_artifacts(evidence_path=run_dir / "evidence.json")
    if not validation.valid:
        status["status"] = "failed_validation"
        status["validation"] = validation.to_dict()
        record_stage_trace(
            run_dir,
            stage="planning",
            agent_role="planner",
            status_payload=status,
            prompt_summary="Create a Codex-native search handoff plan for the research question.",
            tool_call_summary="Resolved local execution config, routed planner angles, and wrote run handoff artifacts.",
            event_type="run_start",
            timestamp=now,
        )
        _write_json(run_dir / "status.json", status)
        raise SearchHandoffError(
            "prepared evidence.json failed validation: "
            + json.dumps(validation.to_dict(), sort_keys=True)
        )
    record_stage_trace(
        run_dir,
        stage="planning",
        agent_role="planner",
        status_payload=status,
        prompt_summary="Create a Codex-native search handoff plan for the research question.",
        tool_call_summary="Resolved local execution config, routed planner angles, and wrote run handoff artifacts.",
        event_type="run_start",
        timestamp=now,
    )
    _write_json(run_dir / "status.json", status)

    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "status": "awaiting_search_results",
        "artifacts": {
            "evidence": str(run_dir / "evidence.json"),
            "search_tasks": str(run_dir / "search_tasks.json"),
            "search_results": str(run_dir / "search_results.jsonl"),
            "visual_tasks": str(run_dir / "visual_tasks.json"),
            "visual_observations": str(run_dir / "visual_observations.jsonl"),
            "status": str(run_dir / "status.json"),
            "run_trace": str(run_dir / "run_trace.jsonl"),
            "run_steps": str(run_dir / "run_steps.json"),
            "budget_estimate": str(run_dir / "budget_estimate.json"),
            "semantic_planner_validation": str(
                run_dir / SEMANTIC_PLANNER_VALIDATION_FILENAME
            ),
            **semantic_integrity_artifacts,
        },
        "budget_estimate": status["budget_estimate"],
        "planner_mode": semantic_plan.planner_mode,
        "semantic_release_eligible": semantic_plan.semantic_release_eligible,
        "semantic_planning_status": semantic_plan.status,
        "semantic_planning": semantic_planning,
        **({"diagnostics": dict(status["diagnostics"])} if "diagnostics" in status else {}),
        **release_identity,
    }


def _prepare_blocked_semantic_planner_run(
    *,
    normalized_question: str,
    config: Any,
    semantic_plan: Any,
    runs_dir: Path,
    release_identity: Mapping[str, Any],
) -> dict[str, Any]:
    now = _utc_now()
    run_dir = _create_unique_run_dir(runs_dir, now)
    run_id = run_dir.name
    begin_stage(run_dir, "planning", run_id=run_id, started_at=now)

    budget = _budget_to_evidence(config.budget_preset, config.budget)
    evidence = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now,
        "question": normalized_question,
        "mode": config.mode,
        "search_provider": config.search_provider,
        "vlm_provider": config.vlm_provider,
        "routing": [],
        "semantic_planner": {
            "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
            "question_class": semantic_plan.question_class,
            "broad_question": semantic_plan.broad_question,
            "source": semantic_plan.source,
            "expected_evidence_needs": list(semantic_plan.expected_evidence_needs),
            "planner_mode": semantic_plan.planner_mode,
            "semantic_release_eligible": semantic_plan.semantic_release_eligible,
            "status": semantic_plan.status,
            "diagnostics": dict(semantic_plan.diagnostics or {}),
        },
        "semantic_angles": [],
        "search_tasks": [],
        "budget": budget,
        "sources": [],
        "images": [],
        "claims": [],
        "handoff": {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "status": semantic_plan.status,
            "search_results_path": None,
            "visual_observations_path": None,
        },
    }
    apply_release_validation_identity(evidence, release_identity)

    status = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "run_id": run_id,
        "status": semantic_plan.status,
        "semantic_planning_status": semantic_plan.status,
        "planner_mode": semantic_plan.planner_mode,
        "semantic_release_eligible": semantic_plan.semantic_release_eligible,
        "created_at": now,
        "question": normalized_question,
        "mode": config.mode,
        "search_provider": config.search_provider,
        "vlm_provider": config.vlm_provider,
        "next_step": (
            "Implement the Codex-native semantic planner adapter or provide explicit "
            "manual angles; heuristic template fallback is release-ineligible."
        ),
        "artifacts": {
            "evidence": str(run_dir / "evidence.json"),
            "status": str(run_dir / "status.json"),
            "semantic_planner_validation": str(
                run_dir / SEMANTIC_PLANNER_VALIDATION_FILENAME
            ),
        },
    }
    apply_release_validation_identity(status, release_identity)

    _write_json(run_dir / "evidence.json", evidence)
    semantic_integrity_artifacts = write_semantic_integrity_artifacts(
        run_dir=run_dir,
        question=normalized_question,
        plan=semantic_plan,
        routing=[],
        search_tasks=[],
        created_at=now,
    )
    status["artifacts"].update(semantic_integrity_artifacts)
    semantic_validation = write_semantic_planner_validation(run_dir=run_dir, evidence=evidence)
    semantic_planning = _prepare_semantic_planning_summary(
        semantic_plan=semantic_plan,
        validation=semantic_validation,
    )
    status["semantic_planning"] = semantic_planning
    status["diagnostics"] = {
        "semantic_planning": semantic_planning["user_visible_diagnostic"],
    }

    validation = validate_artifacts(evidence_path=run_dir / "evidence.json")
    status["validation"] = validation.to_dict()
    if not validation.valid:
        status["status"] = "failed_validation"

    record_stage_trace(
        run_dir,
        stage="planning",
        agent_role="planner",
        status_payload=status,
        prompt_summary="Create a Codex-native semantic plan for the research question.",
        tool_call_summary=(
            "Semantic planner unavailable; refused to materialize heuristic template tasks."
        ),
        event_type="semantic_planner_blocked",
        timestamp=now,
    )
    _write_json(run_dir / "status.json", status)

    artifacts = {
        "evidence": str(run_dir / "evidence.json"),
        "status": str(run_dir / "status.json"),
        "run_trace": str(run_dir / "run_trace.jsonl"),
        "run_steps": str(run_dir / "run_steps.json"),
        "semantic_planner_validation": str(
            run_dir / SEMANTIC_PLANNER_VALIDATION_FILENAME
        ),
        **semantic_integrity_artifacts,
    }
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "status": status["status"],
        "artifacts": artifacts,
        "planner_mode": semantic_plan.planner_mode,
        "semantic_release_eligible": semantic_plan.semantic_release_eligible,
        "semantic_planning_status": semantic_plan.status,
        "semantic_planning": semantic_planning,
        "diagnostics": dict(status["diagnostics"]),
        **release_identity,
    }


def _prepare_semantic_planning_summary(
    *,
    semantic_plan: Any,
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    diagnostics = (
        dict(semantic_plan.diagnostics)
        if isinstance(getattr(semantic_plan, "diagnostics", None), Mapping)
        else {}
    )
    failures = validation.get("failures")
    failure_codes = [
        str(failure.get("code"))
        for failure in failures
        if isinstance(failure, Mapping) and failure.get("code")
    ] if isinstance(failures, list) else []
    return {
        "schema_version": "codex-deepresearch.semantic-planning-summary.v0",
        "status": str(getattr(semantic_plan, "status", "unknown") or "unknown"),
        "planner_mode": str(getattr(semantic_plan, "planner_mode", "unknown") or "unknown"),
        "semantic_release_eligible": bool(
            getattr(semantic_plan, "semantic_release_eligible", False)
        ),
        "validation_ok": validation.get("ok"),
        "failure_codes": failure_codes,
        "fallback_kind": diagnostics.get("fallback_kind"),
        "user_visible_diagnostic": diagnostics.get("user_visible_diagnostic"),
    }


def build_release_validation_identity(
    *,
    question: str,
    prompt_id: str | None = None,
    suite_id: str | None = None,
    prompt_hash: str | None = None,
    original_question: str | None = None,
) -> dict[str, str]:
    """Build the canonical release-validation identity envelope if requested."""

    provided = any(
        isinstance(value, str) and value.strip()
        for value in (prompt_id, suite_id, prompt_hash, original_question)
    )
    if not provided:
        return {}
    clean_prompt_id = (prompt_id or "").strip()
    clean_suite_id = (suite_id or "").strip()
    if not clean_prompt_id:
        raise SearchHandoffError("prompt_id is required for release-validation identity")
    if not clean_suite_id:
        raise SearchHandoffError("suite_id is required for release-validation identity")
    clean_original_question = " ".join((original_question or question).strip().split())
    if not clean_original_question:
        raise SearchHandoffError(
            "original_question cannot be empty for release-validation identity"
        )
    expected_hash = public_beta_prompt_hash(clean_original_question)
    clean_prompt_hash = (prompt_hash or expected_hash).strip()
    if clean_prompt_hash != expected_hash:
        raise SearchHandoffError(
            "prompt_hash does not match the canonical hash of original_question"
        )
    return {
        "prompt_id": clean_prompt_id,
        "suite_id": clean_suite_id,
        "prompt_hash": clean_prompt_hash,
        "original_question": clean_original_question,
        "execution_mode": "codex-plugin",
        "runner_mode": "full-runner",
    }


def release_validation_identity_from_payload(payload: Mapping[str, Any]) -> dict[str, str]:
    """Return canonical release identity fields from an artifact payload."""

    identity: dict[str, str] = {}
    for field in RELEASE_VALIDATION_IDENTITY_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            identity[field] = value.strip()
    required = {"prompt_id", "suite_id", "prompt_hash", "original_question"}
    if not required.issubset(identity):
        return {}
    identity.setdefault("execution_mode", "codex-plugin")
    identity.setdefault("runner_mode", "full-runner")
    return identity


def apply_release_validation_identity(
    payload: dict[str, Any],
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Overlay canonical release identity onto a mutable artifact payload."""

    if not isinstance(identity, Mapping):
        return payload
    clean_identity = release_validation_identity_from_payload(identity)
    if not clean_identity:
        return payload
    payload.update(clean_identity)
    return payload


def ingest_run(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Validate and ingest Codex-native SearchResult records for one run."""

    run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    start = begin_stage(run_dir, "ingest")
    if start.skipped:
        status = skipped_stage_status(
            run_dir,
            stage="ingest",
            schema_version=INGEST_STATUS_SCHEMA_VERSION,
            status_artifact_key="ingest_status",
            status_filename="ingest_status.json",
            reason=start.skip_reason or "stage_already_completed",
        )
        record_stage_trace(
            run_dir,
            stage="ingest",
            agent_role="search_ingest_agent",
            status_payload=status,
            prompt_summary="Ingest SearchResult records from the prepared handoff artifact.",
            tool_call_summary="Skipped ingestion because run_steps.json marks the stage terminal.",
        )
        _write_json(run_dir / "ingest_status.json", status)
        return status
    evidence_path = run_dir / "evidence.json"
    search_results_path = run_dir / "search_results.jsonl"
    if not evidence_path.exists():
        raise SearchHandoffError(f"missing evidence.json in run directory: {run_dir}")
    if not search_results_path.exists():
        return _write_blocked_status(run_dir, "missing_search_results_file")
    if not search_results_path.read_text(encoding="utf-8").strip():
        return _write_blocked_status(run_dir, "empty_search_results_file")

    validation = validate_artifacts(
        evidence_path=evidence_path,
        search_results_path=search_results_path,
    )
    if not validation.valid:
        status = _base_ingest_status(run_dir, "failed_validation")
        status["validation"] = validation.to_dict()
        status["artifacts"] = {
            "evidence": str(evidence_path),
            "search_results": str(search_results_path),
            "ingest_status": str(run_dir / "ingest_status.json"),
        }
        record_stage_trace(
            run_dir,
            stage="ingest",
            agent_role="search_ingest_agent",
            status_payload=status,
            prompt_summary="Ingest SearchResult records from the prepared handoff artifact.",
            tool_call_summary="Validated evidence and search_results JSONL before normalizing sources.",
        )
        _write_json(run_dir / "ingest_status.json", status)
        return status

    evidence = _read_json(evidence_path)
    records = _read_jsonl(search_results_path)
    now = _utc_now()
    sources_dir = run_dir / "sources"
    sources_dir.mkdir(exist_ok=True)

    current_sources = evidence.get("sources", [])
    if not isinstance(current_sources, list):
        current_sources = []
    kept_sources = [
        source
        for source in current_sources
        if not _is_search_handoff_source(source)
    ]
    used_source_ids = {
        source["id"]
        for source in kept_sources
        if isinstance(source, Mapping) and isinstance(source.get("id"), str)
    }

    normalized_sources: list[dict[str, Any]] = []
    fetch_entries: list[dict[str, Any]] = []
    ingest_errors: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        source, fetchable, error = _source_from_search_result(
            record,
            index=index,
            used_source_ids=used_source_ids,
        )
        normalized_sources.append(source)
        if error is not None:
            ingest_errors.append(error)
        if fetchable:
            fetch_entries.append(_fetch_queue_entry(source))
        _write_json(run_dir / source["local_artifact_path"], source)

    evidence["sources"] = kept_sources + normalized_sources
    evidence["handoff"] = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "status": "search_results_ingested",
        "search_results_path": "search_results.jsonl",
        "visual_observations_path": "visual_observations.jsonl",
    }
    evidence["ingest"] = {
        "schema_version": INGEST_STATUS_SCHEMA_VERSION,
        "status": "ingested_with_rejections" if ingest_errors else "ingested",
        "ingested_at": now,
        "search_results_path": "search_results.jsonl",
        "fetch_queue_path": "fetch_queue.json",
        "sources_ingested": len(normalized_sources),
        "fetch_queue_count": len(fetch_entries),
        "errors": ingest_errors,
    }
    _write_json(evidence_path, evidence)

    fetch_queue = {
        "schema_version": FETCH_QUEUE_SCHEMA_VERSION,
        "run_id": evidence.get("run_id"),
        "created_at": now,
        "entries": fetch_entries,
    }
    apply_release_validation_identity(
        fetch_queue,
        release_validation_identity_from_payload(evidence),
    )
    _write_json(run_dir / "fetch_queue.json", fetch_queue)

    evidence_validation = validate_artifacts(evidence_path=evidence_path)
    status = _base_ingest_status(
        run_dir,
        "ingested_with_rejections" if ingest_errors else "ingested",
    )
    status.update(
        {
            "validation": evidence_validation.to_dict(),
            "sources_ingested": len(normalized_sources),
            "fetch_queue_count": len(fetch_entries),
            "errors": ingest_errors,
            "artifacts": {
                "evidence": str(evidence_path),
                "fetch_queue": str(run_dir / "fetch_queue.json"),
                "ingest_status": str(run_dir / "ingest_status.json"),
            },
        }
    )
    record_stage_trace(
        run_dir,
        stage="ingest",
        agent_role="search_ingest_agent",
        status_payload=status,
        prompt_summary="Ingest SearchResult records from the prepared handoff artifact.",
        tool_call_summary="Read search_results.jsonl, normalized source metadata, and wrote fetch_queue.json.",
    )
    _write_json(run_dir / "ingest_status.json", status)
    return status


def resolve_run_dir(run: str | Path, *, runs_dir: str | Path | None = None) -> Path:
    """Resolve a run id or direct run path to an existing run directory."""

    run_path = Path(run)
    if run_path.exists():
        if not run_path.is_dir():
            raise SearchHandoffError(f"run path is not a directory: {run_path}")
        return run_path.resolve()
    if run_path.is_absolute():
        raise SearchHandoffError(f"run directory does not exist: {run_path}")
    if runs_dir is not None:
        candidate = Path(runs_dir) / run_path
        if candidate.exists() and candidate.is_dir():
            return candidate.resolve()
    raise SearchHandoffError(
        f"run '{run}' was not found as a path"
        + (" or under the configured runs directory" if runs_dir is not None else "")
    )


def _write_blocked_status(run_dir: Path, reason: str) -> dict[str, Any]:
    status = _base_ingest_status(run_dir, "blocked_missing_search_handoff")
    status["errors"] = [{"code": reason, "status": "blocked_missing_search_handoff"}]
    status["artifacts"] = {
        "ingest_status": str(run_dir / "ingest_status.json"),
    }
    record_stage_trace(
        run_dir,
        stage="ingest",
        agent_role="search_ingest_agent",
        status_payload=status,
        prompt_summary="Ingest SearchResult records from the prepared handoff artifact.",
        tool_call_summary="Checked required search handoff artifacts before ingestion.",
    )
    _write_json(run_dir / "ingest_status.json", status)
    return status


def _source_from_search_result(
    record: Mapping[str, Any],
    *,
    index: int,
    used_source_ids: set[str],
) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
    source_id = _unique_source_id(record["id"], used_source_ids)
    url = record["url"]
    valid_url = _is_fetchable_url(url)
    policy_decision = record["policy_decision"]
    if policy_decision not in POLICY_DECISIONS:
        raise SearchHandoffError(f"unexpected policy decision: {policy_decision}")

    fetchable = valid_url and policy_decision == "allowed"
    rejection_code: str | None = None
    if not valid_url:
        rejection_code = "invalid_url"
    elif policy_decision == "blocked":
        rejection_code = "policy_blocked"
    elif policy_decision == "manual_review":
        rejection_code = "policy_manual_review"

    local_artifact_path = f"sources/{source_id}.json"
    source = {
        "id": source_id,
        "type": _source_type(record["result_type"]),
        "url": url,
        "title": record["title"],
        "published_at": record.get("published_at"),
        "accessed_at": record["accessed_at"],
        "quality": _source_quality(record.get("raw_provider_metadata")),
        "retrieval_status": "partial" if fetchable else "failed",
        "local_artifact_path": local_artifact_path,
        "license_policy": _license_policy(policy_decision),
        "robots_policy": _robots_policy(policy_decision, record["policy_flags"]),
        "search_result_id": record["id"],
        "task_id": record["task_id"],
        "angle_id": record["angle_id"],
        "route": record["route"],
        "provider": record["provider"],
        "query": record["query"],
        "snippet": record["snippet"],
        "rank": record["rank"],
        "freshness_requirement": record["freshness_requirement"],
        "language": record["language"],
        "region": record["region"],
        "policy_decision": policy_decision,
        "policy_flags": list(record["policy_flags"]),
        "raw_provider_metadata": dict(record["raw_provider_metadata"]),
    }
    error = None
    if rejection_code is not None:
        source["ingest_status"] = f"rejected_{rejection_code}"
        source["retrieval_error"] = rejection_code
        error = {
            "code": rejection_code,
            "status": "failed",
            "record_index": index,
            "record_id": record["id"],
            "source_id": source_id,
            "url": url,
        }
    else:
        source["ingest_status"] = "queued_for_fetch"
    source["cache_key"] = source_cache_key(source)
    return source, fetchable, error


def _fetch_queue_entry(source: Mapping[str, Any]) -> dict[str, Any]:
    entry = {
        "source_id": source["id"],
        "search_result_id": source["search_result_id"],
        "url": source["url"],
        "type": source["type"],
        "title": source["title"],
        "task_id": source["task_id"],
        "angle_id": source["angle_id"],
        "route": source["route"],
        "query": source["query"],
        "policy_decision": source["policy_decision"],
        "policy_flags": list(source["policy_flags"]),
        "retrieval_status": "queued",
    }
    entry["cache_key"] = source_cache_key(source, entry)
    return entry


def _is_search_handoff_source(source: Any) -> bool:
    return isinstance(source, Mapping) and "search_result_id" in source


def _budget_to_evidence(preset: str, budget: BudgetPreset) -> dict[str, Any]:
    data = asdict(budget)
    data["preset"] = preset
    data["verifier_invocations_used"] = 0
    data["model_api_calls_used"] = 0
    data["sources_selected"] = 0
    data["images_selected"] = 0
    return data


def _base_ingest_status(run_dir: Path, status: str) -> dict[str, Any]:
    run_id = run_dir.name
    identity: Mapping[str, Any] = {}
    try:
        evidence = _read_json(run_dir / "evidence.json")
        run_id = evidence.get("run_id", run_id)
        identity = release_validation_identity_from_payload(evidence)
    except SearchHandoffError:
        pass
    payload = {
        "schema_version": INGEST_STATUS_SCHEMA_VERSION,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "status": status,
        "created_at": _utc_now(),
    }
    return apply_release_validation_identity(payload, identity)


def _normalize_planner_angles(angles: Sequence[str] | None) -> list[str]:
    if angles is None:
        return ["primary source discovery"]
    normalized = [" ".join(angle.strip().split()) for angle in angles if angle.strip()]
    if not normalized:
        raise SearchHandoffError("at least one planner angle is required when angles are provided")
    return normalized


def _routing_record(
    decision: Any,
    index: int,
    *,
    semantic_angle: Any | None = None,
) -> dict[str, Any]:
    angle_id = (
        str(getattr(semantic_angle, "angle_id", "") or "")
        if semantic_angle is not None
        else ""
    )
    if not angle_id:
        angle_id = f"angle_{index:03d}"
    record = {
        "id": angle_id,
        "angle": decision.angle,
        "modality": decision.modality,
        "reason": decision.reason,
        "visual_tasks": list(decision.visual_tasks),
        "max_images": decision.max_images,
    }
    if semantic_angle is not None:
        record.update(
            {
                "title": semantic_angle.title,
                "research_question": semantic_angle.research_question,
                "question_context": getattr(semantic_angle, "question_context", ""),
                "route": decision.modality,
                "evidence_need": semantic_angle.evidence_need,
                "expected_artifacts": list(semantic_angle.expected_artifacts),
                "expected_evidence": _expected_evidence_for_angle(
                    semantic_angle.evidence_need,
                    decision.modality,
                ),
                "success_criteria": list(semantic_angle.success_criteria),
                "report_section": semantic_angle.report_section,
            }
        )
    return record


def _effective_explicit_angles_for_route(
    *,
    angles: Sequence[str] | None,
    route: str | None,
) -> Sequence[str] | None:
    return angles


def _semantic_angle_evidence_record(route_record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "angle_id": route_record["id"],
        "title": route_record.get("title") or route_record["angle"],
        "research_question": route_record.get("research_question") or route_record["angle"],
        "question_context": route_record.get("question_context") or "",
        "route": route_record["modality"],
        "evidence_need": route_record.get("evidence_need") or "primary_source",
        "expected_artifacts": list(route_record.get("expected_artifacts", [])),
        "expected_evidence": list(route_record.get("expected_evidence", [])),
        "success_criteria": list(route_record.get("success_criteria", [])),
        "report_section": route_record.get("report_section") or route_record["angle"],
    }


def _expected_evidence_for_angle(evidence_need: str, route: str) -> list[str]:
    expected = [evidence_need]
    if evidence_need == "visual_example":
        expected.append("visual_example")
    if evidence_need == "visual_observation":
        expected.extend(["visual_observation", "vlm_analysis"])
    if route != "text_only" and "visual_observation" not in expected:
        expected.append("visual_observation")
    return list(dict.fromkeys(expected))


def _apply_budget_image_allocations(
    routing: Sequence[Mapping[str, Any]],
    allocations: Mapping[str, int],
) -> list[dict[str, Any]]:
    capped: list[dict[str, Any]] = []
    for route in routing:
        record = dict(route)
        route_id = str(record["id"])
        record["max_images"] = min(
            int(record.get("max_images", 0)),
            int(allocations.get(route_id, 0)),
        )
        if record["max_images"] <= 0:
            record["visual_tasks"] = []
        capped.append(record)
    return capped


def _budget_estimate_summary(estimate: Mapping[str, Any]) -> dict[str, Any]:
    estimates = estimate["estimates"]
    model_calls = estimates["model_call_placeholders"]
    tokens = estimates["token_placeholders"]
    return {
        "source_count": estimates["source_count"],
        "image_count": estimates["image_count"],
        "verifier_invocation_count": estimates["verifier_invocation_count"],
        "codex_subagent_count": estimates["codex_subagent_count"],
        "runner_stage_count": estimates["runner_stage_count"],
        "model_call_placeholders": {
            "total_model_api_calls": model_calls["total_model_api_calls"],
            "total_model_api_calls_uncapped": model_calls[
                "total_model_api_calls_uncapped"
            ],
        },
        "token_placeholders": {
            "total_input_tokens_placeholder": tokens[
                "total_input_tokens_placeholder"
            ],
            "total_output_tokens_placeholder": tokens[
                "total_output_tokens_placeholder"
            ],
        },
        "high_water_cost_usd": estimate["high_water_cost_bounds"]["upper_bound_usd"],
        "suggestion_count": len(estimate["suggestions"]),
    }


def _search_task(
    question: str,
    route_record: Mapping[str, Any],
    index: int,
    *,
    freshness_requirement: str,
    max_results: int,
    use_angle_in_query: bool,
) -> dict[str, Any]:
    angle = str(route_record["angle"])
    query = question if not use_angle_in_query else f"{question} {angle}"
    return {
        "id": f"task_search_{index:03d}",
        "angle_id": route_record["id"],
        "angle": angle,
        "title": route_record.get("title") or angle,
        "research_question": route_record.get("research_question") or angle,
        "question_context": route_record.get("question_context") or question,
        "query": query,
        "evidence_need": route_record.get("evidence_need") or "primary_source",
        "expected_artifacts": list(route_record.get("expected_artifacts", [])),
        "expected_evidence": list(route_record.get("expected_evidence", [])),
        "success_criteria": list(route_record.get("success_criteria", [])),
        "report_section": route_record.get("report_section") or angle,
        "freshness_requirement": freshness_requirement,
        "modality": route_record["modality"],
        "route": route_record["modality"],
        "max_results": max_results,
        "visual_tasks": list(route_record.get("visual_tasks", [])),
        "max_images": int(route_record.get("max_images", 0)),
        "source_policy": {
            "decision": "allowed",
            "flags": [],
        },
    }


def _visual_task(route_record: Mapping[str, Any], index: int) -> dict[str, Any]:
    return {
        "id": f"task_visual_{index:03d}",
        "angle_id": route_record["id"],
        "angle": route_record["angle"],
        "route": route_record["modality"],
        "visual_tasks": list(route_record["visual_tasks"]),
        "max_images": route_record["max_images"],
        "status": "planned",
    }


def _create_unique_run_dir(runs_dir: Path, created_at: str) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = created_at.replace("-", "").replace(":", "").rstrip("Z")
    run_dir = runs_dir / f"dr_{timestamp}"
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = runs_dir / f"dr_{timestamp}_{suffix}"
    run_dir.mkdir()
    return run_dir.resolve()


def _unique_source_id(result_id: str, used_source_ids: set[str]) -> str:
    base = "src_" + re.sub(r"[^A-Za-z0-9_]+", "_", result_id).strip("_")
    if base == "src_":
        base = "src_result"
    candidate = base
    suffix = 1
    while candidate in used_source_ids:
        suffix += 1
        candidate = f"{base}_{suffix}"
    used_source_ids.add(candidate)
    return candidate


def _source_type(result_type: str) -> str:
    if result_type in {"pdf", "image"}:
        return result_type
    return "web"


def _source_quality(raw_metadata: Any) -> str:
    if isinstance(raw_metadata, Mapping):
        quality = raw_metadata.get("source_quality")
        if quality in {"primary", "secondary", "blog", "forum", "unknown"}:
            return str(quality)
    return "unknown"


def _license_policy(policy_decision: str) -> str:
    if policy_decision == "allowed":
        return "allowed"
    if policy_decision == "manual_review":
        return "manual_review"
    return "restricted"


def _robots_policy(policy_decision: str, policy_flags: Sequence[Any]) -> str:
    flags = " ".join(str(flag).lower() for flag in policy_flags)
    if "robots_disallowed" in flags or "robots:disallowed" in flags:
        return "disallowed"
    if policy_decision == "allowed":
        return "allowed"
    if policy_decision == "manual_review":
        return "manual_review"
    return "unknown"


def _is_fetchable_url(url: str) -> bool:
    if not url or any(character.isspace() for character in url):
        return False
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and bool(hostname)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SearchHandoffError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SearchHandoffError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SearchHandoffError(f"expected JSON object in {path}")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
