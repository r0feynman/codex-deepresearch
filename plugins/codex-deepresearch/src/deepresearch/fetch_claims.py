"""Deterministic source fetch and first-pass claim extraction."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from .cache_keys import claim_cache_key, source_cache_key
from .evidence_schema import validate_artifacts
from .run_state import begin_stage, skipped_stage_status
from .search_handoff import SearchHandoffError, resolve_run_dir
from .trace import record_stage_trace


FETCH_CLAIMS_SCHEMA_VERSION = "codex-deepresearch.fetch-claims.v0"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_QUOTE_CANDIDATES_PER_SOURCE = 3
SOURCE_FETCH_BLOCKING_POLICY_FLAGS = {
    "access_controlled",
    "captcha_protected",
    "copyright_restricted",
    "login_gated",
    "paywall",
    "pii_detected",
    "robots_disallowed",
}
SOURCE_FETCH_MANUAL_REVIEW_POLICY_FLAGS = {
    "copyright_manual_review",
    "robots_manual_review",
}


class FetchClaimsError(ValueError):
    """Raised when source fetch and claim extraction cannot continue."""


@dataclass(frozen=True)
class _FetchedResource:
    content: bytes
    mime_type: str
    status_code: int | None
    final_url: str


@dataclass(frozen=True)
class _TextExtraction:
    title: str | None
    text: str
    excerpt: str
    quote_candidates: tuple[str, ...]
    caveats: tuple[str, ...] = ()


class _HTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
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
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._chunks: list[str] = []

    @property
    def title(self) -> str | None:
        title = _compact_whitespace(" ".join(self._title_parts))
        return title or None

    @property
    def text(self) -> str:
        text = "".join(self._chunks)
        lines = [_compact_whitespace(line) for line in text.splitlines()]
        return "\n".join(line for line in lines if line)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        self._chunks.append(data)


def fetch_claims(
    *,
    run: str | Path,
    runs_dir: str | Path | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Fetch queued sources, preserve artifacts, and append low-confidence claims."""

    if timeout_seconds <= 0:
        raise FetchClaimsError("timeout_seconds must be positive")

    run_dir = resolve_run_dir(run, runs_dir=runs_dir)
    start = begin_stage(run_dir, "fetch_claims")
    if start.skipped:
        status = skipped_stage_status(
            run_dir,
            stage="fetch_claims",
            schema_version=FETCH_CLAIMS_SCHEMA_VERSION,
            status_artifact_key="fetch_claims_status",
            status_filename="fetch_claims_status.json",
            reason=start.skip_reason or "stage_already_completed",
        )
        record_stage_trace(
            run_dir,
            stage="fetch_claims",
            agent_role="fetch_claims_agent",
            status_payload=status,
            prompt_summary="Fetch queued sources and extract low-confidence source-linked claims.",
            tool_call_summary="Skipped fetch and claim extraction because run_steps.json marks the stage terminal.",
        )
        _write_run_json(run_dir, "fetch_claims_status.json", status)
        return status
    evidence_path = run_dir / "evidence.json"
    fetch_queue_path = run_dir / "fetch_queue.json"
    if not evidence_path.exists():
        raise FetchClaimsError(f"missing evidence.json in run directory: {run_dir}")
    if not fetch_queue_path.exists():
        status = _base_status(run_dir, "blocked_missing_fetch_queue")
        status["errors"] = [{"code": "missing_fetch_queue", "status": "blocked"}]
        status["artifacts"] = {
            "evidence": str(evidence_path),
            "fetch_claims_status": str(run_dir / "fetch_claims_status.json"),
        }
        record_stage_trace(
            run_dir,
            stage="fetch_claims",
            agent_role="fetch_claims_agent",
            status_payload=status,
            prompt_summary="Fetch queued sources and extract low-confidence source-linked claims.",
            tool_call_summary="Checked for fetch_queue.json before fetching source bodies.",
        )
        _write_run_json(run_dir, "fetch_claims_status.json", status)
        return status

    evidence = _read_json(evidence_path)
    fetch_queue = _read_json(fetch_queue_path)
    sources = evidence.get("sources", [])
    if not isinstance(sources, list):
        raise FetchClaimsError("evidence.sources must be a list")
    entries = fetch_queue.get("entries", [])
    if not isinstance(entries, list):
        raise FetchClaimsError("fetch_queue.entries must be a list")

    now = _utc_now()
    source_by_id = {
        source.get("id"): source
        for source in sources
        if isinstance(source, dict) and isinstance(source.get("id"), str)
    }
    evidence.setdefault("quote_candidates", [])
    evidence.setdefault("claims", [])
    if not isinstance(evidence["quote_candidates"], list):
        raise FetchClaimsError("evidence.quote_candidates must be a list")
    if not isinstance(evidence["claims"], list):
        raise FetchClaimsError("evidence.claims must be a list")
    quote_candidates: list[dict[str, Any]] = evidence["quote_candidates"]
    claims: list[dict[str, Any]] = evidence["claims"]

    artifacts_dir = _resolve_run_relative_path(run_dir, "sources/artifacts")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, Any]] = []
    fetched_count = 0
    reused_count = 0
    partial_count = 0
    failed_count = 0
    created_quote_count = 0
    created_claim_count = 0

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append({"code": "invalid_fetch_queue_entry", "entry_index": index})
            continue
        source_id = entry.get("source_id")
        source = source_by_id.get(source_id)
        if not isinstance(source_id, str) or source is None:
            errors.append(
                {
                    "code": "unknown_source_id",
                    "entry_index": index,
                    "source_id": source_id,
                    "status": "failed",
                }
            )
            entry["retrieval_status"] = "failed"
            continue

        content_hash_proven_current = _attach_current_source_content_metadata(source, entry)
        previous_source_cache_key = source.get("cache_key")
        current_source_cache_key = source_cache_key(source, entry)
        source["cache_key"] = current_source_cache_key
        entry["cache_key"] = current_source_cache_key
        if _can_reuse_completed_source(
            run_dir,
            source,
            current_source_cache_key,
            previous_cache_key=previous_source_cache_key,
            content_hash_proven_current=content_hash_proven_current,
        ):
            _refresh_fetch_claim_cache_keys(
                evidence,
                source=source,
                source_by_id=source_by_id,
            )
            entry["retrieval_status"] = source.get("retrieval_status", "fetched")
            if isinstance(source.get("local_artifact_path"), str):
                entry["local_artifact_path"] = source["local_artifact_path"]
            source_quote_count = _existing_quote_count(quote_candidates, source_id)
            source_claim_count = _existing_claim_count(claims, source_id)
            entry["quote_candidate_count"] = source_quote_count
            entry["claim_count"] = source_claim_count
            _write_source_metadata(run_dir, source)
            reused_count += 1
            continue

        _remove_fetch_claim_records(evidence, source_id)

        if _policy_decision(source, entry) != "allowed":
            _mark_policy_blocked(source, entry, now)
            _write_source_metadata(run_dir, source)
            failed_count += 1
            continue

        try:
            fetched = _fetch_url(str(entry.get("url") or source.get("url")), timeout_seconds)
        except (OSError, HTTPError, URLError, ValueError) as exc:
            _mark_failed(source, entry, now, f"fetch_failed: {exc}")
            _write_source_metadata(run_dir, source)
            errors.append(
                {
                    "code": "fetch_failed",
                    "entry_index": index,
                    "source_id": source_id,
                    "status": "failed",
                    "detail": str(exc),
                }
            )
            failed_count += 1
            continue

        source_type = str(source.get("type", "web"))
        raw_path = _artifact_path(source_id, source_type, fetched.mime_type)
        _write_run_bytes(run_dir, raw_path, fetched.content)
        extraction = _extract_text(
            content=fetched.content,
            mime_type=fetched.mime_type,
            source_type=source_type,
        )

        source["metadata_artifact_path"] = _metadata_path(source)
        source["local_artifact_path"] = raw_path
        source["artifact_mime_type"] = fetched.mime_type
        source["artifact_sha256"] = "sha256:" + hashlib.sha256(fetched.content).hexdigest()
        source["content_sha256"] = source["artifact_sha256"]
        entry["content_sha256"] = source["artifact_sha256"]
        current_source_cache_key = source_cache_key(source, entry)
        source["cache_key"] = current_source_cache_key
        entry["cache_key"] = current_source_cache_key
        source["fetched_at"] = now
        source["accessed_at"] = now
        source["final_url"] = fetched.final_url
        source["http_status"] = fetched.status_code
        source["body_excerpt"] = extraction.excerpt
        source["quote_candidate_ids"] = []
        if extraction.title:
            source["title"] = extraction.title
        if extraction.caveats:
            source["caveats"] = sorted(set([*source.get("caveats", []), *extraction.caveats]))

        if extraction.text:
            text_path = _text_artifact_path(source_id)
            _write_run_text(run_dir, text_path, extraction.text + "\n")
            source["text_artifact_path"] = text_path

        if extraction.quote_candidates:
            source["retrieval_status"] = "fetched"
            fetched_count += 1
        else:
            source["retrieval_status"] = "partial"
            partial_count += 1

        source_quote_count = 0
        source_claim_count = 0
        for quote_index, quote in enumerate(extraction.quote_candidates, start=1):
            quote_id = _quote_id(source_id, quote_index)
            candidate = {
                "id": quote_id,
                "source_id": source_id,
                "quote": quote,
                "location": f"paragraph {quote_index}",
                "extracted_at": now,
                "extraction_stage": "fetch_claims",
                "confidence": "low",
                "source_cache_key": current_source_cache_key,
            }
            quote_candidates.append(candidate)
            source["quote_candidate_ids"].append(quote_id)
            source_quote_count += 1

            claim = {
                "id": _claim_id(source_id, quote_index),
                "text": quote,
                "claim_type": "text",
                "supporting_sources": [source_id],
                "supporting_images": [],
                "quote_spans": [
                    {
                        "source_id": source_id,
                        "quote": quote,
                        "location": candidate["location"],
                    }
                ],
                "votes": [],
                "verification_status": "unverified",
                "review_status": "not_reviewed",
                "promotion_status": "not_eligible",
                "confidence": "low",
                "caveats": ["Automatically extracted from fetched source text; not verified."],
                "quote_candidate_id": quote_id,
                "extraction_stage": "fetch_claims",
                "source_cache_key": current_source_cache_key,
            }
            claim["cache_key"] = claim_cache_key(
                claim,
                sources_by_id={source_id: source},
            )
            claims.append(claim)
            source_claim_count += 1

        created_quote_count += source_quote_count
        created_claim_count += source_claim_count
        entry["retrieval_status"] = source["retrieval_status"]
        entry["local_artifact_path"] = source["local_artifact_path"]
        entry["quote_candidate_count"] = source_quote_count
        entry["claim_count"] = source_claim_count
        _write_source_metadata(run_dir, source)

    evidence["fetch_claims"] = {
        "schema_version": FETCH_CLAIMS_SCHEMA_VERSION,
        "status": "completed_with_errors" if errors else "completed",
        "fetched_at": now,
        "fetch_queue_path": "fetch_queue.json",
        "sources_reused": reused_count,
        "quote_candidates_created": created_quote_count,
        "claims_created": created_claim_count,
        "high_confidence_claims_created": 0,
    }
    _write_run_json(run_dir, "evidence.json", evidence)
    _write_run_json(run_dir, "fetch_queue.json", fetch_queue)

    validation = validate_artifacts(evidence_path=evidence_path)
    status = _base_status(run_dir, "completed_with_errors" if errors else "completed")
    status.update(
        {
            "validation": validation.to_dict(),
            "sources_reused": reused_count,
            "sources_fetched": fetched_count,
            "sources_partial": partial_count,
            "sources_failed": failed_count,
            "quote_candidates_created": created_quote_count,
            "claims_created": created_claim_count,
            "high_confidence_claims_created": 0,
            "errors": errors,
            "artifacts": {
                "evidence": str(evidence_path),
                "fetch_queue": str(fetch_queue_path),
                "fetch_claims_status": str(run_dir / "fetch_claims_status.json"),
            },
        }
    )
    if not validation.valid:
        status["status"] = "failed_validation"
    record_stage_trace(
        run_dir,
        stage="fetch_claims",
        agent_role="fetch_claims_agent",
        status_payload=status,
        prompt_summary="Fetch queued sources and extract low-confidence source-linked claims.",
        tool_call_summary="Reused completed source cache hits, fetched changed source bodies, preserved local artifacts, and extracted quote candidates.",
    )
    _write_run_json(run_dir, "fetch_claims_status.json", status)
    return status


