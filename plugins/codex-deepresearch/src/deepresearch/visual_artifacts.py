"""Phase 3 visual artifact schemas and automatic visual status helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .evidence_schema import SEARCH_ROUTES, ValidationError, ValidationResult


VISUAL_ARTIFACT_SCHEMA_VERSION = "codex-deepresearch.visual-artifacts.v0"
VISUAL_PROVIDER_STATUS_SCHEMA_VERSION = "codex-deepresearch.visual-provider-status.v0"
VISUAL_SEARCH_PLAN_FILENAME = "visual_search_plan.json"
VISUAL_CANDIDATES_FILENAME = "visual_candidates.jsonl"
IMAGE_FETCH_STATUS_FILENAME = "image_fetch_status.jsonl"
VISUAL_PROVIDER_STATUS_FILENAME = "visual_provider_status.json"
DEFAULT_REQUIRED_VISUAL_VLM_IMAGES = 3
CODEX_INTERACTIVE_PROVIDER = "codex-interactive"

AUTOMATIC_VISUAL_TERMINAL_STATUSES = (
    "completed_auto_visual",
    "partial_auto_visual",
    "blocked_missing_visual_provider",
    "blocked_missing_vlm_provider",
    "policy_blocked_visual",
    "budget_pruned_visual",
)
CANDIDATE_PROVIDER_KINDS = (
    "web_image_search",
    "page_extractor",
    "screenshot",
    "pdf_rasterizer",
    "manual",
    "fixture",
)
OBSERVATION_PROVIDER_KINDS = CANDIDATE_PROVIDER_KINDS + ("vlm",)
STATUS_PROVIDER_KINDS = CANDIDATE_PROVIDER_KINDS + (
    "visual_acquisition",
    "vlm",
)
REAL_ACQUISITION_PROVIDER_KINDS = (
    "web_image_search",
    "page_extractor",
    "screenshot",
    "pdf_rasterizer",
    "visual_acquisition",
)
REAL_OBSERVATION_PROVIDER_KINDS = REAL_ACQUISITION_PROVIDER_KINDS + (
    "vlm",
)
PROVIDER_MODES = ("real", "fixture", "manual", "user_provided")
VISUAL_MINIMUM_SHORTFALL_REASONS = (
    "none",
    "insufficient_candidates",
    "fetch_failures",
    "vlm_failures",
    "policy_blocked",
    "budget_pruned",
    "report_linkage_missing",
)
TARGET_EVIDENCE_TYPES = (
    "web_image",
    "page_image",
    "screenshot",
    "pdf_figure",
    "chart_image",
)
PLAN_STATES = ("planned", "running", "completed", "blocked", "skipped")
VISUAL_ORIGINS = (
    "image_search",
    "page_image",
    "open_graph",
    "srcset",
    "lazy_loaded",
    "screenshot",
    "pdf_figure",
    "pdf_page",
)
POLICY_DECISIONS = ("allowed", "blocked", "manual_review", "budget_pruned")
CANDIDATE_STATUSES = (
    "discovered",
    "ranked",
    "selected",
    "rejected",
    "policy_blocked",
    "budget_pruned",
    "fetch_failed",
    "fetched",
    "analyzed",
)
FETCH_STATUSES = (
    "fetched",
    "failed",
    "skipped",
    "policy_blocked",
    "budget_pruned",
    "unsupported_mime",
    "too_large",
    "deduped",
)
OBSERVATION_STATUSES = (
    "analyzed",
    "failed",
    "skipped",
    "needs_manual_review",
    "policy_blocked",
)


class _Collector:
    def __init__(self) -> None:
        self.errors: list[ValidationError] = []

    def add(self, path: str, code: str, message: str) -> None:
        self.errors.append(ValidationError(path=path, code=code, message=message))


class _VisualContext:
    def __init__(self) -> None:
        self.run_dir_validation = False
        self.task_by_id: dict[str, Mapping[str, Any]] = {}
        self.plan_by_id: dict[str, Mapping[str, Any]] = {}
        self.candidate_by_id: dict[str, Mapping[str, Any]] = {}
        self.fetch_by_id: dict[str, Mapping[str, Any]] = {}
        self.fetches_by_candidate_id: dict[str, list[Mapping[str, Any]]] = {}
        self.image_by_id: dict[str, Mapping[str, Any]] = {}
        self.claim_by_id: dict[str, Mapping[str, Any]] = {}
        self.verifier_vote_ids: set[str] = set()
        self.search_result_ids: set[str] = set()
        self.observation_links_by_image_claim: set[tuple[str, str, str]] = set()
        self.report_used_image_ids: set[str] = set()


def automatic_visual_status_envelope(
    status: str,
    *,
    visual_required: bool = True,
    low_budget_excluded: bool = False,
    policy_enforced: bool = True,
) -> dict[str, Any]:
    """Return PRD-defined ok/terminal/metric behavior for visual terminal states."""

    if status == "completed_auto_visual":
        return {"ok": True, "terminal": True, "metric_classification": "success"}
    if status == "partial_auto_visual":
        return {
            "ok": not visual_required,
            "terminal": True,
            "metric_classification": "included_failure",
        }
    if status in {"blocked_missing_visual_provider", "blocked_missing_vlm_provider"}:
        return {
            "ok": False,
            "terminal": True,
            "metric_classification": "excluded_blocked",
        }
    if status == "policy_blocked_visual":
        return {
            "ok": bool(policy_enforced),
            "terminal": True,
            "metric_classification": "excluded_policy_blocked",
        }
    if status == "budget_pruned_visual":
        return {
            "ok": bool(low_budget_excluded),
            "terminal": True,
            "metric_classification": (
                "excluded_budget_pruned" if low_budget_excluded else "included_failure"
            ),
        }
    raise ValueError(
        "automatic visual status must be one of: "
        + ", ".join(AUTOMATIC_VISUAL_TERMINAL_STATUSES)
    )


def is_real_automatic_visual_record(record: Mapping[str, Any]) -> bool:
    """Return whether a record can count toward real automatic visual release metrics."""

    return (
        record.get("provider_mode") == "real"
        and record.get("provider_kind") in REAL_OBSERVATION_PROVIDER_KINDS
    )


def _is_real_acquisition_record(record: Mapping[str, Any]) -> bool:
    return (
        record.get("provider_mode") == "real"
        and record.get("provider_kind") in REAL_ACQUISITION_PROVIDER_KINDS
    )


def _is_real_observation_record(record: Mapping[str, Any]) -> bool:
    return (
        record.get("provider_mode") == "real"
        and record.get("provider_kind") in REAL_OBSERVATION_PROVIDER_KINDS
    )


def real_automatic_visual_release_counts(
    *,
    candidates: Sequence[Mapping[str, Any]] = (),
    fetches: Sequence[Mapping[str, Any]] = (),
    observations: Sequence[Mapping[str, Any]] = (),
    visual_provider_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Count only real automatic visual records; fixture/manual/user records are excluded."""

    providers = visual_provider_status.get("providers", []) if visual_provider_status else []
    provider_records = [item for item in providers if isinstance(item, Mapping)]
    real_providers = [item for item in provider_records if _is_real_acquisition_record(item)]
    excluded_providers = [
        item for item in provider_records if not _is_real_acquisition_record(item)
    ]
    return {
        "real_candidates": sum(1 for item in candidates if _is_real_acquisition_record(item)),
        "real_fetches": sum(1 for item in fetches if _is_real_acquisition_record(item)),
        "real_observations": sum(
            1 for item in observations if _is_real_observation_record(item)
        ),
        "real_provider_invocations": sum(_int(item.get("invocations")) for item in real_providers),
        "real_candidates_discovered": sum(
            _int(item.get("candidates_discovered")) for item in real_providers
        ),
        "real_artifacts_fetched": sum(
            _int(item.get("artifacts_fetched")) for item in real_providers
        ),
        "real_vlm_images_analyzed": sum(
            _int(item.get("vlm_images_analyzed")) for item in real_providers
        ),
        "excluded_non_real_provider_records": len(excluded_providers),
    }


