"""Deterministic report templates and local export formats."""

from __future__ import annotations

import csv
import hashlib
import html
import json
from io import StringIO
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence_schema import validate_artifacts
from .report_generation import (
    REPORT_FILENAME,
    REPORT_STATUS_FILENAME,
    _claim_text_for_report,
    _ordered_unique,
    _quote_text_for_report,
    _string_list,
    _string_value,
    _truncate,
    report_evidence_model,
)
from .report_public_safety import (
    public_artifact_ref,
    public_url_ref,
    sanitize_public_string,
    sanitize_public_value,
)
from .search_handoff import SearchHandoffError, resolve_run_dir


REPORT_EXPORT_SCHEMA_VERSION = "codex-deepresearch.report-export.v0"
EXPORT_FORMATS = ("markdown", "json", "csv", "html")
TEMPLATE_ORDER = (
    "technical_report",
    "market_report",
    "competitor_analysis",
    "incident_report",
)
TEMPLATE_ALIASES = {
    "technical": "technical_report",
    "technical_report": "technical_report",
    "market": "market_report",
    "market_report": "market_report",
    "competitor": "competitor_analysis",
    "competitor_analysis": "competitor_analysis",
    "incident": "incident_report",
    "incident_report": "incident_report",
}
TEMPLATES = {
    "technical_report": {
        "label": "Technical Report",
        "primary_heading": "Technical Findings",
        "summary_focus": "implementation and operational evidence",
    },
    "market_report": {
        "label": "Market Report",
        "primary_heading": "Market Signals",
        "summary_focus": "market evidence and adoption signals",
    },
    "competitor_analysis": {
        "label": "Competitor Analysis",
        "primary_heading": "Competitor Findings",
        "summary_focus": "comparative evidence across competitors or alternatives",
    },
    "incident_report": {
        "label": "Incident Report",
        "primary_heading": "Incident Findings",
        "summary_focus": "incident facts, scope, impact, and response evidence",
    },
}
class ReportExportError(ValueError):
    """Raised when a local report export cannot be produced."""


def export_report(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
    template: str = "technical_report",
    formats: Sequence[str] | None = None,
    output_dir: str | Path | None = None,
    include_excluded_caveats: bool = False,
) -> dict[str, Any]:
    """Export local report artifacts without network, model, or VLM calls."""

    try:
        run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    except SearchHandoffError as exc:
        raise ReportExportError(str(exc)) from exc

    template_id = normalize_report_template(template)
    export_formats = normalize_export_formats(formats or ("all",))
    evidence_path = run_dir / "evidence.json"
    report_status_path = run_dir / REPORT_STATUS_FILENAME
    report_path = run_dir / REPORT_FILENAME
    evidence = _read_json(evidence_path)
    report_status = _read_json(report_status_path)
    report_text = _read_text(report_path)

    validation = validate_artifacts(evidence_path=evidence_path)
    if not validation.valid:
        raise ReportExportError("cannot export report from invalid evidence.json")
    report_state = report_status.get("status")
    if report_state != "completed":
        raise ReportExportError(f"cannot export report_status.json with status {report_state!r}")

    context = _export_context(
        run_dir=run_dir,
        evidence=evidence,
        report_status=report_status,
        report_text=report_text,
        template_id=template_id,
        include_excluded_caveats=include_excluded_caveats,
    )
    destination = Path(output_dir) if output_dir is not None else run_dir / "exports"
    destination.mkdir(parents=True, exist_ok=True)

    outputs: dict[str, str] = {}
    for export_format in export_formats:
        if export_format == "markdown":
            path = destination / f"{template_id}.md"
            path.write_text(render_markdown_export(context), encoding="utf-8")
            outputs[export_format] = public_artifact_ref(str(path), run_dir=run_dir)
        elif export_format == "json":
            path = destination / f"{template_id}.json"
            _write_json(path, render_json_export(context))
            outputs[export_format] = public_artifact_ref(str(path), run_dir=run_dir)
        elif export_format == "csv":
            path = destination / f"{template_id}.csv"
            path.write_text(render_csv_export(context), encoding="utf-8")
            outputs[export_format] = public_artifact_ref(str(path), run_dir=run_dir)
        elif export_format == "html":
            bundle_dir = destination / f"{template_id}-html"
            bundle_dir.mkdir(parents=True, exist_ok=True)
            index_path = bundle_dir / "index.html"
            index_path.write_text(render_html_export(context), encoding="utf-8")
            _write_json(
                bundle_dir / "manifest.json",
                {
                    "schema_version": REPORT_EXPORT_SCHEMA_VERSION,
                    "template": template_id,
                    "entrypoint": "index.html",
                    "used_source_ids": context["used_source_ids"],
                    "used_image_ids": context["used_image_ids"],
                    "claim_ids": context["claim_ids"],
                },
            )
            outputs[export_format] = public_artifact_ref(str(index_path), run_dir=run_dir)
        else:
            raise ReportExportError(f"unsupported export format: {export_format}")

    return {
        "schema_version": REPORT_EXPORT_SCHEMA_VERSION,
        "status": "completed",
        "run_id": context["run_id"],
        "template": template_id,
        "formats": list(export_formats),
        "output_dir": public_artifact_ref(str(destination), run_dir=run_dir),
        "outputs": outputs,
        "claims_exported": len(context["claims"]),
        "excluded_claims_exported": len(context["excluded_claims"]),
        "used_source_ids": context["used_source_ids"],
        "used_image_ids": context["used_image_ids"],
        "external_model_call": False,
    }


