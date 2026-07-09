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
    validate_visual_artifacts,
    verify_claims,
)
from deepresearch.browser_screenshot import BrowserScreenshotCapture  # noqa: E402
from deepresearch.page_image_extraction import FetchResponse  # noqa: E402
from deepresearch.visual_acquisition import (  # noqa: E402
    _BraveImageSearchResponse,
    _merge_visual_observation_records,
    _release_visible_visual_records_if_needed,
)
from deepresearch.visual_artifacts import visual_minimums_for_run  # noqa: E402

prepare_search_handoff_run = prepare_run


def prepare_run(*args, **kwargs):
    kwargs.setdefault("angles", ["primary source discovery"])
    kwargs.setdefault("_allow_release_ineligible_materialization_for_tests", True)
    return prepare_search_handoff_run(*args, **kwargs)


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


class MissingDependencyBrowserScreenshotTransport:
    name = "playwright"
    provider_mode = "real"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def availability(self) -> tuple[bool, str | None]:
        return False, "missing_browser_dependency"

    def availability_diagnostics(self) -> dict:
        return {
            "transport": self.name,
            "reason": "missing_browser_dependency",
            "check": "chromium_launch",
            "install_guidance": [
                "python3 -m playwright install chromium",
                "python3 -m playwright install-deps chromium",
            ],
        }

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
        raise AssertionError("capture should not run when browser dependency preflight fails")


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

    def test_visual_observation_merge_preserves_existing_child_records_and_links(self) -> None:
        merged = _merge_visual_observation_records(
            [
                {
                    "observation_id": "obs_child_001",
                    "evidence_image_id": "img_child_001",
                    "provider": "codex-interactive",
                    "verifier_links": [{"claim_id": "claim_001", "verifier_vote_id": "vote_001"}],
                    "report_links": [{"claim_id": "claim_001", "citation_id": "img:img_child_001"}],
                    "observations": ["child shard visual observation"],
                }
            ],
            [
                {
                    "observation_id": "obs_new_001",
                    "evidence_image_id": "img_new_001",
                    "provider": "codex-interactive",
                    "verifier_links": [],
                    "report_links": [],
                    "observations": ["new auto visual observation"],
                },
                {
                    "observation_id": "obs_child_001_new",
                    "evidence_image_id": "img_child_001",
                    "provider": "codex-interactive",
                    "verifier_links": [{"claim_id": "claim_002", "verifier_vote_id": "vote_002"}],
                    "report_links": [{"claim_id": "claim_002", "citation_id": "img:img_child_001"}],
                    "observations": ["updated child shard visual observation"],
                },
            ],
        )

        by_image = {record["evidence_image_id"]: record for record in merged}
        self.assertEqual(set(by_image), {"img_child_001", "img_new_001"})
        self.assertEqual(
            {link["claim_id"] for link in by_image["img_child_001"]["verifier_links"]},
            {"claim_001", "claim_002"},
        )
        self.assertEqual(
            {link["claim_id"] for link in by_image["img_child_001"]["report_links"]},
            {"claim_001", "claim_002"},
        )
        self.assertEqual(
            by_image["img_child_001"]["observations"],
            ["updated child shard visual observation"],
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

    def test_visual_required_run_generates_supported_visual_claims_from_observations(self) -> None:
        run_dir = self.prepared_visual_run_with_html_source()

        acquire = acquire_visual_candidates(run=run_dir)
        ingest = ingest_vision_observations(run=run_dir, provider="codex-interactive")
        verified = verify_claims(run=run_dir)
        report_status = synthesize_report(run=run_dir)

        self.assertEqual(acquire["status"], "visual_candidates_collected")
        self.assertEqual(ingest["status"], "visual_evidence_ingested")
        self.assertGreater(ingest["observation_claims_created"], 0)
        self.assertEqual(verified["status"], "completed")
        self.assertEqual(report_status["status"], "completed")

        evidence = self.read_json(run_dir / "evidence.json")
        generated_claims = [
            claim
            for claim in evidence["claims"]
            if claim.get("extraction_stage") == "vision_adapter_observation_claim"
        ]
        supported_claims = [
            claim
            for claim in generated_claims
            if claim.get("verification_status") == "supported"
        ]
        self.assertGreater(len(supported_claims), 0)
        claim = supported_claims[0]
        self.assertEqual(claim["claim_type"], "visual")
        self.assertTrue(claim["supporting_images"])
        self.assertTrue(claim["visual_supports"])
        self.assertTrue(claim["visual_verifier_vote_refs"])
        self.assertTrue(claim["verifier_vote_refs"])

        support = claim["visual_supports"][0]
        image_id = support["image_id"]
        self.assertIn(image_id, claim["supporting_images"])
        image = next(image for image in evidence["images"] if image["id"] == image_id)
        self.assertEqual(
            support["observation_ref"],
            f"images.{image_id}.observations[{support['observation_index']}]",
        )
        self.assertEqual(
            support["observation_text"],
            image["observations"][support["observation_index"]],
        )

        verifier_votes = {
            vote["id"]: vote for vote in self.read_jsonl(run_dir / "verifier_votes.jsonl")
        }
        visual_vote = verifier_votes[claim["visual_verifier_vote_refs"][0]]
        self.assertEqual(visual_vote["verifier_type"], "visual")
        self.assertEqual(visual_vote["vote"], "support")
        self.assertIn(image_id, visual_vote["evidence_refs"])

        cited_images = {
            cited_image
            for item in report_status["included_claims"]
            for cited_image in item.get("image_ids", [])
        }
        self.assertEqual(set(report_status["used_images"]), cited_images)
        self.assertIn(image_id, cited_images)
        included_claim = next(
            item for item in report_status["included_claims"] if item["claim_id"] == claim["id"]
        )
        self.assertIn(image_id, included_claim["image_ids"])
        self.assertTrue(included_claim["visual_supports"])
        self.assertTrue(included_claim["visual_verifier_vote_refs"])

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Visual Findings", report)
        self.assertIn(f"claim `{claim['id']}`", report.lower())
        self.assertIn(f"`{image_id}`", report)

        validation = validate_artifacts(
            evidence_path=run_dir / "evidence.json",
            visual_observations_path=run_dir / "visual_observations.jsonl",
            verifier_votes_path=run_dir / "verifier_votes.jsonl",
        )
        self.assertTrue(validation.valid, [error.to_dict() for error in validation.errors])
        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(
            visual_validation.valid,
            [error.to_dict() for error in visual_validation.errors],
        )
        linked_observation = next(
            observation
            for observation in self.read_jsonl(run_dir / "visual_observations.jsonl")
            if observation.get("evidence_image_id") == image_id
        )
        self.assertTrue(linked_observation["verifier_links"])
        self.assertTrue(linked_observation["report_links"])

    def test_policy_blocked_and_inference_only_observations_do_not_create_supported_claims(self) -> None:
        run_dir = self.prepared_visual_run_with_html_source()
        images_dir = run_dir / "images"
        images_dir.mkdir(exist_ok=True)
        (images_dir / "policy.png").write_bytes(b"\x89PNG\r\n\x1a\npolicy")
        (images_dir / "inference.png").write_bytes(b"\x89PNG\r\n\x1a\ninference")
        records = [
            {
                "id": "img_policy_blocked",
                "source_id": "src_visual_page",
                "origin": "screenshot",
                "local_artifact_path": "images/policy.png",
                "mime_type": "image/png",
                "width": 640,
                "height": 360,
                "observations": ["A policy-blocked screenshot contains visible text."],
                "inferences": [],
                "visual_tasks": ["screenshot_support"],
                "analysis_status": "policy_blocked",
                "policy_flags": ["private_image"],
                "route": "visual_required",
                "angle_id": "angle_001",
            },
            {
                "id": "img_inference_only",
                "source_id": "src_visual_page",
                "origin": "screenshot",
                "local_artifact_path": "images/inference.png",
                "mime_type": "image/png",
                "width": 640,
                "height": 360,
                "observations": [],
                "inferences": ["The screenshot probably implies a product capability."],
                "visual_tasks": ["screenshot_support"],
                "analysis_status": "analyzed",
                "policy_flags": [],
                "route": "visual_required",
                "angle_id": "angle_001",
            },
        ]
        (run_dir / "visual_observations.jsonl").write_text(
            "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
            encoding="utf-8",
        )

        ingest = ingest_vision_observations(run=run_dir, provider="codex-interactive")
        verified = verify_claims(run=run_dir)

        self.assertEqual(ingest["status"], "visual_evidence_ingested")
        self.assertEqual(ingest["observation_claims_created"], 0)
        self.assertEqual(verified["status"], "completed")
        evidence = self.read_json(run_dir / "evidence.json")
        risky_claims = [
            claim
            for claim in evidence["claims"]
            if claim.get("source_image_id") in {"img_policy_blocked", "img_inference_only"}
        ]
        self.assertEqual(risky_claims, [])
        self.assertFalse(
            any(
                claim.get("verification_status") == "supported"
                and claim.get("confidence") == "high"
                for claim in evidence["claims"]
            )
        )

    def test_blocked_policy_decision_image_cannot_be_supported_or_cited(self) -> None:
        run_dir = self.prepared_visual_run_with_html_source()
        images_dir = run_dir / "images"
        images_dir.mkdir(exist_ok=True)
        (images_dir / "blocked-decision.png").write_bytes(b"\x89PNG\r\n\x1a\nblocked-decision")
        record = {
            "id": "img_policy_decision_blocked",
            "source_id": "src_visual_page",
            "origin": "screenshot",
            "local_artifact_path": "images/blocked-decision.png",
            "mime_type": "image/png",
            "width": 640,
            "height": 360,
            "observations": ["A blocked-policy screenshot contains visible product evidence."],
            "inferences": [],
            "visual_tasks": ["screenshot_support"],
            "analysis_status": "analyzed",
            "policy_decision": "blocked",
            "policy_flags": [],
            "route": "visual_required",
            "angle_id": "angle_001",
        }
        (run_dir / "visual_observations.jsonl").write_text(
            json.dumps(record, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        ingest = ingest_vision_observations(run=run_dir, provider="codex-interactive")
        self.assertEqual(ingest["status"], "visual_evidence_ingested")
        self.assertEqual(ingest["observation_claims_created"], 0)
        evidence = self.read_json(run_dir / "evidence.json")
        image = next(image for image in evidence["images"] if image["id"] == record["id"])
        support = {
            "image_id": image["id"],
            "observation_ref": f"images.{image['id']}.observations[0]",
            "observation_index": 0,
            "observation_text": image["observations"][0],
            "relation_type": "screenshot_support",
            "provider": "codex-interactive",
            "rationale": "Regression fixture for blocked policy_decision image.",
            "confidence": 0.74,
        }
        evidence["claims"] = [
            {
                "id": "claim_blocked_policy_decision_visual",
                "text": "Visual observation: A blocked-policy screenshot contains visible product evidence.",
                "claim_type": "visual",
                "supporting_sources": ["src_visual_page"],
                "supporting_images": [image["id"]],
                "visual_supports": [support],
                "quote_spans": [],
                "votes": [],
                "verification_status": "unverified",
                "review_status": "not_reviewed",
                "promotion_status": "not_eligible",
                "confidence": "low",
                "caveats": [],
                "route": "visual_required",
                "angle_id": "angle_001",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        verified = verify_claims(run=run_dir)
        report_status = synthesize_report(run=run_dir)

        self.assertEqual(verified["status"], "completed")
        evidence = self.read_json(run_dir / "evidence.json")
        claim = evidence["claims"][0]
        self.assertEqual(claim["verification_status"], "policy_blocked")
        self.assertNotEqual(claim["confidence"], "high")
        self.assertFalse(claim["include_in_final_report"])
        self.assertEqual(report_status["claims_included"], 0)
        self.assertNotIn(image["id"], report_status["used_images"])

    def test_text_only_route_collects_zero_visual_work(self) -> None:
        prepared = prepare_run(
            question="Text-only visual acquisition gating",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
            prompt_id="text-only-visual-identity",
            suite_id="issue-133-suite",
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
        plan = self.read_json(run_dir / "visual_search_plan.json")
        self.assertEqual(plan["prompt_id"], "text-only-visual-identity")
        self.assertEqual(plan["suite_id"], "issue-133-suite")
        self.assertEqual(plan["execution_mode"], "codex-plugin")
        self.assertEqual(plan["runner_mode"], "full-runner")
        self.assertEqual(plan["tasks"], [])
        evidence = self.read_json(run_dir / "evidence.json")
        self.assertEqual(evidence["images"], [])
        self.assertEqual(evidence["visual_acquisition"]["status"], "no_visual_tasks")

    def test_release_visible_filter_handles_null_task_id_and_drops_text_only(self) -> None:
        prepared = prepare_run(
            question="Release visual sidecar filtering",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            prompt_id="sem-reg-null-task-id",
            suite_id="issue-133-suite",
        )
        run_dir = Path(prepared["run_dir"])
        evidence = self.read_json(run_dir / "evidence.json")
        self.write_json(
            run_dir / "semantic_plan.json",
            {
                "schema_version": "codex-deepresearch.semantic-plan.v0",
                "semantic_plan": {
                    "bounded_tasks": [
                        {
                            "id": None,
                            "task_id": "task_001",
                            "angle_id": "angle_001",
                            "route": "visual_required",
                            "max_images": 1,
                        },
                        {
                            "id": None,
                            "task_id": "task_017",
                            "angle_id": "angle_005",
                            "route": "text_only",
                            "max_images": 0,
                        },
                    ]
                },
            },
        )
        records = [
            {
                "candidate_id": "cand_task_001",
                "task_id": "task_001",
                "angle_id": "angle_001",
                "route": "visual_required",
                "candidate_status": "fetched",
                "policy_decision": "allowed",
            },
            {
                "candidate_id": "cand_task_017",
                "task_id": "task_017",
                "angle_id": "angle_005",
                "route": "text_only",
                "candidate_status": "budget_pruned",
                "policy_decision": "budget_pruned",
            },
        ]

        filtered = _release_visible_visual_records_if_needed(run_dir, evidence, records)

        self.assertEqual([record["task_id"] for record in filtered], ["task_001"])
        self.assertEqual(filtered[0]["semantic_plan_task_id"], "task_001")
        self.assertTrue(filtered[0]["semantic_plan_hash"])
        self.assertEqual(filtered[0]["approved_delta_id"], "base_plan")

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

    def test_child_discovered_provider_resolves_legacy_nasa_alsj_image_urls(self) -> None:
        prepared = prepare_run(
            question="Find Apollo 11 image evidence",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        evidence = self.read_json(run_dir / "evidence.json")
        source = {
            "id": "src_nasa_alsj",
            "type": "web",
            "url": "https://www.hq.nasa.gov/alsj/a11/images11.html",
            "title": "Apollo 11 Image Library",
            "published_at": None,
            "accessed_at": "2026-06-26T00:00:00Z",
            "quality": "primary",
            "retrieval_status": "fetched",
            "local_artifact_path": "sources/src_nasa_alsj.html",
            "license_policy": "allowed",
            "robots_policy": "allowed",
            "policy_decision": "allowed",
            "policy_flags": [],
            "angle_id": "angle_001",
            "route": "visual_required",
        }
        original_url = "https://www.hq.nasa.gov/alsj/a11/AS11-40-5868HR.jpg"
        evidence["sources"] = [source]
        evidence["images"] = [
            {
                "id": "img_apollo11_001",
                "source_id": source["id"],
                "origin": "page_image",
                "page_url": source["url"],
                "image_url": original_url,
                "local_artifact_path": "evidence_shards/task_research_001/image_001.jpg",
                "mime_type": "image/jpeg",
                "width": 0,
                "height": 0,
                "observations": [],
                "inferences": [],
                "visual_tasks": ["image_claim_alignment"],
                "analysis_provider": "codex-interactive",
                "analysis_status": "skipped",
                "policy_flags": [],
                "caveats": [],
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        self.write_json(
            run_dir / "merge_status.json",
            {
                "schema_version": "codex-deepresearch.parallel-orchestration.v0",
                "status": "completed",
                "evidence_source": {
                    "type": "real_child_execution",
                    "real_child_execution": True,
                    "fixture_only": False,
                    "manual_handoff": False,
                    "accepted_shards": 1,
                },
            },
        )
        calls: list[str] = []

        def fake_child_fetch(url: str) -> FetchResponse:
            calls.append(url)
            return FetchResponse(
                content=(
                    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                    b"\x00\x00\x02\x80\x00\x00\x01\xe0\x08\x04\x00\x00\x00"
                    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00"
                    b"\x00\x00\x00\x00IEND\xaeB`\x82"
                ),
                mime_type="image/png",
                status_code=200,
                final_url=url,
            )

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["child-discovered-image-url"],
            child_image_transport=fake_child_fetch,
        )

        self.assertEqual(result["status"], "real_image_search_candidates_collected")
        self.assertEqual(
            calls,
            ["https://images-assets.nasa.gov/image/as11-40-5868/as11-40-5868~orig.jpg"],
        )
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        self.assertEqual(candidates[0]["provider"], "child-discovered-image-url")
        self.assertEqual(candidates[0]["candidate_status"], "fetched")
        self.assertEqual(candidates[0]["provider_provenance"]["original_child_image_url"], original_url)
        self.assertEqual(candidates[0]["provider_provenance"]["url_resolution"], "nasa_alsj_images_assets")
        self.assertTrue(candidates[0]["local_artifact_path"].startswith("images/cand_apollo11_001"))
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        self.assertEqual(fetches[0]["fetch_status"], "fetched")
        self.assertEqual(fetches[0]["provider"], "child-discovered-image-url")
        updated = self.read_json(run_dir / "evidence.json")
        self.assertEqual(updated["images"][0]["image_url"], calls[0])
        self.assertEqual(updated["images"][0]["provider_provenance"]["original_child_image_url"], original_url)

    def test_child_discovered_provider_uses_image_angle_task_for_mismatched_source_route(
        self,
    ) -> None:
        prepared = prepare_run(
            question="Find multi-angle child image evidence",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        self.write_json(
            run_dir / "visual_tasks.json",
            {
                "schema_version": "codex-deepresearch.visual-tasks.v0",
                "run_id": run_dir.name,
                "tasks": [
                    {
                        "id": "task_visual_001",
                        "angle_id": "angle_001",
                        "route": "visual_required",
                    },
                    {
                        "id": "task_visual_002",
                        "angle_id": "angle_002",
                        "route": "visual_required",
                    },
                ],
            },
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["routing"] = [
            {
                "id": "angle_001",
                "label": "Primary visual angle",
                "modality": "visual_required",
                "visual_tasks": ["image_claim_alignment"],
                "max_images": 12,
            },
            {
                "id": "angle_002",
                "label": "Secondary visual angle",
                "modality": "visual_required",
                "visual_tasks": ["image_claim_alignment"],
                "max_images": 12,
            },
        ]
        source = {
            "id": "src_angle_001",
            "type": "web",
            "url": "https://example.com/angle-001",
            "title": "Angle 001 Source",
            "published_at": None,
            "accessed_at": "2026-06-30T00:00:00Z",
            "quality": "primary",
            "retrieval_status": "fetched",
            "local_artifact_path": "sources/src_angle_001.html",
            "license_policy": "allowed",
            "robots_policy": "allowed",
            "policy_decision": "allowed",
            "policy_flags": [],
            "route": "visual_required",
            "angle_id": "angle_001",
            "search_result_id": "search_angle_001",
        }
        evidence["sources"] = [source]
        evidence["images"] = [
            {
                "id": "img_child_angle_001",
                "source_id": source["id"],
                "origin": "image_search",
                "page_url": source["url"],
                "image_url": "https://images.example.com/child-angle-001.png",
                "local_artifact_path": "evidence_shards/task_research_001/image_001.png",
                "mime_type": "image/png",
                "width": 640,
                "height": 480,
                "observations": [],
                "inferences": [],
                "visual_tasks": ["image_claim_alignment"],
                "analysis_provider": "codex-interactive",
                "analysis_status": "skipped",
                "policy_flags": [],
                "caveats": [],
                "angle_id": "angle_001",
                "route": "visual_required",
                "source_search_result_id": source["search_result_id"],
            },
            {
                "id": "img_child_angle_002",
                "source_id": source["id"],
                "origin": "image_search",
                "page_url": source["url"],
                "image_url": "https://images.example.com/child-angle-002.png",
                "local_artifact_path": "evidence_shards/task_research_002/image_001.png",
                "mime_type": "image/png",
                "width": 640,
                "height": 480,
                "observations": [],
                "inferences": [],
                "visual_tasks": ["image_claim_alignment"],
                "analysis_provider": "codex-interactive",
                "analysis_status": "skipped",
                "policy_flags": [],
                "caveats": [],
                "angle_id": "angle_002",
                "route": "visual_required",
                "source_search_result_id": source["search_result_id"],
            },
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        self.write_json(
            run_dir / "merge_status.json",
            {
                "schema_version": "codex-deepresearch.parallel-orchestration.v0",
                "status": "completed",
                "evidence_source": {
                    "type": "real_child_execution",
                    "real_child_execution": True,
                    "fixture_only": False,
                    "manual_handoff": False,
                    "accepted_shards": 2,
                },
            },
        )

        def fake_child_fetch(url: str) -> FetchResponse:
            return FetchResponse(
                content=(
                    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                    b"\x00\x00\x02\x80\x00\x00\x01\xe0\x08\x04\x00\x00\x00"
                    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00"
                    b"\x00\x00\x00\x00IEND\xaeB`\x82"
                    + url.rsplit("-", 1)[-1].encode("ascii")
                ),
                mime_type="image/png",
                status_code=200,
                final_url=url,
            )

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["child-discovered-image-url"],
            child_image_transport=fake_child_fetch,
        )

        self.assertEqual(result["status"], "real_image_search_candidates_collected")
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        child_candidates = {
            candidate["provider_provenance"]["source_image_id"]: candidate
            for candidate in candidates
            if candidate["provider"] == "child-discovered-image-url"
        }
        angle_002_candidate = child_candidates["img_child_angle_002"]
        self.assertEqual(angle_002_candidate["angle_id"], "angle_002")
        self.assertEqual(angle_002_candidate["task_id"], "task_visual_002")
        self.assertEqual(
            angle_002_candidate["plan_id"],
            "plan_task_visual_002_angle_002_visual_required",
        )

        plan = self.read_json(run_dir / "visual_search_plan.json")
        plan_angles_by_task: dict[str, set[str]] = {}
        for task in plan["tasks"]:
            plan_angles_by_task.setdefault(task["task_id"], set()).add(task["angle_id"])
        self.assertEqual(
            {task_id: angles for task_id, angles in plan_angles_by_task.items() if len(angles) > 1},
            {},
        )

        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        fetch_by_candidate = {fetch["candidate_id"]: fetch for fetch in fetches}
        angle_002_fetch = fetch_by_candidate[angle_002_candidate["candidate_id"]]
        self.assertEqual(angle_002_fetch["task_id"], "task_visual_002")
        self.assertEqual(angle_002_fetch["angle_id"], "angle_002")

        updated_images = self.read_json(run_dir / "evidence.json")["images"]
        angle_002_evidence_images = [
            image
            for image in updated_images
            if image.get("candidate_id") == angle_002_candidate["candidate_id"]
        ]
        self.assertEqual(len(angle_002_evidence_images), 1)
        self.assertEqual(angle_002_evidence_images[0]["task_id"], "task_visual_002")
        self.assertEqual(angle_002_evidence_images[0]["angle_id"], "angle_002")

    def test_child_discovered_provider_preserves_semantic_visual_plan_identity_and_lineage(
        self,
    ) -> None:
        prepared = prepare_run(
            question="Find release semantic visual lineage evidence",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            prompt_id="sem-reg-visual-lineage",
            suite_id="issue-133-suite",
        )
        run_dir = Path(prepared["run_dir"])
        semantic_hash = "a" * 64
        self.write_json(
            run_dir / "visual_tasks.json",
            {
                "schema_version": "codex-deepresearch.visual-tasks.v0",
                "run_id": run_dir.name,
                "tasks": [
                    {
                        "id": "task_visual_001",
                        "task_id": "task_visual_001",
                        "semantic_plan_task_id": "task_001",
                        "semantic_plan_hash": semantic_hash,
                        "approved_delta_id": "base_plan",
                        "angle_id": "angle_001",
                        "route": "visual_required",
                        "query": "official visual evidence for semantic task one",
                        "visual_tasks": ["image_claim_alignment"],
                        "max_images": 2,
                    }
                ],
            },
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["routing"] = [
            {
                "id": "angle_001",
                "label": "Primary visual angle",
                "modality": "visual_required",
                "visual_tasks": ["image_claim_alignment"],
                "max_images": 2,
            }
        ]
        source = {
            "id": "src_semantic_visual_lineage",
            "type": "web",
            "url": "https://example.com/semantic-visual-lineage",
            "title": "Semantic visual lineage source",
            "published_at": None,
            "accessed_at": "2026-07-09T00:00:00Z",
            "quality": "primary",
            "retrieval_status": "fetched",
            "local_artifact_path": "sources/semantic-visual-lineage.html",
            "license_policy": "allowed",
            "robots_policy": "allowed",
            "policy_decision": "allowed",
            "policy_flags": [],
            "search_result_id": "search_semantic_visual_lineage",
            "task_id": "task_001",
            "semantic_plan_task_id": "task_001",
            "semantic_plan_hash": semantic_hash,
            "approved_delta_id": "base_plan",
            "angle_id": "angle_001",
            "route": "visual_required",
        }
        evidence["sources"] = [source]
        allowed_image = {
            "id": "img_semantic_visual_lineage",
            "source_id": source["id"],
            "origin": "image_search",
            "page_url": source["url"],
            "image_url": "https://images.example.com/semantic-visual-lineage.png",
            "local_artifact_path": "evidence_shards/task_001/image_001.png",
            "mime_type": "image/png",
            "width": 640,
            "height": 480,
            "observations": [],
            "inferences": [],
            "visual_tasks": ["image_claim_alignment"],
            "analysis_provider": "codex-interactive",
            "analysis_status": "skipped",
            "policy_flags": [],
            "caveats": [],
            "source_search_result_id": source["search_result_id"],
            "task_id": "task_001",
            "semantic_plan_task_id": "task_001",
            "semantic_plan_hash": semantic_hash,
            "approved_delta_id": "base_plan",
            "angle_id": "angle_001",
            "route": "visual_required",
        }
        blocked_image = dict(allowed_image)
        blocked_image.update(
            {
                "id": "img_policy_blocked_release_candidate",
                "image_url": "https://images.example.com/policy-blocked.png",
                "analysis_status": "policy_blocked",
                "policy_decision": "blocked",
                "policy_flags": ["license_policy_blocked"],
            }
        )
        evidence["images"] = [allowed_image, blocked_image]
        self.write_json(run_dir / "evidence.json", evidence)
        self.write_json(
            run_dir / "merge_status.json",
            {
                "schema_version": "codex-deepresearch.parallel-orchestration.v0",
                "status": "completed",
                "evidence_source": {
                    "type": "real_child_execution",
                    "real_child_execution": True,
                    "fixture_only": False,
                    "manual_handoff": False,
                    "accepted_shards": 1,
                },
            },
        )

        def fake_child_fetch(url: str) -> FetchResponse:
            return FetchResponse(
                content=(
                    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                    b"\x00\x00\x02\x80\x00\x00\x01\xe0\x08\x04\x00\x00\x00"
                    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00"
                    b"\x00\x00\x00\x00IEND\xaeB`\x82"
                ),
                mime_type="image/png",
                status_code=200,
                final_url=url,
            )

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["child-discovered-image-url"],
            child_image_transport=fake_child_fetch,
        )

        self.assertEqual(result["status"], "real_image_search_candidates_collected")
        plan = self.read_json(run_dir / "visual_search_plan.json")
        self.assertEqual(plan["prompt_id"], "sem-reg-visual-lineage")
        self.assertEqual(plan["suite_id"], "issue-133-suite")
        self.assertEqual(plan["execution_mode"], "codex-plugin")
        self.assertEqual(plan["runner_mode"], "full-runner")
        self.assertEqual(len(plan["tasks"]), 1)
        self.assertEqual(plan["tasks"][0]["task_id"], "task_001")
        self.assertEqual(plan["tasks"][0]["semantic_plan_task_id"], "task_001")
        self.assertEqual(plan["tasks"][0]["semantic_plan_hash"], semantic_hash)
        self.assertEqual(plan["tasks"][0]["approved_delta_id"], "base_plan")

        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        self.assertEqual(
            {candidate["evidence_image_id"] for candidate in candidates},
            {"img_semantic_visual_lineage"},
        )
        self.assertTrue(
            all(candidate.get("policy_decision") == "allowed" for candidate in candidates),
            candidates,
        )
        candidate = candidates[0]
        self.assertEqual(candidate["task_id"], "task_001")
        self.assertEqual(candidate["semantic_plan_task_id"], "task_001")
        self.assertEqual(candidate["semantic_plan_hash"], semantic_hash)
        self.assertEqual(candidate["approved_delta_id"], "base_plan")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        self.assertEqual(
            {fetch["evidence_image_id"] for fetch in fetches},
            {"img_semantic_visual_lineage"},
        )
        self.assertTrue(
            all(fetch.get("policy_decision") == "allowed" for fetch in fetches),
            fetches,
        )
        fetch = fetches[0]
        self.assertEqual(fetch["task_id"], "task_001")
        self.assertEqual(fetch["semantic_plan_task_id"], "task_001")
        self.assertEqual(fetch["semantic_plan_hash"], semantic_hash)
        self.assertEqual(fetch["approved_delta_id"], "base_plan")

    def test_child_discovered_provider_registers_synthetic_visual_plan_tasks(self) -> None:
        prepared = prepare_run(
            question="Find generated visual task lineage evidence",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            angles=[
                "Visual angle one",
                "Visual angle two",
                "Visual angle three",
                "Visual angle four",
                "Visual angle five",
                "Visual angle six",
            ],
        )
        run_dir = Path(prepared["run_dir"])
        visual_tasks = self.read_json(run_dir / "visual_tasks.json")
        visual_tasks["tasks"] = [
            task
            for task in visual_tasks["tasks"]
            if task.get("id") not in {"task_visual_005", "task_visual_006"}
        ]
        self.write_json(run_dir / "visual_tasks.json", visual_tasks)
        evidence = self.read_json(run_dir / "evidence.json")
        source = {
            "id": "src_synthetic_visual_tasks",
            "type": "web",
            "url": "https://example.com/synthetic-visual-tasks",
            "title": "Synthetic Visual Task Source",
            "published_at": None,
            "accessed_at": "2026-07-09T00:00:00Z",
            "quality": "primary",
            "retrieval_status": "fetched",
            "local_artifact_path": "sources/synthetic-visual-tasks.html",
            "license_policy": "allowed",
            "robots_policy": "allowed",
            "policy_decision": "allowed",
            "policy_flags": [],
            "angle_id": "angle_001",
            "route": "visual_required",
            "search_result_id": "search_synthetic_visual_tasks",
        }
        evidence["sources"] = [source]
        evidence["images"] = [
            {
                "id": f"img_child_synthetic_angle_{index:03d}",
                "source_id": source["id"],
                "origin": "image_search",
                "page_url": source["url"],
                "image_url": f"https://images.example.com/synthetic-angle-{index}.png",
                "local_artifact_path": f"evidence_shards/task_research_{index:03d}/image_001.png",
                "mime_type": "image/png",
                "width": 640,
                "height": 480,
                "observations": [],
                "inferences": [],
                "visual_tasks": ["image_claim_alignment"],
                "analysis_provider": "codex-interactive",
                "analysis_status": "skipped",
                "policy_flags": [],
                "caveats": [],
                "angle_id": f"angle_{index:03d}",
                "route": "visual_required",
                "source_search_result_id": source["search_result_id"],
            }
            for index in (5, 6)
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        self.write_json(
            run_dir / "merge_status.json",
            {
                "schema_version": "codex-deepresearch.parallel-orchestration.v0",
                "status": "completed",
                "evidence_source": {
                    "type": "real_child_execution",
                    "real_child_execution": True,
                    "fixture_only": False,
                    "manual_handoff": False,
                    "accepted_shards": 2,
                },
            },
        )

        def fake_child_fetch(url: str) -> FetchResponse:
            return FetchResponse(
                content=(
                    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                    b"\x00\x00\x02\x80\x00\x00\x01\xe0\x08\x04\x00\x00\x00"
                    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00"
                    b"\x00\x00\x00\x00IEND\xaeB`\x82"
                    + url.rsplit("-", 1)[-1].encode("ascii")
                ),
                mime_type="image/png",
                status_code=200,
                final_url=url,
            )

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["child-discovered-image-url"],
            child_image_transport=fake_child_fetch,
        )

        self.assertEqual(result["status"], "real_image_search_candidates_collected")
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        synthetic_task_ids = {"task_visual_005", "task_visual_006"}
        self.assertEqual({candidate["task_id"] for candidate in candidates}, synthetic_task_ids)

        plan = self.read_json(run_dir / "visual_search_plan.json")
        plan_task_ids = {task["task_id"] for task in plan["tasks"]}
        self.assertTrue(synthetic_task_ids <= plan_task_ids)
        registered_task_ids = {
            task["id"] for task in self.read_json(run_dir / "visual_tasks.json")["tasks"]
        }
        self.assertTrue(synthetic_task_ids <= registered_task_ids)

        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(
            visual_validation.valid,
            [error.to_dict() for error in visual_validation.errors],
        )

    def test_page_extractor_remote_html_fallback_covers_failed_child_image_urls(
        self,
    ) -> None:
        prepared = prepare_run(
            question="Find official JWST first image evidence",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        evidence = self.read_json(run_dir / "evidence.json")
        source = {
            "id": "src_jwst_first_images",
            "type": "web",
            "url": "https://science.example.test/mission/webb/webbs-first-images/",
            "title": "Webb first images",
            "published_at": None,
            "accessed_at": "2026-06-30T00:00:00Z",
            "quality": "primary",
            "retrieval_status": "fetched",
            "local_artifact_path": "evidence_shards/task_research_001/source_001.html",
            "license_policy": "allowed",
            "robots_policy": "allowed",
            "policy_decision": "allowed",
            "policy_flags": [],
            "angle_id": "angle_001",
            "route": "visual_required",
            "search_result_id": "search_jwst_first_images",
        }
        evidence["sources"] = [source]
        evidence["images"] = [
            {
                "id": f"img_child_jwst_stale_{index:03d}",
                "source_id": source["id"],
                "origin": "image_search",
                "page_url": source["url"],
                "image_url": f"https://images-assets.example.test/stale-{index}.jpg",
                "local_artifact_path": f"evidence_shards/task_research_001/stale_{index:03d}.jpg",
                "mime_type": "image/jpeg",
                "width": 0,
                "height": 0,
                "observations": [],
                "inferences": [],
                "visual_tasks": ["image_claim_alignment"],
                "analysis_provider": "codex-interactive",
                "analysis_status": "skipped",
                "policy_flags": [],
                "caveats": [],
                "angle_id": "angle_001",
                "route": "visual_required",
                "source_search_result_id": source["search_result_id"],
            }
            for index in range(1, 4)
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        self.write_json(
            run_dir / "merge_status.json",
            {
                "schema_version": "codex-deepresearch.parallel-orchestration.v0",
                "status": "completed",
                "evidence_source": {
                    "type": "real_child_execution",
                    "real_child_execution": True,
                    "fixture_only": False,
                    "manual_handoff": False,
                    "accepted_shards": 1,
                },
            },
        )

        def child_transport(url: str) -> FetchResponse:
            return FetchResponse(
                content=None,
                mime_type=None,
                status_code=404,
                final_url=url,
                error_code="fetch_failed:HTTPError",
            )

        def page_transport(url: str) -> FetchResponse:
            if url == source["url"]:
                html = """
                    <html><body>
                      <img src="/media/jwst-1.png" alt="JWST image 1">
                      <img src="/media/jwst-2.png" alt="JWST image 2">
                      <img src="/media/jwst-3.png" alt="JWST image 3">
                    </body></html>
                """
                return FetchResponse(
                    content=html.encode("utf-8"),
                    mime_type="text/html",
                    status_code=200,
                    final_url=url,
                )
            return FetchResponse(
                content=b"\x89PNG\r\n\x1a\n" + url.encode("ascii"),
                mime_type="image/png",
                status_code=200,
                final_url=url,
            )

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["child-discovered-image-url", "page-image-extractor"],
            child_image_transport=child_transport,
            page_image_transport=page_transport,
            max_fetches=3,
        )

        self.assertEqual(result["status"], "real_image_search_candidates_collected")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        child_fetches = [
            fetch for fetch in fetches if fetch["provider"] == "child-discovered-image-url"
        ]
        page_fetches = [
            fetch for fetch in fetches if fetch["provider"] == "page-image-extractor"
        ]
        self.assertEqual({fetch["fetch_status"] for fetch in child_fetches}, {"failed"})
        self.assertEqual(
            len([fetch for fetch in page_fetches if fetch["fetch_status"] == "fetched"]),
            3,
        )
        minimums = visual_minimums_for_run(run_dir)
        self.assertEqual(minimums["fetched_artifacts"], 3)
        self.assertNotEqual(minimums["shortfall_reason"], "fetch_failures")
        page_status = self.read_json(run_dir / "page_image_extraction_status.json")
        self.assertEqual(page_status["remote_page_fetches"], 1)
        self.assertEqual(page_status["remote_page_fetch_status_counts"], {"fetched": 1})

    def test_page_extractor_og_origin_is_schema_valid_after_vision_ingest(self) -> None:
        prepared = prepare_run(
            question="Analyze Open Graph visual evidence",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        evidence = self.read_json(run_dir / "evidence.json")
        source = {
            "id": "src_og_visual_page",
            "type": "web",
            "url": "https://science.example.test/og-visual/",
            "title": "OG visual page",
            "published_at": None,
            "accessed_at": "2026-06-30T00:00:00Z",
            "quality": "primary",
            "retrieval_status": "fetched",
            "local_artifact_path": "sources/missing-og-page.html",
            "license_policy": "allowed",
            "robots_policy": "allowed",
            "policy_decision": "allowed",
            "policy_flags": [],
            "angle_id": "angle_001",
            "route": "visual_required",
            "search_result_id": "search_og_visual_page",
        }
        evidence["sources"] = [source]
        self.write_json(run_dir / "evidence.json", evidence)

        def page_transport(url: str) -> FetchResponse:
            if url == source["url"]:
                return FetchResponse(
                    content=(
                        b'<html><head><meta property="og:image" '
                        b'content="/media/og-visual.png"></head><body></body></html>'
                    ),
                    mime_type="text/html",
                    status_code=200,
                    final_url=url,
                )
            return FetchResponse(
                content=b"\x89PNG\r\n\x1a\n" + url.encode("ascii"),
                mime_type="image/png",
                status_code=200,
                final_url=url,
            )

        acquire = acquire_visual_candidates(
            run=run_dir,
            providers=["page-image-extractor"],
            page_image_transport=page_transport,
            max_fetches=1,
        )

        self.assertEqual(acquire["status"], "real_image_search_candidates_collected")
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        self.assertEqual(candidates[0]["origin"], "page_image")
        self.assertEqual(candidates[0]["candidate_origin"], "open_graph")
        self.assertEqual(candidates[0]["html_origin"], "open_graph")
        evidence = self.read_json(run_dir / "evidence.json")
        image = evidence["images"][0]
        self.assertEqual(image["origin"], "page_image")
        self.assertEqual(image["candidate_origin"], "open_graph")
        self.assertEqual(image["html_origin"], "open_graph")

        observations_path = run_dir / "og_observations.jsonl"
        observations_path.write_text(
            json.dumps(
                {
                    "id": image["id"],
                    "image": image,
                    "source_id": image["source_id"],
                    "local_artifact_path": image["local_artifact_path"],
                    "mime_type": image["mime_type"],
                    "width": image["width"],
                    "height": image["height"],
                    "observations": ["The Open Graph image shows relevant visual evidence."],
                    "inferences": [],
                    "visual_tasks": ["image_claim_alignment"],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        ingest = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            observations=observations_path,
        )

        self.assertEqual(ingest["status"], "visual_evidence_ingested")
        validation = validate_artifacts(
            evidence_path=run_dir / "evidence.json",
            visual_observations_path=run_dir / "visual_observations.jsonl",
        )
        self.assertTrue(validation.valid, [error.to_dict() for error in validation.errors])
        updated = self.read_json(run_dir / "evidence.json")
        updated_image = next(item for item in updated["images"] if item["id"] == image["id"])
        self.assertEqual(updated_image["origin"], "page_image")
        self.assertEqual(updated_image["candidate_origin"], "open_graph")
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        self.assertEqual(observations[0]["origin"], "page_image")
        self.assertEqual(observations[0]["candidate_origin"], "open_graph")
        self.assertEqual(observations[0]["html_origin"], "open_graph")

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

    def test_mixed_real_providers_keep_missing_browser_dependency_as_diagnostic(self) -> None:
        prepared = prepare_run(
            question="Find image and screenshot evidence with one unavailable real provider",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        brave_transport = FakeBraveImageTransport(count=3)
        browser_transport = MissingDependencyBrowserScreenshotTransport()

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
        self.assertEqual(browser_transport.calls, [])
        self.assertEqual(result["candidate_records"], 3)
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        self.assertEqual({candidate["provider"] for candidate in candidates}, {"brave-image-search"})

        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        self.assertTrue(provider_status["ok"])
        self.assertEqual(provider_status["status"], "real_image_search_candidates_collected")
        browser_provider = [
            provider
            for provider in provider_status["providers"]
            if provider["provider"] == "browser-screenshot"
        ][0]
        self.assertFalse(browser_provider["available"])
        self.assertEqual(browser_provider["blocked_reason"], "missing_browser_dependency")
        self.assertEqual(browser_provider["invocations"], 0)
        self.assertEqual(
            browser_provider["diagnostics"]["install_guidance"],
            [
                "python3 -m playwright install chromium",
                "python3 -m playwright install-deps chromium",
            ],
        )

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
