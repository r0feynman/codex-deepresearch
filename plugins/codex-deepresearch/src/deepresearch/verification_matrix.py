"""Deterministic route-specific verifier matrix for run evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence_schema import SEARCH_ROUTES, validate_artifacts
from .search_handoff import SearchHandoffError, resolve_run_dir


VERIFICATION_MATRIX_SCHEMA_VERSION = "codex-deepresearch.verification-matrix.v0"
MATRIX_METHOD = "runner-agent"
MATRIX_TOOL = "codex-deepresearch"
MATRIX_AGENT_PREFIX = "matrix_"
MATRIX_VOTE_PREFIX = "vote_matrix_"


class VerificationMatrixError(ValueError):
    """Raised when the verifier matrix cannot process a run."""


def verify_claims(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Apply PRD verifier matrix rules to claims in one run directory.

    This slice is intentionally dry: it never calls an external model, hidden
    Codex API, VLM, or live web service. Votes are deterministic runner-agent
    records derived from the evidence already present in the run.
    """

    try:
        run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    except SearchHandoffError as exc:
        raise VerificationMatrixError(str(exc)) from exc

    evidence_path = run_dir / "evidence.json"
    if not evidence_path.exists():
        raise VerificationMatrixError(f"missing evidence.json in run directory: {run_dir}")

    evidence = _read_json(evidence_path)
    claims = evidence.get("claims", [])
    if not isinstance(claims, list):
        raise VerificationMatrixError("evidence.claims must be a list")

    now = _utc_now()
    existing_top_level_votes = _read_existing_verifier_votes(run_dir / "verifier_votes.jsonl")
    sources_by_id = _records_by_id(evidence.get("sources", []))
    images_by_id = _records_by_id(evidence.get("images", []))
    routing_by_angle = _routing_by_angle(evidence.get("routing", []))
    route_counts = {route: 0 for route in SEARCH_ROUTES}
    all_votes: list[dict[str, Any]] = []
    claim_statuses: list[dict[str, Any]] = []
    pruned_count = 0

    for claim in claims:
        if not isinstance(claim, dict):
            continue
        route = _claim_route(
            claim,
            sources_by_id=sources_by_id,
            routing_by_angle=routing_by_angle,
        )
        route_counts[route] += 1
        preserved_votes = _preserved_votes(claim, existing_top_level_votes)
        if _is_budget_pruned(claim):
            pruned_count += 1
            claim["verification_status"] = "budget_pruned"
            claim["review_status"] = "not_reviewed"
            claim["promotion_status"] = "not_eligible"
            claim["confidence"] = "low"
            claim["verification_route"] = route
            claim["votes"] = preserved_votes
            _update_report_eligibility(claim)
            all_votes.extend(preserved_votes)
            claim_statuses.append(_claim_status_record(claim, route, 0))
            continue

        generated_votes = _matrix_votes(
            claim,
            route=route,
            sources_by_id=sources_by_id,
            images_by_id=images_by_id,
            created_at=now,
        )
        claim_votes = preserved_votes + generated_votes
        claim["votes"] = claim_votes
        claim["verification_route"] = route
        _update_claim_state(claim, route=route, votes=claim_votes, images_by_id=images_by_id)
        all_votes.extend(claim_votes)
        claim_statuses.append(_claim_status_record(claim, route, len(generated_votes)))

    evidence["verification_matrix"] = {
        "schema_version": VERIFICATION_MATRIX_SCHEMA_VERSION,
        "status": "completed",
        "verified_at": now,
        "verifier_votes_path": "verifier_votes.jsonl",
        "matrix_status_path": "verification_matrix_status.json",
        "external_model_call": False,
        "routes": route_counts,
        "claims_processed": len([claim for claim in claims if isinstance(claim, Mapping)]),
        "claims_budget_pruned": pruned_count,
    }

    verifier_votes_path = run_dir / "verifier_votes.jsonl"
    matrix_status_path = run_dir / "verification_matrix_status.json"
    _write_json(evidence_path, evidence)
    _write_jsonl(verifier_votes_path, _dedupe_votes(all_votes))

    validation = validate_artifacts(
        evidence_path=evidence_path,
        verifier_votes_path=verifier_votes_path,
    )
    status = {
        "schema_version": VERIFICATION_MATRIX_SCHEMA_VERSION,
        "run_id": evidence.get("run_id", run_dir.name),
        "run_dir": str(run_dir),
        "status": "completed" if validation.valid else "failed_validation",
        "created_at": now,
        "claims_processed": evidence["verification_matrix"]["claims_processed"],
        "claims_budget_pruned": pruned_count,
        "votes_written": len(_dedupe_votes(all_votes)),
        "routes": route_counts,
        "claim_statuses": claim_statuses,
        "validation": validation.to_dict(),
        "artifacts": {
            "evidence": str(evidence_path),
            "verifier_votes": str(verifier_votes_path),
            "verification_matrix_status": str(matrix_status_path),
        },
        "external_model_call": False,
    }
    _write_json(matrix_status_path, status)
    return status