def _fetch_url(url: str, timeout_seconds: float) -> _FetchedResource:
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"}:
        request: str | Request = Request(url, headers={"User-Agent": "codex-deepresearch/0.1"})
    elif parsed.scheme in {"file", "data"}:
        request = url
    else:
        raise ValueError(f"unsupported fetch URL scheme: {parsed.scheme or '<empty>'}")

    with urlopen(request, timeout=timeout_seconds) as response:
        status_code = getattr(response, "status", None) or getattr(response, "code", None)
        if status_code is not None and int(status_code) >= 400:
            raise ValueError(f"HTTP status {status_code}")
        content = response.read()
        headers = getattr(response, "headers", None)
        mime_type = "application/octet-stream"
        if headers is not None and hasattr(headers, "get_content_type"):
            mime_type = headers.get_content_type()
        elif parsed.scheme == "file":
            mime_type = mimetypes.guess_type(parsed.path)[0] or mime_type
        final_url = response.geturl() if hasattr(response, "geturl") else url
    return _FetchedResource(
        content=content,
        mime_type=mime_type or "application/octet-stream",
        status_code=int(status_code) if status_code is not None else None,
        final_url=final_url,
    )


def _extract_text(*, content: bytes, mime_type: str, source_type: str) -> _TextExtraction:
    if source_type == "pdf" or "pdf" in mime_type:
        return _extract_pdf_text(content)
    if "html" in mime_type or source_type == "web":
        return _extract_html_text(content)
    text = _decode_text(content)
    excerpt = _excerpt(text)
    return _TextExtraction(
        title=None,
        text=text,
        excerpt=excerpt,
        quote_candidates=tuple(_select_quote_candidates(text)),
    )


