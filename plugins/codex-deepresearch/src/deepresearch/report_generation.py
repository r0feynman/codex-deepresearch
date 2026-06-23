"""Deterministic Markdown report generation from verified evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence_schema import validate_artifacts
from .search_handoff import SearchHandoffError, resolve_run_dir


REPORT_STATUS_SCHEMA_VERSION = "codex-deepresearch.report-generation.v0"
REPORT_FILENAME = "report.md"
REPORT_STATUS_FILENAME = "report_status.json"
CONFIRMED_REVIEW_STATUSES = {"auto_reviewed", "human_accepted"}
EXCLUDED_STATUSES = {
    "refuted",
    "disputed",
    "insufficient_evidence",
    "needs_visual_evidence",
    "budget_pruned",
    "policy_blocked",
    "unverified",
}
COPYRIGHT_POLICY_FLAGS = {
    "copyright_manual_review",
    "copyright_restricted",
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


class ReportGenerationError(ValueError):
    """Raised when report synthesis cannot process a run."""


def synthesize_report(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Write report.md from locally verified evidence only.

    This stage never calls external web, model, or API services. It treats the
    existing evidence bundle as the only input and records any filtered claims
    in report_status.json so uncertainty is not converted into report prose.
    """

    try:
        run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    except SearchHandoffError as exc:
        raise ReportGenerationError(str(exc)) from exc

    evidence_path = run_dir / "evidence.json"
    if not evidence_path.exists():
        raise ReportGenerationError(f"missing evidence.json in run directory: {run_dir}")

    evidence = _read_json(evidence_path)
    generated_at = _deterministic_generated_at(evidence)
    status_path = run_dir / REPORT_STATUS_FILENAME
    report_path = run_dir / REPORT_FILENAME
    validation = validate_artifacts(evidence_path=evidence_path)
    if not validation.valid:
        _remove_report_if_present(report_path)
        status = {
            "schema_version": REPORT_STATUS_SCHEMA_VERSION,
            "run_id": evidence.get("run_id", run_dir.name),
            "run_dir": str(run_dir),
            "status": "failed_validation",
            "created_at": generated_at,
            "generated_at": generated_at,
            "report_status_path": str(status_path),
            "claims_seen": _claim_count(evidence.get("claims", [])),
            "claims_included": 0,
            "claims_excluded": 0,
            "used_sources": [],
            "used_images": [],
            "included_claims": [],
            "excluded_claims": [],
            "validation": validation.to_dict(),
            "artifacts": {
                "evidence": str(evidence_path),
                "report_status": str(status_path),
            },
            "external_model_call": False,
        }
        _write_json(status_path, status)
        return status

    claims = evidence.get("claims", [])
    if not isinstance(claims, list):
        raise ReportGenerationError("evidence.claims must be a list")

    sources_by_id = _records_by_id(evidence.get("sources", []))
    images_by_id = _records_by_id(evidence.get("images", []))
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        evaluation = _evaluate_claim(
            claim,
            sources_by_id=sources_by_id,
            images_by_id=images_by_id,
        )
        if evaluation["included"]:
            included.append(evaluation)
        else:
            excluded.append(evaluation)

    used_source_ids = _ordered_unique(
        source_id
        for item in included
        for source_id in item["source_ids"]
    )
    used_image_ids = _ordered_unique(
        image_id
        for item in included
        for image_id in item["image_ids"]
    )
    report_markdown = _render_report(
        evidence,
        included,
        excluded,
        sources_by_id=sources_by_id,
        images_by_id=images_by_id,
        generated_at=generated_at,
    )
    report_path.write_text(report_markdown, encoding="utf-8")
    status = {
        "schema_version": REPORT_STATUS_SCHEMA_VERSION,
        "run_id": evidence.get("run_id", run_dir.name),
        "run_dir": str(run_dir),
        "status": "completed",
        "created_at": generated_at,
        "generated_at": generated_at,
        "report_path": str(report_path),
        "report_status_path": str(status_path),
        "claims_seen": _claim_count(claims),
        "claims_included": len(included),
        "claims_excluded": len(excluded),
        "used_sources": used_source_ids,
        "used_images": used_image_ids,
        "included_claims": [
            _status_claim_record(item, include_evidence=True) for item in included
        ],
        "excluded_claims": [
            _status_claim_record(item, include_evidence=False) for item in excluded
        ],
        "validation": validation.to_dict(),
        "artifacts": {
            "evidence": str(evidence_path),
            "report": str(report_path),
            "report_status": str(status_path),
        },
        "external_model_call": False,
    }
    _write_json(status_path, status)
    return status


