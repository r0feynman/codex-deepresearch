"""Page image extraction and fetch/cache helpers for automatic visual runs."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import mimetypes
import re
import socket
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, unquote_to_bytes, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .cache_keys import image_cache_key, normalize_url
from .evidence_schema import validate_artifacts
from .search_handoff import resolve_run_dir
from .visual_artifacts import (
    IMAGE_FETCH_STATUS_FILENAME,
    VISUAL_ARTIFACT_SCHEMA_VERSION,
    VISUAL_CANDIDATES_FILENAME,
    VISUAL_PROVIDER_STATUS_FILENAME,
    VISUAL_PROVIDER_STATUS_SCHEMA_VERSION,
    VISUAL_SEARCH_PLAN_FILENAME,
    validate_visual_artifacts,
)


PAGE_IMAGE_EXTRACTION_SCHEMA_VERSION = "codex-deepresearch.page-image-extraction.v0"
DEFAULT_MAX_IMAGE_BYTES = 5_000_000
DEFAULT_TIMEOUT_SECONDS = 10.0
SUPPORTED_IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
PROVIDER_NAME = "page-image-extractor"
SOURCE_IMAGE_FETCH_BLOCKING_POLICY_FLAGS = {
    "access_controlled",
    "captcha_protected",
    "copyright_restricted",
    "login_gated",
    "paywall",
    "pii_detected",
    "robots_disallowed",
}
SOURCE_IMAGE_FETCH_MANUAL_REVIEW_POLICY_FLAGS = {
    "copyright_manual_review",
    "robots_manual_review",
}
_POLICY_BLOCKED_RETRIEVAL_ERROR_PREFIXES = ("guardrail_", "policy_")
_POLICY_BLOCKING_FETCH_REASONS = {
    "local_file_outside_run",
    "local_url_from_remote_page",
    "private_network_url_from_remote_page",
}
_INTERNAL_HOST_EXACT_MATCHES = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "host.docker.internal",
}
_INTERNAL_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home",
    ".corp",
    ".svc",
    ".cluster.local",
    ".docker.internal",
)


class PageImageExtractionError(ValueError):
    """Raised when page image extraction cannot continue."""


@dataclass(frozen=True)
class FetchResponse:
    """Result returned by an injected image transport."""

    content: bytes | None
    mime_type: str | None = None
    status_code: int | None = None
    final_url: str | None = None
    error_code: str | None = None


ImageTransport = Callable[[str], FetchResponse]


@dataclass(frozen=True)
class _PageImage:
    url: str | None
    origin: str
    alt_text: str | None
    caption_text: str | None
    surrounding_text: str | None
    width_hint: int | None
    height_hint: int | None
    phash_hint: str | None
    html_attrs: Mapping[str, str]


def extract_and_fetch_page_images(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
    transport: ImageTransport | None = None,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    max_fetches: int | None = None,
    provider_mode: str = "real",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Extract page images, fetch/cache allowed artifacts, and link evidence images.

    The default transport supports data URLs, file URLs, local paths, and HTTP(S).
    Tests can pass an injected transport to keep the run no-network and deterministic.
    """

    if max_image_bytes < 1:
        raise PageImageExtractionError("max_image_bytes must be positive")
    if max_fetches is not None and max_fetches < 0:
        raise PageImageExtractionError("max_fetches cannot be negative")
    if provider_mode not in {"real", "fixture", "manual", "user_provided"}:
        raise PageImageExtractionError("provider_mode is invalid")

    run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    evidence_path = run_dir / "evidence.json"
    if not evidence_path.exists():
        raise PageImageExtractionError(f"missing evidence.json in run directory: {run_dir}")

    evidence = _read_json(evidence_path)
    sources = evidence.get("sources", [])
    if not isinstance(sources, list):
        raise PageImageExtractionError("evidence.sources must be a list")
    images = evidence.setdefault("images", [])
    if not isinstance(images, list):
        raise PageImageExtractionError("evidence.images must be a list")

    created_at = _utc_now()
    provider_run_id = str(evidence.get("run_id") or run_dir.name)
    source_by_id = {
        source.get("id"): source
        for source in sources
        if isinstance(source, Mapping) and isinstance(source.get("id"), str)
    }
    visual_task_by_angle = _visual_task_by_angle(run_dir)
    routes_by_angle = _routes_by_angle(evidence)
    page_sources = [
        source
        for source in sources
        if isinstance(source, Mapping) and _source_html_path(run_dir, source) is not None
    ]
    max_selected = _max_fetches(evidence, max_fetches)

    candidates: list[dict[str, Any]] = []
    for source in page_sources:
        candidates.extend(
            _candidate_records_for_source(
                run_dir=run_dir,
                source=source,
                evidence=evidence,
                routes_by_angle=routes_by_angle,
                visual_task_by_angle=visual_task_by_angle,
                provider_mode=provider_mode,
                provider_run_id=provider_run_id,
                created_at=created_at,
            )
        )

    fetch_records, evidence_images = _fetch_candidates(
        run_dir=run_dir,
        candidates=candidates,
        source_by_id=source_by_id,
        transport=transport,
        max_image_bytes=max_image_bytes,
        max_fetches=max_selected,
        timeout_seconds=timeout_seconds,
    )
    _replace_page_extractor_images(images, evidence_images)
    if isinstance(evidence.get("budget"), dict):
        evidence["budget"]["images_selected"] = len(evidence_images)
    evidence["page_image_extraction"] = {
        "schema_version": PAGE_IMAGE_EXTRACTION_SCHEMA_VERSION,
        "status": "page_images_processed",
        "created_at": created_at,
        "provider": PROVIDER_NAME,
        "provider_mode": provider_mode,
        "page_sources_scanned": len(page_sources),
        "candidate_records": len(candidates),
        "fetch_records": len(fetch_records),
        "images_linked": len(evidence_images),
        "image_fetch_status_path": IMAGE_FETCH_STATUS_FILENAME,
        "visual_candidates_path": VISUAL_CANDIDATES_FILENAME,
        "visual_search_plan_path": VISUAL_SEARCH_PLAN_FILENAME,
        "visual_provider_status_path": VISUAL_PROVIDER_STATUS_FILENAME,
        "status_counts": _count_by(fetch_records, "fetch_status"),
        "max_image_bytes": max_image_bytes,
        "max_fetches": max_selected,
    }
    handoff = evidence.get("handoff")
    if isinstance(handoff, dict):
        handoff["visual_candidates_path"] = VISUAL_CANDIDATES_FILENAME
        handoff["visual_search_plan_path"] = VISUAL_SEARCH_PLAN_FILENAME
        handoff["image_fetch_status_path"] = IMAGE_FETCH_STATUS_FILENAME
        handoff["visual_provider_status_path"] = VISUAL_PROVIDER_STATUS_FILENAME
    _write_json(evidence_path, evidence)

    visual_search_plan = _visual_search_plan(
        run_dir=run_dir,
        evidence=evidence,
        candidates=candidates,
        provider_mode=provider_mode,
        created_at=created_at,
        max_fetches=max_selected,
    )
    provider_status = _visual_provider_status(
        run_dir=run_dir,
        provider_mode=provider_mode,
        created_at=created_at,
        candidates=candidates,
        fetch_records=fetch_records,
    )
    _write_json(run_dir / VISUAL_SEARCH_PLAN_FILENAME, visual_search_plan)
    _write_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME, candidates)
    _write_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME, fetch_records)
    _write_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME, provider_status)

    evidence_validation = validate_artifacts(evidence_path=evidence_path)
    visual_validation = validate_visual_artifacts(
        visual_search_plan_path=run_dir / VISUAL_SEARCH_PLAN_FILENAME,
        visual_candidates_path=run_dir / VISUAL_CANDIDATES_FILENAME,
        image_fetch_status_path=run_dir / IMAGE_FETCH_STATUS_FILENAME,
        visual_provider_status_path=run_dir / VISUAL_PROVIDER_STATUS_FILENAME,
        evidence_path=evidence_path,
        visual_tasks_path=run_dir / "visual_tasks.json",
        search_results_path=run_dir / "search_results.jsonl",
    )
    status = {
        "schema_version": PAGE_IMAGE_EXTRACTION_SCHEMA_VERSION,
        "run_id": provider_run_id,
        "run_dir": str(run_dir),
        "status": "page_images_processed",
        "created_at": created_at,
        "page_sources_scanned": len(page_sources),
        "candidate_records": len(candidates),
        "fetch_records": len(fetch_records),
        "images_linked": len(evidence_images),
        "status_counts": _count_by(fetch_records, "fetch_status"),
        "evidence_validation": evidence_validation.to_dict(),
        "visual_artifact_validation": visual_validation.to_dict(),
        "artifacts": {
            "evidence": str(evidence_path),
            "visual_search_plan": str(run_dir / VISUAL_SEARCH_PLAN_FILENAME),
            "visual_candidates": str(run_dir / VISUAL_CANDIDATES_FILENAME),
            "image_fetch_status": str(run_dir / IMAGE_FETCH_STATUS_FILENAME),
            "visual_provider_status": str(run_dir / VISUAL_PROVIDER_STATUS_FILENAME),
            "page_image_extraction_status": str(run_dir / "page_image_extraction_status.json"),
        },
    }
    _write_json(run_dir / "page_image_extraction_status.json", status)
    return status