def _extract_html_text(content: bytes) -> _TextExtraction:
    html = _decode_text(content)
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    text = parser.text
    return _TextExtraction(
        title=parser.title,
        text=text,
        excerpt=_excerpt(text),
        quote_candidates=tuple(_select_quote_candidates(text)),
    )


def _extract_pdf_text(content: bytes) -> _TextExtraction:
    decoded = _decode_text(content)
    literal_strings = [
        _compact_whitespace(match)
        for match in re.findall(r"\(([^()]{20,})\)", decoded)
    ]
    printable_chunks = [
        _compact_whitespace(match)
        for match in re.findall(r"[A-Za-z0-9][A-Za-z0-9 ,.;:'\"?!%$#/@&()\-\n]{30,}", decoded)
    ]
    text = "\n".join(chunk for chunk in [*literal_strings, *printable_chunks] if chunk)
    text = _clean_pdf_text(text)
    if not _has_enough_words(text):
        return _TextExtraction(
            title=None,
            text="",
            excerpt="",
            quote_candidates=(),
            caveats=("PDF text extraction was partial; no simple embedded text was found.",),
        )
    return _TextExtraction(
        title=None,
        text=text,
        excerpt=_excerpt(text),
        quote_candidates=tuple(_select_quote_candidates(text)),
    )


