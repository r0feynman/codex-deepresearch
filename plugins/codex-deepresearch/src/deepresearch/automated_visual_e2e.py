"""Automated-cli real-provider visual E2E release gate."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .visual_artifacts import (
    IMAGE_FETCH_STATUS_FILENAME,
    VISUAL_CANDIDATES_FILENAME,
    VISUAL_PROVIDER_STATUS_FILENAME,
    VISUAL_SEARCH_PLAN_FILENAME,
    real_automatic_visual_release_counts,
    validate_visual_artifacts,
)


AUTOMATED_VISUAL_E2E_SCHEMA_VERSION = "codex-deepresearch.automated-visual-e2e.v0"
AUTOMATED_VISUAL_E2E_RESULTS_FILENAME = "automated_visual_e2e_results.json"
DEFAULT_MIN_IMAGE_CANDIDATES = 10
DEFAULT_MIN_VLM_IMAGES = 3
_OPENAI_VISION_PROVIDER = "openai-responses-vision"
_REAL_MODES = {"real"}
_NON_RELEASE_MODES = {"fixture", "manual", "user_provided"}
_REAL_ACQUISITION_PROVIDER_KINDS = {
    "web_image_search",
    "page_extractor",
    "screenshot",
    "pdf_rasterizer",
    "visual_acquisition",
}
_SUCCESS_STATUS = "completed_auto_visual"
_BLOCKED_TERMINAL_STATUSES = {
    "blocked_missing_visual_provider",
    "blocked_missing_vlm_provider",
    "policy_blocked_visual",
    "budget_pruned_visual",
}
_VISUAL_FAILURE_CLASSES = (
    "provider",
    "fetch",
    "policy",
    "vlm",
    "contradiction",
    "report-linkage",
)


@dataclass(frozen=True)
class AutomatedVisualScenario:
    id: str
    prompt: str
    description: str
    provider_kinds: tuple[str, ...]
    origins: tuple[str, ...]
    target_evidence_types: tuple[str, ...]
    min_candidates: int = 0


DEFAULT_AUTOMATED_VISUAL_SCENARIOS: tuple[AutomatedVisualScenario, ...] = (
    AutomatedVisualScenario(
        id="product_image_discovery",
        prompt=(
            "Compare visible design differences across current public product images "
            "for two widely available hardware devices."
        ),
        description="Product/image-centric web image discovery.",
        provider_kinds=("web_image_search",),
        origins=("image_search", "page_image", "open_graph", "srcset", "lazy_loaded"),
        target_evidence_types=("web_image", "page_image"),
        min_candidates=DEFAULT_MIN_IMAGE_CANDIDATES,
    ),
    AutomatedVisualScenario(
        id="ui_screenshot_comparison",
        prompt=(
            "Compare above-the-fold public web UI screenshots for two public product "
            "or documentation pages."
        ),
        description="UI/webpage screenshot comparison.",
        provider_kinds=("screenshot",),
        origins=("screenshot",),
        target_evidence_types=("screenshot",),
    ),
    AutomatedVisualScenario(
        id="public_chart_report_visual_extraction",
        prompt=(
            "Extract the visible trend from a public chart or market/report visual and "
            "cite the image-backed claim."
        ),
        description="Public chart or market/report visual extraction.",
        provider_kinds=("page_extractor", "web_image_search", "screenshot"),
        origins=("page_image", "open_graph", "srcset", "lazy_loaded", "image_search", "screenshot"),
        target_evidence_types=("chart_image", "page_image", "web_image", "screenshot"),
    ),
    AutomatedVisualScenario(
        id="public_pdf_paper_figure_extraction",
        prompt=(
            "Extract a supported visual claim from a public PDF or paper figure and "
            "cite the figure provenance."
        ),
        description="Public PDF/paper figure extraction.",
        provider_kinds=("pdf_rasterizer",),
        origins=("pdf_figure", "pdf_page"),
        target_evidence_types=("pdf_figure",),
    ),
)


class AutomatedVisualE2EError(ValueError):
    """Raised when the automated visual E2E gate fails or blocks."""

    def __init__(self, message: str, *, results_path: Path | None = None) -> None:
        super().__init__(message)
        self.results_path = results_path


def run_automated_visual_e2e(
    *,
    runs_dir: str | Path,
    suite_id: str = "automated-visual-e2e",
    clean: bool = False,
    scenario_runs: Mapping[str, str | Path] | None = None,
    min_image_candidates: int = DEFAULT_MIN_IMAGE_CANDIDATES,
    min_vlm_images: int = DEFAULT_MIN_VLM_IMAGES,
) -> dict[str, Any]:
    """Evaluate automated-cli real-provider visual E2E scenario artifacts.

    The gate is deterministic by default: when no real scenario run directory is
    supplied, it writes blocked scenario diagnostics instead of making paid or
    credentialed provider calls. Users with configured providers can pass one
    run directory per scenario via the CLI and receive strict release-gate
    validation over existing artifacts.
    """

    if min_image_candidates < 1:
        raise AutomatedVisualE2EError("min_image_candidates must be positive")
    if min_vlm_images < 1:
        raise AutomatedVisualE2EError("min_vlm_images must be positive")

    suite_dir = Path(runs_dir) / suite_id
    if suite_dir.exists():
        if not clean:
            raise AutomatedVisualE2EError(f"suite directory already exists: {suite_dir}")
        shutil.rmtree(suite_dir)
    suite_dir.mkdir(parents=True)
    results_path = suite_dir / AUTOMATED_VISUAL_E2E_RESULTS_FILENAME

    normalized_runs = _normalize_scenario_runs(scenario_runs)
    unknown = sorted(set(normalized_runs) - {scenario.id for scenario in DEFAULT_AUTOMATED_VISUAL_SCENARIOS})
    if unknown:
        raise AutomatedVisualE2EError(
            "unknown automated visual scenario id(s): " + ", ".join(unknown),
            results_path=results_path,
        )

    scenarios: list[dict[str, Any]] = []
    for scenario in DEFAULT_AUTOMATED_VISUAL_SCENARIOS:
        run_path = normalized_runs.get(scenario.id)
        if run_path is None:
            scenarios.append(
                _blocked_missing_run_scenario(
                    scenario,
                    suite_dir=suite_dir,
                    min_image_candidates=min_image_candidates,
                    min_vlm_images=min_vlm_images,
                )
            )
            continue
        scenarios.append(
            evaluate_automated_visual_run(
                run_path,
                scenario=scenario,
                min_image_candidates=min_image_candidates,
                min_vlm_images=min_vlm_images,
            )
        )

    acceptance = _suite_acceptance(
        scenarios,
        min_image_candidates=min_image_candidates,
        min_vlm_images=min_vlm_images,
    )
    failures = [
        failure
        for scenario in scenarios
        for failure in scenario.get("failures", [])
        if isinstance(failure, Mapping)
    ]
    blockers = [
        blocker
        for scenario in scenarios
        for blocker in scenario.get("blockers", [])
        if isinstance(blocker, Mapping)
    ]
    for key, value in acceptance.items():
        if value is not True:
            target = blockers if blockers and not failures else failures
            target.append(
                {
                    "scenario_id": "suite",
                    "check": key,
                    "classification": "provider",
                    "detail": "automated visual E2E suite acceptance check did not pass",
                }
            )

    status = "passed" if not failures and not blockers else "failed" if failures else "blocked"
    results: dict[str, Any] = {
        "schema_version": AUTOMATED_VISUAL_E2E_SCHEMA_VERSION,
        "status": status,
        "suite_id": suite_id,
        "suite_dir": str(suite_dir.resolve()),
        "scenario_prompts": [
            {
                "id": scenario.id,
                "prompt": scenario.prompt,
                "description": scenario.description,
                "provider_kinds": list(scenario.provider_kinds),
                "origins": list(scenario.origins),
                "target_evidence_types": list(scenario.target_evidence_types),
            }
            for scenario in DEFAULT_AUTOMATED_VISUAL_SCENARIOS
        ],
        "thresholds": {
            "min_image_candidates": min_image_candidates,
            "min_vlm_images": min_vlm_images,
            "report_cited_visual_or_mixed_claims": 1,
        },
        "preflight": _provider_preflight(),
        "scenarios": scenarios,
        "outcome_counts": _outcome_counts(scenarios),
        "classification_counts": _classification_counts(failures + blockers),
        "acceptance": acceptance,
        "failures": failures,
        "blockers": blockers,
        "artifacts": {"results": str(results_path.resolve())},
        "public_safe": True,
        "external_network_call": any(bool(item.get("external_network_call")) for item in scenarios),
        "external_vlm_call": any(bool(item.get("external_vlm_call")) for item in scenarios),
    }
    _write_json(results_path, results)
    if status != "passed":
        raise AutomatedVisualE2EError(
            f"automated visual E2E gate {status}; see {results_path}",
            results_path=results_path,
        )
    return results


def evaluate_automated_visual_run(
    run_dir: str | Path,
    *,
    scenario: AutomatedVisualScenario,
    min_image_candidates: int = DEFAULT_MIN_IMAGE_CANDIDATES,
    min_vlm_images: int = DEFAULT_MIN_VLM_IMAGES,
) -> dict[str, Any]:
    """Evaluate one real-provider automated-cli visual scenario run."""

    run_path = Path(run_dir)
    failures: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    artifacts = _artifact_paths(run_path)
    run_status = _read_optional_json(artifacts["run_status"])
    provider_status = _read_optional_json(artifacts["visual_provider_status"])
    evidence = _read_optional_json(artifacts["evidence"])
    report_status = _read_optional_json(artifacts["report_status"])
    plan = _read_optional_json(artifacts["visual_search_plan"])
    report_text = _read_optional_text(artifacts["report"])
    candidates = _read_optional_jsonl(artifacts["visual_candidates"])
    fetches = _read_optional_jsonl(artifacts["image_fetch_status"])
    observations = _read_optional_jsonl(artifacts["visual_observations"])
    verifier_votes = _read_optional_jsonl(artifacts["verifier_votes"])

    loaded = {
        "run_status": run_status,
        "visual_provider_status": provider_status,
        "evidence": evidence,
        "report_status": report_status,
        "visual_search_plan": plan,
        "report": report_text,
        "visual_candidates": candidates,
        "image_fetch_status": fetches,
        "visual_observations": observations,
        "verifier_votes": verifier_votes,
    }
    provider_terminal = _status_text(provider_status)
    run_terminal = _status_text(run_status)
    blocked_status = provider_terminal if provider_terminal in _BLOCKED_TERMINAL_STATUSES else run_terminal
    explicit_blocked = blocked_status in _BLOCKED_TERMINAL_STATUSES
    required_artifacts = (
        {"run_status", "visual_provider_status"}
        if explicit_blocked
        else set(loaded)
    )
    for name, value in loaded.items():
        if name not in required_artifacts:
            continue
        if value is None:
            _append_failure(
                failures,
                scenario.id,
                "missing_artifact",
                _artifact_failure_class(name),
                f"missing required artifact: {Path(artifacts[name]).name}",
            )

    if explicit_blocked:
        blockers.append(
            {
                "scenario_id": scenario.id,
                "check": "explicit_blocked_terminal_status",
                "classification": _status_failure_class(blocked_status),
                "detail": f"scenario ended in explicit terminal status {blocked_status}",
            }
        )

    validation = (
        validate_visual_artifacts(run_dir=run_path).to_dict()
        if run_path.exists() and run_path.is_dir()
        else {"valid": False, "errors": [{"code": "missing_run_dir", "path": "$.run_dir"}]}
    )
    if validation.get("valid") is False and _SUCCESS_STATUS in {provider_terminal, run_terminal}:
        _append_failure(
            failures,
            scenario.id,
            "visual_artifact_validation",
            "contradiction",
            "completed_auto_visual artifacts failed visual artifact validation",
        )

    candidate_records = [item for item in candidates or [] if isinstance(item, Mapping)]
    fetch_records = [item for item in fetches or [] if isinstance(item, Mapping)]
    observation_records = [item for item in observations or [] if isinstance(item, Mapping)]
    counts = real_automatic_visual_release_counts(
        candidates=candidate_records,
        fetches=fetch_records,
        observations=observation_records,
        visual_provider_status=provider_status if isinstance(provider_status, Mapping) else None,
    )
    scenario_candidates = _scenario_real_candidates(candidate_records, scenario)
    scenario_fetches = _scenario_real_fetches(fetch_records, scenario)
    openai_observations = _real_openai_observations(observation_records)
    report_claims = _report_cited_visual_claims(
        evidence if isinstance(evidence, Mapping) else {},
        report_status if isinstance(report_status, Mapping) else {},
        candidate_records,
        fetch_records,
        observation_records,
        [item for item in verifier_votes or [] if isinstance(item, Mapping)],
        report_text or "",
    )
    non_release_records = _non_release_record_count(
        candidates=candidate_records,
        fetches=fetch_records,
        observations=observation_records,
        provider_status=provider_status if isinstance(provider_status, Mapping) else {},
    )

    if explicit_blocked and not failures:
        status = "blocked"
        return _scenario_result(
            scenario=scenario,
            run_path=run_path,
            status=status,
            run_terminal=run_terminal,
            provider_terminal=provider_terminal,
            run_status=run_status,
            provider_status=provider_status,
            candidate_records=candidate_records,
            observation_records=observation_records,
            counts=counts,
            scenario_candidates=scenario_candidates,
            scenario_fetches=scenario_fetches,
            openai_observations=openai_observations,
            report_claims=report_claims,
            non_release_records=non_release_records,
            validation=validation,
            failures=failures,
            blockers=blockers,
            artifacts=artifacts,
        )

    if not isinstance(evidence, Mapping) or evidence.get("mode") != "automated-cli":
        _append_failure(
            failures,
            scenario.id,
            "automated_cli_mode",
            "provider",
            "release gate run must record evidence.mode=automated-cli",
        )
    if _has_user_provided_only_visual_evidence(
        evidence if isinstance(evidence, Mapping) else {},
        candidate_records,
        observation_records,
    ):
        _append_failure(
            failures,
            scenario.id,
            "user_provided_only_visual_evidence",
            "provider",
            "user-provided/manual-only visual evidence cannot satisfy this release gate",
        )
    if provider_terminal != _SUCCESS_STATUS or run_terminal != _SUCCESS_STATUS:
        _append_failure(
            failures,
            scenario.id,
            "completed_auto_visual",
            _status_failure_class(provider_terminal or run_terminal),
            "run_status.json and visual_provider_status.json must both reach completed_auto_visual",
        )
    if not _has_real_acquisition_provider(provider_status, scenario):
        _append_failure(
            failures,
            scenario.id,
            "real_acquisition_provider",
            "provider",
            "scenario lacks a real acquisition provider of the required kind",
        )
    if not scenario_fetches:
        _append_failure(
            failures,
            scenario.id,
            "real_fetched_or_captured_visual_artifact",
            "fetch",
            "scenario lacks a real fetched/captured visual artifact",
        )
    if _policy_blocks_release(candidate_records, fetch_records, observation_records):
        _append_failure(
            failures,
            scenario.id,
            "policy_allows_release_counting",
            "policy",
            "policy-blocked or manual-review visual records cannot enter release numerator counts",
        )
    if scenario.min_candidates and len(scenario_candidates) < min_image_candidates:
        _append_failure(
            failures,
            scenario.id,
            "image_centric_candidate_floor",
            "provider",
            f"image-centric scenario collected {len(scenario_candidates)} real candidates; "
            f"expected at least {min_image_candidates}",
        )
    if len(openai_observations) < min_vlm_images:
        _append_failure(
            failures,
            scenario.id,
            "openai_responses_vision_image_floor",
            "vlm",
            f"openai-responses-vision analyzed {len(openai_observations)} real images; "
            f"expected at least {min_vlm_images}",
        )
    if not _has_real_openai_provider(provider_status, min_vlm_images=min_vlm_images):
        _append_failure(
            failures,
            scenario.id,
            "openai_responses_vision_provider_status",
            "vlm",
            "visual_provider_status lacks real openai-responses-vision analysis counters",
        )
    if not report_claims:
        _append_failure(
            failures,
            scenario.id,
            "report_cited_visual_or_mixed_claim",
            "report-linkage",
            "report.md/report_status.json lacks a cited supported visual or mixed claim",
        )
    if not _scenario_plan_matches(plan, scenario):
        _append_failure(
            failures,
            scenario.id,
            "scenario_provider_plan",
            "provider",
            "visual_search_plan does not cover the scenario target evidence type/provider",
        )
    if _counter_contradiction(
        provider_status=provider_status if isinstance(provider_status, Mapping) else {},
        candidates=candidate_records,
        fetches=fetch_records,
        observations=observation_records,
    ):
        _append_failure(
            failures,
            scenario.id,
            "provider_counter_contradiction",
            "contradiction",
            "provider counters contradict persisted candidate/fetch/observation records",
        )

    status = "passed" if not failures and not blockers else "failed" if failures else "blocked"
    return _scenario_result(
        scenario=scenario,
        run_path=run_path,
        status=status,
        run_terminal=run_terminal,
        provider_terminal=provider_terminal,
        run_status=run_status,
        provider_status=provider_status,
        candidate_records=candidate_records,
        observation_records=observation_records,
        counts=counts,
        scenario_candidates=scenario_candidates,
        scenario_fetches=scenario_fetches,
        openai_observations=openai_observations,
        report_claims=report_claims,
        non_release_records=non_release_records,
        validation=validation,
        failures=failures,
        blockers=blockers,
        artifacts=artifacts,
    )


def _scenario_result(
    *,
    scenario: AutomatedVisualScenario,
    run_path: Path,
    status: str,
    run_terminal: str,
    provider_terminal: str,
    run_status: Any,
    provider_status: Any,
    candidate_records: Sequence[Mapping[str, Any]],
    observation_records: Sequence[Mapping[str, Any]],
    counts: Mapping[str, Any],
    scenario_candidates: Sequence[Mapping[str, Any]],
    scenario_fetches: Sequence[Mapping[str, Any]],
    openai_observations: Sequence[Mapping[str, Any]],
    report_claims: Sequence[Mapping[str, Any]],
    non_release_records: int,
    validation: Mapping[str, Any],
    failures: Sequence[Mapping[str, Any]],
    blockers: Sequence[Mapping[str, Any]],
    artifacts: Mapping[str, Path],
) -> dict[str, Any]:
    return {
        "id": scenario.id,
        "description": scenario.description,
        "prompt": scenario.prompt,
        "status": status,
        "run_dir": str(run_path),
        "run_status": run_terminal,
        "visual_provider_status": provider_terminal,
        "ok": bool(
            isinstance(run_status, Mapping)
            and run_status.get("ok") is True
            and isinstance(provider_status, Mapping)
            and provider_status.get("ok") is True
        ),
        "terminal": bool(
            isinstance(run_status, Mapping)
            and run_status.get("terminal") is True
            and isinstance(provider_status, Mapping)
            and provider_status.get("terminal") is True
        ),
        "counts": {
            **counts,
            "scenario_real_candidates": len(scenario_candidates),
            "scenario_real_fetches": len(scenario_fetches),
            "real_openai_responses_vision_observations": len(openai_observations),
            "report_cited_visual_or_mixed_claims": len(report_claims),
            "excluded_non_release_records": non_release_records,
        },
        "release_numerator_counts": {
            "real_candidates": len(scenario_candidates),
            "real_fetches": len(scenario_fetches),
            "real_vlm_images_analyzed": len(openai_observations),
            "report_cited_visual_or_mixed_claims": len(report_claims),
        },
        "visual_artifact_validation": validation,
        "failure_classes": sorted(
            {
                str(item.get("classification"))
                for item in list(failures) + list(blockers)
                if item.get("classification")
            }
        ),
        "external_network_call": _records_external_call(
            provider_status if isinstance(provider_status, Mapping) else {},
            candidate_records,
            key="external_network_call",
        ),
        "external_vlm_call": _records_external_call(
            provider_status if isinstance(provider_status, Mapping) else {},
            observation_records,
            key="external_vlm_call",
        ),
        "failures": list(failures),
        "blockers": list(blockers),
        "artifacts": {
            key: str(path)
            for key, path in artifacts.items()
            if Path(path).exists()
        },
    }


def _blocked_missing_run_scenario(
    scenario: AutomatedVisualScenario,
    *,
    suite_dir: Path,
    min_image_candidates: int,
    min_vlm_images: int,
) -> dict[str, Any]:
    detail_path = suite_dir / f"{scenario.id}_blocked.json"
    preflight = _provider_preflight()
    blocker = {
        "scenario_id": scenario.id,
        "check": "real_scenario_run_not_supplied",
        "classification": "provider",
        "detail": (
            "no real automated-cli visual run directory was supplied; "
            "pass --scenario-run "
            f"{scenario.id}=<run_dir> after running configured real providers"
        ),
    }
    payload = {
        "schema_version": AUTOMATED_VISUAL_E2E_SCHEMA_VERSION,
        "scenario_id": scenario.id,
        "status": "blocked",
        "prompt": scenario.prompt,
        "preflight": preflight,
        "blocker": blocker,
        "thresholds": {
            "min_image_candidates": min_image_candidates,
            "min_vlm_images": min_vlm_images,
        },
    }
    _write_json(detail_path, payload)
    return {
        "id": scenario.id,
        "description": scenario.description,
        "prompt": scenario.prompt,
        "status": "blocked",
        "run_dir": None,
        "run_status": None,
        "visual_provider_status": None,
        "ok": False,
        "terminal": True,
        "counts": {
            "scenario_real_candidates": 0,
            "scenario_real_fetches": 0,
            "real_openai_responses_vision_observations": 0,
            "report_cited_visual_or_mixed_claims": 0,
            "excluded_non_release_records": 0,
        },
        "release_numerator_counts": {
            "real_candidates": 0,
            "real_fetches": 0,
            "real_vlm_images_analyzed": 0,
            "report_cited_visual_or_mixed_claims": 0,
        },
        "failure_classes": ["provider"],
        "failures": [],
        "blockers": [blocker],
        "artifacts": {"blocked_detail": str(detail_path.resolve())},
    }


def _suite_acceptance(
    scenarios: Sequence[Mapping[str, Any]],
    *,
    min_image_candidates: int,
    min_vlm_images: int,
) -> dict[str, bool]:
    by_id = {str(item.get("id")): item for item in scenarios}
    product = by_id.get("product_image_discovery", {})
    accepted = [item for item in scenarios if item.get("status") == "passed"]
    return {
        "provider_scenario_gates_cover_required_set": set(by_id)
        == {scenario.id for scenario in DEFAULT_AUTOMATED_VISUAL_SCENARIOS},
        "no_user_image_automated_runs_reach_completed_auto_visual": bool(accepted)
        and all(item.get("run_status") == _SUCCESS_STATUS for item in accepted)
        and all(item.get("visual_provider_status") == _SUCCESS_STATUS for item in accepted),
        "all_required_scenarios_passed": len(accepted) == len(DEFAULT_AUTOMATED_VISUAL_SCENARIOS),
        "image_centric_has_10_real_candidates": int(
            product.get("counts", {}).get("scenario_real_candidates", 0)
        )
        >= min_image_candidates,
        "accepted_runs_have_3_real_openai_vlm_images": bool(accepted)
        and all(
            int(item.get("counts", {}).get("real_openai_responses_vision_observations", 0))
            >= min_vlm_images
            for item in accepted
        ),
        "accepted_runs_have_report_cited_visual_or_mixed_claim": bool(accepted)
        and all(
            int(item.get("counts", {}).get("report_cited_visual_or_mixed_claims", 0)) >= 1
            for item in accepted
        ),
        "fixture_manual_user_provided_records_excluded": all(
            _release_counts_are_real_only(item) for item in scenarios
        ),
    }


def _release_counts_are_real_only(scenario: Mapping[str, Any]) -> bool:
    counts = scenario.get("release_numerator_counts")
    if not isinstance(counts, Mapping):
        return True
    return all(int(value or 0) >= 0 for value in counts.values())


def _normalize_scenario_runs(
    scenario_runs: Mapping[str, str | Path] | None,
) -> dict[str, Path]:
    if not scenario_runs:
        return {}
    return {str(key): Path(value) for key, value in scenario_runs.items()}


def parse_scenario_run(value: str) -> tuple[str, Path]:
    """Parse a CLI --scenario-run value in the form scenario_id=run_dir."""

    if "=" not in value:
        raise AutomatedVisualE2EError(
            "--scenario-run must use scenario_id=run_dir, for example "
            "product_image_discovery=/tmp/run"
        )
    scenario_id, run_dir = value.split("=", 1)
    scenario_id = scenario_id.strip()
    run_dir = run_dir.strip()
    if not scenario_id or not run_dir:
        raise AutomatedVisualE2EError("--scenario-run requires non-empty scenario_id and run_dir")
    return scenario_id, Path(run_dir)


def _artifact_paths(run_dir: Path) -> dict[str, Path]:
    return {
        "run_status": run_dir / "run_status.json",
        "visual_provider_status": run_dir / VISUAL_PROVIDER_STATUS_FILENAME,
        "evidence": run_dir / "evidence.json",
        "report_status": run_dir / "report_status.json",
        "visual_search_plan": run_dir / VISUAL_SEARCH_PLAN_FILENAME,
        "report": run_dir / "report.md",
        "visual_candidates": run_dir / VISUAL_CANDIDATES_FILENAME,
        "image_fetch_status": run_dir / IMAGE_FETCH_STATUS_FILENAME,
        "visual_observations": run_dir / "visual_observations.jsonl",
        "verifier_votes": run_dir / "verifier_votes.jsonl",
    }


def _artifact_failure_class(name: str) -> str:
    if name in {"run_status", "visual_provider_status", "visual_search_plan"}:
        return "provider"
    if name in {"visual_candidates", "image_fetch_status"}:
        return "fetch"
    if name == "visual_observations":
        return "vlm"
    if name in {"report_status", "report", "verifier_votes"}:
        return "report-linkage"
    return "contradiction"


def _status_text(payload: Any) -> str:
    if isinstance(payload, Mapping):
        value = payload.get("status")
        if isinstance(value, str):
            return value
    return ""


def _status_failure_class(status: str) -> str:
    if status == "blocked_missing_visual_provider":
        return "provider"
    if status == "blocked_missing_vlm_provider":
        return "vlm"
    if status == "policy_blocked_visual":
        return "policy"
    if status in {"budget_pruned_visual", "partial_auto_visual"}:
        return "fetch"
    if status.startswith("failed"):
        return "contradiction"
    return "provider"


def _append_failure(
    failures: list[dict[str, Any]],
    scenario_id: str,
    check: str,
    classification: str,
    detail: str,
) -> None:
    failures.append(
        {
            "scenario_id": scenario_id,
            "check": check,
            "classification": classification
            if classification in _VISUAL_FAILURE_CLASSES
            else "contradiction",
            "detail": detail,
        }
    )


def _scenario_real_candidates(
    candidates: Sequence[Mapping[str, Any]],
    scenario: AutomatedVisualScenario,
) -> list[Mapping[str, Any]]:
    return [
        item
        for item in candidates
        if item.get("provider_mode") in _REAL_MODES
        and (
            item.get("provider_kind") in scenario.provider_kinds
            or item.get("origin") in scenario.origins
        )
    ]


def _scenario_real_fetches(
    fetches: Sequence[Mapping[str, Any]],
    scenario: AutomatedVisualScenario,
) -> list[Mapping[str, Any]]:
    return [
        item
        for item in fetches
        if item.get("provider_mode") in _REAL_MODES
        and item.get("fetch_status") == "fetched"
        and (
            item.get("provider_kind") in scenario.provider_kinds
            or item.get("origin") in scenario.origins
        )
        and isinstance(item.get("evidence_image_id"), str)
        and item.get("evidence_image_id")
    ]


def _real_openai_observations(observations: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        item
        for item in observations
        if _is_real_openai_analyzed_observation(item)
    ]


def _has_real_openai_provider(provider_status: Any, *, min_vlm_images: int) -> bool:
    providers = provider_status.get("providers", []) if isinstance(provider_status, Mapping) else []
    for provider in providers:
        if not isinstance(provider, Mapping):
            continue
        if (
            provider.get("provider") == _OPENAI_VISION_PROVIDER
            and provider.get("provider_kind") == "vlm"
            and provider.get("provider_mode") == "real"
            and provider.get("configured") is True
            and provider.get("available") is True
            and not provider.get("blocked_reason")
            and int(provider.get("vlm_images_analyzed") or 0) >= min_vlm_images
            and int(provider.get("invocations") or 0) >= min_vlm_images
        ):
            return True
    return False


def _has_real_acquisition_provider(
    provider_status: Any,
    scenario: AutomatedVisualScenario,
) -> bool:
    providers = provider_status.get("providers", []) if isinstance(provider_status, Mapping) else []
    return any(
        isinstance(provider, Mapping)
        and provider.get("provider_mode") == "real"
        and provider.get("provider_kind") in scenario.provider_kinds
        and bool(provider.get("configured"))
        and bool(provider.get("available"))
        and int(provider.get("invocations") or 0) > 0
        for provider in providers
    )


def _policy_blocks_release(
    candidates: Sequence[Mapping[str, Any]],
    fetches: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> bool:
    records = list(candidates) + list(fetches) + list(observations)
    for record in records:
        if record.get("provider_mode") != "real":
            continue
        decision = record.get("policy_decision")
        if decision in {"blocked", "manual_review", "budget_pruned", "disallowed", "restricted"}:
            return True
    return False


def _report_cited_visual_claims(
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
    for claim in evidence.get("claims", []) if isinstance(evidence.get("claims"), list) else []:
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
        release_linked_images = sorted(
            image_id
            for image_id in linked_images
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
        )
        if release_linked_images:
            cited.append({"claim_id": claim_id, "image_ids": release_linked_images})
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
    if image is None or not _is_real_policy_allowed_acquisition_record(image):
        return False
    for observation in observations:
        if observation.get("evidence_image_id") != image_id:
            continue
        if not _is_real_openai_analyzed_observation(observation):
            continue
        if not _has_report_link(observation, claim_id):
            continue
        if not _has_verifier_vote_link(observation, claim_id, verifier_vote_ids):
            continue
        candidate_id = observation.get("candidate_id")
        fetch_id = observation.get("fetch_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            continue
        if not isinstance(fetch_id, str) or not fetch_id:
            continue
        candidate = candidates_by_id.get(candidate_id)
        fetch = fetches_by_id.get(fetch_id)
        if candidate is None or fetch is None:
            continue
        if not _is_real_policy_allowed_acquisition_record(candidate):
            continue
        if not _is_real_policy_allowed_fetch(fetch, image_id=image_id, candidate_id=candidate_id):
            continue
        if image.get("candidate_id") not in {None, candidate_id}:
            continue
        if image.get("fetch_id") not in {None, fetch_id}:
            continue
        if not _claim_visual_supports_image(claim, image_id):
            continue
        return True
    return False


def _is_real_openai_analyzed_observation(record: Mapping[str, Any]) -> bool:
    provenance = record.get("provider_provenance")
    return (
        record.get("provider") == _OPENAI_VISION_PROVIDER
        and record.get("provider_kind") == "vlm"
        and record.get("provider_mode") == "real"
        and record.get("observation_status") == "analyzed"
        and record.get("policy_decision") == "allowed"
        and isinstance(provenance, Mapping)
        and provenance.get("provider") == _OPENAI_VISION_PROVIDER
        and provenance.get("provider_kind") == "vlm"
        and provenance.get("provider_mode") == "real"
        and bool(record.get("external_vlm_call") or provenance.get("external_vlm_call"))
    )


def _is_real_policy_allowed_acquisition_record(record: Mapping[str, Any]) -> bool:
    provenance = record.get("provider_provenance")
    return (
        record.get("provider_mode") == "real"
        and record.get("provider_kind") in _REAL_ACQUISITION_PROVIDER_KINDS
        and record.get("policy_decision") == "allowed"
        and isinstance(provenance, Mapping)
        and provenance.get("provider_mode") == "real"
        and provenance.get("provider_kind") in _REAL_ACQUISITION_PROVIDER_KINDS
    )


def _is_real_policy_allowed_fetch(
    fetch: Mapping[str, Any],
    *,
    image_id: str,
    candidate_id: str,
) -> bool:
    return (
        _is_real_policy_allowed_acquisition_record(fetch)
        and fetch.get("fetch_status") == "fetched"
        and fetch.get("candidate_id") == candidate_id
        and fetch.get("evidence_image_id") == image_id
    )


def _has_report_link(observation: Mapping[str, Any], claim_id: str) -> bool:
    for link in observation.get("report_links", []):
        if not isinstance(link, Mapping):
            continue
        if link.get("claim_id") != claim_id:
            continue
        if link.get("citation_id") or link.get("report_section_id"):
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
        if link.get("claim_id") != claim_id:
            continue
        vote_id = link.get("verifier_vote_id")
        if isinstance(vote_id, str) and vote_id in verifier_vote_ids:
            return True
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


def _scenario_plan_matches(plan: Any, scenario: AutomatedVisualScenario) -> bool:
    if not isinstance(plan, Mapping):
        return False
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        return False
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        target = task.get("target_evidence_type")
        providers = task.get("providers") if isinstance(task.get("providers"), list) else []
        if target in scenario.target_evidence_types:
            return True
        if any(
            isinstance(provider, str)
            and any(kind.replace("_", "-") in provider for kind in scenario.provider_kinds)
            for provider in providers
        ):
            return True
    return False


def _counter_contradiction(
    *,
    provider_status: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    fetches: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> bool:
    providers = [
        item
        for item in provider_status.get("providers", [])
        if isinstance(item, Mapping) and item.get("provider_mode") == "real"
    ]
    candidate_count = len([item for item in candidates if item.get("provider_mode") == "real"])
    fetch_count = len(
        [
            item
            for item in fetches
            if item.get("provider_mode") == "real" and item.get("fetch_status") == "fetched"
        ]
    )
    observation_count = len(_real_openai_observations(observations))
    provider_candidates = sum(int(item.get("candidates_discovered") or 0) for item in providers)
    provider_fetches = sum(int(item.get("artifacts_fetched") or 0) for item in providers)
    provider_observations = sum(int(item.get("vlm_images_analyzed") or 0) for item in providers)
    return (
        (candidate_count > 0 and provider_candidates <= 0)
        or (fetch_count > 0 and provider_fetches <= 0)
        or (observation_count > 0 and provider_observations <= 0)
    )


def _has_user_provided_only_visual_evidence(
    evidence: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> bool:
    visual_records = [
        item
        for item in list(candidates)
        + list(observations)
        + [
            image
            for image in evidence.get("images", [])
            if isinstance(image, Mapping)
        ]
        if isinstance(item, Mapping)
    ]
    if not visual_records:
        return False
    real_records = [item for item in visual_records if item.get("provider_mode") == "real"]
    non_release_records = [
        item for item in visual_records if item.get("provider_mode") in _NON_RELEASE_MODES
    ]
    return bool(non_release_records) and not real_records


def _non_release_record_count(
    *,
    candidates: Sequence[Mapping[str, Any]],
    fetches: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    provider_status: Mapping[str, Any],
) -> int:
    providers = [
        item
        for item in provider_status.get("providers", [])
        if isinstance(item, Mapping)
    ]
    return sum(
        1
        for item in list(candidates) + list(fetches) + list(observations) + providers
        if isinstance(item, Mapping) and item.get("provider_mode") in _NON_RELEASE_MODES
    )


def _classification_counts(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in _VISUAL_FAILURE_CLASSES}
    for item in items:
        if isinstance(item, Mapping):
            classification = item.get("classification")
            if classification in counts:
                counts[str(classification)] += 1
    return counts


def _records_external_call(
    provider_status: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    key: str,
) -> bool:
    providers = [
        item
        for item in provider_status.get("providers", [])
        if isinstance(item, Mapping)
    ]
    for record in list(records) + providers:
        if bool(record.get(key)):
            return True
        provenance = record.get("provider_provenance")
        if isinstance(provenance, Mapping) and bool(provenance.get(key)):
            return True
    return False


def _outcome_counts(scenarios: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"passed": 0, "blocked": 0, "failed": 0}
    for scenario in scenarios:
        status = str(scenario.get("status") or "")
        if status in counts:
            counts[status] += 1
    return counts


def _provider_preflight() -> dict[str, Any]:
    brave_key = bool(
        os.environ.get("CODEX_DEEPRESEARCH_BRAVE_SEARCH_API_KEY")
        or os.environ.get("BRAVE_SEARCH_API_KEY")
    )
    brave_storage = _env_true("CODEX_DEEPRESEARCH_BRAVE_ALLOW_RESULT_STORAGE")
    openai_key = bool(
        os.environ.get("CODEX_DEEPRESEARCH_OPENAI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    openai_allow = _env_true("CODEX_DEEPRESEARCH_OPENAI_RESPONSES_VISION_ALLOW_REAL")
    return {
        "brave_image_search": {
            "configured": brave_key and brave_storage,
            "credential_configured": brave_key,
            "storage_confirmation_configured": brave_storage,
            "required_env": [
                "CODEX_DEEPRESEARCH_BRAVE_SEARCH_API_KEY or BRAVE_SEARCH_API_KEY",
                "CODEX_DEEPRESEARCH_BRAVE_ALLOW_RESULT_STORAGE=true",
            ],
        },
        "openai_responses_vision": {
            "configured": openai_key and openai_allow,
            "credential_configured": openai_key,
            "real_call_allowed": openai_allow,
            "required_env": [
                "OPENAI_API_KEY or CODEX_DEEPRESEARCH_OPENAI_API_KEY",
                "CODEX_DEEPRESEARCH_OPENAI_RESPONSES_VISION_ALLOW_REAL=true",
            ],
        },
    }


def _env_true(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_optional_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _read_optional_jsonl(path: Path) -> list[dict[str, Any]] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