def required_vlm_images_for_evidence(evidence: Mapping[str, Any] | None) -> int:
    """Return the configured visual-required VLM minimum for a run."""

    if not isinstance(evidence, Mapping):
        return DEFAULT_REQUIRED_VISUAL_VLM_IMAGES
    routing = evidence.get("routing")
    if not isinstance(routing, list):
        return DEFAULT_REQUIRED_VISUAL_VLM_IMAGES
    visual_required = any(
        isinstance(route, Mapping) and route.get("modality") == "visual_required"
        for route in routing
    )
    return DEFAULT_REQUIRED_VISUAL_VLM_IMAGES if visual_required else 0


def visual_minimums_for_run(
    run_dir: str | Path,
    *,
    required_vlm_images: int | None = None,
) -> dict[str, Any]:
    """Read run artifacts and compute PRD visual minimum diagnostics."""

    base = Path(run_dir)
    evidence = _read_optional_artifact_json(base / "evidence.json")
    report_status = _read_optional_artifact_json(base / "report_status.json")
    required = (
        required_vlm_images
        if required_vlm_images is not None
        else required_vlm_images_for_evidence(evidence)
    )
    return visual_release_minimums(
        candidates=_read_optional_artifact_jsonl(base / VISUAL_CANDIDATES_FILENAME),
        fetches=_read_optional_artifact_jsonl(base / IMAGE_FETCH_STATUS_FILENAME),
        observations=_read_optional_artifact_jsonl(base / "visual_observations.jsonl"),
        evidence=evidence,
        report_status=report_status,
        required_vlm_images=required,
    )


def visual_release_minimums(
    *,
    candidates: Sequence[Mapping[str, Any]] = (),
    fetches: Sequence[Mapping[str, Any]] = (),
    observations: Sequence[Mapping[str, Any]] = (),
    evidence: Mapping[str, Any] | None = None,
    report_status: Mapping[str, Any] | None = None,
    required_vlm_images: int | None = None,
) -> dict[str, Any]:
    """Count only real automatic lineage eligible for visual release gates."""

    required = (
        required_vlm_images
        if required_vlm_images is not None
        else required_vlm_images_for_evidence(evidence)
    )
    required = max(0, int(required))
    candidate_records = [item for item in candidates if isinstance(item, Mapping)]
    fetch_records = [item for item in fetches if isinstance(item, Mapping)]
    observation_records = [item for item in observations if isinstance(item, Mapping)]
    real_candidates = [item for item in candidate_records if _is_real_acquisition_record(item)]
    real_candidate_ids = {
        str(item.get("candidate_id") or item.get("id"))
        for item in real_candidates
        if item.get("candidate_id") or item.get("id")
    }
    selected_candidates = [
        item for item in real_candidates if _candidate_was_selected_or_attempted(item)
    ]
    real_fetch_records = [item for item in fetch_records if _is_real_acquisition_record(item)]
    real_fetched_artifacts = [
        item
        for item in real_fetch_records
        if item.get("fetch_status") == "fetched"
        and _has_nonempty_string(item.get("local_artifact_path"))
        and (
            not real_candidate_ids
            or str(item.get("candidate_id") or "") in real_candidate_ids
        )
    ]
    real_fetch_ids = {
        str(item.get("fetch_id"))
        for item in real_fetched_artifacts
        if _has_nonempty_string(item.get("fetch_id"))
    }
    real_fetch_image_ids = {
        str(item.get("evidence_image_id"))
        for item in real_fetched_artifacts
        if _has_nonempty_string(item.get("evidence_image_id"))
    }
    eligible_observations = [
        item
        for item in observation_records
        if _is_release_eligible_codex_observation(
            item,
            real_candidate_ids=real_candidate_ids,
            real_fetch_ids=real_fetch_ids,
            real_fetch_image_ids=real_fetch_image_ids,
        )
    ]
    eligible_observation_image_ids = {
        str(item.get("evidence_image_id"))
        for item in eligible_observations
        if _has_nonempty_string(item.get("evidence_image_id"))
    }
    report_cited_image_ids = _report_cited_visual_image_ids(
        evidence=evidence,
        report_status=report_status,
        eligible_image_ids=eligible_observation_image_ids,
    )
    satisfied = (
        required == 0
        or (
            len(real_fetched_artifacts) >= required
            and len(eligible_observations) >= required
            and len(report_cited_image_ids) >= 1
        )
    )
    shortfall_reason = _visual_shortfall_reason(
        required_vlm_images=required,
        candidate_count=len(real_candidates),
        selected_candidates=len(selected_candidates),
        fetched_artifacts=len(real_fetched_artifacts),
        vlm_images_analyzed=len(eligible_observations),
        report_cited_images=len(report_cited_image_ids),
        fetch_records=real_fetch_records,
        evidence=evidence,
        satisfied=satisfied,
    )
    return {
        "required_vlm_images": required,
        "candidate_count": len(real_candidates),
        "selected_candidates": len(selected_candidates),
        "fetched_artifacts": len(real_fetched_artifacts),
        "vlm_images_analyzed": len(eligible_observations),
        "report_cited_images": len(report_cited_image_ids),
        "satisfied": satisfied,
        "shortfall_reason": shortfall_reason,
    }


def visual_failure_code_for_minimums(minimums: Mapping[str, Any] | None) -> str | None:
    if not isinstance(minimums, Mapping):
        return None
    reason = str(minimums.get("shortfall_reason") or "none")
    if reason == "none":
        return None
    if reason == "report_linkage_missing":
        return "visual_report_linkage_missing"
    return "visual_minimum_shortfall"


def _candidate_was_selected_or_attempted(record: Mapping[str, Any]) -> bool:
    candidate_status = str(record.get("candidate_status") or "")
    if candidate_status in {
        "fetched",
        "analyzed",
        "fetch_failed",
        "policy_blocked",
        "rejected",
    }:
        return True
    if candidate_status == "budget_pruned":
        return False
    status = str(record.get("status") or "")
    return status in {"accepted", "removed", "fetch_failed"}


def _is_release_eligible_codex_observation(
    observation: Mapping[str, Any],
    *,
    real_candidate_ids: set[str],
    real_fetch_ids: set[str],
    real_fetch_image_ids: set[str],
) -> bool:
    if observation.get("provider") != CODEX_INTERACTIVE_PROVIDER:
        return False
    if observation.get("provider_kind") != "vlm":
        return False
    if observation.get("provider_mode") != "real":
        return False
    if observation.get("observation_status") != "analyzed":
        return False
    candidate_id = str(observation.get("candidate_id") or "")
    fetch_id = str(observation.get("fetch_id") or "")
    image_id = str(observation.get("evidence_image_id") or "")
    if real_candidate_ids and candidate_id not in real_candidate_ids:
        return False
    if real_fetch_ids and fetch_id not in real_fetch_ids:
        return False
    if real_fetch_image_ids and image_id not in real_fetch_image_ids:
        return False
    return bool(image_id)


def _report_cited_visual_image_ids(
    *,
    evidence: Mapping[str, Any] | None,
    report_status: Mapping[str, Any] | None,
    eligible_image_ids: set[str],
) -> set[str]:
    if not isinstance(evidence, Mapping) or not isinstance(report_status, Mapping):
        return set()
    used_images = report_status.get("used_images")
    if not isinstance(used_images, list):
        return set()
    used_image_ids = {
        image_id for image_id in used_images if isinstance(image_id, str) and image_id
    }
    claims = evidence.get("claims")
    if not isinstance(claims, list):
        return set()
    cited: set[str] = set()
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        if claim.get("verification_status") != "supported":
            continue
        if claim.get("claim_type") not in {"visual", "mixed"}:
            continue
        supporting_images = claim.get("supporting_images")
        if not isinstance(supporting_images, list):
            continue
        for image_id in supporting_images:
            if (
                isinstance(image_id, str)
                and image_id in used_image_ids
                and image_id in eligible_image_ids
            ):
                cited.add(image_id)
    return cited