def _select_quote_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for paragraph in text.splitlines():
        paragraph = _compact_whitespace(paragraph)
        if not _has_enough_words(paragraph):
            continue
        for sentence in _sentence_candidates(paragraph):
            normalized = sentence.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(sentence)
            if len(candidates) >= MAX_QUOTE_CANDIDATES_PER_SOURCE:
                return candidates
    return candidates


def _sentence_candidates(paragraph: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?])\s+", paragraph)
    sentences: list[str] = []
    for chunk in chunks:
        sentence = _compact_whitespace(chunk)
        if len(sentence) > 280:
            sentence = sentence[:277].rstrip() + "..."
        if 40 <= len(sentence) <= 280 and _has_enough_words(sentence):
            sentences.append(sentence)
    if not sentences and 40 <= len(paragraph) <= 280:
        sentences.append(paragraph)
    return sentences


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8", "windows-1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _clean_pdf_text(text: str) -> str:
    text = re.sub(r"%PDF-\d\.\d", " ", text)
    text = re.sub(r"\b\d+\s+\d+\s+obj\b|endobj|stream|endstream|xref|trailer|startxref", " ", text)
    return "\n".join(
        line
        for line in (_compact_whitespace(line) for line in text.splitlines())
        if _has_enough_words(line)
    )


