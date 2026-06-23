"""Deterministic policy guardrail enforcement for local evidence bundles."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .evidence_schema import validate_artifacts
from .run_state import begin_stage, skipped_stage_status
from .search_handoff import SearchHandoffError, resolve_run_dir
from .trace import record_stage_trace


GUARDRAILS_SCHEMA_VERSION = "codex-deepresearch.guardrails.v0"
GUARDRAILS_STATUS_FILENAME = "guardrails_status.json"

PROMOTED_STATUSES = {
    "promoted_memory",
    "promoted_playbook",
    "promoted_skill",
    "promoted_prd",
}
HARD_SOURCE_FLAGS = {
    "login_gated",
    "captcha_protected",
    "access_controlled",
    "robots_disallowed",
    "paywall",
    "copyright_restricted",
    "pii_detected",
}
HARD_IMAGE_FLAGS = {
    "private_image",
    "pii_detected",
}
REVIEW_GATE_IMAGE_FLAGS = {
    "sensitive_possible",
    "unknown_license_image",
}
HIGH_RISK_DOMAINS = ("medical", "legal", "financial")
HIGH_RISK_PATTERNS = {
    "medical": re.compile(
        r"\b(diagnos(?:is|ed)|treatment|dosage|symptom|medical|clinical|drug|"
        r"prescription|disease|patient|therapy)\b",
        re.IGNORECASE,
    ),
    "legal": re.compile(
        r"\b(legal|lawsuit|liability|contract|compliance|regulation|court|"
        r"statute|attorney|lawyer)\b",
        re.IGNORECASE,
    ),
    "financial": re.compile(
        r"\b(invest(?:ment|ing)|financial|stock|security|securities|loan|tax|"
        r"portfolio|mortgage|insurance|retirement)\b",
        re.IGNORECASE,
    ),
}


class GuardrailsError(ValueError):
    """Raised when guardrail enforcement cannot process a run."""


def enforce_guardrails(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Apply deterministic policy, privacy, and risk-domain gates in-place."""

    try:
        run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    except SearchHandoffError as exc:
        raise GuardrailsError(str(exc)) from exc

    start = begin_stage(run_dir, "enforce_guardrails")
    if start.skipped:
        status = skipped_stage_status(
            run_dir,
            stage="enforce_guardrails",
            schema_version=GUARDRAILS_SCHEMA_VERSION,
            status_artifact_key="guardrails_status",
            status_filename=GUARDRAILS_STATUS_FILENAME,
            reason=start.skip_reason or "stage_already_completed",
        )
        record_stage_trace(
            run_dir,
            stage="enforce_guardrails",
            agent_role="guardrails_agent",
            status_payload=status,
            prompt_summary="Apply deterministic source, image, claim, privacy, and risk-domain guardrails.",
            tool_call_summary="Skipped guardrail enforcement because run_steps.json marks the stage terminal.",
        )
        _write_json(run_dir / GUARDRAILS_STATUS_FILENAME, status)
        return status

    evidence_path = run_dir / "evidence.json"
    if not evidence_path.exists():
        raise GuardrailsError(f"missing evidence.json in run directory: {run_dir}")

    evidence = _read_json(evidence_path)
    sources = evidence.get("sources", [])
    images = evidence.get("images", [])
    claims = evidence.get("claims", [])
    if not isinstance(sources, list):
        raise GuardrailsError("evidence.sources must be a list")
    if not isinstance(images, list):
        raise GuardrailsError("evidence.images must be a list")
    if not isinstance(claims, list):
        raise GuardrailsError("evidence.claims must be a list")

    enforced_at = _guardrail_timestamp(evidence)
    source_summaries = _enforce_sources(sources)
    sources_by_id = _records_by_id(sources)
    image_summaries = _enforce_images(images, sources_by_id=sources_by_id)
    images_by_id = _records_by_id(images)
    claim_summaries = _enforce_claims(
        claims,
        sources_by_id=sources_by_id,
        images_by_id=images_by_id,
    )

    evidence["guardrails"] = {
        "schema_version": GUARDRAILS_SCHEMA_VERSION,
        "status": "completed",
        "enforced_at": enforced_at,
        "status_path": GUARDRAILS_STATUS_FILENAME,
        "sources_flagged": len([item for item in source_summaries if item["policy_flags"]]),
        "images_flagged": len([item for item in image_summaries if item["policy_flags"]]),
        "claims_blocked": len(
            [item for item in claim_summaries if item["verification_status"] == "policy_blocked"]
        ),
        "claims_downgraded": len([item for item in claim_summaries if item["downgraded"]]),
        "external_model_call": False,
        "external_network_call": False,
    }

    _write_json(evidence_path, evidence)
    validation = validate_artifacts(evidence_path=evidence_path)
    status_path = run_dir / GUARDRAILS_STATUS_FILENAME
    status = {
        "schema_version": GUARDRAILS_SCHEMA_VERSION,
        "run_id": evidence.get("run_id", run_dir.name),
        "run_dir": str(run_dir),
        "status": "completed" if validation.valid else "failed_validation",
        "created_at": enforced_at,
        "sources_processed": len([source for source in sources if isinstance(source, Mapping)]),
        "images_processed": len([image for image in images if isinstance(image, Mapping)]),
        "claims_processed": len([claim for claim in claims if isinstance(claim, Mapping)]),
        "sources": source_summaries,
        "images": image_summaries,
        "claims": claim_summaries,
        "validation": validation.to_dict(),
        "artifacts": {
            "evidence": str(evidence_path),
            "guardrails_status": str(status_path),
        },
        "external_model_call": False,
        "external_network_call": False,
    }
    record_stage_trace(
        run_dir,
        stage="enforce_guardrails",
        agent_role="guardrails_agent",
        status_payload=status,
        prompt_summary="Apply deterministic source, image, claim, privacy, and risk-domain guardrails.",
        tool_call_summary="Read evidence.json, applied local policy rules, and rewrote guarded evidence/status artifacts.",
    )
    _write_json(status_path, status)
    return status


