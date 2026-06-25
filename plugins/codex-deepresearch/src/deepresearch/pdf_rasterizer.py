"""Deterministic PDF page and figure rasterization candidates."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlparse


DEFAULT_PDF_RASTERIZER_PROVIDER = "local-pdf-rasterizer"
DEFAULT_MAX_PDF_BYTES = 25_000_000
PDF_RASTERIZER_MODE = "deterministic-public-safe"
PDF_RASTER_WIDTH = 1240
PDF_RASTER_HEIGHT = 1754


@dataclass(frozen=True)
class PdfRasterizerResult:
    """Candidate records plus diagnostics from one PDF rasterization pass."""

    candidates: list[dict[str, Any]]
    diagnostics: list[dict[str, Any]]
    pages_configured: int
    pages_rasterized: int
    pages_skipped: int
    estimated_cost_usd: float
    actual_cost_usd: float


def collect_pdf_rasterizer_candidates(
    *,
    run_dir: Path,
    sources: Sequence[Mapping[str, Any]],
    routes: Sequence[Mapping[str, Any]],
    created_at: str,
    max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES,
    provider: str = DEFAULT_PDF_RASTERIZER_PROVIDER,
    provider_mode: str = "fixture",
) -> PdfRasterizerResult:
    """Create visual acquisition candidates for allowed PDF pages and figures.

    This adapter is intentionally public-safe: it reads only already-local PDF
    artifacts and writes deterministic PNG candidates through the shared visual
    acquisition materializer. It never downloads remote PDFs, unlocks encrypted
    documents, or bypasses paywalls.
    """

    if max_pdf_bytes < 1:
        raise ValueError("max_pdf_bytes must be positive")

    visual_routes = [route for route in routes if _route_is_visual(route)]
    if not visual_routes:
        return PdfRasterizerResult([], [], 0, 0, 0, 0.0, 0.0)

    candidates: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    pages_configured = 0
    pages_rasterized = 0

    for source in sources:
        if source.get("type") != "pdf":
            continue
        route = _source_route(visual_routes, source)
        if route is None:
            continue

        page_specs = _page_specs(source)
        pages_configured += len(page_specs)
        pdf_path = _source_pdf_path(run_dir, source)
        blocker = _source_blocker(source, pdf_path=pdf_path, max_pdf_bytes=max_pdf_bytes)
        if blocker is not None:
            diagnostic = _diagnostic_candidate(
                provider=provider,
                provider_mode=provider_mode,
                source=source,
                route=route,
                page_number=page_specs[0]["page_number"] if page_specs else 1,
                reason=blocker["reason"],
                policy_decision=blocker["policy_decision"],
                policy_flags=blocker["policy_flags"],
                created_at=created_at,
            )
            candidates.append(diagnostic)
            diagnostics.append(_diagnostic_from_candidate(diagnostic, blocker["message"]))
            continue

        pdf_size = pdf_path.stat().st_size if pdf_path is not None else 0
        pdf_hash = _sha256_file(pdf_path) if pdf_path is not None else None
        for spec in page_specs:
            candidates.append(
                _page_candidate(
                    provider=provider,
                    provider_mode=provider_mode,
                    source=source,
                    route=route,
                    spec=spec,
                    pdf_path=pdf_path,
                    pdf_size=pdf_size,
                    pdf_hash=pdf_hash,
                    created_at=created_at,
                )
            )
            pages_rasterized += 1

    pages_skipped = len(diagnostics)
    return PdfRasterizerResult(
        candidates=candidates,
        diagnostics=diagnostics,
        pages_configured=pages_configured,
        pages_rasterized=pages_rasterized,
        pages_skipped=pages_skipped,
        estimated_cost_usd=0.0,
        actual_cost_usd=0.0,
    )


def _page_candidate(
    *,
    provider: str,
    provider_mode: str,
    source: Mapping[str, Any],
    route: Mapping[str, Any],
    spec: Mapping[str, Any],
    pdf_path: Path | None,
    pdf_size: int,
    pdf_hash: str | None,
    created_at: str,
) -> dict[str, Any]:
    page_number = int(spec["page_number"])
    figure_hint = spec.get("figure_hint")
    origin = "pdf_figure" if isinstance(figure_hint, Mapping) else "pdf_page"
    candidate_class = origin
    figure_suffix = ""
    if isinstance(figure_hint, Mapping):
        figure_suffix = "_" + _safe_id(
            str(
                figure_hint.get("label")
                or figure_hint.get("figure")
                or figure_hint.get("caption")
                or "figure"
            )
        )
    local_artifact_path = (
        f"images/pdf/{_safe_id(str(source['id']))}_page_{page_number:03d}"
        f"{figure_suffix}.png"
    )
    content_seed = (
        f"{provider}:{source['id']}:page:{page_number}:"
        f"{figure_suffix}:{pdf_hash or source.get('url')}"
    )
    provider_run_id = _provider_run_id(source, route)
    pdf_url = str(source.get("url") or "")
    page_url = _http_url_or_none(pdf_url)
    pdf_local_path = _pdf_local_path_value(pdf_path, source)
    observation = (
        f"PDF page {page_number} from {source.get('title') or source.get('url')} "
        "was rasterized into a deterministic local visual artifact."
    )
    if isinstance(figure_hint, Mapping):
        label = figure_hint.get("label") or figure_hint.get("figure")
        caption = figure_hint.get("caption")
        hint_text = " ".join(str(value) for value in (label, caption) if value)
        if hint_text:
            observation = f"{observation} Figure hint: {hint_text}."

    candidate = {
        "id": "cand_" + _safe_id(f"{provider}_{source['id']}_{candidate_class}_{page_number}{figure_suffix}"),
        "provider": provider,
        "provider_kind": "pdf_rasterizer",
        "provider_mode": provider_mode,
        "provider_run_id": provider_run_id,
        "provider_provenance": _provider_provenance(
            provider=provider,
            provider_mode=provider_mode,
            provider_run_id=provider_run_id,
        ),
        "source_id": source["id"],
        "source_search_result_id": source.get("search_result_id"),
        "source_url": page_url,
        "route": route["modality"],
        "angle_id": route["id"],
        "task_id": _task_id(route),
        "candidate_class": candidate_class,
        "origin": origin,
        "image_url": None,
        "page_url": page_url,
        "local_artifact_path": local_artifact_path,
        "mime_type": "image/png",
        "width": PDF_RASTER_WIDTH,
        "height": PDF_RASTER_HEIGHT,
        "alt_text": f"PDF page {page_number}" if origin == "pdf_page" else f"PDF figure on page {page_number}",
        "content_seed": content_seed,
        "phash": "pdf_" + _safe_id(f"{source['id']}_{page_number}{figure_suffix}"),
        "visual_tasks": list(route.get("visual_tasks", [])),
        "analysis_provider": _analysis_provider(source),
        "analysis_status": "analyzed",
        "observations": [observation],
        "inferences": [],
        "vlm_visual_summary": observation,
        "vlm_visual_description": observation,
        "ocr_text": None,
        "ocr_outputs": [],
        "policy_flags": list(_string_list(source.get("policy_flags"))),
        "policy_decision": "allowed",
        "caveats": [
            "Deterministic public-safe PDF rasterizer artifact; no external raster library or network call was used."
        ],
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "pdf_url": pdf_url,
        "pdf_local_path": pdf_local_path,
        "page_number": page_number,
        "figure_hint": dict(figure_hint) if isinstance(figure_hint, Mapping) else None,
        "rasterizer": {
            "mode": PDF_RASTERIZER_MODE,
            "optional_raster_library": None,
            "optional_raster_library_available": False,
            "external_network_call": False,
            "input_pdf_bytes": pdf_size,
            "input_pdf_hash": pdf_hash,
            "page_number": page_number,
            "figure_hint": dict(figure_hint) if isinstance(figure_hint, Mapping) else None,
        },
        "compute_counters": {
            "pdf_pages_attempted": 1,
            "pdf_pages_rasterized": 1,
            "pdf_pages_skipped": 0,
            "input_pdf_bytes": pdf_size,
        },
        "cost_counters": {
            "estimated_cost_usd": 0.0,
            "actual_cost_usd": 0.0,
        },
        "acquired_at": created_at,
    }
    candidate["candidate_id"] = candidate["id"]
    return candidate


def _diagnostic_candidate(
    *,
    provider: str,
    provider_mode: str,
    source: Mapping[str, Any],
    route: Mapping[str, Any],
    page_number: int,
    reason: str,
    policy_decision: str,
    policy_flags: Sequence[str],
    created_at: str,
) -> dict[str, Any]:
    provider_run_id = _provider_run_id(source, route)
    pdf_url = str(source.get("url") or "")
    page_url = _http_url_or_none(pdf_url)
    candidate_id = "cand_" + _safe_id(f"{provider}_{source['id']}_diagnostic_{reason}_{page_number}")
    record = {
        "id": candidate_id,
        "candidate_id": candidate_id,
        "provider": provider,
        "provider_kind": "pdf_rasterizer",
        "provider_mode": provider_mode,
        "provider_run_id": provider_run_id,
        "provider_provenance": _provider_provenance(
            provider=provider,
            provider_mode=provider_mode,
            provider_run_id=provider_run_id,
        ),
        "source_id": source["id"],
        "source_search_result_id": source.get("search_result_id"),
        "source_url": page_url,
        "route": route["modality"],
        "angle_id": route["id"],
        "task_id": _task_id(route),
        "candidate_class": "pdf_page",
        "origin": "pdf_page",
        "image_url": None,
        "page_url": page_url,
        "local_artifact_path": f"images/pdf/{_safe_id(str(source['id']))}_diagnostic_{_safe_id(reason)}.png",
        "mime_type": "image/png",
        "width": 0,
        "height": 0,
        "hash": "sha256:" + hashlib.sha256(f"{source['id']}:{reason}".encode("utf-8")).hexdigest(),
        "phash": "pdf_diagnostic_" + _safe_id(f"{source['id']}_{reason}"),
        "visual_tasks": list(route.get("visual_tasks", [])),
        "analysis_provider": _analysis_provider(source),
        "analysis_status": "policy_blocked" if policy_decision == "blocked" else "skipped",
        "observations": [],
        "inferences": [],
        "policy_flags": list(policy_flags),
        "policy_decision": policy_decision,
        "caveats": [reason],
        "status": "removed",
        "candidate_status": "policy_blocked" if policy_decision == "blocked" else "fetch_failed",
        "removal_reasons": [reason],
        "rejection_reason": reason,
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "pdf_url": pdf_url,
        "pdf_local_path": _pdf_local_path_value(None, source),
        "page_number": page_number,
        "figure_hint": None,
        "pdf_diagnostic": {
            "reason": reason,
            "policy_decision": policy_decision,
            "policy_flags": list(policy_flags),
        },
        "compute_counters": {
            "pdf_pages_attempted": 1,
            "pdf_pages_rasterized": 0,
            "pdf_pages_skipped": 1,
        },
        "cost_counters": {
            "estimated_cost_usd": 0.0,
            "actual_cost_usd": 0.0,
        },
        "acquired_at": created_at,
    }
    return record


def _diagnostic_from_candidate(candidate: Mapping[str, Any], message: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "source_id": candidate.get("source_id"),
        "pdf_url": candidate.get("pdf_url"),
        "page_number": candidate.get("page_number"),
        "reason": candidate.get("rejection_reason"),
        "message": message,
        "policy_decision": candidate.get("policy_decision"),
        "policy_flags": list(candidate.get("policy_flags", []))
        if isinstance(candidate.get("policy_flags"), list)
        else [],
    }


def _source_blocker(
    source: Mapping[str, Any],
    *,
    pdf_path: Path | None,
    max_pdf_bytes: int,
) -> dict[str, Any] | None:
    policy_flags = list(_string_list(source.get("policy_flags")))
    policy_decision = str(source.get("policy_decision") or "allowed")
    if (
        policy_decision == "blocked"
        or source.get("license_policy") == "restricted"
        or source.get("robots_policy") == "disallowed"
    ):
        flags = _dedupe(policy_flags + ["policy_blocked"])
        return {
            "reason": "policy_blocked_pdf",
            "message": "PDF source is blocked by policy metadata.",
            "policy_decision": "blocked",
            "policy_flags": flags,
        }
    if policy_decision == "manual_review":
        flags = _dedupe(policy_flags + ["manual_review_required"])
        return {
            "reason": "policy_manual_review_pdf",
            "message": "PDF source requires manual policy review before rasterization.",
            "policy_decision": "manual_review",
            "policy_flags": flags,
        }
    if _is_paywalled(source):
        flags = _dedupe(policy_flags + ["paywalled"])
        return {
            "reason": "paywalled_pdf",
            "message": "PDF source appears paywalled or login-gated.",
            "policy_decision": "blocked",
            "policy_flags": flags,
        }
    if pdf_path is None or not pdf_path.is_file():
        return {
            "reason": "missing_pdf_artifact",
            "message": "PDF source has no existing local artifact for rasterization.",
            "policy_decision": "manual_review",
            "policy_flags": _dedupe(policy_flags + ["missing_local_pdf_artifact"]),
        }
    pdf_size = pdf_path.stat().st_size
    if pdf_size > max_pdf_bytes:
        return {
            "reason": "too_large_pdf",
            "message": f"PDF source is {pdf_size} bytes, above the {max_pdf_bytes} byte cap.",
            "policy_decision": "allowed",
            "policy_flags": policy_flags,
        }
    try:
        prefix = pdf_path.read_bytes()[:4096]
    except OSError as exc:
        return {
            "reason": "unsupported_pdf",
            "message": f"PDF artifact could not be read: {exc}",
            "policy_decision": "manual_review",
            "policy_flags": _dedupe(policy_flags + ["read_failed"]),
        }
    if not prefix.startswith(b"%PDF-"):
        return {
            "reason": "unsupported_pdf",
            "message": "PDF artifact does not start with a PDF header.",
            "policy_decision": "manual_review",
            "policy_flags": _dedupe(policy_flags + ["unsupported_pdf"]),
        }
    if b"/Encrypt" in prefix:
        return {
            "reason": "encrypted_pdf",
            "message": "PDF artifact is encrypted and cannot be rasterized.",
            "policy_decision": "manual_review",
            "policy_flags": _dedupe(policy_flags + ["encrypted_pdf"]),
        }
    return None


def _page_specs(source: Mapping[str, Any]) -> list[dict[str, Any]]:
    pages = _configured_pages(source)
    figure_hints = _figure_hints(source)
    specs: list[dict[str, Any]] = [{"page_number": page, "figure_hint": None} for page in pages]
    for hint in figure_hints:
        page_number = _int_value(
            hint.get("page_number")
            or hint.get("page")
            or hint.get("page_index")
            or hint.get("pdf_page")
        )
        if page_number is None:
            page_number = pages[0] if pages else 1
        specs.append({"page_number": page_number, "figure_hint": dict(hint)})
    if not specs:
        specs.append({"page_number": 1, "figure_hint": None})
    return specs


def _configured_pages(source: Mapping[str, Any]) -> list[int]:
    values: list[int] = []
    for container in _source_containers(source):
        for key in ("pdf_pages", "rasterize_pages", "pages", "page_numbers"):
            if key in container:
                values.extend(_page_values(container.get(key)))
    return _dedupe_int(values)


def _figure_hints(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    hints: list[Mapping[str, Any]] = []
    for container in _source_containers(source):
        raw = container.get("figure_hints") or container.get("figures")
        if isinstance(raw, Mapping):
            hints.append(raw)
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            hints.extend(item for item in raw if isinstance(item, Mapping))
    return hints


def _source_containers(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = [source]
    raw = source.get("raw_provider_metadata")
    if isinstance(raw, Mapping):
        containers.append(raw)
    rasterizer = source.get("pdf_rasterizer")
    if isinstance(rasterizer, Mapping):
        containers.append(rasterizer)
    return containers


def _page_values(value: Any) -> list[int]:
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, int):
        return [value] if value > 0 else []
    if isinstance(value, str):
        values: list[int] = []
        for part in re.split(r"[, ]+", value.strip()):
            if not part:
                continue
            if "-" in part:
                start_raw, end_raw = part.split("-", 1)
                start = _int_value(start_raw)
                end = _int_value(end_raw)
                if start is not None and end is not None and 0 < start <= end <= 999:
                    values.extend(range(start, end + 1))
            else:
                item = _int_value(part)
                if item is not None:
                    values.append(item)
        return values
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values: list[int] = []
        for item in value:
            values.extend(_page_values(item))
        return values
    return []


def _source_pdf_path(run_dir: Path, source: Mapping[str, Any]) -> Path | None:
    local_path = source.get("local_artifact_path")
    if isinstance(local_path, str) and local_path:
        candidate = (run_dir / local_path).resolve(strict=False)
        if candidate.is_file() and candidate.suffix.lower() == ".pdf":
            try:
                candidate.relative_to(run_dir.resolve())
                return candidate
            except ValueError:
                return None
    url = str(source.get("url") or "")
    parsed = urlparse(url)
    if parsed.scheme == "file" and parsed.path:
        candidate = Path(unquote(parsed.path))
        if candidate.is_file() and candidate.suffix.lower() == ".pdf":
            return candidate
    return None


def _pdf_local_path_value(pdf_path: Path | None, source: Mapping[str, Any]) -> str | None:
    if pdf_path is not None:
        local_path = source.get("local_artifact_path")
        if isinstance(local_path, str) and local_path:
            return local_path
        return pdf_path.as_uri()
    local_path = source.get("local_artifact_path")
    return str(local_path) if isinstance(local_path, str) and local_path else None


def _source_route(
    routes: Sequence[Mapping[str, Any]],
    source: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    angle_id = source.get("angle_id")
    route = source.get("route")
    for candidate in routes:
        if angle_id and candidate.get("id") == angle_id:
            return candidate
        if route and candidate.get("modality") == route:
            return candidate
    return routes[0] if len(routes) == 1 else None


def _route_is_visual(route: Mapping[str, Any]) -> bool:
    return route.get("modality") in {"visual_required", "visual_optional"} and int(route.get("max_images") or 0) > 0


def _task_id(route: Mapping[str, Any]) -> str:
    task_id = route.get("task_id")
    if isinstance(task_id, str) and task_id:
        return task_id
    angle_id = str(route.get("id") or "angle_001")
    return "task_visual_" + _safe_id(angle_id.removeprefix("angle_"))


def _provider_run_id(source: Mapping[str, Any], route: Mapping[str, Any]) -> str:
    return str(source.get("run_id") or route.get("run_id") or "pdf-rasterizer-local")


def _provider_provenance(
    *,
    provider: str,
    provider_mode: str,
    provider_run_id: str,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "provider_kind": "pdf_rasterizer",
        "provider_mode": provider_mode,
        "provider_run_id": provider_run_id,
        "fixture_only": provider_mode != "real",
        "rasterizer_mode": PDF_RASTERIZER_MODE,
        "optional_raster_library": None,
        "optional_raster_library_available": False,
        "external_network_call": False,
        "external_vlm_call": False,
    }


def _analysis_provider(source: Mapping[str, Any]) -> str:
    provider = source.get("analysis_provider")
    if isinstance(provider, str) and provider:
        return provider
    return "codex-interactive"


def _is_paywalled(source: Mapping[str, Any]) -> bool:
    if source.get("paywalled") is True or source.get("is_paywalled") is True:
        return True
    flags = " ".join(_string_list(source.get("policy_flags"))).lower()
    if any(token in flags for token in ("paywall", "login_required", "subscription_required")):
        return True
    raw = source.get("raw_provider_metadata")
    if isinstance(raw, Mapping):
        return raw.get("paywalled") is True or raw.get("is_paywalled") is True
    return False


def _http_url_or_none(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    return None


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer():
        item = int(value)
        return item if item > 0 else None
    if isinstance(value, str) and value.strip():
        try:
            item = int(float(value.strip()))
        except ValueError:
            return None
        return item if item > 0 else None
    return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return []
    result: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _dedupe_int(values: Sequence[int]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        if value > 0 and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_") or "pdf"
