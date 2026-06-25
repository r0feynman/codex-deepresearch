"""Deterministic cache key helpers for resumable DeepResearch units."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse


CACHE_KEY_SCHEMA_VERSION = "codex-deepresearch.cache-keys.v0"


def source_cache_key(
    source: Mapping[str, Any],
    entry: Mapping[str, Any] | None = None,
) -> str:
    """Return the stable cache key for a source fetch decision."""

    entry = entry or {}
    payload = {
        "kind": "source",
        "schema_version": CACHE_KEY_SCHEMA_VERSION,
        "type": _normalize_scalar(_first_string(entry, "type") or _first_string(source, "type")),
        "url": normalize_url(_first_string(entry, "url") or _first_string(source, "url") or ""),
        "content_hash": _normalize_hash(
            _first_string(
                entry,
                "current_content_sha256",
                "input_content_sha256",
                "content_sha256",
                "source_content_sha256",
            )
            or _first_string(
                source,
                "current_content_sha256",
                "input_content_sha256",
                "content_sha256",
                "source_content_sha256",
                "artifact_sha256",
            )
            or ""
        ),
        "policy_context": {
            "license_policy": _normalize_scalar(
                _first_string(source, "license_policy")
                or _first_string(entry, "license_policy")
                or "unknown"
            ),
            "robots_policy": _normalize_scalar(
                _first_string(source, "robots_policy")
                or _first_string(entry, "robots_policy")
                or "unknown"
            ),
            "policy_decision": _normalize_scalar(
                _first_string(source, "policy_decision")
                or _first_string(entry, "policy_decision")
                or "allowed"
            ),
            "policy_flags": _normalized_string_set(
                [*_string_list(source.get("policy_flags")), *_string_list(entry.get("policy_flags"))]
            ),
        },
    }
    return _digest("source", payload)


def image_cache_key(
    image: Mapping[str, Any],
    *,
    source: Mapping[str, Any] | None = None,
) -> str:
    """Return the stable cache key for a visual evidence/image unit."""

    source = source or {}
    source_url = (
        _first_string(image, "source_url")
        or _first_string(source, "url")
        or _first_string(source, "final_url")
    )
    payload = {
        "kind": "image",
        "schema_version": CACHE_KEY_SCHEMA_VERSION,
        "mime_type": _normalize_mime(_first_string(image, "mime_type") or ""),
        "size_bytes": _integer_or_string(
            image.get("artifact_size_bytes", image.get("size_bytes", image.get("content_length")))
        ),
        "content_hash": _normalize_hash(
            _first_string(image, "hash") or _first_string(image, "sha256") or ""
        ),
        "source_url_metadata": {
            "source_url": normalize_url(source_url or ""),
            "page_url": normalize_url(_first_string(image, "page_url") or ""),
            "image_url": normalize_url(_first_string(image, "image_url") or ""),
            "local_artifact_path": _compact_whitespace(
                _first_string(image, "local_artifact_path") or ""
            ),
        },
        "verification_inputs": {
            "origin": _normalize_scalar(_first_string(image, "origin") or ""),
            "analysis_status": _normalize_scalar(
                _first_string(image, "analysis_status") or ""
            ),
            "policy_context": {
                "policy_decision": _normalize_scalar(
                    _first_string(image, "policy_decision")
                    or _first_string(source, "policy_decision")
                    or "allowed"
                ),
                "license_policy": _normalize_scalar(
                    _first_string(image, "license_policy")
                    or _first_string(source, "license_policy")
                    or "unknown"
                ),
                "robots_policy": _normalize_scalar(
                    _first_string(image, "robots_policy")
                    or _first_string(source, "robots_policy")
                    or "unknown"
                ),
                "source_policy_flags": _normalized_string_set(
                    _string_list(source.get("policy_flags"))
                ),
            },
            "policy_flags": _normalized_string_set(_string_list(image.get("policy_flags"))),
            "observations": _normalized_text_list(image.get("observations")),
            "inferences": _normalized_text_list(image.get("inferences")),
            "ocr_text": normalize_text(_first_string(image, "ocr_text") or ""),
        },
    }
    return _digest("image", payload)


def claim_cache_key(
    claim: Mapping[str, Any],
    *,
    sources_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    images_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    verification_route: str | None = None,
) -> str:
    """Return the stable cache key for a claim verification decision."""

    sources_by_id = sources_by_id or {}
    images_by_id = images_by_id or {}
    source_refs = _normalized_string_set(_string_list(claim.get("supporting_sources")))
    image_refs = _normalized_string_set(_string_list(claim.get("supporting_images")))
    payload = {
        "kind": "claim",
        "schema_version": CACHE_KEY_SCHEMA_VERSION,
        "claim_type": _normalize_scalar(_first_string(claim, "claim_type") or "text"),
        "verification_route": _normalize_scalar(verification_route or ""),
        "text": normalize_text(_first_string(claim, "text") or ""),
        "supporting_sources": [
            {
                "id": source_id,
                "cache_key": _source_ref_cache_key(source_id, sources_by_id),
            }
            for source_id in source_refs
        ],
        "supporting_images": [
            {
                "id": image_id,
                "cache_key": _image_ref_cache_key(image_id, images_by_id, sources_by_id),
            }
            for image_id in image_refs
        ],
        "visual_supports": _visual_support_inputs(claim.get("visual_supports")),
        "quote_spans": _quote_span_inputs(claim.get("quote_spans")),
    }
    return _digest("claim", payload)


def normalize_url(value: str) -> str:
    """Normalize URLs enough for deterministic cache decisions."""

    value = value.strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
    except ValueError:
        return _compact_whitespace(value)
    if not parsed.scheme:
        return _compact_whitespace(value)

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if hostname:
        try:
            port = parsed.port
        except ValueError:
            return _compact_whitespace(value)
        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            port = None
        userinfo = ""
        if parsed.username:
            userinfo = parsed.username
            if parsed.password:
                userinfo += f":{parsed.password}"
            userinfo += "@"
        netloc = f"{userinfo}{hostname}" + (f":{port}" if port is not None else "")
    elif netloc:
        netloc = netloc.lower()

    path = parsed.path or ("/" if scheme in {"http", "https"} else "")
    path = quote(path, safe="/:@!$&'()*+,;=%")
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = urlencode(sorted(query_pairs), doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def normalize_text(value: str) -> str:
    """Normalize claim text for cache decisions."""

    return _compact_whitespace(value).casefold()


def _source_ref_cache_key(
    source_id: str,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> str | None:
    source = sources_by_id.get(source_id)
    if source is None:
        return None
    return source_cache_key(source)


def _image_ref_cache_key(
    image_id: str,
    images_by_id: Mapping[str, Mapping[str, Any]],
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> str | None:
    image = images_by_id.get(image_id)
    if image is None:
        return None
    source = None
    source_id = _first_string(image, "source_id")
    if source_id is not None:
        source = sources_by_id.get(source_id)
    return image_cache_key(image, source=source)


def _quote_span_inputs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray, str)):
        return []
    spans: list[dict[str, str]] = []
    for span in value:
        if not isinstance(span, Mapping):
            continue
        source_id = _first_string(span, "source_id")
        quote_text = _first_string(span, "quote")
        location = _first_string(span, "location")
        spans.append(
            {
                "source_id": source_id or "",
                "quote": normalize_text(quote_text or ""),
                "location": _compact_whitespace(location or ""),
            }
        )
    return sorted(spans, key=lambda span: (span["source_id"], span["quote"], span["location"]))


def _visual_support_inputs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray, str)):
        return []
    supports: list[dict[str, Any]] = []
    for support in value:
        if not isinstance(support, Mapping):
            continue
        image_id = _first_string(support, "image_id") or ""
        observation_ref = _first_string(support, "observation_ref") or ""
        observation_text = _first_string(support, "observation_text") or ""
        relation_type = _first_string(support, "relation_type") or ""
        provider = _first_string(support, "provider") or ""
        rationale = _first_string(support, "rationale") or ""
        supports.append(
            {
                "image_id": _compact_whitespace(image_id),
                "observation_ref": _compact_whitespace(observation_ref),
                "observation_index": _integer_or_string(support.get("observation_index")),
                "observation_text": normalize_text(observation_text),
                "relation_type": _normalize_scalar(relation_type),
                "provider": _normalize_scalar(provider),
                "rationale": normalize_text(rationale),
                "confidence": _number_or_string(support.get("confidence")),
            }
        )
    return sorted(
        supports,
        key=lambda support: (
            str(support["image_id"]),
            str(support["observation_index"]),
            str(support["observation_ref"]),
            str(support["observation_text"]),
            str(support["relation_type"]),
            str(support["provider"]),
            str(support["rationale"]),
            str(support["confidence"]),
        ),
    )


def _digest(kind: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{kind}:{CACHE_KEY_SCHEMA_VERSION}:sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _first_string(container: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = container.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
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


def _normalized_string_set(values: Sequence[str]) -> list[str]:
    return sorted({_normalize_scalar(value) for value in values if value.strip()})


def _normalized_text_list(value: Any) -> list[str]:
    return sorted({normalize_text(item) for item in _string_list(value) if item.strip()})


def _normalize_scalar(value: str | None) -> str:
    return _compact_whitespace(value or "").casefold()


def _normalize_mime(value: str) -> str:
    return _normalize_scalar(value).split(";", 1)[0]


def _normalize_hash(value: str) -> str:
    value = _normalize_scalar(value)
    if not value:
        return ""
    return value if value.startswith("sha256:") else f"sha256:{value}"


def _integer_or_string(value: Any) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return text


def _number_or_string(value: Any) -> int | float | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        return _compact_whitespace(text)
    return int(numeric) if numeric.is_integer() else numeric


def _compact_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