def _visual_shortfall_reason(
    *,
    required_vlm_images: int,
    candidate_count: int,
    selected_candidates: int,
    fetched_artifacts: int,
    vlm_images_analyzed: int,
    report_cited_images: int,
    fetch_records: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any] | None,
    satisfied: bool,
) -> str:
    if satisfied or required_vlm_images <= 0:
        return "none"
    if candidate_count < required_vlm_images:
        return "insufficient_candidates"
    if fetched_artifacts < required_vlm_images:
        statuses = {
            str(record.get("fetch_status") or "")
            for record in fetch_records
            if record.get("fetch_status")
        }
        if statuses and statuses <= {"policy_blocked"}:
            return "policy_blocked"
        if "budget_pruned" in statuses or _configured_image_cap_below_required(
            evidence,
            required_vlm_images,
        ):
            return "budget_pruned"
        if statuses & {"failed", "unsupported_mime", "too_large", "deduped", "skipped"}:
            return "fetch_failures"
        if selected_candidates < required_vlm_images:
            return "insufficient_candidates"
        return "fetch_failures"
    if vlm_images_analyzed < required_vlm_images:
        if _configured_image_cap_below_required(evidence, required_vlm_images):
            return "budget_pruned"
        return "vlm_failures"
    if report_cited_images < 1:
        return "report_linkage_missing"
    return "none"


def _configured_image_cap_below_required(
    evidence: Mapping[str, Any] | None,
    required_vlm_images: int,
) -> bool:
    if not isinstance(evidence, Mapping) or required_vlm_images <= 0:
        return False
    budget = evidence.get("budget")
    if isinstance(budget, Mapping):
        value = budget.get("max_images")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value < required_vlm_images
    routing = evidence.get("routing")
    if not isinstance(routing, list):
        return False
    route_caps = [
        int(route.get("max_images"))
        for route in routing
        if isinstance(route, Mapping)
        and route.get("modality") == "visual_required"
        and isinstance(route.get("max_images"), int)
        and not isinstance(route.get("max_images"), bool)
        and int(route.get("max_images")) > 0
    ]
    return bool(route_caps) and sum(route_caps) < required_vlm_images


