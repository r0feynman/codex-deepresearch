"""Deterministic pre-run budget and work estimator."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .execution_mode import BudgetPreset, RunConfig
from .run_state import RUN_STAGE_ORDER


BUDGET_ESTIMATE_SCHEMA_VERSION = "codex-deepresearch.budget-estimate.v0"
BUDGET_ESTIMATE_FILENAME = "budget_estimate.json"
CODEX_RUNNERS = ("codex-exec", "codex-sdk", "serial")
CONFIRMATION_REQUIRED_PRESETS = {"deep", "exhaustive"}

_ROUTE_VERIFIER_COUNTS = {
    "text_only": 3,
    "visual_required": 4,
    "visual_optional": 3,
}
_SEARCH_CALL_COST_USD = {
    "codex-native": 0.0,
    "manual": 0.0,
    "openai": 0.03,
    "brave": 0.01,
    "tavily": 0.02,
    "serpapi": 0.10,
}
_RUNNER_STAGE_OVERHEAD_USD = {
    "codex-exec": 0.003,
    "codex-sdk": 0.002,
    "serial": 0.0,
}
_FETCH_COMPUTE_PER_SOURCE_USD = 0.001
_TEXT_MODEL_CALL_USD = 0.01
_IMAGE_ANALYSIS_CALL_USD = 0.02
_VERIFIER_INVOCATION_USD = 0.005
_SYNTHESIS_CALL_USD = 0.02
_CODEX_SUBAGENT_RUNTIME_USD = 0.002


class BudgetEstimateError(ValueError):
    """Raised when a pre-run budget cannot produce a feasible plan."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        field: str | None = None,
        value: Any | None = None,
        minimum_supported: Any | None = None,
        allowed_values: Sequence[str] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "schema_version": BUDGET_ESTIMATE_SCHEMA_VERSION,
            "status": "failed",
            "code": code,
            "message": message,
        }
        if field is not None:
            payload["field"] = field
        if value is not None:
            payload["value"] = value
        if minimum_supported is not None:
            payload["minimum_supported"] = minimum_supported
        if allowed_values is not None:
            payload["allowed_values"] = list(allowed_values)
        if details is not None:
            payload["details"] = dict(details)
        self.payload = payload
        super().__init__(json.dumps(payload, sort_keys=True))

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


@dataclass(frozen=True)
class BudgetCaps:
    """Optional user caps that can reduce a preset before expensive work starts."""

    max_sources: int | None = None
    max_images: int | None = None
    max_subagents: int | None = None
    max_agents: int | None = None
    max_cost_usd: float | None = None


@dataclass(frozen=True)
class _Plan:
    source_count: int
    image_count: int
    verifier_invocation_count: int
    codex_subagent_count: int
    codex_subagent_count_uncapped: int
    codex_subagent_concurrency: int
    runner_stage_count: int
    runner_agent_concurrency: int
    max_results_per_task: int
    route_image_allocations: dict[str, int]
    source_distribution: dict[str, int]
    model_call_placeholders: dict[str, int]
    token_placeholders: dict[str, int]
    cost_components_usd: dict[str, float]
    high_water_cost_usd: float
    lower_bound_cost_usd: float


