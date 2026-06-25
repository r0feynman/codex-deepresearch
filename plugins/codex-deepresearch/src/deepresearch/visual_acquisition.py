"""Visual candidate acquisition for local/test and configured real provider runs."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from .browser_screenshot import (
    BrowserScreenshotTransport,
    PlaywrightBrowserTransport,
    collect_browser_screenshot_candidates,
)
from .cache_keys import normalize_url
from .evidence_schema import EVIDENCE_SCHEMA_VERSION, SEARCH_ROUTES, validate_artifacts
from .search_handoff import resolve_run_dir
from .visual_artifacts import (
    IMAGE_FETCH_STATUS_FILENAME,
    VISUAL_ARTIFACT_SCHEMA_VERSION,
    VISUAL_PROVIDER_STATUS_FILENAME,
    VISUAL_PROVIDER_STATUS_SCHEMA_VERSION,
    VISUAL_SEARCH_PLAN_FILENAME,
    validate_visual_artifacts,
)


VISUAL_ACQUISITION_SCHEMA_VERSION = "codex-deepresearch.visual-acquisition.v0"
DEFAULT_VISUAL_PROVIDERS = (
    "local-page",
    "local-image-fixture",
    "local-screenshot-fixture",
)
BRAVE_IMAGE_PROVIDER = "brave-image-search"
BROWSER_SCREENSHOT_PROVIDER = "browser-screenshot"
BRAVE_IMAGE_ENDPOINT = "https://api.search.brave.com/res/v1/images/search"
BRAVE_DEFAULT_SEARCH_LANG = "en"
BRAVE_DEFAULT_COUNTRY = "US"
BRAVE_DEFAULT_SAFESEARCH = "strict"
BRAVE_DEFAULT_TIMEOUT_SECONDS = 20.0
BRAVE_DEFAULT_ESTIMATED_COST_USD = 0.005
BRAVE_DEFAULT_USER_AGENT = "codex-deepresearch/0.1"
BRAVE_STORAGE_CONFIRMATION_ENV = "CODEX_DEEPRESEARCH_BRAVE_ALLOW_RESULT_STORAGE"
REDACTED_SECRET = "[REDACTED]"
SECRET_KEY_PATTERN = re.compile(
    r"(?i)(api[_-]?key|subscription[-_]?token|authorization|bearer|token|secret|password)"
)
SECRET_JSON_FIELD_PATTERN = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|subscription[-_]?token|authorization|bearer|token|secret|password)"
    r"[\"']?\s*:\s*[\"'])([^\"']+)([\"'])"
)
SECRET_FIELD_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|subscription[-_]?token|authorization|bearer|token|secret|password)"
    r"(\s*[:=]\s*)([^\s,;}\]\)]+)"
)
TRUE_CONFIG_VALUES = {"1", "true", "yes", "on"}
FALSE_CONFIG_VALUES = {"0", "false", "no", "off"}
SCREENSHOT_MODES = ("first_viewport", "full_page", "scroll", "interaction")
DEFAULT_MAX_IMAGE_BYTES = 1_000_000
SUPPORTED_MIME_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00\xef\xbf\xa7\xdb"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class VisualAcquisitionError(ValueError):
    """Raised when visual acquisition cannot continue."""


@dataclass(frozen=True)
class _VisualContext:
    run_dir: Path
    evidence: Mapping[str, Any]
    routes: tuple[Mapping[str, Any], ...]
    visual_task_by_angle: Mapping[str, Mapping[str, Any]]
    source_by_id: Mapping[str, Mapping[str, Any]]
    created_at: str
    screenshot_modes: tuple[str, ...]


class _VisualProvider(Protocol):
    name: str

    def collect(self, context: _VisualContext) -> list[dict[str, Any]]:
        """Return unvalidated visual candidates."""


@dataclass(frozen=True)
class _BraveImageSearchConfig:
    api_key: str | None
    endpoint: str
    count: int
    country: str
    search_lang: str
    safesearch: str
    timeout_seconds: float
    estimated_cost_usd: float
    actual_cost_usd: float
    user_agent: str
    allow_result_storage: bool


@dataclass(frozen=True)
class _BraveImageSearchResponse:
    status_code: int
    payload: Mapping[str, Any]
    headers: Mapping[str, str]
    elapsed_ms: int


class _BraveImageSearchTransport(Protocol):
    def fetch(
        self,
        *,
        endpoint: str,
        api_key: str,
        params: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> _BraveImageSearchResponse:
        """Fetch one provider response."""


def acquire_visual_candidates(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
    providers: Sequence[str] | None = None,
    screenshot_modes: Sequence[str] | None = None,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    real_image_search_transport: _BraveImageSearchTransport | None = None,
    real_image_search_config: Mapping[str, Any] | None = None,
    browser_transport: BrowserScreenshotTransport | None = None,
) -> dict[str, Any]:
    """Collect visual candidates for visual routes.

    The default local providers remain deterministic and never call live web,
    OCR services, VLMs, or external APIs. Selecting a configured provider such
    as ``brave-image-search`` or ``browser-screenshot`` uses the provider
    adapter but still keeps image fetch, VLM analysis, and verifier linkage as
    separate stages.
    """

    if max_image_bytes < 1:
        raise VisualAcquisitionError("max_image_bytes must be positive")

    run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    evidence_path = run_dir / "evidence.json"
    if not evidence_path.exists():
        raise VisualAcquisitionError(f"missing evidence.json in run directory: {run_dir}")

    evidence = _read_json(evidence_path)
    visual_task_by_angle = _visual_task_by_angle(run_dir)
    routes = _visual_routes(evidence, visual_task_by_angle=visual_task_by_angle)
    now = _utc_now()
    provider_names = _normalize_provider_names(providers)
    normalized_modes = _normalize_screenshot_modes(screenshot_modes)

    if not routes:
        status = _write_text_only_skip(
            run_dir,
            evidence=evidence,
            evidence_path=evidence_path,
            provider_names=provider_names,
            screenshot_modes=normalized_modes,
            created_at=now,
        )
        return status

    provider_instances = _providers(
        provider_names,
        real_image_search_transport=real_image_search_transport,
        real_image_search_config=real_image_search_config,
        browser_transport=browser_transport,
        evidence=evidence,
    )
    real_provider_requested = any(_is_real_provider_name(provider.name) for provider in provider_instances)
    active_provider_names = provider_names
    if real_provider_requested:
        provider_instances = [
            provider for provider in provider_instances if _is_real_provider_name(provider.name)
        ]
        active_provider_names = tuple(provider.name for provider in provider_instances)
        preflight_statuses = [_initial_provider_status(provider) for provider in provider_instances]
        if not any(
            status.get("configured") is True and status.get("available") is True
            for status in preflight_statuses
        ):
            return _write_blocked_missing_visual_provider(
                run_dir,
                evidence=evidence,
                evidence_path=evidence_path,
                routes=routes,
                provider_names=active_provider_names,
                provider_statuses=preflight_statuses,
                created_at=now,
            )

    source_by_id = _source_by_id(evidence)
    if not real_provider_requested and _uses_fixture_sources(active_provider_names):
        source_by_id = _ensure_fixture_sources(run_dir, evidence, created_at=now)
    context = _VisualContext(
        run_dir=run_dir,
        evidence=evidence,
        routes=tuple(routes),
        visual_task_by_angle=visual_task_by_angle,
        source_by_id=source_by_id,
        created_at=now,
        screenshot_modes=normalized_modes,
    )

    candidates: list[dict[str, Any]] = []
    provider_statuses: list[dict[str, Any]] = []
    for provider in provider_instances:
        provider_candidates = provider.collect(context)
        candidates.extend(provider_candidates)
        provider_statuses.append(_collection_provider_status(provider, provider_candidates))

    if real_provider_requested and not any(
        status.get("available") is True for status in provider_statuses
    ):
        return _write_blocked_missing_visual_provider(
            run_dir,
            evidence=evidence,
            evidence_path=evidence_path,
            routes=routes,
            provider_names=active_provider_names,
            provider_statuses=provider_statuses,
            created_at=now,
        )

    if real_provider_requested:
        selected = []
        metadata_candidates, artifact_candidates = _split_real_candidate_records(candidates)
        candidate_records = _normalize_real_candidate_records(
            candidates=metadata_candidates,
            evidence=evidence,
            run_dir=run_dir,
            created_at=now,
        )
        if artifact_candidates:
            artifact_selected, artifact_records, near_duplicate_groups = (
                _validate_and_select_candidates(
                    run_dir=run_dir,
                    evidence=evidence,
                    candidates=artifact_candidates,
                    max_image_bytes=max_image_bytes,
                    created_at=now,
                )
            )
            selected.extend(artifact_selected)
            candidate_records.extend(artifact_records)
        else:
            near_duplicate_groups = []
    else:
        selected, candidate_records, near_duplicate_groups = _validate_and_select_candidates(
            run_dir=run_dir,
            evidence=evidence,
            candidates=candidates,
            max_image_bytes=max_image_bytes,
            created_at=now,
        )

    visual_candidates_path = run_dir / "visual_candidates.jsonl"
    visual_search_plan_path = run_dir / VISUAL_SEARCH_PLAN_FILENAME
    image_fetch_status_path = run_dir / IMAGE_FETCH_STATUS_FILENAME
    visual_provider_status_path = run_dir / VISUAL_PROVIDER_STATUS_FILENAME
    visual_observations_path = run_dir / "visual_observations.jsonl"
    image_fetch_records = _image_fetch_records_from_candidates(candidate_records)
    visual_search_plan = _visual_search_plan(
        run_dir=run_dir,
        evidence=evidence,
        routes=routes,
        provider_names=active_provider_names,
        created_at=now,
        selected_observations=len(selected),
        state="completed",
    )
    visual_status = (
        "real_image_search_candidates_collected"
        if real_provider_requested and candidate_records
        else "partial_auto_visual"
        if real_provider_requested
        else "fixture_visual_provider"
    )
    visual_ok = not real_provider_requested or bool(candidate_records)
    visual_terminal = real_provider_requested and not candidate_records
    metric_classification = (
        "real_provider_candidate_discovery"
        if real_provider_requested and candidate_records
        else "included_failure"
        if real_provider_requested
        else "fixture_only_not_release_eligible"
    )
    actionable_cause = (
        "configured real image search provider returned normalized candidates"
        if real_provider_requested and candidate_records
        else "configured real image search provider returned no image candidates"
        if real_provider_requested
        else (
            "deterministic fixture/manual visual providers validate mechanics; "
            "records are excluded from real automatic visual release counts"
        )
    )
    visual_provider_status = _visual_provider_status(
        run_dir=run_dir,
        status=visual_status,
        ok=visual_ok,
        terminal=visual_terminal,
        metric_classification=metric_classification,
        provider_names=active_provider_names,
        provider_statuses=provider_statuses,
        candidate_records=candidate_records,
        image_fetch_records=image_fetch_records,
        observations=selected,
        created_at=now,
        actionable_cause=actionable_cause,
    )
    _write_json(visual_search_plan_path, visual_search_plan)
    _write_jsonl(visual_candidates_path, candidate_records)
    _write_jsonl(image_fetch_status_path, image_fetch_records)
    _write_jsonl(visual_observations_path, selected)
    _write_json(visual_provider_status_path, visual_provider_status)

    candidate_counts = _candidate_counts(candidate_records)
    removal_counts = _removal_counts(candidate_records)
    screenshot_capture = _screenshot_capture_summary(candidate_records, provider_statuses)
    route_summary = _route_summary(routes)
    evidence["visual_acquisition"] = {
        "schema_version": VISUAL_ACQUISITION_SCHEMA_VERSION,
        "status": (
            "real_image_search_candidates_collected"
            if real_provider_requested and candidate_records
            else "partial_auto_visual"
            if real_provider_requested
            else "visual_candidates_collected"
        ),
        "created_at": now,
        "providers": provider_statuses,
        "routes": route_summary,
        "candidate_records_path": "visual_candidates.jsonl",
        "visual_search_plan_path": VISUAL_SEARCH_PLAN_FILENAME,
        "image_fetch_status_path": IMAGE_FETCH_STATUS_FILENAME,
        "visual_observations_path": "visual_observations.jsonl",
        "visual_provider_status_path": VISUAL_PROVIDER_STATUS_FILENAME,
        "candidate_counts": candidate_counts,
        "removal_counts": removal_counts,
        "near_duplicate_groups": near_duplicate_groups,
        "screenshot_capture": screenshot_capture,
        "validation": {
            "max_image_bytes": max_image_bytes,
            "mime_types": list(SUPPORTED_MIME_TYPES),
            "url_duplicate_check": True,
            "content_hash_check": True,
            "near_duplicate_check": True,
        },
        "image_search_invocations": _image_search_invocations(provider_statuses),
        "screenshot_capture_requests": len(screenshot_capture["requests"]),
        "ocr_records": len(
            [
                record
                for record in selected
                if isinstance(record.get("ocr_text"), str) and record["ocr_text"]
            ]
        ),
        "external_network_call": any(
            bool(status.get("external_network_call")) for status in provider_statuses
        ),
        "external_ocr_call": False,
        "external_vlm_call": False,
    }
    handoff = evidence.get("handoff")
    if isinstance(handoff, dict):
        handoff["visual_observations_path"] = "visual_observations.jsonl"
        handoff["visual_candidates_path"] = "visual_candidates.jsonl"
        handoff["visual_search_plan_path"] = VISUAL_SEARCH_PLAN_FILENAME
        handoff["image_fetch_status_path"] = IMAGE_FETCH_STATUS_FILENAME
        handoff["visual_provider_status_path"] = VISUAL_PROVIDER_STATUS_FILENAME
        handoff["visual_status"] = "visual_candidates_collected"
    budget = evidence.get("budget")
    if isinstance(budget, dict):
        budget["images_selected"] = len(selected)
    _write_json(evidence_path, evidence)

    validation = validate_artifacts(
        evidence_path=evidence_path,
        visual_observations_path=visual_observations_path,
    )
    visual_artifact_validation = validate_visual_artifacts(
        run_dir=run_dir,
        visual_search_plan_path=visual_search_plan_path,
        visual_candidates_path=visual_candidates_path,
        image_fetch_status_path=image_fetch_status_path,
        visual_observations_path=visual_observations_path,
        visual_provider_status_path=visual_provider_status_path,
        evidence_path=None,
    )
    status_name = (
        "real_image_search_candidates_collected"
        if real_provider_requested and candidate_records
        else "partial_auto_visual"
        if real_provider_requested
        else "visual_candidates_collected"
    )
    status = _base_status(run_dir, evidence, status_name, now)
    status.update(
        {
            "providers": provider_statuses,
            "candidate_records": len(candidate_records),
            "selected_observations": len(selected),
            "candidate_counts": candidate_counts,
            "removal_counts": removal_counts,
            "near_duplicate_groups": near_duplicate_groups,
            "screenshot_capture": screenshot_capture,
            "validation": validation.to_dict(),
            "visual_artifact_validation": visual_artifact_validation.to_dict(),
            "image_search_invocations": evidence["visual_acquisition"][
                "image_search_invocations"
            ],
            "screenshot_capture_requests": evidence["visual_acquisition"][
                "screenshot_capture_requests"
            ],
            "ocr_records": evidence["visual_acquisition"]["ocr_records"],
            "external_network_call": any(
                bool(item.get("external_network_call")) for item in provider_statuses
            ),
            "external_ocr_call": False,
            "external_vlm_call": False,
            "artifacts": {
                "evidence": str(evidence_path),
                "visual_search_plan": str(visual_search_plan_path),
                "visual_candidates": str(visual_candidates_path),
                "image_fetch_status": str(image_fetch_status_path),
                "visual_observations": str(visual_observations_path),
                "visual_provider_status": str(visual_provider_status_path),
                "visual_acquisition_status": str(run_dir / "visual_acquisition_status.json"),
            },
        }
    )
    _write_json(run_dir / "visual_acquisition_status.json", status)
    return status


class _LocalPageProvider:
    name = "local-page"

    def collect(self, context: _VisualContext) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for source in context.source_by_id.values():
            if source.get("type") not in {"web", "image", "screenshot"}:
                continue
            html_path = _source_html_path(context.run_dir, source)
            if html_path is None:
                continue
            parser = _ImageHTMLParser()
            parser.feed(html_path.read_text(encoding="utf-8", errors="replace"))
            page_url = _string(source.get("url")) or html_path.resolve().as_uri()
            for index, image in enumerate(parser.open_graph_images, start=1):
                records.append(
                    _candidate(
                        provider=self.name,
                        source=source,
                        route=_source_route(context.routes, source),
                        candidate_class="open_graph_image",
                        origin="page_image",
                        image_url=urljoin(page_url, image.get("src", "")),
                        page_url=page_url,
                        width=_int_or_zero(image.get("width")),
                        height=_int_or_zero(image.get("height")),
                        alt_text=_string(image.get("alt")) or "Open Graph image",
                        local_artifact_path=f"images/{_safe_id(source['id'])}_og_{index}.png",
                        content_seed=f"{source['id']}:og:{index}",
                        phash=f"og_{_safe_id(source['id'])}_{index}",
                    )
                )
            for index, image in enumerate(parser.body_images, start=1):
                attrs = image.get("attrs", {})
                records.append(
                    _candidate(
                        provider=self.name,
                        source=source,
                        route=_source_route(context.routes, source),
                        candidate_class="body_image",
                        origin="page_image",
                        image_url=urljoin(page_url, image.get("src", "")),
                        page_url=page_url,
                        width=_int_or_zero(image.get("width")),
                        height=_int_or_zero(image.get("height")),
                        alt_text=_string(image.get("alt")),
                        local_artifact_path=f"images/{_safe_id(source['id'])}_body_{index}.png",
                        content_seed=f"{source['id']}:body:{index}",
                        phash=_string(image.get("phash")) or f"body_{_safe_id(source['id'])}_{index}",
                        raw_provider_metadata={"html_attrs": attrs},
                    )
                )
            for index, icon in enumerate(parser.icon_links, start=1):
                records.append(
                    _candidate(
                        provider=self.name,
                        source=source,
                        route=_source_route(context.routes, source),
                        candidate_class="body_image",
                        origin="page_image",
                        image_url=urljoin(page_url, icon.get("src", "")),
                        page_url=page_url,
                        width=16,
                        height=16,
                        alt_text="favicon",
                        local_artifact_path=f"images/{_safe_id(source['id'])}_favicon_{index}.png",
                        content_seed=f"{source['id']}:favicon:{index}",
                        phash=f"favicon_{_safe_id(source['id'])}",
                        raw_provider_metadata={"link_rel": icon.get("rel")},
                    )
                )
        return records


class _LocalImageFixtureProvider:
    name = "local-image-fixture"

    def collect(self, context: _VisualContext) -> list[dict[str, Any]]:
        source = _fixture_image_source(context.source_by_id)
        route = _first_route(context.routes)
        records: list[dict[str, Any]] = []
        for index in range(1, 11):
            suffix = "duplicate-url" if index == 10 else f"result-{index}"
            image_url = f"https://example.com/local-image-fixture/{suffix}.png"
            if index == 9:
                image_url = "https://example.com/local-image-fixture/result-8.png"
            records.append(
                _candidate(
                    provider=self.name,
                    source=source,
                    route=route,
                    candidate_class="image_search",
                    origin="image_search",
                    image_url=image_url,
                    page_url=f"https://example.com/local-image-fixture/page-{index}",
                    width=800 + index,
                    height=450 + index,
                    alt_text=f"Fixture image search result {index}",
                    local_artifact_path=f"images/fixture_search_{index:02d}.png",
                    content_seed="fixture-search:8" if index == 10 else f"fixture-search:{index}",
                    phash="fixture-near-duplicate" if index in {6, 7} else f"fixture-search-{index}",
                    ocr_text="Fixture OCR text from search result 3" if index == 3 else None,
                    vlm_visual_summary=f"Fixture visual summary for search result {index}",
                    vlm_visual_description=(
                        f"Deterministic local fixture image search candidate {index}."
                    ),
                )
            )
        records.append(
            _candidate(
                provider=self.name,
                source=source,
                route=route,
                candidate_class="image_search",
                origin="image_search",
                image_url="https://example.com/local-image-fixture/tracking-pixel.gif",
                page_url="https://example.com/local-image-fixture/tracking",
                width=1,
                height=1,
                alt_text="tracking pixel",
                local_artifact_path="images/fixture_tracking_pixel.gif",
                content_seed="fixture-search:tracking",
                mime_type="image/gif",
                phash="fixture-tracking",
            )
        )
        return records


class _LocalScreenshotFixtureProvider:
    name = "local-screenshot-fixture"
    supported_modes = {
        "first_viewport": True,
        "full_page": True,
        "scroll": False,
        "interaction": False,
    }

    def collect(self, context: _VisualContext) -> list[dict[str, Any]]:
        source = _fixture_page_source(context.source_by_id)
        route = _first_route(context.routes)
        records: list[dict[str, Any]] = []
        for mode in context.screenshot_modes:
            supported = self.supported_modes[mode]
            metadata = {
                "mode": mode,
                "supported": supported,
                "provider": self.name,
                "unsupported_reason": None
                if supported
                else f"{self.name} does not support {mode} screenshot capture",
            }
            if not supported:
                records.append(
                    {
                        "id": f"cand_screenshot_{mode}",
                        "provider": self.name,
                        "candidate_class": "screenshot",
                        "origin": "screenshot",
                        "source_id": source["id"],
                        "page_url": source["url"],
                        "image_url": None,
                        "local_artifact_path": f"screenshots/{mode}.png",
                        "mime_type": "image/png",
                        "width": 0,
                        "height": 0,
                        "route": route["modality"],
                        "angle_id": route["id"],
                        "visual_tasks": list(route.get("visual_tasks", [])),
                        "screenshot": metadata,
                        "status": "removed",
                        "removal_reasons": ["unsupported_screenshot_mode"],
                    }
                )
                continue
            records.append(
                _candidate(
                    provider=self.name,
                    source=source,
                    route=route,
                    candidate_class="screenshot",
                    origin="screenshot",
                    image_url=None,
                    page_url=source["url"],
                    width=1280 if mode == "first_viewport" else 1280,
                    height=720 if mode == "first_viewport" else 2200,
                    alt_text=f"{mode} screenshot",
                    local_artifact_path=f"screenshots/{mode}.png",
                    content_seed=f"screenshot:{mode}",
                    phash=f"screenshot-{mode}",
                    screenshot=metadata,
                    vlm_visual_summary=f"Fixture {mode} screenshot summary",
                    vlm_visual_description=f"Deterministic {mode} screenshot capture artifact.",
                )
            )
        return records


class _BrowserScreenshotProvider:
    name = BROWSER_SCREENSHOT_PROVIDER

    def __init__(self, transport: BrowserScreenshotTransport | None = None) -> None:
        self._transport = transport
        self.last_status: dict[str, Any] = _default_provider_status(
            self.name,
            candidates=0,
            invoked=False,
        )

    def initial_status(self) -> dict[str, Any]:
        active_transport = self._transport or PlaywrightBrowserTransport()
        available, unavailable_reason = active_transport.availability()
        return {
            "provider": self.name,
            "provider_kind": "screenshot",
            "provider_mode": _browser_transport_provider_mode(active_transport),
            "provider_run_id": None,
            "configured": True,
            "available": available,
            "blocked_reason": unavailable_reason,
            "invoked": False,
            "invocations": 0,
            "candidates": 0,
            "candidates_discovered": 0,
            "artifacts_fetched": 0,
            "vlm_images_analyzed": 0,
            "estimated_cost_usd": 0.0,
            "actual_cost_usd": 0.0,
            "last_error": unavailable_reason,
            "external_network_call": False,
            "external_vlm_call": False,
            "transport": active_transport.name,
            "diagnostics": {"transport": active_transport.name},
        }

    def collect(self, context: _VisualContext) -> list[dict[str, Any]]:
        collection = collect_browser_screenshot_candidates(
            run_dir=context.run_dir,
            evidence=context.evidence,
            sources=tuple(context.source_by_id.values()),
            routes=context.routes,
            screenshot_modes=context.screenshot_modes,
            created_at=context.created_at,
            transport=self._transport,
            provider=self.name,
        )
        self.last_status = collection.provider_status
        return collection.candidates


class _HttpBraveImageSearchTransport:
    def fetch(
        self,
        *,
        endpoint: str,
        api_key: str,
        params: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> _BraveImageSearchResponse:
        started = time.monotonic()
        query = urlencode({key: value for key, value in params.items() if value is not None})
        request = Request(
            f"{endpoint}?{query}",
            headers={**headers, "X-Subscription-Token": api_key},
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
                payload = _loads_provider_payload(body)
                return _BraveImageSearchResponse(
                    status_code=int(getattr(response, "status", 200)),
                    payload=payload,
                    headers=dict(response.headers.items()),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return _BraveImageSearchResponse(
                status_code=int(exc.code),
                payload=_loads_provider_payload(body),
                headers=dict(exc.headers.items()) if exc.headers is not None else {},
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )


class _BraveImageSearchProvider:
    name = BRAVE_IMAGE_PROVIDER

    def __init__(
        self,
        *,
        config: _BraveImageSearchConfig,
        transport: _BraveImageSearchTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or _HttpBraveImageSearchTransport()
        self.last_status = self.initial_status()

    def initial_status(self) -> dict[str, Any]:
        configured = bool(self.config.api_key)
        storage_confirmed = self.config.allow_result_storage
        if not configured:
            blocked_reason = "missing_brave_search_api_key"
        elif not storage_confirmed:
            blocked_reason = "brave_result_storage_not_confirmed"
        else:
            blocked_reason = None
        return {
            "provider": self.name,
            "provider_kind": "web_image_search",
            "provider_mode": "real",
            "configured": configured,
            "available": configured and storage_confirmed,
            "blocked_reason": blocked_reason,
            "invoked": False,
            "invocations": 0,
            "candidates": 0,
            "candidates_discovered": 0,
            "artifacts_fetched": 0,
            "vlm_images_analyzed": 0,
            "estimated_cost_usd": 0.0,
            "actual_cost_usd": 0.0,
            "last_error": None,
            "external_network_call": False,
            "external_vlm_call": False,
            "diagnostics": {
                "endpoint": self.config.endpoint,
                "config_keys": _brave_config_keys(
                    configured=configured,
                    storage_confirmed=storage_confirmed,
                ),
                "count": self.config.count,
                "country": self.config.country,
                "search_lang": self.config.search_lang,
                "safesearch": self.config.safesearch,
                "result_storage_confirmed": storage_confirmed,
                "storage_confirmation_env": BRAVE_STORAGE_CONFIRMATION_ENV,
                "storage_policy": "persisted result metadata requires explicit plan/terms confirmation",
            },
        }

    def collect(self, context: _VisualContext) -> list[dict[str, Any]]:
        if not self.config.api_key or not self.config.allow_result_storage:
            self.last_status = self.initial_status()
            return []

        secrets = _provider_secret_values(self.config)
        candidates: list[dict[str, Any]] = []
        invocations = 0
        last_error: str | None = None
        blocked_reason: str | None = None
        successful_invocations = 0
        failed_invocations = 0
        diagnostics_by_query: list[dict[str, Any]] = []
        for route in context.routes:
            query = _query_for_route(context, route)
            if not query:
                continue
            invocations += 1
            params = {
                "q": query[:400],
                "count": self.config.count,
                "country": self.config.country,
                "search_lang": self.config.search_lang,
                "safesearch": self.config.safesearch,
            }
            try:
                response = self.transport.fetch(
                    endpoint=self.config.endpoint,
                    api_key=self.config.api_key,
                    params=params,
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                        "User-Agent": self.config.user_agent,
                    },
                    timeout_seconds=self.config.timeout_seconds,
                )
            except (OSError, TimeoutError, URLError) as exc:
                failed_invocations += 1
                blocked_reason = blocked_reason or "provider_request_failed"
                last_error = _redact_provider_text(
                    str(exc) or exc.__class__.__name__,
                    secrets=secrets,
                )
                diagnostics_by_query.append(
                    _provider_query_diagnostics(
                        query=query,
                        params=params,
                        status_code=None,
                        headers={},
                        elapsed_ms=None,
                        error=last_error,
                    )
                )
                continue
            query_diagnostics = _provider_query_diagnostics(
                query=query,
                params=params,
                status_code=response.status_code,
                headers=response.headers,
                elapsed_ms=response.elapsed_ms,
                error=None,
            )
            diagnostics_by_query.append(query_diagnostics)
            if response.status_code != 200:
                failed_invocations += 1
                reason = _blocked_reason_for_http_status(response.status_code)
                blocked_reason = blocked_reason or reason
                last_error = _redact_provider_text(
                    _provider_error_message(response.payload, response.status_code),
                    secrets=secrets,
                )
                query_diagnostics["error"] = last_error
                continue
            results = response.payload.get("results")
            if not isinstance(results, list):
                failed_invocations += 1
                blocked_reason = blocked_reason or "provider_response_invalid"
                last_error = "provider response did not include results[]"
                query_diagnostics["error"] = last_error
                continue
            provider_run_id = f"{self.name}:{context.run_dir.name}:{invocations}"
            route_candidates = self._candidates_from_results(
                results=results,
                route=route,
                query=query,
                provider_run_id=provider_run_id,
                provider_diagnostics=query_diagnostics,
                created_at=context.created_at,
            )
            successful_invocations += 1
            candidates.extend(route_candidates)

        estimated_cost = invocations * self.config.estimated_cost_usd
        actual_cost = invocations * self.config.actual_cost_usd
        available = successful_invocations > 0
        self.last_status = {
            "provider": self.name,
            "provider_kind": "web_image_search",
            "provider_mode": "real",
            "configured": True,
            "available": available,
            "blocked_reason": None if available else blocked_reason,
            "invoked": invocations > 0,
            "invocations": invocations,
            "candidates": len(candidates),
            "candidates_discovered": len(candidates),
            "artifacts_fetched": 0,
            "vlm_images_analyzed": 0,
            "estimated_cost_usd": estimated_cost,
            "actual_cost_usd": actual_cost,
            "last_error": last_error,
            "external_network_call": invocations > 0,
            "external_vlm_call": False,
            "diagnostics": {
                "endpoint": self.config.endpoint,
                "config_keys": _brave_config_keys(
                    configured=True,
                    storage_confirmed=self.config.allow_result_storage,
                ),
                "queries": diagnostics_by_query,
                "result_count": len(candidates),
                "successful_invocations": successful_invocations,
                "failed_invocations": failed_invocations,
                "partial_failure": successful_invocations > 0 and failed_invocations > 0,
                "rate_limited": any(
                    item.get("http_status") == 429 for item in diagnostics_by_query
                ),
                "auth_failed": any(
                    item.get("http_status") in {401, 403} for item in diagnostics_by_query
                ),
                "result_storage_confirmed": self.config.allow_result_storage,
                "storage_confirmation_env": BRAVE_STORAGE_CONFIRMATION_ENV,
            },
        }
        _assign_real_candidate_costs(
            candidates,
            estimated_cost_usd=estimated_cost,
            actual_cost_usd=actual_cost,
        )
        return candidates

    def _candidates_from_results(
        self,
        *,
        results: Sequence[Any],
        route: Mapping[str, Any],
        query: str,
        provider_run_id: str,
        provider_diagnostics: Mapping[str, Any],
        created_at: str,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for index, result in enumerate(results, start=1):
            if not isinstance(result, Mapping):
                continue
            image_url = _result_image_url(result)
            page_url = _result_page_url(result)
            if not image_url or not page_url:
                continue
            task_id = _string(route.get("task_id")) or _task_id_for_angle(route.get("id"))
            candidate_id = "cand_" + _safe_id(
                f"{self.name}_{task_id}_{index}_{hashlib.sha256(image_url.encode('utf-8')).hexdigest()[:12]}"
            )
            width, height = _result_dimensions(result)
            score = _score_from_rank(index)
            provider_provenance = {
                "provider": self.name,
                "provider_kind": "web_image_search",
                "provider_mode": "real",
                "provider_run_id": provider_run_id,
                "fixture_only": False,
                "external_network_call": True,
                "external_vlm_call": False,
                "retrieved_at": created_at,
                "query": query,
                "result_rank": index,
            }
            record = {
                "id": candidate_id,
                "candidate_id": candidate_id,
                "plan_id": f"plan_{task_id}",
                "task_id": task_id,
                "provider": self.name,
                "provider_kind": "web_image_search",
                "provider_mode": "real",
                "provider_run_id": provider_run_id,
                "provider_provenance": provider_provenance,
                "route": route.get("modality"),
                "angle_id": route.get("id"),
                "candidate_class": "image_search",
                "origin": "image_search",
                "image_url": image_url,
                "page_url": page_url,
                "rank": index,
                "score": score,
                "width": width,
                "height": height,
                "alt_text": _result_title(result),
                "visual_tasks": list(route.get("visual_tasks", []))
                if isinstance(route.get("visual_tasks"), list)
                else [],
                "analysis_status": "skipped",
                "observations": [],
                "inferences": [],
                "policy_flags": [],
                "policy_decision": "allowed",
                "candidate_status": "ranked",
                "rejection_reason": None,
                "removal_reasons": [],
                "caveats": [],
                "estimated_cost_usd": 0.0,
                "actual_cost_usd": 0.0,
                "provider_diagnostics": {
                    "query": query,
                    "provider_http_status": provider_diagnostics.get("http_status"),
                    "rate_limit": provider_diagnostics.get("rate_limit", {}),
                },
            }
            if self.config.allow_result_storage:
                record["raw_provider_metadata"] = _sanitized_result_metadata(
                    result,
                    secrets=_provider_secret_values(self.config),
                )
            records.append(record)
        return records


class _ImageHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open_graph_images: list[dict[str, Any]] = []
        self.body_images: list[dict[str, Any]] = []
        self.icon_links: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name.lower(): value or "" for name, value in attrs}
        if tag.lower() == "meta":
            prop = (attr.get("property") or attr.get("name") or "").lower()
            if prop in {"og:image", "og:image:url", "twitter:image"} and attr.get("content"):
                self.open_graph_images.append(
                    {
                        "src": attr["content"],
                        "alt": attr.get("alt") or "Open Graph image",
                        "width": attr.get("width") or attr.get("og:image:width") or 1200,
                        "height": attr.get("height") or attr.get("og:image:height") or 630,
                    }
                )
        elif tag.lower() == "img" and attr.get("src"):
            self.body_images.append(
                {
                    "src": attr["src"],
                    "alt": attr.get("alt"),
                    "width": attr.get("width") or attr.get("data-width") or 0,
                    "height": attr.get("height") or attr.get("data-height") or 0,
                    "phash": attr.get("data-phash"),
                    "attrs": attr,
                }
            )
        elif tag.lower() == "link":
            rel = attr.get("rel", "").lower()
            if "icon" in rel and attr.get("href"):
                self.icon_links.append({"src": attr["href"], "rel": rel})


def _candidate(
    *,
    provider: str,
    source: Mapping[str, Any],
    route: Mapping[str, Any],
    candidate_class: str,
    origin: str,
    image_url: str | None,
    page_url: str | None,
    width: int,
    height: int,
    alt_text: str | None,
    local_artifact_path: str,
    content_seed: str,
    phash: str,
    mime_type: str = "image/png",
    screenshot: Mapping[str, Any] | None = None,
    raw_provider_metadata: Mapping[str, Any] | None = None,
    ocr_text: str | None = None,
    vlm_visual_summary: str | None = None,
    vlm_visual_description: str | None = None,
) -> dict[str, Any]:
    candidate_id = "cand_" + _safe_id(
        f"{provider}_{source.get('id')}_{candidate_class}_{local_artifact_path}"
    )
    task_id = _string(route.get("task_id")) or f"task_visual_{_safe_id(str(route.get('id') or 'angle_001'))}"
    provider_kind = _provider_kind(provider)
    provider_run_id = _string(source.get("run_id")) or _string(route.get("run_id")) or "fixture-local"
    record = {
        "id": candidate_id,
        "candidate_id": candidate_id,
        "plan_id": f"plan_{task_id}",
        "task_id": task_id,
        "provider": provider,
        "provider_kind": provider_kind,
        "provider_mode": "fixture",
        "provider_run_id": provider_run_id,
        "provider_provenance": {
            "provider": provider,
            "provider_kind": provider_kind,
            "provider_mode": "fixture",
            "provider_run_id": provider_run_id,
            "fixture_only": True,
            "external_network_call": False,
            "external_vlm_call": False,
        },
        "source_id": source["id"],
        "source_url": source.get("url"),
        "route": route["modality"],
        "angle_id": route["id"],
        "candidate_class": candidate_class,
        "origin": origin,
        "image_url": image_url,
        "page_url": page_url,
        "local_artifact_path": local_artifact_path,
        "mime_type": mime_type,
        "width": width,
        "height": height,
        "alt_text": alt_text,
        "content_seed": content_seed,
        "phash": phash,
        "visual_tasks": list(route.get("visual_tasks", [])),
        "analysis_provider": _string(source.get("analysis_provider")) or "codex-interactive",
        "analysis_status": "analyzed",
        "observations": [vlm_visual_summary] if vlm_visual_summary else [],
        "inferences": [],
        "vlm_visual_summary": vlm_visual_summary,
        "vlm_visual_description": vlm_visual_description,
        "ocr_text": ocr_text,
        "ocr_outputs": [{"text": ocr_text, "provider": provider}] if ocr_text else [],
        "policy_flags": [],
        "policy_decision": "allowed",
        "caveats": [],
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
    }
    if screenshot is not None:
        record["screenshot"] = dict(screenshot)
    if raw_provider_metadata is not None:
        record["raw_provider_metadata"] = dict(raw_provider_metadata)
    return record


def _validate_and_select_candidates(
    *,
    run_dir: Path,
    evidence: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    max_image_bytes: int,
    created_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    max_selected = _max_images(evidence)
    selected: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    seen_urls: dict[str, str] = {}
    seen_hashes: dict[str, str] = {}
    seen_phashes: dict[str, str] = {}
    near_duplicate_groups: dict[str, dict[str, Any]] = {}

    for index, raw_candidate in enumerate(candidates, start=1):
        record = dict(raw_candidate)
        record["rank"] = index
        record["acquired_at"] = created_at
        _apply_phase3_candidate_defaults(record, evidence=evidence, run_dir=run_dir)
        if "status" not in record:
            artifact = _materialize_candidate_artifact(run_dir, record)
            record["local_artifact_path"] = artifact.relative_to(run_dir).as_posix()
        else:
            artifact = run_dir / str(record.get("local_artifact_path", ""))

        mime_type = _validated_mime_type(record, artifact)
        size = _artifact_size(record, artifact)
        image_hash = _hash_artifact(record, artifact)
        record["mime_type"] = mime_type
        record["artifact_size_bytes"] = size
        record["hash"] = image_hash
        record["analysis_provider"] = str(evidence.get("vlm_provider", "codex-interactive"))
        record.setdefault("observations", [])
        record.setdefault("inferences", [])
        record.setdefault("visual_tasks", [])
        record.setdefault("policy_flags", [])
        record.setdefault("caveats", [])

        removal_reasons = list(_string_list(record.get("removal_reasons")))
        validation_checks = {
            "mime_type": {
                "status": "passed" if mime_type in SUPPORTED_MIME_TYPES else "failed",
                "value": mime_type,
                "allowed": list(SUPPORTED_MIME_TYPES),
            },
            "size_limit": {
                "status": "passed" if size <= max_image_bytes else "failed",
                "size_bytes": size,
                "limit_bytes": max_image_bytes,
            },
            "content_hash": {
                "status": "passed" if image_hash else "failed",
                "hash": image_hash,
                "duplicate": False,
                "duplicate_of": None,
                "reason": None if image_hash else "missing_content_hash",
            },
            "url_duplicate": {
                "status": "passed",
                "normalized_url": None,
                "duplicate_of": None,
            },
            "near_duplicate": {
                "status": "passed",
                "group_id": None,
                "duplicate_of": None,
            },
        }
        if validation_checks["mime_type"]["status"] == "failed":
            removal_reasons.append("unsupported_mime_type")
        if validation_checks["size_limit"]["status"] == "failed":
            removal_reasons.append("size_limit_exceeded")
        if validation_checks["content_hash"]["status"] == "failed":
            removal_reasons.append("missing_content_hash")

        normalized_url = _normalized_candidate_url(record)
        if normalized_url:
            validation_checks["url_duplicate"]["normalized_url"] = normalized_url
            previous = seen_urls.get(normalized_url)
            if previous is not None:
                validation_checks["url_duplicate"]["status"] = "failed"
                validation_checks["url_duplicate"]["duplicate_of"] = previous
                record["duplicate_of"] = previous
                removal_reasons.append("duplicate_image_url")
            else:
                seen_urls[normalized_url] = str(record["id"])

        previous_hash = seen_hashes.get(image_hash)
        if previous_hash is not None:
            validation_checks["content_hash"]["status"] = "failed"
            validation_checks["content_hash"]["duplicate"] = True
            validation_checks["content_hash"]["duplicate_of"] = previous_hash
            validation_checks["content_hash"]["reason"] = "duplicate_content_hash"
            record["duplicate_of"] = previous_hash
            removal_reasons.append("duplicate_content_hash")
        elif image_hash:
            seen_hashes[image_hash] = str(record["id"])

        phash = _string(record.get("phash"))
        if phash:
            previous_phash = seen_phashes.get(phash)
            if previous_phash is not None and "duplicate_content_hash" not in removal_reasons:
                group_id = "ndg_" + _safe_id(phash)
                validation_checks["near_duplicate"] = {
                    "status": "failed",
                    "group_id": group_id,
                    "duplicate_of": previous_phash,
                }
                record["near_duplicate_group_id"] = group_id
                record["near_duplicate_of"] = previous_phash
                removal_reasons.append("near_duplicate")
                group = near_duplicate_groups.setdefault(
                    group_id,
                    {
                        "id": group_id,
                        "phash": phash,
                        "kept": previous_phash,
                        "removed": [],
                    },
                )
                group["removed"].append(
                    {
                        "candidate_id": record["id"],
                        "reason": "near_duplicate",
                        "duplicate_of": previous_phash,
                    }
                )
            else:
                seen_phashes[phash] = str(record["id"])

        noise_reasons = _noise_reasons(record)
        if noise_reasons:
            removal_reasons.extend(noise_reasons)
        removal_reasons = _dedupe(removal_reasons)
        record["validation_checks"] = validation_checks

        if removal_reasons:
            record["status"] = "removed"
            record["removal_reasons"] = removal_reasons
            record["analysis_status"] = "skipped"
            policy_block_reasons = {
                "access_denied",
                "access_controlled",
                "captcha",
                "captcha_protected",
                "captcha_required",
                "copyright_restricted",
                "license_policy_blocked",
                "login_gated",
                "login_required",
                "paywall",
                "paywalled",
                "pii_detected",
                "policy_blocked",
                "policy_decision_blocked",
                "requires_login",
                "robots_blocked",
                "robots_disallowed",
            }
            if "budget_pruned" in removal_reasons:
                record["candidate_status"] = "budget_pruned"
                record["policy_decision"] = "budget_pruned"
            elif record.get("policy_decision") == "blocked" or (
                set(removal_reasons) & policy_block_reasons
            ):
                record["candidate_status"] = "policy_blocked"
                record["policy_decision"] = "blocked"
            elif record.get("candidate_status") == "fetch_failed":
                record["candidate_status"] = "fetch_failed"
            else:
                record["candidate_status"] = "rejected"
            record.setdefault("observations", [])
            record.setdefault("inferences", [])
        elif len(selected) >= max_selected:
            record["status"] = "removed"
            record["removal_reasons"] = ["budget_pruned"]
            record["analysis_status"] = "skipped"
            record["candidate_status"] = "budget_pruned"
            record["policy_decision"] = "budget_pruned"
        else:
            record["status"] = "accepted"
            record["removal_reasons"] = []
            if record.get("requires_vlm_observation") is True:
                record["candidate_status"] = "fetched"
                record["analysis_status"] = "skipped"
                record["supportable_evidence"] = False
                record.setdefault("caveats", []).append(
                    "requires_vlm_observation_and_verifier_linkage"
                )
            else:
                record["candidate_status"] = "analyzed"
                selected.append(_observation_from_candidate(record))
        record["rejection_reason"] = (
            record["removal_reasons"][0] if record.get("removal_reasons") else None
        )
        records.append(_persistable_candidate(record))

    return selected, records, list(near_duplicate_groups.values())


def _observation_from_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    image_id = _image_id_for_candidate_id(str(candidate["candidate_id"]))
    fetch_id = _fetch_id_for_candidate_id(str(candidate["candidate_id"]))
    observation = {
        "id": image_id,
        "observation_id": "obs_" + _safe_id(str(candidate["candidate_id"]).removeprefix("cand_")),
        "evidence_image_id": image_id,
        "source_id": candidate["source_id"],
        "origin": candidate["origin"],
        "image_url": candidate.get("image_url"),
        "page_url": candidate.get("page_url"),
        "local_artifact_path": candidate["local_artifact_path"],
        "mime_type": candidate["mime_type"],
        "artifact_size_bytes": candidate["artifact_size_bytes"],
        "width": candidate["width"],
        "height": candidate["height"],
        "hash": candidate["hash"],
        "phash": candidate.get("phash"),
        "ocr_text": candidate.get("ocr_text"),
        "ocr_outputs": list(candidate.get("ocr_outputs", []))
        if isinstance(candidate.get("ocr_outputs"), list)
        else [],
        "observations": list(candidate.get("observations", []))
        if isinstance(candidate.get("observations"), list)
        else [],
        "inferences": list(candidate.get("inferences", []))
        if isinstance(candidate.get("inferences"), list)
        else [],
        "vlm_visual_summary": candidate.get("vlm_visual_summary"),
        "vlm_visual_description": candidate.get("vlm_visual_description"),
        "visual_tasks": list(candidate.get("visual_tasks", []))
        if isinstance(candidate.get("visual_tasks"), list)
        else [],
        "analysis_provider": candidate["analysis_provider"],
        "analysis_status": candidate.get("analysis_status", "analyzed"),
        "policy_flags": list(candidate.get("policy_flags", []))
        if isinstance(candidate.get("policy_flags"), list)
        else [],
        "policy_decision": candidate.get("policy_decision", "allowed"),
        "caveats": list(candidate.get("caveats", []))
        if isinstance(candidate.get("caveats"), list)
        else [],
        "candidate_id": candidate["candidate_id"],
        "fetch_id": fetch_id,
        "task_id": candidate.get("task_id"),
        "candidate_class": candidate["candidate_class"],
        "angle_id": candidate.get("angle_id"),
        "route": candidate.get("route"),
        "visual_provider": candidate["provider"],
        "provider": candidate["provider"],
        "provider_kind": candidate.get("provider_kind"),
        "provider_mode": candidate.get("provider_mode"),
        "provider_run_id": candidate.get("provider_run_id"),
        "provider_provenance": dict(candidate.get("provider_provenance", {}))
        if isinstance(candidate.get("provider_provenance"), Mapping)
        else {},
        "model_or_tool": candidate["analysis_provider"],
        "observation_status": candidate.get("analysis_status", "analyzed"),
        "confidence": 1.0,
        "verifier_links": [],
        "report_links": [],
        "estimated_cost_usd": candidate.get("estimated_cost_usd", 0.0),
        "actual_cost_usd": candidate.get("actual_cost_usd", 0.0),
        "created_at": candidate.get("acquired_at"),
        "visual_validation": dict(candidate.get("validation_checks", {})),
        "source_url": candidate.get("source_url"),
    }
    if isinstance(candidate.get("screenshot"), Mapping):
        observation["screenshot"] = dict(candidate["screenshot"])
    return observation


def _persistable_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(candidate)
    record.pop("content_seed", None)
    return record


def _apply_phase3_candidate_defaults(
    record: dict[str, Any],
    *,
    evidence: Mapping[str, Any],
    run_dir: Path,
) -> None:
    candidate_id = _string(record.get("candidate_id")) or _string(record.get("id"))
    if not candidate_id:
        candidate_id = "cand_" + _safe_id(str(record.get("local_artifact_path") or "visual"))
        record["id"] = candidate_id
    record["candidate_id"] = candidate_id
    record.setdefault("id", candidate_id)
    task_id = _string(record.get("task_id")) or _task_id_for_angle(record.get("angle_id"))
    record["task_id"] = task_id
    record.setdefault("plan_id", f"plan_{task_id}")
    provider = _string(record.get("provider")) or "local-fixture"
    provider_kind = _string(record.get("provider_kind")) or _provider_kind(provider)
    provider_mode = _string(record.get("provider_mode")) or "fixture"
    provider_run_id = _string(record.get("provider_run_id")) or str(
        evidence.get("run_id") or run_dir.name
    )
    record["provider"] = provider
    record["provider_kind"] = provider_kind
    record["provider_mode"] = provider_mode
    record["provider_run_id"] = provider_run_id
    if not isinstance(record.get("provider_provenance"), Mapping):
        record["provider_provenance"] = {
            "provider": provider,
            "provider_kind": provider_kind,
            "provider_mode": provider_mode,
            "provider_run_id": provider_run_id,
            "fixture_only": provider_mode != "real",
            "external_network_call": False,
            "external_vlm_call": False,
        }
    record.setdefault("score", _score_from_rank(int(record.get("rank") or 0)))
    record.setdefault("candidate_status", "discovered")
    record.setdefault("policy_decision", "allowed")
    record.setdefault("policy_flags", [])
    record.setdefault("rejection_reason", None)
    record.setdefault("estimated_cost_usd", 0.0)
    record.setdefault("actual_cost_usd", 0.0)


def _normalize_real_candidate_records(
    *,
    candidates: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any],
    run_dir: Path,
    created_at: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_urls: dict[str, str] = {}
    for index, raw_candidate in enumerate(candidates, start=1):
        record = dict(raw_candidate)
        record["rank"] = int(record.get("rank") or index)
        record["score"] = float(record.get("score") or _score_from_rank(record["rank"]))
        record["acquired_at"] = created_at
        _apply_phase3_candidate_defaults(record, evidence=evidence, run_dir=run_dir)
        record.setdefault("candidate_class", "image_search")
        record.setdefault("origin", "image_search")
        record.setdefault("analysis_status", "skipped")
        record.setdefault("observations", [])
        record.setdefault("inferences", [])
        record.setdefault("visual_tasks", [])
        record.setdefault("policy_flags", [])
        record.setdefault("caveats", [])
        record.setdefault("removal_reasons", [])
        normalized_url = _normalized_candidate_url(record)
        if normalized_url:
            previous = seen_urls.get(normalized_url)
            if previous is not None:
                record["duplicate_of"] = previous
                record["candidate_status"] = "rejected"
                record["rejection_reason"] = "duplicate_image_url"
                record["removal_reasons"] = _dedupe(
                    [*_string_list(record.get("removal_reasons")), "duplicate_image_url"]
                )
            else:
                seen_urls[normalized_url] = str(record["candidate_id"])
        records.append(_persistable_candidate(record))
    return records


def _split_real_candidate_records(
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    metadata_candidates: list[Mapping[str, Any]] = []
    artifact_candidates: list[Mapping[str, Any]] = []
    for candidate in candidates:
        if _requires_local_artifact_validation(candidate):
            artifact_candidates.append(candidate)
        else:
            metadata_candidates.append(candidate)
    return metadata_candidates, artifact_candidates


def _requires_local_artifact_validation(candidate: Mapping[str, Any]) -> bool:
    return (
        candidate.get("provider") == BROWSER_SCREENSHOT_PROVIDER
        or candidate.get("provider_kind") == "screenshot"
        or candidate.get("origin") == "screenshot"
    )


def _image_fetch_records_from_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = str(candidate["candidate_id"])
        status = _fetch_status_for_candidate(candidate)
        fetched = status == "fetched"
        requires_vlm_observation = candidate.get("requires_vlm_observation") is True
        records.append(
            {
                "fetch_id": _fetch_id_for_candidate_id(candidate_id),
                "candidate_id": candidate_id,
                "task_id": candidate.get("task_id"),
                "angle_id": candidate.get("angle_id"),
                "source_search_result_id": candidate.get("source_search_result_id"),
                "provider": candidate.get("provider"),
                "provider_kind": candidate.get("provider_kind"),
                "provider_mode": candidate.get("provider_mode"),
                "provider_run_id": candidate.get("provider_run_id"),
                "provider_provenance": dict(candidate.get("provider_provenance", {}))
                if isinstance(candidate.get("provider_provenance"), Mapping)
                else {},
                "fetch_status": status,
                "http_status": candidate.get("http_status"),
                "mime_type": candidate.get("mime_type") if fetched else None,
                "byte_size": candidate.get("artifact_size_bytes") if fetched else None,
                "width": candidate.get("width") if fetched else None,
                "height": candidate.get("height") if fetched else None,
                "hash": candidate.get("hash") if fetched else None,
                "phash": candidate.get("phash") if fetched else None,
                "local_artifact_path": candidate.get("local_artifact_path") if fetched else None,
                "evidence_image_id": _image_id_for_candidate_id(candidate_id)
                if fetched and not requires_vlm_observation
                else None,
                "policy_decision": candidate.get("policy_decision", "allowed"),
                "policy_flags": list(candidate.get("policy_flags", []))
                if isinstance(candidate.get("policy_flags"), list)
                else [],
                "failure_code": None if fetched else _failure_code(candidate),
                "estimated_cost_usd": candidate.get("estimated_cost_usd", 0.0),
                "actual_cost_usd": candidate.get("actual_cost_usd", 0.0),
            }
        )
    return records


def _visual_search_plan(
    *,
    run_dir: Path,
    evidence: Mapping[str, Any],
    routes: Sequence[Mapping[str, Any]],
    provider_names: Sequence[str],
    created_at: str,
    selected_observations: int,
    state: str,
) -> dict[str, Any]:
    tasks = []
    target_evidence_type = (
        "screenshot"
        if provider_names and all(_provider_kind(provider) == "screenshot" for provider in provider_names)
        else "web_image"
    )
    for route in routes:
        task_id = _string(route.get("task_id")) or _task_id_for_angle(route.get("id"))
        max_images = int(route.get("max_images") or selected_observations or 0)
        tasks.append(
            {
                "plan_id": f"plan_{task_id}",
                "task_id": task_id,
                "angle_id": route.get("id"),
                "route": route.get("modality"),
                "target_evidence_type": target_evidence_type,
                "query": str(evidence.get("question") or ""),
                "providers": list(provider_names),
                "source_search_result_ids": [],
                "caps": {
                    "max_candidates": max(max_images, selected_observations),
                    "max_fetches": max_images,
                    "max_vlm_images": max_images,
                    "max_cost_usd": _budget_cost_cap(evidence),
                },
                "policy_constraints": {
                    "license_policy": "allowed",
                    "robots_policy": "allowed",
                    "policy_decision": "allowed",
                },
                "estimated_cost_usd": 0.0,
                "state": state,
            }
        )
    return {
        "schema_version": VISUAL_ARTIFACT_SCHEMA_VERSION,
        "run_id": str(evidence.get("run_id") or run_dir.name),
        "created_at": created_at,
        "tasks": tasks,
    }


def _visual_provider_status(
    *,
    run_dir: Path,
    status: str,
    ok: bool,
    terminal: bool,
    metric_classification: str,
    provider_names: Sequence[str],
    provider_statuses: Sequence[Mapping[str, Any]],
    candidate_records: Sequence[Mapping[str, Any]],
    image_fetch_records: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    created_at: str,
    actionable_cause: str,
) -> dict[str, Any]:
    status_by_provider = {
        str(item.get("provider")): item
        for item in provider_statuses
        if isinstance(item.get("provider"), str)
    }
    providers = []
    for provider in provider_names:
        provider_status = status_by_provider.get(provider, {})
        provider_kind = _string(provider_status.get("provider_kind")) or _provider_kind(provider)
        provider_mode = _string(provider_status.get("provider_mode")) or (
            "real" if _is_real_provider_name(provider) else "fixture"
        )
        provider_candidates = [
            item for item in candidate_records if item.get("provider") == provider
        ]
        provider_fetches = [
            item
            for item in image_fetch_records
            if item.get("provider") == provider and item.get("fetch_status") == "fetched"
        ]
        provider_observations = [
            item for item in observations if item.get("provider") == provider
        ]
        invoked = provider_status.get("invoked")
        providers.append(
            {
                "provider": provider,
                "provider_kind": provider_kind,
                "provider_mode": provider_mode,
                "configured": provider_status.get("configured") is not False,
                "available": provider_status.get("available") is not False,
                "blocked_reason": provider_status.get("blocked_reason"),
                "invocations": _int_or_zero(provider_status.get("invocations"))
                if "invocations" in provider_status
                else 0
                if invoked is False
                else 1,
                "candidates_discovered": _int_or_zero(
                    provider_status.get("candidates_discovered")
                )
                if "candidates_discovered" in provider_status
                else len(provider_candidates),
                "artifacts_fetched": _int_or_zero(provider_status.get("artifacts_fetched"))
                if "artifacts_fetched" in provider_status
                else len(provider_fetches),
                "vlm_images_analyzed": _int_or_zero(
                    provider_status.get("vlm_images_analyzed")
                )
                if "vlm_images_analyzed" in provider_status
                else len(provider_observations),
                "estimated_cost_usd": float(provider_status.get("estimated_cost_usd") or 0.0),
                "actual_cost_usd": float(provider_status.get("actual_cost_usd") or 0.0),
                "last_error": _redact_provider_text(
                    str(provider_status["last_error"]),
                    secrets=(),
                )
                if provider_status.get("last_error") is not None
                else None,
                "external_network_call": bool(provider_status.get("external_network_call")),
                "external_vlm_call": bool(provider_status.get("external_vlm_call")),
                "diagnostics": _redact_provider_value(
                    provider_status.get("diagnostics", {}),
                    secrets=(),
                ),
            }
        )
    return {
        "schema_version": VISUAL_PROVIDER_STATUS_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "status": status,
        "ok": ok,
        "terminal": terminal,
        "created_at": created_at,
        "metric_classification": metric_classification,
        "providers": providers,
        "diagnostics": {"actionable_cause": actionable_cause},
        "artifacts": {
            "visual_search_plan": str(run_dir / VISUAL_SEARCH_PLAN_FILENAME),
            "visual_candidates": str(run_dir / "visual_candidates.jsonl"),
            "image_fetch_status": str(run_dir / IMAGE_FETCH_STATUS_FILENAME),
            "visual_observations": str(run_dir / "visual_observations.jsonl"),
            "visual_provider_status": str(run_dir / VISUAL_PROVIDER_STATUS_FILENAME),
        },
    }


def _provider_kind(provider: str) -> str:
    if provider == BRAVE_IMAGE_PROVIDER:
        return "web_image_search"
    if provider == "local-page":
        return "page_extractor"
    if provider == "local-image-fixture":
        return "web_image_search"
    if provider == "local-screenshot-fixture":
        return "screenshot"
    if "screenshot" in provider:
        return "screenshot"
    if "pdf" in provider:
        return "pdf_rasterizer"
    if "manual" in provider:
        return "manual"
    if "fixture" in provider or provider.startswith("local-"):
        return "fixture"
    return "visual_acquisition"


def _task_id_for_angle(angle_id: Any) -> str:
    raw = _string(angle_id) or "angle_001"
    suffix = raw.removeprefix("angle_")
    return f"task_visual_{suffix}"


def _fetch_id_for_candidate_id(candidate_id: str) -> str:
    return "fetch_" + _safe_id(candidate_id.removeprefix("cand_"))


def _image_id_for_candidate_id(candidate_id: str) -> str:
    return "img_" + _safe_id(candidate_id.removeprefix("cand_"))


def _score_from_rank(rank: int) -> float:
    if rank < 1:
        return 0.0
    return round(1.0 / rank, 6)


def _fetch_status_for_candidate(candidate: Mapping[str, Any]) -> str:
    reasons = set(_string_list(candidate.get("removal_reasons")))
    policy_block_reasons = {
        "access_denied",
        "access_controlled",
        "captcha",
        "captcha_protected",
        "captcha_required",
        "copyright_restricted",
        "license_policy_blocked",
        "login_gated",
        "login_required",
        "paywall",
        "paywalled",
        "pii_detected",
        "policy_blocked",
        "policy_decision_blocked",
        "requires_login",
        "robots_blocked",
        "robots_disallowed",
    }
    if (
        candidate.get("policy_decision") == "blocked"
        or candidate.get("candidate_status") == "policy_blocked"
        or reasons & policy_block_reasons
    ):
        return "policy_blocked"
    if candidate.get("status") == "accepted":
        return "fetched"
    if candidate.get("candidate_status") == "fetch_failed":
        return "failed"
    if "budget_pruned" in reasons:
        return "budget_pruned"
    if "unsupported_mime_type" in reasons:
        return "unsupported_mime"
    if "size_limit_exceeded" in reasons:
        return "too_large"
    if reasons & {"duplicate_image_url", "duplicate_content_hash", "near_duplicate"}:
        return "deduped"
    if reasons & {"browser_transport_unavailable", "capture_failed", "retrieval_failed"}:
        return "failed"
    return "skipped"


def _failure_code(candidate: Mapping[str, Any]) -> str | None:
    reasons = _string_list(candidate.get("removal_reasons"))
    return reasons[0] if reasons else None


def _budget_cost_cap(evidence: Mapping[str, Any]) -> float:
    budget = evidence.get("budget")
    if isinstance(budget, Mapping):
        value = budget.get("max_cost_usd")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def _materialize_candidate_artifact(run_dir: Path, candidate: Mapping[str, Any]) -> Path:
    relative = Path(str(candidate["local_artifact_path"]))
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise VisualAcquisitionError(f"candidate artifact path must be run-relative: {relative}")
    artifact = (run_dir / relative).resolve(strict=False)
    try:
        artifact.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise VisualAcquisitionError(f"candidate artifact escapes run directory: {relative}") from exc
    artifact.parent.mkdir(parents=True, exist_ok=True)
    seed = str(candidate.get("content_seed") or candidate["id"]).encode("utf-8")
    artifact.write_bytes(PNG_1X1 + b"\n" + seed + b"\n")
    return artifact


def _validated_mime_type(record: Mapping[str, Any], artifact: Path) -> str:
    raw = _string(record.get("mime_type"))
    if artifact.is_file():
        content = artifact.read_bytes()[:16]
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if content.startswith(b"RIFF") and b"WEBP" in content:
            return "image/webp"
    if raw:
        return raw
    guessed, _ = mimetypes.guess_type(str(artifact))
    return guessed or "image/unknown"


def _artifact_size(record: Mapping[str, Any], artifact: Path) -> int:
    if artifact.is_file():
        return artifact.stat().st_size
    value = record.get("artifact_size_bytes") or record.get("content_length")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _hash_artifact(record: Mapping[str, Any], artifact: Path) -> str | None:
    value = _string(record.get("hash"))
    if value:
        return value if value.startswith("sha256:") else "sha256:" + value
    if artifact.is_file():
        return "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    return None


def _noise_reasons(record: Mapping[str, Any]) -> list[str]:
    haystack = " ".join(
        str(record.get(key) or "")
        for key in ("image_url", "page_url", "alt_text", "local_artifact_path")
    ).lower()
    width = _int_or_zero(record.get("width"))
    height = _int_or_zero(record.get("height"))
    reasons: list[str] = []
    if "favicon" in haystack or "apple-touch-icon" in haystack or "rel icon" in haystack:
        reasons.append("favicon")
    if "tracking" in haystack or (0 < width <= 2 and 0 < height <= 2):
        reasons.append("tracking_pixel")
    if "logo" in haystack:
        reasons.append("logo")
    if "thumbnail" in haystack or re.search(r"\bthumb\b", haystack):
        reasons.append("thumbnail")
    if "preview" in haystack or "placeholder" in haystack or "sprite" in haystack:
        reasons.append("low_value_preview")
    if record.get("candidate_class") == "body_image" and 0 < width < 64 and 0 < height < 64:
        reasons.append("low_value_preview")
    return _dedupe(reasons)


def _write_text_only_skip(
    run_dir: Path,
    *,
    evidence: dict[str, Any],
    evidence_path: Path,
    provider_names: Sequence[str],
    screenshot_modes: Sequence[str],
    created_at: str,
) -> dict[str, Any]:
    visual_candidates_path = run_dir / "visual_candidates.jsonl"
    visual_search_plan_path = run_dir / VISUAL_SEARCH_PLAN_FILENAME
    image_fetch_status_path = run_dir / IMAGE_FETCH_STATUS_FILENAME
    visual_provider_status_path = run_dir / VISUAL_PROVIDER_STATUS_FILENAME
    visual_observations_path = run_dir / "visual_observations.jsonl"
    visual_search_plan = {
        "schema_version": VISUAL_ARTIFACT_SCHEMA_VERSION,
        "run_id": str(evidence.get("run_id") or run_dir.name),
        "created_at": created_at,
        "tasks": [],
    }
    image_fetch_records: list[dict[str, Any]] = []
    visual_provider_status = _visual_provider_status(
        run_dir=run_dir,
        status="no_visual_tasks",
        ok=True,
        terminal=False,
        metric_classification="excluded_text_only",
        provider_names=provider_names,
        provider_statuses=[
            _default_provider_status(name, candidates=0, invoked=False)
            for name in provider_names
        ],
        candidate_records=[],
        image_fetch_records=image_fetch_records,
        observations=[],
        created_at=created_at,
        actionable_cause="text_only route has no visual tasks",
    )
    _write_json(visual_search_plan_path, visual_search_plan)
    _write_jsonl(visual_candidates_path, [])
    _write_jsonl(image_fetch_status_path, image_fetch_records)
    _write_jsonl(visual_observations_path, [])
    _write_json(visual_provider_status_path, visual_provider_status)
    evidence["visual_acquisition"] = {
        "schema_version": VISUAL_ACQUISITION_SCHEMA_VERSION,
        "status": "no_visual_tasks",
        "created_at": created_at,
        "providers": [
            _default_provider_status(name, candidates=0, invoked=False)
            for name in provider_names
        ],
        "screenshot_capture": {
            "requested_modes": list(screenshot_modes),
            "requests": [],
            "unsupported": [],
        },
        "candidate_counts": {},
        "removal_counts": {},
        "near_duplicate_groups": [],
        "image_search_invocations": 0,
        "screenshot_capture_requests": 0,
        "ocr_records": 0,
        "external_network_call": False,
        "external_ocr_call": False,
        "external_vlm_call": False,
        "reason": "text_only route has no visual tasks",
        "visual_search_plan_path": VISUAL_SEARCH_PLAN_FILENAME,
        "image_fetch_status_path": IMAGE_FETCH_STATUS_FILENAME,
        "visual_provider_status_path": VISUAL_PROVIDER_STATUS_FILENAME,
    }
    handoff = evidence.get("handoff")
    if isinstance(handoff, dict):
        handoff["visual_observations_path"] = "visual_observations.jsonl"
        handoff["visual_candidates_path"] = "visual_candidates.jsonl"
        handoff["visual_search_plan_path"] = VISUAL_SEARCH_PLAN_FILENAME
        handoff["image_fetch_status_path"] = IMAGE_FETCH_STATUS_FILENAME
        handoff["visual_provider_status_path"] = VISUAL_PROVIDER_STATUS_FILENAME
        handoff["visual_status"] = "no_visual_tasks"
    _write_json(evidence_path, evidence)
    validation = validate_artifacts(
        evidence_path=evidence_path,
        visual_observations_path=visual_observations_path,
    )
    visual_artifact_validation = validate_visual_artifacts(
        run_dir=run_dir,
        visual_search_plan_path=visual_search_plan_path,
        visual_candidates_path=visual_candidates_path,
        image_fetch_status_path=image_fetch_status_path,
        visual_observations_path=visual_observations_path,
        visual_provider_status_path=visual_provider_status_path,
        evidence_path=None,
    )
    status = _base_status(run_dir, evidence, "no_visual_tasks", created_at)
    status.update(
        {
            "providers": [
                _default_provider_status(name, candidates=0, invoked=False)
                for name in provider_names
            ],
            "candidate_records": 0,
            "selected_observations": 0,
            "candidate_counts": {},
            "removal_counts": {},
            "near_duplicate_groups": [],
            "screenshot_capture": evidence["visual_acquisition"]["screenshot_capture"],
            "image_search_invocations": 0,
            "screenshot_capture_requests": 0,
            "ocr_records": 0,
            "external_network_call": False,
            "external_ocr_call": False,
            "external_vlm_call": False,
            "validation": validation.to_dict(),
            "visual_artifact_validation": visual_artifact_validation.to_dict(),
            "artifacts": {
                "evidence": str(evidence_path),
                "visual_search_plan": str(visual_search_plan_path),
                "visual_candidates": str(visual_candidates_path),
                "image_fetch_status": str(image_fetch_status_path),
                "visual_observations": str(visual_observations_path),
                "visual_provider_status": str(visual_provider_status_path),
                "visual_acquisition_status": str(run_dir / "visual_acquisition_status.json"),
            },
        }
    )
    _write_json(run_dir / "visual_acquisition_status.json", status)
    return status


def _write_blocked_missing_visual_provider(
    run_dir: Path,
    *,
    evidence: dict[str, Any],
    evidence_path: Path,
    routes: Sequence[Mapping[str, Any]],
    provider_names: Sequence[str],
    provider_statuses: Sequence[Mapping[str, Any]],
    created_at: str,
) -> dict[str, Any]:
    visual_candidates_path = run_dir / "visual_candidates.jsonl"
    visual_search_plan_path = run_dir / VISUAL_SEARCH_PLAN_FILENAME
    image_fetch_status_path = run_dir / IMAGE_FETCH_STATUS_FILENAME
    visual_provider_status_path = run_dir / VISUAL_PROVIDER_STATUS_FILENAME
    visual_observations_path = run_dir / "visual_observations.jsonl"
    visual_search_plan = _visual_search_plan(
        run_dir=run_dir,
        evidence=evidence,
        routes=routes,
        provider_names=provider_names,
        created_at=created_at,
        selected_observations=0,
        state="blocked",
    )
    actionable_cause = _blocked_actionable_cause(provider_statuses)
    visual_provider_status = _visual_provider_status(
        run_dir=run_dir,
        status="blocked_missing_visual_provider",
        ok=False,
        terminal=True,
        metric_classification="excluded_blocked",
        provider_names=provider_names,
        provider_statuses=provider_statuses,
        candidate_records=[],
        image_fetch_records=[],
        observations=[],
        created_at=created_at,
        actionable_cause=actionable_cause,
    )
    _write_json(visual_search_plan_path, visual_search_plan)
    _write_jsonl(visual_candidates_path, [])
    _write_jsonl(image_fetch_status_path, [])
    _write_jsonl(visual_observations_path, [])
    _write_json(visual_provider_status_path, visual_provider_status)
    evidence["visual_acquisition"] = {
        "schema_version": VISUAL_ACQUISITION_SCHEMA_VERSION,
        "status": "blocked_missing_visual_provider",
        "created_at": created_at,
        "providers": [dict(status) for status in provider_statuses],
        "routes": _route_summary(routes),
        "candidate_records_path": "visual_candidates.jsonl",
        "visual_search_plan_path": VISUAL_SEARCH_PLAN_FILENAME,
        "image_fetch_status_path": IMAGE_FETCH_STATUS_FILENAME,
        "visual_observations_path": "visual_observations.jsonl",
        "visual_provider_status_path": VISUAL_PROVIDER_STATUS_FILENAME,
        "candidate_counts": {},
        "removal_counts": {},
        "near_duplicate_groups": [],
        "screenshot_capture": {
            "interface_modes": list(SCREENSHOT_MODES),
            "providers": [],
            "requests": [],
            "unsupported": [],
        },
        "image_search_invocations": sum(
            int(status.get("invocations") or 0) for status in provider_statuses
        ),
        "screenshot_capture_requests": 0,
        "ocr_records": 0,
        "external_network_call": any(
            bool(status.get("external_network_call")) for status in provider_statuses
        ),
        "external_ocr_call": False,
        "external_vlm_call": False,
        "diagnostics": {"actionable_cause": actionable_cause},
    }
    handoff = evidence.get("handoff")
    if isinstance(handoff, dict):
        handoff["visual_observations_path"] = "visual_observations.jsonl"
        handoff["visual_candidates_path"] = "visual_candidates.jsonl"
        handoff["visual_search_plan_path"] = VISUAL_SEARCH_PLAN_FILENAME
        handoff["image_fetch_status_path"] = IMAGE_FETCH_STATUS_FILENAME
        handoff["visual_provider_status_path"] = VISUAL_PROVIDER_STATUS_FILENAME
        handoff["visual_status"] = "blocked_missing_visual_provider"
    _write_json(evidence_path, evidence)
    validation = validate_artifacts(
        evidence_path=evidence_path,
        visual_observations_path=visual_observations_path,
    )
    visual_artifact_validation = validate_visual_artifacts(
        run_dir=run_dir,
        visual_search_plan_path=visual_search_plan_path,
        visual_candidates_path=visual_candidates_path,
        image_fetch_status_path=image_fetch_status_path,
        visual_observations_path=visual_observations_path,
        visual_provider_status_path=visual_provider_status_path,
        evidence_path=None,
    )
    status = _base_status(run_dir, evidence, "blocked_missing_visual_provider", created_at)
    status.update(
        {
            "ok": False,
            "terminal": True,
            "providers": [dict(item) for item in provider_statuses],
            "candidate_records": 0,
            "selected_observations": 0,
            "candidate_counts": {},
            "removal_counts": {},
            "near_duplicate_groups": [],
            "screenshot_capture": evidence["visual_acquisition"]["screenshot_capture"],
            "image_search_invocations": evidence["visual_acquisition"][
                "image_search_invocations"
            ],
            "screenshot_capture_requests": 0,
            "ocr_records": 0,
            "external_network_call": evidence["visual_acquisition"][
                "external_network_call"
            ],
            "external_ocr_call": False,
            "external_vlm_call": False,
            "diagnostics": {"actionable_cause": actionable_cause},
            "validation": validation.to_dict(),
            "visual_artifact_validation": visual_artifact_validation.to_dict(),
            "artifacts": {
                "evidence": str(evidence_path),
                "visual_search_plan": str(visual_search_plan_path),
                "visual_candidates": str(visual_candidates_path),
                "image_fetch_status": str(image_fetch_status_path),
                "visual_observations": str(visual_observations_path),
                "visual_provider_status": str(visual_provider_status_path),
                "visual_acquisition_status": str(run_dir / "visual_acquisition_status.json"),
            },
        }
    )
    _write_json(run_dir / "visual_acquisition_status.json", status)
    return status


def _visual_task_by_angle(run_dir: Path) -> dict[str, Mapping[str, Any]]:
    path = run_dir / "visual_tasks.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VisualAcquisitionError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        return {}
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        angle_id = task.get("angle_id")
        if isinstance(angle_id, str) and angle_id:
            result[angle_id] = task
    return result


def _visual_routes(
    evidence: Mapping[str, Any],
    *,
    visual_task_by_angle: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    routes = evidence.get("routing", [])
    if not isinstance(routes, list):
        return []
    result = []
    for route in routes:
        if not isinstance(route, Mapping):
            continue
        modality = route.get("modality")
        if modality in SEARCH_ROUTES and modality != "text_only" and int(route.get("max_images", 0)) > 0:
            enriched = dict(route)
            visual_task = visual_task_by_angle.get(str(enriched.get("id") or ""))
            if visual_task is not None:
                enriched["task_id"] = visual_task.get("id")
            result.append(enriched)
    return result


def _ensure_fixture_sources(
    run_dir: Path,
    evidence: dict[str, Any],
    *,
    created_at: str,
) -> dict[str, Mapping[str, Any]]:
    sources = evidence.setdefault("sources", [])
    if not isinstance(sources, list):
        raise VisualAcquisitionError("evidence.sources must be a list")
    by_id = {
        source["id"]: source
        for source in sources
        if isinstance(source, Mapping) and isinstance(source.get("id"), str)
    }
    for source in (_fixture_image_source(by_id), _fixture_page_source(by_id)):
        if source["id"] not in by_id:
            source = dict(source)
            source["accessed_at"] = created_at
            sources.append(source)
            _write_json(run_dir / source["local_artifact_path"], source)
            by_id[source["id"]] = source
    _write_json(run_dir / "evidence.json", evidence)
    return by_id


def _source_by_id(evidence: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    sources = evidence.get("sources", [])
    if not isinstance(sources, list):
        raise VisualAcquisitionError("evidence.sources must be a list")
    return {
        source["id"]: source
        for source in sources
        if isinstance(source, Mapping) and isinstance(source.get("id"), str)
    }


def _fixture_image_source(source_by_id: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    if "src_visual_fixture_images" in source_by_id:
        return source_by_id["src_visual_fixture_images"]
    return {
        "id": "src_visual_fixture_images",
        "type": "image",
        "url": "https://example.com/local-image-fixture",
        "title": "Local visual fixture image provider",
        "published_at": None,
        "accessed_at": "2026-06-22T00:00:00Z",
        "quality": "unknown",
        "retrieval_status": "manual",
        "local_artifact_path": "sources/src_visual_fixture_images.json",
        "license_policy": "allowed",
        "robots_policy": "allowed",
        "policy_decision": "allowed",
        "policy_flags": [],
        "route": "visual_required",
        "angle_id": "angle_001",
        "visual_acquisition_provider": "local-image-fixture",
    }


def _fixture_page_source(source_by_id: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    if "src_visual_fixture_page" in source_by_id:
        return source_by_id["src_visual_fixture_page"]
    return {
        "id": "src_visual_fixture_page",
        "type": "web",
        "url": "https://example.com/local-screenshot-fixture",
        "title": "Local visual fixture screenshot page",
        "published_at": None,
        "accessed_at": "2026-06-22T00:00:00Z",
        "quality": "unknown",
        "retrieval_status": "manual",
        "local_artifact_path": "sources/src_visual_fixture_page.json",
        "license_policy": "allowed",
        "robots_policy": "allowed",
        "policy_decision": "allowed",
        "policy_flags": [],
        "route": "visual_required",
        "angle_id": "angle_001",
        "visual_acquisition_provider": "local-screenshot-fixture",
    }


def _brave_image_search_config(
    *,
    evidence: Mapping[str, Any],
    overrides: Mapping[str, Any] | None,
) -> _BraveImageSearchConfig:
    max_images = max(10, _max_images(evidence))
    api_key = _config_string(
        overrides,
        "brave_api_key",
        env_names=("CODEX_DEEPRESEARCH_BRAVE_SEARCH_API_KEY", "BRAVE_SEARCH_API_KEY"),
    )
    return _BraveImageSearchConfig(
        api_key=api_key,
        endpoint=_config_string(
            overrides,
            "brave_endpoint",
            env_names=("CODEX_DEEPRESEARCH_BRAVE_IMAGE_ENDPOINT",),
            default=BRAVE_IMAGE_ENDPOINT,
        )
        or BRAVE_IMAGE_ENDPOINT,
        count=_bounded_int(
            _config_value(
                overrides,
                "brave_image_count",
                env_names=("CODEX_DEEPRESEARCH_BRAVE_IMAGE_COUNT",),
            ),
            default=max_images,
            minimum=1,
            maximum=200,
        ),
        country=(
            _config_string(
                overrides,
                "brave_country",
                env_names=("CODEX_DEEPRESEARCH_BRAVE_COUNTRY",),
                default=BRAVE_DEFAULT_COUNTRY,
            )
            or BRAVE_DEFAULT_COUNTRY
        ).upper(),
        search_lang=(
            _config_string(
                overrides,
                "brave_search_lang",
                env_names=("CODEX_DEEPRESEARCH_BRAVE_SEARCH_LANG",),
                default=BRAVE_DEFAULT_SEARCH_LANG,
            )
            or BRAVE_DEFAULT_SEARCH_LANG
        ).lower(),
        safesearch=(
            _config_string(
                overrides,
                "brave_safesearch",
                env_names=("CODEX_DEEPRESEARCH_BRAVE_SAFESEARCH",),
                default=BRAVE_DEFAULT_SAFESEARCH,
            )
            or BRAVE_DEFAULT_SAFESEARCH
        ).lower(),
        timeout_seconds=_bounded_float(
            _config_value(
                overrides,
                "brave_timeout_seconds",
                env_names=("CODEX_DEEPRESEARCH_BRAVE_TIMEOUT_SECONDS",),
            ),
            default=BRAVE_DEFAULT_TIMEOUT_SECONDS,
            minimum=1.0,
            maximum=120.0,
        ),
        estimated_cost_usd=_bounded_float(
            _config_value(
                overrides,
                "brave_estimated_cost_usd",
                env_names=("CODEX_DEEPRESEARCH_BRAVE_ESTIMATED_COST_USD",),
            ),
            default=BRAVE_DEFAULT_ESTIMATED_COST_USD,
            minimum=0.0,
            maximum=100.0,
        ),
        actual_cost_usd=_bounded_float(
            _config_value(
                overrides,
                "brave_actual_cost_usd",
                env_names=("CODEX_DEEPRESEARCH_BRAVE_ACTUAL_COST_USD",),
            ),
            default=0.0,
            minimum=0.0,
            maximum=100.0,
        ),
        user_agent=_config_string(
            overrides,
            "brave_user_agent",
            env_names=("CODEX_DEEPRESEARCH_BRAVE_USER_AGENT",),
            default=BRAVE_DEFAULT_USER_AGENT,
        )
        or BRAVE_DEFAULT_USER_AGENT,
        allow_result_storage=_config_bool(
            overrides,
            "brave_allow_result_storage",
            env_names=(BRAVE_STORAGE_CONFIRMATION_ENV,),
            default=False,
        ),
    )


def _config_value(
    overrides: Mapping[str, Any] | None,
    key: str,
    *,
    env_names: Sequence[str],
) -> Any:
    if overrides is not None and key in overrides:
        return overrides[key]
    for env_name in env_names:
        if env_name in os.environ:
            return os.environ[env_name]
    return None


def _config_string(
    overrides: Mapping[str, Any] | None,
    key: str,
    *,
    env_names: Sequence[str],
    default: str | None = None,
) -> str | None:
    value = _config_value(overrides, key, env_names=env_names)
    if value is None:
        return default
    return _string(value)


def _config_bool(
    overrides: Mapping[str, Any] | None,
    key: str,
    *,
    env_names: Sequence[str],
    default: bool,
) -> bool:
    value = _config_value(overrides, key, env_names=env_names)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in TRUE_CONFIG_VALUES:
            return True
        if normalized in FALSE_CONFIG_VALUES:
            return False
    return default


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    parsed = _int_or_zero(value)
    if parsed <= 0:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        parsed = default
    elif isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = float(value)
        except ValueError:
            parsed = default
    else:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _brave_config_keys(*, configured: bool, storage_confirmed: bool) -> list[str]:
    keys = [
        "CODEX_DEEPRESEARCH_BRAVE_SEARCH_API_KEY or BRAVE_SEARCH_API_KEY"
        if configured
        else "missing: CODEX_DEEPRESEARCH_BRAVE_SEARCH_API_KEY or BRAVE_SEARCH_API_KEY",
        BRAVE_STORAGE_CONFIRMATION_ENV
        if storage_confirmed
        else f"missing: {BRAVE_STORAGE_CONFIRMATION_ENV}",
        "CODEX_DEEPRESEARCH_BRAVE_IMAGE_COUNT",
        "CODEX_DEEPRESEARCH_BRAVE_COUNTRY",
        "CODEX_DEEPRESEARCH_BRAVE_SEARCH_LANG",
        "CODEX_DEEPRESEARCH_BRAVE_SAFESEARCH",
        "CODEX_DEEPRESEARCH_BRAVE_ESTIMATED_COST_USD",
    ]
    return keys


def _query_for_route(context: _VisualContext, route: Mapping[str, Any]) -> str:
    angle_id = _string(route.get("id"))
    if angle_id:
        task = context.visual_task_by_angle.get(angle_id)
        if isinstance(task, Mapping):
            query = _string(task.get("query"))
            if query:
                return query
    query = _string(route.get("query"))
    if query:
        return query
    return _string(context.evidence.get("question")) or ""


def _loads_provider_payload(body: str) -> Mapping[str, Any]:
    if not body.strip():
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"raw_error": body[:500]}
    return payload if isinstance(payload, Mapping) else {"raw_payload": payload}


def _provider_query_diagnostics(
    *,
    query: str,
    params: Mapping[str, Any],
    status_code: int | None,
    headers: Mapping[str, str],
    elapsed_ms: int | None,
    error: str | None,
) -> dict[str, Any]:
    normalized_headers = {str(key).lower(): str(value) for key, value in headers.items()}
    return {
        "query": query,
        "count": params.get("count"),
        "country": params.get("country"),
        "search_lang": params.get("search_lang"),
        "safesearch": params.get("safesearch"),
        "http_status": status_code,
        "elapsed_ms": elapsed_ms,
        "rate_limit": {
            "limit": normalized_headers.get("x-ratelimit-limit"),
            "remaining": normalized_headers.get("x-ratelimit-remaining"),
            "reset": normalized_headers.get("x-ratelimit-reset"),
            "retry_after": normalized_headers.get("retry-after"),
        },
        "cache": {"cache_control": normalized_headers.get("cache-control")},
        "error": error,
    }


def _blocked_reason_for_http_status(status_code: int) -> str:
    if status_code in {401, 403}:
        return "provider_auth_failed"
    if status_code == 429:
        return "provider_rate_limited"
    if 400 <= status_code < 500:
        return f"provider_http_{status_code}"
    if status_code >= 500:
        return "provider_unavailable"
    return "provider_request_failed"


def _provider_error_message(payload: Mapping[str, Any], status_code: int) -> str:
    error = payload.get("error")
    if isinstance(error, Mapping):
        message = _string(error.get("message")) or _string(error.get("detail"))
        code = _string(error.get("code"))
        if message and code:
            return f"{code}: {message}"
        if message:
            return message
        if code:
            return code
    message = _string(payload.get("message")) or _string(payload.get("detail"))
    return message or f"provider returned HTTP {status_code}"


def _provider_secret_values(config: _BraveImageSearchConfig) -> tuple[str, ...]:
    return tuple(value for value in (config.api_key,) if isinstance(value, str) and value)


def _redact_provider_value(value: Any, *, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return _redact_provider_text(value, secrets=secrets)
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SECRET_KEY_PATTERN.search(key_text):
                redacted[key_text] = REDACTED_SECRET
            else:
                redacted[key_text] = _redact_provider_value(item, secrets=secrets)
        return redacted
    if isinstance(value, list):
        return [_redact_provider_value(item, secrets=secrets) for item in value]
    if isinstance(value, tuple):
        return [_redact_provider_value(item, secrets=secrets) for item in value]
    return value


def _redact_provider_text(value: str, *, secrets: Sequence[str]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTED_SECRET)
    redacted = SECRET_JSON_FIELD_PATTERN.sub(
        lambda match: f"{match.group(1)}{REDACTED_SECRET}{match.group(3)}",
        redacted,
    )
    redacted = SECRET_FIELD_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED_SECRET}",
        redacted,
    )
    return redacted


def _assign_real_candidate_costs(
    candidates: Sequence[dict[str, Any]],
    *,
    estimated_cost_usd: float,
    actual_cost_usd: float,
) -> None:
    if not candidates:
        return
    per_candidate_estimate = estimated_cost_usd / len(candidates)
    per_candidate_actual = actual_cost_usd / len(candidates)
    for candidate in candidates:
        candidate["estimated_cost_usd"] = round(per_candidate_estimate, 8)
        candidate["actual_cost_usd"] = round(per_candidate_actual, 8)


def _result_image_url(result: Mapping[str, Any]) -> str | None:
    direct = _string(result.get("image_url")) or _string(result.get("thumbnail_url"))
    if direct:
        return direct
    properties = result.get("properties")
    if isinstance(properties, Mapping):
        value = (
            _string(properties.get("url"))
            or _string(properties.get("image_url"))
            or _string(properties.get("thumbnail_url"))
        )
        if value:
            return value
    thumbnail = result.get("thumbnail")
    if isinstance(thumbnail, Mapping):
        value = _string(thumbnail.get("src")) or _string(thumbnail.get("url"))
        if value:
            return value
    meta_url = result.get("meta_url")
    if isinstance(meta_url, Mapping):
        value = _string(meta_url.get("path")) or _string(meta_url.get("url"))
        if value and re.search(r"\.(png|jpe?g|gif|webp)(?:\?|$)", value, re.IGNORECASE):
            return value
    return None


def _result_page_url(result: Mapping[str, Any]) -> str | None:
    source = result.get("source")
    if isinstance(source, Mapping):
        value = _string(source.get("url")) or _string(source.get("page_url"))
        if value:
            return value
    return (
        _string(result.get("page_url"))
        or _string(result.get("source_page_url"))
        or _string(result.get("url"))
        or _string(result.get("host_page_url"))
    )


def _result_title(result: Mapping[str, Any]) -> str | None:
    return _string(result.get("title")) or _string(result.get("alt")) or "Image search result"


def _result_dimensions(result: Mapping[str, Any]) -> tuple[int, int]:
    properties = result.get("properties")
    if isinstance(properties, Mapping):
        width = _int_or_zero(properties.get("width"))
        height = _int_or_zero(properties.get("height"))
        if width or height:
            return width, height
    return _int_or_zero(result.get("width")), _int_or_zero(result.get("height"))


def _sanitized_result_metadata(
    result: Mapping[str, Any],
    *,
    secrets: Sequence[str],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in ("title", "type", "age"):
        value = result.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            metadata[key] = value
    source = result.get("source")
    if isinstance(source, Mapping):
        metadata["source"] = {
            str(key): value
            for key, value in source.items()
            if key in {"name", "url"} and (isinstance(value, str) or value is None)
        }
    properties = result.get("properties")
    if isinstance(properties, Mapping):
        metadata["properties"] = {
            str(key): value
            for key, value in properties.items()
            if key in {"width", "height", "format", "content_type"}
            and (isinstance(value, (str, int, float, bool)) or value is None)
        }
    return _redact_provider_value(metadata, secrets=secrets)


def _is_real_provider_name(provider: str) -> bool:
    return provider in {BRAVE_IMAGE_PROVIDER, BROWSER_SCREENSHOT_PROVIDER}


def _uses_fixture_sources(provider_names: Sequence[str]) -> bool:
    return any(
        provider in {"local-image-fixture", "local-screenshot-fixture"}
        for provider in provider_names
    )


def _initial_provider_status(provider: _VisualProvider) -> dict[str, Any]:
    status = getattr(provider, "initial_status", None)
    if callable(status):
        value = status()
        if isinstance(value, Mapping):
            return dict(value)
    return _default_provider_status(provider.name, candidates=0, invoked=False)


def _collection_provider_status(
    provider: _VisualProvider,
    provider_candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    value = getattr(provider, "last_status", None)
    if isinstance(value, Mapping):
        return dict(value)
    return _default_provider_status(provider.name, candidates=len(provider_candidates), invoked=True)


def _default_provider_status(
    provider: str,
    *,
    candidates: int,
    invoked: bool,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "provider_kind": _provider_kind(provider),
        "provider_mode": "real" if _is_real_provider_name(provider) else "fixture",
        "configured": True,
        "available": True,
        "blocked_reason": None,
        "invoked": invoked,
        "invocations": 1 if invoked else 0,
        "candidates": candidates,
        "candidates_discovered": candidates,
        "artifacts_fetched": 0,
        "vlm_images_analyzed": 0,
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "last_error": None,
        "external_network_call": provider == BRAVE_IMAGE_PROVIDER and invoked,
        "external_vlm_call": False,
        "diagnostics": {},
    }


def _browser_transport_provider_mode(transport: BrowserScreenshotTransport) -> str:
    mode = _string(getattr(transport, "provider_mode", "real")) or "real"
    if mode in {"real", "fixture", "manual", "user_provided"}:
        return mode
    return "real"


def _blocked_actionable_cause(provider_statuses: Sequence[Mapping[str, Any]]) -> str:
    reasons = []
    for status in provider_statuses:
        provider = _string(status.get("provider")) or "unknown-provider"
        reason = _string(status.get("blocked_reason")) or _string(status.get("last_error"))
        if reason:
            reasons.append(f"{provider}: {reason}")
    suffix = "; ".join(reasons) if reasons else "no configured or available real visual provider"
    return (
        "visual_required route needs a configured and available real image search provider; "
        + suffix
    )


def _source_html_path(run_dir: Path, source: Mapping[str, Any]) -> Path | None:
    local_path = source.get("local_artifact_path")
    if isinstance(local_path, str):
        candidate = run_dir / local_path
        if candidate.is_file() and candidate.suffix.lower() in {".html", ".htm"}:
            return candidate
    url = _string(source.get("url"))
    if url and url.startswith("file://"):
        parsed = urlparse(url)
        candidate = Path(parsed.path)
        if candidate.is_file() and candidate.suffix.lower() in {".html", ".htm"}:
            return candidate
    return None


def _providers(
    names: Sequence[str],
    *,
    real_image_search_transport: _BraveImageSearchTransport | None,
    real_image_search_config: Mapping[str, Any] | None,
    browser_transport: BrowserScreenshotTransport | None,
    evidence: Mapping[str, Any],
) -> list[_VisualProvider]:
    providers: list[_VisualProvider] = []
    brave_config = _brave_image_search_config(
        evidence=evidence,
        overrides=real_image_search_config,
    )
    for name in names:
        if name == "local-page":
            providers.append(_LocalPageProvider())
        elif name == "local-image-fixture":
            providers.append(_LocalImageFixtureProvider())
        elif name == "local-screenshot-fixture":
            providers.append(_LocalScreenshotFixtureProvider())
        elif name == BROWSER_SCREENSHOT_PROVIDER:
            providers.append(_BrowserScreenshotProvider(transport=browser_transport))
        elif name == BRAVE_IMAGE_PROVIDER:
            providers.append(
                _BraveImageSearchProvider(
                    config=brave_config,
                    transport=real_image_search_transport,
                )
            )
        else:
            raise VisualAcquisitionError(f"unknown visual provider: {name}")
    return providers


def _normalize_provider_names(providers: Sequence[str] | None) -> tuple[str, ...]:
    raw = providers or DEFAULT_VISUAL_PROVIDERS
    normalized: list[str] = []
    for provider in raw:
        for item in str(provider).split(","):
            name = item.strip().lower().replace("_", "-")
            if name:
                normalized.append(name)
    if not normalized:
        raise VisualAcquisitionError("at least one visual provider is required")
    return tuple(_dedupe(normalized))


def _normalize_screenshot_modes(modes: Sequence[str] | None) -> tuple[str, ...]:
    raw = modes or SCREENSHOT_MODES
    normalized: list[str] = []
    for mode in raw:
        for item in str(mode).split(","):
            value = item.strip().lower().replace("-", "_")
            if value == "all":
                normalized.extend(SCREENSHOT_MODES)
            elif value in SCREENSHOT_MODES:
                normalized.append(value)
            elif value:
                raise VisualAcquisitionError(
                    "screenshot mode must be one of: " + ", ".join(("all", *SCREENSHOT_MODES))
                )
    return tuple(_dedupe(normalized or list(SCREENSHOT_MODES)))


def _source_route(
    routes: Sequence[Mapping[str, Any]],
    source: Mapping[str, Any],
) -> Mapping[str, Any]:
    angle_id = source.get("angle_id")
    route = source.get("route")
    for candidate in routes:
        if angle_id and candidate.get("id") == angle_id:
            return candidate
        if route and candidate.get("modality") == route:
            return candidate
    return _first_route(routes)


def _first_route(routes: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not routes:
        return {
            "id": "angle_001",
            "modality": "visual_required",
            "visual_tasks": ["image_claim_alignment"],
            "max_images": 12,
        }
    return routes[0]


def _max_images(evidence: Mapping[str, Any]) -> int:
    budget = evidence.get("budget")
    if isinstance(budget, Mapping):
        value = budget.get("max_images")
        if isinstance(value, int) and value > 0:
            return value
    return 12


def _candidate_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get("candidate_class") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _removal_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        for reason in _string_list(record.get("removal_reasons")):
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _screenshot_capture_summary(
    records: Sequence[Mapping[str, Any]],
    provider_statuses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    requests = []
    unsupported = []
    for record in records:
        screenshot = record.get("screenshot")
        if not isinstance(screenshot, Mapping):
            continue
        item = {
            "mode": screenshot.get("mode"),
            "provider": screenshot.get("provider"),
            "supported": screenshot.get("supported") is True,
            "status": "captured" if record.get("status") == "accepted" else record.get("status"),
            "candidate_id": record.get("id"),
            "unsupported_reason": screenshot.get("unsupported_reason"),
        }
        requests.append(item)
        if item["supported"] is False:
            unsupported.append(item)
    providers = [item.get("provider") for item in provider_statuses]
    return {
        "interface_modes": list(SCREENSHOT_MODES),
        "providers": providers,
        "requests": requests,
        "unsupported": unsupported,
    }


def _route_summary(routes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": route.get("id"),
            "route": route.get("modality"),
            "max_images": route.get("max_images"),
            "visual_tasks": list(route.get("visual_tasks", []))
            if isinstance(route.get("visual_tasks"), list)
            else [],
        }
        for route in routes
    ]


def _count_provider(statuses: Sequence[Mapping[str, Any]], provider: str) -> int:
    return sum(
        _int_or_zero(status.get("invocations")) or 1
        for status in statuses
        if status.get("provider") == provider
    )


def _image_search_invocations(statuses: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for status in statuses:
        provider = _string(status.get("provider")) or ""
        if _provider_kind(provider) != "web_image_search":
            continue
        total += _int_or_zero(status.get("invocations")) or (
            1 if status.get("invoked") is not False else 0
        )
    return total


def _normalized_candidate_url(record: Mapping[str, Any]) -> str | None:
    if record.get("origin") == "screenshot":
        page_url = _string(record.get("page_url"))
        if not page_url:
            return None
        screenshot = record.get("screenshot")
        mode = screenshot.get("mode") if isinstance(screenshot, Mapping) else None
        return normalize_url(page_url) + "#screenshot:" + str(mode or record.get("local_artifact_path"))
    value = _string(record.get("image_url")) or _string(record.get("page_url"))
    if not value:
        return None
    return normalize_url(value)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VisualAcquisitionError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise VisualAcquisitionError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VisualAcquisitionError(f"expected JSON object in {path}")
    if payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise VisualAcquisitionError(f"evidence schema_version must be {EVIDENCE_SCHEMA_VERSION}")
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


def _base_status(
    run_dir: Path,
    evidence: Mapping[str, Any],
    status: str,
    created_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": VISUAL_ACQUISITION_SCHEMA_VERSION,
        "run_id": evidence.get("run_id", run_dir.name),
        "run_dir": str(run_dir),
        "status": status,
        "created_at": created_at,
    }


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return []
    result: list[str] = []
    for item in value:
        text = _string(item)
        if text:
            result.append(text)
    return result


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_") or "visual"


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