def normalize_report_template(value: str) -> str:
    key = value.strip().lower().replace("-", "_").replace(" ", "_")
    template_id = TEMPLATE_ALIASES.get(key)
    if template_id is None:
        expected = ", ".join(TEMPLATE_ORDER)
        raise ReportExportError(f"unsupported report template: {value}; expected one of {expected}")
    return template_id


def normalize_export_formats(values: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        for item in str(value).split(","):
            key = item.strip().lower().replace("-", "_")
            if not key:
                continue
            if key == "md":
                key = "markdown"
            if key == "all":
                return EXPORT_FORMATS
            if key not in EXPORT_FORMATS:
                expected = ", ".join(("all",) + EXPORT_FORMATS)
                raise ReportExportError(f"unsupported export format: {item}; expected one of {expected}")
            normalized.append(key)
    return tuple(dict.fromkeys(normalized)) or EXPORT_FORMATS


def render_markdown_export(context: Mapping[str, Any]) -> str:
    template = context["template"]
    lines: list[str] = []
    lines.append(f"# {context['question']} - {template['label']}")
    lines.append("")
    lines.append(f"Generated: {context['generated_at']}")
    lines.append(f"Run ID: `{context['run_id']}`")
    lines.append(f"Report status: `{context['report_status']['status']}`")
    lines.append(f"Template: `{context['template_id']}`")
    lines.append("")
    lines.append("## Executive Summary")
    if context["claims"]:
        lines.append(
            f"- {len(context['claims'])} supported claim(s) are included from "
            f"{template['summary_focus']}."
        )
        if context["used_source_ids"]:
            lines.append(f"- Sources used: {', '.join(_citation_refs(context['used_source_ids']))}")
        if context["used_image_ids"]:
            lines.append(f"- Images used: {', '.join(_image_refs(context['used_image_ids']))}")
    else:
        lines.append("- No supported claims met the report evidence requirements.")
    if context["caveats"]:
        lines.append(f"- Caveats: {'; '.join(context['caveats'])}")
    lines.append("")

    lines.append(f"## {template['primary_heading']}")
    lines.extend(_markdown_claim_lines(context["claims"], empty="- No supported findings."))
    lines.append("")
    lines.append("## Review Caveats")
    if context["caveats"]:
        for caveat in context["caveats"]:
            lines.append(f"- {caveat}")
    else:
        lines.append("- No caveats recorded for included claims.")
    lines.append("")
    lines.extend(_markdown_image_appendix(context))
    lines.append("")
    lines.extend(_markdown_sources(context))
    if context["excluded_claims"]:
        lines.append("")
        lines.append("## Excluded Or Caveated Claims")
        lines.extend(_markdown_claim_lines(context["excluded_claims"], empty="- No excluded claims requested."))
    lines.append("")
    return "\n".join(lines)


def render_json_export(context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": REPORT_EXPORT_SCHEMA_VERSION,
        "template": context["template_id"],
        "run_id": context["run_id"],
        "question": context["question"],
        "generated_at": context["generated_at"],
        "report_status": context["report_status"],
        "source_report": context["source_report"],
        "claim_ids": context["claim_ids"],
        "used_source_ids": context["used_source_ids"],
        "used_image_ids": context["used_image_ids"],
        "caveats": context["caveats"],
        "claims": context["claims"],
        "excluded_claims": context["excluded_claims"],
        "sources": context["sources"],
        "image_appendix": context["images"],
        "include_excluded_caveats": context["include_excluded_caveats"],
        "external_model_call": False,
    }


def render_csv_export(context: Mapping[str, Any]) -> str:
    fieldnames = [
        "row_type",
        "template",
        "claim_id",
        "claim_type",
        "verification_status",
        "review_status",
        "promotion_status",
        "confidence",
        "source_ids",
        "image_ids",
        "quote_source_ids",
        "visual_support_refs",
        "verifier_vote_refs",
        "visual_verifier_vote_refs",
        "caveats",
        "exclusion_reasons",
        "text",
    ]
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for claim in context["claims"]:
        writer.writerow(_csv_row(context, claim, row_type="included"))
    for claim in context["excluded_claims"]:
        writer.writerow(_csv_row(context, claim, row_type="excluded_caveat"))
    return buffer.getvalue()


def render_html_export(context: Mapping[str, Any]) -> str:
    template = context["template"]
    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>"
        + html.escape(f"{context['question']} - {template['label']}", quote=False)
        + "</title>",
        "<style>",
        "body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;line-height:1.5;margin:2rem;max-width:980px}",
        "code{background:#f4f4f4;padding:0.1rem 0.25rem;border-radius:3px}",
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:0.4rem;text-align:left}",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>{_h(context['question'])} - {_h(template['label'])}</h1>",
        "<dl>",
        f"<dt>Generated</dt><dd>{_h(context['generated_at'])}</dd>",
        f"<dt>Run ID</dt><dd><code>{_h(context['run_id'])}</code></dd>",
        f"<dt>Report status</dt><dd><code>{_h(context['report_status']['status'])}</code></dd>",
        f"<dt>Template</dt><dd><code>{_h(context['template_id'])}</code></dd>",
        "</dl>",
        "<h2>Executive Summary</h2>",
    ]
    if context["claims"]:
        lines.append(
            "<p>"
            + _h(str(len(context["claims"])))
            + " supported claim(s) are included from "
            + _h(template["summary_focus"])
            + ".</p>"
        )
        if context["used_source_ids"]:
            lines.append(f"<p>Sources used: {_html_citation_refs(context['used_source_ids'])}</p>")
        if context["used_image_ids"]:
            lines.append(f"<p>Images used: {_html_image_refs(context['used_image_ids'])}</p>")
    else:
        lines.append("<p>No supported claims met the report evidence requirements.</p>")
    lines.append(f"<h2>{_h(template['primary_heading'])}</h2>")
    lines.extend(_html_claim_lines(context["claims"], empty="No supported findings."))
    lines.append("<h2>Review Caveats</h2>")
    if context["caveats"]:
        lines.append("<ul>")
        for caveat in context["caveats"]:
            lines.append(f"<li>{_h(caveat)}</li>")
        lines.append("</ul>")
    else:
        lines.append("<p>No caveats recorded for included claims.</p>")
    lines.extend(_html_image_appendix(context))
    lines.extend(_html_sources(context))
    if context["excluded_claims"]:
        lines.append("<h2>Excluded Or Caveated Claims</h2>")
        lines.extend(_html_claim_lines(context["excluded_claims"], empty="No excluded claims requested."))
    lines.extend(["</body>", "</html>"])
    return "\n".join(lines) + "\n"