def extract_page_image_candidates(
    *,
    html: str,
    page_url: str,
) -> list[dict[str, Any]]:
    """Return page image candidates from one HTML document without fetching bytes."""

    parser = _PageImageHTMLParser()
    parser.feed(html)
    parser.close()
    records = []
    for index, image in enumerate(parser.images, start=1):
        records.append(
            {
                "index": index,
                "origin": image.origin,
                "page_url": page_url,
                "image_url": _safe_join_url(page_url, image.url or "") if image.url else None,
                "alt_text": image.alt_text,
                "caption_text": image.caption_text,
                "surrounding_text": image.surrounding_text,
                "width": image.width_hint,
                "height": image.height_hint,
                "phash_hint": image.phash_hint,
                "raw_provider_metadata": {"html_attrs": dict(image.html_attrs)},
            }
        )
    return records


class _PageImageHTMLParser(HTMLParser):
    _TEXT_BLOCK_TAGS = {
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dt",
        "figcaption",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "p",
        "section",
        "td",
        "th",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[_PageImage] = []
        self._text_parts: list[str] = []
        self._skip_depth = 0
        self._figure_stack: list[int] = []
        self._figure_images: dict[int, list[int]] = {}
        self._figure_counter = 0
        self._caption_stack: list[int] = []
        self._caption_parts: list[str] = []
        self._meta_image_alt: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {name.lower(): value or "" for name, value in attrs}
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if tag == "figure":
            self._figure_counter += 1
            self._figure_stack.append(self._figure_counter)
            self._figure_images.setdefault(self._figure_counter, [])
        elif tag == "figcaption":
            figure_id = self._figure_stack[-1] if self._figure_stack else 0
            self._caption_stack.append(figure_id)
            self._caption_parts = []
        elif tag == "meta":
            self._handle_meta(attr)
        elif tag == "img":
            self._handle_img(attr)
        elif tag == "source":
            self._handle_srcset(attr, origin="srcset")
        if tag in self._TEXT_BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "figcaption" and self._caption_stack:
            figure_id = self._caption_stack.pop()
            caption = _compact_whitespace(" ".join(self._caption_parts))
            if caption:
                for image_index in self._figure_images.get(figure_id, []):
                    current = self.images[image_index]
                    self.images[image_index] = _PageImage(
                        url=current.url,
                        origin=current.origin,
                        alt_text=current.alt_text,
                        caption_text=caption,
                        surrounding_text=_join_context(current.surrounding_text, caption),
                        width_hint=current.width_hint,
                        height_hint=current.height_hint,
                        phash_hint=current.phash_hint,
                        html_attrs=current.html_attrs,
                    )
            self._caption_parts = []
        elif tag == "figure" and self._figure_stack:
            self._figure_stack.pop()
        if tag in self._TEXT_BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._caption_stack:
            self._caption_parts.append(data)
        self._text_parts.append(data)

    def _handle_meta(self, attr: Mapping[str, str]) -> None:
        prop = (attr.get("property") or attr.get("name") or "").strip().lower()
        content = attr.get("content", "").strip()
        if not content:
            return
        if prop in {"og:image:alt", "twitter:image:alt"}:
            self._meta_image_alt = content
            return
        if prop in {"og:image", "og:image:url", "og:image:secure_url", "twitter:image"}:
            self._append_image(
                url=content,
                origin="open_graph",
                alt_text=self._meta_image_alt or attr.get("alt") or "Open Graph image",
                width_hint=_int_or_none(attr.get("width") or attr.get("data-width")),
                height_hint=_int_or_none(attr.get("height") or attr.get("data-height")),
                phash_hint=attr.get("data-phash") or None,
                attrs=attr,
            )

    def _handle_img(self, attr: Mapping[str, str]) -> None:
        alt_text = attr.get("alt") or attr.get("title") or None
        width_hint = _int_or_none(attr.get("width") or attr.get("data-width"))
        height_hint = _int_or_none(attr.get("height") or attr.get("data-height"))
        phash_hint = attr.get("data-phash") or None
        if attr.get("src"):
            self._append_image(
                url=attr["src"],
                origin="page_image",
                alt_text=alt_text,
                width_hint=width_hint,
                height_hint=height_hint,
                phash_hint=phash_hint,
                attrs=attr,
            )
        for key in ("data-src", "data-lazy-src", "data-original", "data-url", "data-image"):
            if attr.get(key):
                self._append_image(
                    url=attr[key],
                    origin="lazy_loaded",
                    alt_text=alt_text,
                    width_hint=width_hint,
                    height_hint=height_hint,
                    phash_hint=phash_hint,
                    attrs=attr,
                )
        self._handle_srcset(attr, origin="srcset")
        for key in ("data-srcset", "data-lazy-srcset"):
            if attr.get(key):
                self._append_srcset(attr[key], attr=attr, origin="lazy_loaded")

    def _handle_srcset(self, attr: Mapping[str, str], *, origin: str) -> None:
        if attr.get("srcset"):
            self._append_srcset(attr["srcset"], attr=attr, origin=origin)

    def _append_srcset(
        self,
        srcset: str,
        *,
        attr: Mapping[str, str],
        origin: str,
    ) -> None:
        for url, descriptor in _parse_srcset(srcset):
            self._append_image(
                url=url,
                origin=origin,
                alt_text=attr.get("alt") or attr.get("title") or descriptor or None,
                width_hint=_width_from_srcset_descriptor(descriptor),
                height_hint=_int_or_none(attr.get("height") or attr.get("data-height")),
                phash_hint=attr.get("data-phash") or None,
                attrs={**dict(attr), "srcset_descriptor": descriptor},
            )

    def _append_image(
        self,
        *,
        url: str,
        origin: str,
        alt_text: str | None,
        width_hint: int | None,
        height_hint: int | None,
        phash_hint: str | None,
        attrs: Mapping[str, str],
    ) -> None:
        context = _compact_whitespace(" ".join(self._text_parts[-16:]))
        image = _PageImage(
            url=url,
            origin=origin,
            alt_text=_compact_whitespace(alt_text or "") or None,
            caption_text=None,
            surrounding_text=context or None,
            width_hint=width_hint,
            height_hint=height_hint,
            phash_hint=phash_hint,
            html_attrs=dict(attrs),
        )
        self.images.append(image)
        if self._figure_stack:
            self._figure_images.setdefault(self._figure_stack[-1], []).append(len(self.images) - 1)


def _candidate_records_for_source(
    *,
    run_dir: Path,
    source: Mapping[str, Any],
    evidence: Mapping[str, Any],
    routes_by_angle: Mapping[str, Mapping[str, Any]],
    visual_task_by_angle: Mapping[str, Mapping[str, Any]],
    provider_mode: str,
    provider_run_id: str,
    created_at: str,
) -> list[dict[str, Any]]:
    html_path = _source_html_path(run_dir, source)
    if html_path is None:
        return []
    page_url = _string(source.get("url")) or html_path.resolve().as_uri()
    html = html_path.read_text(encoding="utf-8", errors="replace")
    raw_candidates = extract_page_image_candidates(html=html, page_url=page_url)
    records: list[dict[str, Any]] = []
    route = _source_route(source, routes_by_angle)
    visual_task = visual_task_by_angle.get(str(route.get("id") or source.get("angle_id") or ""))
    task_id = (
        _string(visual_task.get("id")) if isinstance(visual_task, Mapping) else None
    ) or _string(source.get("task_id")) or _task_id_for_angle(route.get("id"))
    angle_id = _string(route.get("id")) or _string(source.get("angle_id")) or "angle_001"
    route_name = _string(route.get("modality")) or _string(source.get("route")) or "visual_required"
    provider_provenance = {
        "provider": PROVIDER_NAME,
        "provider_kind": "page_extractor",
        "provider_mode": provider_mode,
        "provider_run_id": provider_run_id,
        "source_id": source.get("id"),
        "source_url": source.get("url"),
        "external_vlm_call": False,
    }
    policy_decision, policy_flags = _source_policy(source)
    for ordinal, raw in enumerate(raw_candidates, start=1):
        image_url = _string(raw.get("image_url"))
        candidate_id = _candidate_id(
            source_id=str(source.get("id") or "source"),
            origin=str(raw["origin"]),
            image_url=image_url or "",
            ordinal=ordinal,
        )
        record = {
            "id": candidate_id,
            "candidate_id": candidate_id,
            "plan_id": f"plan_{task_id}",
            "task_id": task_id,
            "angle_id": angle_id,
            "source_search_result_id": source.get("search_result_id"),
            "source_id": source.get("id"),
            "source_url": source.get("url"),
            "provider": PROVIDER_NAME,
            "provider_kind": "page_extractor",
            "provider_mode": provider_mode,
            "provider_run_id": provider_run_id,
            "provider_provenance": dict(provider_provenance),
            "origin": raw["origin"],
            "candidate_origin": raw["origin"],
            "page_url": raw.get("page_url"),
            "image_url": image_url,
            "rank": ordinal,
            "score": _score_from_rank(ordinal),
            "route": route_name,
            "candidate_status": "discovered",
            "rejection_reason": None,
            "policy_decision": policy_decision,
            "policy_flags": list(policy_flags),
            "license_policy": source.get("license_policy", "unknown"),
            "robots_policy": source.get("robots_policy", "unknown"),
            "alt_text": raw.get("alt_text"),
            "caption_text": raw.get("caption_text"),
            "surrounding_text": raw.get("surrounding_text"),
            "width": raw.get("width") or 0,
            "height": raw.get("height") or 0,
            "phash_hint": raw.get("phash_hint"),
            "raw_provider_metadata": raw.get("raw_provider_metadata", {}),
            "estimated_cost_usd": 0.0,
            "actual_cost_usd": 0.0,
            "created_at": created_at,
        }
        if policy_decision == "blocked":
            record["candidate_status"] = "policy_blocked"
            record["rejection_reason"] = "policy_blocked"
        records.append(record)
    return records


def _fetch_candidates(
    *,
    run_dir: Path,
    candidates: Sequence[dict[str, Any]],
    source_by_id: Mapping[Any, Mapping[str, Any]],
    transport: ImageTransport | None,
    max_image_bytes: int,
    max_fetches: int,
    timeout_seconds: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    fetch_records: list[dict[str, Any]] = []
    evidence_images: list[dict[str, Any]] = []
    seen_urls: dict[str, dict[str, Any]] = {}
    seen_hashes: dict[str, dict[str, Any]] = {}
    seen_phashes: dict[str, dict[str, Any]] = {}
    fetch_attempt_count = 0
    for candidate in candidates:
        candidate_id = str(candidate["candidate_id"])
        fetch_id = _fetch_id(candidate_id)
        base_record = _base_fetch_record(candidate, fetch_id)
        image_url = _string(candidate.get("image_url"))
        source = source_by_id.get(candidate.get("source_id")) or {}
        if candidate.get("policy_decision") == "blocked":
            candidate["candidate_status"] = "policy_blocked"
            candidate["rejection_reason"] = "policy_blocked"
            fetch_records.append(
                _complete_fetch_record(
                    base_record,
                    fetch_status="policy_blocked",
                    failure_code="policy_blocked",
                )
            )
            continue
        if candidate.get("policy_decision") == "manual_review":
            candidate["candidate_status"] = "rejected"
            candidate["rejection_reason"] = "policy_manual_review"
            fetch_records.append(
                _complete_fetch_record(
                    base_record,
                    fetch_status="skipped",
                    failure_code="policy_manual_review",
                )
            )
            continue
        if not image_url:
            candidate["candidate_status"] = "rejected"
            candidate["rejection_reason"] = "missing_image_url"
            fetch_records.append(
                _complete_fetch_record(
                    base_record,
                    fetch_status="skipped",
                    failure_code="missing_image_url",
                )
            )
            continue
        normalized_url = normalize_url(image_url)
        source_html_path = _source_html_path(run_dir, source)
        fetch_block_reason = _image_url_fetch_block_reason(
            image_url,
            source,
            run_dir=run_dir,
            source_html_path=source_html_path,
            resolve_hosts=transport is None,
        )
        if fetch_block_reason in _POLICY_BLOCKING_FETCH_REASONS:
            candidate["candidate_status"] = "policy_blocked"
            candidate["policy_decision"] = "blocked"
            candidate["rejection_reason"] = fetch_block_reason
            policy_flags = candidate.setdefault("policy_flags", [])
            if isinstance(policy_flags, list) and fetch_block_reason not in policy_flags:
                policy_flags.append(fetch_block_reason)
            base_record = _base_fetch_record(candidate, fetch_id)
            fetch_records.append(
                _complete_fetch_record(
                    base_record,
                    fetch_status="policy_blocked",
                    failure_code=fetch_block_reason,
                    normalized_url=normalized_url,
                    policy_decision="blocked",
                )
            )
            continue
        if fetch_block_reason:
            candidate["candidate_status"] = "rejected"
            candidate["rejection_reason"] = fetch_block_reason
            fetch_records.append(
                _complete_fetch_record(
                    base_record,
                    fetch_status="skipped",
                    failure_code=fetch_block_reason,
                    normalized_url=normalized_url,
                )
            )
            continue
        url_target = seen_urls.get(normalized_url)
        if url_target is not None:
            _mark_deduped_candidate(candidate, url_target, "duplicate_image_url", normalized_url)
            fetch_records.append(
                _complete_fetch_record(
                    base_record,
                    fetch_status="deduped",
                    failure_code="duplicate_image_url",
                    dedupe_target=url_target,
                    normalized_url=normalized_url,
                )
            )
            continue
        if fetch_attempt_count >= max_fetches:
            candidate["candidate_status"] = "budget_pruned"
            candidate["policy_decision"] = "budget_pruned"
            candidate["rejection_reason"] = "budget_pruned"
            fetch_records.append(
                _complete_fetch_record(
                    base_record,
                    fetch_status="budget_pruned",
                    failure_code="budget_pruned",
                    normalized_url=normalized_url,
                    policy_decision="budget_pruned",
                )
            )
            continue
        fetch_attempt_count += 1
        seen_urls[normalized_url] = {
            "candidate_id": candidate.get("candidate_id"),
            "fetch_id": fetch_id,
            "evidence_image_id": None,
            "hash": None,
            "phash": None,
        }
        response = _fetch_image(
            image_url,
            transport=transport,
            max_image_bytes=max_image_bytes,
            timeout_seconds=timeout_seconds,
        )
        if response.error_code:
            candidate["candidate_status"] = "fetch_failed"
            candidate["rejection_reason"] = response.error_code
            fetch_records.append(
                _complete_fetch_record(
                    base_record,
                    fetch_status="failed",
                    failure_code=response.error_code,
                    http_status=response.status_code,
                    normalized_url=normalized_url,
                )
            )
            continue
        content = response.content or b""
        mime_type = _sniff_mime_type(content, response.mime_type, image_url)
        byte_size = len(content)
        width, height = _image_dimensions(content, mime_type)
        if not width:
            width = _int_or_none(candidate.get("width")) or 0
        if not height:
            height = _int_or_none(candidate.get("height")) or 0
        content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
        phash = _perceptual_hash(
            content=content,
            mime_type=mime_type,
            width=width,
            height=height,
            hint=_string(candidate.get("phash_hint")),
        )
        candidate.update(
            {
                "mime_type": mime_type,
                "byte_size": byte_size,
                "artifact_size_bytes": byte_size,
                "width": width,
                "height": height,
                "hash": content_hash,
                "phash": phash,
                "normalized_image_url": normalized_url,
                "http_status": response.status_code,
            }
        )
        if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
            candidate["candidate_status"] = "fetch_failed"
            candidate["rejection_reason"] = "unsupported_mime"
            fetch_records.append(
                _complete_fetch_record(
                    base_record,
                    fetch_status="unsupported_mime",
                    failure_code="unsupported_mime",
                    http_status=response.status_code,
                    mime_type=mime_type,
                    byte_size=byte_size,
                    width=width,
                    height=height,
                    content_hash=content_hash,
                    phash=phash,
                    normalized_url=normalized_url,
                )
            )
            continue
        if byte_size > max_image_bytes:
            candidate["candidate_status"] = "fetch_failed"
            candidate["rejection_reason"] = "too_large"
            fetch_records.append(
                _complete_fetch_record(
                    base_record,
                    fetch_status="too_large",
                    failure_code="too_large",
                    http_status=response.status_code,
                    mime_type=mime_type,
                    byte_size=byte_size,
                    width=width,
                    height=height,
                    content_hash=content_hash,
                    phash=phash,
                    normalized_url=normalized_url,
                )
            )
            continue
        hash_target = seen_hashes.get(content_hash)
        if hash_target is not None:
            _mark_deduped_candidate(candidate, hash_target, "duplicate_content_hash", content_hash)
            fetch_records.append(
                _complete_fetch_record(
                    base_record,
                    fetch_status="deduped",
                    failure_code="duplicate_content_hash",
                    http_status=response.status_code,
                    mime_type=mime_type,
                    byte_size=byte_size,
                    width=width,
                    height=height,
                    content_hash=content_hash,
                    phash=phash,
                    dedupe_target=hash_target,
                    normalized_url=normalized_url,
                )
            )
            continue
        phash_target = seen_phashes.get(phash) if phash else None
        if phash_target is not None:
            _mark_deduped_candidate(candidate, phash_target, "duplicate_phash", phash or "")
            fetch_records.append(
                _complete_fetch_record(
                    base_record,
                    fetch_status="deduped",
                    failure_code="duplicate_phash",
                    http_status=response.status_code,
                    mime_type=mime_type,
                    byte_size=byte_size,
                    width=width,
                    height=height,
                    content_hash=content_hash,
                    phash=phash,
                    dedupe_target=phash_target,
                    normalized_url=normalized_url,
                )
            )
            continue
        artifact_path = _write_image_artifact(run_dir, candidate_id, mime_type, content)
        candidate["candidate_status"] = "fetched"
        candidate["local_artifact_path"] = artifact_path
        image_id = _image_id(candidate_id)
        image = _evidence_image(
            candidate=candidate,
            fetch_id=fetch_id,
            image_id=image_id,
            artifact_path=artifact_path,
            source=source,
        )
        image["cache_key"] = image_cache_key(image, source=source)
        evidence_images.append(image)
        target = _dedupe_target(candidate, fetch_id, image_id)
        seen_urls[normalized_url] = target
        seen_hashes[content_hash] = target
        if phash:
            seen_phashes[phash] = target
        fetch_records.append(
            _complete_fetch_record(
                base_record,
                fetch_status="fetched",
                http_status=response.status_code,
                mime_type=mime_type,
                byte_size=byte_size,
                width=width,
                height=height,
                content_hash=content_hash,
                phash=phash,
                local_artifact_path=artifact_path,
                evidence_image_id=image_id,
                normalized_url=normalized_url,
            )
        )
    return fetch_records, evidence_images


def _base_fetch_record(candidate: Mapping[str, Any], fetch_id: str) -> dict[str, Any]:
    return {
        "fetch_id": fetch_id,
        "candidate_id": candidate.get("candidate_id"),
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
        "policy_decision": candidate.get("policy_decision", "allowed"),
        "policy_flags": list(candidate.get("policy_flags", []))
        if isinstance(candidate.get("policy_flags"), list)
        else [],
        "estimated_cost_usd": candidate.get("estimated_cost_usd", 0.0),
        "actual_cost_usd": candidate.get("actual_cost_usd", 0.0),
    }


def _complete_fetch_record(
    base: Mapping[str, Any],
    *,
    fetch_status: str,
    http_status: int | None = None,
    mime_type: str | None = None,
    byte_size: int | None = None,
    width: int | None = None,
    height: int | None = None,
    content_hash: str | None = None,
    phash: str | None = None,
    local_artifact_path: str | None = None,
    evidence_image_id: str | None = None,
    policy_decision: str | None = None,
    failure_code: str | None = None,
    dedupe_target: Mapping[str, Any] | None = None,
    normalized_url: str | None = None,
) -> dict[str, Any]:
    record = dict(base)
    record.update(
        {
            "fetch_status": fetch_status,
            "http_status": http_status,
            "mime_type": mime_type,
            "byte_size": byte_size,
            "width": width,
            "height": height,
            "hash": content_hash,
            "phash": phash,
            "local_artifact_path": local_artifact_path,
            "evidence_image_id": evidence_image_id,
            "policy_decision": policy_decision or base.get("policy_decision", "allowed"),
            "failure_code": failure_code,
        }
    )
    if normalized_url:
        record["normalized_image_url"] = normalized_url
    if dedupe_target is not None:
        record["dedupe_target_candidate_id"] = dedupe_target.get("candidate_id")
        record["dedupe_target_fetch_id"] = dedupe_target.get("fetch_id")
        record["dedupe_target_image_id"] = dedupe_target.get("evidence_image_id")
        record["dedupe_target_hash"] = dedupe_target.get("hash")
        record["dedupe_target_phash"] = dedupe_target.get("phash")
        record["dedupe_target_reason"] = failure_code
    return record


def _evidence_image(
    *,
    candidate: Mapping[str, Any],
    fetch_id: str,
    image_id: str,
    artifact_path: str,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    observations = []
    if candidate.get("alt_text"):
        observations.append(f"Image alt text: {candidate['alt_text']}")
    if candidate.get("caption_text"):
        observations.append(f"Image caption: {candidate['caption_text']}")
    if candidate.get("surrounding_text"):
        observations.append(f"Page context: {candidate['surrounding_text']}")
    return {
        "id": image_id,
        "task_id": candidate.get("task_id"),
        "angle_id": candidate.get("angle_id"),
        "candidate_id": candidate.get("candidate_id"),
        "fetch_id": fetch_id,
        "source_id": candidate.get("source_id"),
        "origin": "page_image",
        "candidate_origin": candidate.get("candidate_origin") or candidate.get("origin"),
        "image_url": candidate.get("image_url"),
        "page_url": candidate.get("page_url"),
        "local_artifact_path": artifact_path,
        "mime_type": candidate.get("mime_type"),
        "artifact_size_bytes": candidate.get("byte_size"),
        "width": candidate.get("width") or 0,
        "height": candidate.get("height") or 0,
        "hash": candidate.get("hash"),
        "phash": candidate.get("phash"),
        "ocr_text": None,
        "observations": observations,
        "inferences": [],
        "visual_tasks": [],
        "analysis_provider": "codex-interactive",
        "analysis_status": "skipped",
        "provider": candidate.get("provider"),
        "provider_kind": candidate.get("provider_kind"),
        "provider_mode": candidate.get("provider_mode"),
        "provider_run_id": candidate.get("provider_run_id"),
        "provider_provenance": dict(candidate.get("provider_provenance", {}))
        if isinstance(candidate.get("provider_provenance"), Mapping)
        else {},
        "policy_decision": candidate.get("policy_decision", "allowed"),
        "policy_flags": list(candidate.get("policy_flags", []))
        if isinstance(candidate.get("policy_flags"), list)
        else [],
        "license_policy": source.get("license_policy", "unknown"),
        "robots_policy": source.get("robots_policy", "unknown"),
        "estimated_cost_usd": candidate.get("estimated_cost_usd", 0.0),
        "actual_cost_usd": candidate.get("actual_cost_usd", 0.0),
        "cost_usd": candidate.get("actual_cost_usd", 0.0),
        "caveats": [],
    }


def _fetch_image(
    url: str,
    *,
    transport: ImageTransport | None,
    max_image_bytes: int,
    timeout_seconds: float,
) -> FetchResponse:
    if transport is not None:
        try:
            return transport(url)
        except Exception as exc:  # pragma: no cover - exercised through public result
            return FetchResponse(
                content=None,
                mime_type=None,
                status_code=None,
                final_url=url,
                error_code=f"fetch_failed:{exc.__class__.__name__}",
            )
    try:
        return _default_fetch_image(
            url,
            timeout_seconds=timeout_seconds,
            max_image_bytes=max_image_bytes,
        )
    except (OSError, ValueError, HTTPError, URLError) as exc:
        status = exc.code if isinstance(exc, HTTPError) else None
        return FetchResponse(
            content=None,
            mime_type=None,
            status_code=status,
            final_url=url,
            error_code=f"fetch_failed:{exc.__class__.__name__}",
        )


def _default_fetch_image(
    url: str,
    *,
    timeout_seconds: float,
    max_image_bytes: int,
) -> FetchResponse:
    parsed = urlparse(url)
    if parsed.scheme == "data":
        header, _, payload = url.partition(",")
        mime_type = "text/plain"
        if header.startswith("data:"):
            mime_type = header[5:].split(";", 1)[0] or "text/plain"
        content = _decode_data_payload_capped(
            header=header,
            payload=payload,
            max_image_bytes=max_image_bytes,
        )
        return FetchResponse(
            content=content,
            mime_type=mime_type,
            status_code=None,
            final_url=url,
        )
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
        return FetchResponse(
            content=_read_capped_file(path, max_image_bytes),
            mime_type=mimetypes.guess_type(path.name)[0],
            status_code=None,
            final_url=path.resolve().as_uri(),
        )
    if parsed.scheme in {"http", "https"}:
        if _is_private_or_reserved_http_url(url, resolve_host=True):
            raise ValueError("private_network_url")
        request = Request(url, headers={"User-Agent": "codex-deepresearch/0"})
        opener = build_opener(_PrivateNetworkBlockingRedirectHandler())
        with opener.open(request, timeout=timeout_seconds) as response:
            content = response.read(max_image_bytes + 1)
            mime_type = response.headers.get_content_type()
            status_code = getattr(response, "status", None)
            final_url = response.geturl()
        return FetchResponse(
            content=content,
            mime_type=mime_type,
            status_code=status_code,
            final_url=final_url,
        )
    path = Path(url)
    if path.is_file():
        return FetchResponse(
            content=_read_capped_file(path, max_image_bytes),
            mime_type=mimetypes.guess_type(path.name)[0],
            status_code=None,
            final_url=path.resolve().as_uri(),
        )
    raise ValueError(f"unsupported image URL: {url}")


def _read_capped_file(path: Path, max_image_bytes: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(max_image_bytes + 1)


def _decode_data_payload_capped(
    *,
    header: str,
    payload: str,
    max_image_bytes: int,
) -> bytes:
    if ";base64" in header.lower():
        normalized = "".join(payload.split())
        required_chars = ((max_image_bytes + 1 + 2) // 3) * 4
        segment = normalized[:required_chars]
        segment += "=" * (-len(segment) % 4)
        return base64.b64decode(segment)[: max_image_bytes + 1]

    result = bytearray()
    index = 0
    while index < len(payload) and len(result) <= max_image_bytes:
        end = min(len(payload), index + 4096)
        chunk = payload[index:end]
        while chunk.endswith("%") or re.search(r"%[0-9A-Fa-f]$", chunk):
            end -= 1
            chunk = payload[index:end]
        if not chunk:
            chunk = payload[index : min(len(payload), index + 1)]
            end = index + len(chunk)
        result.extend(unquote_to_bytes(chunk))
        index = end
    return bytes(result[: max_image_bytes + 1])


class _PrivateNetworkBlockingRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, Any],
        newurl: str,
    ) -> Request | None:
        redirect_url = urljoin(req.full_url, newurl)
        if _is_private_or_reserved_http_url(redirect_url, resolve_host=True):
            raise ValueError("private_network_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _visual_search_plan(
    *,
    run_dir: Path,
    evidence: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    provider_mode: str,
    created_at: str,
    max_fetches: int,
) -> dict[str, Any]:
    task_keys: dict[tuple[str, str, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = (
            str(candidate.get("plan_id")),
            str(candidate.get("task_id")),
            str(candidate.get("angle_id")),
        )
        task_keys.setdefault(
            key,
            {
                "plan_id": candidate.get("plan_id"),
                "task_id": candidate.get("task_id"),
                "angle_id": candidate.get("angle_id"),
                "route": candidate.get("route") or "visual_required",
                "target_evidence_type": "page_image",
                "query": str(evidence.get("question") or ""),
                "providers": [PROVIDER_NAME],
                "source_search_result_ids": [],
                "caps": {
                    "max_candidates": len(candidates),
                    "max_fetches": max_fetches,
                    "max_vlm_images": max_fetches,
                    "max_cost_usd": _budget_cost_cap(evidence),
                },
                "policy_constraints": {
                    "provider_mode": provider_mode,
                    "license_policy": "allowed",
                    "robots_policy": "allowed",
                    "policy_decision": "allowed",
                },
                "estimated_cost_usd": 0.0,
                "state": "completed",
            },
        )
        result_id = candidate.get("source_search_result_id")
        if isinstance(result_id, str) and result_id:
            refs = task_keys[key]["source_search_result_ids"]
            if result_id not in refs:
                refs.append(result_id)
    return {
        "schema_version": VISUAL_ARTIFACT_SCHEMA_VERSION,
        "run_id": str(evidence.get("run_id") or run_dir.name),
        "created_at": created_at,
        "tasks": list(task_keys.values()),
    }


def _visual_provider_status(
    *,
    run_dir: Path,
    provider_mode: str,
    created_at: str,
    candidates: Sequence[Mapping[str, Any]],
    fetch_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    fetched = [record for record in fetch_records if record.get("fetch_status") == "fetched"]
    statuses = {
        str(record.get("fetch_status"))
        for record in fetch_records
        if isinstance(record.get("fetch_status"), str)
    }
    status = "page_images_processed"
    ok = True
    terminal = False
    metric = "page_extraction_fetch_cache"
    if candidates and not fetched and statuses and statuses <= {"policy_blocked"}:
        status = "policy_blocked_visual"
        ok = True
        terminal = True
        metric = "excluded_policy_blocked"
    elif candidates and not fetched and "budget_pruned" in statuses:
        status = "budget_pruned_visual"
        ok = False
        terminal = True
        metric = "included_failure"
    elif candidates and not fetched and statuses:
        status = "partial_auto_visual"
        ok = False
        terminal = True
        metric = "included_failure"
    return {
        "schema_version": VISUAL_PROVIDER_STATUS_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "status": status,
        "ok": ok,
        "terminal": terminal,
        "created_at": created_at,
        "metric_classification": metric,
        "providers": [
            {
                "provider": PROVIDER_NAME,
                "provider_kind": "page_extractor",
                "provider_mode": provider_mode,
                "configured": True,
                "available": True,
                "blocked_reason": None,
                "invocations": 1,
                "candidates_discovered": len(candidates),
                "artifacts_fetched": len(fetched),
                "vlm_images_analyzed": 0,
                "estimated_cost_usd": 0.0,
                "actual_cost_usd": 0.0,
                "last_error": None,
            }
        ],
        "diagnostics": {"actionable_cause": "page image extraction and fetch/cache completed"},
        "artifacts": {
            "visual_search_plan": str(run_dir / VISUAL_SEARCH_PLAN_FILENAME),
            "visual_candidates": str(run_dir / VISUAL_CANDIDATES_FILENAME),
            "image_fetch_status": str(run_dir / IMAGE_FETCH_STATUS_FILENAME),
            "visual_provider_status": str(run_dir / VISUAL_PROVIDER_STATUS_FILENAME),
        },
    }


def _source_html_path(run_dir: Path, source: Mapping[str, Any]) -> Path | None:
    local_path = source.get("local_artifact_path")
    if not isinstance(local_path, str) or not local_path:
        return None
    path = Path(local_path)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        return None
    candidate = (run_dir / path).resolve(strict=False)
    try:
        candidate.relative_to(run_dir.resolve())
    except ValueError:
        return None
    if candidate.is_file() and candidate.suffix.lower() in {".html", ".htm"}:
        return candidate
    return None


def _source_route(
    source: Mapping[str, Any],
    routes_by_angle: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    angle_id = _string(source.get("angle_id"))
    if angle_id and angle_id in routes_by_angle:
        return routes_by_angle[angle_id]
    if routes_by_angle:
        return next(iter(routes_by_angle.values()))
    return {
        "id": angle_id or "angle_001",
        "modality": _string(source.get("route")) or "visual_required",
        "visual_tasks": [],
        "max_images": 0,
    }


def _routes_by_angle(evidence: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    routes = evidence.get("routing", [])
    if not isinstance(routes, list):
        return {}
    result = {}
    for route in routes:
        if isinstance(route, Mapping) and isinstance(route.get("id"), str):
            result[str(route["id"])] = route
    return result


def _visual_task_by_angle(run_dir: Path) -> dict[str, Mapping[str, Any]]:
    path = run_dir / "visual_tasks.json"
    if not path.exists():
        return {}
    payload = _read_json(path)
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        return {}
    result = {}
    for task in tasks:
        if isinstance(task, Mapping) and isinstance(task.get("angle_id"), str):
            result[str(task["angle_id"])] = task
    return result


def _source_policy(source: Mapping[str, Any]) -> tuple[str, list[str]]:
    source_decision = _string(source.get("policy_decision")) or "allowed"
    flags = [
        str(flag)
        for flag in source.get("policy_flags", [])
        if isinstance(flag, str) and flag
    ]
    license_policy = _string(source.get("license_policy")) or "unknown"
    robots_policy = _string(source.get("robots_policy")) or "unknown"
    if robots_policy == "disallowed":
        flags.append("robots_disallowed")
    elif robots_policy == "manual_review":
        flags.append("robots_manual_review")
    if license_policy == "restricted":
        flags.append("copyright_restricted")
    elif license_policy == "manual_review":
        flags.append("copyright_manual_review")
    flags.extend(_source_metadata_policy_flags(source))
    flags = _dedupe(flags)
    if (
        source_decision == "blocked"
        or bool(set(flags).intersection(SOURCE_IMAGE_FETCH_BLOCKING_POLICY_FLAGS))
        or (
            source.get("retrieval_status") == "failed"
            and str(source.get("retrieval_error", "")).startswith(
                _POLICY_BLOCKED_RETRIEVAL_ERROR_PREFIXES
            )
        )
    ):
        return "blocked", _dedupe(flags or ["policy_blocked"])
    if (
        source_decision == "manual_review"
        or bool(set(flags).intersection(SOURCE_IMAGE_FETCH_MANUAL_REVIEW_POLICY_FLAGS))
    ):
        return "manual_review", flags
    return "allowed", flags


def _write_image_artifact(
    run_dir: Path,
    candidate_id: str,
    mime_type: str,
    content: bytes,
) -> str:
    suffix = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(mime_type, mimetypes.guess_extension(mime_type) or ".img")
    relative = Path("images") / f"{_safe_id(candidate_id)}{suffix}"
    path = run_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return relative.as_posix()


def _replace_page_extractor_images(
    images: list[Any],
    new_images: Sequence[Mapping[str, Any]],
) -> None:
    kept = [
        image
        for image in images
        if not (isinstance(image, Mapping) and image.get("provider") == PROVIDER_NAME)
    ]
    images[:] = kept + [dict(image) for image in new_images]


def _sniff_mime_type(content: bytes, declared: str | None, url: str) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WEBP":
        return "image/webp"
    declared_type = declared.split(";", 1)[0].strip().lower() if declared else None
    if declared:
        if declared_type in SUPPORTED_IMAGE_MIME_TYPES:
            return "application/octet-stream"
        return declared_type or "application/octet-stream"
    guessed, _ = mimetypes.guess_type(url)
    if guessed in SUPPORTED_IMAGE_MIME_TYPES:
        return "application/octet-stream"
    return guessed or "application/octet-stream"


def _image_dimensions(content: bytes, mime_type: str) -> tuple[int, int]:
    try:
        if (
            mime_type == "image/png"
            and len(content) >= 24
            and content.startswith(b"\x89PNG\r\n\x1a\n")
        ):
            return struct.unpack(">II", content[16:24])
        if (
            mime_type == "image/gif"
            and len(content) >= 10
            and content.startswith((b"GIF87a", b"GIF89a"))
        ):
            return struct.unpack("<HH", content[6:10])
        if mime_type == "image/jpeg":
            return _jpeg_dimensions(content)
    except (struct.error, ValueError):
        return (0, 0)
    return (0, 0)


def _jpeg_dimensions(content: bytes) -> tuple[int, int]:
    if not content.startswith(b"\xff\xd8"):
        return (0, 0)
    index = 2
    while index + 9 < len(content):
        if content[index] != 0xFF:
            index += 1
            continue
        marker = content[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(content):
            break
        length = struct.unpack(">H", content[index : index + 2])[0]
        if length < 2 or index + length > len(content):
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            if length >= 7:
                height, width = struct.unpack(">HH", content[index + 3 : index + 7])
                return (width, height)
        index += length
    return (0, 0)


def _perceptual_hash(
    *,
    content: bytes,
    mime_type: str,
    width: int,
    height: int,
    hint: str | None,
) -> str | None:
    if hint:
        return hint if hint.startswith("phash:") else "phash:" + hint
    if not width or not height or mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
        return None
    sample_hash = hashlib.sha256(content[:64] + content[-64:]).hexdigest()[:12]
    return f"phash:{mime_type}:{width}x{height}:{sample_hash}"


def _parse_srcset(value: str) -> list[tuple[str, str | None]]:
    result = []
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        pieces = item.split()
        result.append((pieces[0], pieces[1] if len(pieces) > 1 else None))
    return result


def _width_from_srcset_descriptor(value: str | None) -> int | None:
    if not value or not value.endswith("w"):
        return None
    return _int_or_none(value[:-1])


def _mark_deduped_candidate(
    candidate: dict[str, Any],
    target: Mapping[str, Any],
    reason: str,
    key: str,
) -> None:
    candidate["candidate_status"] = "rejected"
    candidate["rejection_reason"] = reason
    candidate["dedupe_target_candidate_id"] = target.get("candidate_id")
    candidate["dedupe_target_fetch_id"] = target.get("fetch_id")
    candidate["dedupe_target_image_id"] = target.get("evidence_image_id")
    candidate["dedupe_target_hash"] = target.get("hash")
    candidate["dedupe_target_phash"] = target.get("phash")
    candidate["dedupe_key"] = key


def _dedupe_target(
    candidate: Mapping[str, Any],
    fetch_id: str,
    image_id: str,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "fetch_id": fetch_id,
        "evidence_image_id": image_id,
        "hash": candidate.get("hash"),
        "phash": candidate.get("phash"),
    }


def _image_url_fetch_block_reason(
    url: str,
    source: Mapping[str, Any],
    *,
    run_dir: Path,
    source_html_path: Path | None,
    resolve_hosts: bool,
) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return "unsupported_url_scheme"
    if parsed.scheme in {"http", "https", "data"}:
        if (
            parsed.scheme in {"http", "https"}
            and _source_is_remote_page(source)
            and _is_private_or_reserved_http_url(url, resolve_host=resolve_hosts)
        ):
            return "private_network_url_from_remote_page"
        return None
    if parsed.scheme == "file":
        if (
            _source_allows_local_image_references(source)
            and _local_image_path_is_allowed(
                Path(unquote(parsed.path)),
                run_dir=run_dir,
                source_html_path=source_html_path,
            )
        ):
            return None
        if _source_is_remote_page(source):
            return "local_url_from_remote_page"
        return "local_file_outside_run"
    if parsed.scheme:
        return "unsupported_url_scheme"
    path = Path(url)
    if path.is_file():
        if (
            _source_allows_local_image_references(source)
            and _local_image_path_is_allowed(
                path,
                run_dir=run_dir,
                source_html_path=source_html_path,
            )
        ):
            return None
        if _source_is_remote_page(source):
            return "local_url_from_remote_page"
        return "local_file_outside_run"
    return "unsupported_url_scheme"


def _source_allows_local_image_references(source: Mapping[str, Any]) -> bool:
    return not _source_is_remote_page(source)


def _source_is_remote_page(source: Mapping[str, Any]) -> bool:
    source_url = _string(source.get("url"))
    if not source_url:
        return False
    try:
        scheme = urlparse(source_url).scheme.lower()
    except ValueError:
        return False
    source_type = _string(source.get("type")) or ""
    return scheme in {"http", "https"} and source_type not in {"local", "fixture"}


def _local_image_path_is_allowed(
    path: Path,
    *,
    run_dir: Path,
    source_html_path: Path | None,
) -> bool:
    try:
        resolved_path = path.resolve(strict=False)
    except OSError:
        return False
    roots = [run_dir.resolve()]
    if source_html_path is not None:
        roots.append(source_html_path.parent.resolve())
    return any(_path_is_relative_to(resolved_path, root) for root in roots)


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_private_or_reserved_http_url(url: str, *, resolve_host: bool = False) -> bool:
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme
        host = (parsed.hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return True
    if scheme not in {"http", "https"}:
        return False
    if not host:
        return True
    if _is_obvious_internal_hostname(host):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        for address in _numeric_hostname_addresses(host):
            if _address_is_private_or_reserved(address):
                return True
        if resolve_host and _hostname_resolves_to_private_or_reserved(host, parsed.port):
            return True
        return False
    return _address_is_private_or_reserved(address)


def _is_obvious_internal_hostname(host: str) -> bool:
    if host in _INTERNAL_HOST_EXACT_MATCHES:
        return True
    if any(host.endswith(suffix) for suffix in _INTERNAL_HOST_SUFFIXES):
        return True
    if "." not in host:
        return True
    return False


def _safe_join_url(page_url: str, image_url: str) -> str | None:
    try:
        return urljoin(page_url, image_url)
    except ValueError:
        return image_url or None


def _numeric_hostname_addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    if not re.match(r"^[0-9][0-9A-Fa-fXx.]*$", host):
        return []
    try:
        addrinfo = socket.getaddrinfo(
            host,
            None,
            type=socket.SOCK_STREAM,
            flags=socket.AI_NUMERICHOST,
        )
    except socket.gaierror:
        return []
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for item in addrinfo:
        sockaddr = item[4]
        if not sockaddr:
            continue
        try:
            addresses.append(ipaddress.ip_address(str(sockaddr[0])))
        except ValueError:
            continue
    return addresses


def _hostname_resolves_to_private_or_reserved(host: str, port: int | None) -> bool:
    try:
        addrinfo = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    except OSError:
        return True
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for item in addrinfo:
        sockaddr = item[4]
        if not sockaddr:
            continue
        try:
            addresses.append(ipaddress.ip_address(str(sockaddr[0])))
        except ValueError:
            return True
    if not addresses:
        return True
    return any(_address_is_private_or_reserved(address) for address in addresses)


def _address_is_private_or_reserved(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _max_fetches(evidence: Mapping[str, Any], explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    budget = evidence.get("budget")
    if isinstance(budget, Mapping):
        value = budget.get("max_images")
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
    route_max = [
        max(0, int(route.get("max_images", 0)))
        for route in evidence.get("routing", [])
        if isinstance(route, Mapping)
        and isinstance(route.get("max_images", 0), int)
        and not isinstance(route.get("max_images", 0), bool)
    ]
    if route_max:
        return sum(route_max)
    return 12


def _budget_cost_cap(evidence: Mapping[str, Any]) -> float:
    budget = evidence.get("budget")
    if isinstance(budget, Mapping):
        value = budget.get("max_cost_usd")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def _candidate_id(*, source_id: str, origin: str, image_url: str, ordinal: int) -> str:
    digest = hashlib.sha1(f"{source_id}|{origin}|{image_url}|{ordinal}".encode("utf-8")).hexdigest()
    return "cand_page_" + digest[:16]


def _fetch_id(candidate_id: str) -> str:
    return "fetch_" + _safe_id(candidate_id.removeprefix("cand_"))


def _image_id(candidate_id: str) -> str:
    return "img_" + _safe_id(candidate_id.removeprefix("cand_"))


def _task_id_for_angle(angle_id: Any) -> str:
    raw = _string(angle_id) or "angle_001"
    return "task_visual_" + _safe_id(raw.removeprefix("angle_") or raw)


def _score_from_rank(rank: int) -> float:
    return round(1.0 / rank, 6) if rank > 0 else 0.0


def _join_context(*parts: str | None) -> str | None:
    value = _compact_whitespace(" ".join(part for part in parts if part))
    return value or None


def _compact_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _int_or_none(value: Any) -> int | None:
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _string(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _source_metadata_policy_flags(source: Mapping[str, Any]) -> list[str]:
    flags: list[str] = []
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
    if _truthy_metadata(source, "contains_pii", "pii_detected") or _metadata_contains(
        source, "pii"
    ):
        flags.append("pii_detected")
    return flags


def _truthy_metadata(record: Mapping[str, Any], *keys: str) -> bool:
    metadata = record.get("raw_provider_metadata")
    for key in keys:
        if _is_truthy(record.get(key)):
            return True
        if isinstance(metadata, Mapping) and _is_truthy(metadata.get(key)):
            return True
    return False


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _metadata_contains(record: Mapping[str, Any], needle: str) -> bool:
    metadata = record.get("raw_provider_metadata")
    if not isinstance(metadata, Mapping):
        return False
    lowered = needle.lower()
    values: list[Any] = list(metadata.values())
    while values:
        value = values.pop()
        if isinstance(value, Mapping):
            values.extend(value.values())
        elif isinstance(value, list):
            values.extend(value)
        elif lowered in str(value).lower():
            return True
    return False


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return safe or "image"


def _dedupe(values: Sequence[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _count_by(records: Sequence[Mapping[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = str(record.get(field) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PageImageExtractionError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PageImageExtractionError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PageImageExtractionError(f"JSON document must be an object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
