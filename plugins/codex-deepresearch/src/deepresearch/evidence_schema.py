"""Schema-level validation for Codex DeepResearch evidence artifacts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


EVIDENCE_SCHEMA_VERSION = "0.1.0"

MODES = ("codex-plugin", "automated-cli", "manual-sources")
SEARCH_PROVIDERS = ("codex-native", "openai", "brave", "tavily", "serpapi", "manual")
VLM_PROVIDERS = (
    "codex-interactive",
    "openai-responses-vision",
    "manual-visual-review",
)
SOURCE_TYPES = ("web", "pdf", "image", "screenshot")
SOURCE_QUALITIES = ("primary", "secondary", "blog", "forum", "unknown")
SOURCE_RETRIEVAL_STATUSES = ("fetched", "failed", "partial", "manual")
SOURCE_LICENSE_POLICIES = ("unknown", "allowed", "restricted", "manual_review")
SOURCE_ROBOTS_POLICIES = ("unknown", "allowed", "disallowed", "manual_review")
CLAIM_TYPES = ("text", "visual", "mixed")
VERIFICATION_STATUSES = (
    "supported",
    "refuted",
    "disputed",
    "insufficient_evidence",
    "needs_visual_evidence",
    "budget_pruned",
    "policy_blocked",
    "unverified",
)
REVIEW_STATUSES = (
    "not_reviewed",
    "auto_reviewed",
    "human_accepted",
    "human_rejected",
    "needs_more_evidence",
)
PROMOTION_STATUSES = (
    "not_eligible",
    "eligible",
    "promoted_memory",
    "promoted_playbook",
    "promoted_skill",
    "promoted_prd",
    "promotion_rejected",
)
CONFIDENCE_LEVELS = ("high", "medium", "low")
VISUAL_ORIGINS = (
    "page_image",
    "image_search",
    "screenshot",
    "pdf_page",
    "pdf_figure",
    "user_upload",
    "manual",
)
ANALYSIS_STATUSES = (
    "analyzed",
    "failed",
    "skipped",
    "needs_manual_review",
    "policy_blocked",
)
SEARCH_ROUTES = ("text_only", "visual_required", "visual_optional")
SEARCH_RESULT_TYPES = ("web", "pdf", "image", "news", "academic", "manual")
FRESHNESS_REQUIREMENTS = ("latest", "recent", "historical", "any")
POLICY_DECISIONS = ("allowed", "blocked", "manual_review")
VERIFIER_TYPES = ("text", "visual", "policy", "freshness")
VERIFIER_METHODS = ("codex-subagent", "runner-agent", "model-call", "manual-review")
VERIFIER_VOTES = ("support", "refute", "uncertain", "blocked")
VISUAL_RELATION_TYPES = (
    "ocr_support",
    "visual_match",
    "chart_support",
    "screenshot_support",
    "context_support",
)


@dataclass(frozen=True)
class ValidationError:
    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: tuple[ValidationError, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [error.to_dict() for error in self.errors],
        }


class _Collector:
    def __init__(self) -> None:
        self.errors: list[ValidationError] = []

    def add(self, path: str, code: str, message: str) -> None:
        self.errors.append(ValidationError(path=path, code=code, message=message))


def validate_artifacts(
    *,
    evidence_path: str | Path | None = None,
    search_results_path: str | Path | None = None,
    visual_observations_path: str | Path | None = None,
    verifier_votes_path: str | Path | None = None,
) -> ValidationResult:
    """Validate one or more evidence-related artifacts."""

    collector = _Collector()
    if not any(
        [evidence_path, search_results_path, visual_observations_path, verifier_votes_path]
    ):
        collector.add("$", "missing_input", "at least one artifact path is required")
        return _result(collector)

    evidence = _load_json(Path(evidence_path), "$.evidence", collector) if evidence_path else None
    search_results = (
        _load_jsonl(Path(search_results_path), "$.search_results", collector)
        if search_results_path
        else None
    )
    visual_observations = (
        _load_jsonl(Path(visual_observations_path), "$.visual_observations", collector)
        if visual_observations_path
        else None
    )
    verifier_votes = (
        _load_jsonl(Path(verifier_votes_path), "$.verifier_votes", collector)
        if verifier_votes_path
        else None
    )

    context = _ValidationContext()
    if verifier_votes is not None:
        context.top_level_verifier_votes_provided = True
        _collect_top_level_verifier_vote_ids(verifier_votes, context)

    if isinstance(evidence, Mapping):
        _validate_evidence(evidence, collector, context)
    elif evidence is not None:
        collector.add("$.evidence", "invalid_type", "evidence artifact must be a JSON object")

    if search_results is not None:
        _validate_search_results(search_results, collector, context)
    if visual_observations is not None:
        _validate_visual_observations(visual_observations, collector, context)
    if verifier_votes is not None:
        _validate_verifier_votes(
            verifier_votes,
            collector,
            context,
            "$.verifier_votes",
            allow_string_refs=False,
        )

    return _result(collector)


class _ValidationContext:
    def __init__(self) -> None:
        self.evidence_provided = False
        self.mode: str | None = None
        self.source_ids: set[str] = set()
        self.image_ids: set[str] = set()
        self.images_by_id: dict[str, Mapping[str, Any]] = {}
        self.claims_provided = False
        self.claim_ids: set[str] = set()
        self.routing_provided = False
        self.angle_routes: dict[str, str] = {}
        self.search_tasks_provided = False
        self.search_task_ids: set[str] = set()
        self.top_level_verifier_votes_provided = False
        self.verifier_vote_ids: set[str] = set()


def _result(collector: _Collector) -> ValidationResult:
    return ValidationResult(valid=not collector.errors, errors=tuple(collector.errors))


def _load_json(path: Path, json_path: str, collector: _Collector) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        collector.add(json_path, "missing_file", f"file not found: {path}")
    except json.JSONDecodeError as exc:
        collector.add(json_path, "invalid_json", f"invalid JSON: {exc}")
    return None


def _load_jsonl(path: Path, json_path: str, collector: _Collector) -> list[Any] | None:
    records: list[Any] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
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


def _validate_evidence(
    evidence: Mapping[str, Any],
    collector: _Collector,
    context: _ValidationContext,
) -> None:
    context.evidence_provided = True
    _require_fields(
        evidence,
        "$.evidence",
        ("schema_version", "run_id", "created_at", "mode", "search_provider", "vlm_provider"),
        collector,
    )
    _check_enum(evidence, "schema_version", (EVIDENCE_SCHEMA_VERSION,), "$.evidence", collector)
    context.mode = _check_enum(evidence, "mode", MODES, "$.evidence", collector)
    _check_enum(evidence, "search_provider", SEARCH_PROVIDERS, "$.evidence", collector)
    _check_enum(evidence, "vlm_provider", VLM_PROVIDERS, "$.evidence", collector)

    sources = _optional_list(evidence, "sources", "$.evidence", collector)
    if sources is not None:
        for index, source in enumerate(sources):
            source_path = f"$.evidence.sources[{index}]"
            if not _require_object(source, source_path, collector):
                continue
            source_id = _validate_source(source, source_path, collector)
            if source_id is not None:
                _add_unique_id(source_id, context.source_ids, source_path, collector)

    routing = _optional_list(evidence, "routing", "$.evidence", collector)
    if routing is not None:
        context.routing_provided = True
        for index, route in enumerate(routing):
            route_path = f"$.evidence.routing[{index}]"
            if not _require_object(route, route_path, collector):
                continue
            angle_id = _optional_string(route, "id", route_path, collector)
            modality = _check_enum(route, "modality", SEARCH_ROUTES, route_path, collector)
            if angle_id is not None and modality is not None:
                context.angle_routes[angle_id] = modality

    search_tasks = _optional_list(evidence, "search_tasks", "$.evidence", collector)
    if search_tasks is not None:
        context.search_tasks_provided = True
        for index, search_task in enumerate(search_tasks):
            search_task_path = f"$.evidence.search_tasks[{index}]"
            if not _require_object(search_task, search_task_path, collector):
                continue
            _require_fields(search_task, search_task_path, ("id",), collector)
            search_task_id = _optional_string(search_task, "id", search_task_path, collector)
            if search_task_id is not None:
                _add_unique_id(
                    search_task_id,
                    context.search_task_ids,
                    search_task_path,
                    collector,
                )

    images = _optional_list(evidence, "images", "$.evidence", collector)
    if images is not None:
        for index, image in enumerate(images):
            image_path = f"$.evidence.images[{index}]"
            if not _require_object(image, image_path, collector):
                continue
            image_id = _validate_visual_evidence(
                image,
                image_path,
                collector,
                context,
                require_source_reference=True,
            )
            if image_id is not None:
                _add_unique_id(image_id, context.image_ids, image_path, collector)
                context.images_by_id[image_id] = image

    claims = _optional_list(evidence, "claims", "$.evidence", collector)
    if claims is not None:
        context.claims_provided = True
        for index, claim in enumerate(claims):
            claim_path = f"$.evidence.claims[{index}]"
            if not _require_object(claim, claim_path, collector):
                continue
            claim_id = _optional_string(claim, "id", claim_path, collector)
            if claim_id is not None:
                _add_unique_id(claim_id, context.claim_ids, claim_path, collector)
        for index, claim in enumerate(claims):
            if isinstance(claim, Mapping):
                _validate_claim(claim, f"$.evidence.claims[{index}]", collector, context)


def _validate_source(
    source: Mapping[str, Any],
    path: str,
    collector: _Collector,
) -> str | None:
    _require_fields(
        source,
        path,
        (
            "id",
            "type",
            "url",
            "title",
            "accessed_at",
            "quality",
            "retrieval_status",
            "local_artifact_path",
            "license_policy",
            "robots_policy",
        ),
        collector,
    )
    source_id = _optional_string(source, "id", path, collector)
    _check_enum(source, "type", SOURCE_TYPES, path, collector)
    _check_string(source, "url", path, collector)
    _check_string(source, "title", path, collector)
    _check_string(source, "accessed_at", path, collector)
    _check_enum(source, "quality", SOURCE_QUALITIES, path, collector)
    _check_enum(source, "retrieval_status", SOURCE_RETRIEVAL_STATUSES, path, collector)
    _check_string(source, "local_artifact_path", path, collector)
    _check_enum(source, "license_policy", SOURCE_LICENSE_POLICIES, path, collector)
    _check_enum(source, "robots_policy", SOURCE_ROBOTS_POLICIES, path, collector)
    return source_id


def _validate_visual_evidence(
    image: Mapping[str, Any],
    path: str,
    collector: _Collector,
    context: _ValidationContext,
    *,
    require_source_reference: bool,
) -> str | None:
    _require_fields(
        image,
        path,
        (
            "id",
            "source_id",
            "origin",
            "local_artifact_path",
            "mime_type",
            "width",
            "height",
            "observations",
            "inferences",
            "visual_tasks",
            "analysis_provider",
            "analysis_status",
            "policy_flags",
            "caveats",
        ),
        collector,
    )
    image_id = _optional_string(image, "id", path, collector)
    source_id = _optional_string(image, "source_id", path, collector)
    if (
        source_id is not None
        and (require_source_reference or context.source_ids)
        and source_id not in context.source_ids
    ):
        collector.add(
            f"{path}.source_id",
            "dangling_reference",
            f"image source_id '{source_id}' does not reference an existing source",
        )
    _check_enum(image, "origin", VISUAL_ORIGINS, path, collector)
    page_url = _check_optional_string(image, "page_url", path, collector)
    image_url = _check_optional_string(image, "image_url", path, collector)
    if page_url is None and image_url is None:
        collector.add(
            path,
            "missing_required_field",
            "VisualEvidence requires at least one valid page_url or image_url",
        )
    _check_string(image, "local_artifact_path", path, collector)
    _check_string(image, "mime_type", path, collector)
    _check_number(image, "width", path, collector)
    _check_number(image, "height", path, collector)
    _check_list(image, "observations", path, collector)
    _check_list(image, "inferences", path, collector)
    _check_list(image, "visual_tasks", path, collector)
    _check_enum(image, "analysis_provider", VLM_PROVIDERS, path, collector)
    _check_enum(image, "analysis_status", ANALYSIS_STATUSES, path, collector)
    _check_list(image, "policy_flags", path, collector)
    _check_list(image, "caveats", path, collector)
    return image_id


def _validate_claim(
    claim: Mapping[str, Any],
    path: str,
    collector: _Collector,
    context: _ValidationContext,
) -> None:
    _require_fields(
        claim,
        path,
        (
            "id",
            "text",
            "claim_type",
            "supporting_sources",
            "supporting_images",
            "quote_spans",
            "verification_status",
            "review_status",
            "promotion_status",
            "confidence",
            "caveats",
        ),
        collector,
    )
    _optional_string(claim, "id", path, collector)
    _check_string(claim, "text", path, collector)
    claim_type = _check_enum(claim, "claim_type", CLAIM_TYPES, path, collector)
    _check_enum(claim, "verification_status", VERIFICATION_STATUSES, path, collector)
    _check_enum(claim, "review_status", REVIEW_STATUSES, path, collector)
    _check_enum(claim, "promotion_status", PROMOTION_STATUSES, path, collector)
    confidence = _check_enum(claim, "confidence", CONFIDENCE_LEVELS, path, collector)
    _check_list(claim, "caveats", path, collector)

    supporting_sources = _check_string_list(claim, "supporting_sources", path, collector)
    for source_id in supporting_sources:
        if source_id not in context.source_ids:
            collector.add(
                f"{path}.supporting_sources",
                "dangling_reference",
                f"claim references unknown source '{source_id}'",
            )

    supporting_images = _check_string_list(claim, "supporting_images", path, collector)
    for image_id in supporting_images:
        if image_id not in context.image_ids:
            collector.add(
                f"{path}.supporting_images",
                "dangling_reference",
                f"claim references unknown image '{image_id}'",
            )

    visual_supports = _validate_visual_supports(
        claim,
        path,
        collector,
        context,
        supporting_images=supporting_images,
    )

    quote_spans = _check_list(claim, "quote_spans", path, collector)
    for index, quote_span in enumerate(quote_spans):
        quote_path = f"{path}.quote_spans[{index}]"
        if not _require_object(quote_span, quote_path, collector):
            continue
        _require_fields(quote_span, quote_path, ("source_id", "quote", "location"), collector)
        source_id = _optional_string(quote_span, "source_id", quote_path, collector)
        _check_string(quote_span, "quote", quote_path, collector)
        _check_string(quote_span, "location", quote_path, collector)
        if source_id is not None and source_id not in context.source_ids:
            collector.add(
                f"{quote_path}.source_id",
                "dangling_reference",
                f"quote span references unknown source '{source_id}'",
            )

    if confidence == "high" and claim_type == "text" and not quote_spans:
        collector.add(
            f"{path}.quote_spans",
            "missing_text_evidence",
            "high-confidence text claims require at least one quote span",
        )
    if confidence == "high" and claim_type in {"visual", "mixed"} and not supporting_images:
        collector.add(
            f"{path}.supporting_images",
            "missing_visual_evidence",
            "high-confidence visual or mixed claims require image evidence",
        )
    if claim_type in {"visual", "mixed"} and supporting_images and not visual_supports:
        collector.add(
            f"{path}.visual_supports",
            "missing_visual_support",
            "visual or mixed claims with supporting_images require visual_supports",
        )

    votes = claim.get("votes", [])
    if votes is None:
        return
    if not isinstance(votes, list):
        collector.add(f"{path}.votes", "invalid_type", "votes must be a list")
        return
    _validate_verifier_votes(
        votes,
        collector,
        context,
        f"{path}.votes",
        allow_string_refs=True,
    )


def _validate_visual_supports(
    claim: Mapping[str, Any],
    path: str,
    collector: _Collector,
    context: _ValidationContext,
    *,
    supporting_images: Sequence[str],
) -> list[Mapping[str, Any]]:
    raw_supports = claim.get("visual_supports", [])
    if "visual_supports" not in claim:
        return []
    if not isinstance(raw_supports, list):
        collector.add(f"{path}.visual_supports", "invalid_type", "visual_supports must be a list")
        return []

    valid_supports: list[Mapping[str, Any]] = []
    for index, visual_support in enumerate(raw_supports):
        support_path = f"{path}.visual_supports[{index}]"
        if not _require_object(visual_support, support_path, collector):
            continue
        _require_fields(
            visual_support,
            support_path,
            (
                "image_id",
                "observation_ref",
                "observation_index",
                "observation_text",
                "relation_type",
                "provider",
                "rationale",
                "confidence",
            ),
            collector,
        )
        image_id = _check_string(visual_support, "image_id", support_path, collector)
        observation_ref = _check_string(
            visual_support,
            "observation_ref",
            support_path,
            collector,
        )
        observation_index = _check_number(
            visual_support,
            "observation_index",
            support_path,
            collector,
        )
        observation_text = _check_string(
            visual_support,
            "observation_text",
            support_path,
            collector,
        )
        _check_enum(
            visual_support,
            "relation_type",
            VISUAL_RELATION_TYPES,
            support_path,
            collector,
        )
        _check_string(visual_support, "provider", support_path, collector)
        _check_string(visual_support, "rationale", support_path, collector)
        confidence = _check_number(visual_support, "confidence", support_path, collector)
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            collector.add(
                f"{support_path}.confidence",
                "invalid_range",
                "confidence must be between 0.0 and 1.0",
            )

        if image_id is not None:
            if image_id not in context.image_ids:
                collector.add(
                    f"{support_path}.image_id",
                    "dangling_reference",
                    f"visual support references unknown image '{image_id}'",
                )
            if supporting_images and image_id not in supporting_images:
                collector.add(
                    f"{support_path}.image_id",
                    "visual_support_not_in_supporting_images",
                    f"visual support image '{image_id}' is not listed in supporting_images",
                )

        if image_id is None or observation_index is None:
            continue
        if not isinstance(observation_index, int):
            collector.add(
                f"{support_path}.observation_index",
                "invalid_type",
                "observation_index must be an integer",
            )
            continue
        image = context.images_by_id.get(image_id)
        if not isinstance(image, Mapping):
            continue
        observations = image.get("observations", [])
        if not isinstance(observations, list):
            continue
        if observation_index < 0 or observation_index >= len(observations):
            collector.add(
                f"{support_path}.observation_index",
                "invalid_observation_reference",
                f"observation_index {observation_index} is out of range for image '{image_id}'",
            )
            continue
        expected_ref = f"images.{image_id}.observations[{observation_index}]"
        if observation_ref is not None and observation_ref != expected_ref:
            collector.add(
                f"{support_path}.observation_ref",
                "invalid_observation_reference",
                f"observation_ref must equal '{expected_ref}'",
            )
        expected_text = observations[observation_index]
        if observation_text is not None and observation_text != expected_text:
            collector.add(
                f"{support_path}.observation_text",
                "observation_text_mismatch",
                "observation_text must match the referenced image observation",
            )
        valid_supports.append(visual_support)
    return valid_supports


def _validate_search_results(
    records: Sequence[Any],
    collector: _Collector,
    context: _ValidationContext,
) -> None:
    for index, record in enumerate(records):
        path = f"$.search_results[{index}]"
        if not _require_object(record, path, collector):
            continue
        _require_fields(
            record,
            path,
            (
                "id",
                "task_id",
                "angle_id",
                "route",
                "provider",
                "query",
                "url",
                "title",
                "snippet",
                "result_type",
                "rank",
                "freshness_requirement",
                "accessed_at",
                "language",
                "region",
                "policy_decision",
                "policy_flags",
                "raw_provider_metadata",
            ),
            collector,
        )
        _optional_string(record, "id", path, collector)
        task_id = _optional_string(record, "task_id", path, collector)
        if (
            task_id is not None
            and context.search_tasks_provided
            and task_id not in context.search_task_ids
        ):
            collector.add(
                f"{path}.task_id",
                "dangling_reference",
                f"search result references unknown search task '{task_id}'",
            )
        angle_id = _optional_string(record, "angle_id", path, collector)
        route = _check_enum(record, "route", SEARCH_ROUTES, path, collector)
        if angle_id is not None and context.routing_provided:
            expected_route = context.angle_routes.get(angle_id)
            if expected_route is None:
                collector.add(
                    f"{path}.angle_id",
                    "dangling_reference",
                    f"search result references unknown routed angle '{angle_id}'",
                )
            elif route is not None and expected_route != route:
                collector.add(
                    f"{path}.route",
                    "route_mismatch",
                    f"route '{route}' does not match angle '{angle_id}' route '{expected_route}'",
                )
        provider = _check_enum(record, "provider", SEARCH_PROVIDERS, path, collector)
        if provider == "codex-native" and context.mode and context.mode != "codex-plugin":
            collector.add(
                f"{path}.provider",
                "invalid_provider_for_mode",
                "provider 'codex-native' is valid only in codex-plugin mode",
            )
        _check_string(record, "query", path, collector)
        _check_string(record, "url", path, collector)
        _check_string(record, "title", path, collector)
        _check_string(record, "snippet", path, collector)
        _check_enum(record, "result_type", SEARCH_RESULT_TYPES, path, collector)
        _check_number(record, "rank", path, collector)
        _check_enum(record, "freshness_requirement", FRESHNESS_REQUIREMENTS, path, collector)
        _check_string(record, "accessed_at", path, collector)
        _check_string(record, "language", path, collector)
        _check_string(record, "region", path, collector)
        _check_enum(record, "policy_decision", POLICY_DECISIONS, path, collector)
        _check_list(record, "policy_flags", path, collector)
        if "raw_provider_metadata" in record and not isinstance(
            record["raw_provider_metadata"], Mapping
        ):
            collector.add(
                f"{path}.raw_provider_metadata",
                "invalid_type",
                "raw_provider_metadata must be an object",
            )


def _validate_visual_observations(
    records: Sequence[Any],
    collector: _Collector,
    context: _ValidationContext,
) -> None:
    for index, record in enumerate(records):
        path = f"$.visual_observations[{index}]"
        if not _require_object(record, path, collector):
            continue
        image_id = _validate_visual_evidence(
            record,
            path,
            collector,
            context,
            require_source_reference=False,
        )
        if image_id is not None and context.image_ids and image_id not in context.image_ids:
            collector.add(
                f"{path}.id",
                "dangling_reference",
                f"visual observation references unknown image '{image_id}'",
            )


def _collect_top_level_verifier_vote_ids(
    records: Sequence[Any],
    context: _ValidationContext,
) -> None:
    for record in records:
        if isinstance(record, Mapping):
            vote_id = record.get("id")
            if isinstance(vote_id, str) and vote_id:
                context.verifier_vote_ids.add(vote_id)


def _validate_verifier_votes(
    records: Sequence[Any],
    collector: _Collector,
    context: _ValidationContext,
    base_path: str,
    *,
    allow_string_refs: bool,
) -> None:
    for index, record in enumerate(records):
        path = f"{base_path}[{index}]"
        if isinstance(record, str):
            if allow_string_refs:
                if (
                    context.top_level_verifier_votes_provided
                    and record not in context.verifier_vote_ids
                ):
                    collector.add(
                        path,
                        "dangling_reference",
                        f"vote reference '{record}' does not reference a top-level verifier vote",
                    )
            else:
                collector.add(path, "invalid_type", "top-level verifier vote records must be objects")
            continue
        if not _require_object(record, path, collector):
            continue
        _require_fields(
            record,
            path,
            (
                "id",
                "claim_id",
                "verifier_type",
                "agent_name",
                "method",
                "model_or_tool",
                "vote",
                "confidence",
                "evidence_refs",
                "rationale",
                "created_at",
            ),
            collector,
        )
        _optional_string(record, "id", path, collector)
        claim_id = _optional_string(record, "claim_id", path, collector)
        if (
            claim_id is not None
            and context.claims_provided
            and claim_id not in context.claim_ids
        ):
            collector.add(
                f"{path}.claim_id",
                "dangling_reference",
                f"verifier vote references unknown claim '{claim_id}'",
            )
        _check_enum(record, "verifier_type", VERIFIER_TYPES, path, collector)
        _check_string(record, "agent_name", path, collector)
        _check_enum(record, "method", VERIFIER_METHODS, path, collector)
        _check_string(record, "model_or_tool", path, collector)
        _check_enum(record, "vote", VERIFIER_VOTES, path, collector)
        _check_number(record, "confidence", path, collector)
        evidence_refs = _check_string_list(record, "evidence_refs", path, collector)
        known_refs = context.source_ids | context.image_ids
        for reference in evidence_refs:
            if context.evidence_provided and reference not in known_refs:
                collector.add(
                    f"{path}.evidence_refs",
                    "dangling_reference",
                    f"verifier vote references unknown evidence '{reference}'",
                )
        _check_string(record, "rationale", path, collector)
        _check_string(record, "created_at", path, collector)


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


def _optional_list(
    record: Mapping[str, Any],
    field: str,
    path: str,
    collector: _Collector,
) -> list[Any] | None:
    if field not in record:
        return None
    return _check_list(record, field, path, collector)


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


def _optional_string(
    record: Mapping[str, Any],
    field: str,
    path: str,
    collector: _Collector,
) -> str | None:
    if field not in record:
        return None
    return _check_string(record, field, path, collector)


def _check_optional_string(
    record: Mapping[str, Any],
    field: str,
    path: str,
    collector: _Collector,
) -> str | None:
    if field not in record or record[field] is None:
        return None
    return _check_string(record, field, path, collector)


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


def _check_enum(
    record: Mapping[str, Any],
    field: str,
    allowed: tuple[str, ...],
    path: str,
    collector: _Collector,
) -> str | None:
    if field not in record:
        return None
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


def _add_unique_id(
    value: str,
    known_ids: set[str],
    path: str,
    collector: _Collector,
) -> None:
    if value in known_ids:
        collector.add(f"{path}.id", "duplicate_id", f"duplicate id '{value}'")
    known_ids.add(value)
