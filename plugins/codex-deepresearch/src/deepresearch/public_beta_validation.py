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
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .semantic_planner import (
    SEMANTIC_MATERIALIZATION_DIFF_FILENAME,
    build_semantic_materialization_diff,
)
from .visual_artifacts import (
    VISUAL_PROVIDER_STATUS_FILENAME,
    real_automatic_visual_release_counts,
)


PUBLIC_BETA_PROMPT_MANIFEST_SCHEMA_VERSION = (
    "codex-deepresearch.public-beta-prompts.v0"
)
SEMANTIC_REGRESSION_MANIFEST_SCHEMA_VERSION = (
    "codex-deepresearch.semantic-regression-manifest.v0"
)
PUBLIC_BETA_SEMANTIC_MANIFEST_SCHEMA_VERSION = (
    "codex-deepresearch.public-beta-semantic-manifest.v0"
)
BLIND_HOLDOUT_MANIFEST_SCHEMA_VERSION = (
    "codex-deepresearch.semantic-blind-holdout-manifest.v0"
)
BLIND_HOLDOUT_SELECTOR_AUDIT_SCHEMA_VERSION = (
    "codex-deepresearch.semantic-blind-holdout-selector-audit.v0"
)
BLIND_HOLDOUT_SELECTOR_TRANSCRIPT_SCHEMA_VERSION = (
    "codex-deepresearch.semantic-blind-holdout-selector-transcript.v0"
)
MANUAL_TRACE_AUDIT_MANIFEST_SCHEMA_VERSION = (
    "codex-deepresearch.manual-trace-audits.v0"
)
PUBLIC_BETA_VALIDATION_SCHEMA_VERSION = (
    "codex-deepresearch.public-beta-validation.v0"
)
PUBLIC_BETA_VALIDATION_RESULTS_FILENAME = "public_beta_validation_results.json"
PUBLIC_BETA_VALIDATION_SUMMARY_FILENAME = "public_beta_validation_summary.md"
SEMANTIC_RELEASE_REPORT_FILENAME = "semantic_release_report.json"
SEMANTIC_RELEASE_VALIDATION_RESULTS_FILENAME = "semantic_release_validation_results.json"
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
DEFAULT_SEMANTIC_REGRESSION_MANIFEST = (
    PLUGIN_ROOT / "validation" / "semantic_regression_prompts.json"
)
DEFAULT_PUBLIC_BETA_SEMANTIC_MANIFEST = (
    PLUGIN_ROOT / "validation" / "public_beta_semantic_prompts.json"
)
DEFAULT_BLIND_HOLDOUT_MANIFEST = (
    PLUGIN_ROOT / "validation" / "blind_holdout_semantic_prompts.json"
)
DEFAULT_MANUAL_TRACE_AUDIT_MANIFEST = (
    PLUGIN_ROOT / "validation" / "manual_trace_audits.json"
)

VISUAL_ROUTES = {"visual_required", "visual_optional"}
SEMANTIC_MANIFEST_DENOMINATOR_RULE = "release_counted_failure_unless_passed"
SEMANTIC_REQUIRED_PUBLIC_BETA_CATEGORIES = {
    "korean": 8,
    "visual": 8,
    "official_regulatory_source_quality": 8,
    "ambiguous_domain": 5,
    "non_software_domain": 5,
    "historical_failure": 1,
}
SEMANTIC_REQUIRED_REGRESSION_CATEGORIES = {
    "korean": 10,
    "visual": 8,
    "policy_regulatory_domain": 8,
    "software_implementation": 5,
    "ambiguous_non_software": 5,
}
SEMANTIC_REQUIRED_HOLDOUT_CATEGORIES = {
    "korean": 4,
    "visual": 4,
    "official_regulatory_source_quality": 4,
    "ambiguous_domain": 3,
    "non_software_domain": 3,
}
MANUAL_TRACE_AUDIT_REQUIRED_CHAIN = (
    "original_question",
    "locked_oracle",
    "semantic_plan",
    "semantic_review",
    "materialization_diff",
    "research_tasks",
    "search_tasks",
    "visual_tasks",
    "accepted_shards",
    "final_report",
)
MANUAL_TRACE_AUDIT_MIN_COUNT = 5
MANUAL_TRACE_AUDIT_MIN_KOREAN = 2
MANUAL_TRACE_AUDIT_MIN_VISUAL = 2
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
    "semantic_materialization_diff",
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
SEMANTIC_MIN_RELEASE_ANGLES = 2
SEMANTIC_MIN_ANGLE_OVERLAP_TOKENS = 2
SEMANTIC_MIN_ANGLE_UNIQUE_TOKENS = 4
SEMANTIC_ANGLE_NEAR_DUPLICATE_THRESHOLD = 0.85
GENERIC_SEMANTIC_ANGLE_TEXTS = {
    "primary source discovery",
    "find authoritative sources that directly answer the research question",
}
GENERIC_SEMANTIC_PLACEHOLDER_PATTERNS = (
    r"\bangle\s*\d+\b",
    r"\bangle_\d+\b",
    r"\bevidence\s+angle\b",
    r"\bsupport\s+angle\b",
)
GENERIC_SEMANTIC_TOKENS = {
    "and",
    "answer",
    "authoritative",
    "compare",
    "directly",
    "does",
    "discovery",
    "do",
    "evidence",
    "find",
    "for",
    "from",
    "how",
    "official",
    "primary",
    "public",
    "question",
    "research",
    "source",
    "sources",
    "support",
    "supports",
    "supporting",
    "that",
    "the",
    "what",
    "with",
    "which",
}
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
    manual_audit_manifest: str | Path | None = None,
    require_manual_trace_audits: bool = False,
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
    semantic_report_path = suite_dir / SEMANTIC_RELEASE_REPORT_FILENAME
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
    semantic_release_report = _semantic_release_report(
        evaluated_runs=evaluated_runs,
        report_path=semantic_report_path,
        generated_at=generated_at,
        manual_audit_manifest=manual_audit_manifest,
        require_manual_trace_audits=require_manual_trace_audits,
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
    semantic_report_ready = semantic_release_report["valid"]
    release_gate_ready = (
        prompt_release_gate_ready
        and external_release_gate_ready
        and semantic_report_ready
    )
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
            "semantic_release_report_ready": semantic_report_ready,
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
        "semantic_release_report": semantic_release_report,
        "acceptance": acceptance,
        "runs": evaluated_runs,
        "remaining_gaps": _remaining_gaps(
            evaluated_runs=evaluated_runs,
            prompt_metrics=prompt_metrics,
            external_gate_results=external_gate_results,
            required_gate_ids=required_gate_ids,
            external_gates_required=external_gates_required,
            reliability=reliability,
            semantic_release_report=semantic_release_report,
        ),
        "artifacts": {
            "results": str(results_path.resolve()),
            "summary": str(summary_path.resolve()),
            "semantic_release_report": str(semantic_report_path.resolve()),
        },
    }
    _write_json(semantic_report_path, semantic_release_report)
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


def run_semantic_release_validation(
    *,
    runs_dir: str | Path,
    suite_id: str = "semantic-release-validation",
    clean: bool = False,
    semantic_regression_manifest: str | Path = DEFAULT_SEMANTIC_REGRESSION_MANIFEST,
    public_beta_semantic_manifest: str | Path = DEFAULT_PUBLIC_BETA_SEMANTIC_MANIFEST,
    blind_holdout_manifest: str | Path = DEFAULT_BLIND_HOLDOUT_MANIFEST,
    manual_audit_manifest: str | Path | None = DEFAULT_MANUAL_TRACE_AUDIT_MANIFEST,
    semantic_regression_runs: Mapping[str, str | Path] | None = None,
    public_beta_semantic_runs: Mapping[str, str | Path] | None = None,
    blind_holdout_runs: Mapping[str, str | Path] | None = None,
) -> dict[str, Any]:
    """Validate the #133 semantic release suites as strict release-counted gates."""

    root_runs_dir = Path(runs_dir)
    suite_dir = root_runs_dir / suite_id
    if suite_dir.exists():
        if not clean:
            raise PublicBetaValidationError(
                f"suite directory already exists: {suite_dir}"
            )
        shutil.rmtree(suite_dir)
    suite_dir.mkdir(parents=True)
    results_path = suite_dir / SEMANTIC_RELEASE_VALIDATION_RESULTS_FILENAME
    semantic_report_path = suite_dir / SEMANTIC_RELEASE_REPORT_FILENAME
    generated_at = _utc_now()

    regression_manifest = load_semantic_regression_manifest(semantic_regression_manifest)
    public_semantic_manifest = load_public_beta_semantic_manifest(
        public_beta_semantic_manifest
    )
    holdout_manifest = load_blind_holdout_manifest(
        blind_holdout_manifest,
        known_manifests=[regression_manifest, public_semantic_manifest],
    )
    regression_evaluated = _evaluate_semantic_manifest_prompt_runs(
        regression_manifest,
        semantic_regression_runs,
        suite_id=suite_id,
        validation_time=generated_at,
    )
    public_beta_evaluated = _evaluate_semantic_manifest_prompt_runs(
        public_semantic_manifest,
        public_beta_semantic_runs,
        suite_id=suite_id,
        validation_time=generated_at,
    )
    holdout_evaluated = _evaluate_semantic_manifest_prompt_runs(
        holdout_manifest,
        blind_holdout_runs,
        suite_id=suite_id,
        validation_time=generated_at,
    )
    suite_gates = {
        "semantic_regression_30": semantic_manifest_execution_gate(
            regression_manifest,
            regression_evaluated,
            suite_label="semantic_regression_30",
            require_all_prompts_release_counted=True,
        ),
        "public_beta_semantic_20": semantic_manifest_execution_gate(
            public_semantic_manifest,
            public_beta_evaluated,
            suite_label="public_beta_semantic_20",
            require_all_prompts_release_counted=True,
        ),
        "blind_holdout_12": semantic_manifest_execution_gate(
            holdout_manifest,
            holdout_evaluated,
            suite_label="blind_holdout_12",
            require_all_prompts_release_counted=True,
        ),
    }
    anti_overfit_scan = run_semantic_anti_overfit_scan(
        holdout_manifest=blind_holdout_manifest
    )
    manual_trace_audit_gate = _manual_trace_audit_gate(
        manual_audit_manifest,
        required=True,
    )
    evaluated_runs = [
        *regression_evaluated,
        *public_beta_evaluated,
        *holdout_evaluated,
    ]
    semantic_release_report = _semantic_release_report(
        evaluated_runs=evaluated_runs,
        report_path=semantic_report_path,
        generated_at=generated_at,
        manual_audit_manifest=manual_audit_manifest,
        require_manual_trace_audits=True,
    )
    holdout_selector_ready = holdout_manifest.get("release_gate_ready") is True
    valid = (
        all(gate.get("valid") is True for gate in suite_gates.values())
        and holdout_selector_ready
        and anti_overfit_scan.get("valid") is True
        and manual_trace_audit_gate.get("valid") is True
    )
    results = {
        "schema_version": "codex-deepresearch.semantic-release-validation.v0",
        "artifact_type": "semantic_release_validation_results",
        "suite_id": suite_id,
        "suite_dir": str(suite_dir.resolve()),
        "generated_at": generated_at,
        "status": "passed" if valid else "failed",
        "valid": valid,
        "release_gate_ready": valid,
        "public_safe": True,
        "denominator_rules": {
            "semantic_regression_30_requires_100_percent_pass": True,
            "public_beta_semantic_20_requires_100_percent_pass": True,
            "blind_holdout_12_requires_100_percent_pass": True,
            "fixture_manual_heuristic_release_ineligible_count_as_failures": True,
            "missing_run_entries_fail_gate": True,
        },
        "manifests": {
            "semantic_regression": {
                "path": str(Path(semantic_regression_manifest).resolve()),
                "prompt_count": len(regression_manifest["prompts"]),
                "hash": _file_sha256(Path(semantic_regression_manifest)),
            },
            "public_beta_semantic": {
                "path": str(Path(public_beta_semantic_manifest).resolve()),
                "prompt_count": len(public_semantic_manifest["prompts"]),
                "hash": _file_sha256(Path(public_beta_semantic_manifest)),
            },
            "blind_holdout": {
                "path": str(Path(blind_holdout_manifest).resolve()),
                "prompt_count": len(holdout_manifest["prompts"]),
                "hash": _file_sha256(Path(blind_holdout_manifest)),
                "selector_release_gate_ready": holdout_selector_ready,
                "selector_failures": list(holdout_manifest.get("failures", [])),
            },
        },
        "semantic_suite_gates": suite_gates,
        "anti_overfit_scan": anti_overfit_scan,
        "manual_trace_audit_gate": manual_trace_audit_gate,
        "semantic_release_report": semantic_release_report,
        "runs": {
            "semantic_regression_30": regression_evaluated,
            "public_beta_semantic_20": public_beta_evaluated,
            "blind_holdout_12": holdout_evaluated,
        },
        "remaining_gaps": _semantic_release_validation_gaps(
            suite_gates=suite_gates,
            holdout_manifest=holdout_manifest,
            anti_overfit_scan=anti_overfit_scan,
            manual_trace_audit_gate=manual_trace_audit_gate,
        ),
        "artifacts": {
            "results": str(results_path.resolve()),
            "semantic_release_report": str(semantic_report_path.resolve()),
        },
    }
    _write_json(semantic_report_path, semantic_release_report)
    _write_json(results_path, results)
    if not valid:
        raise PublicBetaValidationError(
            f"semantic release validation failed; see {results_path}",
            results_path=results_path,
        )
    return results


def _evaluate_semantic_manifest_prompt_runs(
    manifest: Mapping[str, Any],
    prompt_runs: Mapping[str, str | Path] | None,
    *,
    suite_id: str,
    validation_time: str,
) -> list[dict[str, Any]]:
    prompts = {
        str(prompt.get("id")): prompt
        for prompt in manifest.get("prompts", [])
        if isinstance(prompt, Mapping) and prompt.get("id")
    }
    normalized_runs = _normalize_mapping(prompt_runs)
    evaluated: list[dict[str, Any]] = []
    for prompt_id in sorted(set(normalized_runs) - set(prompts)):
        evaluated.append(
            {
                "id": prompt_id,
                "prompt_id": prompt_id,
                "status": "failed",
                "terminal_status": "unknown_prompt_id",
                "metric_classification": "included_failure",
                "failure_category": "artifact_handoff_failure",
                "failure_detail": "unknown semantic manifest prompt id",
                "status_artifacts": {},
                "semantic_release_checks": {"valid": False, "failures": []},
            }
        )
    for prompt_id, run_dir in sorted(normalized_runs.items()):
        prompt = prompts.get(prompt_id)
        if prompt is None:
            continue
        evaluated.append(
            evaluate_public_beta_prompt_run(
                prompt,
                run_dir,
                suite_id=suite_id,
                validation_time=validation_time,
            )
        )
    return evaluated