def _matrix_votes(
    claim: Mapping[str, Any],
    *,
    route: str,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
    created_at: str,
) -> list[dict[str, Any]]:
    votes: list[dict[str, Any]] = []
    for ordinal in range(1, 3):
        votes.append(
            _text_vote(
                claim,
                ordinal=ordinal,
                sources_by_id=sources_by_id,
                created_at=created_at,
            )
        )

    votes.append(
        _policy_vote(
            claim,
            sources_by_id=sources_by_id,
            images_by_id=images_by_id,
            created_at=created_at,
        )
    )

    if route == "visual_required" or (
        route == "visual_optional"
        and _visual_budget_allows(claim, images_by_id=images_by_id)
    ):
        votes.append(
            _visual_vote(
                claim,
                route=route,
                images_by_id=images_by_id,
                created_at=created_at,
            )
        )
    return votes


def _text_vote(
    claim: Mapping[str, Any],
    *,
    ordinal: int,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    created_at: str,
) -> dict[str, Any]:
    source_refs = _string_list(claim.get("supporting_sources"))
    quote_spans = _quote_spans(claim)
    evidence_refs = _quote_source_refs(quote_spans) or source_refs
    if not quote_spans:
        vote = "uncertain"
        confidence = 0.35
        rationale = "No quote span is available for deterministic text verification."
    elif _has_blocked_or_failed_source(source_refs, sources_by_id):
        vote = "blocked"
        confidence = 0.8
        rationale = "A supporting source is blocked or failed retrieval."
    else:
        vote = "support"
        confidence = 0.72
        rationale = "The claim has source-linked quote evidence."
    return _vote(
        claim,
        verifier_type="text",
        agent_name=f"matrix_text_{ordinal}",
        ordinal=ordinal,
        vote=vote,
        confidence=confidence,
        evidence_refs=evidence_refs,
        rationale=rationale,
        created_at=created_at,
    )


def _policy_vote(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
    created_at: str,
) -> dict[str, Any]:
    source_refs = _string_list(claim.get("supporting_sources"))
    image_refs = _string_list(claim.get("supporting_images"))
    refs = source_refs + image_refs
    blocked = _has_policy_block(source_refs, sources_by_id) or _has_policy_image(image_refs, images_by_id)
    if blocked:
        vote = "blocked"
        confidence = 0.9
        rationale = "At least one cited evidence record is policy-blocked or requires manual review."
    else:
        vote = "support"
        confidence = 0.75
        rationale = "No blocking policy flags are present on cited evidence."
    return _vote(
        claim,
        verifier_type="policy",
        agent_name="matrix_policy_1",
        ordinal=1,
        vote=vote,
        confidence=confidence,
        evidence_refs=refs,
        rationale=rationale,
        created_at=created_at,
    )


def _visual_vote(
    claim: Mapping[str, Any],
    *,
    route: str,
    images_by_id: Mapping[str, Mapping[str, Any]],
    created_at: str,
) -> dict[str, Any]:
    usable_refs = _usable_image_refs(claim, images_by_id=images_by_id)
    if usable_refs:
        vote = "support"
        confidence = 0.74
        rationale = "Usable visual evidence is linked to the claim."
        evidence_refs = usable_refs
    else:
        vote = "uncertain"
        confidence = 0.3
        rationale = f"No usable VisualEvidence is linked for the {route} route."
        evidence_refs = []
    return _vote(
        claim,
        verifier_type="visual",
        agent_name="matrix_visual_1",
        ordinal=1,
        vote=vote,
        confidence=confidence,
        evidence_refs=evidence_refs,
        rationale=rationale,
        created_at=created_at,
    )