def _export_context(
    *,
    run_dir: Path,
    evidence: Mapping[str, Any],
    report_status: Mapping[str, Any],
    report_text: str,
    template_id: str,
    include_excluded_caveats: bool,
) -> dict[str, Any]:
    model = report_evidence_model(evidence)
    sources_by_id = model["sources_by_id"]
    images_by_id = model["images_by_id"]
    included_items: list[Mapping[str, Any]] = []
    excluded_items: list[Mapping[str, Any]] = []
    for item in model["included"]:
        exclusion_reasons = _export_exclusion_reasons(item)
        if exclusion_reasons:
            excluded_items.append(dict(item, exclusion_reasons=exclusion_reasons))
        else:
            included_items.append(item)
    for item in model["excluded"]:
        excluded_items.append(dict(item, exclusion_reasons=_export_exclusion_reasons(item)))
    claims = [
        _claim_record(
            item,
            sources_by_id=sources_by_id,
            images_by_id=images_by_id,
            run_dir=run_dir,
        )
        for item in included_items
    ]
    excluded_claims = [
        _claim_record(
            item,
            sources_by_id=sources_by_id,
            images_by_id=images_by_id,
            run_dir=run_dir,
            report_inclusion="excluded_caveat",
        )
        for item in excluded_items
    ] if include_excluded_caveats else []
    used_source_ids = _ordered_unique(source_id for claim in claims for source_id in claim["source_ids"])
    used_image_ids = _ordered_unique(image_id for claim in claims for image_id in claim["image_ids"])
    return {
        "template_id": template_id,
        "template": TEMPLATES[template_id],
        "run_id": _string_value(evidence.get("run_id"), run_dir.name),
        "question": sanitize_public_string(
            _string_value(evidence.get("question"), "Codex DeepResearch Report"),
            run_dir=run_dir,
        ),
        "generated_at": _string_value(report_status.get("generated_at"), _string_value(evidence.get("created_at"), "")),
        "report_status": _sanitized_report_status(report_status, run_dir=run_dir),
        "source_report": {
            "filename": REPORT_FILENAME,
            "sha256": hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
            "line_count": len(report_text.splitlines()),
        },
        "claim_ids": [claim["claim_id"] for claim in claims],
        "used_source_ids": used_source_ids,
        "used_image_ids": used_image_ids,
        "caveats": _ordered_unique(caveat for claim in claims for caveat in claim["caveats"]),
        "claims": claims,
        "excluded_claims": excluded_claims,
        "sources": [_source_record(sources_by_id[source_id], run_dir=run_dir) for source_id in used_source_ids],
        "images": [_image_record(images_by_id[image_id], run_dir=run_dir) for image_id in used_image_ids],
        "include_excluded_caveats": include_excluded_caveats,
    }