def _evaluate_claim(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    claim_id = _string_value(claim.get("id"), "claim")
    claim_type = _string_value(claim.get("claim_type"), "text")
    source_ids = _resolved_source_ids(claim, sources_by_id=sources_by_id)
    quote_spans = _resolved_quote_spans(claim, sources_by_id=sources_by_id)
    image_ids = _resolved_image_ids(claim, images_by_id=images_by_id)
    reasons = _exclusion_reasons(
        claim,
        source_ids=source_ids,
        quote_spans=quote_spans,
        image_ids=image_ids,
    )
    return {
        "included": not reasons,
        "claim": dict(claim),
        "claim_id": claim_id,
        "claim_type": claim_type,
        "source_ids": source_ids,
        "quote_spans": quote_spans,
        "image_ids": image_ids,
        "exclusion_reasons": reasons,
    }


def _exclusion_reasons(
    claim: Mapping[str, Any],
    *,
    source_ids: Sequence[str],
    quote_spans: Sequence[Mapping[str, Any]],
    image_ids: Sequence[str],
) -> list[str]:
    reasons: list[str] = []
    if claim.get("verification_status") != "supported":
        reasons.append(_string_value(claim.get("verification_status"), "not_supported"))
    if claim.get("review_status") not in CONFIRMED_REVIEW_STATUSES:
        reasons.append("review_not_confirmed")
    if claim.get("include_in_final_report") is False:
        reasons.append(_string_value(claim.get("report_exclusion_reason"), "not_report_eligible"))
    if claim.get("promotion_status") == "promotion_rejected":
        reasons.append("promotion_rejected")

    confidence = claim.get("confidence")
    claim_type = claim.get("claim_type")
    if claim_type in {"text", "mixed"} and not quote_spans:
        reasons.append("missing_quote_source")
    if claim_type in {"visual", "mixed"} and not image_ids:
        reasons.append("missing_image_evidence")
    if confidence == "high" and claim_type == "text" and not quote_spans:
        reasons.append("high_confidence_text_missing_quote_source")
    if confidence == "high" and claim_type in {"visual", "mixed"} and not image_ids:
        reasons.append("high_confidence_visual_missing_image_evidence")
    if not source_ids and not image_ids:
        reasons.append("missing_resolved_evidence")

    return _ordered_unique(reasons)


def _render_report(
    evidence: Mapping[str, Any],
    included: Sequence[Mapping[str, Any]],
    excluded: Sequence[Mapping[str, Any]],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
    generated_at: str,
) -> str:
    lines: list[str] = []
    title = _string_value(evidence.get("question"), "Codex DeepResearch Report")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"Generated: {generated_at}")
    lines.append(f"Run ID: `{_string_value(evidence.get('run_id'), 'unknown')}`")
    lines.append("")
    lines.append("## Answer")
    if included:
        lines.append(
            f"{len(included)} supported claim(s) met the report evidence requirements."
        )
    else:
        lines.append("No supported claims met the report evidence requirements.")
    lines.append("")
    lines.append("## Evidence")
    if included:
        for index, item in enumerate(included, start=1):
            claim = item["claim"]
            citation_refs = _citation_refs(item["source_ids"])
            image_refs = _image_refs(item["image_ids"])
            evidence_refs = ", ".join(citation_refs + image_refs)
            confidence = _string_value(claim.get("confidence"), "unknown")
            claim_text = _claim_text_for_report(
                claim,
                item["source_ids"],
                sources_by_id=sources_by_id,
            )
            lines.append(
                f"{index}. {claim_text} "
                f"({confidence} confidence; claim `{item['claim_id']}`; evidence {evidence_refs})."
            )
            for quote in item["quote_spans"]:
                source_id = _string_value(quote.get("source_id"), "unknown")
                quote_text = _quote_text_for_report(
                    quote,
                    sources_by_id=sources_by_id,
                )
                location = _string_value(quote.get("location"), "unspecified location")
                lines.append(f"   - Quote [{source_id}]: \"{quote_text}\" ({location})")
            caveats = _string_list(claim.get("caveats"))
            if caveats:
                lines.append(f"   - Caveats: {'; '.join(caveats)}")
    else:
        lines.append("- No confirmed findings.")
    lines.append("")

    if any(item["image_ids"] for item in included):
        lines.append("## Image Appendix")
        for image_id in _ordered_unique(image_id for item in included for image_id in item["image_ids"]):
            image = images_by_id.get(image_id, {})
            source_id = _string_value(image.get("source_id"), "unknown")
            artifact = _string_value(image.get("local_artifact_path"), "unknown")
            image_url = _string_value(image.get("image_url"), "")
            page_url = _string_value(image.get("page_url"), "")
            lines.append(f"- Image `{image_id}`")
            lines.append(f"  - Source: `{source_id}`")
            lines.append(f"  - Artifact: `{artifact}`")
            if image_url:
                lines.append(f"  - Image URL: {image_url}")
            if page_url:
                lines.append(f"  - Page URL: {page_url}")
            observations = _string_list(image.get("observations"))
            if observations:
                lines.append(f"  - Observations: {_truncate('; '.join(observations), 220)}")
        lines.append("")

    lines.append("## Citation And Evidence Mapping")
    if included:
        for item in included:
            claim = item["claim"]
            lines.append(f"- Claim `{item['claim_id']}`")
            lines.append(f"  - Type: `{_string_value(claim.get('claim_type'), 'unknown')}`")
            lines.append(f"  - Verification: `{_string_value(claim.get('verification_status'), 'unknown')}`")
            lines.append(f"  - Review: `{_string_value(claim.get('review_status'), 'unknown')}`")
            if item["source_ids"]:
                lines.append(f"  - Sources: {', '.join(_citation_refs(item['source_ids']))}")
            if item["image_ids"]:
                lines.append(f"  - Images: {', '.join(_image_refs(item['image_ids']))}")
    else:
        lines.append("- No reportable claim mappings.")
    lines.append("")
    lines.append("## Sources")
    if any(item["source_ids"] for item in included):
        for source_id in _ordered_unique(source_id for item in included for source_id in item["source_ids"]):
            source = sources_by_id.get(source_id, {})
            title = _string_value(source.get("title"), source_id)
            url = _string_value(source.get("url"), "")
            accessed_at = _string_value(source.get("accessed_at"), "unknown")
            lines.append(f"- [{source_id}] {title} - {url} (accessed {accessed_at})")
    else:
        lines.append("- No sources cited.")
    lines.append("")
    lines.append("## Excluded Or Caveated Evidence")
    excluded_records = [
        item
        for item in excluded
        if item["claim"].get("verification_status") in EXCLUDED_STATUSES
        or item["exclusion_reasons"]
    ]
    if excluded_records:
        for item in excluded_records:
            claim = item["claim"]
            reasons = ", ".join(item["exclusion_reasons"]) or "not_report_eligible"
            text = _claim_text_for_report(
                claim,
                item["source_ids"],
                sources_by_id=sources_by_id,
            )
            lines.append(f"- Claim `{item['claim_id']}` excluded: {reasons}. {text}")
    else:
        lines.append("- No excluded claims recorded.")
    lines.append("")
    return "\n".join(lines)