def _enforce_sources(sources: list[Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        flags = _string_list(source.get("policy_flags"))
        flags.extend(_source_flags(source))
        source["policy_flags"] = _ordered_unique(flags)

        hard_flags = sorted(set(source["policy_flags"]).intersection(HARD_SOURCE_FLAGS))
        if hard_flags:
            if set(hard_flags).intersection({"login_gated", "captcha_protected", "access_controlled"}):
                source["retrieval_status"] = "failed"
                source["retrieval_error"] = "guardrail_blocked_access_controlled"
            elif source.get("retrieval_status") not in {"failed", "manual"}:
                source["retrieval_status"] = "manual"
            source["policy_decision"] = "blocked"
            _append_caveats(
                source,
                [f"Guardrail blocked source due to policy flags: {', '.join(hard_flags)}."],
            )
        elif _source_needs_manual_review(source):
            source["policy_decision"] = "manual_review"

        summaries.append(
            {
                "source_id": source.get("id"),
                "policy_decision": source.get("policy_decision"),
                "retrieval_status": source.get("retrieval_status"),
                "policy_flags": list(source.get("policy_flags", [])),
            }
        )
    return summaries


def _enforce_images(
    images: list[Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        source = sources_by_id.get(str(image.get("source_id")))
        flags = _string_list(image.get("policy_flags"))
        flags.extend(_image_flags(image, source=source))
        image["policy_flags"] = _ordered_unique(flags)

        hard_flags = sorted(set(image["policy_flags"]).intersection(HARD_IMAGE_FLAGS))
        if hard_flags:
            image["analysis_status"] = "policy_blocked"
            _append_caveats(
                image,
                [f"Guardrail blocked image due to policy flags: {', '.join(hard_flags)}."],
            )
        elif set(image["policy_flags"]).intersection(REVIEW_GATE_IMAGE_FLAGS):
            if (
                "sensitive_possible" in image["policy_flags"]
                and image.get("analysis_status") == "analyzed"
            ):
                image["analysis_status"] = "needs_manual_review"
            _append_caveats(image, ["Image requires manual policy review before promotion."])

        summaries.append(
            {
                "image_id": image.get("id"),
                "analysis_status": image.get("analysis_status"),
                "policy_flags": list(image.get("policy_flags", [])),
            }
        )
    return summaries


def _enforce_claims(
    claims: list[Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        claim_flags = _string_list(claim.get("policy_flags"))
        source_ids = _string_list(claim.get("supporting_sources"))
        image_ids = _string_list(claim.get("supporting_images"))
        source_flags = _evidence_flags(source_ids, sources_by_id)
        image_flags = _evidence_flags(image_ids, images_by_id)
        blocking_flags = sorted(
            set(source_flags).intersection(HARD_SOURCE_FLAGS).union(
                set(image_flags).intersection(HARD_IMAGE_FLAGS)
            )
        )
        review_gate_flags = sorted(set(image_flags).intersection(REVIEW_GATE_IMAGE_FLAGS))
        high_risk_domains = _high_risk_domains(claim)
        has_primary_source = _has_primary_source(source_ids, sources_by_id)
        downgraded = False

        if source_flags or image_flags:
            claim_flags.extend(source_flags)
            claim_flags.extend(image_flags)
        if high_risk_domains:
            claim_flags.append("high_risk_domain")
            for domain in high_risk_domains:
                claim_flags.append(f"high_risk_{domain}")
        if high_risk_domains and not has_primary_source:
            claim_flags.append("no_primary_source")
            _append_caveats(
                claim,
                [
                    "High-risk medical, legal, or financial claim lacks primary-source support.",
                ],
            )
            if claim.get("confidence") == "high":
                claim["confidence"] = "medium"
                downgraded = True
            if claim.get("promotion_status") in {"eligible", *PROMOTED_STATUSES}:
                claim["promotion_status"] = "not_eligible"
                claim["include_in_final_report"] = False
                claim["report_exclusion_reason"] = "high_risk_no_primary_source"

        if blocking_flags:
            _block_claim(
                claim,
                reason="policy_blocked",
                caveat=f"Guardrail blocked claim due to policy flags: {', '.join(blocking_flags)}.",
            )
        elif review_gate_flags and claim.get("review_status") != "human_accepted":
            _append_caveats(
                claim,
                [
                    f"Manual policy review required before promotion: {', '.join(review_gate_flags)}.",
                ],
            )
            if claim.get("promotion_status") in {"eligible", *PROMOTED_STATUSES}:
                claim["promotion_status"] = "not_eligible"
                claim["include_in_final_report"] = False
                claim["report_exclusion_reason"] = "policy_review_required"

        claim["policy_flags"] = _ordered_unique(claim_flags)
        summaries.append(
            {
                "claim_id": claim.get("id"),
                "verification_status": claim.get("verification_status"),
                "review_status": claim.get("review_status"),
                "promotion_status": claim.get("promotion_status"),
                "confidence": claim.get("confidence"),
                "policy_flags": list(claim.get("policy_flags", [])),
                "downgraded": downgraded,
            }
        )
    return summaries


def _block_claim(claim: dict[str, Any], *, reason: str, caveat: str) -> None:
    claim["verification_status"] = "policy_blocked"
    if claim.get("review_status") == "human_accepted":
        claim["review_status"] = "needs_more_evidence"
    elif claim.get("review_status") not in {"human_rejected", "needs_more_evidence"}:
        claim["review_status"] = "needs_more_evidence"
    if claim.get("promotion_status") in PROMOTED_STATUSES or claim.get("promotion_status") == "eligible":
        claim["promotion_status"] = "not_eligible"
    claim["confidence"] = "low"
    claim["include_in_final_report"] = False
    claim["report_exclusion_reason"] = reason
    _append_caveats(claim, [caveat])


def _source_flags(source: Mapping[str, Any]) -> list[str]:
    flags: list[str] = []
    robots_policy = source.get("robots_policy")
    license_policy = source.get("license_policy")
    if robots_policy == "disallowed":
        flags.append("robots_disallowed")
    elif robots_policy == "manual_review":
        flags.append("robots_manual_review")
    if license_policy == "restricted":
        flags.append("copyright_restricted")
    elif license_policy == "manual_review":
        flags.append("copyright_manual_review")
    if _truthy_metadata(source, "login_gated", "login_required", "auth_required"):
        flags.append("login_gated")
    if _truthy_metadata(source, "captcha", "captcha_required", "captcha_protected"):
        flags.append("captcha_protected")
    if _truthy_metadata(source, "access_controlled", "access_restricted"):
        flags.append("access_controlled")
    if _truthy_metadata(source, "paywall", "paywalled", "is_paywalled") or _metadata_contains(
        source, "paywall"
    ):
        flags.append("paywall")
    if _truthy_metadata(source, "contains_pii", "pii_detected") or _metadata_contains(source, "pii"):
        flags.append("pii_detected")
    if _metadata_contains(source, "copyright"):
        flags.append("copyright_restricted")
    return flags


def _image_flags(image: Mapping[str, Any], *, source: Mapping[str, Any] | None) -> list[str]:
    flags: list[str] = []
    origin = image.get("origin")
    if origin == "user_upload" or (
        origin == "manual"
        and (
            image.get("manual_input_kind") == "local_image"
            or _string_value(image.get("image_url")).startswith("file:")
        )
    ):
        flags.append("sensitive_possible")
    if _truthy_metadata(image, "private", "private_image"):
        flags.append("private_image")
    if _truthy_metadata(image, "contains_pii", "pii_detected") or _metadata_contains(image, "pii"):
        flags.append("pii_detected")

    image_license = (
        image.get("license_policy")
        or image.get("image_license_policy")
        or image.get("copyright_policy")
    )
    source_license = source.get("license_policy") if isinstance(source, Mapping) else None
    if image_license == "unknown" or source_license == "unknown":
        flags.append("unknown_license_image")
    if image_license in {"restricted", "manual_review"}:
        flags.append("copyright_restricted")
    return flags


def _source_needs_manual_review(source: Mapping[str, Any]) -> bool:
    return (
        source.get("policy_decision") == "manual_review"
        or source.get("license_policy") == "manual_review"
        or source.get("robots_policy") == "manual_review"
        or bool(set(_string_list(source.get("policy_flags"))).intersection({"copyright_manual_review"}))
    )


def _evidence_flags(
    ids: Sequence[str],
    records_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    return _ordered_unique(
        flag
        for record_id in ids
        for record in [records_by_id.get(record_id)]
        if isinstance(record, Mapping)
        for flag in _string_list(record.get("policy_flags"))
    )


def _high_risk_domains(claim: Mapping[str, Any]) -> list[str]:
    explicit_domains = _ordered_unique(
        [
            *_string_list(claim.get("domains")),
            *_string_list(claim.get("risk_domains")),
            _string_value(claim.get("domain")),
            _string_value(claim.get("risk_domain")),
        ]
    )
    domains = [
        domain
        for domain in explicit_domains
        if domain.lower() in HIGH_RISK_DOMAINS
    ]
    text = str(claim.get("text") or "")
    for domain, pattern in HIGH_RISK_PATTERNS.items():
        if pattern.search(text):
            domains.append(domain)
    return _ordered_unique([domain.lower() for domain in domains])


def _has_primary_source(
    source_ids: Sequence[str],
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    for source_id in source_ids:
        source = sources_by_id.get(source_id)
        if isinstance(source, Mapping) and source.get("quality") == "primary":
            return True
    return False


def _truthy_metadata(record: Mapping[str, Any], *keys: str) -> bool:
    for key in keys:
        value = record.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1", "required"}:
            return True
    for container_key in ("metadata", "retrieval_metadata", "raw_provider_metadata"):
        container = record.get(container_key)
        if isinstance(container, Mapping) and _truthy_metadata(container, *keys):
            return True
    return False


def _metadata_contains(record: Mapping[str, Any], needle: str) -> bool:
    needle = needle.lower()
    for key in (
        "access_policy",
        "policy_reason",
        "retrieval_error",
        "copyright_status",
        "license_status",
        "content_warning",
    ):
        value = record.get(key)
        if isinstance(value, str) and needle in value.lower():
            return True
    for container_key in ("metadata", "retrieval_metadata", "raw_provider_metadata"):
        container = record.get(container_key)
        if isinstance(container, Mapping):
            for key, value in container.items():
                if needle in str(key).lower() or (
                    isinstance(value, str) and needle in value.lower()
                ):
                    return True
    return False


def _append_caveats(record: dict[str, Any], caveats: Iterable[str]) -> None:
    record["caveats"] = _ordered_unique([*_string_list(record.get("caveats")), *caveats])


def _records_by_id(records: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(records, list):
        return {}
    return {
        record["id"]: record
        for record in records
        if isinstance(record, Mapping) and isinstance(record.get("id"), str)
    }


def _guardrail_timestamp(evidence: Mapping[str, Any]) -> str:
    guardrails = evidence.get("guardrails")
    if isinstance(guardrails, Mapping):
        enforced_at = guardrails.get("enforced_at")
        if isinstance(enforced_at, str) and enforced_at.strip():
            return enforced_at
    created_at = evidence.get("created_at")
    if isinstance(created_at, str) and created_at.strip():
        return created_at
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GuardrailsError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GuardrailsError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GuardrailsError(f"expected JSON object in {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _ordered_unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _string_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return ""