def _export_exclusion_reasons(item: Mapping[str, Any]) -> list[str]:
    claim = item["claim"]
    reasons = list(item.get("exclusion_reasons", []))
    if claim.get("promotion_status") == "not_eligible":
        reasons.append("not_eligible")
    return _ordered_unique(reasons)


def _claim_record(
    item: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    images_by_id: Mapping[str, Mapping[str, Any]],
    run_dir: Path,
    report_inclusion: str = "included",
) -> dict[str, Any]:
    claim = item["claim"]
    source_ids = list(item["source_ids"])
    image_ids = list(item["image_ids"])
    citations = [
        {
            "source_id": _string_value(quote.get("source_id"), "unknown"),
            "quote": _quote_text_for_report(quote, sources_by_id=sources_by_id),
            "location": _string_value(quote.get("location"), "unspecified location"),
        }
        for quote in item["quote_spans"]
    ]
    visual_supports = []
    for support in _mapping_items(claim.get("visual_supports")):
        if _string_value(support.get("image_id"), "") not in image_ids:
            continue
        record = {
            "image_id": _string_value(support.get("image_id"), "unknown"),
            "relation_type": _string_value(support.get("relation_type"), "visual_match"),
            "provider": _string_value(support.get("provider"), "unknown"),
            "observation_text": _truncate(_string_value(support.get("observation_text"), ""), 220),
        }
        if isinstance(support.get("observation_ref"), str):
            record["observation_ref"] = support["observation_ref"]
        if isinstance(support.get("observation_index"), int):
            record["observation_index"] = support["observation_index"]
        visual_supports.append(record)
    record = {
        "report_inclusion": report_inclusion,
        "claim_id": item["claim_id"],
        "claim_type": item["claim_type"],
        "text": _claim_text_for_report(claim, source_ids, sources_by_id=sources_by_id),
        "verification_status": claim.get("verification_status"),
        "review_status": claim.get("review_status"),
        "promotion_status": claim.get("promotion_status"),
        "confidence": claim.get("confidence"),
        "source_ids": source_ids,
        "image_ids": image_ids,
        "citations": citations,
        "visual_supports": visual_supports,
        "verifier_vote_refs": _string_list(claim.get("verifier_vote_refs")),
        "visual_verifier_vote_refs": _string_list(claim.get("visual_verifier_vote_refs")),
        "caveats": _string_list(claim.get("caveats")),
        "exclusion_reasons": list(item.get("exclusion_reasons", [])),
    }
    return sanitize_public_value(record, run_dir=run_dir)


