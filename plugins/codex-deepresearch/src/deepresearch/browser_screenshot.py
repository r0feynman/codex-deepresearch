"""Browser screenshot collection for automatic visual acquisition."""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse


DEFAULT_BROWSER_SCREENSHOT_PROVIDER = "browser-screenshot"
DEFAULT_BROWSER_VIEWPORT = {"width": 1280, "height": 720}
SUPPORTED_BROWSER_SCREENSHOT_MODES = ("first_viewport", "full_page")
ALL_BROWSER_SCREENSHOT_MODES = (
    "first_viewport",
    "full_page",
    "scroll",
    "interaction",
)
BLOCKED_POLICY_FLAGS = {
    "access_denied",
    "access_controlled",
    "captcha",
    "captcha_protected",
    "captcha_required",
    "copyright_restricted",
    "login_gated",
    "login_required",
    "paywall",
    "paywalled",
    "pii_detected",
    "policy_blocked",
    "requires_login",
    "robots_blocked",
    "robots_disallowed",
}
BLOCKED_RETRIEVAL_STATUSES = {
    "access_denied",
    "blocked",
    "captcha",
    "captcha_required",
    "forbidden",
    "login_required",
    "paywall",
    "paywalled",
    "policy_blocked",
    "robots_disallowed",
    "unauthorized",
}


class BrowserScreenshotError(RuntimeError):
    """Raised when a browser transport cannot capture a screenshot."""


@dataclass(frozen=True)
class BrowserScreenshotCapture:
    """A completed screenshot capture result."""

    width: int
    height: int
    mime_type: str = "image/png"
    http_status: int | None = None
    final_url: str | None = None
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)


class BrowserScreenshotTransport(Protocol):
    """Minimal transport protocol for browser screenshot capture."""

    name: str
    provider_mode: str

    def availability(self) -> tuple[bool, str | None]:
        """Return whether this transport is currently usable."""

    def capture(
        self,
        *,
        url: str,
        output_path: Path,
        viewport: Mapping[str, int],
        full_page: bool,
        timeout_ms: int,
    ) -> BrowserScreenshotCapture:
        """Capture a screenshot to output_path and return capture metadata."""


@dataclass(frozen=True)
class BrowserScreenshotCollection:
    """Browser screenshot candidates plus provider diagnostics."""

    candidates: list[dict[str, Any]]
    provider_status: dict[str, Any]


class PlaywrightBrowserTransport:
    """Real browser automation transport using Playwright when installed."""

    name = "playwright"
    provider_mode = "real"

    def availability(self) -> tuple[bool, str | None]:
        if importlib.util.find_spec("playwright") is None:
            return False, "playwright_python_package_missing"
        return True, None

    def capture(
        self,
        *,
        url: str,
        output_path: Path,
        viewport: Mapping[str, int],
        full_page: bool,
        timeout_ms: int,
    ) -> BrowserScreenshotCapture:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserScreenshotError("playwright_python_package_missing") from exc

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(
                        viewport={
                            "width": int(viewport["width"]),
                            "height": int(viewport["height"]),
                        }
                    )
                    response = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(output_path), full_page=full_page)
                    width = int(
                        page.evaluate(
                            "() => Math.max(document.documentElement.scrollWidth, window.innerWidth)"
                        )
                    )
                    height = int(
                        page.evaluate(
                            "() => Math.max(document.documentElement.scrollHeight, window.innerHeight)"
                        )
                    )
                    if not full_page:
                        width = int(viewport["width"])
                        height = int(viewport["height"])
                    return BrowserScreenshotCapture(
                        width=width,
                        height=height,
                        http_status=response.status if response is not None else None,
                        final_url=page.url,
                        provider_metadata={"browser": "chromium", "headless": True},
                    )
                finally:
                    browser.close()
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            raise BrowserScreenshotError(str(exc)) from exc