def _has_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _read_optional_artifact_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _read_optional_artifact_jsonl(path: Path) -> list[Mapping[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[Mapping[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            records.append(payload)
    return records


def build_visual_provider_status(
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
    invocations: int = 0,
    candidates_discovered: int = 0,
    artifacts_fetched: int = 0,
    vlm_images_analyzed: int = 0,
    estimated_cost_usd: float = 0.0,
    actual_cost_usd: float = 0.0,
    last_error: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build the canonical visual_provider_status.json payload."""

    return {
        "schema_version": VISUAL_PROVIDER_STATUS_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "status": status,
        "ok": ok,
        "terminal": terminal,
        "created_at": created_at,
        "metric_classification": metric_classification,
        "minimums": visual_release_minimums(
            required_vlm_images=0
            if status == "no_visual_tasks"
            else DEFAULT_REQUIRED_VISUAL_VLM_IMAGES
        ),
        "providers": [
            {
                "provider": provider,
                "provider_kind": provider_kind,
                "provider_mode": provider_mode,
                "configured": configured,
                "available": available,
                "blocked_reason": blocked_reason,
                "invocations": invocations,
                "candidates_discovered": candidates_discovered,
                "artifacts_fetched": artifacts_fetched,
                "vlm_images_analyzed": vlm_images_analyzed,
                "estimated_cost_usd": estimated_cost_usd,
                "actual_cost_usd": actual_cost_usd,
                "last_error": last_error if last_error is not None else blocked_reason,
            }
        ],
        "diagnostics": {"actionable_cause": actionable_cause},
        "artifacts": {
            "run_status": str(run_dir / "run_status.json"),
            "visual_provider_status": str(run_dir / VISUAL_PROVIDER_STATUS_FILENAME),
        },
    }


def validate_visual_artifacts(
    *,
    run_dir: str | Path | None = None,
    visual_search_plan_path: str | Path | None = None,
    visual_candidates_path: str | Path | None = None,
    image_fetch_status_path: str | Path | None = None,
    visual_observations_path: str | Path | None = None,
    visual_provider_status_path: str | Path | None = None,
    evidence_path: str | Path | None = None,
    research_tasks_path: str | Path | None = None,
    visual_tasks_path: str | Path | None = None,
    search_results_path: str | Path | None = None,
    verifier_votes_path: str | Path | None = None,
    report_status_path: str | Path | None = None,
) -> ValidationResult:
    """Validate Phase 3 automatic visual artifacts and local lineage when present."""

    base = Path(run_dir) if run_dir is not None else None
    visual_search_plan_path = _default_path(
        base,
        visual_search_plan_path,
        VISUAL_SEARCH_PLAN_FILENAME,
        required=base is not None,
    )
    visual_candidates_path = _default_path(
        base,
        visual_candidates_path,
        VISUAL_CANDIDATES_FILENAME,
        required=base is not None,
    )
    image_fetch_status_path = _default_path(
        base,
        image_fetch_status_path,
        IMAGE_FETCH_STATUS_FILENAME,
        required=base is not None,
    )
    visual_observations_path = _default_path(
        base, visual_observations_path, "visual_observations.jsonl"
    )
    visual_provider_status_path = _default_path(
        base,
        visual_provider_status_path,
        VISUAL_PROVIDER_STATUS_FILENAME,
        required=base is not None,
    )
    evidence_path = _default_path(base, evidence_path, "evidence.json")
    research_tasks_path = _default_path(base, research_tasks_path, "research_tasks.json")
    visual_tasks_path = _default_path(base, visual_tasks_path, "visual_tasks.json")
    search_results_path = _default_path(base, search_results_path, "search_results.jsonl")
    verifier_votes_path = _default_path(base, verifier_votes_path, "verifier_votes.jsonl")
    report_status_path = _default_path(base, report_status_path, "report_status.json")

    paths = [
        visual_search_plan_path,
        visual_candidates_path,
        image_fetch_status_path,
        visual_observations_path,
        visual_provider_status_path,
        evidence_path,
        research_tasks_path,
        visual_tasks_path,
        search_results_path,
        verifier_votes_path,
        report_status_path,
    ]
    collector = _Collector()
    if not any(paths):
        collector.add("$", "missing_input", "at least one visual artifact path is required")
        return _result(collector)

    context = _VisualContext()
    context.run_dir_validation = base is not None
    research_tasks = _load_optional_json(research_tasks_path, "$.research_tasks", collector)
    visual_tasks = _load_optional_json(visual_tasks_path, "$.visual_tasks", collector)
    evidence = _load_optional_json(evidence_path, "$.evidence", collector)
    search_results = _load_optional_jsonl(search_results_path, "$.search_results", collector)
    verifier_votes = _load_optional_jsonl(verifier_votes_path, "$.verifier_votes", collector)
    report_status = _load_optional_json(report_status_path, "$.report_status", collector)

    _collect_tasks(research_tasks, "$.research_tasks", context, collector)
    _collect_tasks(visual_tasks, "$.visual_tasks", context, collector)
    _collect_evidence(evidence, context, collector)
    _collect_search_results(search_results, context, collector)
    _collect_verifier_votes(verifier_votes, context, collector)

    plan = _load_optional_json(visual_search_plan_path, "$.visual_search_plan", collector)
    candidates = _load_optional_jsonl(
        visual_candidates_path, "$.visual_candidates", collector
    )
    fetches = _load_optional_jsonl(
        image_fetch_status_path, "$.image_fetch_status", collector
    )
    observations = _load_optional_jsonl(
        visual_observations_path, "$.visual_observations", collector
    )
    provider_status = _load_optional_json(
        visual_provider_status_path, "$.visual_provider_status", collector
    )

    if isinstance(plan, Mapping):
        _validate_visual_search_plan(plan, context, collector)
    elif plan is not None:
        collector.add("$.visual_search_plan", "invalid_type", "visual search plan must be an object")

    if candidates is not None:
        _validate_visual_candidates(candidates, context, collector)
    if fetches is not None:
        _validate_image_fetch_status(fetches, context, collector)
    if observations is not None:
        _validate_phase3_visual_observations(observations, context, collector)
    if isinstance(provider_status, Mapping):
        _validate_visual_provider_status(provider_status, collector)
    elif provider_status is not None:
        collector.add(
            "$.visual_provider_status",
            "invalid_type",
            "visual provider status must be an object",
        )
    if isinstance(report_status, Mapping):
        _validate_report_status(report_status, context, collector)
    if isinstance(provider_status, Mapping):
        _validate_completed_auto_visual_prerequisites(
            provider_status=provider_status,
            candidates=candidates or (),
            fetches=fetches or (),
            observations=observations or (),
            context=context,
            collector=collector,
        )
    _validate_candidate_fetch_lineage(context, collector)
    _validate_claim_observation_report_lineage(context, collector)
    return _result(collector)


def _validate_visual_search_plan(
    plan: Mapping[str, Any],
    context: _VisualContext,
    collector: _Collector,
) -> None:
    path = "$.visual_search_plan"
    _require_fields(plan, path, ("schema_version", "run_id", "created_at", "tasks"), collector)
    _check_string(plan, "schema_version", path, collector)
    _check_string(plan, "run_id", path, collector)
    _check_string(plan, "created_at", path, collector)
    tasks = _check_list(plan, "tasks", path, collector)
    for index, task in enumerate(tasks):
        task_path = f"{path}.tasks[{index}]"
        if not _require_object(task, task_path, collector):
            continue
        _require_fields(
            task,
            task_path,
            (
                "plan_id",
                "task_id",
                "angle_id",
                "route",
                "target_evidence_type",
                "query",
                "providers",
                "caps",
                "policy_constraints",
                "estimated_cost_usd",
                "state",
            ),
            collector,
        )
        plan_id = _check_string(task, "plan_id", task_path, collector)
        if plan_id:
            if plan_id in context.plan_by_id:
                collector.add(
                    f"{task_path}.plan_id",
                    "duplicate_id",
                    f"duplicate plan_id '{plan_id}'",
                )
            context.plan_by_id[plan_id] = task
        _validate_task_reference(task, task_path, context, collector)
        _check_enum(task, "route", SEARCH_ROUTES, task_path, collector)
        _check_enum(
            task, "target_evidence_type", TARGET_EVIDENCE_TYPES, task_path, collector
        )
        _check_string(task, "query", task_path, collector)
        _check_string_list(task, "providers", task_path, collector)
        caps = task.get("caps")
        if _require_object(caps, f"{task_path}.caps", collector):
            _require_fields(
                caps,
                f"{task_path}.caps",
                ("max_candidates", "max_fetches", "max_vlm_images", "max_cost_usd"),
                collector,
            )
            for field in ("max_candidates", "max_fetches", "max_vlm_images", "max_cost_usd"):
                _check_number(caps, field, f"{task_path}.caps", collector)
        if not isinstance(task.get("policy_constraints"), Mapping):
            collector.add(
                f"{task_path}.policy_constraints",
                "invalid_type",
                "policy_constraints must be an object",
            )
        _check_number(task, "estimated_cost_usd", task_path, collector)
        _check_enum(task, "state", PLAN_STATES, task_path, collector)


def _validate_visual_candidates(
    records: Sequence[Any],
    context: _VisualContext,
    collector: _Collector,
) -> None:
    seen: set[str] = set()
    for index, record in enumerate(records):
        path = f"$.visual_candidates[{index}]"
        if not _require_object(record, path, collector):
            continue
        _require_fields(
            record,
            path,
            (
                "candidate_id",
                "plan_id",
                "task_id",
                "angle_id",
                "provider",
                "provider_kind",
                "provider_mode",
                "provider_run_id",
                "provider_provenance",
                "origin",
                "page_url",
                "image_url",
                "rank",
                "score",
                "policy_decision",
                "policy_flags",
                "candidate_status",
                "rejection_reason",
                "estimated_cost_usd",
                "actual_cost_usd",
            ),
            collector,
        )
        candidate_id = _check_string(record, "candidate_id", path, collector)
        if candidate_id:
            if candidate_id in seen:
                collector.add(
                    f"{path}.candidate_id",
                    "duplicate_id",
                    f"duplicate candidate_id '{candidate_id}'",
                )
            seen.add(candidate_id)
            context.candidate_by_id[candidate_id] = record
        _validate_task_reference(record, path, context, collector)
        plan_id = _check_string(record, "plan_id", path, collector)
        if plan_id and context.plan_by_id and plan_id not in context.plan_by_id:
            collector.add(
                f"{path}.plan_id",
                "dangling_reference",
                f"candidate references unknown plan_id '{plan_id}'",
            )
        _validate_provider_fields(
            record,
            path,
            collector,
            allowed_kinds=CANDIDATE_PROVIDER_KINDS,
        )
        _check_enum(record, "origin", VISUAL_ORIGINS, path, collector)
        _check_nullable_string(record, "page_url", path, collector)
        _check_nullable_string(record, "image_url", path, collector)
        _check_number(record, "rank", path, collector)
        _check_number(record, "score", path, collector)
        _check_enum(record, "policy_decision", POLICY_DECISIONS, path, collector)
        _check_list(record, "policy_flags", path, collector)
        _check_enum(record, "candidate_status", CANDIDATE_STATUSES, path, collector)
        _check_nullable_string(record, "rejection_reason", path, collector)
        _check_number(record, "estimated_cost_usd", path, collector)
        _check_number(record, "actual_cost_usd", path, collector)
        _validate_source_search_result(record, path, context, collector)


def _validate_image_fetch_status(
    records: Sequence[Any],
    context: _VisualContext,
    collector: _Collector,
) -> None:
    seen: set[str] = set()
    for index, record in enumerate(records):
        path = f"$.image_fetch_status[{index}]"
        if not _require_object(record, path, collector):
            continue
        _require_fields(
            record,
            path,
            (
                "fetch_id",
                "candidate_id",
                "task_id",
                "angle_id",
                "provider",
                "provider_kind",
                "provider_mode",
                "provider_run_id",
                "provider_provenance",
                "fetch_status",
                "http_status",
                "mime_type",
                "byte_size",
                "width",
                "height",
                "hash",
                "phash",
                "local_artifact_path",
                "evidence_image_id",
                "policy_decision",
                "policy_flags",
                "failure_code",
                "estimated_cost_usd",
                "actual_cost_usd",
            ),
            collector,
        )
        fetch_id = _check_string(record, "fetch_id", path, collector)
        if fetch_id:
            if fetch_id in seen:
                collector.add(
                    f"{path}.fetch_id", "duplicate_id", f"duplicate fetch_id '{fetch_id}'"
                )
            seen.add(fetch_id)
            context.fetch_by_id[fetch_id] = record
        _validate_task_reference(record, path, context, collector)
        candidate_id = _check_string(record, "candidate_id", path, collector)
        if candidate_id:
            context.fetches_by_candidate_id.setdefault(candidate_id, []).append(record)
            candidate = context.candidate_by_id.get(candidate_id)
            if context.candidate_by_id and candidate is None:
                collector.add(
                    f"{path}.candidate_id",
                    "dangling_reference",
                    f"fetch references unknown candidate_id '{candidate_id}'",
                )
            elif candidate is not None:
                _validate_matching_lineage(
                    record,
                    candidate,
                    path,
                    "candidate",
                    ("task_id", "angle_id", "provider_mode", "provider_run_id"),
                    collector,
                )
        _validate_provider_fields(
            record,
            path,
            collector,
            allowed_kinds=CANDIDATE_PROVIDER_KINDS,
        )
        _check_enum(record, "fetch_status", FETCH_STATUSES, path, collector)
        _check_nullable_number(record, "http_status", path, collector)
        _check_nullable_string(record, "mime_type", path, collector)
        _check_nullable_number(record, "byte_size", path, collector)
        _check_nullable_number(record, "width", path, collector)
        _check_nullable_number(record, "height", path, collector)
        _check_nullable_string(record, "hash", path, collector)
        _check_nullable_string(record, "phash", path, collector)
        _check_nullable_string(record, "local_artifact_path", path, collector)
        evidence_image_id = _check_nullable_string(
            record, "evidence_image_id", path, collector
        )
        _check_enum(record, "policy_decision", POLICY_DECISIONS, path, collector)
        _check_list(record, "policy_flags", path, collector)
        _check_nullable_string(record, "failure_code", path, collector)
        _check_number(record, "estimated_cost_usd", path, collector)
        _check_number(record, "actual_cost_usd", path, collector)
        _validate_source_search_result(record, path, context, collector)
        if evidence_image_id:
            _validate_fetch_evidence_image(record, evidence_image_id, path, context, collector)


def _validate_phase3_visual_observations(
    records: Sequence[Any],
    context: _VisualContext,
    collector: _Collector,
) -> None:
    seen: set[str] = set()
    for index, record in enumerate(records):
        path = f"$.visual_observations[{index}]"
        if not _require_object(record, path, collector):
            continue
        _require_fields(
            record,
            path,
            (
                "observation_id",
                "evidence_image_id",
                "task_id",
                "angle_id",
                "candidate_id",
                "fetch_id",
                "provider",
                "provider_kind",
                "provider_mode",
                "provider_run_id",
                "provider_provenance",
                "model_or_tool",
                "observation_status",
                "observations",
                "inferences",
                "confidence",
                "policy_decision",
                "policy_flags",
                "caveats",
                "verifier_links",
                "report_links",
                "estimated_cost_usd",
                "actual_cost_usd",
                "created_at",
            ),
            collector,
        )
        observation_id = _check_string(record, "observation_id", path, collector)
        if observation_id:
            if observation_id in seen:
                collector.add(
                    f"{path}.observation_id",
                    "duplicate_id",
                    f"duplicate observation_id '{observation_id}'",
                )
            seen.add(observation_id)
        _validate_task_reference(record, path, context, collector)
        _validate_provider_fields(
            record,
            path,
            collector,
            allowed_kinds=OBSERVATION_PROVIDER_KINDS,
        )
        _check_string(record, "model_or_tool", path, collector)
        _check_enum(record, "observation_status", OBSERVATION_STATUSES, path, collector)
        _check_string_list(record, "observations", path, collector)
        _check_list(record, "inferences", path, collector)
        _check_number(record, "confidence", path, collector)
        _check_enum(record, "policy_decision", POLICY_DECISIONS, path, collector)
        _check_list(record, "policy_flags", path, collector)
        _check_list(record, "caveats", path, collector)
        _check_number(record, "estimated_cost_usd", path, collector)
        _check_number(record, "actual_cost_usd", path, collector)
        _check_string(record, "created_at", path, collector)
        image_id = _check_string(record, "evidence_image_id", path, collector)
        candidate_id = _check_string(record, "candidate_id", path, collector)
        fetch_id = _check_string(record, "fetch_id", path, collector)
        if candidate_id and context.candidate_by_id and candidate_id not in context.candidate_by_id:
            collector.add(
                f"{path}.candidate_id",
                "dangling_reference",
                f"observation references unknown candidate_id '{candidate_id}'",
            )
        fetch = context.fetch_by_id.get(fetch_id or "")
        if fetch_id and context.fetch_by_id and fetch is None:
            collector.add(
                f"{path}.fetch_id",
                "dangling_reference",
                f"observation references unknown fetch_id '{fetch_id}'",
            )
        elif fetch is not None:
            _validate_matching_lineage(
                record,
                fetch,
                path,
                "fetch",
                ("task_id", "angle_id", "candidate_id", "evidence_image_id"),
                collector,
            )
        if image_id and context.image_by_id and image_id not in context.image_by_id:
            collector.add(
                f"{path}.evidence_image_id",
                "dangling_reference",
                f"observation references unknown evidence image '{image_id}'",
            )
        _validate_observation_links(record, path, image_id, context, collector)


def _validate_visual_provider_status(
    status: Mapping[str, Any],
    collector: _Collector,
) -> None:
    path = "$.visual_provider_status"
    _require_fields(
        status,
        path,
        (
            "schema_version",
            "run_id",
            "status",
            "ok",
            "terminal",
            "metric_classification",
            "providers",
        ),
        collector,
    )
    _check_string(status, "schema_version", path, collector)
    _check_string(status, "run_id", path, collector)
    state = _check_string(status, "status", path, collector)
    _check_bool(status, "ok", path, collector)
    _check_bool(status, "terminal", path, collector)
    _check_string(status, "metric_classification", path, collector)
    if state in AUTOMATIC_VISUAL_TERMINAL_STATUSES:
        envelope = automatic_visual_status_envelope(state)
        for field in ("ok", "terminal", "metric_classification"):
            if status.get(field) != envelope[field]:
                collector.add(
                    f"{path}.{field}",
                    "status_envelope_mismatch",
                    f"{field} must be {envelope[field]!r} for status '{state}'",
                )
    minimums = status.get("minimums")
    if minimums is not None:
        _validate_visual_minimums(minimums, path, collector)
        if isinstance(minimums, Mapping):
            if state == "completed_auto_visual" and minimums.get("satisfied") is not True:
                collector.add(
                    f"{path}.minimums.satisfied",
                    "completed_auto_visual_minimum_mismatch",
                    "completed_auto_visual requires minimums.satisfied=true",
                )
            if state == "completed_auto_visual" and _int(
                minimums.get("vlm_images_analyzed")
            ) < _int(minimums.get("required_vlm_images")):
                collector.add(
                    f"{path}.minimums.vlm_images_analyzed",
                    "completed_auto_visual_minimum_mismatch",
                    "completed_auto_visual requires vlm_images_analyzed >= required_vlm_images",
                )
            if state == "completed_auto_visual" and _int(
                minimums.get("report_cited_images")
            ) < 1:
                collector.add(
                    f"{path}.minimums.report_cited_images",
                    "completed_auto_visual_minimum_mismatch",
                    "completed_auto_visual requires at least one report-cited image",
                )
            if state == "partial_auto_visual" and minimums.get("shortfall_reason") == "none":
                collector.add(
                    f"{path}.minimums.shortfall_reason",
                    "partial_auto_visual_shortfall_missing",
                    "partial_auto_visual requires a non-none shortfall_reason",
                )
    providers = _check_list(status, "providers", path, collector)
    for index, provider in enumerate(providers):
        provider_path = f"{path}.providers[{index}]"
        if not _require_object(provider, provider_path, collector):
            continue
        _require_fields(
            provider,
            provider_path,
            (
                "provider",
                "provider_kind",
                "provider_mode",
                "configured",
                "available",
                "blocked_reason",
                "invocations",
                "candidates_discovered",
                "artifacts_fetched",
                "vlm_images_analyzed",
                "estimated_cost_usd",
                "actual_cost_usd",
                "last_error",
            ),
            collector,
        )
        _validate_provider_fields(
            provider,
            provider_path,
            collector,
            require_run_id=False,
            allowed_kinds=STATUS_PROVIDER_KINDS,
        )
        _check_bool(provider, "configured", provider_path, collector)
        _check_bool(provider, "available", provider_path, collector)
        _check_nullable_string(provider, "blocked_reason", provider_path, collector)
        for field in (
            "invocations",
            "candidates_discovered",
            "artifacts_fetched",
            "vlm_images_analyzed",
            "estimated_cost_usd",
            "actual_cost_usd",
        ):
            _check_number(provider, field, provider_path, collector)
        _check_nullable_string(provider, "last_error", provider_path, collector)


def _validate_visual_minimums(
    minimums: Any,
    path: str,
    collector: _Collector,
) -> None:
    minimums_path = f"{path}.minimums"
    if not _require_object(minimums, minimums_path, collector):
        return
    _require_fields(
        minimums,
        minimums_path,
        (
            "required_vlm_images",
            "candidate_count",
            "selected_candidates",
            "fetched_artifacts",
            "vlm_images_analyzed",
            "report_cited_images",
            "satisfied",
            "shortfall_reason",
        ),
        collector,
    )
    for field in (
        "required_vlm_images",
        "candidate_count",
        "selected_candidates",
        "fetched_artifacts",
        "vlm_images_analyzed",
        "report_cited_images",
    ):
        _check_number(minimums, field, minimums_path, collector)
    _check_bool(minimums, "satisfied", minimums_path, collector)
    _check_enum(
        minimums,
        "shortfall_reason",
        VISUAL_MINIMUM_SHORTFALL_REASONS,
        minimums_path,
        collector,
    )
    if minimums.get("satisfied") is True and minimums.get("shortfall_reason") != "none":
        collector.add(
            f"{minimums_path}.shortfall_reason",
            "minimum_satisfaction_mismatch",
            "satisfied visual minimums must use shortfall_reason='none'",
        )
    if minimums.get("satisfied") is False and minimums.get("shortfall_reason") == "none":
        collector.add(
            f"{minimums_path}.shortfall_reason",
            "minimum_satisfaction_mismatch",
            "unsatisfied visual minimums must provide a non-none shortfall_reason",
        )


def _validate_report_status(
    report_status: Mapping[str, Any],
    context: _VisualContext,
    collector: _Collector,
) -> None:
    used_images = report_status.get("used_images")
    if used_images is None:
        return
    if not isinstance(used_images, list):
        collector.add("$.report_status.used_images", "invalid_type", "used_images must be a list")
        return
    for index, image_id in enumerate(used_images):
        if not isinstance(image_id, str) or not image_id:
            collector.add(
                f"$.report_status.used_images[{index}]",
                "invalid_type",
                "used image IDs must be non-empty strings",
            )
            continue
        if context.image_by_id and image_id not in context.image_by_id:
            collector.add(
                f"$.report_status.used_images[{index}]",
                "dangling_reference",
                f"report status references unknown image '{image_id}'",
            )
        else:
            context.report_used_image_ids.add(image_id)


def _validate_completed_auto_visual_prerequisites(
    *,
    provider_status: Mapping[str, Any],
    candidates: Sequence[Any],
    fetches: Sequence[Any],
    observations: Sequence[Any],
    context: _VisualContext,
    collector: _Collector,
) -> None:
    if provider_status.get("status") != "completed_auto_visual":
        return
    providers = provider_status.get("providers", [])
    provider_records = [item for item in providers if isinstance(item, Mapping)]
    candidate_records = [item for item in candidates if isinstance(item, Mapping)]
    fetch_records = [item for item in fetches if isinstance(item, Mapping)]
    observation_records = [item for item in observations if isinstance(item, Mapping)]
    real_acquisition_ran = any(_is_real_acquisition_record(item) for item in provider_records)
    real_fetched_artifact = any(
        _is_real_acquisition_record(item)
        and item.get("fetch_status") == "fetched"
        and isinstance(item.get("evidence_image_id"), str)
        and item.get("evidence_image_id")
        for item in fetch_records
    )
    real_vlm_observation = any(
        item.get("provider_mode") == "real"
        and item.get("provider_kind") == "vlm"
        and item.get("observation_status") == "analyzed"
        for item in observation_records
    )
    report_cited_supported_claim = _has_report_cited_supported_visual_claim(context)
    missing: list[str] = []
    if not real_acquisition_ran:
        missing.append("real_non_fixture_visual_provider")
    if (context.run_dir_validation or fetch_records) and not real_fetched_artifact:
        missing.append("real_fetched_visual_artifact")
    if (context.run_dir_validation or observation_records) and not real_vlm_observation:
        missing.append("real_vlm_observation")
    if (
        context.run_dir_validation
        or (context.claim_by_id and context.report_used_image_ids)
    ) and not report_cited_supported_claim:
        missing.append("report_cited_supported_visual_claim")
    if missing:
        collector.add(
            "$.visual_provider_status.status",
            "completed_auto_visual_prerequisites",
            "completed_auto_visual requires: " + ", ".join(missing),
        )
    if context.run_dir_validation:
        _validate_completed_auto_visual_counter_reconciliation(
            provider_records=provider_records,
            candidate_records=candidate_records,
            fetch_records=fetch_records,
            observation_records=observation_records,
            collector=collector,
        )


def _validate_completed_auto_visual_counter_reconciliation(
    *,
    provider_records: Sequence[Mapping[str, Any]],
    candidate_records: Sequence[Mapping[str, Any]],
    fetch_records: Sequence[Mapping[str, Any]],
    observation_records: Sequence[Mapping[str, Any]],
    collector: _Collector,
) -> None:
    real_acquisition_providers = [
        record for record in provider_records if _is_real_acquisition_record(record)
    ]
    real_observation_providers = [
        record for record in provider_records if _is_real_observation_record(record)
    ]
    real_candidates = [
        record for record in candidate_records if _is_real_acquisition_record(record)
    ]
    real_fetched_artifacts = [
        record
        for record in fetch_records
        if _is_real_acquisition_record(record)
        and record.get("fetch_status") == "fetched"
        and isinstance(record.get("evidence_image_id"), str)
        and record.get("evidence_image_id")
    ]
    real_vlm_observations = [
        record
        for record in observation_records
        if record.get("provider_mode") == "real"
        and record.get("provider_kind") == "vlm"
        and record.get("observation_status") == "analyzed"
    ]
    missing: list[str] = []
    if (
        real_candidates or real_fetched_artifacts
    ) and _sum_provider_counter(real_acquisition_providers, "invocations") <= 0:
        missing.append("real_acquisition_invocations")
    if real_candidates and _sum_provider_counter(
        real_acquisition_providers, "candidates_discovered"
    ) <= 0:
        missing.append("real_candidates_discovered")
    if real_fetched_artifacts and _sum_provider_counter(
        real_acquisition_providers, "artifacts_fetched"
    ) <= 0:
        missing.append("real_artifacts_fetched")
    if real_vlm_observations and _sum_provider_counter(
        real_observation_providers, "vlm_images_analyzed"
    ) <= 0:
        missing.append("real_vlm_images_analyzed")
    real_vlm_providers = [
        record
        for record in provider_records
        if record.get("provider_mode") == "real" and record.get("provider_kind") == "vlm"
    ]
    if real_vlm_observations and real_vlm_providers and _sum_provider_counter(
        real_vlm_providers, "invocations"
    ) <= 0:
        missing.append("real_vlm_invocations")
    if missing:
        collector.add(
            "$.visual_provider_status.providers",
            "completed_auto_visual_provider_counters",
            "completed_auto_visual provider counters must be positive for: "
            + ", ".join(missing),
        )


def _sum_provider_counter(
    providers: Sequence[Mapping[str, Any]],
    field: str,
) -> int:
    return sum(_int(provider.get(field)) for provider in providers)


def _has_report_cited_supported_visual_claim(context: _VisualContext) -> bool:
    if not context.claim_by_id or not context.report_used_image_ids:
        return False
    for claim in context.claim_by_id.values():
        if claim.get("verification_status") != "supported":
            continue
        if claim.get("claim_type") not in {"visual", "mixed"}:
            continue
        supporting_images = claim.get("supporting_images", [])
        if not isinstance(supporting_images, list):
            continue
        if any(
            isinstance(image_id, str) and image_id in context.report_used_image_ids
            for image_id in supporting_images
        ):
            return True
    return False


def _collect_tasks(
    payload: Any,
    path: str,
    context: _VisualContext,
    collector: _Collector,
) -> None:
    if payload is None:
        return
    if not isinstance(payload, Mapping):
        collector.add(path, "invalid_type", "task artifact must be an object")
        return
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return
    for index, task in enumerate(tasks):
        if not isinstance(task, Mapping):
            continue
        task_id = task.get("id")
        if isinstance(task_id, str) and task_id:
            context.task_by_id[task_id] = task


def _collect_evidence(
    evidence: Any,
    context: _VisualContext,
    collector: _Collector,
) -> None:
    if evidence is None:
        return
    if not isinstance(evidence, Mapping):
        collector.add("$.evidence", "invalid_type", "evidence must be an object")
        return
    for task in evidence.get("search_tasks", []) if isinstance(evidence.get("search_tasks"), list) else []:
        if isinstance(task, Mapping) and isinstance(task.get("id"), str):
            context.task_by_id.setdefault(task["id"], task)
    for image in evidence.get("images", []) if isinstance(evidence.get("images"), list) else []:
        if isinstance(image, Mapping) and isinstance(image.get("id"), str):
            context.image_by_id[image["id"]] = image
    for claim in evidence.get("claims", []) if isinstance(evidence.get("claims"), list) else []:
        if isinstance(claim, Mapping) and isinstance(claim.get("id"), str):
            context.claim_by_id[claim["id"]] = claim
            for vote in claim.get("votes", []) if isinstance(claim.get("votes"), list) else []:
                if isinstance(vote, Mapping) and isinstance(vote.get("id"), str):
                    context.verifier_vote_ids.add(vote["id"])


def _collect_search_results(
    records: Sequence[Any] | None,
    context: _VisualContext,
    collector: _Collector,
) -> None:
    if records is None:
        return
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            collector.add(f"$.search_results[{index}]", "invalid_type", "expected an object")
            continue
        result_id = record.get("id")
        if isinstance(result_id, str) and result_id:
            context.search_result_ids.add(result_id)


def _collect_verifier_votes(
    records: Sequence[Any] | None,
    context: _VisualContext,
    collector: _Collector,
) -> None:
    if records is None:
        return
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            collector.add(f"$.verifier_votes[{index}]", "invalid_type", "expected an object")
            continue
        vote_id = record.get("id")
        if isinstance(vote_id, str) and vote_id:
            context.verifier_vote_ids.add(vote_id)


def _validate_task_reference(
    record: Mapping[str, Any],
    path: str,
    context: _VisualContext,
    collector: _Collector,
) -> None:
    task_id = _check_string(record, "task_id", path, collector)
    angle_id = _check_string(record, "angle_id", path, collector)
    if not task_id or not context.task_by_id:
        return
    task = context.task_by_id.get(task_id)
    if task is None:
        collector.add(
            f"{path}.task_id",
            "dangling_reference",
            f"record references unknown task_id '{task_id}'",
        )
        return
    task_angle_id = task.get("angle_id")
    if angle_id and isinstance(task_angle_id, str) and angle_id != task_angle_id:
        collector.add(
            f"{path}.angle_id",
            "angle_mismatch",
            f"angle_id '{angle_id}' does not match task '{task_id}' angle_id '{task_angle_id}'",
        )
    task_route = task.get("route") or task.get("modality")
    record_route = record.get("route")
    if isinstance(record_route, str) and isinstance(task_route, str) and record_route != task_route:
        collector.add(
            f"{path}.route",
            "route_mismatch",
            f"route '{record_route}' does not match task '{task_id}' route '{task_route}'",
        )


def _validate_provider_fields(
    record: Mapping[str, Any],
    path: str,
    collector: _Collector,
    *,
    require_run_id: bool = True,
    allowed_kinds: tuple[str, ...] = STATUS_PROVIDER_KINDS,
) -> None:
    _check_string(record, "provider", path, collector)
    _check_enum(record, "provider_kind", allowed_kinds, path, collector)
    _check_enum(record, "provider_mode", PROVIDER_MODES, path, collector)
    if require_run_id:
        _check_string(record, "provider_run_id", path, collector)
    provenance = record.get("provider_provenance")
    if "provider_provenance" in record and not isinstance(provenance, Mapping):
        collector.add(
            f"{path}.provider_provenance",
            "invalid_type",
            "provider_provenance must be an object",
        )


def _validate_source_search_result(
    record: Mapping[str, Any],
    path: str,
    context: _VisualContext,
    collector: _Collector,
) -> None:
    result_id = record.get("source_search_result_id")
    if result_id is None:
        return
    if not isinstance(result_id, str) or not result_id:
        collector.add(
            f"{path}.source_search_result_id",
            "invalid_type",
            "source_search_result_id must be a non-empty string when present",
        )
    elif context.search_result_ids and result_id not in context.search_result_ids:
        collector.add(
            f"{path}.source_search_result_id",
            "dangling_reference",
            f"record references unknown search result '{result_id}'",
        )


def _validate_fetch_evidence_image(
    fetch: Mapping[str, Any],
    image_id: str,
    path: str,
    context: _VisualContext,
    collector: _Collector,
) -> None:
    if context.image_by_id and image_id not in context.image_by_id:
        collector.add(
            f"{path}.evidence_image_id",
            "dangling_reference",
            f"fetch references unknown evidence image '{image_id}'",
        )
        return
    image = context.image_by_id.get(image_id)
    if image is None:
        return
    required_image_fields = (
        "task_id",
        "angle_id",
        "candidate_id",
        "fetch_id",
        "local_artifact_path",
        "hash",
        "provider",
        "provider_kind",
        "provider_mode",
        "provider_provenance",
        "policy_decision",
        "estimated_cost_usd",
        "actual_cost_usd",
    )
    for field in required_image_fields:
        if field not in image:
            collector.add(
                f"$.evidence.images.{image_id}.{field}",
                "missing_required_field",
                f"evidence image must preserve visual artifact field '{field}'",
            )
    _validate_matching_lineage(
        image,
        fetch,
        f"$.evidence.images.{image_id}",
        "fetch",
        (
            "task_id",
            "angle_id",
            "candidate_id",
            "fetch_id",
            "local_artifact_path",
            "hash",
            "provider_mode",
            "policy_decision",
        ),
        collector,
    )


def _validate_candidate_fetch_lineage(
    context: _VisualContext,
    collector: _Collector,
) -> None:
    for candidate_id, candidate in context.candidate_by_id.items():
        status = candidate.get("candidate_status")
        if status not in {"fetched", "analyzed"}:
            continue
        fetches = [
            fetch
            for fetch in context.fetches_by_candidate_id.get(candidate_id, [])
            if fetch.get("fetch_status") != "deduped"
        ]
        deduped = [
            fetch
            for fetch in context.fetches_by_candidate_id.get(candidate_id, [])
            if fetch.get("fetch_status") == "deduped"
        ]
        if len(fetches) == 1:
            continue
        if not fetches and deduped:
            continue
        collector.add(
            f"$.visual_candidates.{candidate_id}",
            "candidate_fetch_lineage",
            "fetched/analyzed candidates must have exactly one fetch record or a documented dedupe",
        )


def _validate_observation_links(
    observation: Mapping[str, Any],
    path: str,
    image_id: str | None,
    context: _VisualContext,
    collector: _Collector,
) -> None:
    verifier_links = _check_list(observation, "verifier_links", path, collector)
    report_links = _check_list(observation, "report_links", path, collector)
    for link_type, links in (("verifier_links", verifier_links), ("report_links", report_links)):
        for index, link in enumerate(links):
            link_path = f"{path}.{link_type}[{index}]"
            if not _require_object(link, link_path, collector):
                continue
            claim_id = _check_string(link, "claim_id", link_path, collector)
            if claim_id and context.claim_by_id and claim_id not in context.claim_by_id:
                collector.add(
                    f"{link_path}.claim_id",
                    "dangling_reference",
                    f"{link_type} references unknown claim '{claim_id}'",
                )
                continue
            if link_type == "verifier_links":
                vote_id = _check_nullable_string(link, "verifier_vote_id", link_path, collector)
                if vote_id and context.verifier_vote_ids and vote_id not in context.verifier_vote_ids:
                    collector.add(
                        f"{link_path}.verifier_vote_id",
                        "dangling_reference",
                        f"verifier link references unknown verifier vote '{vote_id}'",
                    )
            else:
                _check_nullable_string(link, "report_section_id", link_path, collector)
                _check_nullable_string(link, "citation_id", link_path, collector)
            if claim_id and image_id:
                context.observation_links_by_image_claim.add((link_type, image_id, claim_id))
                claim = context.claim_by_id.get(claim_id)
                if claim is not None:
                    supporting_images = claim.get("supporting_images", [])
                    if isinstance(supporting_images, list) and image_id not in supporting_images:
                        collector.add(
                            f"{link_path}.claim_id",
                            "image_claim_lineage",
                            f"claim '{claim_id}' does not list supporting image '{image_id}'",
                        )


def _validate_claim_observation_report_lineage(
    context: _VisualContext,
    collector: _Collector,
) -> None:
    verifier_pairs = {
        (image_id, claim_id)
        for link_type, image_id, claim_id in context.observation_links_by_image_claim
        if link_type == "verifier_links"
    }
    report_pairs = {
        (image_id, claim_id)
        for link_type, image_id, claim_id in context.observation_links_by_image_claim
        if link_type == "report_links"
    }
    for claim_id, claim in context.claim_by_id.items():
        if claim.get("verification_status") != "supported":
            continue
        if claim.get("claim_type") not in {"visual", "mixed"}:
            continue
        supporting_images = claim.get("supporting_images", [])
        if not isinstance(supporting_images, list):
            continue
        for image_id in supporting_images:
            if not isinstance(image_id, str):
                continue
            if (
                not context.report_used_image_ids
                or image_id not in context.report_used_image_ids
            ):
                continue
            if (image_id, claim_id) not in verifier_pairs:
                collector.add(
                    f"$.evidence.claims.{claim_id}.visual_supports",
                    "missing_verifier_link",
                    f"supported visual claim '{claim_id}' lacks verifier link for image '{image_id}'",
                )
            if (image_id, claim_id) not in report_pairs:
                collector.add(
                    f"$.evidence.claims.{claim_id}.visual_supports",
                    "missing_report_link",
                    f"supported visual claim '{claim_id}' lacks report link for image '{image_id}'",
                )


def _validate_matching_lineage(
    record: Mapping[str, Any],
    expected: Mapping[str, Any],
    path: str,
    expected_name: str,
    fields: Iterable[str],
    collector: _Collector,
) -> None:
    for field in fields:
        if field in record and field in expected and record.get(field) != expected.get(field):
            collector.add(
                f"{path}.{field}",
                "lineage_mismatch",
                f"{field} does not match {expected_name} value {expected.get(field)!r}",
            )


def _default_path(
    base: Path | None,
    value: str | Path | None,
    filename: str,
    *,
    required: bool = False,
) -> Path | None:
    if value is not None:
        return Path(value)
    if base is None:
        return None
    candidate = base / filename
    if required:
        return candidate
    return candidate if candidate.exists() else None


def _load_optional_json(
    path: str | Path | None,
    json_path: str,
    collector: _Collector,
) -> Any:
    if path is None:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        collector.add(json_path, "missing_file", f"file not found: {path}")
    except json.JSONDecodeError as exc:
        collector.add(json_path, "invalid_json", f"invalid JSON: {exc}")
    return None


def _load_optional_jsonl(
    path: str | Path | None,
    json_path: str,
    collector: _Collector,
) -> list[Any] | None:
    if path is None:
        return None
    records: list[Any] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        collector.add(json_path, "missing_file", f"file not found: {path}")
        return None
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        record_path = f"{json_path}[{index}]"
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            collector.add(record_path, "invalid_jsonl_record", f"invalid JSONL record: {exc}")
    return records


def _require_object(value: Any, path: str, collector: _Collector) -> bool:
    if isinstance(value, Mapping):
        return True
    collector.add(path, "invalid_type", "expected an object")
    return False


def _require_fields(
    record: Mapping[str, Any],
    path: str,
    fields: Iterable[str],
    collector: _Collector,
) -> None:
    for field in fields:
        if field not in record:
            collector.add(
                f"{path}.{field}",
                "missing_required_field",
                f"missing required field '{field}'",
            )


def _check_string(
    record: Mapping[str, Any],
    field: str,
    path: str,
    collector: _Collector,
) -> str | None:
    if field not in record:
        return None
    value = record[field]
    if isinstance(value, str) and value:
        return value
    collector.add(f"{path}.{field}", "invalid_type", f"{field} must be a non-empty string")
    return None


def _check_nullable_string(
    record: Mapping[str, Any],
    field: str,
    path: str,
    collector: _Collector,
) -> str | None:
    if field not in record:
        return None
    value = record[field]
    if value is None:
        return None
    if isinstance(value, str):
        return value
    collector.add(f"{path}.{field}", "invalid_type", f"{field} must be a string or null")
    return None


def _check_string_list(
    record: Mapping[str, Any],
    field: str,
    path: str,
    collector: _Collector,
) -> list[str]:
    values = _check_list(record, field, path, collector)
    strings: list[str] = []
    for index, value in enumerate(values):
        if isinstance(value, str) and value:
            strings.append(value)
        else:
            collector.add(
                f"{path}.{field}[{index}]",
                "invalid_type",
                f"{field} entries must be non-empty strings",
            )
    return strings


def _check_list(
    record: Mapping[str, Any],
    field: str,
    path: str,
    collector: _Collector,
) -> list[Any]:
    if field not in record:
        return []
    value = record[field]
    if isinstance(value, list):
        return value
    collector.add(f"{path}.{field}", "invalid_type", f"{field} must be a list")
    return []


def _check_number(
    record: Mapping[str, Any],
    field: str,
    path: str,
    collector: _Collector,
) -> float | int | None:
    if field not in record:
        return None
    value = record[field]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    collector.add(f"{path}.{field}", "invalid_type", f"{field} must be a number")
    return None


def _check_nullable_number(
    record: Mapping[str, Any],
    field: str,
    path: str,
    collector: _Collector,
) -> float | int | None:
    if field not in record:
        return None
    value = record[field]
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    collector.add(f"{path}.{field}", "invalid_type", f"{field} must be a number or null")
    return None


def _check_bool(
    record: Mapping[str, Any],
    field: str,
    path: str,
    collector: _Collector,
) -> bool | None:
    if field not in record:
        return None
    value = record[field]
    if isinstance(value, bool):
        return value
    collector.add(f"{path}.{field}", "invalid_type", f"{field} must be a boolean")
    return None


def _check_enum(
    record: Mapping[str, Any],
    field: str,
    allowed: tuple[str, ...],
    path: str,
    collector: _Collector,
) -> str | None:
    value = _check_string(record, field, path, collector)
    if value is None:
        return None
    if value in allowed:
        return value
    collector.add(
        f"{path}.{field}",
        "invalid_enum",
        f"{field} must be one of: {', '.join(allowed)}",
    )
    return None


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _result(collector: _Collector) -> ValidationResult:
    return ValidationResult(valid=not collector.errors, errors=tuple(collector.errors))