def _source_record(source: Mapping[str, Any], *, run_dir: Path) -> dict[str, Any]:
    return {
        "source_id": _string_value(source.get("id"), "unknown"),
        "title": sanitize_public_string(_string_value(source.get("title"), ""), run_dir=run_dir),
        "url": public_url_ref(source.get("url"), run_dir=run_dir),
        "accessed_at": _string_value(source.get("accessed_at"), ""),
        "quality": _string_value(source.get("quality"), ""),
        "retrieval_status": _string_value(source.get("retrieval_status"), ""),
        "artifact": public_artifact_ref(source.get("local_artifact_path"), run_dir=run_dir),
    }


def _image_record(image: Mapping[str, Any], *, run_dir: Path) -> dict[str, Any]:
    return {
        "image_id": _string_value(image.get("id"), "unknown"),
        "source_id": _string_value(image.get("source_id"), ""),
        "origin": _string_value(image.get("origin"), ""),
        "image_url": public_url_ref(image.get("image_url"), run_dir=run_dir),
        "page_url": public_url_ref(image.get("page_url"), run_dir=run_dir),
        "artifact": public_artifact_ref(image.get("local_artifact_path"), run_dir=run_dir),
        "analysis_provider": _string_value(image.get("analysis_provider"), ""),
        "analysis_status": _string_value(image.get("analysis_status"), ""),
        "observations": [
            _truncate(sanitize_public_string(value, run_dir=run_dir), 220)
            for value in _string_list(image.get("observations"))
        ],
        "caveats": [
            sanitize_public_string(value, run_dir=run_dir)
            for value in _string_list(image.get("caveats"))
        ],
    }


def _sanitized_report_status(report_status: Mapping[str, Any], *, run_dir: Path) -> dict[str, Any]:
    keys = [
        "schema_version",
        "run_id",
        "status",
        "created_at",
        "generated_at",
        "claims_seen",
        "claims_included",
        "claims_excluded",
        "used_sources",
        "used_images",
        "included_claims",
        "excluded_claims",
        "report_shape",
        "usable_images",
        "visual_evidence_unused",
        "visual_observation_report_links_written",
    ]
    return {key: sanitize_public_value(report_status.get(key), run_dir=run_dir) for key in keys if key in report_status}