def _update_claim_state(
    claim: dict[str, Any],
    *,
    route: str,
    votes: Sequence[Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    incoming_review_status = claim.get("review_status")
    incoming_promotion_status = claim.get("promotion_status")
    refute_count = sum(1 for vote in votes if vote.get("vote") == "refute")
    support_by_type: dict[str, int] = {}
    blocked_count = 0
    for vote in votes:
        verifier_type = vote.get("verifier_type")
        if vote.get("vote") == "support" and isinstance(verifier_type, str):
            support_by_type[verifier_type] = support_by_type.get(verifier_type, 0) + 1
        if vote.get("vote") == "blocked":
            blocked_count += 1

    if refute_count >= 2:
        status = "refuted"
        review_status = "auto_reviewed"
        promotion_status = "not_eligible"
        confidence = "low"
    elif _needs_visual_evidence(claim, route=route, images_by_id=images_by_id):
        status = "needs_visual_evidence"
        review_status = "needs_more_evidence"
        promotion_status = "not_eligible"
        confidence = "low"
    elif claim.get("claim_type") == "text" and not _quote_spans(claim):
        status = "insufficient_evidence"
        review_status = "needs_more_evidence"
        promotion_status = "not_eligible"
        confidence = "low"
    elif blocked_count:
        status = "policy_blocked"
        review_status = "needs_more_evidence"
        promotion_status = "not_eligible"
        confidence = "low"
    elif _has_required_support(route, support_by_type):
        status = "supported"
        review_status = "auto_reviewed"
        promotion_status = "eligible"
        confidence = "medium"
    else:
        status = "insufficient_evidence"
        review_status = "needs_more_evidence"
        promotion_status = "not_eligible"
        confidence = "low"

    if incoming_review_status == "human_rejected":
        review_status = "human_rejected"
        if incoming_promotion_status == "promotion_rejected":
            promotion_status = "promotion_rejected"
        else:
            promotion_status = "not_eligible"
    elif incoming_promotion_status == "promotion_rejected":
        if incoming_review_status == "human_accepted" and status == "supported":
            review_status = "human_accepted"
        elif review_status == "auto_reviewed":
            review_status = "needs_more_evidence"
        promotion_status = "promotion_rejected"
    elif incoming_review_status == "human_accepted" and status == "supported":
        review_status = "human_accepted"

    claim["verification_status"] = status
    claim["review_status"] = review_status
    claim["promotion_status"] = promotion_status
    claim["confidence"] = confidence
    _update_report_eligibility(claim)


def _update_report_eligibility(claim: dict[str, Any]) -> None:
    status = claim.get("verification_status")
    review_status = claim.get("review_status")
    promotion_status = claim.get("promotion_status")
    reportable = (
        status == "supported"
        and review_status in {"auto_reviewed", "human_accepted"}
        and promotion_status != "promotion_rejected"
    )
    claim["include_in_final_report"] = reportable
    if reportable:
        claim.pop("report_exclusion_reason", None)
    else:
        claim["report_exclusion_reason"] = _report_exclusion_reason(claim)


def _report_exclusion_reason(claim: Mapping[str, Any]) -> str:
    if claim.get("review_status") == "human_rejected":
        return "human_rejected"
    if claim.get("promotion_status") == "promotion_rejected":
        return "promotion_rejected"
    status = claim.get("verification_status")
    if isinstance(status, str) and status:
        return status
    return "not_report_eligible"


def _has_required_support(route: str, support_by_type: Mapping[str, int]) -> bool:
    if support_by_type.get("text", 0) < 2:
        return False
    if support_by_type.get("policy", 0) < 1 and support_by_type.get("freshness", 0) < 1:
        return False
    if route == "visual_required" and support_by_type.get("visual", 0) < 1:
        return False
    return True


def _needs_visual_evidence(
    claim: Mapping[str, Any],
    *,
    route: str,
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    if route == "visual_required":
        return not _usable_image_refs(claim, images_by_id=images_by_id)
    if claim.get("claim_type") in {"visual", "mixed"}:
        return not _usable_image_refs(claim, images_by_id=images_by_id)
    return False


def _claim_route(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    routing_by_angle: Mapping[str, str],
) -> str:
    explicit = _first_allowed_route(
        claim.get("verification_route"),
        claim.get("route"),
        claim.get("search_route"),
    )
    if explicit is not None:
        return explicit

    angle_id = claim.get("angle_id")
    if isinstance(angle_id, str) and angle_id in routing_by_angle:
        return routing_by_angle[angle_id]

    source_routes = {
        source.get("route")
        for source_id in _string_list(claim.get("supporting_sources"))
        for source in [sources_by_id.get(source_id)]
        if isinstance(source, Mapping) and source.get("route") in SEARCH_ROUTES
    }
    if len(source_routes) == 1:
        return str(next(iter(source_routes)))
    if "visual_required" in source_routes:
        return "visual_required"
    if "visual_optional" in source_routes:
        return "visual_optional"

    routing_routes = set(routing_by_angle.values())
    if len(routing_routes) == 1:
        return next(iter(routing_routes))

    if claim.get("claim_type") in {"visual", "mixed"}:
        return "visual_required"
    return "text_only"


def _claim_status_record(claim: Mapping[str, Any], route: str, generated_vote_count: int) -> dict[str, Any]:
    return {
        "claim_id": claim.get("id"),
        "route": route,
        "verification_status": claim.get("verification_status"),
        "review_status": claim.get("review_status"),
        "promotion_status": claim.get("promotion_status"),
        "confidence": claim.get("confidence"),
        "include_in_final_report": claim.get("include_in_final_report"),
        "generated_vote_count": generated_vote_count,
    }


def _vote(
    claim: Mapping[str, Any],
    *,
    verifier_type: str,
    agent_name: str,
    ordinal: int,
    vote: str,
    confidence: float,
    evidence_refs: Sequence[str],
    rationale: str,
    created_at: str,
) -> dict[str, Any]:
    claim_id = str(claim.get("id") or "claim")
    return {
        "id": f"{MATRIX_VOTE_PREFIX}{_safe_id(claim_id)}_{verifier_type}_{ordinal}",
        "claim_id": claim_id,
        "verifier_type": verifier_type,
        "agent_name": agent_name,
        "method": MATRIX_METHOD,
        "model_or_tool": MATRIX_TOOL,
        "vote": vote,
        "confidence": confidence,
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "rationale": rationale,
        "created_at": created_at,
    }


def _preserved_votes(
    claim: Mapping[str, Any],
    top_level_votes: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    preserved: list[dict[str, Any]] = []
    votes = claim.get("votes", [])
    if not isinstance(votes, list):
        return preserved
    for vote in votes:
        record = top_level_votes.get(vote) if isinstance(vote, str) else vote
        if not isinstance(record, Mapping) or _is_matrix_vote(record):
            continue
        preserved.append(dict(record))
    return preserved


def _is_matrix_vote(vote: Mapping[str, Any]) -> bool:
    vote_id = vote.get("id")
    agent_name = vote.get("agent_name")
    return (
        vote.get("method") == MATRIX_METHOD
        and vote.get("model_or_tool") == MATRIX_TOOL
    ) or (
        isinstance(vote_id, str)
        and vote_id.startswith(MATRIX_VOTE_PREFIX)
    ) or (
        isinstance(agent_name, str)
        and agent_name.startswith(MATRIX_AGENT_PREFIX)
    )


def _is_budget_pruned(claim: Mapping[str, Any]) -> bool:
    return claim.get("verification_status") == "budget_pruned" or claim.get("budget_pruned") is True


def _quote_spans(claim: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    spans = claim.get("quote_spans", [])
    if not isinstance(spans, list):
        return []
    return [span for span in spans if isinstance(span, Mapping) and span.get("quote")]


def _quote_source_refs(quote_spans: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(span["source_id"])
        for span in quote_spans
        if isinstance(span.get("source_id"), str) and span.get("source_id")
    ]


def _has_blocked_or_failed_source(
    source_refs: Sequence[str],
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    for source_id in source_refs:
        source = sources_by_id.get(source_id)
        if not isinstance(source, Mapping):
            continue
        if source.get("retrieval_status") == "failed" or source.get("retrieval_error"):
            return True
    return False


def _has_policy_block(
    source_refs: Sequence[str],
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    for source_id in source_refs:
        source = sources_by_id.get(source_id)
        if not isinstance(source, Mapping):
            continue
        if source.get("policy_decision") in {"blocked", "manual_review"}:
            return True
        if source.get("license_policy") in {"restricted", "manual_review"}:
            return True
        if source.get("robots_policy") in {"disallowed", "manual_review"}:
            return True
        if _string_list(source.get("policy_flags")):
            return True
    return False


def _has_policy_image(
    image_refs: Sequence[str],
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    for image_id in image_refs:
        image = images_by_id.get(image_id)
        if not isinstance(image, Mapping):
            continue
        if image.get("analysis_status") == "policy_blocked":
            return True
        if _string_list(image.get("policy_flags")):
            return True
    return False


def _usable_image_refs(
    claim: Mapping[str, Any],
    *,
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    usable: list[str] = []
    for image_id in _string_list(claim.get("supporting_images")):
        image = images_by_id.get(image_id)
        if not isinstance(image, Mapping):
            continue
        if image.get("analysis_status") != "analyzed":
            continue
        if _string_list(image.get("policy_flags")):
            continue
        if not _has_image_source_or_capture(claim, image):
            continue
        observations = image.get("observations", [])
        inferences = image.get("inferences", [])
        if isinstance(observations, list) and observations:
            usable.append(image_id)
        elif isinstance(inferences, list) and inferences:
            usable.append(image_id)
    return usable


def _has_image_source_or_capture(
    claim: Mapping[str, Any],
    image: Mapping[str, Any],
) -> bool:
    if claim.get("claim_type") not in {"visual", "mixed"}:
        return True
    if _non_empty_string(image.get("image_url")):
        return True
    return image.get("origin") == "screenshot" and _non_empty_string(
        image.get("local_artifact_path")
    )


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _visual_budget_allows(
    claim: Mapping[str, Any],
    *,
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    return bool(_usable_image_refs(claim, images_by_id=images_by_id))


def _routing_by_angle(routes: Any) -> dict[str, str]:
    if not isinstance(routes, list):
        return {}
    result: dict[str, str] = {}
    for route in routes:
        if not isinstance(route, Mapping):
            continue
        route_id = route.get("id")
        modality = route.get("modality")
        if isinstance(route_id, str) and modality in SEARCH_ROUTES:
            result[route_id] = str(modality)
    return result


def _records_by_id(records: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(records, list):
        return {}
    return {
        record["id"]: record
        for record in records
        if isinstance(record, Mapping) and isinstance(record.get("id"), str)
    }


def _read_existing_verifier_votes(path: Path) -> dict[str, Mapping[str, Any]]:
    if not path.exists():
        return {}
    votes: dict[str, Mapping[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping) and isinstance(record.get("id"), str):
            votes[record["id"]] = record
    return votes


def _dedupe_votes(votes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for vote in votes:
        vote_id = vote.get("id")
        if isinstance(vote_id, str) and vote_id:
            deduped[vote_id] = dict(vote)
    return [deduped[vote_id] for vote_id in sorted(deduped)]


def _first_allowed_route(*values: Any) -> str | None:
    for value in values:
        if value in SEARCH_ROUTES:
            return str(value)
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _safe_id(value: str) -> str:
    safe = "".join(character if character.isalnum() or character == "_" else "_" for character in value)
    safe = "_".join(part for part in safe.split("_") if part)
    return safe or "claim"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VerificationMatrixError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise VerificationMatrixError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VerificationMatrixError(f"expected JSON object in {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        path.write_text("", encoding="utf-8")
        return
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
