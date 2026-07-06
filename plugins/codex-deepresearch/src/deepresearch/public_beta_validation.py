"""Public Beta real-use validation harness.

The harness is intentionally artifact-driven. It can classify real run
directories when the operator supplies them, but in credential-free public
repo validation it records explicit blocked diagnostics instead of pretending
that provider-backed research passed.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .visual_artifacts import (
    VISUAL_PROVIDER_STATUS_FILENAME,
    real_automatic_visual_release_counts,
)


PUBLIC_BETA_PROMPT_MANIFEST_SCHEMA_VERSION = (
    "codex-deepresearch.public-beta-prompts.v0"
)
PUBLIC_BETA_VALIDATION_SCHEMA_VERSION = (
    "codex-deepresearch.public-beta-validation.v0"
)
PUBLIC_BETA_VALIDATION_RESULTS_FILENAME = "public_beta_validation_results.json"
PUBLIC_BETA_VALIDATION_SUMMARY_FILENAME = "public_beta_validation_summary.md"
PROVIDER_PROVENANCE_FILENAME = "provider_provenance.json"
RUN_STATUS_SCHEMA_VERSION = "codex-deepresearch.run-status.v0"
REPORT_STATUS_SCHEMA_VERSIONS = {
    "codex-deepresearch.report-status.v0",
    "codex-deepresearch.report-generation.v0",
}
SUPPLIED_RUN_MAX_AGE_DAYS = 30
MIN_PUBLIC_BETA_REAL_VISUAL_CANDIDATES = 10
MIN_PUBLIC_BETA_REAL_VLM_IMAGES_ANALYZED = 3
MIN_PUBLIC_BETA_REPORT_CITED_VISUAL_OR_MIXED_CLAIMS = 1

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST = (
    PLUGIN_ROOT / "validation" / "public_beta_prompts.json"
)

VISUAL_ROUTES = {"visual_required", "visual_optional"}
SEARCH_RESULT_ROUTES = {"text_only", "visual_required", "visual_optional"}
RELEASE_SEARCH_RESULT_TYPES = {"web", "pdf", "image", "news", "academic", "manual"}
RELEASE_SEARCH_RESULT_REQUIRED_FIELDS = (
    "id",
    "task_id",
    "angle_id",
    "route",
    "query",
    "url",
    "title",
    "snippet",
    "result_type",
    "rank",
    "accessed_at",
    "policy_decision",
    "provider",
    "provider_mode",
    "retrieval_status",
    "prompt_id",
    "suite_id",
    "prompt_hash",
    "handoff_artifact",
)
PASS_TERMINAL_STATUSES = {
    "completed_parallel",
    "completed_partial_parallel",
    "completed_serial_handoff",
    "completed_auto_visual",
}
SEMANTIC_RELEASE_TERMINAL_STATUSES = {
    "completed_parallel",
    "completed_partial_parallel",
    "completed_serial_handoff",
}
SEMANTIC_RELEASE_FAILURE_TERMINAL_STATUSES = {
    "blocked_semantic_planner_unavailable",
    "completed_fixture",
    "completed_manual_planner_fallback",
}
SEMANTIC_RELEASE_REQUIRED_ARTIFACTS = (
    "semantic_expectation_oracle",
    "semantic_plan",
    "semantic_plan_review",
    "semantic_planner_validation",
)
SEMANTIC_RELEASE_REQUIRED_FIELD_SOURCES = (
    "run_status",
    "evidence.semantic_planner",
    "semantic_expectation_oracle",
    "semantic_plan",
    "semantic_plan.semantic_plan",
    "semantic_plan_review",
    "semantic_planner_validation",
)
SEMANTIC_RELEASE_REQUIRED_CODEX_SOURCE_SOURCES = (
    "evidence.semantic_planner",
    "semantic_plan.semantic_plan",
)
BLOCKED_TERMINAL_STATUSES = {
    "blocked_preflight",
    "blocked_semantic_planner_unavailable",
    "blocked_missing_search_handoff",
    "blocked_parallel_execution",
    "blocked_missing_visual_provider",
    "blocked_missing_vlm_provider",
    "policy_blocked_visual",
    "policy_blocked",
    "needs_manual_review",
}
INCLUDED_FAILURE_STATUSES = {
    "partial_auto_visual",
    "budget_pruned_visual",
    "completed_manual_planner_fallback",
    "failed_parallel_no_accepted_shards",
    "failed_release_handoff_invalid",
    "failed_validation",
    "failed_synthesis",
}
EXCLUDED_FIXTURE_STATUSES = {"completed_fixture"}
FAILURE_CATEGORIES = (
    "provider_failure",
    "fetch_failure",
    "policy_block",
    "vlm_failure",
    "visual_contradiction",
    "report_linkage_failure",
    "artifact_handoff_failure",
    "synthesis_shape_failure",
)
PUBLIC_BETA_COMPLETION_MODES = {"codex-native", "external-gated"}
DEFAULT_PUBLIC_BETA_COMPLETION_MODE = "codex-native"
GATE_THRESHOLDS = {
    "fresh_session_full_runner_artifact_handoff": 0.98,
    "codex_plugin_interactive_visual_e2e": 0.90,
    "automated_cli_real_provider_visual_e2e": 0.90,
    "automatic_web_visual_e2e": 0.90,
}
PARTIAL_PARALLEL_TARGET_RATE = 0.05
PARTIAL_PARALLEL_WARNING_RATE = 0.10
PARTIAL_PARALLEL_MIN_ENFORCEMENT_DENOMINATOR = 20
PARTIAL_PARALLEL_HISTORY_SUITE_COUNT = 3
CODEX_NATIVE_COMPLETION_GATE_IDS = {
    "fresh_session_full_runner_artifact_handoff",
    "codex_plugin_interactive_visual_e2e",
    "automatic_web_visual_e2e",
}
OPTIONAL_DIAGNOSTIC_GATE_IDS = set(GATE_THRESHOLDS) - CODEX_NATIVE_COMPLETION_GATE_IDS
EXTERNAL_GATE_REQUIREMENTS = {
    "fresh_session_full_runner_artifact_handoff": {
        "schemas": {"codex-deepresearch.fresh-session-e2e.v0"},
        "min_counts": {"completed_real_parallel": 1},
        "zero_counts": {"blocked_explicit"},
    },
    "codex_plugin_interactive_visual_e2e": {
        "schemas": {"codex-deepresearch.fresh-session-visual-e2e.v0"},
        "min_counts": {"release_gate_passed": 1, "completed_auto_visual": 1},
        "zero_counts": {"blocked_public_safe"},
    },
    "automated_cli_real_provider_visual_e2e": {
        "schemas": {"codex-deepresearch.automated-visual-e2e.v0"},
        "min_counts": {"passed": 4},
        "zero_counts": {"blocked", "failed"},
        "thresholds": {
            "min_image_candidates": 10,
            "min_vlm_images": 3,
            "report_cited_visual_or_mixed_claims": 1,
        },
    },
    "automatic_web_visual_e2e": {
        "schemas": {"codex-deepresearch.automated-visual-e2e.v0"},
        "min_counts": {"passed": 4},
        "zero_counts": {"blocked", "failed"},
        "thresholds": {
            "min_image_candidates": 10,
            "min_vlm_images": 3,
            "report_cited_visual_or_mixed_claims": 1,
        },
    },
}
_REAL_ACQUISITION_PROVIDER_KINDS = {
    "web_image_search",
    "page_extractor",
    "screenshot",
    "pdf_rasterizer",
    "visual_acquisition",
}
_CODEX_NATIVE_SEARCH_PROVIDERS = {
    "codex-native",
    "codex-native-search",
    "codex-web-search",
}
_CODEX_NATIVE_VISUAL_ACQUISITION_PROVIDERS = _CODEX_NATIVE_SEARCH_PROVIDERS | {
    "browser-screenshot",
    "child-discovered-image-url",
    "child_discovered_image_url",
    "codex-browser-screenshot",
    "page-image-extractor",
    "page_extractor",
    "pdf_rasterizer",
    "screenshot",
}
_FRESH_SESSION_REQUIRED_ACCEPTANCE = {
    "fixture_scenario_completed",
    "serial_fallback_blocked_explicit",
    "real_codex_exec_asserted_or_explicit",
    "provenance_distinguishes_fixture_serial_real",
    "ci_public_safe_without_private_artifacts",
}
_FRESH_SESSION_VISUAL_REQUIRED_ACCEPTANCE = {
    "visual_required_prompt_exercised",
    "release_gate_passed",
    "completed_auto_visual_validation_rules_met",
    "blocked_runs_name_missing_capability",
    "blocked_runs_do_not_count_as_release_passes",
    "fixture_manual_user_evidence_excluded",
    "final_transcript_exposes_artifacts_and_status_summary",
}
_AUTOMATED_VISUAL_REQUIRED_ACCEPTANCE = {
    "provider_scenario_gates_cover_required_set",
    "no_user_image_automated_runs_reach_completed_auto_visual",
    "all_required_scenarios_passed",
    "image_centric_has_10_real_candidates",
    "accepted_runs_have_3_real_openai_vlm_images",
    "accepted_runs_have_report_cited_visual_or_mixed_claim",
    "fixture_manual_user_provided_records_excluded",
}
_AUTOMATED_VISUAL_REQUIRED_SCENARIOS = {
    "product_image_discovery",
    "ui_screenshot_comparison",
    "public_chart_report_visual_extraction",
    "public_pdf_paper_figure_extraction",
}
_AUTOMATED_VISUAL_REQUIRED_ARTIFACTS = {
    "run_status",
    "visual_provider_status",
    "evidence",
    "report_status",
    "visual_search_plan",
    "report",
    "visual_candidates",
    "image_fetch_status",
    "visual_observations",
    "verifier_votes",
}


class PublicBetaValidationError(ValueError):
    """Raised when the Public Beta validation gate is not ready."""

    def __init__(self, message: str, *, results_path: Path | None = None) -> None:
        super().__init__(message)
        self.results_path = results_path


def parse_public_beta_run(value: str) -> tuple[str, Path]:
    """Parse prompt_id=run_dir CLI input."""

    if "=" not in value:
        raise PublicBetaValidationError(
            "--run must be in the form prompt_id=run_dir"
        )
    prompt_id, run_dir = value.split("=", 1)
    prompt_id = prompt_id.strip()
    run_dir = run_dir.strip()
    if not prompt_id or not run_dir:
        raise PublicBetaValidationError(
            "--run must include both a prompt id and a run directory"
        )
    return prompt_id, Path(run_dir)


def parse_public_beta_gate_result(value: str) -> tuple[str, Path]:
    """Parse gate_id=results_json CLI input."""

    if "=" not in value:
        raise PublicBetaValidationError(
            "--gate-result must be in the form gate_id=results_json"
        )
    gate_id, results_path = value.split("=", 1)
    gate_id = gate_id.strip()
    results_path = results_path.strip()
    if not gate_id or not results_path:
        raise PublicBetaValidationError(
            "--gate-result must include both a gate id and a results JSON path"
        )
    if gate_id not in GATE_THRESHOLDS:
        raise PublicBetaValidationError(
            "unknown gate id for --gate-result: " + gate_id
        )
    return gate_id, Path(results_path)


def normalize_public_beta_completion_mode(value: str | None) -> str:
    """Normalize the Public Beta completion path."""

    mode = (value or DEFAULT_PUBLIC_BETA_COMPLETION_MODE).strip().lower()
    mode = mode.replace("_", "-")
    if mode not in PUBLIC_BETA_COMPLETION_MODES:
        raise PublicBetaValidationError(
            "unknown public beta completion mode: "
            + str(value)
            + "; expected one of "
            + ", ".join(sorted(PUBLIC_BETA_COMPLETION_MODES))
        )
    return mode


def run_public_beta_validation(
    *,
    runs_dir: str | Path,
    suite_id: str = "public-beta-validation",
    clean: bool = False,
    prompt_manifest: str | Path = DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST,
    prompt_runs: Mapping[str, str | Path] | None = None,
    gate_results: Mapping[str, str | Path] | None = None,
    completion_mode: str = DEFAULT_PUBLIC_BETA_COMPLETION_MODE,
) -> dict[str, Any]:
    """Classify the Phase 3 Public Beta real-use validation set."""

    completion_mode = normalize_public_beta_completion_mode(completion_mode)
    root_runs_dir = Path(runs_dir)
    suite_dir = root_runs_dir / suite_id
    if suite_dir.exists():
        if not clean:
            raise PublicBetaValidationError(
                f"suite directory already exists: {suite_dir}"
            )
        shutil.rmtree(suite_dir)
    suite_dir.mkdir(parents=True)
    results_path = suite_dir / PUBLIC_BETA_VALIDATION_RESULTS_FILENAME
    summary_path = suite_dir / PUBLIC_BETA_VALIDATION_SUMMARY_FILENAME
    generated_at = _utc_now()

    manifest_path = Path(prompt_manifest)
    manifest = load_public_beta_prompt_manifest(manifest_path)
    prompts = manifest["prompts"]
    normalized_runs = _normalize_mapping(prompt_runs)
    unknown_runs = sorted(set(normalized_runs) - {prompt["id"] for prompt in prompts})
    if unknown_runs:
        raise PublicBetaValidationError(
            "unknown public beta prompt id(s): " + ", ".join(unknown_runs),
            results_path=results_path,
        )

    evaluated_runs = []
    for prompt in prompts:
        run_dir = normalized_runs.get(prompt["id"])
        if run_dir is None:
            evaluated_runs.append(_blocked_prompt_run(prompt, suite_dir=suite_dir))
        else:
            evaluated_runs.append(
                evaluate_public_beta_prompt_run(
                    prompt,
                    run_dir,
                    suite_id=suite_id,
                    validation_time=generated_at,
                )
            )

    prompt_metrics = _metric_summaries(evaluated_runs)
    external_gate_results = _external_gate_results(
        gate_results,
        validation_time=generated_at,
    )
    required_gate_ids = _required_completion_gate_ids(
        completion_mode,
        prompt_metrics=prompt_metrics,
    )
    prompt_metrics = _prompt_metrics_with_completion_roles(
        prompt_metrics,
        required_gate_ids=required_gate_ids,
    )
    acceptance = _acceptance(
        manifest=manifest,
        evaluated_runs=evaluated_runs,
        prompt_metrics=prompt_metrics,
        summary_path=summary_path,
    )
    outcome_counts = _outcome_counts(evaluated_runs)
    classification_counts = _classification_counts(evaluated_runs)
    reliability = _reliability_summary(
        root_runs_dir=root_runs_dir,
        current_suite_id=suite_id,
        current_generated_at=generated_at,
        current_runs=evaluated_runs,
    )
    prompt_release_gate_ready = all(
        prompt_metrics[metric_id].get("release_gate_ready") is True
        for metric_id in required_gate_ids
    )
    external_gates_required = completion_mode == "external-gated"
    external_release_gate_ready = (
        all(
            result.get("release_gate_ready") is True
            for result in external_gate_results.values()
        )
        if external_gates_required
        else True
    )
    release_gate_ready = prompt_release_gate_ready and external_release_gate_ready
    external_gate_failed = any(
        result.get("status") == "failed"
        for result in external_gate_results.values()
    )
    pre_summary_acceptance = {
        key: value
        for key, value in acceptance.items()
        if key != "summary_artifact_public_safe"
    }
    reliability_regression = (
        reliability.get("partial_parallel", {}).get("release_gate_blocking") is True
    )
    status = (
        "failed"
        if outcome_counts["failed"] > 0
        or (external_gates_required and external_gate_failed)
        or reliability_regression
        else "passed"
        if release_gate_ready and all(pre_summary_acceptance.values())
        else "blocked"
    )
    issue_75_completion_ready = (
        status == "passed"
        and release_gate_ready
        and all(pre_summary_acceptance.values())
    )
    results: dict[str, Any] = {
        "schema_version": PUBLIC_BETA_VALIDATION_SCHEMA_VERSION,
        "status": status,
        "suite_id": suite_id,
        "suite_dir": str(suite_dir.resolve()),
        "prompt_manifest": str(manifest_path.resolve()),
        "generated_at": generated_at,
        "public_safe": True,
        "raw_run_bundles_copied": False,
        "release_gate_ready": release_gate_ready and status == "passed",
        "issue_75_completion_ready": issue_75_completion_ready,
        "completion_mode": completion_mode,
        "validation_mode": (
            "release_validation" if issue_75_completion_ready else "diagnostic_harness"
        ),
        "release_gate_components": {
            "completion_mode": completion_mode,
            "required_prompt_metric_gate_ids": sorted(required_gate_ids),
            "required_codex_native_gate_ids": sorted(required_gate_ids),
            "prompt_metrics_ready": prompt_release_gate_ready,
            "external_gates_required": external_gates_required,
            "required_external_gate_ids": sorted(external_gate_results)
            if external_gates_required
            else [],
            "external_gates_ready": external_release_gate_ready,
            "supplied_real_runs_ready": acceptance.get(
                "all_prompt_runs_are_supplied_sanitized_real_runs",
                False,
            ),
            "optional_diagnostic_gate_ids": sorted(
                set(GATE_THRESHOLDS)
                if not external_gates_required
                else set()
            ),
            "optional_diagnostic_gates_failed": [
                gate_id
                for gate_id, result in sorted(external_gate_results.items())
                if not external_gates_required and result.get("status") == "failed"
            ],
            "failed_external_gate_ids": [
                gate_id
                for gate_id, result in sorted(external_gate_results.items())
                if external_gates_required and result.get("status") == "failed"
            ],
        },
        "prompt_coverage": {
            "total_prompts": len(prompts),
            "visual_prompts": sum(
                1 for prompt in prompts if prompt.get("route") in VISUAL_ROUTES
            ),
            "text_only_prompts": sum(
                1 for prompt in prompts if prompt.get("route") == "text_only"
            ),
            "public_safe_prompts": sum(
                1 for prompt in prompts if prompt.get("public_safe") is True
            ),
            "required_total_prompts": 20,
            "required_visual_prompts": 8,
        },
        "outcome_counts": outcome_counts,
        "classification_counts": classification_counts,
        "failure_category_counts": _failure_category_counts(evaluated_runs),
        "reliability": reliability,
        "prompt_metrics": prompt_metrics,
        "external_gate_results": external_gate_results,
        "acceptance": acceptance,
        "runs": evaluated_runs,
        "remaining_gaps": _remaining_gaps(
            evaluated_runs=evaluated_runs,
            prompt_metrics=prompt_metrics,
            external_gate_results=external_gate_results,
            required_gate_ids=required_gate_ids,
            external_gates_required=external_gates_required,
            reliability=reliability,
        ),
        "artifacts": {
            "results": str(results_path.resolve()),
            "summary": str(summary_path.resolve()),
        },
    }
    summary_path.write_text(_render_summary(results), encoding="utf-8")
    acceptance["summary_artifact_public_safe"] = _summary_is_public_safe(summary_path)
    results["acceptance"] = acceptance
    _write_json(results_path, results)

    if status != "passed":
        raise PublicBetaValidationError(
            f"public beta validation {status}; see {results_path}",
            results_path=results_path,
        )
    return results


def load_public_beta_prompt_manifest(path: str | Path) -> dict[str, Any]:
    """Load and validate the curated Public Beta prompt manifest."""

    manifest_path = Path(path)
    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != PUBLIC_BETA_PROMPT_MANIFEST_SCHEMA_VERSION:
        raise PublicBetaValidationError(
            "public beta prompt manifest has unsupported schema_version"
        )
    prompts = manifest.get("prompts")
    if not isinstance(prompts, list):
        raise PublicBetaValidationError("public beta prompt manifest prompts must be a list")
    seen: set[str] = set()
    for prompt in prompts:
        if not isinstance(prompt, Mapping):
            raise PublicBetaValidationError("public beta prompt entries must be objects")
        prompt_id = prompt.get("id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise PublicBetaValidationError("public beta prompt entries need id")
        if prompt_id in seen:
            raise PublicBetaValidationError(
                f"duplicate public beta prompt id: {prompt_id}"
            )
        seen.add(prompt_id)
        if prompt.get("route") not in {"text_only", "visual_required", "visual_optional"}:
            raise PublicBetaValidationError(
                f"public beta prompt {prompt_id} has invalid route"
            )
        if prompt.get("public_safe") is not True:
            raise PublicBetaValidationError(
                f"public beta prompt {prompt_id} must be public_safe=true"
            )
        if not isinstance(prompt.get("prompt"), str) or not prompt["prompt"].strip():
            raise PublicBetaValidationError(
                f"public beta prompt {prompt_id} needs prompt text"
            )
        if not isinstance(prompt.get("gate_tags"), list) or not prompt["gate_tags"]:
            raise PublicBetaValidationError(
                f"public beta prompt {prompt_id} needs gate_tags"
            )
    visual_count = sum(1 for prompt in prompts if prompt.get("route") in VISUAL_ROUTES)
    if len(prompts) < 20:
        raise PublicBetaValidationError(
            "public beta prompt manifest must contain at least 20 prompts"
        )
    if visual_count < 8:
        raise PublicBetaValidationError(
            "public beta prompt manifest must contain at least 8 visual prompts"
        )
    return {"schema_version": manifest["schema_version"], "prompts": prompts, **manifest}


def evaluate_public_beta_prompt_run(
    prompt: Mapping[str, Any],
    run_dir: str | Path,
    *,
    suite_id: str = "public-beta-validation",
    validation_time: str | None = None,
) -> dict[str, Any]:
    """Evaluate one supplied real run directory for a manifest prompt."""

    run_path = Path(run_dir)
    artifacts = _artifact_paths(run_path, route=str(prompt.get("route", "")))
    loaded = {
        name: _read_optional_json(path)
        for name, path in artifacts.items()
        if path.name.endswith(".json")
    }
    run_status = loaded.get("run_status")
    visual_provider_status = loaded.get("visual_provider_status")
    report_status = loaded.get("report_status")
    parallel_status = loaded.get("parallel_status")
    evidence = loaded.get("evidence")
    report_text = _read_optional_text(artifacts["report"])
    terminal_status = _terminal_status(run_status)
    missing_status = not isinstance(run_status, Mapping)
    required_artifacts = _required_status_artifacts(prompt, terminal_status)
    missing_artifacts = [
        name
        for name in required_artifacts
        if not artifacts[name].exists()
    ]
    run_binding = _supplied_run_binding(
        prompt=prompt,
        run_dir=run_path,
        suite_id=suite_id,
        loaded_artifacts=loaded,
        required_artifacts=required_artifacts,
        run_status=run_status if isinstance(run_status, Mapping) else {},
        evidence=evidence if isinstance(evidence, Mapping) else {},
        validation_time=validation_time,
    )
    status_consistency_failures = _status_consistency_failures(
        prompt=prompt,
        run_status=run_status if isinstance(run_status, Mapping) else {},
        visual_provider_status=visual_provider_status
        if isinstance(visual_provider_status, Mapping)
        else {},
        report_status=report_status if isinstance(report_status, Mapping) else {},
    )
    codex_native_checks = _codex_native_handoff_checks(
        prompt=prompt,
        suite_id=suite_id,
        run_path=run_path,
        artifacts=artifacts,
        run_status=run_status if isinstance(run_status, Mapping) else {},
        evidence=evidence if isinstance(evidence, Mapping) else {},
    )
    semantic_release_checks: dict[str, Any] | None = None
    if _requires_semantic_release_gate(prompt, terminal_status):
        semantic_release_checks = _semantic_release_checks(
            loaded_artifacts=loaded,
            run_status=run_status if isinstance(run_status, Mapping) else {},
            evidence=evidence if isinstance(evidence, Mapping) else {},
        )

    provider_provenance = _provider_provenance(
        prompt=prompt,
        run_status=run_status if isinstance(run_status, Mapping) else {},
        evidence=evidence if isinstance(evidence, Mapping) else {},
        visual_provider_status=visual_provider_status
        if isinstance(visual_provider_status, Mapping)
        else {},
        supplied_run=True,
    )
    if missing_status:
        classification = "included_failure"
        status = "failed"
        failure_category = "artifact_handoff_failure"
        detail = "run_status.json is missing or invalid"
    else:
        classification, status = _metric_classification(
            prompt,
            terminal_status,
            run_status if isinstance(run_status, Mapping) else {},
        )
        failure_category = None if status == "passed" else _failure_category(
            terminal_status,
            missing_artifacts=missing_artifacts,
            visual_provider_status=visual_provider_status
            if isinstance(visual_provider_status, Mapping)
            else {},
            report_status=report_status if isinstance(report_status, Mapping) else {},
        )
        detail = _failure_detail(
            status=status,
            terminal_status=terminal_status,
            missing_artifacts=missing_artifacts,
        )
    visual_release_checks: dict[str, Any] | None = None
    if prompt.get("route") in VISUAL_ROUTES:
        visual_release_checks = _visual_release_checks(
            run_path=run_path,
            prompt=prompt,
            run_status=run_status if isinstance(run_status, Mapping) else {},
            visual_provider_status=visual_provider_status
            if isinstance(visual_provider_status, Mapping)
            else {},
            evidence=evidence if isinstance(evidence, Mapping) else {},
            report_status=report_status if isinstance(report_status, Mapping) else {},
            report_text=report_text or "",
        )
    if status == "passed" and missing_artifacts:
        status = "failed"
        classification = "included_failure"
        failure_category = "artifact_handoff_failure"
        detail = "passing terminal status did not expose required status artifacts"
    if status == "passed" and not run_binding["valid"]:
        status = "failed"
        classification = "included_failure"
        failure_category = "artifact_handoff_failure"
        detail = "supplied run is not bound to this public beta prompt: " + "; ".join(
            run_binding["failures"]
        )
    if status == "passed" and status_consistency_failures:
        status = "failed"
        classification = "included_failure"
        failure_category = "artifact_handoff_failure"
        detail = "supplied run status artifacts are stale or inconsistent: " + "; ".join(
            status_consistency_failures
        )
    if status == "passed" and not codex_native_checks["valid"]:
        status = "failed"
        classification = "included_failure"
        failure_category = "artifact_handoff_failure"
        detail = "supplied run lacks Codex-native handoff proof: " + "; ".join(
            codex_native_checks["failures"]
        )
    if (
        status == "passed"
        and semantic_release_checks is not None
        and not semantic_release_checks["valid"]
    ):
        status = "failed"
        classification = "included_failure"
        failure_category = "artifact_handoff_failure"
        detail = "semantic planner release gate failed: " + "; ".join(
            failure["detail"] for failure in semantic_release_checks["failures"]
        )
    if (
        status == "passed"
        and visual_release_checks is not None
        and not visual_release_checks["valid"]
    ):
        status = "failed"
        classification = "included_failure"
        failure_category = _visual_failure_category(visual_release_checks["failures"])
        detail = "visual prompt lacks release-grade evidence: " + "; ".join(
            failure["detail"] for failure in visual_release_checks["failures"]
        )

    result = {
        "id": prompt["id"],
        "prompt": prompt["prompt"],
        "route": prompt["route"],
        "gate_tags": list(prompt.get("gate_tags", [])),
        "mode_targets": list(prompt.get("mode_targets", [])),
        "status": status,
        "terminal_status": terminal_status,
        "metric_classification": classification,
        "ok": bool(isinstance(run_status, Mapping) and run_status.get("ok") is True),
        "terminal": bool(
            isinstance(run_status, Mapping) and run_status.get("terminal") is True
        ),
        "run_dir": str(run_path.resolve()),
        "public_run_ref": f"supplied-run:{prompt['id']}",
        "status_artifacts": {
            name: str(path.resolve())
            for name, path in artifacts.items()
            if path.exists()
        },
        "missing_artifacts": missing_artifacts,
        "provider_provenance": provider_provenance,
        "supplied_run_binding": run_binding,
        "status_consistency_failures": status_consistency_failures,
        "codex_native_handoff_checks": codex_native_checks,
        "partial_parallel_summary": _run_partial_parallel_summary(
            run_status if isinstance(run_status, Mapping) else {},
            parallel_status if isinstance(parallel_status, Mapping) else {},
            final_artifact_gate_passed=status == "passed",
        ),
        "failure_category": failure_category,
        "failure_detail": detail,
        "public_safe": prompt.get("public_safe") is True,
        "raw_run_bundle_copied": False,
    }
    if semantic_release_checks is not None:
        result["semantic_release_checks"] = semantic_release_checks
    if visual_release_checks is not None:
        result["visual_release_checks"] = visual_release_checks
    return result


def _blocked_prompt_run(
    prompt: Mapping[str, Any],
    *,
    suite_dir: Path,
) -> dict[str, Any]:
    run_dir = suite_dir / "prompt-runs" / str(prompt["id"])
    run_dir.mkdir(parents=True, exist_ok=True)
    is_visual = prompt.get("route") in VISUAL_ROUTES
    status = "blocked_missing_visual_provider" if is_visual else "blocked_missing_search_handoff"
    category = _failure_category(status)
    provider_provenance = _provider_provenance(
        prompt=prompt,
        run_status={},
        evidence={},
        visual_provider_status={},
        supplied_run=False,
    )
    now = _utc_now()
    status_payload = {
        "schema_version": "codex-deepresearch.run-status.v0",
        "status": status,
        "ok": False,
        "terminal": True,
        "created_at": now,
        "prompt_id": prompt["id"],
        "selected_mode": list(prompt.get("mode_targets", [])),
        "metric_classification": "excluded_blocked",
        "diagnostics": {
            "actionable_cause": (
                "No sanitized real run directory was supplied for this public-safe "
                "beta validation prompt."
            ),
            "next_step": (
                "Run the prompt with configured real providers, verify the output is "
                "public-safe, then pass --run "
                f"{prompt['id']}=<run_dir> to this validation command."
            ),
        },
    }
    _write_json(run_dir / "run_status.json", status_payload)
    _write_json(run_dir / PROVIDER_PROVENANCE_FILENAME, provider_provenance)
    artifacts = {
        "run_status": str((run_dir / "run_status.json").resolve()),
        "provider_provenance": str((run_dir / PROVIDER_PROVENANCE_FILENAME).resolve()),
    }
    if is_visual:
        visual_status = {
            "schema_version": "codex-deepresearch.visual-provider-status.v0",
            "run_id": run_dir.name,
            "status": status,
            "ok": False,
            "terminal": True,
            "metric_classification": "excluded_blocked",
            "providers": [
                {
                    "provider": provider,
                    "provider_kind": _provider_kind(provider),
                    "provider_mode": "real",
                    "configured": False,
                    "available": False,
                    "blocked_reason": "real_run_directory_not_supplied",
                    "invocations": 0,
                    "candidates_discovered": 0,
                    "artifacts_fetched": 0,
                    "vlm_images_analyzed": 0,
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                    "last_error": "real_run_directory_not_supplied",
                }
                for provider in prompt.get("visual_provider_requirements", [])
            ],
        }
        _write_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME, visual_status)
        artifacts["visual_provider_status"] = str(
            (run_dir / VISUAL_PROVIDER_STATUS_FILENAME).resolve()
        )

    return {
        "id": prompt["id"],
        "prompt": prompt["prompt"],
        "route": prompt["route"],
        "gate_tags": list(prompt.get("gate_tags", [])),
        "mode_targets": list(prompt.get("mode_targets", [])),
        "status": "blocked",
        "terminal_status": status,
        "metric_classification": "excluded_blocked",
        "ok": False,
        "terminal": True,
        "run_dir": str(run_dir.resolve()),
        "public_run_ref": f"blocked-diagnostic:{prompt['id']}",
        "status_artifacts": artifacts,
        "missing_artifacts": [],
        "provider_provenance": provider_provenance,
        "supplied_run_binding": {
            "valid": False,
            "prompt_id": prompt["id"],
            "suite_id": None,
            "prompt_hash": _prompt_hash(str(prompt["prompt"])),
            "created_at": now,
            "failures": ["sanitized real run directory was not supplied"],
        },
        "status_consistency_failures": [],
        "failure_category": category,
        "failure_detail": status_payload["diagnostics"]["actionable_cause"],
        "public_safe": True,
        "raw_run_bundle_copied": False,
    }


def _metric_summaries(runs: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        metric_id: _metric_summary(
            metric_id=metric_id,
            threshold=threshold,
            runs=[run for run in runs if metric_id in run.get("gate_tags", [])],
        )
        for metric_id, threshold in GATE_THRESHOLDS.items()
    }


def _metric_summary(
    *,
    metric_id: str,
    threshold: float,
    runs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    passed = sum(1 for run in runs if run.get("status") == "passed")
    failed = sum(1 for run in runs if run.get("status") == "failed")
    blocked = sum(1 for run in runs if run.get("status") == "blocked")
    excluded = sum(1 for run in runs if run.get("status") == "excluded")
    denominator = passed + failed
    pass_rate = passed / denominator if denominator else None
    threshold_met = pass_rate is not None and pass_rate >= threshold
    return {
        "metric_id": metric_id,
        "threshold": threshold,
        "completion_required": metric_id in CODEX_NATIVE_COMPLETION_GATE_IDS,
        "diagnostic_only": metric_id in OPTIONAL_DIAGNOSTIC_GATE_IDS,
        "prompt_count": len(runs),
        "passed": passed,
        "failed_non_blocked": failed,
        "blocked": blocked,
        "excluded": excluded,
        "denominator_completed_non_blocked": denominator,
        "pass_rate": pass_rate,
        "threshold_met": threshold_met,
        "release_gate_ready": threshold_met and blocked == 0 and failed == 0,
        "blocked_prompt_ids": [
            str(run.get("id")) for run in runs if run.get("status") == "blocked"
        ],
        "failed_prompt_ids": [
            str(run.get("id")) for run in runs if run.get("status") == "failed"
        ],
    }


def _required_completion_gate_ids(
    completion_mode: str,
    *,
    prompt_metrics: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    if completion_mode == "codex-native":
        return set(CODEX_NATIVE_COMPLETION_GATE_IDS)
    return {
        metric_id
        for metric_id, metric in prompt_metrics.items()
        if int(metric.get("prompt_count") or 0) > 0
    }


def _prompt_metrics_with_completion_roles(
    prompt_metrics: Mapping[str, Mapping[str, Any]],
    *,
    required_gate_ids: set[str],
) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    for metric_id, metric in prompt_metrics.items():
        updated = dict(metric)
        updated["completion_required"] = metric_id in required_gate_ids
        updated["diagnostic_only"] = metric_id not in required_gate_ids
        metrics[metric_id] = updated
    return metrics


def _external_gate_results(
    gate_results: Mapping[str, str | Path] | None,
    *,
    validation_time: str | None = None,
) -> dict[str, dict[str, Any]]:
    normalized = _normalize_mapping(gate_results)
    results: dict[str, dict[str, Any]] = {}
    for gate_id in GATE_THRESHOLDS:
        path = normalized.get(gate_id)
        if path is None:
            results[gate_id] = {
                "status": "not_supplied",
                "release_gate_ready": False,
                "detail": (
                    "No external gate result was supplied; use --gate-result "
                    f"{gate_id}=<results.json> after running the gate."
                ),
            }
            continue
        payload = _read_optional_json(path)
        if not isinstance(payload, Mapping):
            results[gate_id] = {
                "status": "failed",
                "release_gate_ready": False,
                "path": str(path),
                "detail": "supplied external gate result is missing or invalid JSON",
            }
            continue
        results[gate_id] = _validated_external_gate_result(
            gate_id=gate_id,
            payload=payload,
            path=path,
            validation_time=validation_time,
        )
    return results


def _validated_external_gate_result(
    *,
    gate_id: str,
    payload: Mapping[str, Any],
    path: Path,
    validation_time: str | None,
) -> dict[str, Any]:
    failures: list[str] = []
    requirements = EXTERNAL_GATE_REQUIREMENTS[gate_id]
    schema_version = payload.get("schema_version")
    if schema_version not in requirements["schemas"]:
        failures.append("unsupported or missing schema_version")
    if payload.get("public_safe") is not True:
        failures.append("public_safe must be true")

    status = str(payload.get("status") or "unknown")
    release_gate_passed = payload.get("release_gate_passed")
    release_gate_ready = payload.get("release_gate_ready")
    if release_gate_passed is not True:
        failures.append("release_gate_passed must be explicitly true")
    if release_gate_ready is not None and release_gate_ready is not True:
        failures.append("release_gate_ready contradicts release_gate_passed")
    if status != "passed":
        failures.append(f"status must be passed, got {status}")
    if release_gate_passed is True and status != "passed":
        failures.append("release gate flag contradicts non-passed status")

    outcome_counts = payload.get("outcome_counts")
    if not isinstance(outcome_counts, Mapping):
        failures.append("outcome_counts must be present")
        outcome_counts = {}
    for count_name, minimum in requirements.get("min_counts", {}).items():
        value = _strict_int_count(outcome_counts, count_name)
        if value is None:
            failures.append(f"outcome_counts.{count_name} is missing or non-integer")
        elif value < int(minimum):
            failures.append(
                f"outcome_counts.{count_name}={value} is below required {minimum}"
            )
    for count_name in requirements.get("zero_counts", set()):
        value = _strict_int_count(outcome_counts, count_name)
        if value is None:
            failures.append(f"outcome_counts.{count_name} is missing or non-integer")
        elif value != 0:
            failures.append(f"outcome_counts.{count_name} must be 0, got {value}")

    thresholds = payload.get("thresholds")
    for threshold_name, minimum in requirements.get("thresholds", {}).items():
        if not isinstance(thresholds, Mapping):
            failures.append("thresholds must be present")
            break
        value = _strict_int_count(thresholds, threshold_name)
        if value is None:
            failures.append(f"thresholds.{threshold_name} is missing or non-integer")
        elif value < int(minimum):
            failures.append(
                f"thresholds.{threshold_name}={value} is below required {minimum}"
            )

    freshness = _freshness_check(
        _timestamp_candidates(payload),
        validation_time=validation_time,
    )
    if not freshness["fresh"]:
        failures.extend(freshness["failures"])

    failures.extend(_external_gate_proof_failures(gate_id, payload))

    failed = _strict_int_count(outcome_counts, "failed")
    blocked = _strict_int_count(outcome_counts, "blocked")
    result_status = "passed" if not failures else "failed"
    return {
        "status": result_status,
        "release_gate_ready": result_status == "passed",
        "path": str(Path(path).resolve()),
        "schema_version": schema_version,
        "source_status": status,
        "blocked": blocked,
        "failed": failed,
        "release_gate_passed": release_gate_passed,
        "release_gate_ready_source": release_gate_ready,
        "public_safe": payload.get("public_safe") is True,
        "generated_at": freshness.get("selected_timestamp"),
        "failures": failures,
    }


def _external_gate_proof_failures(
    gate_id: str,
    payload: Mapping[str, Any],
) -> list[str]:
    failures = _common_external_gate_proof_failures(payload)
    if gate_id == "fresh_session_full_runner_artifact_handoff":
        failures.extend(_fresh_session_external_gate_failures(payload))
    elif gate_id == "codex_plugin_interactive_visual_e2e":
        failures.extend(_fresh_session_visual_external_gate_failures(payload))
    elif gate_id in {
        "automated_cli_real_provider_visual_e2e",
        "automatic_web_visual_e2e",
    }:
        failures.extend(_automated_visual_external_gate_failures(payload))
    return failures


def _common_external_gate_proof_failures(payload: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if not isinstance(payload.get("suite_id"), str) or not payload["suite_id"].strip():
        failures.append("suite_id is missing from external gate result")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping) or not isinstance(
        artifacts.get("results"), str
    ) or not artifacts["results"].strip():
        failures.append("artifacts.results is missing from external gate result")
    gate_failures = payload.get("failures")
    if not isinstance(gate_failures, list):
        failures.append("failures must be present as a list")
    elif gate_failures:
        failures.append("failures must be empty when status is passed")
    return failures


def _fresh_session_external_gate_failures(payload: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    failures.extend(
        _acceptance_key_failures(
            payload.get("acceptance"),
            required_keys=_FRESH_SESSION_REQUIRED_ACCEPTANCE,
            context="acceptance",
        )
    )
    skill_gate = payload.get("skill_transcript_gate")
    if not isinstance(skill_gate, Mapping) or skill_gate.get("status") != "passed":
        failures.append("skill_transcript_gate.status must be passed")
    elif not isinstance(skill_gate.get("route_command"), str) or not skill_gate[
        "route_command"
    ].strip():
        failures.append("skill_transcript_gate.route_command is missing")
    runner_gate = payload.get("runner_artifact_gate")
    if not isinstance(runner_gate, Mapping) or runner_gate.get("status") != "passed":
        failures.append("runner_artifact_gate.status must be passed")

    scenarios = _mapping_list(payload.get("scenarios", []))
    if not scenarios:
        failures.append("scenarios must include fresh-session proof records")
        return failures
    real_scenarios = [
        scenario
        for scenario in scenarios
        if scenario.get("terminal_outcome") == "completed_real_parallel"
        and scenario.get("provenance_class") == "real_parallel"
    ]
    if not real_scenarios:
        failures.append(
            "scenarios must include a completed_real_parallel real_parallel proof"
        )
    for scenario in real_scenarios:
        if scenario.get("status") != "passed":
            failures.append("completed_real_parallel scenario.status must be passed")
        validation = scenario.get("validation")
        if not isinstance(validation, Mapping) or validation.get("status") != "passed":
            failures.append(
                "completed_real_parallel scenario.validation.status must be passed"
            )
            continue
        required = {
            str(name)
            for name in validation.get("required_artifacts", [])
            if isinstance(name, str) and name
        }
        artifacts = scenario.get("artifacts")
        if not required:
            failures.append(
                "completed_real_parallel scenario.validation.required_artifacts is missing"
            )
        elif not isinstance(artifacts, Mapping):
            failures.append("completed_real_parallel scenario.artifacts is missing")
        else:
            missing = sorted(name for name in required if name not in artifacts)
            if missing:
                failures.append(
                    "completed_real_parallel scenario.artifacts lacks required "
                    + ", ".join(missing)
                )
    return failures


def _fresh_session_visual_external_gate_failures(payload: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if payload.get("release_gate_status") != "passed":
        failures.append("release_gate_status must be passed")
    failures.extend(
        _acceptance_key_failures(
            payload.get("acceptance"),
            required_keys=_FRESH_SESSION_VISUAL_REQUIRED_ACCEPTANCE,
            context="acceptance",
        )
    )
    skill_gate = payload.get("skill_transcript_gate")
    if not isinstance(skill_gate, Mapping) or skill_gate.get("status") != "passed":
        failures.append("skill_transcript_gate.status must be passed")

    scenarios = _mapping_list(payload.get("scenarios", []))
    release_scenarios = [
        scenario
        for scenario in scenarios
        if isinstance(scenario.get("visual_release_gate"), Mapping)
        and scenario["visual_release_gate"].get("release_gate_passed") is True
    ]
    if not release_scenarios:
        failures.append("scenarios must include a visual_release_gate release pass")
    for scenario in release_scenarios:
        gate = scenario["visual_release_gate"]
        if gate.get("schema_version") != "codex-deepresearch.fresh-session-visual-e2e.v0":
            failures.append("visual_release_gate.schema_version is missing or unsupported")
        if gate.get("status") != "completed_auto_visual":
            failures.append("visual_release_gate.status must be completed_auto_visual")
        if int(gate.get("codex_interactive_analyzed_images") or 0) < 3:
            failures.append(
                "visual_release_gate.codex_interactive_analyzed_images is below 3"
            )
        if int(gate.get("report_cited_visual_or_mixed_claims") or 0) < 1:
            failures.append(
                "visual_release_gate.report_cited_visual_or_mixed_claims is below 1"
            )
        validation = gate.get("visual_artifact_validation")
        if not isinstance(validation, Mapping) or validation.get("valid") is not True:
            failures.append("visual_release_gate.visual_artifact_validation.valid must be true")
        checks = gate.get("checks")
        if not isinstance(checks, Mapping) or not checks:
            failures.append("visual_release_gate.checks must be present")
        else:
            failed_checks = sorted(key for key, value in checks.items() if value is not True)
            if failed_checks:
                failures.append(
                    "visual_release_gate.checks not all true: "
                    + ", ".join(failed_checks)
                )
        required_artifacts = {
            str(name)
            for name in gate.get("required_response_artifacts", [])
            if isinstance(name, str) and name
        }
        expected_artifacts = {
            "run_status",
            "evidence",
            "visual_tasks",
            "visual_observations",
            "visual_provider_status",
            "report",
            "report_status",
            "visual_candidates",
            "image_fetch_status",
        }
        if not expected_artifacts.issubset(required_artifacts):
            missing = sorted(expected_artifacts - required_artifacts)
            failures.append(
                "visual_release_gate.required_response_artifacts lacks "
                + ", ".join(missing)
            )
    return failures


def _automated_visual_external_gate_failures(payload: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    failures.extend(
        _acceptance_key_failures(
            payload.get("acceptance"),
            required_keys=_AUTOMATED_VISUAL_REQUIRED_ACCEPTANCE,
            context="acceptance",
        )
    )
    if payload.get("external_network_call") is not True:
        failures.append("external_network_call must be true")
    if payload.get("external_vlm_call") is not True:
        failures.append("external_vlm_call must be true")
    blockers = payload.get("blockers")
    if not isinstance(blockers, list):
        failures.append("blockers must be present as a list")
    elif blockers:
        failures.append("blockers must be empty when status is passed")

    scenario_prompts = _mapping_list(payload.get("scenario_prompts", []))
    prompt_ids = {str(item.get("id")) for item in scenario_prompts if item.get("id")}
    if prompt_ids != _AUTOMATED_VISUAL_REQUIRED_SCENARIOS:
        failures.append("scenario_prompts must cover the required visual scenario set")

    thresholds = payload.get("thresholds")
    min_image_candidates = (
        _strict_int_count(thresholds, "min_image_candidates")
        if isinstance(thresholds, Mapping)
        else None
    ) or 10
    min_vlm_images = (
        _strict_int_count(thresholds, "min_vlm_images")
        if isinstance(thresholds, Mapping)
        else None
    ) or 3

    scenarios = _mapping_list(payload.get("scenarios", []))
    by_id = {str(item.get("id")): item for item in scenarios if item.get("id")}
    if set(by_id) != _AUTOMATED_VISUAL_REQUIRED_SCENARIOS:
        failures.append("scenarios must cover the required visual scenario set")
    for scenario_id in sorted(_AUTOMATED_VISUAL_REQUIRED_SCENARIOS):
        scenario = by_id.get(scenario_id)
        if scenario is None:
            continue
        failures.extend(
            _automated_visual_scenario_failures(
                scenario_id=scenario_id,
                scenario=scenario,
                min_image_candidates=min_image_candidates,
                min_vlm_images=min_vlm_images,
            )
        )
    return failures


def _automated_visual_scenario_failures(
    *,
    scenario_id: str,
    scenario: Mapping[str, Any],
    min_image_candidates: int,
    min_vlm_images: int,
) -> list[str]:
    prefix = f"scenario {scenario_id}: "
    failures: list[str] = []
    if scenario.get("status") != "passed":
        failures.append(prefix + "status must be passed")
    if scenario.get("run_status") != "completed_auto_visual":
        failures.append(prefix + "run_status must be completed_auto_visual")
    if scenario.get("visual_provider_status") != "completed_auto_visual":
        failures.append(prefix + "visual_provider_status must be completed_auto_visual")
    if scenario.get("ok") is not True or scenario.get("terminal") is not True:
        failures.append(prefix + "ok and terminal must be true")
    if scenario.get("external_network_call") is not True:
        failures.append(prefix + "external_network_call must be true")
    if scenario.get("external_vlm_call") is not True:
        failures.append(prefix + "external_vlm_call must be true")
    validation = scenario.get("visual_artifact_validation")
    if not isinstance(validation, Mapping) or validation.get("valid") is not True:
        failures.append(prefix + "visual_artifact_validation.valid must be true")
    artifacts = scenario.get("artifacts")
    if not isinstance(artifacts, Mapping):
        failures.append(prefix + "artifacts must be present")
    else:
        missing = sorted(_AUTOMATED_VISUAL_REQUIRED_ARTIFACTS - set(artifacts))
        if missing:
            failures.append(prefix + "artifacts lacks " + ", ".join(missing))
    counts = scenario.get("counts")
    release_counts = scenario.get("release_numerator_counts")
    if not isinstance(counts, Mapping):
        failures.append(prefix + "counts must be present")
        counts = {}
    if not isinstance(release_counts, Mapping):
        failures.append(prefix + "release_numerator_counts must be present")
        release_counts = {}
    if scenario_id == "product_image_discovery":
        candidates = _strict_int_count(counts, "scenario_real_candidates")
        if candidates is None or candidates < min_image_candidates:
            failures.append(prefix + "scenario_real_candidates is below threshold")
    vlm = _strict_int_count(counts, "real_openai_responses_vision_observations")
    if vlm is None or vlm < min_vlm_images:
        failures.append(
            prefix + "real_openai_responses_vision_observations is below threshold"
        )
    report_claims = _strict_int_count(counts, "report_cited_visual_or_mixed_claims")
    if report_claims is None or report_claims < 1:
        failures.append(prefix + "report_cited_visual_or_mixed_claims is below 1")
    release_vlm = _strict_int_count(release_counts, "real_vlm_images_analyzed")
    if release_vlm is None or release_vlm < min_vlm_images:
        failures.append(prefix + "release_numerator_counts.real_vlm_images_analyzed is below threshold")
    release_claims = _strict_int_count(
        release_counts,
        "report_cited_visual_or_mixed_claims",
    )
    if release_claims is None or release_claims < 1:
        failures.append(
            prefix
            + "release_numerator_counts.report_cited_visual_or_mixed_claims is below 1"
        )
    return failures


def _acceptance_key_failures(
    acceptance: Any,
    *,
    required_keys: set[str],
    context: str,
) -> list[str]:
    if not isinstance(acceptance, Mapping):
        return [f"{context} must be present"]
    failures = []
    for key in sorted(required_keys):
        if acceptance.get(key) is not True:
            failures.append(f"{context}.{key} must be true")
    return failures


def _acceptance(
    *,
    manifest: Mapping[str, Any],
    evaluated_runs: Sequence[Mapping[str, Any]],
    prompt_metrics: Mapping[str, Mapping[str, Any]],
    summary_path: Path,
) -> dict[str, bool]:
    prompts = manifest.get("prompts", [])
    visual_count = sum(1 for prompt in prompts if prompt.get("route") in VISUAL_ROUTES)
    return {
        "validation_covers_at_least_20_public_safe_prompts": (
            len(prompts) >= 20
            and all(prompt.get("public_safe") is True for prompt in prompts)
        ),
        "visual_prompt_count_at_least_8": visual_count >= 8,
        "each_run_records_run_dir_and_status_artifacts": all(
            bool(run.get("run_dir")) and bool(run.get("status_artifacts"))
            for run in evaluated_runs
        ),
        "each_run_records_provider_provenance": all(
            isinstance(run.get("provider_provenance"), Mapping)
            and bool(run.get("provider_provenance"))
            for run in evaluated_runs
        ),
        "each_non_passing_run_records_failure_category": all(
            run.get("status") == "passed" or run.get("failure_category") in FAILURE_CATEGORIES
            for run in evaluated_runs
        ),
        "all_prompt_runs_are_supplied_sanitized_real_runs": all(
            run.get("status") == "passed"
            and run.get("metric_classification") == "success"
            and isinstance(run.get("provider_provenance"), Mapping)
            and run["provider_provenance"].get("supplied_real_run_directory") is True
            and isinstance(run.get("supplied_run_binding"), Mapping)
            and run["supplied_run_binding"].get("valid") is True
            for run in evaluated_runs
        ),
        "passed_runs_use_codex_plugin_mode": all(
            run.get("status") != "passed"
            or (
                isinstance(run.get("codex_native_handoff_checks"), Mapping)
                and run["codex_native_handoff_checks"].get("execution_mode")
                == "codex-plugin"
            )
            for run in evaluated_runs
        ),
        "passed_runs_have_codex_native_search_handoff": all(
            run.get("status") != "passed"
            or (
                isinstance(run.get("codex_native_handoff_checks"), Mapping)
                and run["codex_native_handoff_checks"].get("valid") is True
                and int(
                    run["codex_native_handoff_checks"].get(
                        "codex_native_search_results",
                        0,
                    )
                    or 0
                )
                > 0
            )
            for run in evaluated_runs
        ),
        "passed_visual_runs_have_codex_interactive_handoff_evidence": all(
            run.get("status") != "passed"
            or run.get("route") not in VISUAL_ROUTES
            or (
                isinstance(run.get("visual_release_checks"), Mapping)
                and run["visual_release_checks"].get("valid") is True
                and int(
                    run["visual_release_checks"].get("counts", {}).get(
                        "real_vlm_observations",
                        0,
                    )
                    or 0
                )
                > 0
            )
            for run in evaluated_runs
        ),
        "blocked_runs_counted_separately_from_failed_non_blocked_runs": all(
            metric.get("blocked", 0) >= 0
            and metric.get("failed_non_blocked", 0) >= 0
            and set(metric.get("blocked_prompt_ids", [])).isdisjoint(
                metric.get("failed_prompt_ids", [])
            )
            for metric in prompt_metrics.values()
        ),
        "summary_artifact_public_safe": summary_path.exists()
        and _summary_is_public_safe(summary_path),
    }


def _remaining_gaps(
    *,
    evaluated_runs: Sequence[Mapping[str, Any]],
    prompt_metrics: Mapping[str, Mapping[str, Any]],
    external_gate_results: Mapping[str, Mapping[str, Any]],
    required_gate_ids: set[str],
    external_gates_required: bool,
    reliability: Mapping[str, Any],
) -> list[str]:
    gaps: list[str] = []
    blocked = [run for run in evaluated_runs if run.get("status") == "blocked"]
    failed = [run for run in evaluated_runs if run.get("status") == "failed"]
    if blocked:
        gaps.append(
            f"{len(blocked)} prompt run(s) are explicit blocked diagnostics and need "
            "sanitized real run directories before release readiness can be claimed."
        )
    if failed:
        gaps.append(
            f"{len(failed)} non-blocked prompt run(s) failed and must be fixed or rerun."
        )
    for metric_id in sorted(required_gate_ids):
        metric = prompt_metrics[metric_id]
        if metric.get("release_gate_ready") is not True:
            gaps.append(
                f"{metric_id} is not release-gate ready: "
                f"passed={metric.get('passed')}, "
                f"failed_non_blocked={metric.get('failed_non_blocked')}, "
                f"blocked={metric.get('blocked')}, "
                f"threshold={metric.get('threshold')}."
            )
    missing_external = [
        gate_id
        for gate_id, result in external_gate_results.items()
        if result.get("status") == "not_supplied"
    ]
    if external_gates_required and missing_external:
        gaps.append(
            "External gate result artifacts were not supplied for: "
            + ", ".join(sorted(missing_external))
            + "."
        )
    failed_external = [
        gate_id
        for gate_id, result in external_gate_results.items()
        if result.get("status") == "failed"
    ]
    if external_gates_required and failed_external:
        details = []
        for gate_id in sorted(failed_external):
            failures = external_gate_results[gate_id].get("failures")
            if isinstance(failures, list) and failures:
                details.append(f"{gate_id}: " + "; ".join(str(item) for item in failures))
            else:
                details.append(gate_id)
        gaps.append(
            "External gate result artifacts failed strict validation for: "
            + " | ".join(details)
            + "."
        )
    partial = reliability.get("partial_parallel")
    if isinstance(partial, Mapping) and partial.get("release_gate_blocking") is True:
        rate = partial.get("partial_parallel_rate")
        rate_label = "unknown" if rate is None else f"{rate:.1%}"
        gaps.append(
            "Product v1 partial-parallel reliability regression: "
            f"rate={rate_label}, "
            f"partial_parallel_runs={partial.get('partial_parallel_runs')}, "
            "completed_real_parallel_stage_runs="
            f"{partial.get('completed_real_parallel_stage_runs')}."
        )
    if not gaps:
        gaps.append("No remaining release-gate gaps were detected.")
    return gaps


def _render_summary(results: Mapping[str, Any]) -> str:
    prompt_coverage = results["prompt_coverage"]
    lines = [
        "# Public Beta Validation Summary",
        "",
        f"Generated: {results['generated_at']}",
        f"Status: {results['status']}",
        f"Release gate ready: {str(results['release_gate_ready']).lower()}",
        f"Issue #75 completion ready: {str(results['issue_75_completion_ready']).lower()}",
        f"Completion mode: {results['completion_mode']}",
        f"Validation mode: {results['validation_mode']}",
        f"Public safe: {str(results['public_safe']).lower()}",
        "Raw run bundles copied: false",
        "",
        "## Prompt Coverage",
        "",
        f"- Total prompts: {prompt_coverage['total_prompts']} "
        f"(required >= {prompt_coverage['required_total_prompts']})",
        f"- Visual-required or visual-optional prompts: "
        f"{prompt_coverage['visual_prompts']} "
        f"(required >= {prompt_coverage['required_visual_prompts']})",
        f"- Public-safe prompts: {prompt_coverage['public_safe_prompts']}",
        "",
        "## Metric Summary",
        "",
        "| Gate | Role | Threshold | Pass rate | Passed | Failed non-blocked | Blocked | Ready |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for metric in results["prompt_metrics"].values():
        pass_rate = metric.get("pass_rate")
        rate = "n/a" if pass_rate is None else f"{pass_rate:.1%}"
        lines.append(
            "| {metric_id} | {role} | {threshold:.0%} | {rate} | {passed} | "
            "{failed} | {blocked} | {ready} |".format(
                metric_id=metric["metric_id"],
                role="required" if metric.get("completion_required") else "diagnostic",
                threshold=metric["threshold"],
                rate=rate,
                passed=metric["passed"],
                failed=metric["failed_non_blocked"],
                blocked=metric["blocked"],
                ready="yes" if metric["release_gate_ready"] else "no",
            )
        )
    lines.extend(
        [
            "",
            "## Outcome Counts",
            "",
            f"- Passed: {results['outcome_counts']['passed']}",
            f"- Failed non-blocked: {results['outcome_counts']['failed']}",
            f"- Blocked: {results['outcome_counts']['blocked']}",
            f"- Excluded: {results['outcome_counts']['excluded']}",
            "",
            "## Product v1 Reliability",
            "",
        ]
    )
    partial = results.get("reliability", {}).get("partial_parallel", {})
    rate = partial.get("partial_parallel_rate")
    rate_label = "n/a" if rate is None else f"{rate:.1%}"
    lines.extend(
        [
            "- Partial parallel rate: "
            f"{rate_label} ({partial.get('partial_parallel_runs', 0)} / "
            f"{partial.get('completed_real_parallel_stage_runs', 0)})",
            "- Formula: partial_parallel_runs / completed_real_parallel_stage_runs",
            "- Bands: target <= 5%; warning > 5% and <= 10%; "
            "regression/failure > 10%",
            f"- Window suites: {', '.join(partial.get('window', {}).get('suite_ids', [])) or '<none>'}",
            f"- Threshold status: {partial.get('threshold_status') or '<unknown>'}; "
            f"enforcement_active={str(partial.get('enforcement_active')).lower()}",
            "",
            "## Remaining Gaps",
            "",
        ]
    )
    lines.extend(f"- {gap}" for gap in results["remaining_gaps"])
    lines.extend(
        [
            "",
            "## Prompt Results",
            "",
            "| Prompt ID | Route | Status | Metric class | Failure category | Run dir |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for run in results["runs"]:
        lines.append(
            "| {id} | {route} | {status} | {metric_classification} | "
            "{failure_category} | {run_ref} |".format(
                id=run["id"],
                route=run["route"],
                status=run["status"],
                metric_classification=run["metric_classification"],
                failure_category=run.get("failure_category") or "",
                run_ref=run.get("public_run_ref") or f"prompt:{run['id']}",
            )
        )
    lines.extend(
        [
            "",
            "This summary contains curated prompt IDs, sanitized status paths, "
            "provider provenance summaries, and release-readiness counts only. It "
            "does not copy raw evidence bundles, credentials, non-public screenshots, "
            "or personal data.",
            "",
        ]
    )
    return "\n".join(lines)


def _summary_is_public_safe(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8").lower()
    forbidden = (
        "api" + "_key",
        "api" + "key",
        "authorization" + ":",
        "bearer ",
        "private" + " screenshot",
        "." + "env",
        "personal data" + " bundle",
        "file://",
    )
    if any(token in text for token in forbidden):
        return False
    private_path_patterns = (
        r"(?<![a-z0-9_./-])/home/[^\s|)]+",
        r"(?<![a-z0-9_./-])/users/[^\s|)]+",
        r"[a-z]:\\users\\[^\s|)]+",
    )
    if any(re.search(pattern, text) for pattern in private_path_patterns):
        return False
    username = Path.home().name.lower()
    if username and re.search(rf"/(?:home|users)/{re.escape(username)}(?:/|\b)", text):
        return False
    return True


def _artifact_paths(run_dir: Path, *, route: str) -> dict[str, Path]:
    artifacts = {
        "run_status": run_dir / "run_status.json",
        "evidence": run_dir / "evidence.json",
        "search_tasks": run_dir / "search_tasks.json",
        "search_results": run_dir / "search_results.jsonl",
        "report": run_dir / "report.md",
        "report_status": run_dir / "report_status.json",
        "parallel_status": run_dir / "parallel_orchestration_status.json",
        "run_trace": run_dir / "run_trace.jsonl",
        "semantic_expectation_oracle": run_dir / "semantic_expectation_oracle.json",
        "semantic_plan": run_dir / "semantic_plan.json",
        "semantic_plan_review": run_dir / "semantic_plan_review.json",
        "semantic_planner_validation": run_dir / "semantic_planner_validation.json",
    }
    if route in VISUAL_ROUTES:
        artifacts.update(
            {
                "visual_provider_status": run_dir / VISUAL_PROVIDER_STATUS_FILENAME,
                "visual_search_plan": run_dir / "visual_search_plan.json",
                "visual_candidates": run_dir / "visual_candidates.jsonl",
                "image_fetch_status": run_dir / "image_fetch_status.jsonl",
                "visual_observations": run_dir / "visual_observations.jsonl",
                "verifier_votes": run_dir / "verifier_votes.jsonl",
            }
        )
    return artifacts


def _required_status_artifacts(
    prompt: Mapping[str, Any],
    terminal_status: str,
) -> list[str]:
    required = ["run_status"]
    if terminal_status in PASS_TERMINAL_STATUSES:
        required.extend(["evidence", "search_tasks", "search_results", "report", "report_status"])
    if _requires_semantic_release_gate(prompt, terminal_status):
        required.extend(SEMANTIC_RELEASE_REQUIRED_ARTIFACTS)
    if prompt.get("route") in VISUAL_ROUTES:
        required.append("visual_provider_status")
    if terminal_status == "completed_auto_visual":
        required.extend(
            [
                "visual_search_plan",
                "visual_candidates",
                "image_fetch_status",
                "visual_observations",
                "verifier_votes",
            ]
        )
    return required


def _requires_semantic_release_gate(
    prompt: Mapping[str, Any],
    terminal_status: str,
) -> bool:
    if prompt.get("route") in VISUAL_ROUTES:
        return terminal_status in (
            SEMANTIC_RELEASE_FAILURE_TERMINAL_STATUSES | {"completed_auto_visual"}
        )
    return terminal_status in (
        SEMANTIC_RELEASE_TERMINAL_STATUSES
        | SEMANTIC_RELEASE_FAILURE_TERMINAL_STATUSES
    )


def _semantic_release_checks(
    *,
    loaded_artifacts: Mapping[str, Any],
    run_status: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    required_payloads: dict[str, Mapping[str, Any]] = {}
    for name in SEMANTIC_RELEASE_REQUIRED_ARTIFACTS:
        payload = loaded_artifacts.get(name)
        if isinstance(payload, Mapping):
            required_payloads[name] = payload
        else:
            failures.append(
                {
                    "check": "required_semantic_artifact_present",
                    "artifact": name,
                    "detail": f"{name}.json is missing or invalid",
                }
            )

    planner_sources = _semantic_planner_mode_sources(
        run_status=run_status,
        evidence=evidence,
        artifacts=required_payloads,
    )
    missing_planner_sources = [
        source
        for source in SEMANTIC_RELEASE_REQUIRED_FIELD_SOURCES
        if source not in planner_sources
    ]
    if missing_planner_sources:
        failures.append(
            {
                "check": "semantic_planner_mode",
                "missing_sources": missing_planner_sources,
                "detail": "planner_mode is missing from semantic release artifacts",
            }
        )
    for source, planner_mode in sorted(planner_sources.items()):
        if planner_mode != "codex_semantic":
            failures.append(
                {
                    "check": "semantic_planner_mode",
                    "source": source,
                    "planner_mode": planner_mode,
                    "detail": f"{source}.planner_mode is {planner_mode}, expected codex_semantic",
                }
            )

    eligible_sources = _semantic_release_eligible_sources(
        run_status=run_status,
        evidence=evidence,
        artifacts=required_payloads,
    )
    missing_eligible_sources = [
        source
        for source in SEMANTIC_RELEASE_REQUIRED_FIELD_SOURCES
        if source not in eligible_sources
    ]
    if missing_eligible_sources:
        failures.append(
            {
                "check": "semantic_release_eligible",
                "missing_sources": missing_eligible_sources,
                "detail": "semantic_release_eligible is missing from semantic release artifacts",
            }
        )
    for source, eligible in sorted(eligible_sources.items()):
        if eligible is not True:
            failures.append(
                {
                    "check": "semantic_release_eligible",
                    "source": source,
                    "semantic_release_eligible": eligible,
                    "detail": f"{source}.semantic_release_eligible is not true",
                }
            )

    codex_source_sources = _semantic_codex_source_sources(
        evidence=evidence,
        artifacts=required_payloads,
    )
    missing_codex_source_sources = [
        source
        for source in SEMANTIC_RELEASE_REQUIRED_CODEX_SOURCE_SOURCES
        if source not in codex_source_sources
    ]
    if missing_codex_source_sources:
        failures.append(
            {
                "check": "semantic_codex_source",
                "missing_sources": missing_codex_source_sources,
                "detail": "codex_semantic source provenance is missing from semantic release artifacts",
            }
        )
    for source, codex_source in sorted(codex_source_sources.items()):
        if codex_source != "codex_semantic":
            failures.append(
                {
                    "check": "semantic_codex_source",
                    "source": source,
                    "semantic_source": codex_source,
                    "detail": f"{source}.source is {codex_source}, expected codex_semantic",
                }
            )

    validation = required_payloads.get("semantic_planner_validation")
    if not isinstance(validation, Mapping) or validation.get("ok") is not True:
        failures.append(
            {
                "check": "semantic_planner_validation_ok",
                "detail": "semantic_planner_validation.ok is not true",
            }
        )

    review = required_payloads.get("semantic_plan_review")
    if isinstance(review, Mapping):
        score = _numeric_or_none(review.get("semantic_fit_score"))
        if score is None:
            failures.append(
                {
                    "check": "semantic_fit_score",
                    "semantic_fit_score": review.get("semantic_fit_score"),
                    "detail": "semantic_plan_review.semantic_fit_score is missing or non-numeric",
                }
            )
        elif score < 9.0:
            failures.append(
                {
                    "check": "semantic_fit_score",
                    "semantic_fit_score": score,
                    "detail": "semantic_plan_review.semantic_fit_score is below 9.0",
                }
            )
        blockers = review.get("blockers")
        if not isinstance(blockers, list):
            failures.append(
                {
                    "check": "semantic_review_blockers",
                    "detail": "semantic_plan_review.blockers must be a list",
                }
            )
        elif blockers:
            failures.append(
                {
                    "check": "semantic_review_blockers",
                    "blocker_count": len(blockers),
                    "detail": "semantic_plan_review.blockers is not empty",
                }
            )
        substitute = review.get("substitute_implementation_check")
        if not isinstance(substitute, Mapping) or substitute.get("passed") is not True:
            failures.append(
                {
                    "check": "substitute_implementation_check",
                    "detail": "semantic_plan_review.substitute_implementation_check.passed is not true",
                }
            )
        independence = review.get("reviewer_independence")
        if not isinstance(independence, Mapping) or independence.get("independent") is not True:
            failures.append(
                {
                    "check": "reviewer_independence",
                    "detail": "semantic_plan_review.reviewer_independence.independent is not true",
                }
            )

    if _claims_eligible_codex_semantic(
        planner_sources=planner_sources,
        eligible_sources=eligible_sources,
    ):
        failures.extend(_semantic_artifact_integrity_failures(required_payloads))

    return {
        "schema_version": "codex-deepresearch.semantic-release-checks.v0",
        "valid": not failures,
        "required_artifacts": list(SEMANTIC_RELEASE_REQUIRED_ARTIFACTS),
        "present_artifacts": sorted(required_payloads),
        "planner_modes": planner_sources,
        "semantic_release_eligible": eligible_sources,
        "semantic_codex_sources": codex_source_sources,
        "validation_ok": validation.get("ok") if isinstance(validation, Mapping) else None,
        "semantic_fit_score": review.get("semantic_fit_score") if isinstance(review, Mapping) else None,
        "failures": failures,
    }


def _claims_eligible_codex_semantic(
    *,
    planner_sources: Mapping[str, str],
    eligible_sources: Mapping[str, Any],
) -> bool:
    return (
        any(
            planner_mode == "codex_semantic"
            for planner_mode in planner_sources.values()
        )
        and any(eligible is True for eligible in eligible_sources.values())
    )


def _semantic_artifact_integrity_failures(
    artifacts: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for artifact_name in SEMANTIC_RELEASE_REQUIRED_ARTIFACTS:
        payload = artifacts.get(artifact_name)
        if not isinstance(payload, Mapping):
            continue
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name=artifact_name,
            field="question_scope",
            invalid=not _valid_semantic_question_scope(payload.get("question_scope")),
        )
        for field in ("raw_request_path", "raw_response_path"):
            _append_semantic_artifact_failure_if(
                failures,
                artifact_name=artifact_name,
                field=field,
                invalid=not _non_empty_string(payload.get(field)),
            )
        for field in ("raw_request_hash", "raw_response_hash"):
            _append_semantic_artifact_failure_if(
                failures,
                artifact_name=artifact_name,
                field=field,
                invalid=not _sha256_hex_string(payload.get(field)),
            )
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name=artifact_name,
            field="provenance",
            invalid=not _valid_semantic_provenance(payload.get("provenance")),
        )
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name=artifact_name,
            field="template_use",
            invalid=not _valid_semantic_template_use(payload.get("template_use")),
        )
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name=artifact_name,
            field="session_id_unavailable_reason",
            invalid=not _non_empty_string(payload.get("session_id_unavailable_reason")),
        )

    oracle = artifacts.get("semantic_expectation_oracle")
    if isinstance(oracle, Mapping):
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name="semantic_expectation_oracle",
            field="oracle_requirement_map",
            invalid=not _valid_oracle_requirement_map(
                oracle.get("oracle_requirement_map")
            ),
        )

    plan = artifacts.get("semantic_plan")
    if isinstance(plan, Mapping):
        semantic_plan = plan.get("semantic_plan")
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name="semantic_plan",
            field="semantic_plan",
            invalid=not isinstance(semantic_plan, Mapping) or not semantic_plan,
        )
        if isinstance(semantic_plan, Mapping):
            _append_semantic_artifact_failure_if(
                failures,
                artifact_name="semantic_plan",
                field="semantic_plan.angles",
                invalid=not _valid_semantic_angles(semantic_plan.get("angles")),
            )
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name="semantic_plan",
            field="angles",
            invalid=not _valid_semantic_angles(plan.get("angles")),
        )
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name="semantic_plan",
            field="requirement_coverage_map",
            invalid=not _valid_requirement_coverage_map(
                plan.get("requirement_coverage_map")
            ),
        )

    return failures


def _append_semantic_artifact_failure_if(
    failures: list[dict[str, Any]],
    *,
    artifact_name: str,
    field: str,
    invalid: bool,
) -> None:
    if not invalid:
        return
    failures.append(
        {
            "check": "semantic_artifact_integrity",
            "artifact": artifact_name,
            "field": field,
            "detail": f"{artifact_name}.{field} is missing or shallow",
        }
    )


def _valid_semantic_question_scope(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    angle_count = value.get("angle_count")
    return (
        _non_empty_string(value.get("original_question"))
        and _sha256_hex_string(value.get("question_hash"))
        and _non_empty_string(value.get("question_class"))
        and _non_empty_string(value.get("planner_mode"))
        and isinstance(angle_count, int)
        and not isinstance(angle_count, bool)
        and angle_count > 0
    )


def _valid_semantic_provenance(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and _non_empty_string(value.get("planner_mode"))
        and _non_empty_string(value.get("planner_source"))
        and value.get("raw_request_required") is True
        and value.get("raw_response_required") is True
        and _non_empty_string(value.get("session_id_unavailable_reason"))
        and isinstance(value.get("semantic_release_eligible"), bool)
    )


def _valid_semantic_template_use(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and isinstance(value.get("uses_preselected_template"), bool)
        and isinstance(value.get("template_release_eligible"), bool)
        and isinstance(value.get("template_angle_titles"), list)
    )


def _valid_oracle_requirement_map(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_valid_oracle_requirement(requirement) for requirement in value)
    )


def _valid_oracle_requirement(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        _non_empty_string(value.get("requirement_id"))
        and (
            _non_empty_string(value.get("text"))
            or _non_empty_string(value.get("description"))
        )
        and _non_empty_string_list(value.get("covered_by_angle_ids"))
    )


def _valid_semantic_angles(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_valid_semantic_angle(angle) for angle in value)
    )


def _valid_semantic_angle(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return _non_empty_string(value.get("angle_id")) and (
        _non_empty_string(value.get("title"))
        or _non_empty_string(value.get("research_question"))
    )


def _valid_requirement_coverage_map(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_valid_requirement_coverage(coverage) for coverage in value)
    )


def _valid_requirement_coverage(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        _non_empty_string(value.get("requirement_id"))
        and _non_empty_string(value.get("angle_id"))
        and _non_empty_string(value.get("coverage_status"))
    )


def _non_empty_string_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_non_empty_string(item) for item in value)
    )


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sha256_hex_string(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-fA-F]{64}", value.strip()) is not None
    )


def _semantic_planner_mode_sources(
    *,
    run_status: Mapping[str, Any],
    evidence: Mapping[str, Any],
    artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    sources: dict[str, str] = {}
    _add_string_source(sources, "run_status", run_status.get("planner_mode"))
    semantic_planner = evidence.get("semantic_planner")
    if isinstance(semantic_planner, Mapping):
        _add_string_source(
            sources,
            "evidence.semantic_planner",
            semantic_planner.get("planner_mode"),
        )
    for name, payload in artifacts.items():
        _add_string_source(sources, name, payload.get("planner_mode"))
        nested = payload.get("semantic_plan")
        if isinstance(nested, Mapping):
            _add_string_source(
                sources,
                f"{name}.semantic_plan",
                nested.get("planner_mode"),
            )
    return sources


def _semantic_release_eligible_sources(
    *,
    run_status: Mapping[str, Any],
    evidence: Mapping[str, Any],
    artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    sources: dict[str, Any] = {}
    _add_bool_source(sources, "run_status", run_status.get("semantic_release_eligible"))
    semantic_planner = evidence.get("semantic_planner")
    if isinstance(semantic_planner, Mapping):
        _add_bool_source(
            sources,
            "evidence.semantic_planner",
            semantic_planner.get("semantic_release_eligible"),
        )
    for name, payload in artifacts.items():
        _add_bool_source(sources, name, payload.get("semantic_release_eligible"))
        nested = payload.get("semantic_plan")
        if isinstance(nested, Mapping):
            _add_bool_source(
                sources,
                f"{name}.semantic_plan",
                nested.get("semantic_release_eligible"),
            )
    return sources


def _semantic_codex_source_sources(
    *,
    evidence: Mapping[str, Any],
    artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    sources: dict[str, str] = {}
    semantic_planner = evidence.get("semantic_planner")
    if isinstance(semantic_planner, Mapping):
        _add_string_source(
            sources,
            "evidence.semantic_planner",
            semantic_planner.get("source"),
        )
    semantic_plan = artifacts.get("semantic_plan")
    if isinstance(semantic_plan, Mapping):
        nested = semantic_plan.get("semantic_plan")
        if isinstance(nested, Mapping):
            _add_string_source(
                sources,
                "semantic_plan.semantic_plan",
                nested.get("source"),
            )
    return sources


def _add_string_source(sources: dict[str, str], source: str, value: Any) -> None:
    if isinstance(value, str) and value.strip():
        sources[source] = value.strip()


def _add_bool_source(sources: dict[str, Any], source: str, value: Any) -> None:
    if isinstance(value, bool):
        sources[source] = value


def _numeric_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    return None


def _metric_classification(
    prompt: Mapping[str, Any],
    terminal_status: str,
    run_status: Mapping[str, Any],
) -> tuple[str, str]:
    ok = run_status.get("ok") is True
    terminal = run_status.get("terminal") is True
    route = prompt.get("route")
    if (
        _requires_semantic_release_gate(prompt, terminal_status)
        and terminal_status in SEMANTIC_RELEASE_FAILURE_TERMINAL_STATUSES
    ):
        return "included_failure", "failed"
    if route in VISUAL_ROUTES and terminal_status in PASS_TERMINAL_STATUSES:
        if terminal_status == "completed_auto_visual" and ok and terminal:
            return "success", "passed"
        return "included_failure", "failed"
    if terminal_status in PASS_TERMINAL_STATUSES and ok and terminal:
        return "success", "passed"
    if terminal_status in BLOCKED_TERMINAL_STATUSES:
        return "excluded_blocked", "blocked"
    if terminal_status in EXCLUDED_FIXTURE_STATUSES:
        return "excluded_fixture", "excluded"
    if terminal_status in INCLUDED_FAILURE_STATUSES or terminal_status.startswith("failed"):
        return "included_failure", "failed"
    return "included_failure", "failed"


def _failure_category(
    terminal_status: str,
    *,
    missing_artifacts: Sequence[str] | None = None,
    visual_provider_status: Mapping[str, Any] | None = None,
    report_status: Mapping[str, Any] | None = None,
) -> str:
    missing_artifacts = list(missing_artifacts or [])
    if missing_artifacts:
        if any(name in {"report", "report_status"} for name in missing_artifacts):
            return "report_linkage_failure"
        return "artifact_handoff_failure"
    if terminal_status in {
        "blocked_semantic_planner_unavailable",
        "blocked_missing_visual_provider",
        "blocked_preflight",
        "blocked_parallel_execution",
        "budget_pruned_visual",
    }:
        return "provider_failure"
    if terminal_status == "blocked_missing_search_handoff":
        return "artifact_handoff_failure"
    if terminal_status == "completed_manual_planner_fallback":
        return "artifact_handoff_failure"
    if terminal_status == "blocked_missing_vlm_provider":
        return "vlm_failure"
    if terminal_status in {"policy_blocked_visual", "policy_blocked", "needs_manual_review"}:
        return "policy_block"
    if terminal_status == "failed_synthesis":
        return "synthesis_shape_failure"
    if terminal_status == "failed_validation":
        return "artifact_handoff_failure"
    if terminal_status == "failed_parallel_no_accepted_shards":
        return "artifact_handoff_failure"
    if terminal_status == "failed_release_handoff_invalid":
        return "artifact_handoff_failure"
    if terminal_status == "partial_auto_visual":
        if _provider_has_vlm_gap(visual_provider_status or {}):
            return "vlm_failure"
        if not _report_status_uses_images(report_status or {}):
            return "report_linkage_failure"
        return "visual_contradiction"
    return "provider_failure"


def _failure_detail(
    *,
    status: str,
    terminal_status: str,
    missing_artifacts: Sequence[str],
) -> str | None:
    if status == "passed":
        return None
    if terminal_status == "completed_manual_planner_fallback":
        return (
            "manual planner fallback completed a useful run but cannot satisfy "
            "semantic planner release metrics"
        )
    if terminal_status == "blocked_semantic_planner_unavailable":
        return (
            "Codex-native semantic planner was unavailable; this release-counted "
            "run remains a semantic planner denominator failure"
        )
    if terminal_status == "completed_fixture":
        return (
            "fixture-only completion cannot satisfy semantic planner release metrics"
        )
    if missing_artifacts:
        return "missing required status artifact(s): " + ", ".join(missing_artifacts)
    if status == "excluded":
        return (
            f"{terminal_status} is valid for fixture validation but excluded from "
            "real-use Public Beta release metrics"
        )
    return f"terminal status {terminal_status} did not pass the Public Beta metric gate"


def _supplied_run_binding(
    *,
    prompt: Mapping[str, Any],
    run_dir: Path,
    suite_id: str,
    loaded_artifacts: Mapping[str, Any],
    required_artifacts: Sequence[str],
    run_status: Mapping[str, Any],
    evidence: Mapping[str, Any],
    validation_time: str | None,
) -> dict[str, Any]:
    failures: list[str] = []
    prompt_id = str(prompt.get("id"))
    expected_prompt = str(prompt.get("prompt") or "")
    expected_hash = _prompt_hash(expected_prompt)

    if run_status.get("schema_version") != RUN_STATUS_SCHEMA_VERSION:
        failures.append("run_status.schema_version is missing or unsupported")

    identity_artifacts = _identity_artifact_payloads(
        loaded_artifacts=loaded_artifacts,
        required_artifacts=required_artifacts,
    )
    invalid_identity_artifacts = _invalid_identity_artifacts(
        loaded_artifacts=loaded_artifacts,
        required_artifacts=required_artifacts,
    )
    if invalid_identity_artifacts:
        failures.append(
            "required status artifact(s) are missing or invalid JSON: "
            + ", ".join(invalid_identity_artifacts)
        )

    prompt_ids = _string_field_values(
        *identity_artifacts.values(),
        names=("prompt_id", "public_beta_prompt_id"),
    )
    prompt_id_sources = _artifact_field_sources(
        identity_artifacts,
        names=("prompt_id", "public_beta_prompt_id"),
    )
    if not _artifact_has_any_field(
        "run_status",
        identity_artifacts,
        names=("prompt_id", "public_beta_prompt_id"),
    ):
        failures.append("prompt_id is missing from run_status metadata")
    missing_prompt_id = _identity_artifacts_missing_field(
        identity_artifacts,
        names=("prompt_id", "public_beta_prompt_id"),
    )
    if missing_prompt_id:
        failures.append(
            "prompt_id is missing from required identity artifact(s): "
            + ", ".join(missing_prompt_id)
        )
    elif any(value != prompt_id for value in prompt_ids):
        failures.append(
            "prompt_id does not match manifest prompt "
            f"{prompt_id}: {_format_artifact_sources(prompt_id_sources)}"
        )

    prompt_hashes = _string_field_values(
        *identity_artifacts.values(),
        names=("prompt_hash", "public_beta_prompt_hash"),
    )
    questions = _string_field_values(
        *identity_artifacts.values(),
        names=("prompt", "question", "original_question"),
    )
    if prompt_hashes:
        if any(value != expected_hash for value in prompt_hashes):
            failures.append("prompt_hash does not match manifest prompt text")
    elif not any(
        _normalize_prompt_text(value) == _normalize_prompt_text(expected_prompt)
        for value in questions
    ):
        failures.append("original question or prompt_hash does not match manifest prompt")

    suite_ids = _string_field_values(
        *identity_artifacts.values(),
        names=("suite_id", "validation_suite_id", "public_beta_suite_id"),
    )
    suite_id_sources = _artifact_field_sources(
        identity_artifacts,
        names=("suite_id", "validation_suite_id", "public_beta_suite_id"),
    )
    if not _artifact_has_any_field(
        "run_status",
        identity_artifacts,
        names=("suite_id", "validation_suite_id", "public_beta_suite_id"),
    ):
        failures.append("suite_id is missing from run_status metadata")
    missing_suite_id = _identity_artifacts_missing_field(
        identity_artifacts,
        names=("suite_id", "validation_suite_id", "public_beta_suite_id"),
    )
    if missing_suite_id:
        failures.append(
            "suite_id is missing from required identity artifact(s): "
            + ", ".join(missing_suite_id)
        )
    elif any(value != suite_id for value in suite_ids):
        failures.append(
            "suite_id does not match validation suite "
            f"{suite_id}: {_format_artifact_sources(suite_id_sources)}"
        )

    execution_modes = _string_field_values(
        *identity_artifacts.values(),
        names=("execution_mode",),
    )
    execution_mode_sources = _artifact_field_sources(
        identity_artifacts,
        names=("execution_mode",),
    )
    missing_execution_mode = sorted(
        set(identity_artifacts) - set(execution_mode_sources)
    )
    if missing_execution_mode:
        failures.append(
            "execution_mode is missing from required identity artifact(s): "
            + ", ".join(missing_execution_mode)
        )
    elif any(value != "codex-plugin" for value in execution_modes):
        failures.append(
            "execution_mode must be codex-plugin for supplied release runs: "
            + _format_artifact_sources(execution_mode_sources)
        )

    runner_modes = _string_field_values(
        *identity_artifacts.values(),
        names=("runner_mode",),
    )
    runner_mode_sources = _artifact_field_sources(
        identity_artifacts,
        names=("runner_mode",),
    )
    missing_runner_mode = sorted(set(identity_artifacts) - set(runner_mode_sources))
    if missing_runner_mode:
        failures.append(
            "runner_mode is missing from required identity artifact(s): "
            + ", ".join(missing_runner_mode)
        )
    elif any(value != "full-runner" for value in runner_modes):
        failures.append(
            "runner_mode must be full-runner for supplied release runs: "
            + _format_artifact_sources(runner_mode_sources)
        )

    freshness = _freshness_check_by_artifact(
        identity_artifacts,
        validation_time=validation_time,
    )
    if not freshness["fresh"]:
        failures.extend(freshness["failures"])

    run_id_sources = _artifact_field_sources(identity_artifacts, names=("run_id",))
    missing_run_id = sorted(set(identity_artifacts) - set(run_id_sources))
    if missing_run_id:
        failures.append(
            "run_id is missing from required artifact(s): "
            + ", ".join(missing_run_id)
        )
    elif len(set(run_id_sources.values())) > 1:
        failures.append(
            "run_id values disagree across required status artifacts: "
            + _format_artifact_sources(run_id_sources)
        )

    return {
        "valid": not failures,
        "prompt_id": prompt_id,
        "suite_id": suite_ids[0] if suite_ids else None,
        "prompt_hash": expected_hash,
        "execution_mode": execution_modes[0] if execution_modes else None,
        "runner_mode": runner_modes[0] if runner_modes else None,
        "created_at": freshness.get("selected_timestamp"),
        "max_age_days": SUPPLIED_RUN_MAX_AGE_DAYS,
        "bound_artifacts": sorted(identity_artifacts),
        "failures": failures,
    }


def _identity_artifact_payloads(
    *,
    loaded_artifacts: Mapping[str, Any],
    required_artifacts: Sequence[str],
) -> dict[str, Mapping[str, Any]]:
    identity_names = {
        "run_status",
        "evidence",
        "report_status",
        "visual_provider_status",
        "visual_search_plan",
    }
    payloads: dict[str, Mapping[str, Any]] = {}
    for name in required_artifacts:
        if name not in identity_names:
            continue
        payload = loaded_artifacts.get(name)
        if isinstance(payload, Mapping):
            payloads[name] = payload
    for name in sorted(identity_names):
        payload = loaded_artifacts.get(name)
        if isinstance(payload, Mapping):
            payloads.setdefault(name, payload)
    return payloads


def _invalid_identity_artifacts(
    *,
    loaded_artifacts: Mapping[str, Any],
    required_artifacts: Sequence[str],
) -> list[str]:
    identity_names = {
        "run_status",
        "evidence",
        "report_status",
        "visual_provider_status",
        "visual_search_plan",
    }
    return [
        name
        for name in required_artifacts
        if name in identity_names and not isinstance(loaded_artifacts.get(name), Mapping)
    ]


def _artifact_has_any_field(
    artifact_name: str,
    payloads: Mapping[str, Mapping[str, Any]],
    *,
    names: Sequence[str],
) -> bool:
    payload = payloads.get(artifact_name)
    if not isinstance(payload, Mapping):
        return False
    return any(
        isinstance(payload.get(name), str) and bool(payload.get(name).strip())
        for name in names
    )


def _identity_artifacts_missing_field(
    payloads: Mapping[str, Mapping[str, Any]],
    *,
    names: Sequence[str],
) -> list[str]:
    missing = []
    for artifact_name in sorted(payloads):
        if not _artifact_has_any_field(artifact_name, payloads, names=names):
            missing.append(artifact_name)
    return missing


def _artifact_field_sources(
    payloads: Mapping[str, Mapping[str, Any]],
    *,
    names: Sequence[str],
) -> dict[str, str]:
    sources: dict[str, str] = {}
    for artifact_name, payload in payloads.items():
        for name in names:
            value = payload.get(name)
            if isinstance(value, str) and value.strip():
                sources[artifact_name] = value.strip()
                break
    return sources


def _format_artifact_sources(sources: Mapping[str, str]) -> str:
    return ", ".join(
        f"{artifact}={value}" for artifact, value in sorted(sources.items())
    )


def _freshness_check_by_artifact(
    payloads: Mapping[str, Mapping[str, Any]],
    *,
    validation_time: str | None,
) -> dict[str, Any]:
    failures: list[str] = []
    selected: dict[str, str] = {}
    for artifact_name, payload in payloads.items():
        freshness = _freshness_check(
            _timestamp_candidates(payload),
            validation_time=validation_time,
        )
        if freshness["fresh"]:
            selected_timestamp = freshness.get("selected_timestamp")
            if isinstance(selected_timestamp, str):
                selected[artifact_name] = selected_timestamp
            continue
        for failure in freshness["failures"]:
            failures.append(f"{artifact_name}: {failure}")
    selected_timestamp = max(selected.values()) if selected else None
    return {
        "fresh": not failures,
        "selected_timestamp": selected_timestamp,
        "selected_timestamps": selected,
        "failures": failures,
    }


def _status_consistency_failures(
    *,
    prompt: Mapping[str, Any],
    run_status: Mapping[str, Any],
    visual_provider_status: Mapping[str, Any],
    report_status: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    terminal_status = _terminal_status(run_status)
    if terminal_status in PASS_TERMINAL_STATUSES:
        if run_status.get("terminal") is not True:
            failures.append("run_status.terminal must be true for completed runs")
        if run_status.get("ok") is not True:
            failures.append("run_status.ok must be true for completed runs")
    if report_status:
        schema = report_status.get("schema_version")
        if schema not in REPORT_STATUS_SCHEMA_VERSIONS:
            failures.append("report_status.schema_version is missing or unsupported")
        report_terminal = str(report_status.get("status") or "")
        if report_terminal and report_terminal not in {"completed", "passed"}:
            failures.append(f"report_status.status is not completed: {report_terminal}")
    if prompt.get("route") in VISUAL_ROUTES:
        if visual_provider_status.get("schema_version") != "codex-deepresearch.visual-provider-status.v0":
            failures.append("visual_provider_status.schema_version is missing or unsupported")
        provider_terminal = _terminal_status(visual_provider_status)
        if terminal_status in PASS_TERMINAL_STATUSES and provider_terminal != terminal_status:
            failures.append(
                "run_status.status and visual_provider_status.status disagree"
            )
        if provider_terminal == "completed_auto_visual":
            if visual_provider_status.get("terminal") is not True:
                failures.append("visual_provider_status.terminal must be true")
            if visual_provider_status.get("ok") is not True:
                failures.append("visual_provider_status.ok must be true")
    return failures


def _codex_native_handoff_checks(
    *,
    prompt: Mapping[str, Any],
    suite_id: str,
    run_path: Path,
    artifacts: Mapping[str, Path],
    run_status: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    failures: list[str] = []
    execution_mode = _execution_mode(run_status, evidence)
    runner_mode = _runner_mode(run_status, evidence)
    selected_mode = _selected_mode(run_status, evidence)
    if execution_mode != "codex-plugin":
        failures.append(
            "execution_mode must be codex-plugin for Codex-native completion"
        )
    if runner_mode != "full-runner":
        failures.append(
            "runner_mode must be full-runner for Codex-native completion"
        )
    for payload_name, payload in (("run_status", run_status), ("evidence", evidence)):
        hidden_api = payload.get("hidden_codex_api_call")
        if hidden_api is True or payload.get("codex_native_api_call") is True:
            failures.append(
                f"{payload_name} claims a hidden Codex-native API call; "
                "Codex-native runs must use handoff artifacts"
            )

    search_tasks_path = artifacts.get("search_tasks", run_path / "search_tasks.json")
    search_results_path = artifacts.get("search_results", run_path / "search_results.jsonl")
    search_tasks = _read_optional_json(search_tasks_path)
    search_results = _mapping_list(_read_optional_jsonl(search_results_path))
    if not isinstance(search_tasks, (Mapping, list)):
        failures.append("search_tasks.json is missing or invalid")
    codex_native_results = [
        record for record in search_results if _is_codex_native_search_result(record)
    ]
    invalid_codex_api_records = [
        record
        for record in search_results
        if _claims_hidden_codex_api(record)
    ]
    if invalid_codex_api_records:
        failures.append(
            "search_results.jsonl contains hidden Codex-native API markers"
        )
    expected_hash = public_beta_prompt_hash(str(prompt.get("prompt") or ""))
    matching_codex_native_results = [
        record
        for record in codex_native_results
        if record.get("prompt_id") == prompt.get("id")
        and record.get("suite_id") == suite_id
        and record.get("prompt_hash") == expected_hash
    ]
    if not matching_codex_native_results:
        failures.append(
            "search_results.jsonl lacks allowed Codex-native search handoff results "
            "with matching prompt_id, suite_id, and prompt_hash"
        )
    if _has_non_release_handoff_records(search_results):
        failures.append(
            "search_results.jsonl contains fixture, manual, user-provided-only, or post-hoc records"
        )
    return {
        "valid": not failures,
        "execution_mode": execution_mode,
        "runner_mode": runner_mode,
        "selected_mode": selected_mode,
        "codex_native_search_results": len(codex_native_results),
        "matching_codex_native_search_results": len(matching_codex_native_results),
        "search_results": len(search_results),
        "failures": failures,
    }


def _visual_release_checks(
    *,
    run_path: Path,
    prompt: Mapping[str, Any],
    run_status: Mapping[str, Any],
    visual_provider_status: Mapping[str, Any],
    evidence: Mapping[str, Any],
    report_status: Mapping[str, Any],
    report_text: str,
) -> dict[str, Any]:
    artifacts = _artifact_paths(run_path, route=str(prompt.get("route") or ""))
    candidates = _mapping_list(_read_optional_jsonl(artifacts["visual_candidates"]))
    fetches = _mapping_list(_read_optional_jsonl(artifacts["image_fetch_status"]))
    observations = _mapping_list(_read_optional_jsonl(artifacts["visual_observations"]))
    verifier_votes = _mapping_list(_read_optional_jsonl(artifacts["verifier_votes"]))
    counts = real_automatic_visual_release_counts(
        candidates=candidates,
        fetches=fetches,
        observations=observations,
        visual_provider_status=visual_provider_status,
    )
    real_candidates = _codex_native_acquisition_records(candidates)
    real_fetches = _codex_native_fetch_records(fetches)
    real_observations = _codex_interactive_observations(
        candidates=candidates,
        fetches=fetches,
        observations=observations,
    )
    real_vlm_image_ids = {
        str(observation.get("evidence_image_id"))
        for observation in real_observations
        if isinstance(observation.get("evidence_image_id"), str)
        and observation.get("evidence_image_id")
    }
    report_claims = _report_cited_visual_claims(
        evidence=evidence,
        report_status=report_status,
        candidates=candidates,
        fetches=fetches,
        observations=observations,
        verifier_votes=verifier_votes,
        report_text=report_text,
    )

    failures: list[dict[str, str]] = []
    if _terminal_status(run_status) != "completed_auto_visual" or _terminal_status(
        visual_provider_status
    ) != "completed_auto_visual":
        failures.append(
            {
                "check": "completed_auto_visual_status_consistency",
                "classification": "provider",
                "detail": "run_status and visual_provider_status must both be completed_auto_visual",
            }
        )
    if not _has_codex_native_acquisition_provider(visual_provider_status):
        failures.append(
            {
                "check": "codex_native_acquisition_provider",
                "classification": "provider",
                "detail": "visual_provider_status lacks a configured Codex-native acquisition provider",
            }
        )
    if len(real_candidates) < MIN_PUBLIC_BETA_REAL_VISUAL_CANDIDATES:
        failures.append(
            {
                "check": "at_least_10_real_image_centric_candidates",
                "classification": "fetch",
                "detail": (
                    "visual artifacts must include at least "
                    f"{MIN_PUBLIC_BETA_REAL_VISUAL_CANDIDATES} real Codex-native candidates"
                ),
            }
        )
    if not real_candidates or not real_fetches:
        failures.append(
            {
                "check": "codex_native_visual_acquisition_evidence",
                "classification": "fetch",
                "detail": "visual artifacts lack real Codex-native candidates and fetched images",
            }
        )
    if not _has_codex_interactive_vlm_provider(visual_provider_status) or not real_observations:
        failures.append(
            {
                "check": "codex_interactive_vlm_handoff_observations",
                "classification": "vlm",
                "detail": "visual artifacts lack real Codex-interactive VLM handoff observations",
            }
        )
    if _visual_observations_claim_hidden_codex_api(observations):
        failures.append(
            {
                "check": "codex_interactive_hidden_api_rejected",
                "classification": "vlm",
                "detail": (
                    "visual observations must use explicit Codex-interactive "
                    "artifact handoff, not hidden API markers"
                ),
            }
        )
    if len(real_vlm_image_ids) < MIN_PUBLIC_BETA_REAL_VLM_IMAGES_ANALYZED:
        failures.append(
            {
                "check": "at_least_3_codex_interactive_real_analyzed_images",
                "classification": "vlm",
                "detail": (
                    "visual artifacts must include at least "
                    f"{MIN_PUBLIC_BETA_REAL_VLM_IMAGES_ANALYZED} eligible "
                    "Codex-interactive VLM-analyzed images"
                ),
            }
        )
    if _policy_blocks_release(candidates, fetches, observations):
        failures.append(
            {
                "check": "policy_allows_release_counting",
                "classification": "policy",
                "detail": "policy-blocked visual records cannot enter release validation",
            }
        )
    if len(report_claims) < MIN_PUBLIC_BETA_REPORT_CITED_VISUAL_OR_MIXED_CLAIMS:
        failures.append(
            {
                "check": "report_cited_visual_or_mixed_claim",
                "classification": "report-linkage",
                "detail": "report lacks a cited supported visual or mixed claim tied to real visual evidence",
            }
        )
    return {
        "valid": not failures,
        "counts": {
            **counts,
            "real_candidates": len(real_candidates),
            "real_fetches": len(real_fetches),
            "real_vlm_observations": len(real_observations),
            "real_vlm_images_analyzed": len(real_vlm_image_ids),
            "report_cited_visual_or_mixed_claims": len(report_claims),
        },
        "failures": failures,
    }


def _visual_observations_claim_hidden_codex_api(
    observations: Sequence[Mapping[str, Any]],
) -> bool:
    for observation in observations:
        if _claims_hidden_codex_api(observation):
            return True
        provenance = observation.get("provider_provenance")
        if isinstance(provenance, Mapping) and _claims_hidden_codex_api(provenance):
            return True
    return False


def _visual_failure_category(failures: Sequence[Mapping[str, Any]]) -> str:
    classes = [str(item.get("classification") or "") for item in failures]
    if "policy" in classes:
        return "policy_block"
    if "vlm" in classes:
        return "vlm_failure"
    if "report-linkage" in classes:
        return "report_linkage_failure"
    if "fetch" in classes:
        return "fetch_failure"
    if "contradiction" in classes:
        return "visual_contradiction"
    return "provider_failure"


def _provider_provenance(
    *,
    prompt: Mapping[str, Any],
    run_status: Mapping[str, Any],
    evidence: Mapping[str, Any],
    visual_provider_status: Mapping[str, Any],
    supplied_run: bool,
) -> dict[str, Any]:
    providers = []
    for provider in _mapping_list(visual_provider_status.get("providers", [])):
        providers.append(
            {
                "provider": provider.get("provider"),
                "provider_kind": provider.get("provider_kind"),
                "provider_mode": provider.get("provider_mode"),
                "configured": provider.get("configured"),
                "available": provider.get("available"),
                "blocked_reason": provider.get("blocked_reason"),
                "invocations": provider.get("invocations", 0),
                "candidates_discovered": provider.get("candidates_discovered", 0),
                "artifacts_fetched": provider.get("artifacts_fetched", 0),
                "vlm_images_analyzed": provider.get("vlm_images_analyzed", 0),
                "estimated_cost_usd": provider.get("estimated_cost_usd", 0.0),
                "actual_cost_usd": provider.get("actual_cost_usd", 0.0),
                "codex_native_handoff": provider.get("codex_native_handoff"),
                "codex_interactive_handoff": provider.get("codex_interactive_handoff"),
                "handoff_artifact": provider.get("handoff_artifact"),
            }
        )
    expected_visual_providers = list(prompt.get("visual_provider_requirements", []))
    return {
        "supplied_real_run_directory": supplied_run,
        "prompt_id": prompt.get("id"),
        "route": prompt.get("route"),
        "mode_targets": list(prompt.get("mode_targets", [])),
        "selected_mode": run_status.get("selected_mode") or evidence.get("mode"),
        "execution_mode": _execution_mode(run_status, evidence),
        "runner_mode": _runner_mode(run_status, evidence),
        "evidence_mode": evidence.get("mode"),
        "parallel_adapter": run_status.get("adapter") or run_status.get("parallel_adapter"),
        "expected_visual_providers": expected_visual_providers,
        "visual_providers": providers,
        "external_network_call": _truthy_provider_field(
            visual_provider_status,
            providers,
            "external_network_call",
        ),
        "external_vlm_call": _truthy_provider_field(
            visual_provider_status,
            providers,
            "external_vlm_call",
        ),
        "codex_native_handoff": _truthy_provider_field(
            visual_provider_status,
            providers,
            "codex_native_handoff",
        ),
        "codex_interactive_handoff": _truthy_provider_field(
            visual_provider_status,
            providers,
            "codex_interactive_handoff",
        ),
    }


def _reliability_summary(
    *,
    root_runs_dir: Path,
    current_suite_id: str,
    current_generated_at: str,
    current_runs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    current_suite = _partial_parallel_suite_summary(
        suite_id=current_suite_id,
        generated_at=current_generated_at,
        runs=current_runs,
    )
    previous_suites = _load_previous_partial_parallel_suite_summaries(
        root_runs_dir=root_runs_dir,
        current_suite_id=current_suite_id,
    )
    suites = _latest_partial_parallel_suites([*previous_suites, current_suite])
    denominator = sum(
        _count(suite.get("completed_real_parallel_stage_runs")) for suite in suites
    )
    partial_runs = sum(_count(suite.get("partial_parallel_runs")) for suite in suites)
    rate = (partial_runs / denominator) if denominator else None
    enforcement_active = denominator >= PARTIAL_PARALLEL_MIN_ENFORCEMENT_DENOMINATOR
    band = _partial_parallel_rate_band(rate)
    return {
        "partial_parallel": {
            "formula": "partial_parallel_runs / completed_real_parallel_stage_runs",
            "window": {
                "latest_public_safe_suite_count": PARTIAL_PARALLEL_HISTORY_SUITE_COUNT,
                "suite_ids": [str(suite.get("suite_id") or "") for suite in suites],
                "suites": suites,
            },
            "partial_parallel_runs": partial_runs,
            "completed_real_parallel_stage_runs": denominator,
            "partial_parallel_rate": rate,
            "target_rate": PARTIAL_PARALLEL_TARGET_RATE,
            "warning_rate": PARTIAL_PARALLEL_WARNING_RATE,
            "bands": {
                "target": "<= 5%",
                "warning": "> 5% and <= 10%",
                "regression_failure": "> 10%",
            },
            "band": band,
            "enforcement_min_denominator": PARTIAL_PARALLEL_MIN_ENFORCEMENT_DENOMINATOR,
            "enforcement_active": enforcement_active,
            "threshold_status": band if enforcement_active else "insufficient_denominator",
            "release_gate_blocking": enforcement_active and band == "regression_failure",
        }
    }


def _partial_parallel_suite_summary(
    *,
    suite_id: str,
    generated_at: str,
    runs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    denominator_runs = [run for run in runs if _completed_real_parallel_stage_run(run)]
    partial_runs = [run for run in denominator_runs if _run_partial_parallel(run)]
    denominator = len(denominator_runs)
    partial_count = len(partial_runs)
    return {
        "suite_id": suite_id,
        "generated_at": generated_at,
        "public_safe": True,
        "partial_parallel_runs": partial_count,
        "completed_real_parallel_stage_runs": denominator,
        "partial_parallel_rate": (partial_count / denominator) if denominator else None,
        "partial_prompt_ids": [str(run.get("id") or "") for run in partial_runs],
    }


def _load_previous_partial_parallel_suite_summaries(
    *,
    root_runs_dir: Path,
    current_suite_id: str,
) -> list[dict[str, Any]]:
    suites: list[dict[str, Any]] = []
    if not root_runs_dir.exists():
        return suites
    for child in root_runs_dir.iterdir():
        if not child.is_dir() or child.name == current_suite_id:
            continue
        path = child / PUBLIC_BETA_VALIDATION_RESULTS_FILENAME
        payload = _read_optional_json(path)
        if not isinstance(payload, Mapping) or payload.get("public_safe") is not True:
            continue
        existing = _existing_partial_parallel_suite_summary(payload)
        if existing is not None:
            suites.append(existing)
            continue
        runs = payload.get("runs")
        if isinstance(runs, list):
            suites.append(
                _partial_parallel_suite_summary(
                    suite_id=str(payload.get("suite_id") or child.name),
                    generated_at=str(payload.get("generated_at") or ""),
                    runs=[run for run in runs if isinstance(run, Mapping)],
                )
            )
    return suites


def _existing_partial_parallel_suite_summary(
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    reliability = payload.get("reliability")
    if not isinstance(reliability, Mapping):
        return None
    partial = reliability.get("partial_parallel")
    if not isinstance(partial, Mapping):
        return None
    window = partial.get("window")
    if not isinstance(window, Mapping):
        return None
    suites = window.get("suites")
    if not isinstance(suites, list):
        return None
    suite_id = str(payload.get("suite_id") or "")
    for suite in suites:
        if isinstance(suite, Mapping) and str(suite.get("suite_id") or "") == suite_id:
            return {
                "suite_id": suite_id,
                "generated_at": str(suite.get("generated_at") or payload.get("generated_at") or ""),
                "public_safe": True,
                "partial_parallel_runs": _count(suite.get("partial_parallel_runs")),
                "completed_real_parallel_stage_runs": _count(
                    suite.get("completed_real_parallel_stage_runs")
                ),
                "partial_parallel_rate": suite.get("partial_parallel_rate"),
                "partial_prompt_ids": list(suite.get("partial_prompt_ids") or []),
            }
    return None


def _latest_partial_parallel_suites(
    suites: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for suite in suites:
        suite_id = str(suite.get("suite_id") or "")
        if suite_id:
            deduped[suite_id] = dict(suite)
    return sorted(
        deduped.values(),
        key=lambda suite: str(suite.get("generated_at") or ""),
        reverse=True,
    )[:PARTIAL_PARALLEL_HISTORY_SUITE_COUNT]


def _run_partial_parallel_summary(
    run_status: Mapping[str, Any],
    parallel_status: Mapping[str, Any],
    *,
    final_artifact_gate_passed: bool,
) -> dict[str, Any]:
    source = _as_mapping(parallel_status.get("partial_parallel_summary"))
    failure_counts = _as_mapping(parallel_status.get("failure_counts"))
    retry_summary = _as_mapping(parallel_status.get("retry_summary"))
    evidence_source = _as_mapping(parallel_status.get("evidence_source"))
    planned = _first_count(
        source.get("planned_task_count"),
        parallel_status.get("planned_task_count"),
    )
    accepted = _first_count(
        source.get("accepted_shard_count"),
        parallel_status.get("accepted_shard_count"),
        evidence_source.get("accepted_shards"),
    )
    omitted = _first_count(source.get("omitted_task_count"))
    if omitted is None:
        omitted = max(0, (planned or 0) - (accepted or 0)) if planned is not None else 0
    failed = _first_count(source.get("failed_task_count"), failure_counts.get("failed_tasks")) or 0
    blocked = _first_count(source.get("blocked_task_count"), failure_counts.get("blocked_tasks")) or 0
    rejected = _first_count(source.get("rejected_shard_count"), failure_counts.get("rejected_shards")) or 0
    discarded = _first_count(source.get("discarded_task_count"), failure_counts.get("discarded_tasks")) or 0
    retried = _first_count(source.get("retried_task_count"), retry_summary.get("retry_count")) or 0
    retry_exhausted = _first_count(
        source.get("retry_exhausted_task_count"),
        retry_summary.get("retry_exhausted_count"),
    ) or 0
    partial = source.get("partial")
    if not isinstance(partial, bool):
        partial = (
            str(parallel_status.get("status") or run_status.get("status") or "")
            == "completed_partial_parallel"
            or ((accepted or 0) > 0 and omitted > 0)
            or any(count > 0 for count in (failed, blocked, rejected, discarded))
            or bool(parallel_status.get("parallel_degraded"))
        )
    reason = str(
        source.get("reason_category")
        or parallel_status.get("partial_reason_category")
        or ""
    )
    if not reason:
        reason = _partial_reason_category_from_counts(
            partial=partial,
            accepted=accepted or 0,
            failed=failed,
            blocked=blocked,
            rejected=rejected,
            discarded=discarded,
            retry_exhausted=retry_exhausted,
            omitted=omitted,
            parallel_degraded=bool(parallel_status.get("parallel_degraded")),
        )
    return {
        "partial": partial,
        "reason_category": reason,
        "completed_real_parallel_stage": _completed_real_parallel_stage_from_artifacts(
            run_status=run_status,
            parallel_status=parallel_status,
        ),
        "parallel_stage_status": str(
            parallel_status.get("status") or run_status.get("status") or "unknown"
        ),
        "planned_task_count": planned or 0,
        "accepted_shard_count": accepted or 0,
        "omitted_task_count": omitted,
        "failed_task_count": failed,
        "blocked_task_count": blocked,
        "rejected_shard_count": rejected,
        "discarded_task_count": discarded,
        "retried_task_count": retried,
        "retry_exhausted_task_count": retry_exhausted,
        "final_artifact_gate_passed": final_artifact_gate_passed,
    }


def _completed_real_parallel_stage_run(run: Mapping[str, Any]) -> bool:
    summary = run.get("partial_parallel_summary")
    return isinstance(summary, Mapping) and summary.get("completed_real_parallel_stage") is True


def _run_partial_parallel(run: Mapping[str, Any]) -> bool:
    summary = run.get("partial_parallel_summary")
    return isinstance(summary, Mapping) and summary.get("partial") is True


def _completed_real_parallel_stage_from_artifacts(
    *,
    run_status: Mapping[str, Any],
    parallel_status: Mapping[str, Any],
) -> bool:
    stage_status = str(parallel_status.get("status") or run_status.get("status") or "")
    if stage_status not in {"completed_parallel", "completed_partial_parallel"}:
        return False
    evidence_source = _as_mapping(parallel_status.get("evidence_source"))
    adapter = str(
        parallel_status.get("adapter")
        or evidence_source.get("adapter")
        or run_status.get("adapter")
        or ""
    )
    selected_mode = str(run_status.get("selected_mode") or "")
    if selected_mode in {"quick-chat", "manual-handoff"} or adapter == "fixture":
        return False
    return (
        evidence_source.get("real_child_execution") is True
        or str(evidence_source.get("type") or "") == "real_child_execution"
        or adapter == "codex-exec"
    )


def _partial_parallel_rate_band(rate: float | None) -> str:
    if rate is None:
        return "insufficient_denominator"
    if rate <= PARTIAL_PARALLEL_TARGET_RATE:
        return "target"
    if rate <= PARTIAL_PARALLEL_WARNING_RATE:
        return "warning"
    return "regression_failure"


def _partial_reason_category_from_counts(
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


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_count(*values: Any) -> int | None:
    for value in values:
        if value is None:
            continue
        return _count(value)
    return None


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(0, value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _outcome_counts(runs: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "passed": sum(1 for run in runs if run.get("status") == "passed"),
        "failed": sum(1 for run in runs if run.get("status") == "failed"),
        "blocked": sum(1 for run in runs if run.get("status") == "blocked"),
        "excluded": sum(1 for run in runs if run.get("status") == "excluded"),
    }


def _classification_counts(runs: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {
        "success": 0,
        "included_failure": 0,
        "excluded_blocked": 0,
        "excluded_fixture": 0,
    }
    for run in runs:
        classification = str(run.get("metric_classification", "included_failure"))
        counts[classification] = counts.get(classification, 0) + 1
    return counts


def _failure_category_counts(runs: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {category: 0 for category in FAILURE_CATEGORIES}
    for run in runs:
        category = run.get("failure_category")
        if category in counts:
            counts[category] += 1
    return counts


def _terminal_status(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return "missing_run_status"
    status = payload.get("status")
    return str(status) if status else "unknown"


def public_beta_prompt_hash(prompt: str) -> str:
    """Return the canonical Public Beta prompt identity hash."""

    normalized = normalize_public_beta_prompt_text(prompt)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _prompt_hash(prompt: str) -> str:
    return public_beta_prompt_hash(prompt)


def normalize_public_beta_prompt_text(value: str) -> str:
    """Normalize prompt text before hashing or equality checks."""

    return " ".join(value.strip().split())


def _normalize_prompt_text(value: str) -> str:
    return normalize_public_beta_prompt_text(value)


def _string_field_values(
    *payloads: Mapping[str, Any],
    names: Sequence[str],
) -> list[str]:
    values: list[str] = []
    for payload in payloads:
        for name in names:
            value = payload.get(name)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
    return values


def _timestamp_candidates(*payloads: Mapping[str, Any]) -> list[str]:
    candidates: list[str] = []
    for payload in payloads:
        for name in ("completed_at", "generated_at", "created_at", "updated_at"):
            value = payload.get(name)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
    return candidates


def _freshness_check(
    timestamps: Sequence[str],
    *,
    validation_time: str | None,
) -> dict[str, Any]:
    failures: list[str] = []
    parsed: list[datetime] = []
    for value in timestamps:
        parsed_at = _parse_timestamp(value)
        if parsed_at is None:
            failures.append(f"timestamp is invalid: {value}")
        else:
            parsed.append(parsed_at)
    if not parsed:
        failures.append("fresh generated_at/created_at/completed_at timestamp is missing")
        return {"fresh": False, "selected_timestamp": None, "failures": failures}

    reference = _parse_timestamp(validation_time) if validation_time else datetime.now(timezone.utc)
    if reference is None:
        reference = datetime.now(timezone.utc)
    selected = max(parsed)
    if selected > reference + timedelta(minutes=5):
        failures.append("timestamp is in the future relative to validation time")
    if reference - selected > timedelta(days=SUPPLIED_RUN_MAX_AGE_DAYS):
        failures.append(
            f"timestamp is older than {SUPPLIED_RUN_MAX_AGE_DAYS} days"
        )
    return {
        "fresh": not failures,
        "selected_timestamp": selected.isoformat().replace("+00:00", "Z"),
        "failures": failures,
    }


def _parse_timestamp(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _strict_int_count(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _codex_native_acquisition_records(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        record
        for record in records
        if _is_codex_native_policy_allowed_acquisition_record(record)
    ]


def _codex_native_fetch_records(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        record
        for record in records
        if _is_codex_native_policy_allowed_acquisition_record(record)
        and record.get("fetch_status") == "fetched"
        and isinstance(record.get("evidence_image_id"), str)
        and record.get("evidence_image_id")
    ]


def _codex_interactive_observations(
    *,
    candidates: Sequence[Mapping[str, Any]],
    fetches: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    candidates_by_id = {
        str(item.get("candidate_id")): item
        for item in candidates
        if isinstance(item.get("candidate_id"), str) and item.get("candidate_id")
    }
    fetches_by_id = {
        str(item.get("fetch_id")): item
        for item in fetches
        if isinstance(item.get("fetch_id"), str) and item.get("fetch_id")
    }
    eligible: list[Mapping[str, Any]] = []
    for observation in observations:
        if not _is_codex_interactive_vlm_observation(observation):
            continue
        candidate_id = observation.get("candidate_id")
        fetch_id = observation.get("fetch_id")
        image_id = observation.get("evidence_image_id")
        if not (
            isinstance(candidate_id, str)
            and candidate_id
            and isinstance(fetch_id, str)
            and fetch_id
            and isinstance(image_id, str)
            and image_id
        ):
            continue
        candidate = candidates_by_id.get(candidate_id)
        fetch = fetches_by_id.get(fetch_id)
        if candidate is None or fetch is None:
            continue
        if not _is_codex_native_policy_allowed_acquisition_record(candidate):
            continue
        if not _is_codex_native_policy_allowed_fetch(
            fetch,
            image_id=image_id,
            candidate_id=candidate_id,
        ):
            continue
        eligible.append(observation)
    return eligible


def _has_codex_native_acquisition_provider(payload: Mapping[str, Any]) -> bool:
    for provider in _mapping_list(payload.get("providers", [])):
        if (
            provider.get("provider_mode") == "real"
            and provider.get("provider_kind") in _REAL_ACQUISITION_PROVIDER_KINDS
            and _is_codex_native_provider_record(provider)
            and provider.get("configured") is True
            and provider.get("available") is True
            and int(provider.get("invocations") or 0) > 0
        ):
            return True
    return False


def _has_codex_interactive_vlm_provider(payload: Mapping[str, Any]) -> bool:
    for provider in _mapping_list(payload.get("providers", [])):
        if (
            provider.get("provider_mode") == "real"
            and provider.get("provider_kind") == "vlm"
            and _provider_name(provider) == "codex-interactive"
            and provider.get("configured") is True
            and provider.get("available") is True
            and int(provider.get("invocations") or 0) > 0
            and int(provider.get("vlm_images_analyzed") or 0) > 0
            and _is_explicit_codex_handoff(provider)
        ):
            return True
    return False


def _policy_blocks_release(
    candidates: Sequence[Mapping[str, Any]],
    fetches: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> bool:
    for record in list(candidates) + list(fetches) + list(observations):
        if record.get("provider_mode") != "real":
            continue
        if record.get("policy_decision") in {
            "blocked",
            "manual_review",
            "disallowed",
            "restricted",
        }:
            return True
    return False


def _report_cited_visual_claims(
    *,
    evidence: Mapping[str, Any],
    report_status: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    fetches: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    verifier_votes: Sequence[Mapping[str, Any]],
    report_text: str,
) -> list[dict[str, Any]]:
    used_images = {
        image_id
        for image_id in report_status.get("used_images", [])
        if isinstance(image_id, str) and image_id
    }
    if not used_images:
        return []
    candidates_by_id = {
        str(item.get("candidate_id")): item
        for item in candidates
        if isinstance(item.get("candidate_id"), str) and item.get("candidate_id")
    }
    fetches_by_id = {
        str(item.get("fetch_id")): item
        for item in fetches
        if isinstance(item.get("fetch_id"), str) and item.get("fetch_id")
    }
    images_by_id = {
        str(image.get("id")): image
        for image in evidence.get("images", [])
        if isinstance(image, Mapping)
        and isinstance(image.get("id"), str)
        and image.get("id")
    }
    verifier_vote_ids = {
        str(vote.get("id"))
        for vote in verifier_votes
        if isinstance(vote.get("id"), str) and vote.get("id")
    }
    cited: list[dict[str, Any]] = []
    claims = evidence.get("claims") if isinstance(evidence.get("claims"), list) else []
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        claim_id = str(claim.get("id") or "")
        if not claim_id:
            continue
        if claim.get("verification_status") != "supported":
            continue
        if claim.get("claim_type") not in {"visual", "mixed"}:
            continue
        supporting_images = {
            image_id
            for image_id in claim.get("supporting_images", [])
            if isinstance(image_id, str) and image_id
        }
        linked_images = supporting_images & used_images
        if not linked_images:
            continue
        if report_text and claim_id not in report_text and not any(
            image_id in report_text for image_id in linked_images
        ):
            continue
        release_images = [
            image_id
            for image_id in sorted(linked_images)
            if _has_real_report_cited_observation(
                claim=claim,
                claim_id=claim_id,
                image_id=image_id,
                candidates_by_id=candidates_by_id,
                fetches_by_id=fetches_by_id,
                images_by_id=images_by_id,
                observations=observations,
                verifier_vote_ids=verifier_vote_ids,
            )
        ]
        if release_images:
            cited.append({"claim_id": claim_id, "image_ids": release_images})
    return cited


def _has_real_report_cited_observation(
    *,
    claim: Mapping[str, Any],
    claim_id: str,
    image_id: str,
    candidates_by_id: Mapping[str, Mapping[str, Any]],
    fetches_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    verifier_vote_ids: set[str],
) -> bool:
    image = images_by_id.get(image_id)
    if image is None:
        return False
    if image.get("policy_decision") != "allowed":
        return False
    if str(image.get("provider_mode") or "").strip().lower().replace("_", "-") in {
        "fixture",
        "manual",
        "user-provided",
        "post-hoc",
    }:
        return False
    for observation in observations:
        if observation.get("evidence_image_id") != image_id:
            continue
        if not _is_codex_interactive_vlm_observation(observation):
            continue
        if not _has_report_link(observation, claim_id):
            continue
        if not _has_verifier_vote_link(observation, claim_id, verifier_vote_ids):
            continue
        candidate_id = observation.get("candidate_id")
        fetch_id = observation.get("fetch_id")
        if not isinstance(candidate_id, str) or not isinstance(fetch_id, str):
            continue
        candidate = candidates_by_id.get(candidate_id)
        fetch = fetches_by_id.get(fetch_id)
        if candidate is None or fetch is None:
            continue
        if not _is_codex_native_policy_allowed_acquisition_record(candidate):
            continue
        if not _is_codex_native_policy_allowed_fetch(fetch, image_id=image_id, candidate_id=candidate_id):
            continue
        if image.get("candidate_id") not in {None, candidate_id}:
            continue
        if image.get("fetch_id") not in {None, fetch_id}:
            continue
        if not _claim_visual_supports_image(claim, image_id):
            continue
        return True
    return False


def _is_codex_interactive_vlm_observation(record: Mapping[str, Any]) -> bool:
    provenance = record.get("provider_provenance")
    return (
        record.get("provider_kind") == "vlm"
        and record.get("provider_mode") == "real"
        and record.get("observation_status") == "analyzed"
        and record.get("policy_decision") == "allowed"
        and isinstance(provenance, Mapping)
        and _provider_name(record) == "codex-interactive"
        and provenance.get("provider_kind") == "vlm"
        and provenance.get("provider_mode") == "real"
        and _provider_name(provenance) == "codex-interactive"
        and _is_explicit_codex_handoff(record)
        and _is_explicit_codex_handoff(provenance)
        and not _claims_hidden_codex_api(record)
        and not _claims_hidden_codex_api(provenance)
        and record.get("external_vlm_call") is not True
        and provenance.get("external_vlm_call") is not True
    )


def _is_codex_native_policy_allowed_acquisition_record(record: Mapping[str, Any]) -> bool:
    provenance = record.get("provider_provenance")
    return (
        record.get("provider_mode") == "real"
        and record.get("provider_kind") in _REAL_ACQUISITION_PROVIDER_KINDS
        and record.get("policy_decision") == "allowed"
        and isinstance(provenance, Mapping)
        and provenance.get("provider_mode") == "real"
        and provenance.get("provider_kind") in _REAL_ACQUISITION_PROVIDER_KINDS
        and _is_codex_native_provider_record(record)
        and _is_codex_native_provider_record(provenance)
        and not _claims_hidden_codex_api(record)
        and not _claims_hidden_codex_api(provenance)
    )


def _is_codex_native_policy_allowed_fetch(
    fetch: Mapping[str, Any],
    *,
    image_id: str,
    candidate_id: str,
) -> bool:
    return (
        _is_codex_native_policy_allowed_acquisition_record(fetch)
        and fetch.get("fetch_status") == "fetched"
        and fetch.get("candidate_id") == candidate_id
        and fetch.get("evidence_image_id") == image_id
    )


def _has_report_link(observation: Mapping[str, Any], claim_id: str) -> bool:
    for link in observation.get("report_links", []):
        if not isinstance(link, Mapping):
            continue
        if link.get("claim_id") == claim_id and (
            link.get("citation_id") or link.get("report_section_id")
        ):
            return True
    return False


def _has_verifier_vote_link(
    observation: Mapping[str, Any],
    claim_id: str,
    verifier_vote_ids: set[str],
) -> bool:
    if not verifier_vote_ids:
        return False
    for link in observation.get("verifier_links", []):
        if not isinstance(link, Mapping):
            continue
        vote_id = link.get("verifier_vote_id")
        if link.get("claim_id") == claim_id and isinstance(vote_id, str):
            return vote_id in verifier_vote_ids
    return False


def _claim_visual_supports_image(claim: Mapping[str, Any], image_id: str) -> bool:
    for support in claim.get("visual_supports", []):
        if not isinstance(support, Mapping):
            continue
        if support.get("image_id") == image_id and isinstance(
            support.get("observation_ref"), str
        ):
            return True
    return False


def _provider_has_vlm_gap(payload: Mapping[str, Any]) -> bool:
    providers = _mapping_list(payload.get("providers", []))
    return any(
        provider.get("provider") in {"openai-responses-vision", "codex-interactive"}
        and int(provider.get("vlm_images_analyzed", 0) or 0) == 0
        for provider in providers
    )


def _report_status_uses_images(payload: Mapping[str, Any]) -> bool:
    used_images = payload.get("used_images")
    return isinstance(used_images, list) and bool(used_images)


def _truthy_provider_field(
    provider_status: Mapping[str, Any],
    providers: Sequence[Mapping[str, Any]],
    field: str,
) -> bool:
    if provider_status.get(field) is True:
        return True
    return any(provider.get(field) is True for provider in providers)


def _selected_mode(
    run_status: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> str | None:
    selected = run_status.get("selected_mode") or evidence.get("mode")
    if isinstance(selected, list):
        selected = selected[0] if selected else None
    if isinstance(selected, str) and selected.strip():
        return selected.strip()
    return None


def _execution_mode(
    run_status: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> str | None:
    for value in (
        run_status.get("execution_mode"),
        evidence.get("execution_mode"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _runner_mode(
    run_status: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> str | None:
    for value in (
        run_status.get("runner_mode"),
        evidence.get("runner_mode"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _provider_name(record: Mapping[str, Any]) -> str:
    for name in ("provider", "search_provider", "analysis_provider", "vlm_provider"):
        value = record.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip().lower().replace("_", "-")
    return ""


def _is_codex_native_search_result(record: Mapping[str, Any]) -> bool:
    provider = _provider_name(record)
    retrieval_status = str(record.get("retrieval_status") or "").strip().lower()
    provider_mode = (
        str(record.get("provider_mode") or "").strip().lower().replace("_", "-")
    )
    policy_decision = (
        str(record.get("policy_decision") or "").strip().lower().replace("_", "-")
    )
    return (
        _has_complete_release_search_result_fields(record)
        and record.get("route") in SEARCH_RESULT_ROUTES
        and record.get("result_type") in RELEASE_SEARCH_RESULT_TYPES
        and provider == "codex-native"
        and provider_mode == "real"
        and policy_decision == "allowed"
        and retrieval_status == "fetched"
        and str(record.get("handoff_artifact") or "").strip() == "search_results.jsonl"
        and not _has_hidden_codex_api_marker_field(record)
    )


def _has_complete_release_search_result_fields(record: Mapping[str, Any]) -> bool:
    return all(
        _release_search_result_field_present(record, field)
        for field in RELEASE_SEARCH_RESULT_REQUIRED_FIELDS
    )


def _release_search_result_field_present(
    record: Mapping[str, Any],
    field: str,
) -> bool:
    if field not in record:
        return False
    value = record.get(field)
    if value is None:
        return False
    if field == "rank":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _has_hidden_codex_api_marker_field(record: Mapping[str, Any]) -> bool:
    return any(
        field in record
        for field in (
            "hidden_codex_api_call",
            "codex_native_api_call",
            "hidden_api_call",
        )
    )


def _has_non_release_handoff_records(records: Sequence[Mapping[str, Any]]) -> bool:
    for record in records:
        mode = (
            str(record.get("provider_mode") or "").strip().lower().replace("_", "-")
        )
        provider = _provider_name(record)
        if mode in {"fixture", "manual", "user-provided", "post-hoc"}:
            return True
        if provider in {"fixture", "manual", "manual-sources", "user-provided", "post-hoc"}:
            return True
        if record.get("post_hoc_patch") is True or record.get("post_hoc_patched") is True:
            return True
    return False


def _is_codex_native_provider_record(record: Mapping[str, Any]) -> bool:
    provider = _provider_name(record)
    if provider in _CODEX_NATIVE_VISUAL_ACQUISITION_PROVIDERS:
        return True
    search_provider = record.get("search_provider")
    if (
        isinstance(search_provider, str)
        and search_provider.strip().lower().replace("_", "-")
        in _CODEX_NATIVE_SEARCH_PROVIDERS
    ):
        return True
    return _handoff_artifact_mentions(
        record,
        {
            "search_results.jsonl",
            "visual_candidates.jsonl",
            "image_fetch_status.jsonl",
        },
    )


def _is_explicit_codex_handoff(record: Mapping[str, Any]) -> bool:
    if record.get("codex_native_handoff") is True:
        return True
    if record.get("codex_interactive_handoff") is True:
        return True
    if record.get("handoff_recorded") is True:
        return True
    return _handoff_artifact_mentions(
        record,
        {
            "search_results.jsonl",
            "visual_candidates.jsonl",
            "image_fetch_status.jsonl",
            "visual_observations.jsonl",
        },
    )


def _handoff_artifact_mentions(
    record: Mapping[str, Any],
    expected: set[str],
) -> bool:
    values: list[Any] = []
    for name in (
        "handoff_artifact",
        "handoff_artifacts",
        "handoff_path",
        "handoff_paths",
        "source_handoff_artifact",
        "source_handoff_path",
    ):
        value = record.get(name)
        if isinstance(value, list):
            values.extend(value)
        elif value is not None:
            values.append(value)
    return any(
        isinstance(value, str)
        and any(item in value.replace("\\", "/") for item in expected)
        for value in values
    )


def _claims_hidden_codex_api(record: Mapping[str, Any]) -> bool:
    return bool(
        record.get("hidden_codex_api_call") is True
        or record.get("codex_native_api_call") is True
        or record.get("hidden_api_call") is True
    )


def _provider_kind(provider: str) -> str:
    if provider in {"openai-responses-vision", "codex-interactive"}:
        return "vlm"
    if provider == "pdf_rasterizer":
        return "pdf_rasterizer"
    if provider == "screenshot":
        return "screenshot"
    if provider == "page_extractor":
        return "page_extractor"
    if provider == "web_image_search":
        return "web_image_search"
    return "visual_provider"


def _normalize_mapping(
    values: Mapping[str, str | Path] | None,
) -> dict[str, Path]:
    return {key: Path(value) for key, value in (values or {}).items()}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_optional_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _read_optional_jsonl(path: str | Path) -> list[Any] | None:
    try:
        records = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _read_optional_text(path: str | Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
