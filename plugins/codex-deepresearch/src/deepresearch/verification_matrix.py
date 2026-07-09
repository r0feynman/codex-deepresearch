"""Deterministic route-specific verifier matrix for run evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cache_keys import claim_cache_key
from .evidence_schema import SEARCH_ROUTES, validate_artifacts
from .run_state import begin_stage, skipped_stage_status
from .search_handoff import SearchHandoffError, resolve_run_dir
from .trace import record_stage_trace


VERIFICATION_MATRIX_SCHEMA_VERSION = "codex-deepresearch.verification-matrix.v0"
MATRIX_METHOD = "runner-agent"
MATRIX_TOOL = "codex-deepresearch"
MATRIX_AGENT_PREFIX = "matrix_"
MATRIX_VOTE_PREFIX = "vote_matrix_"
SOURCE_BLOCKING_POLICY_FLAGS = {
    "access_controlled",
    "captcha_protected",
    "copyright_restricted",
    "login_gated",
    "paywall",
    "pii_detected",
    "robots_disallowed",
}
SOURCE_MANUAL_REVIEW_POLICY_FLAGS = {
    "copyright_manual_review",
    "robots_manual_review",
}
IMAGE_BLOCKING_POLICY_FLAGS = {
    "copyright_restricted",
    "pii_detected",
    "private_image",
}
IMAGE_REVIEW_GATE_POLICY_FLAGS = {
    "sensitive_possible",
    "unknown_license_image",
}
IMAGE_BLOCKING_POLICY_DECISIONS = {
    "blocked",
    "budget_pruned",
    "disallowed",
    "manual_review",
    "restricted",
}
CLAIM_BLOCKING_POLICY_FLAGS = SOURCE_BLOCKING_POLICY_FLAGS | IMAGE_BLOCKING_POLICY_FLAGS | {
    "no_primary_source",
}


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

    start = begin_stage(run_dir, "verify_claims")
    if start.skipped:
        status = skipped_stage_status(
            run_dir,
            stage="verify_claims",
            schema_version=VERIFICATION_MATRIX_SCHEMA_VERSION,
            status_artifact_key="verification_matrix_status",
            status_filename="verification_matrix_status.json",
            reason=start.skip_reason or "stage_already_completed",
        )
        record_stage_trace(
            run_dir,
            stage="verify_claims",
            agent_role="verification_matrix_agent",
            status_payload=status,
            prompt_summary="Apply the deterministic verifier matrix to extracted claims.",
            tool_call_summary="Skipped claim verification because run_steps.json marks the stage terminal.",
        )
        _write_json(run_dir / "verification_matrix_status.json", status)
        return status

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
    visual_image_ref_reconciliation = _reconcile_visual_claim_supporting_images(
        claims,
        images_by_id=images_by_id,
    )
    route_counts = {route: 0 for route in SEARCH_ROUTES}
    all_votes: list[dict[str, Any]] = []
    claim_statuses: list[dict[str, Any]] = []
    pruned_count = 0
    reused_count = 0

    for claim in claims:
        if not isinstance(claim, dict):
            continue
        route = _claim_route(
            claim,
            sources_by_id=sources_by_id,
            routing_by_angle=routing_by_angle,
        )
        route_counts[route] += 1
        current_claim_cache_key = claim_cache_key(
            claim,
            sources_by_id=sources_by_id,
            images_by_id=images_by_id,
            verification_route=route,
        )
        preserved_votes = _preserved_votes(claim, existing_top_level_votes)
        reusable_matrix_votes = _existing_matrix_votes(claim, existing_top_level_votes)
        if _can_reuse_claim_verification(
            claim,
            current_claim_cache_key,
            reusable_matrix_votes,
        ):
            claim["cache_key"] = current_claim_cache_key
            claim["verification_cache_key"] = current_claim_cache_key
            claim_votes = preserved_votes + reusable_matrix_votes
            claim["votes"] = claim_votes
            claim["verification_route"] = route
            _update_vote_reference_fields(claim, claim_votes)
            if _current_policy_blocks(
                claim,
                sources_by_id=sources_by_id,
                images_by_id=images_by_id,
            ):
                _apply_current_policy_block(claim)
            else:
                _update_report_eligibility(claim)
            all_votes.extend(claim_votes)
            claim_statuses.append(_claim_status_record(claim, route, 0, cache_hit=True))
            reused_count += 1
            continue

        if _is_budget_pruned(claim):
            pruned_count += 1
            claim["cache_key"] = current_claim_cache_key
            claim["verification_cache_key"] = current_claim_cache_key
            claim["verification_status"] = "budget_pruned"
            claim["review_status"] = "not_reviewed"
            claim["promotion_status"] = "not_eligible"
            claim["confidence"] = "low"
            claim["verification_route"] = route
            claim["votes"] = preserved_votes
            _update_vote_reference_fields(claim, preserved_votes)
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
        claim["cache_key"] = current_claim_cache_key
        claim["verification_cache_key"] = current_claim_cache_key
        claim["verified_at"] = now
        _update_vote_reference_fields(claim, claim_votes)
        _update_claim_state(
            claim,
            route=route,
            votes=claim_votes,
            images_by_id=images_by_id,
            current_cache_key=current_claim_cache_key,
        )
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
        "claims_reused": reused_count,
        "claims_budget_pruned": pruned_count,
        "dangling_visual_supporting_images_pruned": visual_image_ref_reconciliation[
            "supporting_images_pruned"
        ],
        "visual_supporting_images_added": visual_image_ref_reconciliation[
            "supporting_images_added"
        ],
    }

    verifier_votes_path = run_dir / "verifier_votes.jsonl"
    matrix_status_path = run_dir / "verification_matrix_status.json"
    _write_json(evidence_path, evidence)
    _write_jsonl(verifier_votes_path, _dedupe_votes(all_votes))
    _persist_visual_observation_verifier_links(
        run_dir / "visual_observations.jsonl",
        claims,
    )

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
        "claims_reused": reused_count,
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
    record_stage_trace(
        run_dir,
        stage="verify_claims",
        agent_role="verification_matrix_agent",
        status_payload=status,
        prompt_summary="Apply the deterministic verifier matrix to extracted claims.",
        tool_call_summary="Reused unchanged claim verification cache hits, generated local verifier votes for changed claims by route, and wrote verifier_votes.jsonl.",
    )
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
    quote_spans = _quote_spans(claim)
    source_refs = _claim_source_refs(claim)
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
    source_refs = _claim_source_refs(claim)
    image_refs = _string_list(claim.get("supporting_images"))
    refs = source_refs + image_refs
    blocked = (
        _has_policy_block(source_refs, sources_by_id)
        or _has_policy_image(image_refs, images_by_id, claim=claim)
        or _has_claim_policy_block(claim)
    )
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
    current_cache_key: str,
) -> None:
    incoming_review_status = claim.get("review_status")
    incoming_promotion_status = claim.get("promotion_status")
    human_review_current = _human_review_matches_current_evidence(
        claim,
        current_cache_key=current_cache_key,
    )
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
    elif blocked_count:
        status = "policy_blocked"
        review_status = "needs_more_evidence"
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
    elif _has_required_support(claim, route=route, support_by_type=support_by_type):
        status = "supported"
        review_status = "auto_reviewed"
        promotion_status = "eligible"
        confidence = "medium"
    else:
        status = "insufficient_evidence"
        review_status = "needs_more_evidence"
        promotion_status = "not_eligible"
        confidence = "low"

    if incoming_review_status == "human_rejected" and human_review_current:
        review_status = "human_rejected"
        if incoming_promotion_status == "promotion_rejected":
            promotion_status = "promotion_rejected"
        else:
            promotion_status = "not_eligible"
    elif incoming_promotion_status == "promotion_rejected" and human_review_current:
        if incoming_review_status == "human_accepted" and status == "supported":
            review_status = "human_accepted"
        elif review_status == "auto_reviewed":
            review_status = "needs_more_evidence"
        promotion_status = "promotion_rejected"
    elif incoming_review_status == "human_accepted" and status == "supported" and human_review_current:
        review_status = "human_accepted"

    if incoming_review_status in {"human_accepted", "human_rejected"}:
        claim["review_stale"] = not human_review_current
        if human_review_current:
            claim.pop("review_stale_reason", None)
        else:
            claim["review_stale_reason"] = "evidence_changed_since_review"

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


def _current_policy_blocks(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    source_refs = _claim_source_refs(claim)
    image_refs = _string_list(claim.get("supporting_images"))
    return (
        _has_policy_block(source_refs, sources_by_id)
        or _has_policy_image(image_refs, images_by_id, claim=claim)
        or _has_claim_policy_block(claim)
    )


def _apply_current_policy_block(claim: dict[str, Any]) -> None:
    claim["verification_status"] = "policy_blocked"
    claim["review_status"] = "needs_more_evidence"
    claim["promotion_status"] = "not_eligible"
    claim["confidence"] = "low"
    _update_report_eligibility(claim)


def _human_review_matches_current_evidence(
    claim: Mapping[str, Any],
    *,
    current_cache_key: str,
) -> bool:
    review_cache_key = claim.get("review_evidence_cache_key")
    if not isinstance(review_cache_key, str) or not review_cache_key:
        return True
    return review_cache_key == current_cache_key


def _has_required_support(
    claim: Mapping[str, Any],
    *,
    route: str,
    support_by_type: Mapping[str, int],
) -> bool:
    if support_by_type.get("policy", 0) < 1 and support_by_type.get("freshness", 0) < 1:
        return False
    claim_type = claim.get("claim_type")
    if claim_type == "visual":
        return support_by_type.get("visual", 0) >= 1
    if support_by_type.get("text", 0) < 2:
        return False
    if (
        route == "visual_required"
        or claim_type == "mixed"
    ) and support_by_type.get("visual", 0) < 1:
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
    explicit = _first_allowed_route(claim.get("route"), claim.get("search_route"))
    if explicit is not None:
        return explicit

    source_routes = {
        source.get("route")
        for source_id in _claim_source_refs(claim)
        for source in [sources_by_id.get(source_id)]
        if isinstance(source, Mapping) and source.get("route") in SEARCH_ROUTES
    }
    if len(source_routes) == 1:
        return str(next(iter(source_routes)))
    if "visual_required" in source_routes:
        return "visual_required"
    if "visual_optional" in source_routes:
        return "visual_optional"

    angle_id = claim.get("angle_id")
    if isinstance(angle_id, str) and angle_id in routing_by_angle:
        return routing_by_angle[angle_id]

    persisted = _first_allowed_route(claim.get("verification_route"))
    if persisted is not None:
        return persisted

    routing_routes = set(routing_by_angle.values())
    if len(routing_routes) == 1:
        return next(iter(routing_routes))

    if claim.get("claim_type") in {"visual", "mixed"}:
        return "visual_required"
    return "text_only"


def _claim_status_record(
    claim: Mapping[str, Any],
    route: str,
    generated_vote_count: int,
    *,
    cache_hit: bool = False,
) -> dict[str, Any]:
    return {
        "claim_id": claim.get("id"),
        "route": route,
        "verification_status": claim.get("verification_status"),
        "review_status": claim.get("review_status"),
        "promotion_status": claim.get("promotion_status"),
        "confidence": claim.get("confidence"),
        "include_in_final_report": claim.get("include_in_final_report"),
        "generated_vote_count": generated_vote_count,
        "cache_hit": cache_hit,
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


def _update_vote_reference_fields(
    claim: dict[str, Any],
    votes: Sequence[Mapping[str, Any]],
) -> None:
    vote_ids = [
        vote_id
        for vote in votes
        for vote_id in [vote.get("id")]
        if isinstance(vote_id, str) and vote_id
    ]
    visual_vote_ids = [
        vote_id
        for vote in votes
        for vote_id in [vote.get("id")]
        if vote.get("verifier_type") == "visual"
        and isinstance(vote_id, str)
        and vote_id
    ]
    claim["verifier_vote_refs"] = list(dict.fromkeys(vote_ids))
    if visual_vote_ids:
        claim["visual_verifier_vote_refs"] = list(dict.fromkeys(visual_vote_ids))
    else:
        claim.pop("visual_verifier_vote_refs", None)


def _persist_visual_observation_verifier_links(
    path: Path,
    claims: Sequence[Any],
) -> None:
    if not path.exists():
        return
    observations = _read_jsonl_records(path)
    if observations is None:
        return

    links_by_image_id: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        claim_id = claim.get("id")
        if not isinstance(claim_id, str) or not claim_id:
            continue
        vote_ids = _string_list(claim.get("visual_verifier_vote_refs"))
        if not vote_ids:
            continue
        supports = claim.get("visual_supports", [])
        if not isinstance(supports, list):
            continue
        for support in supports:
            if not isinstance(support, Mapping):
                continue
            image_id = support.get("image_id")
            observation_ref = support.get("observation_ref")
            if not isinstance(image_id, str) or not isinstance(observation_ref, str):
                continue
            for vote_id in vote_ids:
                link = {
                    "claim_id": claim_id,
                    "visual_support_ref": observation_ref,
                    "verifier_vote_id": vote_id,
                }
                for field in (
                    "plan_id",
                    "task_id",
                    "angle_id",
                    "route",
                    "candidate_id",
                    "fetch_id",
                    "evidence_image_id",
                ):
                    if isinstance(support.get(field), str) and support[field]:
                        link[field] = support[field]
                links_by_image_id.setdefault(image_id, []).append(
                    link
                )

    if not links_by_image_id:
        return

    changed = False
    updated: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        record = dict(observation)
        image_id = record.get("evidence_image_id") or record.get("id")
        links = links_by_image_id.get(image_id) if isinstance(image_id, str) else None
        if links:
            record["verifier_links"] = _merge_link_records(
                record.get("verifier_links"),
                links,
                key_fields=("claim_id", "visual_support_ref", "verifier_vote_id"),
            )
            changed = True
        updated.append(record)

    if changed:
        _write_jsonl(path, updated)


def _merge_link_records(
    existing: Any,
    additions: Sequence[Mapping[str, Any]],
    *,
    key_fields: Sequence[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(existing, list):
        records = [dict(item) for item in existing if isinstance(item, Mapping)]
    seen = {
        tuple(record.get(field) for field in key_fields)
        for record in records
    }
    for addition in additions:
        record = dict(addition)
        key = tuple(record.get(field) for field in key_fields)
        if key in seen:
            continue
        records.append(record)
        seen.add(key)
    return records


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


def _existing_matrix_votes(
    claim: Mapping[str, Any],
    top_level_votes: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    matrix_votes: dict[str, dict[str, Any]] = {}
    votes = claim.get("votes", [])
    if not isinstance(votes, list):
        return []
    for vote in votes:
        record = top_level_votes.get(vote) if isinstance(vote, str) else vote
        if not isinstance(record, Mapping) or not _is_matrix_vote(record):
            continue
        vote_id = record.get("id")
        if isinstance(vote_id, str) and vote_id:
            matrix_votes[vote_id] = dict(record)
    return [matrix_votes[vote_id] for vote_id in sorted(matrix_votes)]


def _can_reuse_claim_verification(
    claim: Mapping[str, Any],
    current_cache_key: str,
    matrix_votes: Sequence[Mapping[str, Any]],
) -> bool:
    if claim.get("verification_cache_key") != current_cache_key:
        return False
    if claim.get("verification_status") in {None, "unverified"}:
        return False
    return bool(matrix_votes) or _is_budget_pruned(claim)


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


def _claim_source_refs(claim: Mapping[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            [
                *_string_list(claim.get("supporting_sources")),
                *_quote_source_refs(_quote_spans(claim)),
            ]
        )
    )


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
        flags = set(_string_list(source.get("policy_flags")))
        if flags.intersection(SOURCE_BLOCKING_POLICY_FLAGS | SOURCE_MANUAL_REVIEW_POLICY_FLAGS):
            return True
    return False


def _has_policy_image(
    image_refs: Sequence[str],
    images_by_id: Mapping[str, Mapping[str, Any]],
    *,
    claim: Mapping[str, Any],
) -> bool:
    for image_id in image_refs:
        image = images_by_id.get(image_id)
        if not isinstance(image, Mapping):
            continue
        if _image_policy_blocks(image, claim=claim):
            return True
    return False


def _has_claim_policy_block(claim: Mapping[str, Any]) -> bool:
    return bool(set(_string_list(claim.get("policy_flags"))).intersection(CLAIM_BLOCKING_POLICY_FLAGS))


def _usable_image_refs(
    claim: Mapping[str, Any],
    *,
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    if claim.get("claim_type") in {"visual", "mixed"}:
        return _usable_visual_support_refs(claim, images_by_id=images_by_id)

    usable: list[str] = []
    for image_id in _string_list(claim.get("supporting_images")):
        image = images_by_id.get(image_id)
        if not isinstance(image, Mapping):
            continue
        if image.get("analysis_status") != "analyzed":
            continue
        if _image_policy_blocks(image, claim=claim):
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


def _usable_visual_support_refs(
    claim: Mapping[str, Any],
    *,
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    supports = claim.get("visual_supports", [])
    if not isinstance(supports, list):
        return []
    usable: list[str] = []
    for support in supports:
        if not isinstance(support, Mapping):
            continue
        image_id = support.get("image_id")
        observation_index = support.get("observation_index")
        if not isinstance(image_id, str) or not isinstance(observation_index, int):
            continue
        if image_id not in _string_list(claim.get("supporting_images")):
            continue
        image = images_by_id.get(image_id)
        if not isinstance(image, Mapping):
            continue
        if image.get("analysis_status") != "analyzed":
            continue
        if _image_policy_blocks(image, claim=claim):
            continue
        if not _has_image_source_or_capture(claim, image):
            continue
        observations = image.get("observations", [])
        if not isinstance(observations, list):
            continue
        if observation_index < 0 or observation_index >= len(observations):
            continue
        if support.get("observation_text") != observations[observation_index]:
            continue
        usable.append(image_id)
    return list(dict.fromkeys(usable))


def _reconcile_visual_claim_supporting_images(
    claims: Sequence[Any],
    *,
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, int]:
    pruned = 0
    added = 0
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        if claim.get("claim_type") not in {"visual", "mixed"}:
            continue

        original_images = list(dict.fromkeys(_string_list(claim.get("supporting_images"))))
        supported_images = _valid_visual_support_image_refs(
            claim,
            images_by_id=images_by_id,
        )
        if not supported_images:
            continue
        supported_image_set = set(supported_images)
        reconciled_images = [
            image_id for image_id in original_images if image_id in supported_image_set
        ]
        reconciled_image_set = set(reconciled_images)
        for image_id in supported_images:
            if image_id not in reconciled_image_set:
                reconciled_images.append(image_id)
                reconciled_image_set.add(image_id)

        if reconciled_images == original_images:
            continue
        pruned += len(
            [
                image_id
                for image_id in original_images
                if image_id not in supported_image_set
            ]
        )
        original_image_set = set(original_images)
        added += len(
            [image_id for image_id in supported_images if image_id not in original_image_set]
        )
        claim["supporting_images"] = reconciled_images

    return {
        "supporting_images_pruned": pruned,
        "supporting_images_added": added,
    }


def _valid_visual_support_image_refs(
    claim: Mapping[str, Any],
    *,
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    supports = claim.get("visual_supports", [])
    if not isinstance(supports, list):
        return []
    usable: list[str] = []
    for support in supports:
        if not isinstance(support, Mapping):
            continue
        image_id = support.get("image_id")
        observation_index = support.get("observation_index")
        if not isinstance(image_id, str) or not isinstance(observation_index, int):
            continue
        image = images_by_id.get(image_id)
        if not isinstance(image, Mapping):
            continue
        observations = image.get("observations", [])
        if not isinstance(observations, list):
            continue
        if observation_index < 0 or observation_index >= len(observations):
            continue
        if support.get("observation_text") != observations[observation_index]:
            continue
        usable.append(image_id)
    return list(dict.fromkeys(usable))


def _image_policy_blocks(image: Mapping[str, Any], *, claim: Mapping[str, Any]) -> bool:
    if image.get("analysis_status") == "policy_blocked":
        return True
    if image.get("policy_decision") in IMAGE_BLOCKING_POLICY_DECISIONS:
        return True
    if image.get("license_policy") in {"restricted", "manual_review"}:
        return True
    if image.get("robots_policy") in {"disallowed", "manual_review"}:
        return True
    flags = set(_string_list(image.get("policy_flags")))
    if flags.intersection(IMAGE_BLOCKING_POLICY_FLAGS):
        return True
    if image.get("analysis_status") == "needs_manual_review":
        return claim.get("review_status") != "human_accepted"
    if flags.intersection(IMAGE_REVIEW_GATE_POLICY_FLAGS):
        return claim.get("review_status") != "human_accepted"
    return False


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


def _read_jsonl_records(path: Path) -> list[Any] | None:
    records: list[Any] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            return None
    return records


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
