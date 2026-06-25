from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "plugins" / "codex-deepresearch" / "scripts" / "codex-deepresearch"
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import (  # noqa: E402
    acquire_visual_candidates,
    ingest_vision_observations,
    prepare_run,
    synthesize_report,
    validate_artifacts,
    verify_claims,
)
from deepresearch.browser_screenshot import BrowserScreenshotCapture  # noqa: E402
from deepresearch.visual_acquisition import _BraveImageSearchResponse  # noqa: E402


class FakeBraveImageTransport:
    def __init__(self, *, count: int = 12) -> None:
        self.count = count
        self.calls: list[dict] = []

    def fetch(self, **kwargs) -> _BraveImageSearchResponse:
        self.calls.append(kwargs)
        return brave_image_response(count=self.count)


class FakeBrowserScreenshotTransport:
    name = "fake-browser"
    provider_mode = "real"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def availability(self) -> tuple[bool, str | None]:
        return True, None

    def capture(
        self,
        *,
        url: str,
        output_path: Path,
        viewport: dict,
        full_page: bool,
        timeout_ms: int,
    ) -> BrowserScreenshotCapture:
        self.calls.append(
            {
                "url": url,
                "output_path": output_path,
                "viewport": dict(viewport),
                "full_page": full_page,
                "timeout_ms": timeout_ms,
            }
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\x89PNG\r\n\x1a\nmixed-real-browser-screenshot")
        return BrowserScreenshotCapture(
            width=int(viewport["width"]),
            height=int(viewport["height"]),
            http_status=200,
            final_url=url,
            external_network_call=url.startswith(("http://", "https://")),
            provider_metadata={"fake": True},
        )


class ScriptedBraveImageTransport:
    def __init__(self, outcomes: list[_BraveImageSearchResponse | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def fetch(self, **kwargs) -> _BraveImageSearchResponse:
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("unexpected Brave transport call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def brave_image_response(
    *,
    count: int = 12,
    status_code: int = 200,
    payload: dict | None = None,
    headers: dict | None = None,
) -> _BraveImageSearchResponse:
    if payload is None:
        results = []
        for index in range(1, count + 1):
            results.append(
                {
                    "title": f"Provider image result {index}",
                    "type": "image",
                    "age": "1 week",
                    "url": f"https://example.com/source/page-{index}",
                    "source": {
                        "name": "Example Source",
                        "url": f"https://example.com/source/page-{index}",
                        "api_key": "metadata-secret-should-redact",
                    },
                    "thumbnail": {"src": f"https://images.example.com/result-{index}.jpg"},
                    "properties": {
                        "url": f"https://images.example.com/result-{index}.jpg",
                        "width": 800 + index,
                        "height": 450 + index,
                        "tracking_token": "metadata-secret-should-redact",
                    },
                }
            )
        payload = {"type": "images", "results": results, "extra": {}}
    return _BraveImageSearchResponse(
        status_code=status_code,
        payload=payload,
        headers=headers
        or {
            "x-ratelimit-limit": "100",
            "x-ratelimit-remaining": "99",
        },
        elapsed_ms=7,
    )


class VisualAcquisitionTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def assert_no_fixture_sources(self, run_dir: Path) -> None:
        evidence = self.read_json(run_dir / "evidence.json")
        sources = evidence.get("sources", [])
        fixture_source_ids = [
            source.get("id")
            for source in sources
            if isinstance(source, dict) and str(source.get("id", "")).startswith("src_visual_fixture")
        ]
        self.assertEqual(fixture_source_ids, [])
        source_files = sorted((run_dir / "sources").glob("src_visual_fixture_*"))
        self.assertEqual(source_files, [])

    def artifact_text(self, run_dir: Path) -> str:
        paths = [
            run_dir / "evidence.json",
            run_dir / "visual_acquisition_status.json",
            run_dir / "visual_provider_status.json",
            run_dir / "visual_candidates.jsonl",
            run_dir / "image_fetch_status.jsonl",
            run_dir / "visual_observations.jsonl",
        ]
        return "\n".join(
            path.read_text(encoding="utf-8")
            for path in paths
            if path.exists()
        )

    def prepared_visual_run_with_html_source(self) -> Path:
        prepared = prepare_run(
            question="Inspect product screenshots and image search candidates",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        source_path = run_dir / "sources" / "visual-page.html"
        source_path.parent.mkdir(exist_ok=True)
        source_path.write_text(
            """
            <html>
              <head>
                <meta property="og:image" content="/media/hero.png">
                <link rel="icon" href="/favicon.ico">
              </head>
              <body>
                <img src="/media/body-large.png" alt="Product screenshot" width="640" height="360">
                <img src="/media/logo.png" alt="Company logo" width="128" height="64">
                <img src="/media/thumb.jpg" alt="Thumbnail image" width="80" height="45">
                <img src="/media/pixel.gif" alt="tracking pixel" width="1" height="1">
                <img src="/media/preview.png" alt="low value preview" width="300" height="200">
              </body>
            </html>
            """,
            encoding="utf-8",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_visual_page",
                "type": "web",
                "url": "https://example.com/product",
                "title": "Product visual source",
                "published_at": None,
                "accessed_at": "2026-06-22T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/visual-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "route": "visual_required",
                "angle_id": "angle_001",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        return run_dir

    def test_visual_required_collects_rich_candidates_and_ingests_observations(self) -> None:
        run_dir = self.prepared_visual_run_with_html_source()

        result = acquire_visual_candidates(run=run_dir)

        self.assertEqual(result["status"], "visual_candidates_collected")
        self.assertTrue(result["validation"]["valid"], result["validation"]["errors"])
        self.assertGreaterEqual(result["candidate_records"], 10)
        provider_names = {provider["provider"] for provider in result["providers"]}
        self.assertEqual(
            provider_names,
            {
                "local-page",
                "local-image-fixture",
                "local-screenshot-fixture",
                "local-pdf-rasterizer",
            },
        )
        self.assertGreaterEqual(result["candidate_counts"]["open_graph_image"], 1)
        self.assertGreaterEqual(result["candidate_counts"]["body_image"], 1)
        self.assertGreaterEqual(result["candidate_counts"]["image_search"], 1)
        self.assertGreaterEqual(result["candidate_counts"]["screenshot"], 4)

        removal_counts = result["removal_counts"]
        for reason in (
            "favicon",
            "logo",
            "thumbnail",
            "tracking_pixel",
            "low_value_preview",
            "duplicate_image_url",
            "duplicate_content_hash",
            "near_duplicate",
        ):
            self.assertGreaterEqual(removal_counts.get(reason, 0), 1, reason)

        screenshot = result["screenshot_capture"]
        self.assertEqual(set(screenshot["interface_modes"]), {"first_viewport", "full_page", "scroll", "interaction"})
        unsupported_modes = {item["mode"] for item in screenshot["unsupported"]}
        self.assertEqual(unsupported_modes, {"scroll", "interaction"})
        captured_modes = {
            item["mode"]
            for item in screenshot["requests"]
            if item["status"] == "captured"
        }
        self.assertEqual(captured_modes, {"first_viewport", "full_page"})
        self.assertTrue(result["near_duplicate_groups"])

        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        self.assertEqual(len(candidates), result["candidate_records"])
        self.assertEqual(len(observations), result["selected_observations"])
        self.assertGreaterEqual(len(observations), 10)
        self.assertEqual(
            {record["candidate_class"] for record in observations},
            {"open_graph_image", "body_image", "image_search", "screenshot"},
        )
        duplicate_hash_candidates = [
            candidate
            for candidate in candidates
            if "duplicate_content_hash" in candidate.get("removal_reasons", [])
        ]
        self.assertEqual(len(duplicate_hash_candidates), 1)
        duplicate_hash_check = duplicate_hash_candidates[0]["validation_checks"]["content_hash"]
        self.assertEqual(duplicate_hash_check["status"], "failed")
        self.assertTrue(duplicate_hash_check["duplicate"])
        self.assertEqual(duplicate_hash_check["reason"], "duplicate_content_hash")
        self.assertTrue(duplicate_hash_check["duplicate_of"].startswith("cand_"))

        ingest = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
        )
        self.assertEqual(ingest["status"], "visual_evidence_ingested")
        validation = validate_artifacts(
            evidence_path=run_dir / "evidence.json",
            visual_observations_path=run_dir / "visual_observations.jsonl",
        )
        self.assertTrue(validation.valid, [error.to_dict() for error in validation.errors])

        evidence = self.read_json(run_dir / "evidence.json")
        self.assertEqual(len(evidence["images"]), len(observations))
        image_classes = {image["candidate_class"] for image in evidence["images"]}
        self.assertEqual(image_classes, {"open_graph_image", "body_image", "image_search", "screenshot"})
        first = evidence["images"][0]
        checks = first["visual_validation"]
        self.assertEqual(checks["mime_type"]["status"], "passed")
        self.assertEqual(checks["size_limit"]["status"], "passed")
        self.assertEqual(checks["content_hash"]["status"], "passed")
        self.assertIn("candidate_id", first)
        self.assertIn("visual_provider", first)
        self.assertTrue(first["hash"].startswith("sha256:"))
        screenshot_images = [image for image in evidence["images"] if image["origin"] == "screenshot"]
        self.assertEqual({image["screenshot"]["mode"] for image in screenshot_images}, {"first_viewport", "full_page"})
        self.assertEqual({image["visual_provider"] for image in screenshot_images}, {"local-screenshot-fixture"})

        ocr_images = [image for image in evidence["images"] if image.get("ocr_text")]
        self.assertEqual(len(ocr_images), 1)
        ocr_image = ocr_images[0]
        self.assertEqual(ocr_image["ocr_text"], "Fixture OCR text from search result 3")
        self.assertNotIn(ocr_image["ocr_text"], ocr_image["observations"])
        self.assertTrue(ocr_image["vlm_visual_summary"].startswith("Fixture visual summary"))
        self.assertEqual(ocr_image["ocr_outputs"][0]["text"], ocr_image["ocr_text"])

        acquisition = evidence["visual_acquisition"]
        self.assertTrue(acquisition["near_duplicate_groups"])
        self.assertEqual(acquisition["external_vlm_call"], False)

    def test_visual_required_fixture_links_observation_to_claim_and_report(self) -> None:
        run_dir = self.prepared_visual_run_with_html_source()
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["claims"] = [
            {
                "id": "claim_visual_fixture",
                "text": "The product visual source contains fixture visual summary evidence.",
                "claim_type": "visual",
                "supporting_sources": ["src_visual_page"],
                "supporting_images": [],
                "quote_spans": [
                    {
                        "source_id": "src_visual_page",
                        "quote": "Product screenshot",
                        "location": "body image alt text",
                    }
                ],
                "votes": [],
                "verification_status": "unverified",
                "review_status": "not_reviewed",
                "promotion_status": "not_eligible",
                "confidence": "low",
                "caveats": [],
                "angle_id": "angle_001",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        acquire = acquire_visual_candidates(run=run_dir)
        ingest = ingest_vision_observations(run=run_dir, provider="codex-interactive")
        verified = verify_claims(run=run_dir)
        report_status = synthesize_report(run=run_dir)

        self.assertEqual(acquire["status"], "visual_candidates_collected")
        self.assertGreater(acquire["candidate_records"], 0)
        self.assertGreater(acquire["selected_observations"], 0)
        self.assertEqual(ingest["status"], "visual_evidence_ingested")
        self.assertGreater(ingest["claim_visual_links_created"], 0)
        self.assertEqual(verified["status"], "completed")
        evidence = self.read_json(run_dir / "evidence.json")
        claim = evidence["claims"][0]
        self.assertEqual(claim["verification_status"], "supported")
        self.assertTrue(claim["supporting_images"])
        self.assertTrue(claim["visual_supports"])
        first_support = claim["visual_supports"][0]
        first_image = next(
            image for image in evidence["images"] if image["id"] == first_support["image_id"]
        )
        self.assertEqual(
            first_support["observation_ref"],
            f"images.{first_support['image_id']}.observations[{first_support['observation_index']}]",
        )
        self.assertEqual(
            first_support["observation_text"],
            first_image["observations"][first_support["observation_index"]],
        )
        self.assertIn(
            first_support["provider"],
            {
                "local-page",
                "local-image-fixture",
                "local-screenshot-fixture",
                "local-pdf-rasterizer",
            },
        )
        self.assertEqual(report_status["status"], "completed")
        self.assertTrue(report_status["used_images"])
        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Visual Findings", report)
        self.assertIn("claim `claim_visual_fixture`", report)

    def test_text_only_route_collects_zero_visual_work(self) -> None:
        prepared = prepare_run(
            question="Text-only visual acquisition gating",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])

        result = acquire_visual_candidates(run=run_dir)

        self.assertEqual(result["status"], "no_visual_tasks")
        self.assertTrue(result["validation"]["valid"], result["validation"]["errors"])
        self.assertEqual(result["candidate_records"], 0)
        self.assertEqual(result["selected_observations"], 0)
        self.assertEqual(result["image_search_invocations"], 0)
        self.assertEqual(result["screenshot_capture_requests"], 0)
        self.assertEqual(result["ocr_records"], 0)
        self.assertFalse(result["external_vlm_call"])
        self.assertEqual(self.read_jsonl(run_dir / "visual_candidates.jsonl"), [])
        self.assertEqual(self.read_jsonl(run_dir / "visual_observations.jsonl"), [])
        evidence = self.read_json(run_dir / "evidence.json")
        self.assertEqual(evidence["images"], [])
        self.assertEqual(evidence["visual_acquisition"]["status"], "no_visual_tasks")

    def test_real_brave_image_search_provider_normalizes_candidates(self) -> None:
        prepared = prepare_run(
            question="Find image evidence for a public product interface",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        transport = FakeBraveImageTransport(count=12)

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["brave-image-search"],
            real_image_search_transport=transport,
            real_image_search_config={
                "brave_api_key": "test-secret-token",
                "brave_allow_result_storage": True,
                "brave_image_count": 12,
                "brave_estimated_cost_usd": 0.006,
            },
        )

        self.assertEqual(result["status"], "real_image_search_candidates_collected")
        self.assertTrue(result["validation"]["valid"], result["validation"]["errors"])
        self.assertTrue(
            result["visual_artifact_validation"]["valid"],
            result["visual_artifact_validation"]["errors"],
        )
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result["candidate_records"], 12)
        self.assertEqual(result["selected_observations"], 0)
        self.assertEqual(result["image_search_invocations"], 1)
        self.assertTrue(result["external_network_call"])

        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        self.assertEqual(len(candidates), 12)
        first = candidates[0]
        self.assertEqual(first["provider"], "brave-image-search")
        self.assertEqual(first["provider_kind"], "web_image_search")
        self.assertEqual(first["provider_mode"], "real")
        self.assertEqual(first["origin"], "image_search")
        self.assertEqual(first["candidate_status"], "ranked")
        self.assertEqual(first["rank"], 1)
        self.assertGreater(first["score"], 0)
        self.assertEqual(first["policy_decision"], "allowed")
        self.assertTrue(first["page_url"].startswith("https://example.com/source/"))
        self.assertTrue(first["image_url"].startswith("https://images.example.com/"))
        self.assertFalse(first["provider_provenance"]["fixture_only"])
        self.assertTrue(first["provider_provenance"]["external_network_call"])
        self.assertIn("provider_diagnostics", first)
        self.assertIn("raw_provider_metadata", first)
        self.assertEqual(
            set(first["raw_provider_metadata"]["properties"].keys()),
            {"width", "height"},
        )
        self.assertNotIn("api_key", json.dumps(first["raw_provider_metadata"]))
        self.assertNotIn("tracking_token", json.dumps(first["raw_provider_metadata"]))
        self.assertGreater(first["estimated_cost_usd"], 0)
        self.assertEqual(first["actual_cost_usd"], 0.0)
        self.assertEqual(self.read_jsonl(run_dir / "visual_observations.jsonl"), [])
        self.assertTrue(all(not (run_dir / f"images/result-{index}.jpg").exists() for index in range(1, 13)))
        self.assert_no_fixture_sources(run_dir)

        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        provider = provider_status["providers"][0]
        self.assertEqual(provider_status["status"], "real_image_search_candidates_collected")
        self.assertTrue(provider_status["ok"])
        self.assertFalse(provider_status["terminal"])
        self.assertEqual(provider["provider"], "brave-image-search")
        self.assertEqual(provider["provider_mode"], "real")
        self.assertTrue(provider["configured"])
        self.assertTrue(provider["available"])
        self.assertEqual(provider["invocations"], 1)
        self.assertEqual(provider["candidates_discovered"], 12)
        self.assertEqual(provider["artifacts_fetched"], 0)
        self.assertEqual(provider["vlm_images_analyzed"], 0)
        self.assertGreater(provider["estimated_cost_usd"], 0)
        self.assertEqual(provider["actual_cost_usd"], 0.0)
        self.assertTrue(provider["external_network_call"])
        self.assertFalse(provider["external_vlm_call"])
        diagnostics = provider["diagnostics"]
        self.assertTrue(diagnostics["result_storage_confirmed"])
        self.assertIn(
            "CODEX_DEEPRESEARCH_BRAVE_ALLOW_RESULT_STORAGE",
            diagnostics["config_keys"],
        )
        self.assertEqual(diagnostics["successful_invocations"], 1)
        self.assertEqual(diagnostics["failed_invocations"], 0)
        self.assertEqual(diagnostics["queries"][0]["rate_limit"]["remaining"], "99")
        self.assertNotIn(
            "test-secret-token",
            json.dumps(
                {
                    "result": result,
                    "candidates": candidates,
                    "provider_status": provider_status,
                },
                sort_keys=True,
            ),
        )

    def test_real_brave_missing_config_blocks_without_fixture_candidates(self) -> None:
        prepared = prepare_run(
            question="Find image evidence but no provider credentials are configured",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["brave-image-search"],
            real_image_search_config={"brave_api_key": ""},
        )

        self.assertEqual(result["status"], "blocked_missing_visual_provider")
        self.assertFalse(result["ok"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["candidate_records"], 0)
        self.assertEqual(result["selected_observations"], 0)
        self.assertEqual(self.read_jsonl(run_dir / "visual_candidates.jsonl"), [])
        self.assertEqual(self.read_jsonl(run_dir / "visual_observations.jsonl"), [])
        self.assert_no_fixture_sources(run_dir)

        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        provider = provider_status["providers"][0]
        self.assertEqual(provider_status["status"], "blocked_missing_visual_provider")
        self.assertFalse(provider_status["ok"])
        self.assertTrue(provider_status["terminal"])
        self.assertEqual(provider["provider"], "brave-image-search")
        self.assertEqual(provider["provider_kind"], "web_image_search")
        self.assertEqual(provider["provider_mode"], "real")
        self.assertFalse(provider["configured"])
        self.assertFalse(provider["available"])
        self.assertEqual(provider["blocked_reason"], "missing_brave_search_api_key")
        self.assertEqual(provider["invocations"], 0)
        self.assertEqual(provider["diagnostics"]["result_storage_confirmed"], False)

    def test_real_brave_storage_not_confirmed_blocks_before_provider_call(self) -> None:
        prepared = prepare_run(
            question="Find image evidence but storage rights are not confirmed",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        transport = FakeBraveImageTransport(count=12)

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["brave-image-search"],
            real_image_search_transport=transport,
            real_image_search_config={"brave_api_key": "test-secret-token"},
        )

        self.assertEqual(result["status"], "blocked_missing_visual_provider")
        self.assertEqual(len(transport.calls), 0)
        self.assertEqual(result["candidate_records"], 0)
        self.assertEqual(self.read_jsonl(run_dir / "visual_candidates.jsonl"), [])
        self.assert_no_fixture_sources(run_dir)

        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        provider = provider_status["providers"][0]
        self.assertTrue(provider["configured"])
        self.assertFalse(provider["available"])
        self.assertEqual(provider["blocked_reason"], "brave_result_storage_not_confirmed")
        self.assertEqual(provider["invocations"], 0)
        self.assertFalse(provider["external_network_call"])
        diagnostics = provider["diagnostics"]
        self.assertFalse(diagnostics["result_storage_confirmed"])
        self.assertIn(
            "missing: CODEX_DEEPRESEARCH_BRAVE_ALLOW_RESULT_STORAGE",
            diagnostics["config_keys"],
        )
        self.assertNotIn("test-secret-token", self.artifact_text(run_dir))

    def test_real_brave_auth_failure_persists_sanitized_diagnostics(self) -> None:
        token = "test-secret-token"
        prepared = prepare_run(
            question="Find image evidence with an invalid provider key",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        transport = ScriptedBraveImageTransport(
            [
                brave_image_response(
                    status_code=401,
                    payload={
                        "error": {
                            "code": "unauthorized",
                            "message": f"invalid API key {token}",
                        }
                    },
                )
            ]
        )

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["brave-image-search"],
            real_image_search_transport=transport,
            real_image_search_config={
                "brave_api_key": token,
                "brave_allow_result_storage": True,
            },
        )

        self.assertEqual(result["status"], "blocked_missing_visual_provider")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.read_jsonl(run_dir / "visual_candidates.jsonl"), [])
        self.assert_no_fixture_sources(run_dir)
        provider = self.read_json(run_dir / "visual_provider_status.json")["providers"][0]
        self.assertTrue(provider["configured"])
        self.assertFalse(provider["available"])
        self.assertEqual(provider["blocked_reason"], "provider_auth_failed")
        self.assertTrue(provider["external_network_call"])
        self.assertEqual(provider["diagnostics"]["queries"][0]["http_status"], 401)
        self.assertEqual(provider["diagnostics"]["auth_failed"], True)
        artifact_text = self.artifact_text(run_dir)
        self.assertNotIn(token, artifact_text)
        self.assertIn("[REDACTED]", artifact_text)

    def test_real_brave_rate_limit_persists_rate_limit_diagnostics(self) -> None:
        prepared = prepare_run(
            question="Find image evidence while provider is rate limited",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        transport = ScriptedBraveImageTransport(
            [
                brave_image_response(
                    status_code=429,
                    payload={"error": {"message": "rate limit exceeded"}},
                    headers={
                        "retry-after": "60",
                        "x-ratelimit-limit": "100",
                        "x-ratelimit-remaining": "0",
                        "x-ratelimit-reset": "1710000000",
                    },
                )
            ]
        )

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["brave-image-search"],
            real_image_search_transport=transport,
            real_image_search_config={
                "brave_api_key": "test-secret-token",
                "brave_allow_result_storage": True,
            },
        )

        self.assertEqual(result["status"], "blocked_missing_visual_provider")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.read_jsonl(run_dir / "visual_candidates.jsonl"), [])
        self.assert_no_fixture_sources(run_dir)
        provider = self.read_json(run_dir / "visual_provider_status.json")["providers"][0]
        self.assertFalse(provider["available"])
        self.assertEqual(provider["blocked_reason"], "provider_rate_limited")
        diagnostics = provider["diagnostics"]
        self.assertTrue(diagnostics["rate_limited"])
        self.assertEqual(diagnostics["failed_invocations"], 1)
        self.assertEqual(diagnostics["queries"][0]["rate_limit"]["retry_after"], "60")
        self.assertEqual(diagnostics["queries"][0]["rate_limit"]["remaining"], "0")

    def test_real_brave_transport_exception_redacts_configured_secret(self) -> None:
        token = "test-secret-token"
        prepared = prepare_run(
            question="Find image evidence when transport raises",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        transport = ScriptedBraveImageTransport(
            [OSError(f"transport failed with api_key={token}")]
        )

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["brave-image-search"],
            real_image_search_transport=transport,
            real_image_search_config={
                "brave_api_key": token,
                "brave_allow_result_storage": True,
            },
        )

        self.assertEqual(result["status"], "blocked_missing_visual_provider")
        provider = self.read_json(run_dir / "visual_provider_status.json")["providers"][0]
        self.assertEqual(provider["blocked_reason"], "provider_request_failed")
        self.assertIn("[REDACTED]", provider["last_error"])
        artifact_text = self.artifact_text(run_dir)
        self.assertNotIn(token, artifact_text)
        self.assertIn("[REDACTED]", artifact_text)

    def test_real_brave_partial_rate_limit_keeps_successful_candidates(self) -> None:
        prepared = prepare_run(
            question="Find image evidence across multiple visual angles",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            angles=[
                "interface screenshots showing the product navigation",
                "pricing screenshots showing product tiers",
            ],
        )
        run_dir = Path(prepared["run_dir"])
        transport = ScriptedBraveImageTransport(
            [
                brave_image_response(count=5),
                brave_image_response(
                    status_code=429,
                    payload={"error": {"message": "rate limit exceeded"}},
                    headers={
                        "retry-after": "45",
                        "x-ratelimit-limit": "100",
                        "x-ratelimit-remaining": "0",
                    },
                ),
            ]
        )

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["brave-image-search"],
            real_image_search_transport=transport,
            real_image_search_config={
                "brave_api_key": "test-secret-token",
                "brave_allow_result_storage": True,
                "brave_image_count": 5,
            },
        )

        self.assertEqual(result["status"], "real_image_search_candidates_collected")
        self.assertEqual(len(transport.calls), 2)
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        self.assertEqual(len(candidates), 5)
        self.assertEqual({candidate["provider_mode"] for candidate in candidates}, {"real"})
        self.assert_no_fixture_sources(run_dir)

        provider = self.read_json(run_dir / "visual_provider_status.json")["providers"][0]
        self.assertTrue(provider["available"])
        self.assertIsNone(provider["blocked_reason"])
        self.assertEqual(provider["invocations"], 2)
        self.assertEqual(provider["candidates_discovered"], 5)
        diagnostics = provider["diagnostics"]
        self.assertEqual(diagnostics["successful_invocations"], 1)
        self.assertEqual(diagnostics["failed_invocations"], 1)
        self.assertTrue(diagnostics["partial_failure"])
        self.assertTrue(diagnostics["rate_limited"])
        self.assertEqual(diagnostics["queries"][1]["rate_limit"]["retry_after"], "45")

    def test_real_provider_request_ignores_fixture_provider_candidates(self) -> None:
        prepared = prepare_run(
            question="Find image evidence with mixed provider input",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        transport = FakeBraveImageTransport(count=10)

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["local-image-fixture", "brave-image-search"],
            real_image_search_transport=transport,
            real_image_search_config={
                "brave_api_key": "test-secret-token",
                "brave_allow_result_storage": True,
                "brave_image_count": 10,
            },
        )

        self.assertEqual(result["status"], "real_image_search_candidates_collected")
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        self.assertEqual(len(candidates), 10)
        self.assertEqual({candidate["provider"] for candidate in candidates}, {"brave-image-search"})
        self.assert_no_fixture_sources(run_dir)
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        self.assertEqual(
            {provider["provider"] for provider in provider_status["providers"]},
            {"brave-image-search"},
        )
        self.assertEqual(provider_status["providers"][0]["invocations"], 1)

    def test_mixed_real_providers_keep_browser_screenshot_candidates(self) -> None:
        prepared = prepare_run(
            question="Find image and screenshot evidence with mixed real providers",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [
            {
                "id": "src_browser_page",
                "type": "web",
                "url": "https://example.test/browser-page",
                "title": "Browser page",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "primary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/browser-page.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "route": "visual_required",
                "angle_id": "angle_001",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        brave_transport = FakeBraveImageTransport(count=3)
        browser_transport = FakeBrowserScreenshotTransport()

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["brave-image-search", "browser-screenshot"],
            screenshot_modes=["first_viewport"],
            real_image_search_transport=brave_transport,
            real_image_search_config={
                "brave_api_key": "test-secret-token",
                "brave_allow_result_storage": True,
                "brave_image_count": 3,
            },
            browser_transport=browser_transport,
        )

        self.assertEqual(result["status"], "real_image_search_candidates_collected")
        self.assertEqual(len(brave_transport.calls), 1)
        self.assertEqual(len(browser_transport.calls), 1)
        self.assertEqual(result["candidate_records"], 4)
        self.assertEqual(result["screenshot_capture_requests"], 1)
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        self.assertEqual(
            {candidate["provider"] for candidate in candidates},
            {"brave-image-search", "browser-screenshot"},
        )
        screenshots = [
            candidate for candidate in candidates if candidate["provider"] == "browser-screenshot"
        ]
        self.assertEqual(len(screenshots), 1)
        self.assertEqual(screenshots[0]["candidate_status"], "fetched")
        self.assertTrue(screenshots[0]["requires_vlm_observation"])
        self.assertFalse(screenshots[0]["supportable_evidence"])
        self.assertTrue((run_dir / screenshots[0]["local_artifact_path"]).is_file())
        self.assertTrue(screenshots[0]["hash"].startswith("sha256:"))
        self.assert_no_fixture_sources(run_dir)

        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        self.assertEqual(
            {provider["provider"] for provider in provider_status["providers"]},
            {"brave-image-search", "browser-screenshot"},
        )
        browser_provider = [
            provider
            for provider in provider_status["providers"]
            if provider["provider"] == "browser-screenshot"
        ][0]
        self.assertEqual(browser_provider["provider_kind"], "screenshot")
        self.assertEqual(browser_provider["provider_mode"], "real")
        self.assertTrue(browser_provider["available"])
        self.assertEqual(browser_provider["invocations"], 1)
        self.assertEqual(browser_provider["artifacts_fetched"], 1)
        self.assertEqual(browser_provider["vlm_images_analyzed"], 0)

    def test_text_only_route_does_not_call_real_image_search_provider(self) -> None:
        prepared = prepare_run(
            question="Text-only route should not call real image search",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])
        transport = FakeBraveImageTransport(count=12)

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["brave-image-search"],
            real_image_search_transport=transport,
            real_image_search_config={"brave_api_key": "test-secret-token"},
        )

        self.assertEqual(result["status"], "no_visual_tasks")
        self.assertEqual(len(transport.calls), 0)
        self.assertEqual(result["candidate_records"], 0)
        self.assertEqual(result["image_search_invocations"], 0)
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        provider = provider_status["providers"][0]
        self.assertEqual(provider["provider"], "brave-image-search")
        self.assertEqual(provider["provider_mode"], "real")
        self.assertEqual(provider["invocations"], 0)

    def test_cli_acquire_visual_outputs_machine_readable_status(self) -> None:
        run_dir = self.prepared_visual_run_with_html_source()

        command = subprocess.run(
            [
                str(RUNNER),
                "acquire-visual",
                "--run",
                str(run_dir),
                "--screenshot-mode",
                "all",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["status"], "visual_candidates_collected")
        self.assertGreaterEqual(payload["candidate_records"], 10)
        self.assertTrue((run_dir / "visual_candidates.jsonl").is_file())
        self.assertTrue((run_dir / "visual_acquisition_status.json").is_file())


if __name__ == "__main__":
    unittest.main()