def collect_browser_screenshot_candidates(
    *,
    run_dir: Path,
    evidence: Mapping[str, Any],
    sources: Sequence[Mapping[str, Any]],
    routes: Sequence[Mapping[str, Any]],
    screenshot_modes: Sequence[str],
    created_at: str,
    transport: BrowserScreenshotTransport | None = None,
    provider: str = DEFAULT_BROWSER_SCREENSHOT_PROVIDER,
    viewport: Mapping[str, int] | None = None,
    timeout_ms: int = 15_000,
) -> BrowserScreenshotCollection:
    """Capture allowed browser screenshots and return visual candidate records.

    The collector only writes screenshot artifacts and candidate/fetch metadata.
    It deliberately marks captured screenshots as requiring later VLM observation
    so they cannot become supportable claim evidence at acquisition time.
    """

    active_transport = transport or PlaywrightBrowserTransport()
    provider_mode = _provider_mode(active_transport)
    provider_run_id = str(evidence.get("run_id") or run_dir.name)
    viewport_payload = _viewport(viewport)
    available, unavailable_reason = active_transport.availability()
    candidates: list[dict[str, Any]] = []
    captures_attempted = 0
    captures_succeeded = 0
    captures_skipped = 0
    last_error: str | None = unavailable_reason
    external_network_call = False

    for source in _capture_sources(sources):
        page_url = _page_url(source)
        if page_url is None:
            continue
        external_network_call = external_network_call or _is_remote_url(page_url)
        route = _source_route(routes, source)
        policy = _page_policy(source)
        for mode in screenshot_modes:
            if mode not in ALL_BROWSER_SCREENSHOT_MODES:
                continue
            relative_path = _artifact_path(source, mode)
            base_record = _candidate_record(
                provider=provider,
                provider_mode=provider_mode,
                provider_run_id=provider_run_id,
                transport=active_transport.name,
                source=source,
                route=route,
                mode=mode,
                page_url=page_url,
                local_artifact_path=relative_path,
                viewport=viewport_payload,
                created_at=created_at,
                policy_decision=policy["decision"],
                policy_flags=policy["flags"],
            )
            if mode not in SUPPORTED_BROWSER_SCREENSHOT_MODES:
                captures_skipped += 1
                candidates.append(
                    _removed_record(
                        base_record,
                        reason="unsupported_screenshot_mode",
                        unsupported_reason=(
                            f"{provider} does not support {mode} screenshot capture"
                        ),
                    )
                )
                continue
            if policy["decision"] != "allowed":
                captures_skipped += 1
                candidates.append(
                    _removed_record(
                        base_record,
                        reason=policy["reason"],
                        unsupported_reason=policy["message"],
                    )
                )
                continue
            if not available:
                captures_skipped += 1
                candidates.append(
                    _removed_record(
                        base_record,
                        reason="browser_transport_unavailable",
                        unsupported_reason=unavailable_reason
                        or "browser transport is unavailable",
                        candidate_status="fetch_failed",
                    )
                )
                continue

            output_path = run_dir / relative_path
            captures_attempted += 1
            try:
                capture = active_transport.capture(
                    url=page_url,
                    output_path=output_path,
                    viewport=viewport_payload,
                    full_page=mode == "full_page",
                    timeout_ms=timeout_ms,
                )
            except BrowserScreenshotError as exc:
                captures_skipped += 1
                last_error = str(exc)
                candidates.append(
                    _removed_record(
                        base_record,
                        reason="capture_failed",
                        unsupported_reason=str(exc),
                        candidate_status="fetch_failed",
                    )
                )
                continue

            final_url = capture.final_url or page_url
            post_navigation_decision = _post_navigation_decision(capture.http_status)
            if post_navigation_decision is not None:
                _remove_artifact(output_path)
                captures_skipped += 1
                last_error = post_navigation_decision["message"]
                candidates.append(
                    _removed_record(
                        _with_capture_context(
                            base_record,
                            capture=capture,
                            final_url=final_url,
                        ),
                        reason=post_navigation_decision["reason"],
                        unsupported_reason=post_navigation_decision["message"],
                        candidate_status=post_navigation_decision["candidate_status"],
                        policy_decision=post_navigation_decision["policy_decision"],
                        policy_flags=post_navigation_decision["policy_flags"],
                    )
                )
                continue

            captures_succeeded += 1
            candidates.append(
                _captured_record(
                    base_record,
                    capture=capture,
                    final_url=final_url,
                )
            )

    provider_status = {
        "provider": provider,
        "provider_kind": "screenshot",
        "provider_mode": provider_mode,
        "provider_run_id": provider_run_id,
        "configured": True,
        "available": available,
        "blocked_reason": unavailable_reason,
        "invoked": captures_attempted > 0,
        "invocations": captures_attempted,
        "candidates": len(candidates),
        "captures_attempted": captures_attempted,
        "captures_succeeded": captures_succeeded,
        "captures_skipped": captures_skipped,
        "external_network_call": external_network_call and captures_attempted > 0,
        "external_vlm_call": False,
        "transport": active_transport.name,
        "last_error": last_error,
    }
    return BrowserScreenshotCollection(candidates=candidates, provider_status=provider_status)