def _markdown_claim_lines(claims: Sequence[Mapping[str, Any]], *, empty: str) -> list[str]:
    if not claims:
        return [empty]
    lines: list[str] = []
    for index, claim in enumerate(claims, start=1):
        refs = ", ".join(_citation_refs(claim["source_ids"]) + _image_refs(claim["image_ids"])) or "no resolved evidence"
        prefix = "Excluded claim" if claim["report_inclusion"] == "excluded_caveat" else "Claim"
        lines.append(
            f"{index}. {prefix} `{claim['claim_id']}`: {claim['text']} "
            f"(status `{claim['verification_status']}`; review `{claim['review_status']}`; "
            f"confidence `{claim['confidence']}`; evidence {refs})."
        )
        for citation in claim["citations"]:
            lines.append(
                f"   - Quote [{citation['source_id']}]: \"{citation['quote']}\" "
                f"({citation['location']})"
            )
        for support in claim["visual_supports"]:
            lines.append(
                f"   - Image `{support['image_id']}` ({support['relation_type']}; "
                f"provider `{support['provider']}`): {support['observation_text']}"
            )
        if claim["caveats"]:
            lines.append(f"   - Caveats: {'; '.join(claim['caveats'])}")
        if claim["exclusion_reasons"]:
            lines.append(f"   - Exclusion reasons: {', '.join(claim['exclusion_reasons'])}")
    return lines


def _markdown_image_appendix(context: Mapping[str, Any]) -> list[str]:
    lines = ["## Image Appendix"]
    if not context["images"]:
        lines.append("- No cited images.")
        return lines
    for image in context["images"]:
        lines.append(f"- Image `{image['image_id']}`")
        lines.append(f"  - Source: `{image['source_id']}`")
        if image["artifact"]:
            lines.append(f"  - Artifact reference: `{image['artifact']}`")
        if image["image_url"]:
            lines.append(f"  - Image URL: {image['image_url']}")
        if image["page_url"]:
            lines.append(f"  - Page URL: {image['page_url']}")
        if image["observations"]:
            lines.append(f"  - Observations: {'; '.join(image['observations'])}")
    return lines


def _markdown_sources(context: Mapping[str, Any]) -> list[str]:
    lines = ["## Sources"]
    if not context["sources"]:
        lines.append("- No cited sources.")
        return lines
    for source in context["sources"]:
        title = source["title"] or source["source_id"]
        accessed = source["accessed_at"] or "unknown"
        lines.append(f"- [{source['source_id']}] {title} - {source['url']} (accessed {accessed})")
    return lines


def _html_claim_lines(claims: Sequence[Mapping[str, Any]], *, empty: str) -> list[str]:
    if not claims:
        return [f"<p>{_h(empty)}</p>"]
    lines = ["<ol>"]
    for claim in claims:
        refs = ", ".join(_citation_refs(claim["source_ids"]) + _image_refs(claim["image_ids"])) or "no resolved evidence"
        prefix = "Excluded claim" if claim["report_inclusion"] == "excluded_caveat" else "Claim"
        lines.append("<li>")
        lines.append(
            f"{_h(prefix)} <code>{_h(claim['claim_id'])}</code>: {_h(claim['text'])} "
            f"(status <code>{_h(str(claim['verification_status']))}</code>; "
            f"review <code>{_h(str(claim['review_status']))}</code>; "
            f"confidence <code>{_h(str(claim['confidence']))}</code>; "
            f"evidence {_html_refs_text(refs)})."
        )
        details: list[str] = []
        for citation in claim["citations"]:
            details.append(
                f"Quote [{_h(citation['source_id'])}]: &quot;{_h(citation['quote'])}&quot; "
                f"({_h(citation['location'])})"
            )
        for support in claim["visual_supports"]:
            details.append(
                f"Image <code>{_h(support['image_id'])}</code> "
                f"({_h(support['relation_type'])}; provider <code>{_h(support['provider'])}</code>): "
                f"{_h(support['observation_text'])}"
            )
        if claim["caveats"]:
            details.append("Caveats: " + _h("; ".join(claim["caveats"])))
        if claim["exclusion_reasons"]:
            details.append("Exclusion reasons: " + _h(", ".join(claim["exclusion_reasons"])))
        if details:
            lines.append("<ul>")
            for detail in details:
                lines.append(f"<li>{detail}</li>")
            lines.append("</ul>")
        lines.append("</li>")
    lines.append("</ol>")
    return lines


