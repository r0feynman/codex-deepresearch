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

from deepresearch import ingest_vision_observations, prepare_run, validate_artifacts


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00\xef\xbf\xa7\xdb"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class VisionAdapterTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def load_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def write_jsonl(self, path: Path, records: list[dict]) -> None:
        path.write_text(
            "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
            encoding="utf-8",
        )

    def prepared_visual_run(self, provider: str = "codex-interactive") -> Path:
        runs_dir = self.temp_runs_dir()
        prepared = prepare_run(
            question="Compare checkout UI screenshots",
            runs_dir=runs_dir,
            route="visual_required",
            vlm_provider=provider,
        )
        run_dir = Path(prepared["run_dir"])
        images_dir = run_dir / "images"
        sources_dir = run_dir / "sources"
        images_dir.mkdir(exist_ok=True)
        sources_dir.mkdir(exist_ok=True)
        (images_dir / "checkout.png").write_bytes(PNG_1X1)

        evidence = self.load_json(run_dir / "evidence.json")
        source = {
            "id": "src_checkout",
            "type": "image",
            "url": "https://example.com/checkout",
            "title": "Checkout screenshot",
            "published_at": None,
            "accessed_at": "2026-06-22T00:00:00Z",
            "quality": "unknown",
            "retrieval_status": "manual",
            "local_artifact_path": "sources/src_checkout.json",
            "license_policy": "manual_review",
            "robots_policy": "manual_review",
        }
        evidence["sources"] = [source]
        self.write_json(run_dir / "evidence.json", evidence)
        self.write_json(sources_dir / "src_checkout.json", source)
        return run_dir

    def assert_valid_run(self, run_dir: Path) -> dict:
        result = validate_artifacts(
            evidence_path=run_dir / "evidence.json",
            visual_observations_path=run_dir / "visual_observations.jsonl",
        )
        self.assertTrue(result.valid, [error.to_dict() for error in result.errors])
        return self.load_json(run_dir / "evidence.json")

    def test_codex_interactive_accepts_handoff_observations(self) -> None:
        run_dir = self.prepared_visual_run()
        observations_path = run_dir / "codex_handoff.jsonl"
        self.write_jsonl(
            observations_path,
            [
                {
                    "id": "img_checkout",
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "local_artifact_path": "images/checkout.png",
                    "mime_type": "image/png",
                    "width": 1,
                    "height": 1,
                    "observations": ["The screenshot shows a primary checkout button."],
                    "inferences": ["The visual evidence supports a checkout UI claim."],
                    "visual_tasks": ["layout_review"],
                }
            ],
        )

        result = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            observations=observations_path,
        )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        evidence = self.assert_valid_run(run_dir)
        image = evidence["images"][0]
        self.assertEqual(image["analysis_provider"], "codex-interactive")
        self.assertEqual(image["analysis_status"], "analyzed")
        self.assertEqual(image["observations"], ["The screenshot shows a primary checkout button."])
        self.assertEqual(image["artifact_size_bytes"], len(PNG_1X1))
        self.assertEqual(image["source_url"], "https://example.com/checkout")
        self.assertTrue(image["cache_key"].startswith("image:codex-deepresearch.cache-keys.v0:sha256:"))
        self.assertTrue(image["hash"].startswith("sha256:"))

        first_cache_key = image["cache_key"]
        first_analyzed_at = image["analyzed_at"]
        resumed = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            observations=observations_path,
        )
        resumed_evidence = self.assert_valid_run(run_dir)
        self.assertEqual(resumed["images_reused"], 1)
        self.assertEqual(resumed_evidence["images"][0]["cache_key"], first_cache_key)
        self.assertEqual(resumed_evidence["images"][0]["analyzed_at"], first_analyzed_at)

    def test_openai_responses_vision_fixture_emits_visual_evidence_schema(self) -> None:
        run_dir = self.prepared_visual_run(provider="openai-responses-vision")
        observations_path = run_dir / "openai_adapter_response.jsonl"
        self.write_jsonl(
            observations_path,
            [
                {
                    "image_id": "checkout-openai",
                    "source_id": "src_checkout",
                    "image": {
                        "local_artifact_path": "images/checkout.png",
                        "mime_type": "image/png",
                        "width": 1,
                        "height": 1,
                        "page_url": "https://example.com/checkout",
                    },
                    "response": {
                        "output_text": "The image contains a checkout button.",
                        "inferences": ["The UI state is visually inspectable."],
                        "model": "dry-openai-vision-fixture",
                    },
                }
            ],
        )

        result = ingest_vision_observations(
            run=run_dir,
            provider="openai-responses-vision",
            observations=observations_path,
        )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        evidence = self.assert_valid_run(run_dir)
        image = evidence["images"][0]
        self.assertEqual(image["analysis_provider"], "openai-responses-vision")
        self.assertIn("The image contains a checkout button.", image["observations"])
        self.assertEqual(image["inferences"], ["The UI state is visually inspectable."])

    def test_manual_visual_review_emits_visual_evidence_schema(self) -> None:
        run_dir = self.prepared_visual_run(provider="manual-visual-review")
        observations_path = run_dir / "manual_review.jsonl"
        self.write_jsonl(
            observations_path,
            [
                {
                    "image_id": "manual-review-1",
                    "source_id": "src_checkout",
                    "local_artifact_path": "images/checkout.png",
                    "human_observations": ["Reviewer sees the logo and checkout form."],
                    "inferences": ["Human review confirms the visible UI state."],
                    "width": 1,
                    "height": 1,
                }
            ],
        )

        result = ingest_vision_observations(
            run=run_dir,
            provider="manual-visual-review",
            observations=observations_path,
        )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        evidence = self.assert_valid_run(run_dir)
        image = evidence["images"][0]
        self.assertEqual(image["analysis_provider"], "manual-visual-review")
        self.assertEqual(image["origin"], "manual")
        self.assertEqual(image["observations"], ["Reviewer sees the logo and checkout form."])

    def test_visual_required_without_result_creates_needs_visual_evidence_claim(self) -> None:
        run_dir = self.prepared_visual_run()

        result = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            observations=run_dir / "visual_observations.jsonl",
        )

        self.assertEqual(result["status"], "needs_visual_evidence")
        evidence = self.assert_valid_run(run_dir)
        self.assertEqual(evidence["images"], [])
        self.assertEqual(len(evidence["claims"]), 1)
        claim = evidence["claims"][0]
        self.assertEqual(claim["verification_status"], "needs_visual_evidence")
        self.assertEqual(claim["confidence"], "low")
        self.assertEqual(claim["supporting_images"], [])

    def test_missing_local_artifact_fails_normalization(self) -> None:
        run_dir = self.prepared_visual_run()
        observations_path = run_dir / "missing_artifact.jsonl"
        self.write_jsonl(
            observations_path,
            [
                {
                    "image_id": "missing-artifact",
                    "source_id": "src_checkout",
                    "local_artifact_path": "images/missing.png",
                    "observations": ["This should not become analyzed evidence."],
                    "width": 1,
                    "height": 1,
                }
            ],
        )

        result = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            observations=observations_path,
        )

        self.assertEqual(result["status"], "failed_normalization")
        self.assertEqual(result["errors"][0]["code"], "normalization_failed")
        self.assertIn("local_artifact_path", result["errors"][0]["detail"])
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["images"], [])

    def test_invalid_image_url_fails_normalization(self) -> None:
        run_dir = self.prepared_visual_run()
        observations_path = run_dir / "invalid_url.jsonl"
        self.write_jsonl(
            observations_path,
            [
                {
                    "image_id": "bad-url",
                    "source_id": "src_checkout",
                    "local_artifact_path": "images/checkout.png",
                    "image_url": "not a url",
                    "observations": ["This should not become analyzed evidence."],
                    "width": 1,
                    "height": 1,
                }
            ],
        )

        result = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            observations=observations_path,
        )

        self.assertEqual(result["status"], "failed_normalization")
        self.assertEqual(result["errors"][0]["code"], "normalization_failed")
        self.assertIn("image_url", result["errors"][0]["detail"])
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["images"], [])

    def test_invalid_page_url_fails_normalization(self) -> None:
        run_dir = self.prepared_visual_run()
        observations_path = run_dir / "invalid_page_url.jsonl"
        self.write_jsonl(
            observations_path,
            [
                {
                    "image_id": "bad-page-url",
                    "source_id": "src_checkout",
                    "local_artifact_path": "images/checkout.png",
                    "page_url": "not a url",
                    "observations": ["This should not become analyzed evidence."],
                    "width": 1,
                    "height": 1,
                }
            ],
        )

        result = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            observations=observations_path,
        )

        self.assertEqual(result["status"], "failed_normalization")
        self.assertEqual(result["errors"][0]["code"], "normalization_failed")
        self.assertIn("page_url", result["errors"][0]["detail"])
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["images"], [])

    def test_cli_ingest_vision_normalizes_observations(self) -> None:
        run_dir = self.prepared_visual_run()
        observations_path = run_dir / "cli_observations.jsonl"
        self.write_jsonl(
            observations_path,
            [
                {
                    "image_id": "cli-vision",
                    "source_id": "src_checkout",
                    "local_artifact_path": "images/checkout.png",
                    "observations": ["CLI observation"],
                    "width": 1,
                    "height": 1,
                }
            ],
        )

        ingest = subprocess.run(
            [
                str(RUNNER),
                "ingest-vision",
                "--run",
                str(run_dir),
                "--provider",
                "codex-interactive",
                "--observations",
                str(observations_path),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(ingest.returncode, 0, ingest.stderr)
        payload = json.loads(ingest.stdout)
        self.assertEqual(payload["status"], "visual_evidence_ingested")
        self.assertEqual(payload["images_ingested"], 1)
        self.assert_valid_run(run_dir)


if __name__ == "__main__":
    unittest.main()
