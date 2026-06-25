"""Public Beta real-use validation harness.

The harness is intentionally artifact-driven. It can classify real run
directories when the operator supplies them, but in credential-free public
repo validation it records explicit blocked diagnostics instead of pretending
that provider-backed research passed.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .visual_artifacts import VISUAL_PROVIDER_STATUS_FILENAME


PUBLIC_BETA_PROMPT_MANIFEST_SCHEMA_VERSION = (
    "codex-deepresearch.public-beta-prompts.v0"
)
PUBLIC_BETA_VALIDATION_SCHEMA_VERSION = (
    "codex-deepresearch.public-beta-validation.v0"
)
PUBLIC_BETA_VALIDATION_RESULTS_FILENAME = "public_beta_validation_results.json"
PUBLIC_BETA_VALIDATION_SUMMARY_FILENAME = "public_beta_validation_summary.md"
PROVIDER_PROVENANCE_FILENAME = "provider_provenance.json"

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST = (
    PLUGIN_ROOT / "validation" / "public_beta_prompts.json"
)

VISUAL_ROUTES = {"visual_required", "visual_optional"}
PASS_TERMINAL_STATUSES = {
    "completed_parallel",
    "completed_partial_parallel",
    "completed_serial_handoff",
    "completed_auto_visual",
}
BLOCKED_TERMINAL_STATUSES = {
    "blocked_preflight",
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
    "failed_parallel_no_accepted_shards",
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
GATE_THRESHOLDS = {
    "fresh_session_full_runner_artifact_handoff": 0.98,
    "codex_plugin_interactive_visual_e2e": 0.90,
    "automated_cli_real_provider_visual_e2e": 0.90,
    "automatic_web_visual_e2e": 0.90,
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


def run_public_beta_validation(
    *,
    runs_dir: str | Path,
    suite_id: str = "public-beta-validation",
    clean: bool = False,
    prompt_manifest: str | Path = DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST,
    prompt_runs: Mapping[str, str | Path] | None = None,
    gate_results: Mapping[str, str | Path] | None = None,
) -> dict[str, Any]:
    """Classify the Phase 3 Public Beta real-use validation set."""

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
            evaluated_runs.append(evaluate_public_beta_prompt_run(prompt, run_dir))

    prompt_metrics = _metric_summaries(evaluated_runs)
    external_gate_results = _external_gate_results(gate_results)
    acceptance = _acceptance(
        manifest=manifest,
        evaluated_runs=evaluated_runs,
        prompt_metrics=prompt_metrics,
        summary_path=summary_path,
    )
    outcome_counts = _outcome_counts(evaluated_runs)
    classification_counts = _classification_counts(evaluated_runs)
    release_gate_ready = all(
        metric.get("release_gate_ready") is True
        for metric in prompt_metrics.values()
    )
    pre_summary_acceptance = {
        key: value
        for key, value in acceptance.items()
        if key != "summary_artifact_public_safe"
    }
    status = (
        "failed"
        if outcome_counts["failed"] > 0
        else "passed"
        if release_gate_ready and all(pre_summary_acceptance.values())
        else "blocked"
    )
    results: dict[str, Any] = {
        "schema_version": PUBLIC_BETA_VALIDATION_SCHEMA_VERSION,
        "status": status,
        "suite_id": suite_id,
        "suite_dir": str(suite_dir.resolve()),
        "prompt_manifest": str(manifest_path.resolve()),
        "generated_at": _utc_now(),
        "public_safe": True,
        "raw_run_bundles_copied": False,
        "release_gate_ready": release_gate_ready and status == "passed",
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
        "prompt_metrics": prompt_metrics,
        "external_gate_results": external_gate_results,
        "acceptance": acceptance,
        "runs": evaluated_runs,
        "remaining_gaps": _remaining_gaps(
            evaluated_runs=evaluated_runs,
            prompt_metrics=prompt_metrics,
            external_gate_results=external_gate_results,
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
    evidence = loaded.get("evidence")
    terminal_status = _terminal_status(run_status)
    missing_status = not isinstance(run_status, Mapping)
    missing_artifacts = [
        name
        for name in _required_status_artifacts(prompt, terminal_status)
        if not artifacts[name].exists()
    ]

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
    if status == "passed" and missing_artifacts:
        status = "failed"
        classification = "included_failure"
        failure_category = "artifact_handoff_failure"
        detail = "passing terminal status did not expose required status artifacts"

    return {
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
        "status_artifacts": {
            name: str(path.resolve())
            for name, path in artifacts.items()
            if path.exists()
        },
        "missing_artifacts": missing_artifacts,
        "provider_provenance": provider_provenance,
        "failure_category": failure_category,
        "failure_detail": detail,
        "public_safe": prompt.get("public_safe") is True,
        "raw_run_bundle_copied": False,
    }


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
        "status_artifacts": artifacts,
        "missing_artifacts": [],
        "provider_provenance": provider_provenance,
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


def _external_gate_results(
    gate_results: Mapping[str, str | Path] | None,
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
        status = str(payload.get("status", "unknown"))
        blocked = int(payload.get("outcome_counts", {}).get("blocked", 0)) if isinstance(payload.get("outcome_counts"), Mapping) else 0
        failed = int(payload.get("outcome_counts", {}).get("failed", 0)) if isinstance(payload.get("outcome_counts"), Mapping) else 0
        release_gate_passed = payload.get("release_gate_passed")
        results[gate_id] = {
            "status": status,
            "release_gate_ready": (
                status == "passed"
                and blocked == 0
                and failed == 0
                and release_gate_passed is not False
            ),
            "path": str(Path(path).resolve()),
            "blocked": blocked,
            "failed": failed,
            "release_gate_passed": release_gate_passed,
            "public_safe": payload.get("public_safe") is True,
        }
    return results


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
    for metric_id, metric in prompt_metrics.items():
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
    if missing_external:
        gaps.append(
            "External gate result artifacts were not supplied for: "
            + ", ".join(sorted(missing_external))
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
        "| Gate | Threshold | Pass rate | Passed | Failed non-blocked | Blocked | Ready |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for metric in results["prompt_metrics"].values():
        pass_rate = metric.get("pass_rate")
        rate = "n/a" if pass_rate is None else f"{pass_rate:.1%}"
        lines.append(
            "| {metric_id} | {threshold:.0%} | {rate} | {passed} | "
            "{failed} | {blocked} | {ready} |".format(
                metric_id=metric["metric_id"],
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
            "{failure_category} | {run_dir} |".format(
                id=run["id"],
                route=run["route"],
                status=run["status"],
                metric_classification=run["metric_classification"],
                failure_category=run.get("failure_category") or "",
                run_dir=run["run_dir"],
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
    )
    return not any(token in text for token in forbidden)


def _artifact_paths(run_dir: Path, *, route: str) -> dict[str, Path]:
    artifacts = {
        "run_status": run_dir / "run_status.json",
        "evidence": run_dir / "evidence.json",
        "report": run_dir / "report.md",
        "report_status": run_dir / "report_status.json",
        "parallel_status": run_dir / "parallel_orchestration_status.json",
        "run_trace": run_dir / "run_trace.jsonl",
    }
    if route in VISUAL_ROUTES:
        artifacts.update(
            {
                "visual_provider_status": run_dir / VISUAL_PROVIDER_STATUS_FILENAME,
                "visual_search_plan": run_dir / "visual_search_plan.json",
                "visual_candidates": run_dir / "visual_candidates.jsonl",
                "image_fetch_status": run_dir / "image_fetch_status.jsonl",
                "visual_observations": run_dir / "visual_observations.jsonl",
            }
        )
    return artifacts


def _required_status_artifacts(
    prompt: Mapping[str, Any],
    terminal_status: str,
) -> list[str]:
    required = ["run_status"]
    if terminal_status in PASS_TERMINAL_STATUSES:
        required.extend(["evidence", "report", "report_status"])
    if prompt.get("route") in VISUAL_ROUTES:
        required.append("visual_provider_status")
    if terminal_status == "completed_auto_visual":
        required.extend(["visual_search_plan", "visual_candidates", "image_fetch_status", "visual_observations"])
    return required


def _metric_classification(
    terminal_status: str,
    run_status: Mapping[str, Any],
) -> tuple[str, str]:
    ok = run_status.get("ok") is True
    terminal = run_status.get("terminal") is True
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
        "blocked_missing_visual_provider",
        "blocked_preflight",
        "blocked_parallel_execution",
        "budget_pruned_visual",
    }:
        return "provider_failure"
    if terminal_status == "blocked_missing_search_handoff":
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
    if missing_artifacts:
        return "missing required status artifact(s): " + ", ".join(missing_artifacts)
    if status == "excluded":
        return (
            f"{terminal_status} is valid for fixture validation but excluded from "
            "real-use Public Beta release metrics"
        )
    return f"terminal status {terminal_status} did not pass the Public Beta metric gate"


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
            }
        )
    expected_visual_providers = list(prompt.get("visual_provider_requirements", []))
    return {
        "supplied_real_run_directory": supplied_run,
        "prompt_id": prompt.get("id"),
        "route": prompt.get("route"),
        "mode_targets": list(prompt.get("mode_targets", [])),
        "selected_mode": run_status.get("selected_mode") or evidence.get("mode"),
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
    }


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