def _semantic_release_validation_gaps(
    *,
    suite_gates: Mapping[str, Mapping[str, Any]],
    holdout_manifest: Mapping[str, Any],
    anti_overfit_scan: Mapping[str, Any],
    manual_trace_audit_gate: Mapping[str, Any],
) -> list[str]:
    gaps: list[str] = []
    for gate_id, gate in sorted(suite_gates.items()):
        if gate.get("valid") is not True:
            failures = gate.get("failures")
            detail = "; ".join(str(item) for item in failures) if isinstance(failures, list) else "not ready"
            gaps.append(f"{gate_id} failed semantic suite gate: {detail}.")
    if holdout_manifest.get("release_gate_ready") is not True:
        failures = holdout_manifest.get("failures")
        detail = "; ".join(str(item) for item in failures) if isinstance(failures, list) else "not ready"
        gaps.append(f"blind holdout selector/provenance is not release-ready: {detail}.")
    if anti_overfit_scan.get("valid") is not True:
        gaps.append(
            "semantic anti-overfit scan failed: "
            + str(len(anti_overfit_scan.get("findings", [])))
            + " finding(s)."
        )
    if manual_trace_audit_gate.get("valid") is not True:
        failures = manual_trace_audit_gate.get("failures")
        detail = "; ".join(str(item) for item in failures) if isinstance(failures, list) else "not ready"
        gaps.append(f"manual trace audits are not release-ready: {detail}.")
    if not gaps:
        gaps.append("No remaining semantic release validation gaps were detected.")
    return gaps


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


def load_semantic_regression_manifest(
    path: str | Path = DEFAULT_SEMANTIC_REGRESSION_MANIFEST,
) -> dict[str, Any]:
    """Load the oracle-backed 30-prompt semantic regression manifest."""

    return _load_semantic_prompt_manifest(
        path,
        expected_schema=SEMANTIC_REGRESSION_MANIFEST_SCHEMA_VERSION,
        min_prompts=30,
        category_quotas=SEMANTIC_REQUIRED_REGRESSION_CATEGORIES,
        manifest_kind="semantic regression",
        require_release_counted=True,
    )


def load_public_beta_semantic_manifest(
    path: str | Path = DEFAULT_PUBLIC_BETA_SEMANTIC_MANIFEST,
) -> dict[str, Any]:
    """Load the pre-registered 20-run Public Beta semantic manifest."""

    manifest = _load_semantic_prompt_manifest(
        path,
        expected_schema=PUBLIC_BETA_SEMANTIC_MANIFEST_SCHEMA_VERSION,
        min_prompts=20,
        category_quotas=SEMANTIC_REQUIRED_PUBLIC_BETA_CATEGORIES,
        manifest_kind="public beta semantic",
        require_release_counted=True,
    )
    historical = [
        prompt
        for prompt in manifest["prompts"]
        if "historical_failure" in set(prompt.get("categories", []))
        and re.search(r"(?i)ev|battery|fire|thermal", prompt.get("prompt", ""))
    ]
    if not historical:
        raise PublicBetaValidationError(
            "public beta semantic manifest must include an EV battery fire safety historical failure prompt"
        )
    return manifest