def _html_image_appendix(context: Mapping[str, Any]) -> list[str]:
    lines = ["<h2>Image Appendix</h2>"]
    if not context["images"]:
        lines.append("<p>No cited images.</p>")
        return lines
    lines.append("<ul>")
    for image in context["images"]:
        lines.append("<li>")
        lines.append(f"Image <code>{_h(image['image_id'])}</code>")
        lines.append("<ul>")
        lines.append(f"<li>Source: <code>{_h(image['source_id'])}</code></li>")
        if image["artifact"]:
            lines.append(f"<li>Artifact reference: <code>{_h(image['artifact'])}</code></li>")
        if image["image_url"]:
            lines.append(f"<li>Image URL: {_h(image['image_url'])}</li>")
        if image["page_url"]:
            lines.append(f"<li>Page URL: {_h(image['page_url'])}</li>")
        if image["observations"]:
            lines.append(f"<li>Observations: {_h('; '.join(image['observations']))}</li>")
        lines.append("</ul>")
        lines.append("</li>")
    lines.append("</ul>")
    return lines


def _html_sources(context: Mapping[str, Any]) -> list[str]:
    lines = ["<h2>Sources</h2>"]
    if not context["sources"]:
        lines.append("<p>No cited sources.</p>")
        return lines
    lines.append("<ul>")
    for source in context["sources"]:
        title = source["title"] or source["source_id"]
        accessed = source["accessed_at"] or "unknown"
        lines.append(
            f"<li>[{_h(source['source_id'])}] {_h(title)} - {_h(source['url'])} "
            f"(accessed {_h(accessed)})</li>"
        )
    lines.append("</ul>")
    return lines


def _csv_row(context: Mapping[str, Any], claim: Mapping[str, Any], *, row_type: str) -> dict[str, str]:
    return {
        "row_type": row_type,
        "template": context["template_id"],
        "claim_id": claim["claim_id"],
        "claim_type": claim["claim_type"],
        "verification_status": str(claim["verification_status"]),
        "review_status": str(claim["review_status"]),
        "promotion_status": str(claim["promotion_status"]),
        "confidence": str(claim["confidence"]),
        "source_ids": ";".join(claim["source_ids"]),
        "image_ids": ";".join(claim["image_ids"]),
        "quote_source_ids": ";".join(_ordered_unique(citation["source_id"] for citation in claim["citations"])),
        "visual_support_refs": ";".join(
            _ordered_unique(
                _string_value(support.get("observation_ref"), "")
                for support in claim["visual_supports"]
                if support.get("observation_ref")
            )
        ),
        "verifier_vote_refs": ";".join(claim["verifier_vote_refs"]),
        "visual_verifier_vote_refs": ";".join(claim["visual_verifier_vote_refs"]),
        "caveats": ";".join(claim["caveats"]),
        "exclusion_reasons": ";".join(claim["exclusion_reasons"]),
        "text": claim["text"],
    }


def _citation_refs(source_ids: Sequence[str]) -> list[str]:
    return [f"[{source_id}]" for source_id in source_ids]


def _image_refs(image_ids: Sequence[str]) -> list[str]:
    return [f"`{image_id}`" for image_id in image_ids]


def _html_citation_refs(source_ids: Sequence[str]) -> str:
    return ", ".join(f"[{_h(source_id)}]" for source_id in source_ids)


def _html_image_refs(image_ids: Sequence[str]) -> str:
    return ", ".join(f"<code>{_h(image_id)}</code>" for image_id in image_ids)


def _html_refs_text(value: str) -> str:
    return html.escape(value, quote=False).replace("`", "")


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReportExportError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReportExportError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReportExportError(f"expected JSON object in {path}")
    return payload


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ReportExportError(f"missing report file: {path}") from exc


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _h(value: Any) -> str:
    return html.escape(str(value), quote=False)


__all__ = [
    "EXPORT_FORMATS",
    "REPORT_EXPORT_SCHEMA_VERSION",
    "ReportExportError",
    "TEMPLATE_ORDER",
    "export_report",
    "normalize_export_formats",
    "normalize_report_template",
    "render_csv_export",
    "render_html_export",
    "render_json_export",
    "render_markdown_export",
]