def _candidate_record(
    *,
    provider: str,
    provider_mode: str,
    provider_run_id: str,
    transport: str,
    source: Mapping[str, Any],
    route: Mapping[str, Any],
    mode: str,
    page_url: str,
    local_artifact_path: str,
    viewport: Mapping[str, int],
    created_at: str,
    policy_decision: str,
    policy_flags: Sequence[str],
) -> dict[str, Any]:
    source_id = _string(source.get("id")) or "source"
    route_id = _string(route.get("id")) or _string(source.get("angle_id")) or "angle_001"
    task_id = _string(route.get("task_id")) or _task_id_for_angle(route_id)
    candidate_id = "cand_" + _safe_id(f"{provider}_{source_id}_{mode}")
    screenshot = {
        "mode": mode,
        "capture_mode": mode,
        "supported": mode in SUPPORTED_BROWSER_SCREENSHOT_MODES,
        "provider": provider,
        "transport": transport,
        "viewport": dict(viewport),
        "full_page": mode == "full_page",
        "local_artifact_path": local_artifact_path,
        "policy_decision": policy_decision,
        "policy_flags": list(policy_flags),
        "unsupported_reason": None,
    }
    return {
        "id": candidate_id,
        "candidate_id": candidate_id,
        "plan_id": f"plan_{task_id}",
        "task_id": task_id,
        "angle_id": route_id,
        "provider": provider,
        "provider_kind": "screenshot",
        "provider_mode": provider_mode,
        "provider_run_id": provider_run_id,
        "provider_provenance": {
            "provider": provider,
            "provider_kind": "screenshot",
            "provider_mode": provider_mode,
            "provider_run_id": provider_run_id,
            "transport": transport,
            "fixture_only": provider_mode != "real",
            "external_network_call": False,
            "external_vlm_call": False,
        },
        "source_id": source_id,
        "source_url": source.get("url"),
        "source_search_result_id": source.get("search_result_id"),
        "route": _string(route.get("modality")) or _string(source.get("route")),
        "candidate_class": "screenshot",
        "origin": "screenshot",
        "image_url": None,
        "page_url": page_url,
        "local_artifact_path": local_artifact_path,
        "mime_type": "image/png",
        "width": 0,
        "height": 0,
        "alt_text": f"{mode} browser screenshot",
        "phash": None,
        "visual_tasks": list(route.get("visual_tasks", []))
        if isinstance(route.get("visual_tasks"), list)
        else [],
        "analysis_provider": None,
        "analysis_status": "skipped",
        "observations": [],
        "inferences": [],
        "ocr_text": None,
        "ocr_outputs": [],
        "policy_flags": list(policy_flags),
        "policy_decision": policy_decision,
        "caveats": ["requires_vlm_observation_and_verifier_linkage"],
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "screenshot": screenshot,
        "requires_vlm_observation": True,
        "supportable_evidence": False,
        "acquired_at": created_at,
    }


def _captured_record(
    record: Mapping[str, Any],
    *,
    capture: BrowserScreenshotCapture,
    final_url: str,
) -> dict[str, Any]:
    updated = _with_capture_context(record, capture=capture, final_url=final_url)
    updated.update(
        {
            "status": "accepted",
            "candidate_status": "fetched",
            "analysis_status": "skipped",
            "mime_type": capture.mime_type,
            "width": int(capture.width),
            "height": int(capture.height),
        }
    )
    return updated


def _with_capture_context(
    record: Mapping[str, Any],
    *,
    capture: BrowserScreenshotCapture,
    final_url: str,
) -> dict[str, Any]:
    updated = dict(record)
    screenshot = dict(updated["screenshot"])
    provider_provenance = dict(updated.get("provider_provenance", {}))
    provider_provenance["external_network_call"] = _is_remote_url(final_url)
    screenshot.update(
        {
            "supported": True,
            "final_url": final_url,
            "http_status": capture.http_status,
            "unsupported_reason": None,
        }
    )
    updated.update(
        {
            "http_status": capture.http_status,
            "screenshot": screenshot,
            "provider_provenance": provider_provenance,
            "raw_provider_metadata": dict(capture.provider_metadata),
        }
    )
    return updated


def _removed_record(
    record: Mapping[str, Any],
    *,
    reason: str,
    unsupported_reason: str,
    candidate_status: str = "rejected",
    policy_decision: str | None = None,
    policy_flags: Sequence[str] = (),
) -> dict[str, Any]:
    updated = dict(record)
    screenshot = dict(updated["screenshot"])
    screenshot.update({"supported": False, "unsupported_reason": unsupported_reason})
    decision = policy_decision or str(updated.get("policy_decision") or "allowed")
    flags = _dedupe([*_string_list(updated.get("policy_flags")), *policy_flags])
    if decision == "blocked":
        candidate_status = "policy_blocked"
    updated.update(
        {
            "status": "removed",
            "candidate_status": candidate_status,
            "analysis_status": "skipped",
            "removal_reasons": _dedupe([reason, *flags]),
            "rejection_reason": reason,
            "policy_decision": decision,
            "policy_flags": flags,
            "screenshot": screenshot,
        }
    )
    return updated