def _excerpt(text: str, limit: int = 500) -> str:
    compact = _compact_whitespace(text)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _compact_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _has_enough_words(value: str) -> bool:
    return len(re.findall(r"[A-Za-z0-9]+", value)) >= 5


def _artifact_path(source_id: str, source_type: str, mime_type: str) -> str:
    extension = ".bin"
    if source_type == "pdf" or "pdf" in mime_type:
        extension = ".pdf"
    elif "html" in mime_type or source_type == "web":
        extension = ".html"
    elif "text" in mime_type:
        extension = ".txt"
    return f"sources/artifacts/{_safe_id(source_id)}{extension}"


def _text_artifact_path(source_id: str) -> str:
    return f"sources/artifacts/{_safe_id(source_id)}.txt"


def _metadata_path(source: Mapping[str, Any]) -> str:
    existing = source.get("metadata_artifact_path")
    if isinstance(existing, str) and existing:
        return existing
    local = source.get("local_artifact_path")
    if isinstance(local, str) and local.endswith(".json"):
        return local
    return f"sources/{_safe_id(str(source.get('id', 'source')))}.json"


def _safe_metadata_path(run_dir: Path, source: dict[str, Any]) -> str:
    candidate = _metadata_path(source)
    try:
        _resolve_run_relative_path(run_dir, candidate)
        source["metadata_artifact_path"] = candidate
        return candidate
    except FetchClaimsError:
        fallback = f"sources/{_safe_id(str(source.get('id', 'source')))}.json"
        _resolve_run_relative_path(run_dir, fallback)
        source["metadata_artifact_path"] = fallback
        local_artifact_path = source.get("local_artifact_path")
        if not isinstance(local_artifact_path, str) or local_artifact_path.endswith(".json"):
            source["local_artifact_path"] = fallback
        source["caveats"] = sorted(
            set(
                [
                    *source.get("caveats", []),
                    "Unsafe metadata artifact path replaced with run-local fallback.",
                ]
            )
        )
        return fallback


def _quote_id(source_id: str, index: int) -> str:
    return f"quote_{_safe_id(source_id)}_{index:03d}"


def _claim_id(source_id: str, index: int) -> str:
    return f"claim_{_safe_id(source_id)}_{index:03d}"


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return safe or "source"


