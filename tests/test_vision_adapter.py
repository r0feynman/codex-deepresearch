from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "plugins" / "codex-deepresearch" / "scripts" / "codex-deepresearch"
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import (
    ingest_vision_observations,
    prepare_run,
    synthesize_report,
    validate_artifacts,
    validate_visual_artifacts,
    verify_claims,
)
from deepresearch.vision_adapter import (
    OpenAIResponsesVisionResult,
    _openai_result_from_response_payload,
)

prepare_search_handoff_run = prepare_run


def prepare_run(*args, **kwargs):
    kwargs.setdefault("angles", ["primary source discovery"])
    kwargs.setdefault("_allow_release_ineligible_materialization_for_tests", True)
    return prepare_search_handoff_run(*args, **kwargs)


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00\xef\xbf\xa7\xdb"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeOpenAIResponsesVisionClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def analyze_image(self, *, image_input, mime_type, prompt, config, metadata):
        self.calls.append(
            {
                "image_input": image_input,
                "mime_type": mime_type,
                "prompt": prompt,
                "model": config.model,
                "metadata": dict(metadata),
            }
        )
        return OpenAIResponsesVisionResult(
            observations=(
                "The screenshot visibly contains a checkout button.",
                "The visible OCR text says Pay now.",
            ),
            inferences=("The UI is likely ready for checkout submission.",),
            caveats=("Small text may be approximate.",),
            ocr_text="Pay now",
            confidence=0.82,
            response_id="resp_fake_vision",
            model=config.model,
            usage={"input_tokens": 123, "api_key": "redaction-sentinel"},
            raw_provider_metadata={
                "response_id": "resp_fake_vision",
                "authorization": "Bearer redaction-sentinel",
                "endpoint": config.endpoint,
                "benign_exact_secret": f"echoed {config.api_key}",
                "credential_hint": "credential-secret-value",
                "credentials_blob": "credentials-secret-value",
                "benign_bearer_text": "Authorization: Bearer bearer-secret-value",
                "source_path": "/home/user/private/source.png",
                "nested": {
                    "api_key": "redaction-sentinel",
                    "items": [
                        "Authorization: Bearer nested-token-value",
                        {"path": "/home/user/private/nested-source.png"},
                    ],
                },
            },
            actual_cost_usd=0.004,
        )


class FakeCodexInteractiveVisionClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def analyze_image(self, *, image_path, mime_type, prompt, config, metadata):
        self.calls.append(
            {
                "image_path": image_path,
                "mime_type": mime_type,
                "prompt": prompt,
                "model": config.model,
                "metadata": dict(metadata),
            }
        )
        return OpenAIResponsesVisionResult(
            observations=(
                "The screenshot visibly contains a checkout button.",
                "The visible OCR text says Pay now.",
            ),
            inferences=("The UI likely supports checkout submission.",),
            caveats=("Small text may be approximate.",),
            ocr_text="Pay now",
            confidence=0.83,
            response_id="codex_fake_vision",
            model=config.model,
            usage={"events": 3},
            raw_provider_metadata={
                "response_id": "codex_fake_vision",
                "source_path": "/home/user/private/codex-image.png",
            },
            actual_cost_usd=0.0,
        )


class FreeFormOpenAIResponsesVisionClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    def analyze_image(self, *, image_input, mime_type, prompt, config, metadata):
        self.calls.append({"image_input": image_input, "metadata": dict(metadata)})
        return _openai_result_from_response_payload(
            {
                "id": "resp_freeform",
                "model": config.model,
                "output_text": self.text,
                "usage": {"input_tokens": 22},
            },
            fallback_model=config.model,
            elapsed_ms=7,
            mime_type=mime_type,
            metadata=metadata,
        )


class HostileOpenAIResponsesVisionClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def analyze_image(self, *, image_input, mime_type, prompt, config, metadata):
        self.calls.append({"image_input": image_input, "metadata": dict(metadata)})
        return OpenAIResponsesVisionResult(
            observations=(
                "Visible safe label; credential_hint=credential-secret-value",
                f"Configured exact secret echoed as {config.api_key}",
            ),
            inferences=(
                "credentials_blob: 'credentials-secret-value' while preserving safe inference",
            ),
            caveats=(
                "Authorization: Bearer bearer-secret-value",
                "Source path /home/user/private/source.png is not public",
            ),
            ocr_text="OCR token=nested-token-value and safe OCR text",
            confidence=0.66,
            response_id="resp_hostile_vision",
            model=config.model,
            usage={
                "input_tokens": 12,
                "note": "access_token=usage-token-value",
            },
            raw_provider_metadata={
                "note": (
                    "credential_hint=credential-secret-value "
                    "credentials_blob=credentials-secret-value "
                    "Authorization: Bearer raw-bearer-value "
                    "/home/user/private/source.png"
                ),
                "nested": [
                    "auth: nested-auth-value",
                    {"safe_key_name_but_private_path": "/home/user/private/nested-source.png"},
                ],
            },
            actual_cost_usd=0.001,
        )