def _post_navigation_decision(http_status: int | None) -> dict[str, Any] | None:
    if http_status in {401, 403, 407, 451}:
        return {
            "reason": "access_denied",
            "message": f"browser navigation returned HTTP {http_status}; screenshot policy blocked",
            "candidate_status": "policy_blocked",
            "policy_decision": "blocked",
            "policy_flags": ["access_denied"],
        }
    if isinstance(http_status, int) and 400 <= http_status <= 599:
        return {
            "reason": "retrieval_failed",
            "message": f"browser navigation returned HTTP {http_status}; screenshot retrieval failed",
            "candidate_status": "fetch_failed",
            "policy_decision": "allowed",
            "policy_flags": [],
        }
    return None


def _remove_artifact(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def _capture_sources(sources: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    result = []
    for source in sources:
        if source.get("type") not in {"web", "screenshot"}:
            continue
        if _string(source.get("visual_acquisition_provider")) in {
            "local-image-fixture",
            "local-screenshot-fixture",
        }:
            continue
        if _page_url(source) is None:
            continue
        result.append(source)
    return result


def _page_policy(source: Mapping[str, Any]) -> dict[str, Any]:
    flags = _string_list(source.get("policy_flags"))
    decision = _string(source.get("policy_decision")) or "allowed"
    reason = "policy_blocked"
    if decision in {"blocked", "manual_review", "budget_pruned"}:
        flags.append(f"policy_decision_{decision}")
        reason = f"policy_decision_{decision}"
    if _string(source.get("robots_policy")) not in {"", "allowed"}:
        decision = "blocked"
        flags.append("robots_disallowed")
        reason = "robots_disallowed"
    if _string(source.get("license_policy")) not in {"", "allowed"}:
        decision = "blocked"
        flags.append("license_policy_blocked")
        reason = "license_policy_blocked"

    retrieval_status = _string(source.get("retrieval_status")).lower()
    if retrieval_status in BLOCKED_RETRIEVAL_STATUSES:
        decision = "blocked"
        flags.append(retrieval_status)
        reason = retrieval_status

    http_status = source.get("http_status")
    if isinstance(http_status, int) and http_status in {401, 403, 407, 451}:
        decision = "blocked"
        flags.append("access_denied")
        reason = "access_denied"

    normalized_flags = {_normalize_flag(flag) for flag in flags}
    blocked_flag = sorted(normalized_flags & BLOCKED_POLICY_FLAGS)
    if blocked_flag:
        decision = "blocked"
        reason = blocked_flag[0]

    if decision == "allowed":
        return {
            "decision": "allowed",
            "flags": _dedupe(flags),
            "reason": "allowed",
            "message": None,
        }
    if decision not in {"blocked", "manual_review", "budget_pruned"}:
        decision = "blocked"
    return {
        "decision": decision,
        "flags": _dedupe(flags),
        "reason": reason,
        "message": f"browser screenshot skipped because page policy is {decision}: {reason}",
    }


def _page_url(source: Mapping[str, Any]) -> str | None:
    for key in ("page_url", "url"):
        value = _string(source.get(key))
        if value:
            return value
    return None


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
    if routes:
        return routes[0]
    return {
        "id": "angle_001",
        "modality": "visual_required",
        "visual_tasks": ["image_claim_alignment"],
        "max_images": 12,
    }


def _artifact_path(source: Mapping[str, Any], mode: str) -> str:
    source_id = _string(source.get("id")) or "source"
    return f"screenshots/{_safe_id(source_id)}_{mode}.png"


def _viewport(viewport: Mapping[str, int] | None) -> dict[str, int]:
    raw = viewport or DEFAULT_BROWSER_VIEWPORT
    width = int(raw.get("width", DEFAULT_BROWSER_VIEWPORT["width"]))
    height = int(raw.get("height", DEFAULT_BROWSER_VIEWPORT["height"]))
    return {"width": max(width, 1), "height": max(height, 1)}


def _provider_mode(transport: BrowserScreenshotTransport) -> str:
    value = _string(getattr(transport, "provider_mode", "real"))
    if value in {"real", "fixture", "manual", "user_provided"}:
        return value
    return "real"


def _task_id_for_angle(angle_id: str) -> str:
    suffix = angle_id.removeprefix("angle_")
    return f"task_visual_{suffix}"


def _is_remote_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"}


def _normalize_flag(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return normalized or "item"


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