def estimate_budget(
    *,
    question: str,
    config: RunConfig,
    routing: Sequence[Mapping[str, Any]],
    max_results: int,
    codex_runner: str = "codex-exec",
    caps: BudgetCaps | None = None,
    confirmation_provided: bool = False,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic estimate for one run plan."""

    normalized_question = " ".join(question.strip().split())
    if not normalized_question:
        raise BudgetEstimateError(
            code="invalid_question",
            message="question cannot be empty",
            field="question",
        )
    if not routing:
        raise BudgetEstimateError(
            code="invalid_routing",
            message="at least one routing angle is required",
            field="routing",
            minimum_supported=1,
        )
    if max_results < 1:
        raise BudgetEstimateError(
            code="invalid_cap",
            message="max_results must be at least 1",
            field="max_results",
            value=max_results,
            minimum_supported=1,
        )
    normalized_runner = _normalize_runner(codex_runner)
    user_caps = caps or BudgetCaps()
    _validate_caps(user_caps)
    if (
        config.budget_preset in CONFIRMATION_REQUIRED_PRESETS
        and not confirmation_provided
    ):
        raise BudgetEstimateError(
            code="budget_confirmation_required",
            message=(
                f"budget preset '{config.budget_preset}' requires explicit "
                "confirmation before execution"
            ),
            field="budget_preset",
            value=config.budget_preset,
            details={"required_flag": "--confirm-budget"},
        )
    if config.budget_preset == "exhaustive" and user_caps.max_cost_usd is None:
        raise BudgetEstimateError(
            code="budget_cost_cap_required",
            message="budget preset 'exhaustive' requires max_cost_usd before execution",
            field="max_cost_usd",
            details={
                "required_flag": "--max-cost-usd",
                "budget_preset": "exhaustive",
            },
        )

    route_records = [_normalized_route(route, index) for index, route in enumerate(routing, start=1)]
    preset = config.budget
    source_cap = _initial_source_cap(
        preset=preset,
        angle_count=len(route_records),
        max_results=max_results,
        user_max_sources=user_caps.max_sources,
    )
    image_cap = _initial_image_cap(
        preset=preset,
        routes=route_records,
        user_max_images=user_caps.max_images,
    )
    suggestions: list[dict[str, Any]] = []
    _append_direct_cap_suggestions(
        suggestions,
        preset=preset,
        source_cap=source_cap,
        image_cap=image_cap,
        caps=user_caps,
        codex_runner=normalized_runner,
    )

    effective_subagent_concurrency = _effective_subagent_concurrency(
        preset,
        user_caps.max_subagents,
        normalized_runner,
    )
    effective_runner_concurrency = _effective_runner_concurrency(
        preset,
        user_caps.max_agents,
        normalized_runner,
    )
    if user_caps.max_subagents is not None and user_caps.max_subagents < preset.max_concurrent_codex_subagents:
        suggestions.append(
            _suggestion(
                "reduce_codex_subagent_concurrency",
                "--max-subagents",
                preset.max_concurrent_codex_subagents,
                effective_subagent_concurrency,
                "Codex-side fan-out will run with the lower requested concurrency cap.",
            )
        )
    if user_caps.max_agents is not None and user_caps.max_agents < preset.max_concurrent_runner_agents:
        suggestions.append(
            _suggestion(
                "reduce_runner_agent_concurrency",
                "--max-agents",
                preset.max_concurrent_runner_agents,
                effective_runner_concurrency,
                "Runner fan-out will run with the lower requested concurrency cap.",
            )
        )
    if normalized_runner == "serial":
        suggestions.append(
            _suggestion(
                "serial_runner_limits_concurrency",
                "--codex-runner",
                codex_runner,
                "serial",
                "Serial runner mode limits runner and Codex subagent concurrency to one.",
            )
        )

    plan = _build_plan(
        config=config,
        routes=route_records,
        max_results=max_results,
        source_cap=source_cap,
        image_cap=image_cap,
        codex_runner=normalized_runner,
        codex_subagent_concurrency=effective_subagent_concurrency,
        runner_agent_concurrency=effective_runner_concurrency,
    )
    if user_caps.max_cost_usd is not None and plan.high_water_cost_usd > user_caps.max_cost_usd:
        min_source_cap = len(route_records)
        min_image_cap = _minimum_image_cap(route_records)
        min_plan = _build_plan(
            config=config,
            routes=route_records,
            max_results=max_results,
            source_cap=min_source_cap,
            image_cap=min_image_cap,
            codex_runner=normalized_runner,
            codex_subagent_concurrency=effective_subagent_concurrency,
            runner_agent_concurrency=effective_runner_concurrency,
        )
        if min_plan.high_water_cost_usd > user_caps.max_cost_usd:
            raise BudgetEstimateError(
                code="impossible_cost_cap",
                message="max_cost_usd is below the minimum feasible deterministic plan",
                field="max_cost_usd",
                value=user_caps.max_cost_usd,
                minimum_supported=min_plan.high_water_cost_usd,
                details={
                    "minimum_sources": min_plan.source_count,
                    "minimum_images": min_plan.image_count,
                    "codex_runner": normalized_runner,
                    "search_provider": config.search_provider,
                },
            )
        reduced_source_cap, reduced_image_cap, reduced_plan = _reduce_for_cost_cap(
            config=config,
            routes=route_records,
            max_results=max_results,
            source_cap=source_cap,
            image_cap=image_cap,
            min_source_cap=min_source_cap,
            min_image_cap=min_image_cap,
            max_cost_usd=user_caps.max_cost_usd,
            codex_runner=normalized_runner,
            codex_subagent_concurrency=effective_subagent_concurrency,
            runner_agent_concurrency=effective_runner_concurrency,
        )
        suggestions.append(
            {
                "code": "reduce_to_cost_cap",
                "flag": "--max-cost-usd",
                "current": {
                    "source_cap": source_cap,
                    "image_cap": image_cap,
                    "high_water_cost_usd": plan.high_water_cost_usd,
                },
                "suggested": {
                    "source_cap": reduced_source_cap,
                    "image_cap": reduced_image_cap,
                    "high_water_cost_usd": reduced_plan.high_water_cost_usd,
                },
                "reason": "Prune optional visual work first, then source volume, to satisfy the requested cost cap.",
            }
        )
        source_cap = reduced_source_cap
        image_cap = reduced_image_cap
        plan = reduced_plan

    preset_caps = _preset_caps(config.budget)
    effective_caps = {
        "max_sources": plan.source_count,
        "max_images": plan.image_count,
        "max_verifier_invocations": config.budget.max_verifier_invocations,
        "max_model_api_calls": config.budget.max_model_api_calls,
        "max_codex_handoff_tasks": config.budget.max_codex_handoff_tasks,
        "max_concurrent_codex_subagents": plan.codex_subagent_concurrency,
        "max_concurrent_runner_agents": plan.runner_agent_concurrency,
        "max_cost_usd": user_caps.max_cost_usd,
    }
    estimates = {
        "source_count": plan.source_count,
        "image_count": plan.image_count,
        "verifier_invocation_count": plan.verifier_invocation_count,
        "codex_subagent_count": plan.codex_subagent_count,
        "codex_subagent_count_uncapped": plan.codex_subagent_count_uncapped,
        "codex_subagent_concurrency": plan.codex_subagent_concurrency,
        "runner_stage_count": plan.runner_stage_count,
        "runner_agent_concurrency": plan.runner_agent_concurrency,
        "search_call_count": 0 if config.search_provider == "manual" else len(route_records),
        "model_call_placeholders": plan.model_call_placeholders,
        "token_placeholders": plan.token_placeholders,
    }
    return {
        "schema_version": BUDGET_ESTIMATE_SCHEMA_VERSION,
        "status": "estimated",
        "generated_at": generated_at or _utc_now(),
        "question": normalized_question,
        "mode": config.mode,
        "search_provider": config.search_provider,
        "vlm_provider": config.vlm_provider,
        "budget_preset": config.budget_preset,
        "codex_runner": normalized_runner,
        "confirmation": {
            "required": config.budget_preset in CONFIRMATION_REQUIRED_PRESETS,
            "provided": confirmation_provided,
        },
        "route_summary": {
            "angle_count": len(route_records),
            "routes": _route_counts(route_records),
            "visual_angle_count": sum(1 for route in route_records if route["modality"] != "text_only"),
        },
        "preset_caps": preset_caps,
        "user_caps": _caps_to_dict(user_caps),
        "effective_caps": effective_caps,
        "planned_search": {
            "max_results_per_task": plan.max_results_per_task,
            "source_distribution": plan.source_distribution,
            "route_image_allocations": plan.route_image_allocations,
        },
        "estimates": estimates,
        "high_water_cost_bounds": {
            "currency": "USD",
            "pricing_status": "deterministic_placeholder_not_live_pricing",
            "lower_bound_usd": plan.lower_bound_cost_usd,
            "upper_bound_usd": plan.high_water_cost_usd,
            "max_cost_usd": user_caps.max_cost_usd,
            "within_max_cost": (
                True
                if user_caps.max_cost_usd is None
                else plan.high_water_cost_usd <= user_caps.max_cost_usd
            ),
            "components_usd": plan.cost_components_usd,
        },
        "suggestions": suggestions,
    }


def write_budget_estimate(
    run_dir: str | Path,
    estimate: Mapping[str, Any],
) -> Path:
    """Write the canonical budget estimate artifact under a run directory."""

    path = budget_estimate_path(run_dir)
    path.write_text(json.dumps(dict(estimate), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def budget_estimate_path(run_dir: str | Path) -> Path:
    """Return the canonical budget estimate path for a run directory."""

    return Path(run_dir) / BUDGET_ESTIMATE_FILENAME


def add_budget_estimate_artifact(payload: dict[str, Any], run_dir: str | Path) -> None:
    """Link the canonical budget estimate artifact from a status payload."""

    artifacts = payload.setdefault("artifacts", {})
    if isinstance(artifacts, dict):
        artifacts["budget_estimate"] = str(budget_estimate_path(run_dir))


def _build_plan(
    *,
    config: RunConfig,
    routes: Sequence[Mapping[str, Any]],
    max_results: int,
    source_cap: int,
    image_cap: int,
    codex_runner: str,
    codex_subagent_concurrency: int,
    runner_agent_concurrency: int,
) -> _Plan:
    source_count, max_results_per_task, source_distribution = _source_plan(
        routes=routes,
        max_results=max_results,
        source_cap=source_cap,
    )
    route_image_allocations = _image_allocations(routes, image_cap)
    image_count = sum(route_image_allocations.values())
    verifier_invocations = _verifier_invocation_count(
        routes=routes,
        source_distribution=source_distribution,
        route_image_allocations=route_image_allocations,
        max_verifier_invocations=config.budget.max_verifier_invocations,
    )
    codex_subagent_count_uncapped = (
        1
        + 1
        + len(routes)
        + sum(1 for route in routes if route["modality"] != "text_only")
        + source_count
        + image_count
        + verifier_invocations
        + 1
    )
    codex_subagent_count = min(
        config.budget.max_codex_handoff_tasks,
        codex_subagent_count_uncapped,
    )
    runner_stage_count = len(RUN_STAGE_ORDER)
    model_calls = _model_call_placeholders(
        config=config,
        source_count=source_count,
        image_count=image_count,
        verifier_invocations=verifier_invocations,
        runner_stage_count=runner_stage_count,
        codex_runner=codex_runner,
        route_count=len(routes),
    )
    tokens = _token_placeholders(
        source_count=source_count,
        image_count=image_count,
        verifier_invocations=verifier_invocations,
    )
    components = _cost_components(
        config=config,
        source_count=source_count,
        image_count=image_count,
        verifier_invocations=verifier_invocations,
        codex_subagent_count=codex_subagent_count,
        runner_stage_count=runner_stage_count,
        codex_runner=codex_runner,
        model_calls=model_calls,
    )
    high_water = _round_cost(sum(components.values()))
    lower_bound = _minimum_bound_cost(
        components,
        source_count=source_count,
        image_count=image_count,
        verifier_invocations=verifier_invocations,
    )
    return _Plan(
        source_count=source_count,
        image_count=image_count,
        verifier_invocation_count=verifier_invocations,
        codex_subagent_count=codex_subagent_count,
        codex_subagent_count_uncapped=codex_subagent_count_uncapped,
        codex_subagent_concurrency=min(codex_subagent_concurrency, codex_subagent_count),
        runner_stage_count=runner_stage_count,
        runner_agent_concurrency=runner_agent_concurrency,
        max_results_per_task=max_results_per_task,
        route_image_allocations=route_image_allocations,
        source_distribution=source_distribution,
        model_call_placeholders=model_calls,
        token_placeholders=tokens,
        cost_components_usd=components,
        high_water_cost_usd=high_water,
        lower_bound_cost_usd=lower_bound,
    )


def _source_plan(
    *,
    routes: Sequence[Mapping[str, Any]],
    max_results: int,
    source_cap: int,
) -> tuple[int, int, dict[str, int]]:
    angle_count = len(routes)
    if source_cap < angle_count:
        raise BudgetEstimateError(
            code="impossible_source_cap",
            message="max_sources must allow at least one source per planner angle",
            field="max_sources",
            value=source_cap,
            minimum_supported=angle_count,
        )
    max_results_per_task = min(max_results, max(1, source_cap // angle_count))
    source_count = max_results_per_task * angle_count
    distribution = {
        str(route["id"]): max_results_per_task
        for route in routes
    }
    return source_count, max_results_per_task, distribution


def _image_allocations(
    routes: Sequence[Mapping[str, Any]],
    image_cap: int,
) -> dict[str, int]:
    if image_cap < 0:
        raise BudgetEstimateError(
            code="invalid_cap",
            message="max_images must be non-negative",
            field="max_images",
            value=image_cap,
            minimum_supported=0,
        )
    required_routes = [route for route in routes if route["modality"] == "visual_required"]
    if image_cap < len(required_routes):
        raise BudgetEstimateError(
            code="impossible_image_cap",
            message="visual_required routes require at least one image each",
            field="max_images",
            value=image_cap,
            minimum_supported=len(required_routes),
        )
    allocations = {
        str(route["id"]): 0
        for route in routes
    }
    remaining = image_cap
    for route in required_routes:
        route_id = str(route["id"])
        allocations[route_id] = 1
        remaining -= 1
    visual_routes = [route for route in routes if route["modality"] != "text_only"]
    while remaining > 0:
        progressed = False
        for route in visual_routes:
            route_id = str(route["id"])
            max_for_route = int(route.get("max_images", 0))
            if allocations[route_id] >= max_for_route:
                continue
            allocations[route_id] += 1
            remaining -= 1
            progressed = True
            if remaining <= 0:
                break
        if not progressed:
            break
    return allocations


def _verifier_invocation_count(
    *,
    routes: Sequence[Mapping[str, Any]],
    source_distribution: Mapping[str, int],
    route_image_allocations: Mapping[str, int],
    max_verifier_invocations: int,
) -> int:
    total = 0
    for route in routes:
        route_id = str(route["id"])
        route_name = str(route["modality"])
        verifier_count = _ROUTE_VERIFIER_COUNTS[route_name]
        if route_name == "visual_optional" and route_image_allocations.get(route_id, 0) > 0:
            verifier_count += 1
        total += source_distribution.get(route_id, 0) * verifier_count
    return min(max_verifier_invocations, total)


def _model_call_placeholders(
    *,
    config: RunConfig,
    source_count: int,
    image_count: int,
    verifier_invocations: int,
    runner_stage_count: int,
    codex_runner: str,
    route_count: int,
) -> dict[str, int]:
    provider_api_calls = 0 if config.search_provider in {"codex-native", "manual"} else max(1, route_count)
    source_processing_calls = source_count
    image_analysis_calls = 0 if config.vlm_provider == "manual-visual-review" else image_count
    verifier_calls = verifier_invocations
    synthesis_calls = 1
    if codex_runner == "serial":
        runner_orchestration_calls = 0
    elif codex_runner == "codex-sdk":
        runner_orchestration_calls = max(1, math.ceil(runner_stage_count / 2))
    else:
        runner_orchestration_calls = runner_stage_count
    uncapped_total = (
        provider_api_calls
        + source_processing_calls
        + image_analysis_calls
        + verifier_calls
        + synthesis_calls
        + runner_orchestration_calls
    )
    total = min(config.budget.max_model_api_calls, uncapped_total)
    return {
        "provider_api_calls": provider_api_calls,
        "source_processing_calls": source_processing_calls,
        "image_analysis_calls": image_analysis_calls,
        "verifier_calls": verifier_calls,
        "synthesis_calls": synthesis_calls,
        "runner_orchestration_calls": runner_orchestration_calls,
        "total_model_api_calls_uncapped": uncapped_total,
        "total_model_api_calls": total,
    }


def _token_placeholders(
    *,
    source_count: int,
    image_count: int,
    verifier_invocations: int,
) -> dict[str, int]:
    source_input_tokens = source_count * 2_000
    image_input_tokens = image_count * 1_000
    verifier_tokens = verifier_invocations * 600
    synthesis_tokens = max(1_000, source_count * 400)
    output_tokens = 1_200 + source_count * 200 + image_count * 120
    return {
        "source_input_tokens": source_input_tokens,
        "image_input_tokens": image_input_tokens,
        "verifier_tokens": verifier_tokens,
        "synthesis_tokens": synthesis_tokens,
        "output_tokens": output_tokens,
        "total_input_tokens_placeholder": (
            source_input_tokens
            + image_input_tokens
            + verifier_tokens
            + synthesis_tokens
        ),
        "total_output_tokens_placeholder": output_tokens,
    }


def _cost_components(
    *,
    config: RunConfig,
    source_count: int,
    image_count: int,
    verifier_invocations: int,
    codex_subagent_count: int,
    runner_stage_count: int,
    codex_runner: str,
    model_calls: Mapping[str, int],
) -> dict[str, float]:
    provider_api_calls = model_calls["provider_api_calls"]
    return {
        "search_call_cost": _round_cost(provider_api_calls * _SEARCH_CALL_COST_USD[config.search_provider]),
        "fetch_compute_runtime_cost": _round_cost(source_count * _FETCH_COMPUTE_PER_SOURCE_USD),
        "codex_subagent_runtime_cost": _round_cost(codex_subagent_count * _CODEX_SUBAGENT_RUNTIME_USD),
        "runner_orchestration_overhead": _round_cost(
            runner_stage_count * _RUNNER_STAGE_OVERHEAD_USD[codex_runner]
        ),
        "text_model_input_output_cost": _round_cost(
            (
                model_calls["source_processing_calls"]
                + model_calls["synthesis_calls"]
            )
            * _TEXT_MODEL_CALL_USD
        ),
        "image_input_token_cost": _round_cost(image_count * _IMAGE_ANALYSIS_CALL_USD),
        "verifier_agent_cost": _round_cost(verifier_invocations * _VERIFIER_INVOCATION_USD),
        "synthesis_agent_cost": _round_cost(_SYNTHESIS_CALL_USD),
    }


def _minimum_bound_cost(
    components: Mapping[str, float],
    *,
    source_count: int,
    image_count: int,
    verifier_invocations: int,
) -> float:
    variable_discount = (
        source_count * _FETCH_COMPUTE_PER_SOURCE_USD
        + image_count * _IMAGE_ANALYSIS_CALL_USD
        + verifier_invocations * _VERIFIER_INVOCATION_USD
    ) * 0.35
    return _round_cost(max(0.0, sum(components.values()) - variable_discount))


def _reduce_for_cost_cap(
    *,
    config: RunConfig,
    routes: Sequence[Mapping[str, Any]],
    max_results: int,
    source_cap: int,
    image_cap: int,
    min_source_cap: int,
    min_image_cap: int,
    max_cost_usd: float,
    codex_runner: str,
    codex_subagent_concurrency: int,
    runner_agent_concurrency: int,
) -> tuple[int, int, _Plan]:
    reduced_source_cap = source_cap
    reduced_image_cap = image_cap
    plan = _build_plan(
        config=config,
        routes=routes,
        max_results=max_results,
        source_cap=reduced_source_cap,
        image_cap=reduced_image_cap,
        codex_runner=codex_runner,
        codex_subagent_concurrency=codex_subagent_concurrency,
        runner_agent_concurrency=runner_agent_concurrency,
    )
    while plan.high_water_cost_usd > max_cost_usd and reduced_image_cap > min_image_cap:
        reduced_image_cap -= 1
        plan = _build_plan(
            config=config,
            routes=routes,
            max_results=max_results,
            source_cap=reduced_source_cap,
            image_cap=reduced_image_cap,
            codex_runner=codex_runner,
            codex_subagent_concurrency=codex_subagent_concurrency,
            runner_agent_concurrency=runner_agent_concurrency,
        )
    step = len(routes)
    while plan.high_water_cost_usd > max_cost_usd and reduced_source_cap > min_source_cap:
        reduced_source_cap = max(min_source_cap, reduced_source_cap - step)
        plan = _build_plan(
            config=config,
            routes=routes,
            max_results=max_results,
            source_cap=reduced_source_cap,
            image_cap=reduced_image_cap,
            codex_runner=codex_runner,
            codex_subagent_concurrency=codex_subagent_concurrency,
            runner_agent_concurrency=runner_agent_concurrency,
        )
    return reduced_source_cap, reduced_image_cap, plan


def _initial_source_cap(
    *,
    preset: BudgetPreset,
    angle_count: int,
    max_results: int,
    user_max_sources: int | None,
) -> int:
    preset_cap = min(preset.max_sources, angle_count * max_results)
    source_cap = preset_cap if user_max_sources is None else min(preset_cap, user_max_sources)
    if source_cap < angle_count:
        raise BudgetEstimateError(
            code="impossible_source_cap",
            message="max_sources must allow at least one source per planner angle",
            field="max_sources",
            value=source_cap,
            minimum_supported=angle_count,
        )
    return source_cap


def _initial_image_cap(
    *,
    preset: BudgetPreset,
    routes: Sequence[Mapping[str, Any]],
    user_max_images: int | None,
) -> int:
    visual_capacity = sum(int(route.get("max_images", 0)) for route in routes if route["modality"] != "text_only")
    preset_cap = min(preset.max_images, visual_capacity)
    if user_max_images is None:
        image_cap = preset_cap
    else:
        image_cap = min(preset_cap, user_max_images)
    minimum = _minimum_image_cap(routes)
    if image_cap < minimum:
        raise BudgetEstimateError(
            code="impossible_image_cap",
            message="visual_required routes require at least one image each",
            field="max_images",
            value=image_cap,
            minimum_supported=minimum,
        )
    return image_cap


def _minimum_image_cap(routes: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for route in routes if route["modality"] == "visual_required")


def _effective_subagent_concurrency(
    preset: BudgetPreset,
    user_max_subagents: int | None,
    codex_runner: str,
) -> int:
    cap = preset.max_concurrent_codex_subagents
    if user_max_subagents is not None:
        cap = min(cap, user_max_subagents)
    if codex_runner == "serial":
        cap = min(cap, 1)
    return max(1, cap)


def _effective_runner_concurrency(
    preset: BudgetPreset,
    user_max_agents: int | None,
    codex_runner: str,
) -> int:
    cap = preset.max_concurrent_runner_agents
    if user_max_agents is not None:
        cap = min(cap, user_max_agents)
    if codex_runner == "serial":
        cap = min(cap, 1)
    return max(1, cap)


def _append_direct_cap_suggestions(
    suggestions: list[dict[str, Any]],
    *,
    preset: BudgetPreset,
    source_cap: int,
    image_cap: int,
    caps: BudgetCaps,
    codex_runner: str,
) -> None:
    if caps.max_sources is not None and source_cap < preset.max_sources:
        suggestions.append(
            _suggestion(
                "reduce_sources",
                "--max-sources",
                preset.max_sources,
                source_cap,
                "Search/fetch work will be reduced to the requested source cap.",
            )
        )
    if caps.max_images is not None and image_cap < preset.max_images:
        suggestions.append(
            _suggestion(
                "reduce_images",
                "--max-images",
                preset.max_images,
                image_cap,
                "Visual acquisition and VLM work will be reduced to the requested image cap.",
            )
        )
    if codex_runner == "serial":
        return


def _suggestion(
    code: str,
    flag: str,
    current: Any,
    suggested: Any,
    reason: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "flag": flag,
        "current": current,
        "suggested": suggested,
        "reason": reason,
    }


def _validate_caps(caps: BudgetCaps) -> None:
    positive_int_fields = {
        "max_sources": caps.max_sources,
        "max_subagents": caps.max_subagents,
        "max_agents": caps.max_agents,
    }
    for field, value in positive_int_fields.items():
        if value is not None and value < 1:
            raise BudgetEstimateError(
                code="invalid_cap",
                message=f"{field} must be at least 1",
                field=field,
                value=value,
                minimum_supported=1,
            )
    if caps.max_images is not None and caps.max_images < 0:
        raise BudgetEstimateError(
            code="invalid_cap",
            message="max_images must be non-negative",
            field="max_images",
            value=caps.max_images,
            minimum_supported=0,
        )
    if caps.max_cost_usd is not None and (
        not math.isfinite(caps.max_cost_usd) or caps.max_cost_usd <= 0
    ):
        raise BudgetEstimateError(
            code="invalid_cap",
            message="max_cost_usd must be greater than 0",
            field="max_cost_usd",
            value=caps.max_cost_usd,
            minimum_supported=0.01,
        )


def _normalize_runner(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized not in CODEX_RUNNERS:
        raise BudgetEstimateError(
            code="invalid_codex_runner",
            message="codex_runner must be one of: " + ", ".join(CODEX_RUNNERS),
            field="codex_runner",
            value=value,
            allowed_values=CODEX_RUNNERS,
        )
    return normalized


def _normalized_route(route: Mapping[str, Any], index: int) -> dict[str, Any]:
    route_id = str(route.get("id") or f"angle_{index:03d}")
    modality = str(route.get("modality", ""))
    if modality not in _ROUTE_VERIFIER_COUNTS:
        raise BudgetEstimateError(
            code="invalid_route",
            message="route modality must be one of: " + ", ".join(_ROUTE_VERIFIER_COUNTS),
            field="routing.modality",
            value=modality,
        )
    max_images = int(route.get("max_images", 0))
    return {
        "id": route_id,
        "modality": modality,
        "max_images": max(0, max_images),
    }


def _route_counts(routes: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {route: 0 for route in _ROUTE_VERIFIER_COUNTS}
    for route in routes:
        counts[str(route["modality"])] += 1
    return counts


def _preset_caps(preset: BudgetPreset) -> dict[str, Any]:
    return asdict(preset)


def _caps_to_dict(caps: BudgetCaps) -> dict[str, Any]:
    return {
        "max_sources": caps.max_sources,
        "max_images": caps.max_images,
        "max_subagents": caps.max_subagents,
        "max_agents": caps.max_agents,
        "max_cost_usd": caps.max_cost_usd,
    }


def _round_cost(value: float) -> float:
    return round(float(value) + 0.0000000001, 6)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
