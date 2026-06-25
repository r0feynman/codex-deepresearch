"""Evidence provenance browser and local human review workflow."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cache_keys import claim_cache_key
from .evidence_schema import validate_artifacts
from .search_handoff import SearchHandoffError, resolve_run_dir


EVIDENCE_REVIEW_SCHEMA_VERSION = "codex-deepresearch.evidence-review.v0"
REVIEW_STATUS_FILENAME = "review_status.json"
REUSABLE_PROMOTION_STATUSES = {
    "eligible",
    "promoted_memory",
    "promoted_playbook",
    "promoted_skill",
    "promoted_prd",
}
PROMOTED_STATUSES = REUSABLE_PROMOTION_STATUSES - {"eligible"}
CONFIRMED_REVIEW_STATUSES = {"auto_reviewed", "human_accepted"}
REVIEW_DECISIONS = {
    "accepted": "human_accepted",
    "accept": "human_accepted",
    "human_accepted": "human_accepted",
    "rejected": "human_rejected",
    "reject": "human_rejected",
    "human_rejected": "human_rejected",
    "needs_more_evidence": "needs_more_evidence",
    "needs-more-evidence": "needs_more_evidence",
}
SOURCE_BLOCKING_POLICY_FLAGS = {
    "access_controlled",
    "captcha_protected",
    "copyright_restricted",
    "login_gated",
    "paywall",
    "pii_detected",
    "robots_disallowed",
}
SOURCE_REVIEW_POLICY_FLAGS = {
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


class EvidenceReviewError(ValueError):
    """Raised when evidence review cannot process a run or claim."""


def inspect_evidence(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
    claim_id: str | None = None,
) -> dict[str, Any]:
    """Return a JSON provenance graph for claims in a run directory."""

    try:
        run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    except SearchHandoffError as exc:
        raise EvidenceReviewError(str(exc)) from exc

    evidence_path = run_dir / "evidence.json"
    evidence = _read_json(evidence_path)
    claims = evidence.get("claims", [])
    if not isinstance(claims, list):
        raise EvidenceReviewError("evidence.claims must be a list")

    sources_by_id = _records_by_id(evidence.get("sources", []))
    images_by_id = _records_by_id(evidence.get("images", []))
    votes_by_id = _read_jsonl_by_id(run_dir / "verifier_votes.jsonl")
    observations = _read_jsonl_records(run_dir / "visual_observations.jsonl")
    report_status = _read_optional_json(run_dir / "report_status.json")
    review_status = _read_optional_json(run_dir / REVIEW_STATUS_FILENAME)

    selected_claims = [
        claim
        for claim in claims
        if isinstance(claim, Mapping)
        and (claim_id is None or claim.get("id") == claim_id)
    ]
    if claim_id is not None and not selected_claims:
        raise EvidenceReviewError(f"claim not found: {claim_id}")

    claim_records = [
        _claim_provenance(
            claim,
            sources_by_id=sources_by_id,
            images_by_id=images_by_id,
            votes_by_id=votes_by_id,
            observations=observations,
            report_status=report_status,
        )
        for claim in selected_claims
    ]
    return {
        "schema_version": EVIDENCE_REVIEW_SCHEMA_VERSION,
        "run_id": evidence.get("run_id", run_dir.name),
        "run_dir": str(run_dir),
        "status": "completed",
        "claims_seen": len([claim for claim in claims if isinstance(claim, Mapping)]),
        "claims_returned": len(claim_records),
        "claims": claim_records,
        "reuse": _reuse_summary(claim_records),
        "review_status": _compact_review_status(review_status),
        "artifacts": _existing_artifacts(
            run_dir,
            {
                "evidence": "evidence.json",
                "review_status": REVIEW_STATUS_FILENAME,
                "verifier_votes": "verifier_votes.jsonl",
                "visual_observations": "visual_observations.jsonl",
                "report_status": "report_status.json",
                "report": "report.md",
            },
        ),
        "external_model_call": False,
        "external_network_call": False,
    }


def review_claim(
    *,
    run: str | Path,
    claim_id: str,
    decision: str,
    runs_dir: str | Path | None = None,
    reviewer: str | None = None,
    rationale: str | None = None,
) -> dict[str, Any]:
    """Persist a human review decision for a claim in evidence.json."""

    try:
        run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    except SearchHandoffError as exc:
        raise EvidenceReviewError(str(exc)) from exc

    normalized_review_status = _normalize_review_decision(decision)
    evidence_path = run_dir / "evidence.json"
    evidence = _read_json(evidence_path)
    claims = evidence.get("claims", [])
    if not isinstance(claims, list):
        raise EvidenceReviewError("evidence.claims must be a list")

    claim = _claim_by_id(claims, claim_id)
    if claim is None:
        raise EvidenceReviewError(f"claim not found: {claim_id}")

    sources_by_id = _records_by_id(evidence.get("sources", []))
    images_by_id = _records_by_id(evidence.get("images", []))
    current_cache_key = _claim_review_cache_key(
        claim,
        sources_by_id=sources_by_id,
        images_by_id=images_by_id,
    )
    previous = {
        "review_status": claim.get("review_status"),
        "promotion_status": claim.get("promotion_status"),
        "include_in_final_report": claim.get("include_in_final_report"),
        "review_evidence_cache_key": claim.get("review_evidence_cache_key"),
    }
    reviewed_at = _utc_now()

    if normalized_review_status == "human_accepted":
        blockers = _acceptance_blockers(
            claim,
            sources_by_id=sources_by_id,
            images_by_id=images_by_id,
        )
        if blockers:
            raise EvidenceReviewError(
                "claim cannot be accepted while verification or guardrail blockers remain: "
                + ", ".join(blockers)
            )
        claim["review_status"] = "human_accepted"
        if claim.get("promotion_status") not in PROMOTED_STATUSES:
            claim["promotion_status"] = "eligible"
        claim["include_in_final_report"] = True
        claim.pop("report_exclusion_reason", None)
    elif normalized_review_status == "human_rejected":
        claim["review_status"] = "human_rejected"
        claim["promotion_status"] = "promotion_rejected"
        claim["include_in_final_report"] = False
        claim["report_exclusion_reason"] = "human_rejected"
    else:
        claim["review_status"] = "needs_more_evidence"
        claim["promotion_status"] = "not_eligible"
        claim["include_in_final_report"] = False
        claim["report_exclusion_reason"] = "needs_more_evidence"

    claim["review_evidence_cache_key"] = current_cache_key
    claim["reviewed_at"] = reviewed_at
    human_review = {
        "schema_version": EVIDENCE_REVIEW_SCHEMA_VERSION,
        "decision": _decision_name(normalized_review_status),
        "review_status": claim["review_status"],
        "reviewer": reviewer or "local-human-review",
        "rationale": rationale or "",
        "reviewed_at": reviewed_at,
        "evidence_cache_key": current_cache_key,
        "previous": previous,
    }
    claim["human_review"] = human_review

    evidence["review_workflow"] = {
        "schema_version": EVIDENCE_REVIEW_SCHEMA_VERSION,
        "status_path": REVIEW_STATUS_FILENAME,
        "updated_at": reviewed_at,
        "claims_reviewed": len(
            [
                item
                for item in claims
                if isinstance(item, Mapping)
                and item.get("review_status") in {"human_accepted", "human_rejected", "needs_more_evidence"}
            ]
        ),
    }
    _write_json(evidence_path, evidence)

    validation = validate_artifacts(evidence_path=evidence_path)
    updated_evidence = _read_json(evidence_path)
    status = _review_status_payload(
        updated_evidence,
        run_dir=run_dir,
        validation=validation.to_dict(),
        latest_event={
            "claim_id": claim_id,
            "decision": human_review["decision"],
            "review_status": claim["review_status"],
            "promotion_status": claim["promotion_status"],
            "include_in_final_report": claim["include_in_final_report"],
            "reviewed_at": reviewed_at,
            "reviewer": human_review["reviewer"],
            "previous": previous,
        },
    )
    _write_json(run_dir / REVIEW_STATUS_FILENAME, status)
    if not validation.valid:
        status["status"] = "failed_validation"
        _write_json(run_dir / REVIEW_STATUS_FILENAME, status)
    return status


def list_reusable_claims(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
) -> dict[str, Any]:
    """List claims eligible for downstream Codex reuse or promotion."""

    try:
        run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    except SearchHandoffError as exc:
        raise EvidenceReviewError(str(exc)) from exc

    evidence = _read_json(run_dir / "evidence.json")
    claims = evidence.get("claims", [])
    if not isinstance(claims, list):
        raise EvidenceReviewError("evidence.claims must be a list")
    sources_by_id = _records_by_id(evidence.get("sources", []))
    images_by_id = _records_by_id(evidence.get("images", []))

    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        blockers = _reuse_blockers(
            claim,
            sources_by_id=sources_by_id,
            images_by_id=images_by_id,
        )
        record = {
            "claim_id": claim.get("id"),
            "claim_type": claim.get("claim_type"),
            "verification_status": claim.get("verification_status"),
            "review_status": claim.get("review_status"),
            "promotion_status": claim.get("promotion_status"),
            "reuse_eligible": not blockers,
            "reuse_blockers": blockers,
        }
        if blockers:
            excluded.append(record)
        else:
            eligible.append(record)

    return {
        "schema_version": EVIDENCE_REVIEW_SCHEMA_VERSION,
        "run_id": evidence.get("run_id", run_dir.name),
        "run_dir": str(run_dir),
        "status": "completed",
        "eligible_claims": eligible,
        "excluded_claims": excluded,
        "eligible_count": len(eligible),
        "excluded_count": len(excluded),
        "artifacts": _existing_artifacts(
            run_dir,
            {
                "evidence": "evidence.json",
                "review_status": REVIEW_STATUS_FILENAME,
            },
        ),
        "external_model_call": False,
        "external_network_call": False,
    }


def _claim_provenance(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
    votes_by_id: Mapping[str, Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    report_status: Mapping[str, Any] | None,
) -> dict[str, Any]:
    source_ids = _ordered_unique(
        [
            *_string_list(claim.get("supporting_sources")),
            *[
                str(span.get("source_id"))
                for span in _mapping_list(claim.get("quote_spans"))
                if isinstance(span.get("source_id"), str)
            ],
        ]
    )
    image_ids = _ordered_unique(_string_list(claim.get("supporting_images")))
    votes = _claim_votes(claim, votes_by_id=votes_by_id)
    visual_observations = _claim_visual_observations(
        claim,
        image_ids=image_ids,
        observations=observations,
    )
    reuse_blockers = _reuse_blockers(
        claim,
        sources_by_id=sources_by_id,
        images_by_id=images_by_id,
    )
    report_record = _claim_report_record(claim.get("id"), report_status)
    return {
        "claim": dict(claim),
        "claim_id": claim.get("id"),
        "claim_type": claim.get("claim_type"),
        "verification_status": claim.get("verification_status"),
        "review_status": claim.get("review_status"),
        "promotion_status": claim.get("promotion_status"),
        "include_in_final_report": claim.get("include_in_final_report"),
        "reuse_eligible": not reuse_blockers,
        "reuse_blockers": reuse_blockers,
        "sources": [
            _source_record(source_id, sources_by_id.get(source_id))
            for source_id in source_ids
        ],
        "quote_spans": _mapping_list(claim.get("quote_spans")),
        "images": [
            _image_record(image_id, images_by_id.get(image_id), sources_by_id=sources_by_id)
            for image_id in image_ids
        ],
        "visual_supports": _mapping_list(claim.get("visual_supports")),
        "visual_observations": visual_observations,
        "verifier_votes": votes,
        "report": report_record,
        "provenance_chain": _provenance_chain(
            claim,
            source_ids=source_ids,
            image_ids=image_ids,
            votes=votes,
            visual_observations=visual_observations,
            report_record=report_record,
        ),
    }


def _source_record(source_id: str, source: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        return {"source_id": source_id, "missing": True}
    return {
        "source_id": source_id,
        "url": source.get("url"),
        "title": source.get("title"),
        "type": source.get("type"),
        "quality": source.get("quality"),
        "retrieval_status": source.get("retrieval_status"),
        "retrieval_error": source.get("retrieval_error"),
        "local_artifact_path": source.get("local_artifact_path"),
        "policy_decision": source.get("policy_decision"),
        "policy_flags": _string_list(source.get("policy_flags")),
        "license_policy": source.get("license_policy"),
        "robots_policy": source.get("robots_policy"),
        "route": source.get("route"),
        "angle_id": source.get("angle_id"),
    }


def _image_record(
    image_id: str,
    image: Mapping[str, Any] | None,
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(image, Mapping):
        return {"image_id": image_id, "missing": True}
    source = sources_by_id.get(str(image.get("source_id")))
    return {
        "image_id": image_id,
        "source_id": image.get("source_id"),
        "source": _source_record(str(image.get("source_id")), source)
        if isinstance(image.get("source_id"), str)
        else None,
        "origin": image.get("origin"),
        "image_url": image.get("image_url"),
        "page_url": image.get("page_url"),
        "local_artifact_path": image.get("local_artifact_path"),
        "hash": image.get("hash"),
        "analysis_provider": image.get("analysis_provider"),
        "analysis_status": image.get("analysis_status"),
        "policy_decision": image.get("policy_decision"),
        "policy_flags": _string_list(image.get("policy_flags")),
        "candidate_id": image.get("candidate_id"),
        "fetch_id": image.get("fetch_id"),
        "provider_provenance": image.get("provider_provenance"),
        "observations": _string_list(image.get("observations")),
        "inferences": _string_list(image.get("inferences")),
        "ocr_text": image.get("ocr_text"),
        "caveats": _string_list(image.get("caveats")),
    }


def _claim_votes(
    claim: Mapping[str, Any],
    *,
    votes_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    votes: dict[str, dict[str, Any]] = {}
    claim_id = claim.get("id")
    for vote in _mapping_list(claim.get("votes")):
        vote_id = vote.get("id")
        if isinstance(vote_id, str) and vote_id:
            votes[vote_id] = dict(vote)
    for vote_id in _string_list(claim.get("votes")):
        record = votes_by_id.get(vote_id)
        if isinstance(record, Mapping):
            votes[vote_id] = dict(record)
    for vote in votes_by_id.values():
        if vote.get("claim_id") == claim_id and isinstance(vote.get("id"), str):
            votes[str(vote["id"])] = dict(vote)
    return [votes[vote_id] for vote_id in sorted(votes)]


def _claim_visual_observations(
    claim: Mapping[str, Any],
    *,
    image_ids: Sequence[str],
    observations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    claim_id = claim.get("id")
    matched: list[dict[str, Any]] = []
    for observation in observations:
        image_id = observation.get("evidence_image_id") or observation.get("id")
        linked_to_claim = any(
            isinstance(link, Mapping) and link.get("claim_id") == claim_id
            for link in [
                *_mapping_list(observation.get("verifier_links")),
                *_mapping_list(observation.get("report_links")),
            ]
        )
        if image_id in image_ids or linked_to_claim:
            matched.append(dict(observation))
    return matched


def _claim_report_record(
    claim_id: Any,
    report_status: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(claim_id, str) or not isinstance(report_status, Mapping):
        return {"linked": False}
    for section_name in ("included_claims", "excluded_claims"):
        records = report_status.get(section_name, [])
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, Mapping) and record.get("claim_id") == claim_id:
                return {
                    "linked": True,
                    "section": section_name,
                    "status": report_status.get("status"),
                    "report_path": report_status.get("report_path"),
                    "record": dict(record),
                }
    return {
        "linked": False,
        "status": report_status.get("status"),
        "report_path": report_status.get("report_path"),
    }


def _provenance_chain(
    claim: Mapping[str, Any],
    *,
    source_ids: Sequence[str],
    image_ids: Sequence[str],
    votes: Sequence[Mapping[str, Any]],
    visual_observations: Sequence[Mapping[str, Any]],
    report_record: Mapping[str, Any],
) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    for source_id in source_ids:
        chain.append({"kind": "source", "id": source_id})
    for image_id in image_ids:
        chain.append({"kind": "image", "id": image_id})
    for observation in visual_observations:
        observation_id = observation.get("observation_id") or observation.get("id")
        if isinstance(observation_id, str):
            chain.append({"kind": "visual_observation", "id": observation_id})
    for vote in votes:
        vote_id = vote.get("id")
        if isinstance(vote_id, str):
            chain.append(
                {
                    "kind": "verifier_vote",
                    "id": vote_id,
                    "verifier_type": vote.get("verifier_type"),
                    "vote": vote.get("vote"),
                }
            )
    if isinstance(claim.get("id"), str):
        chain.append({"kind": "claim", "id": claim.get("id")})
    if report_record.get("linked"):
        chain.append(
            {
                "kind": "report_citation",
                "section": report_record.get("section"),
                "claim_id": claim.get("id"),
            }
        )
    return chain


def _review_status_payload(
    evidence: Mapping[str, Any],
    *,
    run_dir: Path,
    validation: Mapping[str, Any],
    latest_event: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    sources_by_id = _records_by_id(evidence.get("sources", []))
    images_by_id = _records_by_id(evidence.get("images", []))
    claims = evidence.get("claims", [])
    claim_records: list[dict[str, Any]] = []
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            blockers = _reuse_blockers(
                claim,
                sources_by_id=sources_by_id,
                images_by_id=images_by_id,
            )
            claim_records.append(
                {
                    "claim_id": claim.get("id"),
                    "verification_status": claim.get("verification_status"),
                    "review_status": claim.get("review_status"),
                    "promotion_status": claim.get("promotion_status"),
                    "include_in_final_report": claim.get("include_in_final_report"),
                    "review_evidence_cache_key": claim.get("review_evidence_cache_key"),
                    "reviewed_at": claim.get("reviewed_at"),
                    "reuse_eligible": not blockers,
                    "reuse_blockers": blockers,
                }
            )
    status = {
        "schema_version": EVIDENCE_REVIEW_SCHEMA_VERSION,
        "run_id": evidence.get("run_id", run_dir.name),
        "run_dir": str(run_dir),
        "status": "completed",
        "updated_at": _utc_now(),
        "claims_reviewed": len(
            [
                record
                for record in claim_records
                if record.get("review_status")
                in {"human_accepted", "human_rejected", "needs_more_evidence"}
            ]
        ),
        "claims": claim_records,
        "latest_event": dict(latest_event) if isinstance(latest_event, Mapping) else None,
        "validation": dict(validation),
        "artifacts": {
            "evidence": str(run_dir / "evidence.json"),
            "review_status": str(run_dir / REVIEW_STATUS_FILENAME),
        },
        "external_model_call": False,
        "external_network_call": False,
    }
    return status


def _reuse_summary(claim_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    eligible = [claim for claim in claim_records if claim.get("reuse_eligible") is True]
    return {
        "eligible_count": len(eligible),
        "eligible_claim_ids": [
            claim_id
            for claim in eligible
            for claim_id in [claim.get("claim_id")]
            if isinstance(claim_id, str)
        ],
    }


def _reuse_blockers(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    blockers: list[str] = []
    if claim.get("verification_status") != "supported":
        blockers.append(f"verification_status:{claim.get('verification_status') or 'missing'}")
    if claim.get("review_status") not in CONFIRMED_REVIEW_STATUSES:
        blockers.append(f"review_status:{claim.get('review_status') or 'missing'}")
    if claim.get("promotion_status") not in REUSABLE_PROMOTION_STATUSES:
        blockers.append(f"promotion_status:{claim.get('promotion_status') or 'missing'}")
    blockers.extend(
        _policy_blockers(
            claim,
            sources_by_id=sources_by_id,
            images_by_id=images_by_id,
            include_review_gates=True,
        )
    )
    return _ordered_unique(blockers)


def _acceptance_blockers(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    blockers: list[str] = []
    if claim.get("verification_status") != "supported":
        blockers.append(f"verification_status:{claim.get('verification_status') or 'missing'}")
    blockers.extend(
        _policy_blockers(
            claim,
            sources_by_id=sources_by_id,
            images_by_id=images_by_id,
            include_review_gates=False,
        )
    )
    return _ordered_unique(blockers)


def _policy_blockers(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
    include_review_gates: bool,
) -> list[str]:
    blockers: list[str] = []
    for source_id in _string_list(claim.get("supporting_sources")):
        source = sources_by_id.get(source_id)
        if not isinstance(source, Mapping):
            blockers.append(f"missing_source:{source_id}")
            continue
        blockers.extend(_source_policy_blockers(source, source_id=source_id))
    for image_id in _string_list(claim.get("supporting_images")):
        image = images_by_id.get(image_id)
        if not isinstance(image, Mapping):
            blockers.append(f"missing_image:{image_id}")
            continue
        blockers.extend(
            _image_policy_blockers(
                image,
                image_id=image_id,
                claim=claim,
                include_review_gates=include_review_gates,
            )
        )
    claim_flags = set(_string_list(claim.get("policy_flags")))
    for flag in sorted(claim_flags.intersection(CLAIM_BLOCKING_POLICY_FLAGS)):
        blockers.append(f"claim_policy_flag:{flag}")
    if claim.get("verification_status") == "policy_blocked":
        blockers.append("claim_policy_blocked")
    return blockers


def _source_policy_blockers(source: Mapping[str, Any], *, source_id: str) -> list[str]:
    blockers: list[str] = []
    decision = source.get("policy_decision")
    if decision in {"blocked", "manual_review", "budget_pruned", "disallowed", "restricted"}:
        blockers.append(f"source_policy_decision:{source_id}:{decision}")
    if source.get("license_policy") in {"restricted", "manual_review"}:
        blockers.append(f"source_license_policy:{source_id}:{source.get('license_policy')}")
    if source.get("robots_policy") in {"disallowed", "manual_review"}:
        blockers.append(f"source_robots_policy:{source_id}:{source.get('robots_policy')}")
    retrieval_error = source.get("retrieval_error")
    if (
        source.get("retrieval_status") == "failed"
        and isinstance(retrieval_error, str)
        and retrieval_error.startswith(("guardrail_", "policy_"))
    ):
        blockers.append(f"source_retrieval_error:{source_id}:{retrieval_error}")
    flags = set(_string_list(source.get("policy_flags")))
    for flag in sorted(flags.intersection(SOURCE_BLOCKING_POLICY_FLAGS | SOURCE_REVIEW_POLICY_FLAGS)):
        blockers.append(f"source_policy_flag:{source_id}:{flag}")
    return blockers


def _image_policy_blockers(
    image: Mapping[str, Any],
    *,
    image_id: str,
    claim: Mapping[str, Any],
    include_review_gates: bool,
) -> list[str]:
    blockers: list[str] = []
    if image.get("analysis_status") == "policy_blocked":
        blockers.append(f"image_analysis_status:{image_id}:policy_blocked")
    decision = image.get("policy_decision")
    if decision in IMAGE_BLOCKING_POLICY_DECISIONS:
        blockers.append(f"image_policy_decision:{image_id}:{decision}")
    if image.get("license_policy") in {"restricted", "manual_review"}:
        blockers.append(f"image_license_policy:{image_id}:{image.get('license_policy')}")
    if image.get("robots_policy") in {"disallowed", "manual_review"}:
        blockers.append(f"image_robots_policy:{image_id}:{image.get('robots_policy')}")
    flags = set(_string_list(image.get("policy_flags")))
    for flag in sorted(flags.intersection(IMAGE_BLOCKING_POLICY_FLAGS)):
        blockers.append(f"image_policy_flag:{image_id}:{flag}")
    if include_review_gates and claim.get("review_status") != "human_accepted":
        if image.get("analysis_status") == "needs_manual_review":
            blockers.append(f"image_analysis_status:{image_id}:needs_manual_review")
        for flag in sorted(flags.intersection(IMAGE_REVIEW_GATE_POLICY_FLAGS)):
            blockers.append(f"image_review_gate:{image_id}:{flag}")
    return blockers


def _claim_review_cache_key(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    return claim_cache_key(
        claim,
        sources_by_id=sources_by_id,
        images_by_id=images_by_id,
        verification_route=_string_or_none(claim.get("verification_route"))
        or _string_or_none(claim.get("route"))
        or _string_or_none(claim.get("search_route")),
    )


def _normalize_review_decision(decision: str) -> str:
    normalized = REVIEW_DECISIONS.get(str(decision).strip().lower())
    if normalized is None:
        raise EvidenceReviewError(
            "review decision must be one of accepted, rejected, or needs_more_evidence"
        )
    return normalized


def _decision_name(review_status: str) -> str:
    if review_status == "human_accepted":
        return "accepted"
    if review_status == "human_rejected":
        return "rejected"
    return "needs_more_evidence"


def _claim_by_id(claims: Sequence[Any], claim_id: str) -> dict[str, Any] | None:
    for claim in claims:
        if isinstance(claim, dict) and claim.get("id") == claim_id:
            return claim
    return None


def _compact_review_status(review_status: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(review_status, Mapping):
        return None
    return {
        "status": review_status.get("status"),
        "updated_at": review_status.get("updated_at"),
        "claims_reviewed": review_status.get("claims_reviewed"),
        "latest_event": review_status.get("latest_event"),
    }


def _existing_artifacts(run_dir: Path, filenames: Mapping[str, str]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for key, filename in filenames.items():
        path = run_dir / filename
        if path.exists():
            artifacts[key] = str(path)
    return artifacts


def _records_by_id(records: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(records, list):
        return {}
    return {
        record["id"]: record
        for record in records
        if isinstance(record, Mapping) and isinstance(record.get("id"), str)
    }


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _ordered_unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys([value for value in values if value]))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvidenceReviewError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvidenceReviewError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceReviewError(f"expected JSON object in {path}")
    return payload


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return _read_json(path)


def _read_jsonl_records(path: Path) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceReviewError(f"invalid JSONL in {path}: {exc}") from exc
        if isinstance(record, Mapping):
            records.append(record)
    return records


def _read_jsonl_by_id(path: Path) -> dict[str, Mapping[str, Any]]:
    records: dict[str, Mapping[str, Any]] = {}
    for record in _read_jsonl_records(path):
        record_id = record.get("id")
        if isinstance(record_id, str) and record_id:
            records[record_id] = record
    return records


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