class FailingSecondOpenAIResponsesVisionClient(FakeOpenAIResponsesVisionClient):
    def analyze_image(self, *, image_input, mime_type, prompt, config, metadata):
        if len(self.calls) >= 1:
            self.calls.append(
                {
                    "image_input": image_input,
                    "mime_type": mime_type,
                    "prompt": prompt,
                    "model": config.model,
                    "metadata": dict(metadata),
                    "failed": True,
                }
            )
            raise RuntimeError("provider request failed after first image")
        return super().analyze_image(
            image_input=image_input,
            mime_type=mime_type,
            prompt=prompt,
            config=config,
            metadata=metadata,
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

    def write_openai_vlm_handoff(
        self,
        run_dir: Path,
        *,
        fetch_evidence_image_id: str | None = "img_checkout_001",
    ) -> None:
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["mode"] = "automated-cli"
        evidence["vlm_provider"] = "openai-responses-vision"
        self.write_json(run_dir / "evidence.json", evidence)
        created_at = "2026-06-25T00:00:00Z"
        task_id = "task_visual_001"
        angle_id = "angle_001"
        route = "visual_required"
        plan_id = f"plan_{task_id}"
        candidate_id = "cand_checkout_001"
        fetch_id = "fetch_checkout_001"
        evidence_image_id = "img_checkout_001"
        provider_provenance = {
            "provider": "browser-screenshot",
            "provider_kind": "screenshot",
            "provider_mode": "real",
            "provider_run_id": "browser-screenshot:test",
            "external_network_call": False,
            "external_vlm_call": False,
        }
        self.write_json(
            run_dir / "visual_search_plan.json",
            {
                "schema_version": "codex-deepresearch.visual-artifacts.v0",
                "run_id": run_dir.name,
                "created_at": created_at,
                "tasks": [
                    {
                        "plan_id": plan_id,
                        "task_id": task_id,
                        "angle_id": angle_id,
                        "route": route,
                        "target_evidence_type": "screenshot",
                        "query": "checkout screenshot",
                        "providers": ["browser-screenshot"],
                        "source_search_result_ids": [],
                        "caps": {
                            "max_candidates": 1,
                            "max_fetches": 1,
                            "max_vlm_images": 1,
                            "max_cost_usd": 0.25,
                        },
                        "policy_constraints": {"policy_decision": "allowed"},
                        "estimated_cost_usd": 0.01,
                        "state": "completed",
                    }
                ],
            },
        )
        self.write_jsonl(
            run_dir / "visual_candidates.jsonl",
            [
                {
                    "candidate_id": candidate_id,
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "angle_id": angle_id,
                    "route": route,
                    "provider": "browser-screenshot",
                    "provider_kind": "screenshot",
                    "provider_mode": "real",
                    "provider_run_id": "browser-screenshot:test",
                    "provider_provenance": provider_provenance,
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "page_url": "https://example.com/checkout",
                    "image_url": None,
                    "rank": 1,
                    "score": 1.0,
                    "candidate_class": "screenshot",
                    "local_artifact_path": "images/checkout.png",
                    "mime_type": "image/png",
                    "width": 1,
                    "height": 1,
                    "hash": "sha256:fixture-checkout",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "candidate_status": "fetched",
                    "rejection_reason": None,
                    "estimated_cost_usd": 0.01,
                    "actual_cost_usd": 0.01,
                    "visual_tasks": ["layout_review", "ocr"],
                    "requires_vlm_observation": True,
                }
            ],
        )
        self.write_jsonl(
            run_dir / "image_fetch_status.jsonl",
            [
                {
                    "fetch_id": fetch_id,
                    "candidate_id": candidate_id,
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "angle_id": angle_id,
                    "route": route,
                    "provider": "browser-screenshot",
                    "provider_kind": "screenshot",
                    "provider_mode": "real",
                    "provider_run_id": "browser-screenshot:test",
                    "provider_provenance": provider_provenance,
                    "fetch_status": "fetched",
                    "http_status": 200,
                    "mime_type": "image/png",
                    "byte_size": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "hash": "sha256:fixture-checkout",
                    "phash": "checkout-phash",
                    "local_artifact_path": "images/checkout.png",
                    "evidence_image_id": fetch_evidence_image_id,
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "failure_code": None,
                    "estimated_cost_usd": 0.01,
                    "actual_cost_usd": 0.01,
                }
            ],
        )
        self.write_json(
            run_dir / "visual_provider_status.json",
            {
                "schema_version": "codex-deepresearch.visual-provider-status.v0",
                "run_id": run_dir.name,
                "status": "real_image_search_candidates_collected",
                "ok": True,
                "terminal": False,
                "metric_classification": "real_provider_candidate_discovery",
                "providers": [
                    {
                        "provider": "browser-screenshot",
                        "provider_kind": "screenshot",
                        "provider_mode": "real",
                        "configured": True,
                        "available": True,
                        "blocked_reason": None,
                        "invocations": 1,
                        "candidates_discovered": 1,
                        "artifacts_fetched": 1,
                        "vlm_images_analyzed": 0,
                        "estimated_cost_usd": 0.01,
                        "actual_cost_usd": 0.01,
                        "last_error": None,
                    }
                ],
            },
        )

    def write_codex_vlm_handoff(self, run_dir: Path) -> None:
        self.write_openai_vlm_handoff(run_dir)
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["mode"] = "codex-plugin"
        evidence["vlm_provider"] = "codex-interactive"
        for source in evidence.get("sources", []):
            if isinstance(source, dict) and source.get("id") == "src_checkout":
                source["policy_decision"] = "allowed"
                source["license_policy"] = "allowed"
                source["robots_policy"] = "allowed"
        self.write_json(run_dir / "evidence.json", evidence)

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

    def test_corrected_observation_rerun_replaces_previous_image_analysis(self) -> None:
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
                    "observations": ["Old observation"],
                    "inferences": ["Old inference"],
                    "visual_tasks": ["layout_review"],
                }
            ],
        )
        ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            observations=observations_path,
        )
        evidence = self.assert_valid_run(run_dir)
        old_image = evidence["images"][0]
        old_cache_key = old_image["cache_key"]
        evidence["images"][0].pop("cache_key", None)
        self.write_json(run_dir / "evidence.json", evidence)

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
                    "observations": ["Corrected observation"],
                    "inferences": ["Corrected inference"],
                    "visual_tasks": ["layout_review"],
                }
            ],
        )
        result = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            observations=observations_path,
        )

        evidence = self.assert_valid_run(run_dir)
        image = evidence["images"][0]
        self.assertEqual(result["images_reused"], 0)
        self.assertNotEqual(image["cache_key"], old_cache_key)
        self.assertEqual(image["observations"], ["Corrected observation"])
        self.assertEqual(image["inferences"], ["Corrected inference"])

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

    def test_openai_responses_vision_analyzes_local_artifact_with_real_provider_mode(self) -> None:
        run_dir = self.prepared_visual_run(provider="openai-responses-vision")
        self.write_openai_vlm_handoff(run_dir, fetch_evidence_image_id=None)
        client = FakeOpenAIResponsesVisionClient()

        result = ingest_vision_observations(
            run=run_dir,
            provider="openai-responses-vision",
            provider_mode="real",
            allow_real_vlm=True,
            openai_config={
                "api_key": "redaction-sentinel",
                "model": "gpt-4.1-mini",
                "estimated_cost_usd_per_image": 0.006,
            },
            openai_client=client,
        )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertEqual(result["provider_mode"], "real")
        self.assertEqual(result["vlm_images_analyzed"], 1)
        self.assertEqual(result["estimated_cost_usd"], 0.006)
        self.assertEqual(result["actual_cost_usd"], 0.004)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.calls[0]["image_input"].startswith("data:image/png;base64,"))
        evidence = self.assert_valid_run(run_dir)
        image = evidence["images"][0]
        self.assertEqual(image["id"], "img_checkout_001")
        self.assertEqual(image["candidate_id"], "cand_checkout_001")
        self.assertEqual(image["fetch_id"], "fetch_checkout_001")
        self.assertEqual(image["task_id"], "task_visual_001")
        self.assertEqual(image["angle_id"], "angle_001")
        self.assertEqual(image["provider_mode"], "real")
        self.assertEqual(image["provider_kind"], "vlm")
        self.assertIn("The screenshot visibly contains a checkout button.", image["observations"])
        self.assertEqual(image["inferences"], ["The UI is likely ready for checkout submission."])
        self.assertTrue(any("model-generated" in caveat for caveat in image["caveats"]))
        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(observations[0]["provider_mode"], "real")
        self.assertEqual(observations[0]["provider_kind"], "vlm")
        self.assertEqual(observations[0]["candidate_id"], "cand_checkout_001")
        self.assertEqual(observations[0]["fetch_id"], "fetch_checkout_001")
        self.assertEqual(observations[0]["estimated_cost_usd"], 0.006)
        self.assertEqual(observations[0]["actual_cost_usd"], 0.004)
        provider_status = self.load_json(run_dir / "visual_provider_status.json")
        self.assertEqual(provider_status["providers"][-1]["provider"], "openai-responses-vision")
        self.assertEqual(provider_status["providers"][-1]["provider_mode"], "real")
        self.assertEqual(provider_status["providers"][-1]["vlm_images_analyzed"], 1)
        self.assertEqual(provider_status["run_dir"], run_dir.name)
        self.assertEqual(provider_status["artifacts"]["visual_observations"], "visual_observations.jsonl")
        self.assertNotIn(str(run_dir), json.dumps(provider_status, sort_keys=True))
        fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(fetches[0]["evidence_image_id"], "img_checkout_001")
        self.assertEqual(result["fetch_lineage_records_reconciled"], 1)
        serialized = json.dumps(
            {
                "evidence": evidence,
                "observations": observations,
                "provider_status": provider_status,
            },
            sort_keys=True,
        )
        self.assertNotIn("redaction-sentinel", serialized)
        self.assertIn("[REDACTED]", serialized)
        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(visual_validation.valid, [error.to_dict() for error in visual_validation.errors])

    def test_codex_interactive_analyzes_local_artifact_with_worker_client(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        client = FakeCodexInteractiveVisionClient()

        result = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            codex_client=client,
        )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertEqual(result["provider_mode"], "real")
        self.assertEqual(result["model_or_tool"], "codex-exec-image-worker")
        self.assertEqual(result["vlm_images_analyzed"], 1)
        self.assertFalse(result["external_vlm_call"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["image_path"], run_dir / "images/checkout.png")
        self.assertIn("--image", client.calls[0]["prompt"])
        self.assertEqual(client.calls[0]["metadata"]["evidence_image_id"], "img_checkout_001")

        evidence = self.assert_valid_run(run_dir)
        self.assertEqual(evidence["vision_adapter"]["provider"], "codex-interactive")
        self.assertFalse(evidence["vision_adapter"]["external_vlm_call"])
        image = evidence["images"][0]
        self.assertEqual(image["id"], "img_checkout_001")
        self.assertEqual(image["analysis_provider"], "codex-interactive")
        self.assertEqual(image["provider_mode"], "real")
        self.assertEqual(image["provider_kind"], "vlm")
        self.assertEqual(image["provider_provenance"]["provider"], "codex-interactive")
        self.assertTrue(image["provider_provenance"]["codex_native_handoff"])
        self.assertTrue(image["provider_provenance"]["codex_interactive_handoff"])
        self.assertFalse(image["provider_provenance"]["hidden_codex_api_call"])
        self.assertIn("The screenshot visibly contains a checkout button.", image["observations"])

        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(observations[0]["provider"], "codex-interactive")
        self.assertEqual(observations[0]["evidence_image_id"], "img_checkout_001")
        self.assertEqual(observations[0]["local_artifact_path"], "images/checkout.png")
        self.assertTrue(observations[0]["codex_native_handoff"])
        self.assertTrue(observations[0]["codex_interactive_handoff"])
        self.assertTrue(observations[0]["explicit_artifact_handoff"])
        self.assertEqual(observations[0]["handoff_artifact"], "visual_observations.jsonl")
        self.assertFalse(observations[0]["hidden_codex_api_call"])
        self.assertTrue(observations[0]["provider_provenance"]["codex_native_handoff"])
        self.assertFalse(observations[0]["provider_provenance"]["external_vlm_call"])

        provider_status = self.load_json(run_dir / "visual_provider_status.json")
        self.assertEqual(provider_status["status"], "codex_interactive_visual_worker_analyzed")
        self.assertEqual(provider_status["providers"][-1]["provider"], "codex-interactive")
        self.assertEqual(provider_status["providers"][-1]["invocations"], 1)
        self.assertEqual(provider_status["providers"][-1]["vlm_images_analyzed"], 1)
        self.assertTrue(provider_status["providers"][-1]["codex_native_handoff"])
        self.assertFalse(provider_status["providers"][-1]["external_vlm_call"])
        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(visual_validation.valid, [error.to_dict() for error in visual_validation.errors])

    def test_codex_interactive_subprocess_stdout_jsonl_ingests_worker_observations(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        visual_payload = {
            "observations": [
                "The screenshot visibly contains a Pay now button.",
                "OCR text reads Pay now.",
            ],
            "inferences": ["The visible UI appears ready for checkout submission."],
            "ocr_text": "Pay now",
            "caveats": ["Small fixture image limits fine visual detail."],
            "confidence": 0.91,
        }
        stdout = "\n".join(
            json.dumps(record, sort_keys=True)
            for record in [
                {"type": "session.started", "session_id": "sess_visual_test"},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_visual_test",
                        "type": "agent_message",
                        "text": json.dumps(visual_payload, sort_keys=True),
                    },
                },
                {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 34}},
            ]
        )
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout=stdout + "\n",
            stderr="",
        )

        with (
            mock.patch("deepresearch.vision_adapter.shutil.which", return_value="/usr/bin/codex"),
            mock.patch(
                "deepresearch.vision_adapter.subprocess.run",
                return_value=completed,
            ) as run_mock,
        ):
            result = ingest_vision_observations(
                run=run_dir,
                provider="codex-interactive",
                codex_config={"codex_binary": "codex"},
            )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertEqual(result["provider_mode"], "real")
        self.assertEqual(result["vlm_images_analyzed"], 1)
        self.assertFalse(result["external_vlm_call"])
        self.assertEqual(run_mock.call_count, 1)
        command = run_mock.call_args.args[0]
        self.assertEqual(command[:3], ["codex", "exec", "--json"])
        self.assertIn("--image", command)
        self.assertIn(str((run_dir / "images/checkout.png").resolve()), command)
        self.assertNotIn("Inspect the attached image artifact", command)
        self.assertIn("Inspect the attached image artifact", run_mock.call_args.kwargs["input"])
        self.assertNotIn("stdin", run_mock.call_args.kwargs)

        evidence = self.assert_valid_run(run_dir)
        image = evidence["images"][0]
        self.assertEqual(image["analysis_provider"], "codex-interactive")
        self.assertEqual(image["provider_mode"], "real")
        self.assertEqual(image["provider_kind"], "vlm")
        self.assertEqual(image["observations"], visual_payload["observations"])
        self.assertEqual(image["ocr_text"], "Pay now")
        self.assertTrue(image["provider_provenance"]["codex_native_handoff"])
        self.assertFalse(image["provider_provenance"]["external_vlm_call"])
        self.assertEqual(image["raw_provider_metadata"]["output_format"], "codex-exec-json")
        self.assertEqual(
            image["raw_provider_metadata"]["lineage"]["evidence_image_id"],
            "img_checkout_001",
        )

        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(observations[0]["observations"], visual_payload["observations"])
        provider_status = self.load_json(run_dir / "visual_provider_status.json")
        self.assertEqual(provider_status["providers"][-1]["provider"], "codex-interactive")
        self.assertEqual(
            provider_status["providers"][-1]["diagnostics"]["worker_command"],
            "codex exec --json --image <artifact>",
        )

    def test_codex_interactive_worker_output_reaches_report_citation(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)

        ingest = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            codex_client=FakeCodexInteractiveVisionClient(),
        )
        verify = verify_claims(run=run_dir)
        report = synthesize_report(run=run_dir)

        self.assertEqual(ingest["status"], "visual_evidence_ingested")
        self.assertEqual(verify["status"], "completed")
        self.assertEqual(report["status"], "completed")
        self.assertIn("img_checkout_001", report["used_images"])
        self.assertEqual(report["visual_observation_report_links_written"], 1)
        evidence = self.assert_valid_run(run_dir)
        visual_claims = [
            claim for claim in evidence["claims"]
            if "img_checkout_001" in claim.get("supporting_images", [])
        ]
        self.assertTrue(visual_claims)
        self.assertEqual(visual_claims[0]["verification_status"], "supported")
        expected_lineage = {
            "plan_id": "plan_task_visual_001",
            "task_id": "task_visual_001",
            "angle_id": "angle_001",
            "route": "visual_required",
            "candidate_id": "cand_checkout_001",
            "fetch_id": "fetch_checkout_001",
            "evidence_image_id": "img_checkout_001",
        }
        visual_support = visual_claims[0]["visual_supports"][0]
        for field, value in expected_lineage.items():
            self.assertEqual(visual_support[field], value)
        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        verifier_link = observations[0]["verifier_links"][0]
        for field, value in expected_lineage.items():
            self.assertEqual(verifier_link[field], value)
        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(visual_validation.valid, [error.to_dict() for error in visual_validation.errors])
        self.assertTrue((run_dir / "report.md").is_file())

    def test_codex_interactive_provider_status_preserves_release_identity(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        evidence = self.load_json(run_dir / "evidence.json")
        original_question = "Compare checkout UI screenshots"
        evidence.update(
            {
                "prompt_id": "pb-visual-identity",
                "suite_id": "public-beta-validation",
                "prompt_hash": hashlib.sha256(
                    original_question.encode("utf-8")
                ).hexdigest(),
                "original_question": original_question,
                "execution_mode": "codex-plugin",
                "runner_mode": "full-runner",
            }
        )
        self.write_json(run_dir / "evidence.json", evidence)
        self.write_codex_vlm_handoff(run_dir)

        result = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            codex_client=FakeCodexInteractiveVisionClient(),
        )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        provider_status = self.load_json(run_dir / "visual_provider_status.json")
        for field in (
            "prompt_id",
            "suite_id",
            "prompt_hash",
            "original_question",
            "execution_mode",
            "runner_mode",
        ):
            self.assertEqual(provider_status[field], evidence[field])

    def test_codex_interactive_without_worker_blocks_when_artifacts_exist(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)

        result = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            codex_config={"codex_binary": "definitely-missing-codex-binary"},
        )

        self.assertEqual(result["status"], "blocked_missing_vlm_provider")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked_reason"], "codex_exec_unavailable")
        self.assertFalse(result["external_vlm_call"])
        provider_status = self.load_json(run_dir / "visual_provider_status.json")
        self.assertEqual(provider_status["status"], "blocked_missing_vlm_provider")
        self.assertEqual(provider_status["providers"][-1]["provider"], "codex-interactive")
        self.assertFalse(provider_status["providers"][-1]["available"])
        self.assertEqual(provider_status["providers"][-1]["blocked_reason"], "codex_exec_unavailable")

    def test_openai_responses_vision_requires_explicit_real_provider_mode(self) -> None:
        run_dir = self.prepared_visual_run(provider="openai-responses-vision")
        self.write_openai_vlm_handoff(run_dir)
        client = FakeOpenAIResponsesVisionClient()

        result = ingest_vision_observations(
            run=run_dir,
            provider="openai-responses-vision",
            allow_real_vlm=True,
            openai_config={"api_key": "redaction-sentinel"},
            openai_client=client,
        )

        self.assertEqual(result["status"], "blocked_missing_vlm_provider")
        self.assertEqual(result["provider_mode"], "fixture")
        self.assertEqual(result["blocked_reason"], "openai_responses_vision_mode_not_real")
        self.assertFalse(result["external_vlm_call"])
        self.assertEqual(client.calls, [])

    def test_openai_responses_vision_redacts_sensitive_endpoint_diagnostics(self) -> None:
        run_dir = self.prepared_visual_run(provider="openai-responses-vision")
        self.write_openai_vlm_handoff(run_dir)
        client = FakeOpenAIResponsesVisionClient()
        endpoint = (
            "https://user:pass@proxy.example/v1/responses?"
            "key=credential-value&api_key=api-key-value&access_token=token-value&"
            "password=password-value&debug=1"
        )

        result = ingest_vision_observations(
            run=run_dir,
            provider="openai-responses-vision",
            provider_mode="real",
            allow_real_vlm=True,
            openai_config={
                "api_key": "redaction-sentinel",
                "endpoint": endpoint,
            },
            openai_client=client,
        )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        status_payloads = {
            "result": result,
            "evidence": self.load_json(run_dir / "evidence.json"),
            "vision_ingest_status": self.load_json(run_dir / "vision_ingest_status.json"),
            "visual_provider_status": self.load_json(run_dir / "visual_provider_status.json"),
            "run_trace": (run_dir / "run_trace.jsonl").read_text(encoding="utf-8"),
            "visual_observations": (run_dir / "visual_observations.jsonl").read_text(
                encoding="utf-8"
            ),
            "openai_observations": (run_dir / "openai_responses_vision_observations.jsonl")
            .read_text(encoding="utf-8"),
        }
        serialized = json.dumps(status_payloads, sort_keys=True)
        for sensitive in (
            "user:pass",
            "credential-value",
            "api-key-value",
            "token-value",
            "password-value",
            "redaction-sentinel",
            "credential-secret-value",
            "credentials-secret-value",
            "bearer-secret-value",
            "nested-token-value",
            "/home/user/private/source.png",
            "/home/user/private/nested-source.png",
        ):
            self.assertNotIn(sensitive, serialized)
        self.assertIn("https://proxy.example/v1/responses", serialized)
        self.assertIn("key=[REDACTED]", serialized)
        self.assertIn("api_key=[REDACTED]", serialized)
        self.assertIn("access_token=[REDACTED]", serialized)
        self.assertIn("password=[REDACTED]", serialized)
        self.assertIn("debug=1", serialized)

    def test_openai_responses_vision_hostile_provider_text_is_redacted(self) -> None:
        run_dir = self.prepared_visual_run(provider="openai-responses-vision")
        self.write_openai_vlm_handoff(run_dir)
        client = HostileOpenAIResponsesVisionClient()

        result = ingest_vision_observations(
            run=run_dir,
            provider="openai-responses-vision",
            provider_mode="real",
            allow_real_vlm=True,
            openai_config={"api_key": "redaction-sentinel"},
            openai_client=client,
        )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        artifacts = {
            "result": result,
            "evidence": self.load_json(run_dir / "evidence.json"),
            "vision_ingest_status": self.load_json(run_dir / "vision_ingest_status.json"),
            "visual_provider_status": self.load_json(run_dir / "visual_provider_status.json"),
            "run_trace": (run_dir / "run_trace.jsonl").read_text(encoding="utf-8"),
            "visual_observations": (run_dir / "visual_observations.jsonl").read_text(
                encoding="utf-8"
            ),
            "openai_observations": (run_dir / "openai_responses_vision_observations.jsonl")
            .read_text(encoding="utf-8"),
        }
        serialized = json.dumps(artifacts, sort_keys=True)
        for sensitive in (
            "redaction-sentinel",
            "credential-secret-value",
            "credentials-secret-value",
            "bearer-secret-value",
            "raw-bearer-value",
            "nested-token-value",
            "usage-token-value",
            "nested-auth-value",
            "/home/user/private/source.png",
            "/home/user/private/nested-source.png",
        ):
            self.assertNotIn(sensitive, serialized)
        self.assertIn("Visible safe label", serialized)
        self.assertIn("safe inference", serialized)
        self.assertIn("safe OCR text", serialized)
        self.assertIn("credential_hint=[REDACTED]", serialized)
        self.assertIn("credentials_blob: [REDACTED]", serialized)

    def test_openai_responses_vision_freeform_response_is_caveated_inference(self) -> None:
        run_dir = self.prepared_visual_run(provider="openai-responses-vision")
        self.write_openai_vlm_handoff(run_dir)
        client = FreeFormOpenAIResponsesVisionClient(
            "The checkout flow is probably ready because the button is prominent."
        )

        result = ingest_vision_observations(
            run=run_dir,
            provider="openai-responses-vision",
            provider_mode="real",
            allow_real_vlm=True,
            openai_config={"api_key": "redaction-sentinel"},
            openai_client=client,
        )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        evidence = self.assert_valid_run(run_dir)
        image = evidence["images"][0]
        self.assertEqual(image["observations"], [])
        self.assertEqual(
            image["inferences"],
            ["The checkout flow is probably ready because the button is prominent."],
        )
        self.assertTrue(any("not promoted to observations" in caveat for caveat in image["caveats"]))

    def test_openai_responses_vision_analyzes_image_url_with_fetch_lineage(self) -> None:
        run_dir = self.prepared_visual_run(provider="openai-responses-vision")
        self.write_openai_vlm_handoff(run_dir)
        candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        candidates[0].pop("source_id", None)
        candidates[0].update(
            {
                "provider": "real-image-search",
                "provider_kind": "web_image_search",
                "provider_run_id": "real-image-search:test",
                "provider_provenance": {
                    "provider": "real-image-search",
                    "provider_kind": "web_image_search",
                    "provider_mode": "real",
                    "provider_run_id": "real-image-search:test",
                    "external_network_call": True,
                    "external_vlm_call": False,
                },
                "origin": "image_search",
                "image_url": "https://example.com/checkout.png",
                "page_url": None,
                "local_artifact_path": None,
                "candidate_status": "fetched",
                "requires_vlm_observation": True,
            }
        )
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", [])
        provider_status = self.load_json(run_dir / "visual_provider_status.json")
        provider_status["providers"][0].update(
            {
                "provider": "real-image-search",
                "provider_kind": "web_image_search",
                "provider_run_id": "real-image-search:test",
            }
        )
        self.write_json(run_dir / "visual_provider_status.json", provider_status)
        client = FakeOpenAIResponsesVisionClient()

        result = ingest_vision_observations(
            run=run_dir,
            provider="openai-responses-vision",
            provider_mode="real",
            allow_real_vlm=True,
            openai_config={"api_key": "redaction-sentinel"},
            openai_client=client,
        )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertEqual(client.calls[0]["image_input"], "https://example.com/checkout.png")
        fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0]["fetch_id"], "fetch_checkout_001")
        self.assertEqual(fetches[0]["evidence_image_id"], "img_checkout_001")
        self.assertEqual(fetches[0]["provider_kind"], "web_image_search")
        self.assertEqual(result["fetch_lineage_records_reconciled"], 1)
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["images"][0]["source_id"], "src_checkout_001")
        source_ids = {source["id"] for source in evidence["sources"]}
        self.assertEqual(source_ids, {"src_checkout", "src_checkout_001"})
        generated_source = next(source for source in evidence["sources"] if source["id"] == "src_checkout_001")
        self.assertEqual(generated_source["url"], "https://example.com/checkout.png")
        self.assertTrue((run_dir / generated_source["local_artifact_path"]).is_file())
        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(visual_validation.valid, [error.to_dict() for error in visual_validation.errors])

    def test_openai_responses_vision_skips_rejected_image_url_candidates(self) -> None:
        run_dir = self.prepared_visual_run(provider="openai-responses-vision")
        self.write_openai_vlm_handoff(run_dir)
        candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        candidates[0].update(
            {
                "provider": "real-image-search",
                "provider_kind": "web_image_search",
                "provider_run_id": "real-image-search:test",
                "origin": "image_search",
                "image_url": "https://example.com/duplicate-checkout.png",
                "local_artifact_path": None,
                "candidate_status": "rejected",
                "status": "removed",
                "removal_reasons": ["duplicate_image_url"],
                "rejection_reason": "duplicate_image_url",
            }
        )
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", [])
        client = FakeOpenAIResponsesVisionClient()

        result = ingest_vision_observations(
            run=run_dir,
            provider="openai-responses-vision",
            provider_mode="real",
            allow_real_vlm=True,
            openai_config={"api_key": "redaction-sentinel"},
            openai_client=client,
        )

        self.assertEqual(result["status"], "blocked_missing_vlm_provider")
        self.assertEqual(result["blocked_reason"], "no_fetched_visual_artifacts")
        self.assertEqual(client.calls, [])
        observations_text = (run_dir / "visual_observations.jsonl").read_text(encoding="utf-8")
        self.assertEqual(observations_text, "")

    def test_openai_responses_vision_empty_placeholder_files_remain_no_visual_tasks(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepared = prepare_run(
            question="Summarize a text-only topic",
            runs_dir=runs_dir,
            route="text_only",
            vlm_provider="openai-responses-vision",
        )
        run_dir = Path(prepared["run_dir"])
        self.write_jsonl(run_dir / "visual_candidates.jsonl", [])
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", [])
        self.write_jsonl(run_dir / "visual_observations.jsonl", [])
        client = FakeOpenAIResponsesVisionClient()

        result = ingest_vision_observations(
            run=run_dir,
            provider="openai-responses-vision",
            provider_mode="real",
            allow_real_vlm=True,
            openai_config={"api_key": "redaction-sentinel"},
            openai_client=client,
        )

        self.assertEqual(result["status"], "no_visual_tasks")
        self.assertFalse(result["external_vlm_call"])
        self.assertEqual(client.calls, [])

    def test_openai_responses_vision_missing_credentials_blocks_and_preserves_artifacts(self) -> None:
        run_dir = self.prepared_visual_run(provider="openai-responses-vision")
        self.write_openai_vlm_handoff(run_dir)
        first_client = FakeOpenAIResponsesVisionClient()
        first_result = ingest_vision_observations(
            run=run_dir,
            provider="openai-responses-vision",
            provider_mode="real",
            allow_real_vlm=True,
            openai_config={"api_key": "redaction-sentinel"},
            openai_client=first_client,
        )
        self.assertEqual(first_result["status"], "visual_evidence_ingested")
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual([image["id"] for image in evidence["images"]], ["img_checkout_001"])
        evidence["claims"] = [
            {
                "id": "claim_checkout_visual",
                "text": "The checkout button is visible.",
                "claim_type": "visual",
                "supporting_sources": ["src_checkout"],
                "supporting_images": ["img_checkout_001"],
                "quote_spans": [],
                "votes": [],
                "verification_status": "unverified",
                "review_status": "not_reviewed",
                "promotion_status": "not_eligible",
                "confidence": "low",
                "caveats": [],
                "visual_supports": [
                    {
                        "image_id": "img_checkout_001",
                        "observation_ref": "images.img_checkout_001.observations[0]",
                        "observation_index": 0,
                        "observation_text": "The screenshot visibly contains a checkout button.",
                        "relation_type": "direct_visual_support",
                        "provider": "openai-responses-vision",
                        "rationale": "Regression fixture for stale OpenAI visual evidence cleanup.",
                        "confidence": 0.8,
                    }
                ],
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "observation_id": "stale_pre_vlm",
                    "observation_status": "analyzed",
                    "observations": ["stale acquisition observation"],
                }
            ],
        )

        result = ingest_vision_observations(
            run=run_dir,
            provider="openai-responses-vision",
            provider_mode="real",
            allow_real_vlm=True,
            openai_config={"api_key": None},
        )

        self.assertEqual(result["status"], "blocked_missing_vlm_provider")
        self.assertEqual(result["blocked_reason"], "missing_openai_api_key")
        self.assertTrue((run_dir / "images" / "checkout.png").is_file())
        fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(fetches[0]["fetch_status"], "fetched")
        self.assertEqual(fetches[0]["local_artifact_path"], "images/checkout.png")
        provider_status = self.load_json(run_dir / "visual_provider_status.json")
        self.assertEqual(provider_status["status"], "blocked_missing_vlm_provider")
        self.assertFalse(provider_status["ok"])
        self.assertTrue(provider_status["terminal"])
        self.assertEqual(provider_status["providers"][-1]["blocked_reason"], "missing_openai_api_key")
        self.assertTrue(provider_status["providers"][-1]["diagnostics"]["stale_visual_observations_cleared"])
        self.assertEqual(provider_status["providers"][-1]["diagnostics"]["stale_openai_images_cleared"], 1)
        self.assertEqual(
            provider_status["providers"][-1]["diagnostics"]["stale_openai_visual_supports_cleared"],
            1,
        )
        self.assertEqual((run_dir / "visual_observations.jsonl").read_text(encoding="utf-8"), "")
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["images"], [])
        self.assertEqual(evidence["claims"][0]["supporting_images"], [])
        self.assertEqual(evidence["claims"][0]["visual_supports"], [])
        self.assertEqual(evidence["vision_adapter"]["status"], "blocked_missing_vlm_provider")

    def test_openai_responses_vision_partial_provider_failure_records_attempted_costs(self) -> None:
        run_dir = self.prepared_visual_run(provider="openai-responses-vision")
        self.write_openai_vlm_handoff(run_dir)
        (run_dir / "images" / "checkout-2.png").write_bytes(PNG_1X1)
        candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        second_candidate = dict(candidates[0])
        second_candidate.update(
            {
                "candidate_id": "cand_checkout_002",
                "local_artifact_path": "images/checkout-2.png",
                "page_url": "https://example.com/checkout-2",
            }
        )
        candidates.append(second_candidate)
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)
        fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        second_fetch = dict(fetches[0])
        second_fetch.update(
            {
                "fetch_id": "fetch_checkout_002",
                "candidate_id": "cand_checkout_002",
                "local_artifact_path": "images/checkout-2.png",
                "evidence_image_id": "img_checkout_002",
            }
        )
        fetches.append(second_fetch)
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)
        client = FailingSecondOpenAIResponsesVisionClient()

        result = ingest_vision_observations(
            run=run_dir,
            provider="openai-responses-vision",
            provider_mode="real",
            allow_real_vlm=True,
            openai_config={
                "api_key": "redaction-sentinel",
                "estimated_cost_usd_per_image": 0.006,
            },
            openai_client=client,
        )

        self.assertEqual(result["status"], "blocked_missing_vlm_provider")
        self.assertEqual(result["blocked_reason"], "provider_request_failed")
        self.assertTrue(result["external_vlm_call"])
        self.assertEqual(result["vlm_images_analyzed"], 1)
        self.assertEqual(result["estimated_cost_usd"], 0.012)
        self.assertEqual(result["actual_cost_usd"], 0.004)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual((run_dir / "visual_observations.jsonl").read_text(encoding="utf-8"), "")
        partial_records = [
            json.loads(line)
            for line in (run_dir / "openai_responses_vision_observations.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        self.assertEqual(len(partial_records), 1)
        provider_status = self.load_json(run_dir / "visual_provider_status.json")
        provider = provider_status["providers"][-1]
        self.assertTrue(provider["invoked"])
        self.assertEqual(provider["invocations"], 2)
        self.assertEqual(provider["vlm_images_analyzed"], 1)
        self.assertEqual(provider["estimated_cost_usd"], 0.012)
        self.assertEqual(provider["actual_cost_usd"], 0.004)
        self.assertTrue(provider["external_vlm_call"])

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