def _status_claim_record(item: Mapping[str, Any], *, include_evidence: bool) -> dict[str, Any]:
    claim = item["claim"]
    record = {
        "claim_id": item["claim_id"],
        "claim_type": item["claim_type"],
        "verification_status": claim.get("verification_status"),
        "review_status": claim.get("review_status"),
        "promotion_status": claim.get("promotion_status"),
        "confidence": claim.get("confidence"),
    }
    if include_evidence:
        record["source_ids"] = item["source_ids"]
        record["image_ids"] = item["image_ids"]
    else:
        record["exclusion_reasons"] = item["exclusion_reasons"]
    return record


def _resolved_source_ids(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    ids = _string_list(claim.get("supporting_sources"))
    quote_ids = [
        span["source_id"]
        for span in _quote_spans(claim)
        if isinstance(span.get("source_id"), str)
    ]
    return [
        source_id
        for source_id in _ordered_unique(ids + quote_ids)
        if source_id in sources_by_id
    ]


def _resolved_quote_spans(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    spans: list[Mapping[str, Any]] = []
    for span in _quote_spans(claim):
        source_id = span.get("source_id")
        if not isinstance(source_id, str) or source_id not in sources_by_id:
            continue
        if not _string_value(span.get("quote"), "").strip():
            continue
        spans.append(span)
    return spans


def _resolved_image_ids(
    claim: Mapping[str, Any],
    *,
    images_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    ids: list[str] = []
    for image_id in _string_list(claim.get("supporting_images")):
        image = images_by_id.get(image_id)
        if not isinstance(image, Mapping):
            continue
        if image.get("analysis_status") == "policy_blocked":
            continue
        if _image_policy_blocks(image, claim=claim):
            continue
        ids.append(image_id)
    return _ordered_unique(ids)


def _quote_spans(claim: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    spans = claim.get("quote_spans", [])
    if not isinstance(spans, list):
        return []
    return [span for span in spans if isinstance(span, Mapping)]


def _claim_text_for_report(
    claim: Mapping[str, Any],
    source_ids: Sequence[str],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    text = str(claim.get("text", "")).strip()
    if _has_copyright_restricted_source(source_ids, sources_by_id=sources_by_id):
        return _truncate(text, 80) + " [copyright-truncated]"
    return _truncate(text, 180)


def _quote_text_for_report(
    quote: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    source_id = quote.get("source_id")
    quote_text = str(quote.get("quote", "")).strip()
    if isinstance(source_id, str) and _has_copyright_restricted_source(
        [source_id],
        sources_by_id=sources_by_id,
    ):
        return _truncate(quote_text, 80) + " [copyright-truncated]"
    return _truncate(quote_text, 180)


def _has_copyright_restricted_source(
    source_ids: Sequence[str],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    for source_id in source_ids:
        source = sources_by_id.get(source_id)
        if not isinstance(source, Mapping):
            continue
        if source.get("license_policy") in {"restricted", "manual_review"}:
            return True
        if set(_string_list(source.get("policy_flags"))).intersection(COPYRIGHT_POLICY_FLAGS):
            return True
    return False


def _image_policy_blocks(image: Mapping[str, Any], *, claim: Mapping[str, Any]) -> bool:
    if image.get("analysis_status") == "policy_blocked":
        return True
    flags = set(_string_list(image.get("policy_flags")))
    if flags.intersection(IMAGE_BLOCKING_POLICY_FLAGS):
        return True
    if image.get("analysis_status") == "needs_manual_review":
        return claim.get("review_status") != "human_accepted"
    if flags.intersection(IMAGE_REVIEW_GATE_POLICY_FLAGS):
        return claim.get("review_status") != "human_accepted"
    return False


def _citation_refs(source_ids: Sequence[str]) -> list[str]:
    return [f"[{source_id}]" for source_id in source_ids]


def _image_refs(image_ids: Sequence[str]) -> list[str]:
    return [f"`{image_id}`" for image_id in image_ids]


def _records_by_id(records: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(records, list):
        return {}
    return {
        record["id"]: record
        for record in records
        if isinstance(record, Mapping) and isinstance(record.get("id"), str)
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReportGenerationError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReportGenerationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReportGenerationError(f"expected JSON object in {path}")
    return payload


def _deterministic_generated_at(evidence: Mapping[str, Any]) -> str:
    report_generation = evidence.get("report_generation")
    if isinstance(report_generation, Mapping):
        generated_at = report_generation.get("generated_at")
        if isinstance(generated_at, str) and generated_at.strip():
            return generated_at
    created_at = evidence.get("created_at")
    if isinstance(created_at, str) and created_at.strip():
        return created_at
    return "1970-01-01T00:00:00Z"


def _claim_count(claims: Any) -> int:
    if not isinstance(claims, list):
        return 0
    return len([claim for claim in claims if isinstance(claim, Mapping)])


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _remove_report_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _ordered_unique(values: Sequence[str] | Any) -> list[str]:
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


def _string_value(value: Any, default: str) -> str:
    if isinstance(value, str):
        return value
    return default


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 3].rstrip() + "..."