def load_blind_holdout_manifest(
    path: str | Path = DEFAULT_BLIND_HOLDOUT_MANIFEST,
    *,
    known_manifests: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Load blind holdout metadata and report honest release readiness."""

    manifest_path = Path(path)
    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != BLIND_HOLDOUT_MANIFEST_SCHEMA_VERSION:
        raise PublicBetaValidationError(
            "blind holdout manifest has unsupported schema_version"
        )
    prompts = manifest.get("prompts")
    if not isinstance(prompts, list):
        raise PublicBetaValidationError("blind holdout manifest prompts must be a list")
    checked = _validate_semantic_prompt_entries(
        manifest_path=manifest_path,
        prompts=prompts,
        min_prompts=12,
        category_quotas=SEMANTIC_REQUIRED_HOLDOUT_CATEGORIES,
        manifest_kind="blind holdout",
        require_release_counted=False,
    )
    freeze = manifest.get("implementation_freeze")
    selector = manifest.get("selector_provenance")
    failures: list[str] = []
    if not isinstance(freeze, Mapping) or not (
        _non_empty_manifest_string(freeze.get("timestamp"))
        or _non_empty_manifest_string(freeze.get("commit"))
        or _non_empty_manifest_string(freeze.get("tag"))
    ):
        failures.append("implementation_freeze_missing")
    if not isinstance(selector, Mapping):
        failures.append("selector_provenance_missing")
        selector = {}
    elif selector.get("independent") is not True:
        failures.append("selector_not_independent")
    if selector.get("release_eligible") is not True:
        failures.append("selector_not_release_eligible")
    selector_audit = _blind_holdout_selector_audit_failures(
        manifest_path=manifest_path,
        selector=selector,
        freeze=freeze if isinstance(freeze, Mapping) else {},
        prompts=checked["prompts"],
        selection_checks=manifest.get("selection_checks"),
    )
    failures.extend(selector_audit)
    overlap_failures = _holdout_overlap_failures(
        holdout_prompts=checked["prompts"],
        known_manifests=known_manifests or [],
    )
    failures.extend(overlap_failures)
    release_gate_ready = not failures
    return {
        **manifest,
        "prompts": checked["prompts"],
        "category_counts": checked["category_counts"],
        "category_quotas": checked["category_quotas"],
        "valid": release_gate_ready,
        "release_gate_ready": release_gate_ready,
        "failures": failures,
    }


def _blind_holdout_selector_audit_failures(
    *,
    manifest_path: Path,
    selector: Mapping[str, Any],
    freeze: Mapping[str, Any],
    prompts: Sequence[Mapping[str, Any]],
    selection_checks: Any,
) -> list[str]:
    failures: list[str] = []
    audit_path_value = str(selector.get("audit_artifact_path") or "").strip()
    audit_hash_value = str(selector.get("audit_artifact_hash") or "").strip()
    if not audit_path_value:
        return ["selector_audit_artifact_path_missing"]
    if not _sha256_hex_string(audit_hash_value):
        failures.append("selector_audit_artifact_hash_invalid")
    audit_path = (manifest_path.parent / audit_path_value).resolve()
    try:
        audit_path.relative_to(manifest_path.parent.resolve())
    except ValueError:
        failures.append("selector_audit_artifact_outside_manifest_dir")
        return failures
    if not audit_path.exists():
        failures.append("selector_audit_artifact_missing")
        return failures
    if _sha256_hex_string(audit_hash_value) and _file_sha256(audit_path) != audit_hash_value:
        failures.append("selector_audit_artifact_hash_mismatch")
    try:
        audit = _read_json(audit_path)
    except (OSError, json.JSONDecodeError):
        failures.append("selector_audit_artifact_unreadable")
        return failures
    if audit.get("schema_version") != BLIND_HOLDOUT_SELECTOR_AUDIT_SCHEMA_VERSION:
        failures.append("selector_audit_schema_version_invalid")
    if audit.get("release_eligible") is not True:
        failures.append("selector_audit_not_release_eligible")
    if audit.get("selector_independent") is not True:
        failures.append("selector_audit_not_independent")
    freeze_commit = str(freeze.get("commit") or "").strip()
    audit_freeze = audit.get("implementation_freeze")
    if freeze_commit and (
        not isinstance(audit_freeze, Mapping)
        or str(audit_freeze.get("commit") or "").strip() != freeze_commit
    ):
        failures.append("selector_audit_freeze_commit_mismatch")
    prompt_ids = [str(prompt.get("id") or "") for prompt in prompts]
    if audit.get("prompt_ids") != prompt_ids:
        failures.append("selector_audit_prompt_ids_mismatch")
    if audit.get("prompt_count") != len(prompt_ids):
        failures.append("selector_audit_prompt_count_mismatch")
    audit_checks = audit.get("selection_checks")
    manifest_checks = selection_checks if isinstance(selection_checks, Mapping) else {}
    if not isinstance(audit_checks, Mapping):
        failures.append("selector_audit_selection_checks_missing")
    else:
        for field in (
            "exact_prompt_string_scan_found_matches",
            "distinctive_keyword_scan_found_matches",
            "known_manifest_overlap_detected",
            "selected_after_implementation_freeze",
        ):
            if audit_checks.get(field) != manifest_checks.get(field):
                failures.append(f"selector_audit_selection_check_mismatch:{field}")
        if audit_checks.get("oracle_hashes_verified") is not True:
            failures.append("selector_audit_oracle_hashes_not_verified")
    failures.extend(
        _blind_holdout_selector_raw_transcript_failures(
            manifest_path=manifest_path,
            selector=selector,
            freeze=freeze,
            prompt_ids=prompt_ids,
        )
    )
    return failures


def _blind_holdout_selector_raw_transcript_failures(
    *,
    manifest_path: Path,
    selector: Mapping[str, Any],
    freeze: Mapping[str, Any],
    prompt_ids: Sequence[str],
) -> list[str]:
    failures: list[str] = []
    transcript_path_value = str(
        selector.get("raw_selector_transcript_path") or ""
    ).strip()
    transcript_hash_value = str(
        selector.get("raw_selector_transcript_hash") or ""
    ).strip()
    if not transcript_path_value:
        return ["selector_raw_transcript_path_missing"]
    if not _sha256_hex_string(transcript_hash_value):
        failures.append("selector_raw_transcript_hash_invalid")
    transcript_path = (manifest_path.parent / transcript_path_value).resolve()
    try:
        transcript_path.relative_to(manifest_path.parent.resolve())
    except ValueError:
        failures.append("selector_raw_transcript_outside_manifest_dir")
        return failures
    if not transcript_path.exists():
        failures.append("selector_raw_transcript_missing")
        return failures
    if (
        _sha256_hex_string(transcript_hash_value)
        and _file_sha256(transcript_path) != transcript_hash_value
    ):
        failures.append("selector_raw_transcript_hash_mismatch")
    try:
        transcript = _read_json(transcript_path)
    except (OSError, json.JSONDecodeError):
        failures.append("selector_raw_transcript_unreadable")
        return failures
    if transcript.get("schema_version") != BLIND_HOLDOUT_SELECTOR_TRANSCRIPT_SCHEMA_VERSION:
        failures.append("selector_raw_transcript_schema_version_invalid")
    if transcript.get("artifact_type") != "blind_holdout_selector_raw_transcript":
        failures.append("selector_raw_transcript_artifact_type_invalid")
    if transcript.get("release_eligible") is not True:
        failures.append("selector_raw_transcript_not_release_eligible")
    if transcript.get("selector_independent") is not True:
        failures.append("selector_raw_transcript_not_independent")
    freeze_commit = str(freeze.get("commit") or "").strip()
    transcript_freeze = transcript.get("implementation_freeze")
    if freeze_commit and (
        not isinstance(transcript_freeze, Mapping)
        or str(transcript_freeze.get("commit") or "").strip() != freeze_commit
    ):
        failures.append("selector_raw_transcript_freeze_commit_mismatch")
    raw_request = transcript.get("raw_request")
    raw_response = transcript.get("raw_response")
    if not isinstance(raw_request, Mapping):
        failures.append("selector_raw_transcript_raw_request_missing")
    if not isinstance(raw_response, Mapping):
        failures.append("selector_raw_transcript_raw_response_missing")
        return failures
    selected_prompt_ids = raw_response.get("selected_prompt_ids")
    if selected_prompt_ids != list(prompt_ids):
        failures.append("selector_raw_transcript_prompt_ids_mismatch")
    if raw_response.get("prompt_count") != len(prompt_ids):
        failures.append("selector_raw_transcript_prompt_count_mismatch")
    if raw_response.get("selection_completed") is not True:
        failures.append("selector_raw_transcript_selection_not_completed")
    return failures


def load_manual_trace_audits(
    path: str | Path = DEFAULT_MANUAL_TRACE_AUDIT_MANIFEST,
) -> dict[str, Any]:
    """Load and gate risk-based manual trace audits for semantic release."""

    manifest_path = Path(path)
    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != MANUAL_TRACE_AUDIT_MANIFEST_SCHEMA_VERSION:
        raise PublicBetaValidationError(
            "manual trace audit manifest has unsupported schema_version"
        )
    failures: list[str] = []
    if manifest.get("public_safe") is not True:
        failures.append("manifest_not_public_safe")
    if manifest.get("live_release_audits_performed") is not True:
        failures.append("live_release_audits_not_performed")
    risk_selection = manifest.get("risk_selection")
    if not isinstance(risk_selection, Mapping):
        failures.append("risk_selection_missing")
        risk_selection = {}
    elif risk_selection.get("hardest_highest_risk") is not True:
        failures.append("risk_selection_not_hardest_highest_risk")
    audits = manifest.get("audits")
    if not isinstance(audits, list):
        raise PublicBetaValidationError("manual trace audit manifest audits must be a list")
    checked_audits: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()
    for index, audit in enumerate(audits, start=1):
        if not isinstance(audit, Mapping):
            raise PublicBetaValidationError("manual trace audit entries must be objects")
        audit_id = str(audit.get("id") or f"audit_{index:03d}")
        audit_failures = _manual_trace_audit_failures(
            manifest_path=manifest_path,
            audit=audit,
            audit_id=audit_id,
        )
        failures.extend(audit_failures)
        categories = audit.get("categories")
        if isinstance(categories, list):
            category_counts.update(
                str(category)
                for category in categories
                if isinstance(category, str) and category.strip()
            )
        if audit.get("route") in VISUAL_ROUTES:
            category_counts.update(["visual"])
        checked_audits.append(
            {
                "id": audit_id,
                "prompt_id": audit.get("prompt_id"),
                "route": audit.get("route"),
                "categories": list(categories) if isinstance(categories, list) else [],
                "risk_rank": audit.get("risk_rank"),
                "release_eligible": audit.get("release_eligible"),
                "fixture_only": audit.get("fixture_only"),
                "chain_steps_verified": [
                    step
                    for step in MANUAL_TRACE_AUDIT_REQUIRED_CHAIN
                    if isinstance(audit.get("chain_proof"), Mapping)
                    and isinstance(audit["chain_proof"].get(step), Mapping)
                    and audit["chain_proof"][step].get("status") == "verified"
                ],
                "failures": audit_failures,
            }
        )
    if len(audits) < MANUAL_TRACE_AUDIT_MIN_COUNT:
        failures.append(
            f"manual_trace_audit_count={len(audits)} < {MANUAL_TRACE_AUDIT_MIN_COUNT}"
        )
    korean_count = category_counts.get("korean", 0)
    visual_count = category_counts.get("visual", 0)
    if korean_count < MANUAL_TRACE_AUDIT_MIN_KOREAN:
        failures.append(
            f"manual_trace_korean_count={korean_count} < {MANUAL_TRACE_AUDIT_MIN_KOREAN}"
        )
    if visual_count < MANUAL_TRACE_AUDIT_MIN_VISUAL:
        failures.append(
            f"manual_trace_visual_count={visual_count} < {MANUAL_TRACE_AUDIT_MIN_VISUAL}"
        )
    valid = not failures
    return {
        **manifest,
        "valid": valid,
        "release_gate_ready": valid,
        "status": "passed" if valid else "failed",
        "manifest_path": str(manifest_path.resolve()),
        "audit_count": len(audits),
        "category_counts": dict(sorted(category_counts.items())),
        "category_quotas": {
            "korean": MANUAL_TRACE_AUDIT_MIN_KOREAN,
            "visual": MANUAL_TRACE_AUDIT_MIN_VISUAL,
        },
        "required_chain": list(MANUAL_TRACE_AUDIT_REQUIRED_CHAIN),
        "failures": failures,
        "audits": checked_audits,
    }


def semantic_manifest_execution_gate(
    manifest: Mapping[str, Any],
    evaluated_runs: Sequence[Mapping[str, Any]],
    *,
    suite_label: str | None = None,
    require_all_prompts_release_counted: bool = True,
) -> dict[str, Any]:
    """Require every release-counted semantic manifest prompt to pass exactly once."""

    prompts = manifest.get("prompts")
    if not isinstance(prompts, list):
        raise PublicBetaValidationError("semantic manifest execution gate needs prompts")
    label = suite_label or str(manifest.get("name") or "semantic_manifest")
    prompt_ids = [
        str(prompt.get("id"))
        for prompt in prompts
        if isinstance(prompt, Mapping) and prompt.get("id")
    ]
    non_release_counted = [
        str(prompt.get("id"))
        for prompt in prompts
        if isinstance(prompt, Mapping) and prompt.get("release_counted") is not True
    ]
    if require_all_prompts_release_counted:
        expected_ids = list(prompt_ids)
    else:
        expected_ids = [
            str(prompt.get("id"))
            for prompt in prompts
            if isinstance(prompt, Mapping)
            and prompt.get("release_counted") is True
            and prompt.get("id")
        ]
    run_by_id: dict[str, Mapping[str, Any]] = {}
    duplicate_ids: list[str] = []
    for run in evaluated_runs:
        if not isinstance(run, Mapping):
            continue
        run_id = run.get("id") or run.get("prompt_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        if run_id in run_by_id:
            duplicate_ids.append(run_id)
        run_by_id[run_id] = run
    expected_set = set(expected_ids)
    run_ids = set(run_by_id)
    missing_ids = sorted(expected_set - run_ids)
    extra_ids = sorted(run_ids - expected_set)
    run_results: list[dict[str, Any]] = []
    for prompt_id in expected_ids:
        run = run_by_id.get(prompt_id)
        if run is None:
            run_results.append(
                {
                    "prompt_id": prompt_id,
                    "status": "missing",
                    "release_counted_status": "denominator_failure",
                    "semantic_release_ready": False,
                    "failure_reason": "missing release-counted semantic run",
                }
            )
            continue
        ready, reason = _semantic_manifest_run_ready(run)
        run_results.append(
            {
                "prompt_id": prompt_id,
                "status": run.get("status"),
                "terminal_status": run.get("terminal_status"),
                "metric_classification": run.get("metric_classification"),
                "release_counted_status": "passed"
                if ready
                else "denominator_failure",
                "semantic_release_ready": ready,
                "failure_reason": reason,
                "artifact_paths": dict(run.get("status_artifacts", {}))
                if isinstance(run.get("status_artifacts"), Mapping)
                else dict(run.get("artifact_paths", {}))
                if isinstance(run.get("artifact_paths"), Mapping)
                else {},
            }
        )
    failures: list[str] = []
    if require_all_prompts_release_counted and non_release_counted:
        failures.append(
            "non_release_counted_prompt_ids:" + ",".join(sorted(non_release_counted))
        )
    if not expected_ids:
        failures.append("no_release_counted_prompt_ids")
    if duplicate_ids:
        failures.append("duplicate_run_entries:" + ",".join(sorted(set(duplicate_ids))))
    if extra_ids:
        failures.append("unknown_run_entries:" + ",".join(extra_ids))
    if missing_ids:
        failures.append("missing_run_entries:" + ",".join(missing_ids))
    denominator_failures = [
        result["prompt_id"]
        for result in run_results
        if result.get("semantic_release_ready") is not True
    ]
    if denominator_failures:
        failures.append("denominator_failures:" + ",".join(denominator_failures))
    valid = not failures
    return {
        "schema_version": "codex-deepresearch.semantic-manifest-execution-gate.v0",
        "suite_label": label,
        "valid": valid,
        "release_gate_ready": valid,
        "denominator_rule": SEMANTIC_MANIFEST_DENOMINATOR_RULE,
        "fixture_manual_heuristic_release_ineligible_count_as_failures": True,
        "expected_prompt_count": len(expected_ids),
        "represented_prompt_count": len(expected_set & run_ids),
        "passed_prompt_count": sum(
            1 for result in run_results if result.get("semantic_release_ready") is True
        ),
        "failed_prompt_count": sum(
            1 for result in run_results if result.get("semantic_release_ready") is not True
        ),
        "missing_prompt_ids": missing_ids,
        "duplicate_prompt_ids": sorted(set(duplicate_ids)),
        "extra_prompt_ids": extra_ids,
        "non_release_counted_prompt_ids": sorted(non_release_counted),
        "failures": failures,
        "runs": run_results,
    }


def run_semantic_anti_overfit_scan(
    *,
    repo_root: str | Path | None = None,
    holdout_manifest: str | Path = DEFAULT_BLIND_HOLDOUT_MANIFEST,
    scan_paths: Sequence[str | Path] | None = None,
) -> dict[str, Any]:
    """Scan planner-facing files for prompt-specific overfit hooks."""

    root = Path(repo_root) if repo_root is not None else PLUGIN_ROOT.parents[1]
    try:
        holdout = load_blind_holdout_manifest(holdout_manifest)
    except PublicBetaValidationError:
        holdout = _read_json(holdout_manifest)
    prompt_texts = [
        str(prompt.get("prompt") or "")
        for prompt in holdout.get("prompts", [])
        if isinstance(prompt, Mapping)
    ]
    prompt_hashes = {_prompt_hash(prompt) for prompt in prompt_texts if prompt.strip()}
    if scan_paths is None:
        scan_paths = [
            PLUGIN_ROOT / "src" / "deepresearch" / "semantic_planner.py",
            PLUGIN_ROOT / "skills",
            root / "tests" / "fixtures",
        ]
    files = _semantic_scan_files(scan_paths)
    findings: list[dict[str, Any]] = []
    holdout_manifest_path = Path(holdout_manifest).resolve()
    for path in files:
        resolved = path.resolve()
        if resolved == holdout_manifest_path:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        normalized = _normalize_prompt_text(text)
        for prompt in prompt_texts:
            if prompt and _normalize_prompt_text(prompt) in normalized:
                findings.append(
                    {
                        "code": "known_holdout_string",
                        "path": str(path),
                        "detail": "holdout prompt text appears in scanned planner-facing files",
                    }
                )
                break
        for prompt_hash in prompt_hashes:
            if prompt_hash and prompt_hash in text:
                findings.append(
                    {
                        "code": "known_holdout_prompt_hash",
                        "path": str(path),
                        "detail": "holdout prompt hash appears in scanned planner-facing files",
                    }
                )
                break
        if re.search(
            r"(?is)\b(if|elif)\b.{0,160}prompt_hash.{0,160}"
            r"(semantic_plan|bounded_tasks|expected_plan|angle|route)",
            text,
        ):
            findings.append(
                {
                    "code": "prompt_hash_routing",
                    "path": str(path),
                    "detail": "planner-facing file appears to branch semantic planning on prompt_hash",
                }
            )
        alias_findings = _semantic_prompt_hash_alias_routing_findings(text)
        for code in alias_findings:
            findings.append(
                {
                    "code": code,
                    "path": str(path),
                    "detail": "planner-facing file appears to route semantic planning through a prompt hash alias",
                }
            )
        if re.search(
            r"(?i)\b(expected_plan|expected_bounded_tasks|canned_expected_plan|golden_semantic_plan)\b",
            text,
        ):
            findings.append(
                {
                    "code": "canned_expected_plan",
                    "path": str(path),
                    "detail": "planner-facing file contains canned expected-plan marker",
                }
            )
    return {
        "schema_version": "codex-deepresearch.semantic-anti-overfit-scan.v0",
        "valid": not findings,
        "status": "passed" if not findings else "failed",
        "scan_file_count": len(files),
        "findings": findings,
    }


def _semantic_prompt_hash_alias_routing_findings(text: str) -> list[str]:
    alias_names: set[str] = set()
    for match in re.finditer(
        r"(?im)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*prompt_hash\b",
        text,
    ):
        alias_names.add(match.group(1))
    for match in re.finditer(
        r"(?is)\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[^=\n]{0,120}"
        r"sha256\s*\(\s*prompt\b[^;\n]{0,200}hexdigest\s*\(",
        text,
    ):
        alias_names.add(match.group(1))
    hash_helper = (
        r"(?:public_beta_prompt_hash|_prompt_hash|prompt_hash_for_question|"
        r"normalized_question_hash|question_hash|hash_prompt|hash_question)"
    )
    for match in re.finditer(
        rf"(?is)\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*{hash_helper}\s*\(",
        text,
    ):
        alias_names.add(match.group(1))
    for match in re.finditer(
        rf"(?is)\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[^=\n]{{0,160}}"
        rf"sha256\s*\(\s*(?:normalize_public_beta_prompt_text|normalize_question|"
        rf"_normalize_prompt_text)\s*\([^;\n]{{0,200}}hexdigest\s*\(",
        text,
    ):
        alias_names.add(match.group(1))

    findings: list[str] = []
    semantic_target = r"(semantic_plan|bounded_tasks|expected_plan|angle|route)"
    for alias in sorted(alias_names):
        branches_on_alias = re.search(
            rf"(?is)\b(if|elif|match)\b.{{0,180}}\b{re.escape(alias)}\b"
            rf".{{0,260}}{semantic_target}",
            text,
        )
        lookup_by_alias = re.search(
            rf"(?is){semantic_target}\s*=\s*.{{0,260}}\[[^\]]*"
            rf"\b{re.escape(alias)}\b[^\]]*\]",
            text,
        )
        if branches_on_alias or lookup_by_alias:
            findings.append("prompt_hash_alias_routing")
            break
    if re.search(
        rf"(?is)\b(if|elif|match)\b.{{0,220}}sha256\s*\(\s*prompt\b"
        rf".{{0,280}}{semantic_target}",
        text,
    ):
        findings.append("prompt_sha256_routing")
    if re.search(
        rf"(?is){semantic_target}\s*=\s*.{{0,260}}\[[^\]]*"
        rf"(?:{hash_helper}\s*\(|sha256\s*\()[^\]]*\]",
        text,
    ):
        findings.append("prompt_hash_alias_routing")
    if re.search(
        rf"(?is)\b(?:plan|route|angle|task|bounded_tasks|semantic_plan)_by_"
        rf"(?:hash|digest)\b.{{0,420}}(?:prompt_hash|{hash_helper}\s*\(|sha256\s*\()"
        rf".{{0,420}}{semantic_target}",
        text,
    ):
        findings.append("prompt_hash_alias_routing")
    if re.search(
        rf"(?is)\{{.{{0,260}}(?:{hash_helper}\s*\(|sha256\s*\()"
        rf".{{0,420}}{semantic_target}",
        text,
    ):
        findings.append("prompt_hash_alias_routing")
    return findings


def _load_semantic_prompt_manifest(
    path: str | Path,
    *,
    expected_schema: str,
    min_prompts: int,
    category_quotas: Mapping[str, int],
    manifest_kind: str,
    require_release_counted: bool,
) -> dict[str, Any]:
    manifest_path = Path(path)
    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != expected_schema:
        raise PublicBetaValidationError(
            f"{manifest_kind} manifest has unsupported schema_version"
        )
    prompts = manifest.get("prompts")
    if not isinstance(prompts, list):
        raise PublicBetaValidationError(f"{manifest_kind} manifest prompts must be a list")
    checked = _validate_semantic_prompt_entries(
        manifest_path=manifest_path,
        prompts=prompts,
        min_prompts=min_prompts,
        category_quotas=category_quotas,
        manifest_kind=manifest_kind,
        require_release_counted=require_release_counted,
    )
    return {
        **manifest,
        "prompts": checked["prompts"],
        "category_counts": checked["category_counts"],
        "category_quotas": checked["category_quotas"],
        "valid": True,
    }


def _validate_semantic_prompt_entries(
    *,
    manifest_path: Path,
    prompts: Sequence[Any],
    min_prompts: int,
    category_quotas: Mapping[str, int],
    manifest_kind: str,
    require_release_counted: bool,
) -> dict[str, Any]:
    if len(prompts) < min_prompts:
        raise PublicBetaValidationError(
            f"{manifest_kind} manifest must contain at least {min_prompts} prompts"
        )
    seen: set[str] = set()
    checked_prompts: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()
    for prompt in prompts:
        if not isinstance(prompt, Mapping):
            raise PublicBetaValidationError(f"{manifest_kind} prompt entries must be objects")
        prompt_id = prompt.get("id")
        if not isinstance(prompt_id, str) or not prompt_id.strip():
            raise PublicBetaValidationError(f"{manifest_kind} prompt entry needs id")
        if prompt_id in seen:
            raise PublicBetaValidationError(f"duplicate {manifest_kind} prompt id: {prompt_id}")
        seen.add(prompt_id)
        if prompt.get("route") not in {"text_only", "visual_required", "visual_optional"}:
            raise PublicBetaValidationError(f"{manifest_kind} prompt {prompt_id} has invalid route")
        if prompt.get("public_safe") is not True:
            raise PublicBetaValidationError(f"{manifest_kind} prompt {prompt_id} must be public_safe=true")
        if not _non_empty_manifest_string(prompt.get("prompt")):
            raise PublicBetaValidationError(f"{manifest_kind} prompt {prompt_id} needs prompt text")
        categories = prompt.get("categories")
        if not isinstance(categories, list) or not all(
            isinstance(category, str) and category.strip() for category in categories
        ):
            raise PublicBetaValidationError(f"{manifest_kind} prompt {prompt_id} needs categories")
        category_counts.update(str(category) for category in categories)
        if prompt.get("route") in VISUAL_ROUTES:
            category_counts.update(["visual"])
        if require_release_counted and prompt.get("release_counted") is not True:
            raise PublicBetaValidationError(
                f"{manifest_kind} prompt {prompt_id} must be release_counted=true"
            )
        if prompt.get("denominator_rule") != SEMANTIC_MANIFEST_DENOMINATOR_RULE:
            raise PublicBetaValidationError(
                f"{manifest_kind} prompt {prompt_id} has invalid denominator_rule"
            )
        oracle_path = prompt.get("oracle_path")
        oracle_hash = prompt.get("oracle_hash")
        if not _non_empty_manifest_string(oracle_path):
            raise PublicBetaValidationError(f"{manifest_kind} prompt {prompt_id} needs oracle_path")
        if not isinstance(oracle_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}",
            oracle_hash,
        ):
            raise PublicBetaValidationError(f"{manifest_kind} prompt {prompt_id} needs sha256 oracle_hash")
        expected_prompt_hash = _prompt_hash(str(prompt["prompt"]))
        if prompt.get("prompt_hash") not in {None, expected_prompt_hash}:
            raise PublicBetaValidationError(
                f"{manifest_kind} prompt {prompt_id} prompt_hash does not match prompt text"
            )
        oracle_file, oracle_fragment = _semantic_oracle_fragment(
            manifest_path,
            str(oracle_path),
            manifest_kind=manifest_kind,
            prompt_id=prompt_id,
        )
        actual_oracle_hash = _canonical_json_sha256(oracle_fragment)
        if actual_oracle_hash != oracle_hash:
            raise PublicBetaValidationError(
                f"{manifest_kind} prompt {prompt_id} oracle_hash does not match oracle fragment"
            )
        if oracle_fragment.get("prompt_id") != prompt_id:
            raise PublicBetaValidationError(
                f"{manifest_kind} prompt {prompt_id} oracle fragment prompt_id mismatch"
            )
        if oracle_fragment.get("prompt_hash") != expected_prompt_hash:
            raise PublicBetaValidationError(
                f"{manifest_kind} prompt {prompt_id} oracle fragment prompt_hash mismatch"
            )
        if oracle_fragment.get("route") != prompt.get("route"):
            raise PublicBetaValidationError(
                f"{manifest_kind} prompt {prompt_id} oracle fragment route mismatch"
            )
        if set(oracle_fragment.get("categories", [])) != set(categories):
            raise PublicBetaValidationError(
                f"{manifest_kind} prompt {prompt_id} oracle fragment categories mismatch"
            )
        checked = dict(prompt)
        checked["prompt_hash"] = expected_prompt_hash
        checked["oracle_hash_verified"] = True
        checked["oracle_fragment_path"] = str(oracle_file.resolve())
        checked_prompts.append(checked)
    quota_failures = [
        f"{category}={category_counts.get(category, 0)} < {minimum}"
        for category, minimum in sorted(category_quotas.items())
        if category_counts.get(category, 0) < minimum
    ]
    if quota_failures:
        raise PublicBetaValidationError(
            f"{manifest_kind} manifest misses category quota(s): "
            + "; ".join(quota_failures)
        )
    return {
        "prompts": checked_prompts,
        "category_counts": dict(sorted(category_counts.items())),
        "category_quotas": dict(sorted(category_quotas.items())),
    }


def _oracle_manifest_file(manifest_path: Path, oracle_path: str) -> Path:
    raw_path = oracle_path.split("#", 1)[0]
    path = Path(raw_path)
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path


def _semantic_oracle_fragment(
    manifest_path: Path,
    oracle_path: str,
    *,
    manifest_kind: str,
    prompt_id: str,
) -> tuple[Path, Mapping[str, Any]]:
    oracle_file = _oracle_manifest_file(manifest_path, oracle_path)
    if not oracle_file.is_file():
        raise PublicBetaValidationError(
            f"{manifest_kind} prompt {prompt_id} oracle_path does not exist"
        )
    if "#" not in oracle_path:
        raise PublicBetaValidationError(
            f"{manifest_kind} prompt {prompt_id} oracle_path must name an oracle fragment"
        )
    fragment_id = oracle_path.split("#", 1)[1].strip()
    if not fragment_id:
        raise PublicBetaValidationError(
            f"{manifest_kind} prompt {prompt_id} oracle_path fragment is empty"
        )
    bundle = _read_json(oracle_file)
    if bundle.get("public_safe") is not True:
        raise PublicBetaValidationError(
            f"{manifest_kind} prompt {prompt_id} oracle bundle must be public_safe=true"
        )
    oracles = bundle.get("oracles")
    if not isinstance(oracles, Mapping):
        raise PublicBetaValidationError(
            f"{manifest_kind} prompt {prompt_id} oracle bundle is missing oracles"
        )
    fragment = oracles.get(fragment_id)
    if not isinstance(fragment, Mapping):
        raise PublicBetaValidationError(
            f"{manifest_kind} prompt {prompt_id} oracle fragment does not exist"
        )
    return oracle_file, fragment


def _canonical_json_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _manual_trace_audit_gate(
    audit_manifest: str | Path | None,
    *,
    required: bool,
) -> dict[str, Any]:
    if audit_manifest is None:
        if not required:
            return {
                "schema_version": MANUAL_TRACE_AUDIT_MANIFEST_SCHEMA_VERSION,
                "status": "not_required",
                "valid": True,
                "release_gate_ready": True,
                "required": False,
                "failures": [],
            }
        return {
            "schema_version": MANUAL_TRACE_AUDIT_MANIFEST_SCHEMA_VERSION,
            "status": "not_supplied",
            "valid": False,
            "release_gate_ready": False,
            "required": True,
            "failures": ["manual_trace_audit_manifest_not_supplied"],
        }
    path = Path(audit_manifest)
    if not path.exists():
        return {
            "schema_version": MANUAL_TRACE_AUDIT_MANIFEST_SCHEMA_VERSION,
            "status": "not_supplied",
            "valid": False,
            "release_gate_ready": False,
            "required": required,
            "manifest_path": str(path.resolve()),
            "failures": ["manual_trace_audit_manifest_missing"],
        }
    gate = load_manual_trace_audits(path)
    gate["required"] = required
    return gate


def _manual_trace_audit_failures(
    *,
    manifest_path: Path,
    audit: Mapping[str, Any],
    audit_id: str,
) -> list[str]:
    failures: list[str] = []
    if audit.get("public_safe") is not True:
        failures.append(f"{audit_id}:not_public_safe")
    if audit.get("release_eligible") is not True:
        failures.append(f"{audit_id}:not_release_eligible")
    if audit.get("fixture_only") is True:
        failures.append(f"{audit_id}:fixture_only")
    if audit.get("route") not in {"text_only", "visual_required", "visual_optional"}:
        failures.append(f"{audit_id}:invalid_route")
    categories = audit.get("categories")
    if not isinstance(categories, list) or not all(
        isinstance(category, str) and category.strip() for category in categories
    ):
        failures.append(f"{audit_id}:categories_missing")
    risk_rank = audit.get("risk_rank")
    if isinstance(risk_rank, bool) or not isinstance(risk_rank, int) or risk_rank < 1:
        failures.append(f"{audit_id}:risk_rank_missing")
    if not _non_empty_manifest_string(audit.get("risk_rationale")):
        failures.append(f"{audit_id}:risk_rationale_missing")
    run_id = str(audit.get("run_id") or "").strip()
    prompt_id = str(audit.get("prompt_id") or "").strip()
    if not run_id:
        failures.append(f"{audit_id}:run_id_missing")
    if not prompt_id:
        failures.append(f"{audit_id}:prompt_id_missing")
    for hash_field in (
        "oracle_hash",
        "semantic_plan_hash",
        "materialization_diff_hash",
        "final_report_hash",
    ):
        if not _sha256_hex_string(audit.get(hash_field)):
            failures.append(f"{audit_id}:{hash_field}_missing")
    chain = audit.get("chain_proof")
    if not isinstance(chain, Mapping):
        failures.append(f"{audit_id}:chain_proof_missing")
        return failures
    for step_index, step in enumerate(MANUAL_TRACE_AUDIT_REQUIRED_CHAIN):
        proof = chain.get(step)
        if not isinstance(proof, Mapping):
            failures.append(f"{audit_id}:{step}_proof_missing")
            continue
        if proof.get("status") != "verified":
            failures.append(f"{audit_id}:{step}_not_verified")
        raw_path = proof.get("artifact_path")
        if not _non_empty_manifest_string(raw_path):
            failures.append(f"{audit_id}:{step}_artifact_path_missing")
            continue
        artifact_path = Path(str(raw_path))
        if not artifact_path.is_absolute():
            artifact_path = manifest_path.parent / artifact_path
        if not artifact_path.is_file():
            failures.append(f"{audit_id}:{step}_artifact_missing")
            continue
        artifact_hash = proof.get("artifact_hash")
        if not _sha256_hex_string(artifact_hash):
            failures.append(f"{audit_id}:{step}_artifact_hash_missing")
            continue
        if _file_sha256(artifact_path) != str(artifact_hash):
            failures.append(f"{audit_id}:{step}_artifact_hash_mismatch")
            continue
        payload = _read_optional_json(artifact_path)
        failures.extend(
            _manual_trace_step_content_failures(
                audit=audit,
                audit_id=audit_id,
                step=step,
                step_index=step_index,
                payload=payload,
            )
        )
    return failures


def _manual_trace_step_content_failures(
    *,
    audit: Mapping[str, Any],
    audit_id: str,
    step: str,
    step_index: int,
    payload: Any,
) -> list[str]:
    failures: list[str] = []
    if not isinstance(payload, Mapping):
        return [f"{audit_id}:{step}_artifact_not_structured_json"]
    if payload.get("schema_version") == "codex-deepresearch.manual-trace-step.v0":
        failures.append(f"{audit_id}:{step}_artifact_wrapper_not_allowed")
    failures.extend(
        _manual_trace_step_artifact_shape_failures(
            audit_id=audit_id,
            step=step,
            payload=payload,
        )
    )

    expected_run_id = str(audit.get("run_id") or "").strip()
    expected_prompt_id = str(audit.get("prompt_id") or "").strip()
    trace = payload.get("manual_trace_audit")
    if not isinstance(trace, Mapping):
        failures.append(f"{audit_id}:{step}_manual_trace_audit_missing")
        trace = {}
    for field, expected in (("run_id", expected_run_id), ("prompt_id", expected_prompt_id)):
        if expected and payload.get(field) != expected:
            failures.append(f"{audit_id}:{step}_{field}_mismatch")
        elif not expected:
            failures.append(f"{audit_id}:{step}_{field}_expected_value_missing")
    for field, expected in (
        ("audit_id", audit_id),
        ("step", step),
        ("run_id", expected_run_id),
        ("prompt_id", expected_prompt_id),
    ):
        if expected and trace.get(field) != expected:
            failures.append(f"{audit_id}:{step}_trace_{field}_mismatch")
        elif not expected:
            failures.append(f"{audit_id}:{step}_trace_{field}_expected_value_missing")
    if trace.get("status") != "verified":
        failures.append(f"{audit_id}:{step}_artifact_not_verified")
    if payload.get("public_safe") is not True or trace.get("public_safe") is not True:
        failures.append(f"{audit_id}:{step}_artifact_not_public_safe")
    provenance = payload.get("provenance")
    if payload.get("fixture_only") is True or (
        isinstance(provenance, Mapping) and provenance.get("fixture_only") is True
    ) or trace.get("fixture_only") is True:
        failures.append(f"{audit_id}:{step}_fixture_only")

    previous_step = (
        MANUAL_TRACE_AUDIT_REQUIRED_CHAIN[step_index - 1]
        if step_index > 0
        else None
    )
    next_step = (
        MANUAL_TRACE_AUDIT_REQUIRED_CHAIN[step_index + 1]
        if step_index + 1 < len(MANUAL_TRACE_AUDIT_REQUIRED_CHAIN)
        else None
    )
    if trace.get("previous_step") != previous_step:
        failures.append(f"{audit_id}:{step}_previous_step_mismatch")
    if trace.get("next_step") != next_step:
        failures.append(f"{audit_id}:{step}_next_step_mismatch")

    required_hashes = _manual_trace_required_hash_fields_for_step(step)
    for field in required_hashes:
        expected_hash = str(audit.get(field) or "").strip()
        actual_hash = payload.get(field) or trace.get(field)
        if not _sha256_hex_string(actual_hash):
            failures.append(f"{audit_id}:{step}_{field}_missing")
        elif expected_hash and str(actual_hash) != expected_hash:
            failures.append(f"{audit_id}:{step}_{field}_mismatch")

    if step == "accepted_shards":
        shard_refs = payload.get("accepted_shards") or payload.get("accepted_shard_refs")
        if not isinstance(shard_refs, list) or not shard_refs:
            failures.append(f"{audit_id}:accepted_shards_refs_missing")
        else:
            for index, ref in enumerate(shard_refs, start=1):
                if not isinstance(ref, Mapping):
                    failures.append(f"{audit_id}:accepted_shards_ref_{index}_invalid")
                    continue
                for field in ("task_id", "shard_path"):
                    if not _non_empty_manifest_string(ref.get(field)):
                        failures.append(
                            f"{audit_id}:accepted_shards_ref_{index}_{field}_missing"
                        )
                if not _sha256_hex_string(ref.get("artifact_hash")):
                    failures.append(
                        f"{audit_id}:accepted_shards_ref_{index}_artifact_hash_missing"
                    )
    if step == "final_report":
        alignment = payload.get("final_report_alignment")
        required_alignment = (
            "original_question",
            "locked_oracle",
            "semantic_plan",
            "semantic_review",
            "materialization_diff",
            "accepted_shards",
            "final_answer",
        )
        if not isinstance(alignment, Mapping):
            failures.append(f"{audit_id}:final_report_alignment_missing")
        else:
            for field in required_alignment:
                if alignment.get(field) is not True:
                    failures.append(
                        f"{audit_id}:final_report_alignment_{field}_missing"
                    )
    return failures


def _manual_trace_step_artifact_shape_failures(
    *,
    audit_id: str,
    step: str,
    payload: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    schema = payload.get("schema_version")
    artifact_type = payload.get("artifact_type")
    if step == "original_question":
        if schema != RUN_STATUS_SCHEMA_VERSION:
            failures.append(f"{audit_id}:{step}_artifact_schema_mismatch")
        if not _non_empty_manifest_string(payload.get("original_question")) and not _non_empty_manifest_string(payload.get("question")):
            failures.append(f"{audit_id}:{step}_original_question_missing")
    elif step == "locked_oracle":
        if artifact_type != "semantic_expectation_oracle":
            failures.append(f"{audit_id}:{step}_artifact_type_mismatch")
        if payload.get("plan_visible_to_oracle") is not False:
            failures.append(f"{audit_id}:{step}_oracle_reverse_fit_missing")
    elif step == "semantic_plan":
        if artifact_type != "semantic_plan":
            failures.append(f"{audit_id}:{step}_artifact_type_mismatch")
        semantic_plan = payload.get("semantic_plan")
        if not isinstance(semantic_plan, Mapping) or not isinstance(semantic_plan.get("bounded_tasks"), list) or not semantic_plan.get("bounded_tasks"):
            failures.append(f"{audit_id}:{step}_bounded_tasks_missing")
    elif step == "semantic_review":
        if artifact_type != "semantic_plan_review":
            failures.append(f"{audit_id}:{step}_artifact_type_mismatch")
        if _numeric_or_none(payload.get("semantic_fit_score")) is None:
            failures.append(f"{audit_id}:{step}_semantic_fit_score_missing")
    elif step == "materialization_diff":
        if artifact_type != "semantic_materialization_diff":
            failures.append(f"{audit_id}:{step}_artifact_type_mismatch")
        if payload.get("valid") is not True:
            failures.append(f"{audit_id}:{step}_materialization_diff_not_valid")
    elif step == "research_tasks":
        if schema != "codex-deepresearch.parallel-orchestration.v0":
            failures.append(f"{audit_id}:{step}_artifact_schema_mismatch")
        if not _manual_trace_payload_has_tasks(payload):
            failures.append(f"{audit_id}:{step}_tasks_missing")
    elif step in {"search_tasks", "visual_tasks"}:
        if schema != "codex-deepresearch.search-handoff.v0":
            failures.append(f"{audit_id}:{step}_artifact_schema_mismatch")
        if not _manual_trace_payload_has_tasks(payload):
            failures.append(f"{audit_id}:{step}_tasks_missing")
    elif step == "accepted_shards":
        if schema != "codex-deepresearch.parallel-orchestration.v0":
            failures.append(f"{audit_id}:{step}_artifact_schema_mismatch")
        if payload.get("status") not in {"completed", "completed_parallel"}:
            failures.append(f"{audit_id}:{step}_status_not_completed")
    elif step == "final_report":
        if schema not in {"codex-deepresearch.report-status.v0", "codex-deepresearch.report-generation.v0"}:
            failures.append(f"{audit_id}:{step}_artifact_schema_mismatch")
        if payload.get("status") != "completed":
            failures.append(f"{audit_id}:{step}_status_not_completed")
    return failures


def _manual_trace_payload_has_tasks(payload: Mapping[str, Any]) -> bool:
    tasks = payload.get("tasks")
    return isinstance(tasks, list) and bool(tasks) and all(isinstance(task, Mapping) for task in tasks)


def _manual_trace_required_hash_fields_for_step(step: str) -> tuple[str, ...]:
    if step == "locked_oracle":
        return ("oracle_hash",)
    if step in {"semantic_plan", "semantic_review"}:
        return ("oracle_hash", "semantic_plan_hash")
    if step == "materialization_diff":
        return ("semantic_plan_hash", "materialization_diff_hash")
    if step in {"research_tasks", "search_tasks", "visual_tasks", "accepted_shards"}:
        return ("semantic_plan_hash",)
    if step == "final_report":
        return ("semantic_plan_hash", "materialization_diff_hash", "final_report_hash")
    return ()


def _semantic_manifest_run_ready(run: Mapping[str, Any]) -> tuple[bool, str | None]:
    if run.get("release_counted") is False:
        return False, "run is marked release-ineligible"
    status = run.get("status")
    if status != "passed":
        return False, str(run.get("failure_detail") or status or "not passed")
    terminal_status = str(run.get("terminal_status") or "")
    if terminal_status in SEMANTIC_RELEASE_FAILURE_TERMINAL_STATUSES:
        return False, f"{terminal_status} counts as a denominator failure"
    if terminal_status not in PASS_TERMINAL_STATUSES:
        return False, f"{terminal_status} is not a passing terminal status"
    metric_classification = run.get("metric_classification")
    if metric_classification not in {None, "success"}:
        return False, f"{metric_classification} counts as a denominator failure"
    semantic_checks = run.get("semantic_release_checks")
    if isinstance(semantic_checks, Mapping):
        if semantic_checks.get("valid") is not True:
            return False, "semantic release checks did not pass"
    elif run.get("semantic_release_ready") is not True:
        return False, "semantic release checks are missing"
    if run.get("semantic_release_ready") is False:
        return False, "semantic release report row is not ready"
    return True, None


def _holdout_overlap_failures(
    *,
    holdout_prompts: Sequence[Mapping[str, Any]],
    known_manifests: Sequence[Mapping[str, Any]],
) -> list[str]:
    known_texts: set[str] = set()
    known_hashes: set[str] = set()
    for manifest in known_manifests:
        prompts = manifest.get("prompts") if isinstance(manifest, Mapping) else None
        if not isinstance(prompts, list):
            continue
        for prompt in prompts:
            if not isinstance(prompt, Mapping):
                continue
            text = str(prompt.get("prompt") or "")
            if text:
                known_texts.add(_normalize_prompt_text(text))
                known_hashes.add(_prompt_hash(text))
    failures: list[str] = []
    for prompt in holdout_prompts:
        text = str(prompt.get("prompt") or "")
        normalized = _normalize_prompt_text(text)
        prompt_hash = _prompt_hash(text)
        if normalized in known_texts or prompt_hash in known_hashes:
            failures.append(
                "holdout_prompt_overlap:" + str(prompt.get("id") or "<unknown>")
            )
    return failures


def _semantic_scan_files(paths: Sequence[str | Path]) -> list[Path]:
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            for child in path.rglob("*"):
                if child.is_file() and child.suffix in {".py", ".md", ".json", ".txt"}:
                    files.append(child)
    return sorted(set(files))


def _non_empty_manifest_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


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
            prompt=prompt,
            loaded_artifacts=loaded,
            run_status=run_status if isinstance(run_status, Mapping) else {},
            evidence=evidence if isinstance(evidence, Mapping) else {},
            run_path=run_path,
            artifact_paths=artifacts,
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
    if visual_release_checks is not None and not visual_release_checks["valid"]:
        visual_detail = "visual prompt lacks release-grade evidence: " + "; ".join(
            failure["detail"] for failure in visual_release_checks["failures"]
        )
        visual_category = _visual_failure_category(visual_release_checks["failures"])
        if status != "passed" and detail:
            detail = detail + " | " + visual_detail
            if failure_category == "artifact_handoff_failure":
                failure_category = visual_category
        elif status != "passed":
            detail = visual_detail
            if failure_category == "artifact_handoff_failure":
                failure_category = visual_category
        else:
            detail = visual_detail
            failure_category = visual_category
        status = "failed"
        classification = "included_failure"

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


def _semantic_release_report(
    *,
    evaluated_runs: Sequence[Mapping[str, Any]],
    report_path: Path,
    generated_at: str,
    manual_audit_manifest: str | Path | None = None,
    require_manual_trace_audits: bool = False,
) -> dict[str, Any]:
    report_runs = [
        _semantic_release_report_run(run)
        for run in evaluated_runs
        if isinstance(run, Mapping)
    ]
    missing_entries = [
        str(run.get("id"))
        for run in evaluated_runs
        if not any(item.get("id") == run.get("id") for item in report_runs)
    ]
    manual_trace_audit_gate = _manual_trace_audit_gate(
        manual_audit_manifest,
        required=True,
    )
    valid = (
        not missing_entries
        and bool(report_runs)
        and all(run.get("semantic_release_ready") is True for run in report_runs)
        and manual_trace_audit_gate.get("valid") is True
    )
    return {
        "schema_version": "codex-deepresearch.semantic-release-report.v0",
        "generated_at": generated_at,
        "artifact_type": "semantic_release_report",
        "report_path": str(report_path.resolve()),
        "valid": valid,
        "release_gate_ready": valid,
        "denominator_rules": {
            "fixture_manual_heuristic_release_ineligible_count_as_failures": True,
            "blocked_and_failed_runs_count_as_failures": True,
            "missing_run_entries_fail_gate": True,
            "manual_trace_audits_required_when_claiming_semantic_readiness": True,
        },
        "manual_trace_audit_gate": manual_trace_audit_gate,
        "release_counted_run_count": len(report_runs),
        "ready_run_count": sum(
            1 for run in report_runs if run.get("semantic_release_ready") is True
        ),
        "failed_run_count": sum(
            1 for run in report_runs if run.get("semantic_release_ready") is not True
        ),
        "missing_run_entries": missing_entries,
        "runs": report_runs,
    }


def _semantic_release_report_run(run: Mapping[str, Any]) -> dict[str, Any]:
    status_artifacts = run.get("status_artifacts")
    artifact_paths = status_artifacts if isinstance(status_artifacts, Mapping) else {}
    artifact_hashes = {
        name: _artifact_file_hash(path)
        for name, path in sorted(artifact_paths.items())
        if name
        in {
            "semantic_expectation_oracle",
            "semantic_plan",
            "semantic_plan_review",
            "semantic_planner_validation",
            "semantic_materialization_diff",
            "research_tasks",
            "search_tasks",
            "visual_tasks",
            "visual_search_plan",
            "search_results",
            "visual_candidates",
            "image_fetch_status",
            "visual_observations",
            "subagent_assignments",
            "run_trace",
            "report",
            "report_status",
            "verifier_votes",
        }
    }
    semantic_checks = run.get("semantic_release_checks")
    semantic_failures = (
        semantic_checks.get("failures")
        if isinstance(semantic_checks, Mapping)
        and isinstance(semantic_checks.get("failures"), list)
        else []
    )
    required_names = {
        "semantic_expectation_oracle",
        "semantic_plan",
        "semantic_plan_review",
        "semantic_planner_validation",
        "semantic_materialization_diff",
        "research_tasks",
        "search_tasks",
        "visual_tasks",
        "search_results",
        "subagent_assignments",
        "run_trace",
        "report",
        "report_status",
    }
    if run.get("route") in VISUAL_ROUTES:
        required_names.update(
            {
                "visual_provider_status",
                "visual_search_plan",
                "visual_candidates",
                "image_fetch_status",
                "visual_observations",
                "verifier_votes",
            }
        )
    missing_required = sorted(
        name for name in required_names if name not in artifact_paths
    )
    semantic_ready = (
        run.get("status") == "passed"
        and isinstance(semantic_checks, Mapping)
        and semantic_checks.get("valid") is True
        and not missing_required
    )
    failure_reason = run.get("failure_detail")
    if not semantic_ready and not failure_reason:
        if missing_required:
            failure_reason = "missing semantic release artifact(s): " + ", ".join(
                missing_required
            )
        elif semantic_failures:
            failure_reason = "semantic release checks failed"
        else:
            failure_reason = str(run.get("status") or "not ready")
    return {
        "id": run.get("id"),
        "prompt_id": run.get("id"),
        "route": run.get("route"),
        "status": run.get("status"),
        "terminal_status": run.get("terminal_status"),
        "metric_classification": run.get("metric_classification"),
        "release_counted": True,
        "release_counted_status": (
            "passed" if semantic_ready else "denominator_failure"
        ),
        "semantic_release_ready": semantic_ready,
        "semantic_release_check_valid": (
            semantic_checks.get("valid") if isinstance(semantic_checks, Mapping) else None
        ),
        "holdout_or_regression_class": run.get("holdout_or_regression_class")
        or "public_beta_semantic_manifest",
        "required_artifacts": sorted(required_names),
        "artifact_paths": dict(sorted(artifact_paths.items())),
        "artifact_hashes": artifact_hashes,
        "failure_category": run.get("failure_category"),
        "failure_reason": failure_reason,
        "semantic_failures": semantic_failures,
    }


def _artifact_file_hash(path_value: Any) -> str | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    path = Path(path_value)
    try:
        if path.is_file():
            return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return None


def _remaining_gaps(
    *,
    evaluated_runs: Sequence[Mapping[str, Any]],
    prompt_metrics: Mapping[str, Mapping[str, Any]],
    external_gate_results: Mapping[str, Mapping[str, Any]],
    required_gate_ids: set[str],
    external_gates_required: bool,
    reliability: Mapping[str, Any],
    semantic_release_report: Mapping[str, Any],
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
    if semantic_release_report.get("valid") is not True:
        failed_ids = [
            str(run.get("id"))
            for run in semantic_release_report.get("runs", [])
            if isinstance(run, Mapping) and run.get("semantic_release_ready") is not True
        ]
        manual_gate = semantic_release_report.get("manual_trace_audit_gate")
        manual_failures: list[str] = []
        if isinstance(manual_gate, Mapping) and manual_gate.get("valid") is not True:
            raw_failures = manual_gate.get("failures")
            if isinstance(raw_failures, list):
                manual_failures = [str(item) for item in raw_failures]
        gaps.append(
            "Semantic release report is not ready; failing or blocked release-counted "
            "run ids: "
            + (", ".join(failed_ids) if failed_ids else "<none recorded>")
            + (
                "; manual trace audit failures: " + "; ".join(manual_failures)
                if manual_failures
                else ""
            )
            + "."
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
        "Semantic release report ready: "
        f"{str(results.get('semantic_release_report', {}).get('valid') is True).lower()}",
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
        "research_tasks": run_dir / "research_tasks.json",
        "search_tasks": run_dir / "search_tasks.json",
        "search_results": run_dir / "search_results.jsonl",
        "visual_tasks": run_dir / "visual_tasks.json",
        "report": run_dir / "report.md",
        "report_status": run_dir / "report_status.json",
        "parallel_status": run_dir / "parallel_orchestration_status.json",
        "subagent_assignments": run_dir / "subagent_assignments.jsonl",
        "run_trace": run_dir / "run_trace.jsonl",
        "semantic_expectation_oracle": run_dir / "semantic_expectation_oracle.json",
        "semantic_plan": run_dir / "semantic_plan.json",
        "semantic_plan_review": run_dir / "semantic_plan_review.json",
        "semantic_planner_validation": run_dir / "semantic_planner_validation.json",
        "semantic_materialization_diff": run_dir / SEMANTIC_MATERIALIZATION_DIFF_FILENAME,
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
        required.extend(
            [
                "evidence",
                "research_tasks",
                "search_tasks",
                "search_results",
                "visual_tasks",
                "subagent_assignments",
                "report",
                "report_status",
            ]
        )
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
    prompt: Mapping[str, Any],
    loaded_artifacts: Mapping[str, Any],
    run_status: Mapping[str, Any],
    evidence: Mapping[str, Any],
    run_path: Path,
    artifact_paths: Mapping[str, Path],
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
        reviewer_provenance = review.get("reviewer_provenance") or review.get("provenance")
        if not isinstance(reviewer_provenance, Mapping):
            failures.append(
                {
                    "check": "reviewer_provenance",
                    "detail": "semantic_plan_review.reviewer_provenance is missing",
                }
            )
        else:
            for field in ("raw_request_hash", "raw_response_hash"):
                if not isinstance(reviewer_provenance.get(field), str) or not reviewer_provenance.get(field):
                    failures.append(
                        {
                            "check": "reviewer_provenance",
                            "field": field,
                            "detail": f"semantic reviewer provenance {field} is missing",
                        }
                    )
            if not _semantic_provenance_has_codex_identity(reviewer_provenance):
                failures.append(
                    {
                        "check": "reviewer_provenance",
                        "detail": "semantic reviewer provenance lacks a Codex session/response/event identity",
                    }
                )
            if reviewer_provenance.get("non_release_fixture") is True:
                failures.append(
                    {
                        "check": "reviewer_provenance",
                        "detail": "deterministic non-release reviewer fixture cannot satisfy semantic release",
                    }
                )
        for field in (
            "reviewer_raw_request_path",
            "reviewer_raw_response_path",
            "reviewer_raw_request_hash",
            "reviewer_raw_response_hash",
            "oracle_hash",
        ):
            if not isinstance(review.get(field), str) or not review.get(field):
                failures.append(
                    {
                        "check": "reviewer_raw_artifact_provenance",
                        "field": field,
                        "detail": f"semantic_plan_review.{field} is missing",
                    }
                )
        failures.extend(
            _semantic_raw_artifact_failures(
                run_path=run_path,
                artifact_label="reviewer",
                request_path=review.get("reviewer_raw_request_path"),
                response_path=review.get("reviewer_raw_response_path"),
                request_hash=(
                    review.get("reviewer_raw_request_artifact_hash")
                    or review.get("reviewer_raw_request_hash")
                ),
                response_hash=(
                    review.get("reviewer_raw_response_artifact_hash")
                    or review.get("reviewer_raw_response_hash")
                ),
                request_content_hash=review.get("reviewer_raw_request_content_hash"),
            )
        )

    oracle = required_payloads.get("semantic_expectation_oracle")
    if isinstance(oracle, Mapping):
        provenance = oracle.get("oracle_provenance") or oracle.get("provenance")
        if not isinstance(provenance, Mapping):
            failures.append(
                {
                    "check": "oracle_provenance",
                    "detail": "semantic_expectation_oracle.oracle_provenance is missing",
                }
            )
        else:
            for field in ("raw_request_hash", "raw_response_hash"):
                if not isinstance(provenance.get(field), str) or not provenance.get(field):
                    failures.append(
                        {
                            "check": "oracle_provenance",
                            "field": field,
                            "detail": f"semantic oracle provenance {field} is missing",
                        }
                )
            if not _semantic_provenance_has_codex_identity(provenance):
                failures.append(
                    {
                        "check": "oracle_provenance",
                        "detail": "semantic oracle provenance lacks a Codex session/response/event identity",
                    }
                )
            if provenance.get("non_release_fixture") is True:
                failures.append(
                    {
                        "check": "oracle_provenance",
                        "detail": "deterministic non-release oracle fixture cannot satisfy semantic release",
                    }
                )
            failures.extend(
                _semantic_raw_artifact_failures(
                    run_path=run_path,
                    artifact_label="oracle",
                    request_path=provenance.get("raw_request_path") or oracle.get("raw_request_path"),
                    response_path=provenance.get("raw_response_path") or oracle.get("raw_response_path"),
                    request_hash=(
                        provenance.get("raw_request_artifact_hash")
                        or oracle.get("raw_request_artifact_hash")
                        or provenance.get("raw_request_hash")
                        or oracle.get("raw_request_hash")
                    ),
                    response_hash=(
                        provenance.get("raw_response_artifact_hash")
                        or oracle.get("raw_response_artifact_hash")
                        or provenance.get("raw_response_hash")
                        or oracle.get("raw_response_hash")
                    ),
                    request_content_hash=(
                        provenance.get("raw_request_content_hash")
                        or oracle.get("raw_request_content_hash")
                    ),
                )
            )
        for field in (
            "plan_visible_to_oracle",
            "used_production_planner_output",
            "used_hidden_template_class",
            "used_fixed_angle_inventory",
        ):
            if oracle.get(field) is not False:
                failures.append(
                    {
                        "check": "oracle_reverse_fit",
                        "field": field,
                        "detail": f"semantic_expectation_oracle.{field} must be false",
                    }
                )
        failures.extend(
            _manifest_oracle_binding_failures(
                prompt=prompt,
                oracle=oracle,
                review=review,
            )
        )

    trace_failures = _semantic_trace_ordering_failures(
        run_path=run_path,
        artifact_paths=artifact_paths,
    )
    failures.extend(trace_failures)

    materialization_payload = required_payloads.get("semantic_materialization_diff")
    if (
        not isinstance(materialization_payload, Mapping)
        or materialization_payload.get("valid") is not True
    ):
        failures.append(
            {
                "check": "semantic_materialization_diff",
                "detail": "semantic_materialization_diff.valid is not true",
            }
        )
    computed_materialization = build_semantic_materialization_diff(
        run_dir=run_path,
        require_research_tasks=True,
        require_downstream=True,
    )
    if computed_materialization.get("valid") is not True:
        failures.append(
            {
                "check": "semantic_materialization_diff",
                "detail": "computed semantic materialization diff is not valid",
                "failure_codes": [
                    failure.get("code")
                    for failure in computed_materialization.get("failures", [])
                    if isinstance(failure, Mapping)
                ],
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
        "semantic_materialization_diff_valid": (
            materialization_payload.get("valid")
            if isinstance(materialization_payload, Mapping)
            else None
        ),
        "computed_materialization_diff_valid": computed_materialization.get("valid"),
        "computed_materialization_diff": computed_materialization,
        "failures": failures,
    }


def _manifest_oracle_binding_failures(
    *,
    prompt: Mapping[str, Any],
    oracle: Any,
    review: Any,
) -> list[dict[str, Any]]:
    expected_hash = prompt.get("oracle_hash")
    expected_path = str(prompt.get("oracle_path") or "").strip()
    if expected_hash is None or not expected_path:
        return [
            {
                "check": "manifest_oracle_binding",
                "detail": "prompt manifest lacks required oracle_hash/oracle_path for semantic release readiness",
            }
        ]
    if not _sha256_hex_string(expected_hash):
        return [
            {
                "check": "manifest_oracle_binding",
                "detail": "prompt manifest oracle_hash is missing or invalid",
            }
        ]
    if not isinstance(oracle, Mapping):
        return [
            {
                "check": "manifest_oracle_binding",
                "detail": "semantic_expectation_oracle artifact is missing",
            }
        ]

    oracle_provenance = oracle.get("oracle_provenance") or oracle.get("provenance")
    review_provenance = (
        review.get("reviewer_provenance") or review.get("provenance")
        if isinstance(review, Mapping)
        else {}
    )
    manifest_hash_payloads = [
        payload
        for payload in (oracle, oracle_provenance, review, review_provenance)
        if isinstance(payload, Mapping)
    ]
    manifest_hash_values = _string_field_values(
        *manifest_hash_payloads,
        names=(
            "manifest_oracle_hash",
            "registered_oracle_hash",
            "oracle_manifest_hash",
        ),
    )
    failures: list[dict[str, Any]] = []
    expected_hash_str = str(expected_hash)
    if not manifest_hash_values:
        failures.append(
            {
                "check": "manifest_oracle_binding",
                "field": "manifest_oracle_hash",
                "expected_oracle_hash": expected_hash_str,
                "detail": "run oracle artifact does not record the manifest oracle hash",
            }
        )
    elif any(value != expected_hash_str for value in manifest_hash_values):
        failures.append(
            {
                "check": "manifest_oracle_binding",
                "field": "manifest_oracle_hash",
                "expected_oracle_hash": expected_hash_str,
                "observed_oracle_hashes": sorted(set(manifest_hash_values)),
                "detail": "run oracle manifest hash does not match the prompt manifest",
            }
        )

    expected_fragment = (
        expected_path.split("#", 1)[1].strip() if "#" in expected_path else ""
    )
    if expected_path or expected_fragment:
        path_values = _string_field_values(
            *manifest_hash_payloads,
            names=(
                "manifest_oracle_path",
                "registered_oracle_path",
                "oracle_manifest_path",
            ),
        )
        fragment_values = _string_field_values(
            *manifest_hash_payloads,
            names=(
                "manifest_oracle_fragment_id",
                "registered_oracle_fragment_id",
                "oracle_manifest_fragment_id",
            ),
        )
        path_matches = expected_path and expected_path in path_values
        fragment_matches = expected_fragment and expected_fragment in fragment_values
        if not path_matches and not fragment_matches:
            failures.append(
                {
                    "check": "manifest_oracle_binding",
                    "field": "manifest_oracle_fragment",
                    "expected_oracle_path": expected_path,
                    "expected_oracle_fragment_id": expected_fragment,
                    "observed_oracle_paths": sorted(set(path_values)),
                    "observed_oracle_fragment_ids": sorted(set(fragment_values)),
                    "detail": "run oracle artifact does not bind to the prompt manifest oracle fragment",
                }
            )
    return failures


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


def _semantic_trace_ordering_failures(
    *,
    run_path: Path,
    artifact_paths: Mapping[str, Path],
) -> list[dict[str, Any]]:
    trace_path = artifact_paths.get("run_trace") or (run_path / "run_trace.jsonl")
    if not trace_path.exists():
        return [
            {
                "check": "semantic_trace_ordering",
                "detail": "run_trace.jsonl is missing",
            }
        ]
    try:
        records = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except json.JSONDecodeError:
        return [
            {
                "check": "semantic_trace_ordering",
                "detail": "run_trace.jsonl contains invalid JSON",
            }
        ]
    required = [
        "semantic_oracle_request_created",
        "semantic_oracle_locked",
        "semantic_planner_request_created",
        "semantic_plan_created",
        "semantic_reviewer_request_created",
        "semantic_review_completed",
    ]
    expected_semantic_indexes = {
        event: index for index, event in enumerate(required, start=1)
    }
    first_indexes: dict[str, int] = {}
    first_records: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            continue
        event_type = str(record.get("event_type") or "")
        if event_type in required and event_type not in first_indexes:
            first_indexes[event_type] = index
            first_records[event_type] = record
    failures: list[dict[str, Any]] = []
    missing = [event for event in required if event not in first_indexes]
    if missing:
        failures.append(
            {
                "check": "semantic_trace_ordering",
                "missing_events": missing,
                "detail": "run_trace.jsonl is missing required semantic ordering events",
            }
        )
        return failures
    indexes = [first_indexes[event] for event in required]
    if indexes != sorted(indexes):
        failures.append(
            {
                "check": "semantic_trace_ordering",
                "event_indexes": dict(zip(required, indexes)),
                "detail": "semantic ordering events are out of order",
            }
        )
    semantic_indexes: list[int] = []
    for event in required:
        record = first_records[event]
        semantic_index = record.get("semantic_event_index")
        order_validation = record.get("order_validation")
        order_index = (
            order_validation.get("semantic_event_index")
            if isinstance(order_validation, Mapping)
            else None
        )
        order_required = (
            order_validation.get("required_order")
            if isinstance(order_validation, Mapping)
            else None
        )
        order_current_event = (
            order_validation.get("current_event")
            if isinstance(order_validation, Mapping)
            else None
        )
        expected_index = expected_semantic_indexes[event]
        if order_required != required or order_current_event != event:
            failures.append(
                {
                    "check": "semantic_trace_ordering",
                    "event": event,
                    "detail": "semantic trace order_validation does not match required order proof",
                }
            )
        if (
            not isinstance(semantic_index, int)
            or isinstance(semantic_index, bool)
            or semantic_index != expected_index
        ):
            failures.append(
                {
                    "check": "semantic_trace_ordering",
                    "event": event,
                    "semantic_event_index": semantic_index,
                    "expected_semantic_event_index": expected_index,
                    "detail": "semantic trace event has an invalid semantic_event_index",
                }
            )
        else:
            semantic_indexes.append(semantic_index)
        if order_index != semantic_index:
            failures.append(
                {
                    "check": "semantic_trace_ordering",
                    "event": event,
                    "semantic_event_index": semantic_index,
                    "order_validation_index": order_index,
                    "detail": "semantic trace event index does not match order_validation",
                }
            )
    if len(semantic_indexes) == len(required) and semantic_indexes != sorted(semantic_indexes):
        failures.append(
            {
                "check": "semantic_trace_ordering",
                "semantic_event_indexes": dict(zip(required, semantic_indexes)),
                "detail": "semantic trace event indexes are not monotonic",
            }
        )
    semantic_timestamps: list[datetime] = []
    raw_semantic_timestamps: dict[str, Any] = {}
    for event in required:
        record = first_records[event]
        raw_timestamp = record.get("timestamp")
        raw_semantic_timestamps[event] = raw_timestamp
        parsed_timestamp = _parse_timestamp(raw_timestamp if isinstance(raw_timestamp, str) else None)
        if parsed_timestamp is None:
            failures.append(
                {
                    "check": "semantic_trace_ordering",
                    "event": event,
                    "timestamp": raw_timestamp,
                    "detail": "semantic trace event timestamp is missing or invalid",
                }
            )
        else:
            semantic_timestamps.append(parsed_timestamp)
    if len(semantic_timestamps) == len(required):
        for previous, current in zip(semantic_timestamps, semantic_timestamps[1:]):
            if not previous < current:
                failures.append(
                    {
                        "check": "semantic_trace_ordering",
                        "semantic_event_timestamps": raw_semantic_timestamps,
                        "detail": "semantic trace event timestamps are not strictly increasing",
                    }
                )
                break
    for event in required:
        record = first_records[event]
        hashes = record.get("artifact_hashes")
        paths = record.get("semantic_artifact_paths")
        if not isinstance(hashes, Mapping) or not hashes:
            failures.append(
                {
                    "check": "semantic_trace_ordering",
                    "event": event,
                    "detail": "semantic trace event is missing artifact_hashes",
                }
            )
            continue
        if not isinstance(paths, Mapping):
            failures.append(
                {
                    "check": "semantic_trace_ordering",
                    "event": event,
                    "detail": "semantic trace event is missing semantic_artifact_paths",
                }
            )
            continue
        for key, expected_hash in hashes.items():
            raw_path = paths.get(key)
            if not isinstance(raw_path, str) or not raw_path:
                failures.append(
                    {
                        "check": "semantic_trace_ordering",
                        "event": event,
                        "artifact": key,
                        "detail": "semantic trace artifact path is missing",
                    }
                )
                continue
            path = Path(raw_path)
            if not path.is_absolute():
                path = run_path / path
            if not path.exists():
                failures.append(
                    {
                        "check": "semantic_trace_ordering",
                        "event": event,
                        "artifact": key,
                        "path": str(path),
                        "detail": "semantic trace artifact path does not exist",
                    }
                )
                continue
            if _file_sha256(path) != expected_hash:
                if (
                    event == "semantic_plan_created"
                    and key == "semantic_plan"
                    and _reviewed_candidate_hash(path) == expected_hash
                ):
                    continue
                failures.append(
                    {
                        "check": "semantic_trace_ordering",
                        "event": event,
                        "artifact": key,
                        "detail": "semantic trace artifact hash does not match the artifact",
                    }
                )
    return failures


def _semantic_raw_artifact_failures(
    *,
    run_path: Path,
    artifact_label: str,
    request_path: Any,
    response_path: Any,
    request_hash: Any,
    response_hash: Any,
    request_content_hash: Any = None,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    failures.extend(
        _semantic_file_hash_failures(
            run_path=run_path,
            artifact_label=f"{artifact_label}_raw_request",
            path_value=request_path,
            expected_hash=request_hash,
        )
    )
    failures.extend(
        _semantic_file_hash_failures(
            run_path=run_path,
            artifact_label=f"{artifact_label}_raw_response",
            path_value=response_path,
            expected_hash=response_hash,
        )
    )
    if request_content_hash:
        request_artifact_path = _semantic_artifact_path(run_path, request_path)
        if request_artifact_path is not None and request_artifact_path.exists():
            payload = _read_optional_json(request_artifact_path)
            if isinstance(payload, Mapping):
                content_payload = dict(payload)
                content_payload.pop("raw_request_content_hash", None)
                content_payload.pop("raw_request_hash", None)
                actual_content_hash = hashlib.sha256(
                    json.dumps(
                        content_payload,
                        sort_keys=True,
                        ensure_ascii=True,
                    ).encode("utf-8")
                ).hexdigest()
                if actual_content_hash != str(request_content_hash):
                    failures.append(
                        {
                            "check": "semantic_raw_artifact",
                            "artifact": f"{artifact_label}_raw_request",
                            "detail": "semantic raw request content hash does not match the request payload",
                        }
                    )
    return failures


def _semantic_file_hash_failures(
    *,
    run_path: Path,
    artifact_label: str,
    path_value: Any,
    expected_hash: Any,
) -> list[dict[str, Any]]:
    path = _semantic_artifact_path(run_path, path_value)
    if path is None:
        return [
            {
                "check": "semantic_raw_artifact",
                "artifact": artifact_label,
                "detail": "semantic raw artifact path is missing",
            }
        ]
    if not path.exists():
        return [
            {
                "check": "semantic_raw_artifact",
                "artifact": artifact_label,
                "path": str(path),
                "detail": "semantic raw artifact path does not exist",
            }
        ]
    expected = str(expected_hash or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return [
            {
                "check": "semantic_raw_artifact",
                "artifact": artifact_label,
                "detail": "semantic raw artifact hash is missing or invalid",
            }
        ]
    actual = _file_sha256(path)
    if actual != expected:
        return [
            {
                "check": "semantic_raw_artifact",
                "artifact": artifact_label,
                "path": str(path),
                "detail": "semantic raw artifact hash does not match file content",
            }
        ]
    return []


def _semantic_artifact_path(run_path: Path, path_value: Any) -> Path | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    path = Path(path_value)
    if not path.is_absolute():
        path = run_path / path
    return path


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reviewed_candidate_hash(path: Path) -> str | None:
    payload = _read_optional_json(path)
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("reviewed_candidate_hash")
    return str(value) if value else None


def _semantic_artifact_integrity_failures(
    artifacts: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    plan = artifacts.get("semantic_plan")
    validation = artifacts.get("semantic_planner_validation")
    semantic_plan = (
        plan.get("semantic_plan")
        if isinstance(plan, Mapping) and isinstance(plan.get("semantic_plan"), Mapping)
        else {}
    )
    fallback_question_scope = _first_valid_semantic_question_scope(
        plan.get("question_scope") if isinstance(plan, Mapping) else None,
        validation.get("question_scope") if isinstance(validation, Mapping) else None,
    )
    fallback_template_use = _first_valid_semantic_template_use(
        plan.get("template_use") if isinstance(plan, Mapping) else None,
        validation.get("template_use") if isinstance(validation, Mapping) else None,
    )
    for artifact_name in SEMANTIC_RELEASE_REQUIRED_ARTIFACTS:
        if artifact_name == "semantic_materialization_diff":
            continue
        payload = artifacts.get(artifact_name)
        if not isinstance(payload, Mapping):
            continue
        question_scope = _semantic_artifact_question_scope(
            artifact_name=artifact_name,
            payload=payload,
            fallback_question_scope=fallback_question_scope,
        )
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name=artifact_name,
            field="question_scope",
            invalid=not _valid_semantic_question_scope(question_scope),
        )
        for field in ("raw_request_path", "raw_response_path"):
            _append_semantic_artifact_failure_if(
                failures,
                artifact_name=artifact_name,
                field=field,
                invalid=not _non_empty_string(
                    _semantic_artifact_common_field(
                        payload,
                        artifact_name=artifact_name,
                        field=field,
                    )
                ),
            )
        for field in ("raw_request_hash", "raw_response_hash"):
            _append_semantic_artifact_failure_if(
                failures,
                artifact_name=artifact_name,
                field=field,
                invalid=not _sha256_hex_string(
                    _semantic_artifact_common_field(
                        payload,
                        artifact_name=artifact_name,
                        field=field,
                    )
                ),
            )
        provenance = _semantic_artifact_provenance(
            payload,
            artifact_name=artifact_name,
        )
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name=artifact_name,
            field="provenance",
            invalid=not _valid_semantic_provenance(
                provenance,
                artifact_name=artifact_name,
                payload=payload,
            ),
        )
        template_use = _semantic_artifact_template_use(
            artifact_name=artifact_name,
            payload=payload,
            fallback_template_use=fallback_template_use,
        )
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name=artifact_name,
            field="template_use",
            invalid=not _valid_semantic_template_use(template_use),
        )
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name=artifact_name,
            field="session_id_unavailable_reason",
            invalid=not _valid_semantic_session_availability(payload, provenance),
        )

    plan_angle_ids: set[str] = set()
    if isinstance(plan, Mapping):
        original_question = _semantic_original_question(plan, semantic_plan)
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name="semantic_plan",
            field="semantic_plan",
            invalid=not isinstance(semantic_plan, Mapping) or not semantic_plan,
        )
        nested_angle_ids: set[str] = set()
        if isinstance(semantic_plan, Mapping):
            nested_angles = semantic_plan.get("angles")
            nested_angles_valid = _valid_semantic_angles(
                nested_angles,
                original_question=original_question,
            )
            _append_semantic_artifact_failure_if(
                failures,
                artifact_name="semantic_plan",
                field="semantic_plan.angles",
                invalid=not nested_angles_valid,
            )
            if nested_angles_valid:
                nested_angle_ids = _semantic_angle_ids(nested_angles)
        top_level_angles = plan.get("angles")
        top_level_angles_valid = _valid_semantic_angles(
            top_level_angles,
            original_question=original_question,
        )
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name="semantic_plan",
            field="angles",
            invalid=not top_level_angles_valid,
        )
        if top_level_angles_valid:
            plan_angle_ids = _semantic_angle_ids(top_level_angles)
        if (
            top_level_angles_valid
            and nested_angle_ids
            and nested_angle_ids != plan_angle_ids
        ):
            _append_semantic_artifact_failure_if(
                failures,
                artifact_name="semantic_plan",
                field="semantic_plan.angles",
                invalid=True,
            )
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name="semantic_plan",
            field="requirement_coverage_map",
            invalid=not _valid_requirement_coverage_map(
                plan.get("requirement_coverage_map"),
                plan_angle_ids=plan_angle_ids,
            ),
        )

    oracle = artifacts.get("semantic_expectation_oracle")
    if isinstance(oracle, Mapping):
        _append_semantic_artifact_failure_if(
            failures,
            artifact_name="semantic_expectation_oracle",
            field="oracle_requirement_map",
            invalid=not _valid_oracle_requirement_map(
                oracle.get("oracle_requirement_map"),
                plan_angle_ids=plan_angle_ids,
                requirement_coverage_map=(
                    plan.get("requirement_coverage_map")
                    if isinstance(plan, Mapping)
                    else None
                ),
            ),
        )

    return failures


def _first_valid_semantic_question_scope(*values: Any) -> Mapping[str, Any] | None:
    for value in values:
        if isinstance(value, Mapping) and _valid_semantic_question_scope(value):
            return value
    return None


def _first_valid_semantic_template_use(*values: Any) -> Mapping[str, Any] | None:
    for value in values:
        if isinstance(value, Mapping) and _valid_semantic_template_use(value):
            return value
    return None


def _semantic_artifact_question_scope(
    *,
    artifact_name: str,
    payload: Mapping[str, Any],
    fallback_question_scope: Mapping[str, Any] | None,
) -> Any:
    value = payload.get("question_scope")
    if _valid_semantic_question_scope(value):
        return value
    if (
        artifact_name in {"semantic_expectation_oracle", "semantic_plan_review"}
        and fallback_question_scope is not None
    ):
        return fallback_question_scope
    return value


def _semantic_artifact_template_use(
    *,
    artifact_name: str,
    payload: Mapping[str, Any],
    fallback_template_use: Mapping[str, Any] | None,
) -> Any:
    value = payload.get("template_use")
    if _valid_semantic_template_use(value):
        return value
    if (
        artifact_name in {"semantic_expectation_oracle", "semantic_plan_review"}
        and fallback_template_use is not None
    ):
        return fallback_template_use
    return value


def _semantic_artifact_common_field(
    payload: Mapping[str, Any],
    *,
    artifact_name: str,
    field: str,
) -> Any:
    value = payload.get(field)
    if value is not None:
        return value
    if artifact_name == "semantic_plan_review":
        reviewer_field = f"reviewer_{field}"
        return payload.get(reviewer_field)
    return value


def _semantic_artifact_provenance(
    payload: Mapping[str, Any],
    *,
    artifact_name: str,
) -> Any:
    if artifact_name == "semantic_expectation_oracle":
        return payload.get("provenance") or payload.get("oracle_provenance")
    if artifact_name == "semantic_plan_review":
        return payload.get("provenance") or payload.get("reviewer_provenance")
    if artifact_name == "semantic_plan":
        return payload.get("provenance") or payload.get("planner_provenance")
    return payload.get("provenance")


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


def _valid_semantic_provenance(
    value: Any,
    *,
    artifact_name: str,
    payload: Mapping[str, Any],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("non_release_fixture") is True:
        return False
    if not _semantic_provenance_has_codex_identity(value):
        return False
    if not (
        _sha256_hex_string(value.get("raw_request_hash"))
        and _sha256_hex_string(value.get("raw_response_hash"))
    ):
        return False
    if artifact_name in {"semantic_plan", "semantic_planner_validation"}:
        planner_mode = value.get("planner_mode") or payload.get("planner_mode")
        planner_source = (
            value.get("planner_source")
            or payload.get("source")
            or payload.get("planner_source")
        )
        return (
            planner_mode == "codex_semantic"
            and planner_source == "codex_semantic"
            and value.get("raw_request_required") is True
            and value.get("raw_response_required") is True
        )
    return True


def _valid_semantic_session_availability(
    payload: Mapping[str, Any],
    provenance: Any,
) -> bool:
    if isinstance(provenance, Mapping) and _semantic_provenance_has_codex_identity(
        provenance
    ):
        return True
    if _semantic_provenance_has_codex_identity(payload):
        return True
    return (
        _non_empty_string(payload.get("session_id_unavailable_reason"))
        or (
            isinstance(provenance, Mapping)
            and _non_empty_string(provenance.get("session_id_unavailable_reason"))
        )
    )


def _semantic_provenance_has_codex_identity(value: Mapping[str, Any]) -> bool:
    for field in (
        "child_session_id",
        "session_id",
        "raw_response_id",
        "codex_event_id",
        "response_id",
    ):
        if _non_empty_string(value.get(field)):
            return True
    return False


def _valid_semantic_template_use(value: Any) -> bool:
    template_source = value.get("template_source") if isinstance(value, Mapping) else None
    return (
        isinstance(value, Mapping)
        and value.get("uses_preselected_template") is False
        and value.get("template_release_eligible") is False
        and value.get("template_angle_titles") == []
        and (
            template_source is None
            or (isinstance(template_source, str) and not template_source.strip())
        )
    )


def _semantic_original_question(*payloads: Any) -> str:
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        scope = payload.get("question_scope")
        if isinstance(scope, Mapping) and _non_empty_string(scope.get("original_question")):
            return str(scope["original_question"]).strip()
        for field in ("original_question", "question"):
            if _non_empty_string(payload.get(field)):
                return str(payload[field]).strip()
    return ""


def _valid_oracle_requirement_map(
    value: Any,
    *,
    plan_angle_ids: set[str],
    requirement_coverage_map: Any = None,
) -> bool:
    if (
        not isinstance(value, list)
        or len(value) < SEMANTIC_MIN_RELEASE_ANGLES
        or not plan_angle_ids
    ):
        return False
    coverage_by_requirement = _coverage_angle_ids_by_requirement(
        requirement_coverage_map
    )
    covered_angle_ids: set[str] = set()
    for requirement in value:
        if not _valid_oracle_requirement(requirement):
            return False
        requirement_angle_ids = {
            str(angle_id).strip()
            for angle_id in requirement.get("covered_by_angle_ids", [])
            if _non_empty_string(angle_id)
        }
        if not requirement_angle_ids:
            requirement_angle_ids = coverage_by_requirement.get(
                str(requirement.get("requirement_id") or "").strip(),
                set(),
            )
        covered_angle_ids.update(requirement_angle_ids)
    return (
        covered_angle_ids == plan_angle_ids
    )


def _valid_oracle_requirement(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        _non_empty_string(value.get("requirement_id"))
        and (
            _non_empty_string(value.get("text"))
            or _non_empty_string(value.get("description"))
            or _non_empty_string(value.get("requirement_text"))
            or _non_empty_string(value.get("prompt_text"))
        )
    )


def _valid_semantic_angles(value: Any, *, original_question: str) -> bool:
    original_tokens = _semantic_meaningful_tokens(original_question)
    if (
        not isinstance(value, list)
        or len(value) < SEMANTIC_MIN_RELEASE_ANGLES
        or not original_tokens
    ):
        return False
    angle_ids = [
        str(angle.get("angle_id")).strip()
        for angle in value
        if isinstance(angle, Mapping) and _non_empty_string(angle.get("angle_id"))
    ]
    if len(angle_ids) != len(value) or len(set(angle_ids)) != len(value):
        return False
    if not all(
        _valid_semantic_angle(
            angle,
            original_question=original_question,
            original_tokens=original_tokens,
        )
        for angle in value
    ):
        return False
    return not _has_duplicate_semantic_angle_content(value)


def _valid_semantic_angle(
    value: Any,
    *,
    original_question: str,
    original_tokens: set[str],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    required_string_fields = (
        "angle_id",
        "title",
        "research_question",
        "evidence_need",
        "report_section",
    )
    if not all(_non_empty_string(value.get(field)) for field in required_string_fields):
        return False
    if not _non_empty_string_list(value.get("expected_artifacts")):
        return False
    if not _non_empty_string_list(value.get("success_criteria")):
        return False

    title = str(value["title"]).strip()
    research_question = str(value["research_question"]).strip()
    if _generic_or_original_semantic_text(title, original_question):
        return False
    if _generic_or_original_semantic_text(research_question, original_question):
        return False
    combined_text = f"{title} {research_question}"
    if _semantic_placeholder_text(combined_text):
        return False

    all_tokens = _semantic_token_list(combined_text)
    meaningful_tokens = _semantic_meaningful_token_list(combined_text)
    unique_meaningful_tokens = set(meaningful_tokens)
    if len(unique_meaningful_tokens) < SEMANTIC_MIN_ANGLE_UNIQUE_TOKENS:
        return False
    if len(meaningful_tokens) * 2 < len(all_tokens):
        return False
    overlap_tokens = unique_meaningful_tokens & original_tokens
    return len(overlap_tokens) >= SEMANTIC_MIN_ANGLE_OVERLAP_TOKENS


def _generic_or_original_semantic_text(text: str, original_question: str) -> bool:
    normalized = _normalized_semantic_text(text)
    if normalized in GENERIC_SEMANTIC_ANGLE_TEXTS:
        return True
    return bool(original_question) and normalized == _normalized_semantic_text(
        original_question
    )


def _semantic_placeholder_text(text: str) -> bool:
    normalized = _normalized_semantic_text(text)
    return any(
        re.search(pattern, normalized)
        for pattern in GENERIC_SEMANTIC_PLACEHOLDER_PATTERNS
    )


def _has_duplicate_semantic_angle_content(angles: Sequence[Any]) -> bool:
    token_sets = [
        _semantic_angle_content_tokens(angle)
        for angle in angles
        if isinstance(angle, Mapping)
    ]
    signatures: set[tuple[str, ...]] = set()
    for index, left_tokens in enumerate(token_sets):
        signature = tuple(sorted(left_tokens))
        if signature in signatures:
            return True
        signatures.add(signature)
        for right_tokens in token_sets[index + 1:]:
            if _near_duplicate_semantic_token_sets(left_tokens, right_tokens):
                return True
    return False


def _semantic_angle_content_tokens(angle: Mapping[str, Any]) -> set[str]:
    return set(
        _semantic_meaningful_token_list(
            f"{angle.get('title') or ''} {angle.get('research_question') or ''}"
        )
    )


def _near_duplicate_semantic_token_sets(left: set[str], right: set[str]) -> bool:
    if not left or not right:
        return False
    intersection = len(left & right)
    union = len(left | right)
    jaccard = intersection / union if union else 0.0
    containment = intersection / min(len(left), len(right))
    return (
        jaccard >= SEMANTIC_ANGLE_NEAR_DUPLICATE_THRESHOLD
        or containment >= SEMANTIC_ANGLE_NEAR_DUPLICATE_THRESHOLD
    )


def _valid_requirement_coverage_map(
    value: Any,
    *,
    plan_angle_ids: set[str],
) -> bool:
    if (
        not isinstance(value, list)
        or len(value) < SEMANTIC_MIN_RELEASE_ANGLES
        or not plan_angle_ids
    ):
        return False
    covered_angle_ids: set[str] = set()
    for coverage in value:
        if not _valid_requirement_coverage(coverage):
            return False
        covered_angle_ids.update(_coverage_angle_ids(coverage))
    return (
        covered_angle_ids == plan_angle_ids
    )


def _valid_requirement_coverage(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        _non_empty_string(value.get("requirement_id"))
        and bool(_coverage_angle_ids(value))
        and _non_empty_string(value.get("coverage_status"))
    )


def _coverage_angle_ids_by_requirement(value: Any) -> dict[str, set[str]]:
    output: dict[str, set[str]] = {}
    if not isinstance(value, list):
        return output
    for coverage in value:
        if not isinstance(coverage, Mapping):
            continue
        requirement_id = str(coverage.get("requirement_id") or "").strip()
        if not requirement_id:
            continue
        output.setdefault(requirement_id, set()).update(_coverage_angle_ids(coverage))
    return output


def _coverage_angle_ids(value: Mapping[str, Any]) -> set[str]:
    angle_ids = {
        str(angle_id).strip()
        for angle_id in value.get("covered_by_angle_ids", [])
        if _non_empty_string(angle_id)
    }
    if _non_empty_string(value.get("angle_id")):
        angle_ids.add(str(value["angle_id"]).strip())
    return angle_ids


def _semantic_angle_ids(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(angle["angle_id"]).strip()
        for angle in value
        if isinstance(angle, Mapping) and _non_empty_string(angle.get("angle_id"))
    }


def _semantic_token_list(text: str) -> list[str]:
    tokens: list[str] = []
    for token in re.findall(r"[A-Za-z0-9\uac00-\ud7a3]+", text.lower()):
        if _semantic_token_has_hangul(token):
            normalized = _normalize_korean_semantic_token(token)
            if len(normalized) >= 2:
                tokens.append(normalized)
        elif len(token) > 2:
            tokens.append(token)
    return tokens


def _semantic_token_has_hangul(token: str) -> bool:
    return any("\uac00" <= char <= "\ud7a3" for char in token)


KOREAN_SEMANTIC_PARTICLE_SUFFIXES = (
    "께서는",
    "에서는",
    "에게는",
    "으로는",
    "로는",
    "에는",
    "과는",
    "와는",
    "에서",
    "에게",
    "께서",
    "으로",
    "로",
    "부터",
    "까지",
    "처럼",
    "보다",
    "마다",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "와",
    "과",
    "도",
    "만",
    "에",
)


def _normalize_korean_semantic_token(token: str) -> str:
    for suffix in KOREAN_SEMANTIC_PARTICLE_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


def _semantic_meaningful_token_list(text: str) -> list[str]:
    return [
        token
        for token in _semantic_token_list(text)
        if token not in GENERIC_SEMANTIC_TOKENS
    ]


def _semantic_meaningful_tokens(text: str) -> set[str]:
    return set(_semantic_meaningful_token_list(text))


def _normalized_semantic_text(text: str) -> str:
    return " ".join(
        re.findall(r"[A-Za-z0-9\uac00-\ud7a3]+", text.lower())
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
        if (
            name == "semantic_expectation_oracle"
            and name not in sources
            and _oracle_release_support_artifact(payload)
        ):
            sources[name] = "codex_semantic"
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
        if name == "semantic_expectation_oracle" and _oracle_release_support_artifact(
            payload
        ):
            sources[name] = True
        else:
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


def _oracle_release_support_artifact(payload: Mapping[str, Any]) -> bool:
    if payload.get("artifact_type") != "semantic_expectation_oracle":
        return False
    for field in (
        "plan_visible_to_oracle",
        "used_production_planner_output",
        "used_hidden_template_class",
        "used_fixed_angle_inventory",
    ):
        if payload.get(field) is not False:
            return False
    provenance = payload.get("oracle_provenance") or payload.get("provenance")
    if not isinstance(provenance, Mapping):
        return False
    if provenance.get("non_release_fixture") is True:
        return False
    return (
        _semantic_provenance_has_codex_identity(provenance)
        and _sha256_hex_string(provenance.get("raw_request_hash") or payload.get("raw_request_hash"))
        and _sha256_hex_string(provenance.get("raw_response_hash") or payload.get("raw_response_hash"))
    )


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
    identity_matched_results = [
        record
        for record in search_results
        if record.get("prompt_id") == prompt.get("id")
        and record.get("suite_id") == suite_id
        and record.get("prompt_hash") == expected_hash
    ]
    matching_codex_native_results = [
        record
        for record in codex_native_results
        if record.get("prompt_id") == prompt.get("id")
        and record.get("suite_id") == suite_id
        and record.get("prompt_hash") == expected_hash
    ]
    incomplete_identity_records = [
        record
        for record in identity_matched_results
        if not _is_codex_native_search_result(record)
    ]
    if incomplete_identity_records:
        failures.append(
            "search_results.jsonl contains incomplete or non-release Codex-native "
            "search handoff records for this prompt"
        )
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
    if _policy_blocks_release(
        candidates=candidates,
        fetches=fetches,
        observations=observations,
        release_candidates=real_candidates,
        release_fetches=real_fetches,
        release_observations=real_observations,
    ):
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
        candidate_id = _observation_linked_candidate_id(observation)
        fetch_id = _observation_linked_fetch_id(observation)
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


_RELEASE_BLOCKING_POLICY_DECISIONS = {
    "blocked",
    "manual_review",
    "disallowed",
    "restricted",
}


def _policy_blocks_release(
    *,
    candidates: Sequence[Mapping[str, Any]],
    fetches: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    release_candidates: Sequence[Mapping[str, Any]],
    release_fetches: Sequence[Mapping[str, Any]],
    release_observations: Sequence[Mapping[str, Any]],
) -> bool:
    release_candidate_ids = _release_record_ids(release_candidates, "candidate_id")
    release_fetch_ids = _release_record_ids(release_fetches, "fetch_id")
    release_image_ids = _release_record_ids(release_observations, "evidence_image_id")
    release_observation_ids = _release_record_ids(release_observations, "observation_id")

    for record in candidates:
        if not _has_release_blocking_policy(record):
            continue
        if _string_record_field(record, "candidate_id") in release_candidate_ids:
            return True
    for record in fetches:
        if not _has_release_blocking_policy(record):
            continue
        if _string_record_field(record, "fetch_id") in release_fetch_ids:
            return True
        if _string_record_field(record, "candidate_id") in release_candidate_ids:
            return True
        if _string_record_field(record, "evidence_image_id") in release_image_ids:
            return True
    for record in observations:
        if not _has_release_blocking_policy(record):
            continue
        if _string_record_field(record, "observation_id") in release_observation_ids:
            return True
        if _string_record_field(record, "evidence_image_id") in release_image_ids:
            return True
        if _string_record_field(record, "candidate_id") in release_candidate_ids:
            return True
        if _string_record_field(record, "linked_candidate_id") in release_candidate_ids:
            return True
        if _string_record_field(record, "fetch_id") in release_fetch_ids:
            return True
        if _string_record_field(record, "linked_fetch_id") in release_fetch_ids:
            return True
    return False


def _has_release_blocking_policy(record: Mapping[str, Any]) -> bool:
    if record.get("provider_mode") != "real":
        return False
    if record.get("policy_decision") in _RELEASE_BLOCKING_POLICY_DECISIONS:
        return True
    return any(
        record.get(status_field) == "policy_blocked"
        for status_field in (
            "analysis_status",
            "candidate_status",
            "fetch_status",
            "observation_status",
        )
    )


def _release_record_ids(
    records: Sequence[Mapping[str, Any]],
    field: str,
) -> set[str]:
    return {
        value
        for value in (_string_record_field(record, field) for record in records)
        if value
    }


def _string_record_field(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    return value if isinstance(value, str) and value else ""


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
        candidate_id = _observation_linked_candidate_id(observation)
        fetch_id = _observation_linked_fetch_id(observation)
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


def _observation_linked_candidate_id(observation: Mapping[str, Any]) -> Any:
    return observation.get("linked_candidate_id") or observation.get("candidate_id")


def _observation_linked_fetch_id(observation: Mapping[str, Any]) -> Any:
    return observation.get("linked_fetch_id") or observation.get("fetch_id")


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
    return any(
        record.get(field) is True
        for field in (
            "hidden_codex_api_call",
            "codex_native_api_call",
            "hidden_api_call",
        )
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