def _attach_current_source_content_metadata(
    source: dict[str, Any],
    entry: dict[str, Any],
) -> bool:
    url = str(entry.get("url") or source.get("url") or "")
    parsed = urlparse(url)
    if parsed.scheme == "file":
        try:
            file_path = _file_uri_path(parsed)
            digest = "sha256:" + hashlib.sha256(file_path.read_bytes()).hexdigest()
        except (OSError, ValueError):
            source.pop("current_content_sha256", None)
            entry.pop("current_content_sha256", None)
            return False
        source["current_content_sha256"] = digest
        entry["current_content_sha256"] = digest
        return True

    explicit_hash = _first_current_content_hash(entry) or _first_current_content_hash(source)
    if explicit_hash:
        normalized_hash = _normalize_sha256(explicit_hash)
        entry["current_content_sha256"] = normalized_hash
        source["current_content_sha256"] = normalized_hash
        return True
    source.pop("current_content_sha256", None)
    entry.pop("current_content_sha256", None)
    return False


def _first_current_content_hash(record: Mapping[str, Any]) -> str | None:
    for key in ("input_content_sha256", "source_content_sha256"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _normalize_sha256(value: str) -> str:
    value = value.strip().lower()
    return value if value.startswith("sha256:") else f"sha256:{value}"


def _file_uri_path(parsed: Any) -> Path:
    if parsed.scheme != "file":
        raise ValueError("not a file URI")
    if parsed.netloc not in {"", "localhost"}:
        raise ValueError(f"unsupported file URI host: {parsed.netloc}")
    return Path(unquote(parsed.path))


def _can_reuse_completed_source(
    run_dir: Path,
    source: Mapping[str, Any],
    current_cache_key: str,
    *,
    previous_cache_key: Any,
    content_hash_proven_current: bool,
) -> bool:
    if not content_hash_proven_current:
        return False
    if isinstance(previous_cache_key, str) and previous_cache_key != current_cache_key:
        return False
    if source.get("retrieval_status") not in {"fetched", "partial"}:
        return False
    local_artifact_path = source.get("local_artifact_path")
    if not isinstance(local_artifact_path, str) or not local_artifact_path:
        return False
    if local_artifact_path == _metadata_path(source):
        return False
    try:
        return _resolve_run_relative_path(run_dir, local_artifact_path).is_file()
    except FetchClaimsError:
        return False


def _refresh_fetch_claim_cache_keys(
    evidence: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
    source_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    source_id = source.get("id")
    source_key = source.get("cache_key")
    if not isinstance(source_id, str) or not isinstance(source_key, str):
        return
    quotes = evidence.get("quote_candidates", [])
    if isinstance(quotes, list):
        for quote in quotes:
            if (
                isinstance(quote, dict)
                and quote.get("extraction_stage") == "fetch_claims"
                and quote.get("source_id") == source_id
            ):
                quote["source_cache_key"] = source_key
    claims = evidence.get("claims", [])
    if isinstance(claims, list):
        for claim in claims:
            if not _is_fetch_claim_for_source(claim, source_id):
                continue
            claim["source_cache_key"] = source_key
            claim["cache_key"] = claim_cache_key(claim, sources_by_id=source_by_id)


def _remove_fetch_claim_records(evidence: Mapping[str, Any], source_id: str) -> None:
    quotes = evidence.get("quote_candidates", [])
    if isinstance(quotes, list):
        quotes[:] = [
            quote
            for quote in quotes
            if not (
                isinstance(quote, Mapping)
                and quote.get("extraction_stage") == "fetch_claims"
                and quote.get("source_id") == source_id
            )
        ]
    claims = evidence.get("claims", [])
    if isinstance(claims, list):
        claims[:] = [
            claim
            for claim in claims
            if not _is_fetch_claim_for_source(claim, source_id)
        ]


def _is_fetch_claim_for_source(claim: Any, source_id: str) -> bool:
    return (
        isinstance(claim, Mapping)
        and claim.get("extraction_stage") == "fetch_claims"
        and source_id in _string_list(claim.get("supporting_sources"))
    )


def _existing_quote_count(quotes: list[dict[str, Any]], source_id: str) -> int:
    return sum(
        1
        for quote in quotes
        if isinstance(quote, Mapping)
        and quote.get("extraction_stage") == "fetch_claims"
        and quote.get("source_id") == source_id
    )


def _existing_claim_count(claims: list[dict[str, Any]], source_id: str) -> int:
    return sum(1 for claim in claims if _is_fetch_claim_for_source(claim, source_id))


def _policy_decision(source: Mapping[str, Any], entry: Mapping[str, Any]) -> str:
    source_decision = str(source.get("policy_decision", "allowed"))
    if source_decision in {"blocked", "manual_review"}:
        return source_decision

    flags = set(_string_list(source.get("policy_flags")))
    if flags.intersection(SOURCE_FETCH_BLOCKING_POLICY_FLAGS):
        return "blocked"
    if flags.intersection(SOURCE_FETCH_MANUAL_REVIEW_POLICY_FLAGS):
        return "manual_review"
    if source.get("robots_policy") == "disallowed":
        return "blocked"
    if source.get("robots_policy") == "manual_review":
        return "manual_review"
    if source.get("license_policy") == "restricted":
        return "blocked"
    if source.get("license_policy") == "manual_review":
        return "manual_review"
    if source.get("retrieval_status") == "failed" and str(
        source.get("retrieval_error", "")
    ).startswith(("guardrail_", "policy_")):
        return "blocked"

    entry_decision = entry.get("policy_decision", source_decision)
    return str(entry_decision)


def _mark_policy_blocked(source: dict[str, Any], entry: dict[str, Any], fetched_at: str) -> None:
    source["retrieval_status"] = "failed"
    source.setdefault("retrieval_error", "policy_blocked")
    source["fetched_at"] = fetched_at
    source["caveats"] = sorted(set([*source.get("caveats", []), "Source policy blocked fetch."]))
    entry["retrieval_status"] = "failed"
    entry["retrieval_error"] = "policy_blocked"


def _mark_failed(
    source: dict[str, Any],
    entry: dict[str, Any],
    fetched_at: str,
    error: str,
) -> None:
    source["retrieval_status"] = "failed"
    source["retrieval_error"] = error
    source["fetched_at"] = fetched_at
    entry["retrieval_status"] = "failed"
    entry["retrieval_error"] = error


def _write_source_metadata(run_dir: Path, source: Mapping[str, Any]) -> None:
    if not isinstance(source, dict):
        raise FetchClaimsError("source metadata must be a JSON object")
    metadata_path = _safe_metadata_path(run_dir, source)
    _write_run_json(run_dir, metadata_path, source)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _base_status(run_dir: Path, status: str) -> dict[str, Any]:
    run_id = run_dir.name
    try:
        evidence = _read_json(run_dir / "evidence.json")
        run_id = evidence.get("run_id", run_id)
    except FetchClaimsError:
        pass
    return {
        "schema_version": FETCH_CLAIMS_SCHEMA_VERSION,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "status": status,
        "created_at": _utc_now(),
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FetchClaimsError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FetchClaimsError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FetchClaimsError(f"expected JSON object in {path}")
    return payload


def _resolve_run_relative_path(run_dir: Path, relative_path: str | Path) -> Path:
    path = Path(relative_path)
    if path.is_absolute():
        raise FetchClaimsError(f"artifact path must be run-relative: {relative_path}")
    if not str(path) or path == Path("."):
        raise FetchClaimsError("artifact path cannot be empty")
    if any(part == ".." for part in path.parts):
        raise FetchClaimsError(f"artifact path cannot traverse outside run directory: {relative_path}")
    root = run_dir.resolve()
    target = (root / path).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise FetchClaimsError(
            f"artifact path resolves outside run directory: {relative_path}"
        ) from exc
    return target


def _write_run_bytes(run_dir: Path, relative_path: str | Path, content: bytes) -> None:
    path = _resolve_run_relative_path(run_dir, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _write_run_text(run_dir: Path, relative_path: str | Path, content: str) -> None:
    path = _resolve_run_relative_path(run_dir, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_run_json(run_dir: Path, relative_path: str | Path, payload: Mapping[str, Any]) -> None:
    _write_json(_resolve_run_relative_path(run_dir, relative_path), payload)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
