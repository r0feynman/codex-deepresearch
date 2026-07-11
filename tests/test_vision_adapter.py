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
    _codex_interactive_supplemental_vision_tasks,
    _openai_result_from_response_payload,
)
from deepresearch.visual_artifacts import visual_minimums_for_run

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

    def append_codex_vlm_handoff_artifact(self, run_dir: Path, index: int) -> None:
        image_id = f"img_checkout_{index:03d}"
        candidate_id = f"cand_checkout_{index:03d}"
        fetch_id = f"fetch_checkout_{index:03d}"
        local_artifact_path = f"images/checkout_{index}.png"
        (run_dir / local_artifact_path).write_bytes(PNG_1X1)
        candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        candidate = dict(candidates[0])
        candidate.update(
            {
                "candidate_id": candidate_id,
                "rank": index,
                "local_artifact_path": local_artifact_path,
                "hash": f"sha256:fixture-checkout-{index}",
                "phash": f"checkout-phash-{index}",
            }
        )
        fetch = dict(fetches[0])
        fetch.update(
            {
                "fetch_id": fetch_id,
                "candidate_id": candidate_id,
                "local_artifact_path": local_artifact_path,
                "evidence_image_id": image_id,
                "hash": f"sha256:fixture-checkout-{index}",
                "phash": f"checkout-phash-{index}",
            }
        )
        self.write_jsonl(run_dir / "visual_candidates.jsonl", [*candidates, candidate])
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", [*fetches, fetch])

    def write_codex_interactive_observation(
        self,
        run_dir: Path,
        *,
        provider_run_id: str,
        image_id: str = "img_checkout_001",
        candidate_id: str = "cand_checkout_001",
        fetch_id: str = "fetch_checkout_001",
        plan_id: str = "plan_task_visual_001",
        task_id: str = "task_visual_001",
        angle_id: str = "angle_001",
        route: str = "visual_required",
        policy_decision: str = "allowed",
    ) -> None:
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "id": image_id,
                    "image_id": image_id,
                    "evidence_image_id": image_id,
                    "observation_id": f"obs_{image_id}_001",
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "image_url": (run_dir / "images" / "checkout.png").resolve().as_uri(),
                    "page_url": "https://example.com/checkout",
                    "local_artifact_path": "images/checkout.png",
                    "mime_type": "image/png",
                    "artifact_size_bytes": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "hash": "sha256:fixture-checkout",
                    "phash": "checkout-phash",
                    "candidate_id": candidate_id,
                    "fetch_id": fetch_id,
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "angle_id": angle_id,
                    "route": route,
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "provider_run_id": provider_run_id,
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "model_or_tool": "codex-exec-image-worker",
                    "observations": ["The checkout screenshot shows a primary payment button."],
                    "inferences": ["The analyzed image supports checkout completion."],
                    "visual_tasks": ["layout_review"],
                    "confidence": 0.83,
                    "policy_decision": policy_decision,
                    "policy_flags": [],
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                    "caveats": [],
                    "verifier_links": [],
                    "report_links": [],
                    "created_at": "2026-06-22T00:00:00Z",
                    "codex_interactive_handoff": True,
                    "codex_native_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "external_vlm_call": False,
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "provider_run_id": provider_run_id,
                        "codex_interactive_handoff": True,
                        "codex_native_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            ],
        )

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

    def test_codex_interactive_child_handoff_records_provider_status(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "id": "img_checkout_001",
                    "image_id": "img_checkout_001",
                    "evidence_image_id": "img_checkout_001",
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "local_artifact_path": "images/checkout.png",
                    "mime_type": "image/png",
                    "width": 1,
                    "height": 1,
                    "candidate_id": "cand_checkout_001",
                    "fetch_id": "fetch_checkout_001",
                    "task_id": "task_visual_001",
                    "angle_id": "angle_001",
                    "route": "visual_required",
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "provider_run_id": "codex-child:test",
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "observations": ["The screenshot shows a primary checkout button."],
                    "inferences": ["The visual evidence supports a checkout UI claim."],
                    "visual_tasks": ["layout_review"],
                    "codex_interactive_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "external_vlm_call": False,
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "provider_run_id": "codex-child:test",
                        "codex_interactive_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            ],
        )

        result = ingest_vision_observations(run=run_dir, provider="codex-interactive")

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertEqual(result["provider_mode"], "real")
        self.assertEqual(result["vlm_images_analyzed"], 1)
        self.assertFalse(result["external_vlm_call"])
        evidence = self.assert_valid_run(run_dir)
        self.assertEqual(evidence["vision_adapter"]["visual_provider_status_path"], "visual_provider_status.json")
        provider_status = self.load_json(run_dir / "visual_provider_status.json")
        self.assertEqual(provider_status["status"], "codex_interactive_visual_handoff_ingested")
        provider = provider_status["providers"][-1]
        self.assertEqual(provider["provider"], "codex-interactive")
        self.assertEqual(provider["provider_kind"], "vlm")
        self.assertEqual(provider["provider_mode"], "real")
        self.assertEqual(provider["provider_run_id"], "codex-child:test")
        self.assertEqual(provider["artifacts_fetched"], 1)
        self.assertEqual(provider["vlm_images_analyzed"], 1)
        self.assertEqual(provider["invocations"], 1)
        self.assertTrue(provider["codex_interactive_handoff"])
        self.assertFalse(provider["external_vlm_call"])
        minimums = visual_minimums_for_run(run_dir, required_vlm_images=1)
        self.assertEqual(minimums["vlm_images_analyzed"], 1)

    def test_codex_interactive_ingest_uniquifies_duplicate_observation_ids(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        self.append_codex_vlm_handoff_artifact(run_dir, 2)

        def observation(image_id: str, candidate_id: str, fetch_id: str, local_path: str) -> dict:
            return {
                "id": image_id,
                "image_id": image_id,
                "evidence_image_id": image_id,
                "observation_id": "obs_img_001_001",
                "source_id": "src_checkout",
                "origin": "screenshot",
                "local_artifact_path": local_path,
                "mime_type": "image/png",
                "width": 1,
                "height": 1,
                "candidate_id": candidate_id,
                "fetch_id": fetch_id,
                "task_id": "task_visual_001",
                "angle_id": "angle_001",
                "route": "visual_required",
                "provider": "codex-interactive",
                "provider_kind": "vlm",
                "provider_mode": "real",
                "provider_run_id": "codex-child:duplicate-observation-id",
                "analysis_provider": "codex-interactive",
                "analysis_status": "analyzed",
                "observation_status": "analyzed",
                "observations": [f"The screenshot {image_id} has visible UI content."],
                "inferences": [f"The analyzed image {image_id} supports the visual claim."],
                "visual_tasks": ["layout_review"],
                "codex_interactive_handoff": True,
                "handoff_artifact": "visual_observations.jsonl",
                "external_vlm_call": False,
                "provider_provenance": {
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "provider_run_id": "codex-child:duplicate-observation-id",
                    "codex_interactive_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "external_vlm_call": False,
                },
            }

        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                observation(
                    "img_checkout_001",
                    "cand_checkout_001",
                    "fetch_checkout_001",
                    "images/checkout.png",
                ),
                observation(
                    "img_checkout_002",
                    "cand_checkout_002",
                    "fetch_checkout_002",
                    "images/checkout_2.png",
                ),
            ],
        )

        result = ingest_vision_observations(run=run_dir, provider="codex-interactive")

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertTrue(
            result["visual_artifact_validation"]["valid"],
            result["visual_artifact_validation"],
        )
        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        observation_ids = [record["observation_id"] for record in observations]
        self.assertEqual(len(observation_ids), len(set(observation_ids)))
        self.assertNotIn("obs_img_001_001", observation_ids)
        by_image = {record["evidence_image_id"]: record for record in observations}
        for image_id in ("img_checkout_001", "img_checkout_002"):
            self.assertEqual(
                by_image[image_id]["raw_child_observation_id"],
                "obs_img_001_001",
            )
            self.assertEqual(by_image[image_id]["task_id"], "task_visual_001")
            self.assertEqual(by_image[image_id]["angle_id"], "angle_001")

    def test_codex_interactive_handoff_uses_fetch_canonical_image_id(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "id": "img_obs_task019_001",
                    "evidence_image_id": "img_obs_task019_001",
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "local_artifact_path": "images/checkout.png",
                    "mime_type": "image/png",
                    "width": 1,
                    "height": 1,
                    "candidate_id": "cand_checkout_001",
                    "fetch_id": "fetch_checkout_001",
                    "plan_id": "plan_task_visual_001",
                    "task_id": "task_visual_001",
                    "angle_id": "angle_001",
                    "route": "visual_required",
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "provider_run_id": "codex-child:test",
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "observations": ["A reused visual observation references the checkout screenshot."],
                    "inferences": ["The stale observation id should not fork image lineage."],
                    "visual_tasks": ["layout_review"],
                    "codex_interactive_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "external_vlm_call": False,
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "provider_run_id": "codex-child:test",
                        "codex_interactive_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                },
                {
                    "id": "img_checkout_001",
                    "evidence_image_id": "img_checkout_001",
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "local_artifact_path": "images/checkout.png",
                    "mime_type": "image/png",
                    "width": 1,
                    "height": 1,
                    "candidate_id": "cand_checkout_001",
                    "fetch_id": "fetch_checkout_001",
                    "plan_id": "plan_task_visual_001",
                    "task_id": "task_visual_001",
                    "angle_id": "angle_001",
                    "route": "visual_required",
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "provider_run_id": "codex-child:test",
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "observations": ["The canonical observation uses the existing checkout id."],
                    "inferences": [],
                    "visual_tasks": ["layout_review"],
                    "codex_interactive_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "external_vlm_call": False,
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "provider_run_id": "codex-child:test",
                        "codex_interactive_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            ],
        )

        result = ingest_vision_observations(run=run_dir, provider="codex-interactive")

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertEqual(result["duplicate_images_merged"], 1)
        evidence = self.load_json(run_dir / "evidence.json")
        image_ids = [image["id"] for image in evidence["images"]]
        self.assertEqual(image_ids, ["img_checkout_001"])
        image = evidence["images"][0]
        self.assertEqual(image["evidence_image_id"], "img_checkout_001")
        self.assertEqual(image["raw_visual_observation_image_id"], "img_obs_task019_001")
        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0]["evidence_image_id"], "img_checkout_001")
        self.assertEqual(observations[0]["fetch_id"], "fetch_checkout_001")
        self.assertEqual(observations[0]["candidate_id"], "cand_checkout_001")
        for claim in evidence["claims"]:
            self.assertNotIn("img_obs_task019_001", claim.get("supporting_images", []))
            for support in claim.get("visual_supports", []):
                self.assertNotEqual(support.get("evidence_image_id"), "img_obs_task019_001")
        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(visual_validation.valid, [error.to_dict() for error in visual_validation.errors])

    def test_codex_interactive_materializes_child_lineage_after_acquisition_overwrite(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        existing_fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for fetch in existing_fetches:
            fetch["evidence_image_id"] = None
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", existing_fetches)
        image_id = "img_task_001_001"
        candidate_id = "cand_img_task_001_001"
        fetch_id = "fetch_img_task_001_001"
        task_id = "task_001"
        angle_id = "angle_001"
        route = "visual_required"
        plan_id = "plan_task_001_angle_001_visual_required"
        image_hash = "sha256:" + hashlib.sha256(PNG_1X1).hexdigest()
        original_question = "Compare checkout UI screenshots"
        release_identity = {
            "prompt_id": "pb-visual-plan-identity",
            "suite_id": "public-beta-validation",
            "prompt_hash": hashlib.sha256(original_question.encode("utf-8")).hexdigest(),
            "execution_mode": "codex-plugin",
            "runner_mode": "full-runner",
            "original_question": original_question,
        }

        evidence = self.load_json(run_dir / "evidence.json")
        evidence.update(release_identity)
        evidence.setdefault("search_tasks", []).append(
            {
                "id": task_id,
                "angle_id": angle_id,
                "route": route,
                "modality": route,
                "visual_tasks": ["layout_review"],
            }
        )
        evidence["images"] = [
            {
                "id": image_id,
                "source_id": "src_checkout",
                "origin": "screenshot",
                "source_url": "https://example.com/checkout",
                "image_url": (run_dir / "images" / "checkout.png").resolve().as_uri(),
                "page_url": "https://example.com/checkout",
                "local_artifact_path": "images/checkout.png",
                "mime_type": "image/png",
                "artifact_size_bytes": len(PNG_1X1),
                "width": 1,
                "height": 1,
                "hash": image_hash,
                "phash": "checkout-phash",
                "observations": ["The child VLM observation identifies the checkout button."],
                "inferences": ["The visual evidence supports checkout completion."],
                "visual_tasks": ["layout_review"],
                "analysis_provider": "codex-interactive",
                "analysis_status": "analyzed",
                "candidate_id": candidate_id,
                "fetch_id": fetch_id,
                "plan_id": plan_id,
                "task_id": task_id,
                "angle_id": angle_id,
                "route": route,
                "provider": "codex-interactive",
                "provider_kind": "vlm",
                "provider_mode": "real",
                "provider_run_id": "codex-child:overwrite",
                "provider_provenance": {
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "provider_run_id": "codex-child:overwrite",
                    "codex_interactive_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "external_vlm_call": False,
                },
                "policy_decision": "allowed",
                "policy_flags": [],
                "estimated_cost_usd": 0.0,
                "actual_cost_usd": 0.0,
                "caveats": [],
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        run_status_path = run_dir / "run_status.json"
        run_status = self.load_json(run_status_path) if run_status_path.exists() else {}
        run_status.update(release_identity)
        self.write_json(run_status_path, run_status)
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "id": image_id,
                    "image_id": image_id,
                    "evidence_image_id": image_id,
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "image_url": (run_dir / "images" / "checkout.png").resolve().as_uri(),
                    "page_url": "https://example.com/checkout",
                    "local_artifact_path": "images/checkout.png",
                    "mime_type": "image/png",
                    "artifact_size_bytes": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "hash": image_hash,
                    "phash": "checkout-phash",
                    "candidate_id": candidate_id,
                    "fetch_id": fetch_id,
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "angle_id": angle_id,
                    "route": route,
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "provider_run_id": "codex-child:overwrite",
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "observations": ["The child VLM observation identifies the checkout button."],
                    "inferences": ["The visual evidence supports checkout completion."],
                    "visual_tasks": ["layout_review"],
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                    "caveats": [],
                    "codex_interactive_handoff": True,
                    "codex_native_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "external_vlm_call": False,
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "provider_run_id": "codex-child:overwrite",
                        "codex_interactive_handoff": True,
                        "codex_native_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            ],
        )

        pre_plan = self.load_json(run_dir / "visual_search_plan.json")
        pre_candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        pre_fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertNotIn(plan_id, {item["plan_id"] for item in pre_plan["tasks"]})
        self.assertNotIn(candidate_id, {item["candidate_id"] for item in pre_candidates})
        self.assertNotIn(fetch_id, {item["fetch_id"] for item in pre_fetches})

        result = ingest_vision_observations(run=run_dir, provider="codex-interactive")

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertTrue(
            result["visual_artifact_validation"]["valid"],
            result["visual_artifact_validation"],
        )
        evidence = self.assert_valid_run(run_dir)
        materialization = evidence["vision_adapter"]["codex_interactive_lineage_materialization"]
        self.assertEqual(materialization["plans_materialized"], 1)
        self.assertEqual(materialization["candidates_materialized"], 1)
        self.assertEqual(materialization["fetches_materialized"], 1)
        post_plan = self.load_json(run_dir / "visual_search_plan.json")
        post_candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        post_fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for field, value in release_identity.items():
            self.assertEqual(post_plan[field], value)
        self.assertIn(plan_id, {item["plan_id"] for item in post_plan["tasks"]})
        self.assertIn(candidate_id, {item["candidate_id"] for item in post_candidates})
        self.assertIn(fetch_id, {item["fetch_id"] for item in post_fetches})
        child_candidate = next(item for item in post_candidates if item["candidate_id"] == candidate_id)
        child_fetch = next(item for item in post_fetches if item["fetch_id"] == fetch_id)
        self.assertEqual(child_candidate["plan_id"], plan_id)
        self.assertEqual(child_candidate["candidate_status"], "fetched")
        self.assertEqual(child_candidate["provider"], "codex-native")
        self.assertEqual(child_candidate["provider_kind"], "web_image_search")
        self.assertEqual(child_candidate["provider_mode"], "real")
        self.assertTrue(child_candidate["provider_provenance"]["codex_native_handoff"])
        self.assertEqual(child_fetch["candidate_id"], candidate_id)
        self.assertEqual(child_fetch["evidence_image_id"], image_id)
        self.assertEqual(child_fetch["provider"], "codex-native")
        self.assertEqual(child_fetch["provider_kind"], "web_image_search")
        self.assertEqual(child_fetch["provider_mode"], "real")
        self.assertTrue(child_fetch["provider_provenance"]["codex_native_handoff"])

        minimums = visual_minimums_for_run(run_dir, required_vlm_images=1)
        self.assertEqual(minimums["vlm_images_analyzed"], 1)
        self.assertGreaterEqual(minimums["fetched_artifacts"], 1)

        verify = verify_claims(run=run_dir)
        report = synthesize_report(run=run_dir)
        self.assertEqual(verify["status"], "completed")
        self.assertEqual(report["status"], "completed")
        linked_minimums = visual_minimums_for_run(run_dir, required_vlm_images=1)
        self.assertGreaterEqual(linked_minimums["report_cited_images"], 1)

    def test_codex_interactive_reconciles_budget_pruned_sidecars_for_used_image(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        image_id = "img_checkout_001"
        candidate_id = "cand_checkout_001"
        fetch_id = "fetch_checkout_001"
        plan_id = "plan_task_visual_001"
        task_id = "task_visual_001"
        angle_id = "angle_001"
        route = "visual_required"

        candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        candidate = next(item for item in candidates if item["candidate_id"] == candidate_id)
        candidate.update(
            {
                "image_id": image_id,
                "evidence_image_id": image_id,
                "candidate_status": "budget_pruned",
                "policy_decision": "budget_pruned",
                "rejection_reason": "budget_pruned",
            }
        )
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)

        fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        fetch = next(item for item in fetches if item["fetch_id"] == fetch_id)
        fetch.update(
            {
                "image_id": image_id,
                "evidence_image_id": image_id,
                "fetch_status": "budget_pruned",
                "retrieval_status": "budget_pruned",
                "policy_decision": "budget_pruned",
                "failure_code": "budget_pruned",
            }
        )
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)

        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "id": image_id,
                    "image_id": image_id,
                    "evidence_image_id": image_id,
                    "observation_id": "obs_img_checkout_001_001",
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "image_url": (run_dir / "images" / "checkout.png").resolve().as_uri(),
                    "page_url": "https://example.com/checkout",
                    "local_artifact_path": "images/checkout.png",
                    "mime_type": "image/png",
                    "artifact_size_bytes": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "hash": "sha256:fixture-checkout",
                    "phash": "checkout-phash",
                    "candidate_id": candidate_id,
                    "fetch_id": fetch_id,
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "angle_id": angle_id,
                    "route": route,
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "provider_run_id": "codex-child:budget-pruned-reconcile",
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "model_or_tool": "codex-exec-image-worker",
                    "observations": ["The checkout screenshot shows a primary payment button."],
                    "inferences": ["The analyzed image supports checkout completion."],
                    "visual_tasks": ["layout_review"],
                    "confidence": 0.83,
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                    "caveats": [],
                    "verifier_links": [],
                    "report_links": [],
                    "created_at": "2026-06-22T00:00:00Z",
                    "codex_interactive_handoff": True,
                    "codex_native_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "external_vlm_call": False,
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "provider_run_id": "codex-child:budget-pruned-reconcile",
                        "codex_interactive_handoff": True,
                        "codex_native_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            ],
        )

        result = ingest_vision_observations(run=run_dir, provider="codex-interactive")

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertTrue(
            result["visual_artifact_validation"]["valid"],
            result["visual_artifact_validation"],
        )
        evidence = self.assert_valid_run(run_dir)
        image = next(item for item in evidence["images"] if item["id"] == image_id)
        self.assertEqual(image["policy_decision"], "allowed")
        self.assertEqual(image["visual_acquisition_policy_decision"], "allowed")

        post_candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        post_fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        post_candidate = next(item for item in post_candidates if item["candidate_id"] == candidate_id)
        post_fetch = next(item for item in post_fetches if item["fetch_id"] == fetch_id)
        self.assertEqual(post_candidate["policy_decision"], "allowed")
        self.assertEqual(post_candidate["candidate_status"], "fetched")
        self.assertIsNone(post_candidate["rejection_reason"])
        self.assertEqual(
            post_candidate["raw_codex_interactive_handoff_policy_decision"],
            "budget_pruned",
        )
        self.assertEqual(
            post_candidate["raw_codex_interactive_handoff_candidate_status"],
            "budget_pruned",
        )
        self.assertEqual(post_fetch["policy_decision"], "allowed")
        self.assertEqual(post_fetch["fetch_status"], "fetched")
        self.assertIsNone(post_fetch["failure_code"])
        self.assertEqual(
            post_fetch["raw_codex_interactive_handoff_policy_decision"],
            "budget_pruned",
        )
        self.assertEqual(
            post_fetch["raw_codex_interactive_handoff_fetch_status"],
            "budget_pruned",
        )

        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(visual_validation.valid, [error.to_dict() for error in visual_validation.errors])

    def test_codex_interactive_does_not_convert_blocked_sidecars_to_allowed(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        image_id = "img_checkout_001"
        candidate_id = "cand_checkout_001"
        fetch_id = "fetch_checkout_001"
        plan_id = "plan_task_visual_001"
        task_id = "task_visual_001"
        angle_id = "angle_001"
        route = "visual_required"

        candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        candidate = next(item for item in candidates if item["candidate_id"] == candidate_id)
        candidate.update(
            {
                "image_id": image_id,
                "evidence_image_id": image_id,
                "candidate_status": "policy_blocked",
                "policy_decision": "blocked",
                "rejection_reason": "policy_blocked",
            }
        )
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)

        fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        fetch = next(item for item in fetches if item["fetch_id"] == fetch_id)
        fetch.update(
            {
                "image_id": image_id,
                "evidence_image_id": image_id,
                "fetch_status": "policy_blocked",
                "retrieval_status": "policy_blocked",
                "policy_decision": "blocked",
                "failure_code": "policy_blocked",
            }
        )
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)

        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "id": image_id,
                    "image_id": image_id,
                    "evidence_image_id": image_id,
                    "observation_id": "obs_img_checkout_001_001",
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "image_url": (run_dir / "images" / "checkout.png").resolve().as_uri(),
                    "page_url": "https://example.com/checkout",
                    "local_artifact_path": "images/checkout.png",
                    "mime_type": "image/png",
                    "artifact_size_bytes": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "hash": "sha256:fixture-checkout",
                    "phash": "checkout-phash",
                    "candidate_id": candidate_id,
                    "fetch_id": fetch_id,
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "angle_id": angle_id,
                    "route": route,
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "provider_run_id": "codex-child:blocked-not-reconciled",
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "model_or_tool": "codex-exec-image-worker",
                    "observations": ["The checkout screenshot shows a primary payment button."],
                    "inferences": ["The analyzed image would support checkout completion."],
                    "visual_tasks": ["layout_review"],
                    "confidence": 0.83,
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                    "caveats": [],
                    "verifier_links": [],
                    "report_links": [],
                    "created_at": "2026-06-22T00:00:00Z",
                    "codex_interactive_handoff": True,
                    "codex_native_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "external_vlm_call": False,
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "provider_run_id": "codex-child:blocked-not-reconciled",
                        "codex_interactive_handoff": True,
                        "codex_native_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            ],
        )

        result = ingest_vision_observations(run=run_dir, provider="codex-interactive")

        self.assertEqual(result["status"], "visual_evidence_ingested")
        post_candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        post_fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        post_candidate = next(item for item in post_candidates if item["candidate_id"] == candidate_id)
        post_fetch = next(item for item in post_fetches if item["fetch_id"] == fetch_id)
        self.assertEqual(post_candidate["policy_decision"], "blocked")
        self.assertEqual(post_candidate["candidate_status"], "policy_blocked")
        self.assertEqual(post_fetch["policy_decision"], "blocked")
        self.assertEqual(post_fetch["fetch_status"], "policy_blocked")

        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertFalse(visual_validation.valid)
        self.assertTrue(
            any(
                error.code == "lineage_mismatch"
                and "policy_decision" in error.path
                for error in visual_validation.errors
            ),
            [error.to_dict() for error in visual_validation.errors],
        )

    def test_codex_interactive_does_not_convert_blocked_policy_with_budget_pruned_status(
        self,
    ) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        image_id = "img_checkout_001"
        candidate_id = "cand_checkout_001"
        fetch_id = "fetch_checkout_001"

        candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        candidate = next(item for item in candidates if item["candidate_id"] == candidate_id)
        candidate.update(
            {
                "image_id": image_id,
                "evidence_image_id": image_id,
                "candidate_status": "budget_pruned",
                "policy_decision": "blocked",
                "rejection_reason": "budget_pruned",
            }
        )
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)

        fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        fetch = next(item for item in fetches if item["fetch_id"] == fetch_id)
        fetch.update(
            {
                "image_id": image_id,
                "evidence_image_id": image_id,
                "fetch_status": "budget_pruned",
                "retrieval_status": "budget_pruned",
                "policy_decision": "blocked",
                "failure_code": "budget_pruned",
            }
        )
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)
        self.write_codex_interactive_observation(
            run_dir,
            provider_run_id="codex-child:mixed-blocked-budget-pruned",
        )

        result = ingest_vision_observations(run=run_dir, provider="codex-interactive")

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertFalse(result["visual_artifact_validation"]["valid"])
        post_candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        post_fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        post_candidate = next(item for item in post_candidates if item["candidate_id"] == candidate_id)
        post_fetch = next(item for item in post_fetches if item["fetch_id"] == fetch_id)
        self.assertEqual(post_candidate["policy_decision"], "blocked")
        self.assertEqual(post_candidate["candidate_status"], "budget_pruned")
        self.assertEqual(post_candidate["rejection_reason"], "budget_pruned")
        self.assertEqual(post_fetch["policy_decision"], "blocked")
        self.assertEqual(post_fetch["fetch_status"], "budget_pruned")
        self.assertEqual(post_fetch["retrieval_status"], "budget_pruned")
        self.assertEqual(post_fetch["failure_code"], "budget_pruned")
        self.assertNotIn("raw_codex_interactive_handoff_candidate_status", post_candidate)
        self.assertNotIn("raw_codex_interactive_handoff_fetch_status", post_fetch)

        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertFalse(visual_validation.valid)
        self.assertTrue(
            any(
                error.code == "lineage_mismatch"
                and "policy_decision" in error.path
                for error in visual_validation.errors
            ),
            [error.to_dict() for error in visual_validation.errors],
        )

    def test_codex_interactive_keeps_license_robots_protected_budget_pruned_sidecars_visible(
        self,
    ) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        image_id = "img_checkout_001"
        candidate_id = "cand_checkout_001"
        fetch_id = "fetch_checkout_001"

        candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        candidate = next(item for item in candidates if item["candidate_id"] == candidate_id)
        candidate.update(
            {
                "image_id": image_id,
                "evidence_image_id": image_id,
                "candidate_status": "budget_pruned",
                "policy_decision": "budget_pruned",
                "rejection_reason": "budget_pruned",
                "license_policy": "manual_review",
                "robots_policy": "allowed",
            }
        )
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)

        fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        fetch = next(item for item in fetches if item["fetch_id"] == fetch_id)
        fetch.update(
            {
                "image_id": image_id,
                "evidence_image_id": image_id,
                "fetch_status": "budget_pruned",
                "retrieval_status": "budget_pruned",
                "policy_decision": "budget_pruned",
                "failure_code": "budget_pruned",
                "license_policy": "allowed",
                "robots_policy": "disallowed",
            }
        )
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)
        self.write_codex_interactive_observation(
            run_dir,
            provider_run_id="codex-child:license-robots-protected-budget-pruned",
        )

        result = ingest_vision_observations(run=run_dir, provider="codex-interactive")

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertFalse(result["visual_artifact_validation"]["valid"])
        post_candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        post_fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        post_candidate = next(item for item in post_candidates if item["candidate_id"] == candidate_id)
        post_fetch = next(item for item in post_fetches if item["fetch_id"] == fetch_id)
        self.assertEqual(post_candidate["license_policy"], "manual_review")
        self.assertEqual(post_candidate["policy_decision"], "budget_pruned")
        self.assertEqual(post_candidate["candidate_status"], "budget_pruned")
        self.assertEqual(post_candidate["rejection_reason"], "budget_pruned")
        self.assertEqual(post_fetch["robots_policy"], "disallowed")
        self.assertEqual(post_fetch["policy_decision"], "budget_pruned")
        self.assertEqual(post_fetch["fetch_status"], "budget_pruned")
        self.assertEqual(post_fetch["retrieval_status"], "budget_pruned")
        self.assertEqual(post_fetch["failure_code"], "budget_pruned")
        self.assertNotIn("raw_codex_interactive_handoff_policy_decision", post_candidate)
        self.assertNotIn("raw_codex_interactive_handoff_candidate_status", post_candidate)
        self.assertNotIn("raw_codex_interactive_handoff_policy_decision", post_fetch)
        self.assertNotIn("raw_codex_interactive_handoff_fetch_status", post_fetch)

        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertFalse(visual_validation.valid)
        self.assertTrue(
            any(
                error.code == "lineage_mismatch"
                and "policy_decision" in error.path
                for error in visual_validation.errors
            ),
            [error.to_dict() for error in visual_validation.errors],
        )

    def test_codex_interactive_keeps_non_budget_fetch_artifact_mismatch_visible(
        self,
    ) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        image_id = "img_checkout_001"
        candidate_id = "cand_checkout_001"
        fetch_id = "fetch_checkout_001"

        candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        candidate = next(item for item in candidates if item["candidate_id"] == candidate_id)
        candidate.update(
            {
                "image_id": "img_corrupt_sidecar",
                "evidence_image_id": "img_corrupt_sidecar",
                "candidate_status": "fetched",
                "policy_decision": "allowed",
                "rejection_reason": None,
            }
        )
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)

        fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        fetch = next(item for item in fetches if item["fetch_id"] == fetch_id)
        fetch.update(
            {
                "image_id": "img_corrupt_sidecar",
                "evidence_image_id": image_id,
                "fetch_status": "fetched",
                "retrieval_status": "fetched",
                "policy_decision": "allowed",
                "failure_code": None,
                "local_artifact_path": "images/corrupt-sidecar.png",
                "hash": "sha256:corrupt-sidecar",
                "width": 99,
                "height": 99,
                "byte_size": 999,
            }
        )
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)
        self.write_codex_interactive_observation(
            run_dir,
            provider_run_id="codex-child:non-budget-artifact-mismatch",
        )

        result = ingest_vision_observations(run=run_dir, provider="codex-interactive")

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertFalse(result["visual_artifact_validation"]["valid"])
        post_candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        post_fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        post_candidate = next(item for item in post_candidates if item["candidate_id"] == candidate_id)
        post_fetch = next(item for item in post_fetches if item["fetch_id"] == fetch_id)
        self.assertEqual(post_candidate["image_id"], "img_corrupt_sidecar")
        self.assertEqual(post_candidate["evidence_image_id"], "img_corrupt_sidecar")
        self.assertEqual(post_fetch["local_artifact_path"], "images/corrupt-sidecar.png")
        self.assertEqual(post_fetch["hash"], "sha256:corrupt-sidecar")
        self.assertEqual(post_fetch["width"], 99)
        self.assertEqual(post_fetch["height"], 99)
        self.assertEqual(post_fetch["byte_size"], 999)
        self.assertNotIn("raw_codex_interactive_handoff_local_artifact_path", post_fetch)
        self.assertNotIn("raw_codex_interactive_handoff_hash", post_fetch)

        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertFalse(visual_validation.valid)
        self.assertTrue(
            any(
                error.code == "lineage_mismatch"
                and "local_artifact_path" in error.path
                for error in visual_validation.errors
            ),
            [error.to_dict() for error in visual_validation.errors],
        )
        self.assertTrue(
            any(
                error.code == "lineage_mismatch"
                and error.path.endswith(".hash")
                for error in visual_validation.errors
            ),
            [error.to_dict() for error in visual_validation.errors],
        )

    def test_codex_interactive_metadata_child_reconciles_to_fetched_root_artifact(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        image_id = "img_x"
        candidate_id = "cand_x"
        fetch_id = "fetch_x"
        task_id = "task_001"
        angle_id = "angle_001"
        route = "visual_required"
        plan_id = "plan_task_001_angle_001_visual_required"
        image_hash = "sha256:" + hashlib.sha256(PNG_1X1).hexdigest()
        metadata_path = run_dir / "images" / "img_x.json"
        fetched_path = run_dir / "images" / "cand_x.jpg"
        fetched_path.write_bytes(PNG_1X1)
        self.assertFalse(metadata_path.exists())

        evidence = self.load_json(run_dir / "evidence.json")
        evidence.setdefault("search_tasks", []).append(
            {
                "id": task_id,
                "angle_id": angle_id,
                "route": route,
                "modality": route,
                "visual_tasks": ["layout_review"],
            }
        )
        self.write_json(run_dir / "evidence.json", evidence)
        plan_payload = self.load_json(run_dir / "visual_search_plan.json")
        plan_payload.setdefault("tasks", []).append(
            {
                "plan_id": plan_id,
                "task_id": task_id,
                "semantic_plan_task_id": task_id,
                "angle_id": angle_id,
                "route": route,
                "target_evidence_type": "page_image",
                "query": "https://example.com/checkout#root-image",
                "providers": ["codex-native"],
                "caps": {
                    "max_candidates": 1,
                    "max_fetches": 1,
                    "max_vlm_images": 1,
                    "max_cost_usd": 0.0,
                },
                "policy_constraints": {},
                "estimated_cost_usd": 0.0,
                "state": "completed",
                "provider": "codex-native",
                "provider_mode": "real",
                "handoff_artifact": "visual_search_plan.json",
            }
        )
        self.write_json(run_dir / "visual_search_plan.json", plan_payload)
        provider_provenance = {
            "provider": "browser-screenshot",
            "provider_kind": "screenshot",
            "provider_mode": "real",
            "provider_run_id": "browser-screenshot:root",
            "source_image_id": image_id,
            "external_network_call": False,
            "external_vlm_call": False,
        }
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
                    "provider_run_id": "browser-screenshot:root",
                    "provider_provenance": provider_provenance,
                    "raw_provider_metadata": {"source_image_id": image_id},
                    "source_id": "src_checkout",
                    "origin": "page_image",
                    "source_url": "https://example.com/checkout",
                    "page_url": "https://example.com/checkout",
                    "image_url": "https://example.com/checkout#root-image",
                    "rank": 1,
                    "score": 1.0,
                    "candidate_class": "page_image",
                    "local_artifact_path": "images/cand_x.jpg",
                    "mime_type": "image/jpeg",
                    "artifact_size_bytes": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "hash": image_hash,
                    "phash": "cand-x-phash",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "candidate_status": "fetched",
                    "rejection_reason": None,
                    "estimated_cost_usd": 0.02,
                    "actual_cost_usd": 0.01,
                    "visual_tasks": ["layout_review"],
                    "requires_vlm_observation": True,
                },
                {
                    "candidate_id": f"cand_{image_id}",
                    "image_id": image_id,
                    "evidence_image_id": image_id,
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "semantic_plan_task_id": task_id,
                    "angle_id": angle_id,
                    "route": route,
                    "provider": "codex-native",
                    "provider_kind": "web_image_search",
                    "provider_mode": "real",
                    "provider_run_id": task_id,
                    "provider_provenance": {
                        "provider": "codex-native",
                        "provider_kind": "web_image_search",
                        "provider_mode": "real",
                        "search_provider": "codex-native",
                        "codex_native_handoff": True,
                        "source_image_id": image_id,
                    },
                    "source_id": "src_checkout",
                    "origin": "page_image",
                    "source_url": "https://example.com/checkout",
                    "page_url": "https://example.com/checkout",
                    "image_url": "https://example.com/checkout#root-image",
                    "rank": 1,
                    "score": 1.0,
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "candidate_status": "fetch_failed",
                    "rejection_reason": "missing_local_artifact",
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                    "handoff_artifact": "visual_candidates.jsonl",
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
                    "provider_run_id": "browser-screenshot:root",
                    "provider_provenance": provider_provenance,
                    "raw_provider_metadata": {"source_image_id": image_id},
                    "fetch_status": "fetched",
                    "http_status": 200,
                    "mime_type": "image/jpeg",
                    "byte_size": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "hash": image_hash,
                    "phash": "cand-x-phash",
                    "local_artifact_path": "images/cand_x.jpg",
                    "evidence_image_id": None,
                    "source_image_id": image_id,
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "failure_code": None,
                    "estimated_cost_usd": 0.02,
                    "actual_cost_usd": 0.01,
                },
                {
                    "fetch_id": f"fetch_{image_id}",
                    "candidate_id": f"cand_{image_id}",
                    "image_id": image_id,
                    "evidence_image_id": image_id,
                    "source_image_id": image_id,
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "semantic_plan_task_id": task_id,
                    "angle_id": angle_id,
                    "route": route,
                    "provider": "codex-native",
                    "provider_kind": "web_image_search",
                    "provider_mode": "real",
                    "provider_run_id": task_id,
                    "provider_provenance": {
                        "provider": "codex-native",
                        "provider_kind": "web_image_search",
                        "provider_mode": "real",
                        "search_provider": "codex-native",
                        "codex_native_handoff": True,
                        "source_image_id": image_id,
                    },
                    "raw_provider_metadata": {"source_image_id": image_id},
                    "fetch_status": "failed",
                    "retrieval_status": "failed",
                    "http_status": None,
                    "mime_type": "image/jpeg",
                    "byte_size": None,
                    "width": 512,
                    "height": 512,
                    "hash": None,
                    "phash": None,
                    "local_artifact_path": "images/img_x.json",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "failure_code": "missing_local_artifact",
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                    "handoff_artifact": "image_fetch_status.jsonl",
                }
            ],
        )
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "id": image_id,
                    "image_id": image_id,
                    "evidence_image_id": image_id,
                    "source_id": "src_checkout",
                    "origin": "page_image",
                    "source_url": "https://example.com/checkout",
                    "image_url": "https://example.com/checkout#root-image",
                    "page_url": "https://example.com/checkout",
                    "local_artifact_path": "images/img_x.json",
                    "mime_type": "image/jpeg",
                    "width": 0,
                    "height": 0,
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "provider_run_id": "codex-child:metadata-root",
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "observations": ["The child VLM observation identifies the checkout button."],
                    "inferences": ["The visual evidence supports checkout completion."],
                    "visual_tasks": ["layout_review"],
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                    "caveats": ["metadata-only visual record; no local image artifact was provided"],
                    "codex_interactive_handoff": True,
                    "external_vlm_call": False,
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "provider_run_id": "codex-child:metadata-root",
                        "codex_interactive_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            ],
        )

        result = ingest_vision_observations(run=run_dir, provider="codex-interactive")

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertFalse(metadata_path.exists())
        self.assertTrue(
            result["visual_artifact_validation"]["valid"],
            result["visual_artifact_validation"],
        )
        evidence = self.assert_valid_run(run_dir)
        image = next(item for item in evidence["images"] if item["id"] == image_id)
        self.assertEqual(image["candidate_id"], candidate_id)
        self.assertEqual(image["fetch_id"], fetch_id)
        self.assertEqual(image["local_artifact_path"], "images/cand_x.jpg")
        self.assertEqual(image["mime_type"], "image/jpeg")
        self.assertEqual(image["artifact_size_bytes"], len(PNG_1X1))
        self.assertEqual(image["estimated_cost_usd"], 0.02)
        self.assertEqual(image["actual_cost_usd"], 0.01)
        self.assertNotIn(
            "metadata-only visual record; no local image artifact was provided",
            image.get("caveats", []),
        )
        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(observations[0]["evidence_image_id"], image_id)
        self.assertEqual(observations[0]["local_artifact_path"], "images/cand_x.jpg")
        self.assertEqual(observations[0]["candidate_id"], candidate_id)
        self.assertEqual(observations[0]["fetch_id"], fetch_id)
        self.assertEqual(observations[0]["estimated_cost_usd"], 0.02)
        self.assertEqual(observations[0]["actual_cost_usd"], 0.01)
        post_fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        stale_fetch = next(item for item in post_fetches if item["fetch_id"] == f"fetch_{image_id}")
        self.assertEqual(stale_fetch["fetch_status"], "failed")
        self.assertEqual(stale_fetch["failure_code"], "missing_local_artifact")
        self.assertEqual(stale_fetch["local_artifact_path"], "images/img_x.json")
        self.assertIsNone(stale_fetch["evidence_image_id"])
        self.assertIsNone(stale_fetch["image_id"])
        self.assertIsNone(stale_fetch["source_image_id"])
        self.assertIsNone(stale_fetch["provider_provenance"]["source_image_id"])
        self.assertIsNone(stale_fetch["raw_provider_metadata"]["source_image_id"])
        post_candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        stale_candidate = next(
            item for item in post_candidates if item["candidate_id"] == f"cand_{image_id}"
        )
        self.assertEqual(stale_candidate["candidate_status"], "fetch_failed")
        self.assertIsNone(stale_candidate["evidence_image_id"])
        self.assertIsNone(stale_candidate["image_id"])
        minimums = visual_minimums_for_run(run_dir, required_vlm_images=1)
        self.assertEqual(minimums["fetched_artifacts"], 1)
        self.assertEqual(minimums["vlm_images_analyzed"], 1)

        verify = verify_claims(run=run_dir)
        report = synthesize_report(run=run_dir)
        self.assertEqual(verify["status"], "completed")
        self.assertEqual(report["status"], "completed")
        linked_minimums = visual_minimums_for_run(run_dir, required_vlm_images=1)
        self.assertGreaterEqual(linked_minimums["report_cited_images"], 1)

    def test_codex_interactive_lineage_uses_actual_task_route_and_angle(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        image_id = "img_route_angle_reconciled"
        candidate_id = "cand_route_angle_reconciled"
        fetch_id = "fetch_route_angle_reconciled"
        task_id = "task_visual_015"
        actual_angle_id = "angle_004"
        actual_route = "visual_optional"
        stale_angle_id = "angle_999"
        stale_route = "visual_required"
        stale_plan_id = "plan_task_visual_015_angle_999_visual_required"
        expected_plan_id = "plan_task_visual_015_angle_004_visual_optional"
        image_hash = "sha256:" + hashlib.sha256(PNG_1X1).hexdigest()

        self.write_json(
            run_dir / "visual_tasks.json",
            {
                "schema_version": "codex-deepresearch.visual-tasks.v0",
                "tasks": [
                    {
                        "id": task_id,
                        "angle_id": actual_angle_id,
                        "route": actual_route,
                        "query": "Inspect the official visual handwashing card.",
                    }
                ],
            },
        )
        self.write_json(
            run_dir / "visual_search_plan.json",
            {
                "schema_version": "codex-deepresearch.visual-artifacts.v0",
                "run_id": run_dir.name,
                "created_at": "2026-06-25T00:00:00Z",
                "tasks": [
                    {
                        "plan_id": stale_plan_id,
                        "task_id": task_id,
                        "angle_id": stale_angle_id,
                        "route": stale_route,
                        "target_evidence_type": "screenshot",
                        "query": "stale generated visual task",
                        "providers": ["browser-screenshot"],
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
        provider_provenance = {
            "provider": "browser-screenshot",
            "provider_kind": "screenshot",
            "provider_mode": "real",
            "provider_run_id": "browser-screenshot:stale-lineage",
            "external_network_call": False,
            "external_vlm_call": False,
        }
        self.write_jsonl(
            run_dir / "visual_candidates.jsonl",
            [
                {
                    "candidate_id": candidate_id,
                    "plan_id": stale_plan_id,
                    "task_id": task_id,
                    "angle_id": stale_angle_id,
                    "route": stale_route,
                    "provider": "browser-screenshot",
                    "provider_kind": "screenshot",
                    "provider_mode": "real",
                    "provider_run_id": "browser-screenshot:stale-lineage",
                    "provider_provenance": provider_provenance,
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "page_url": "https://example.com/checkout",
                    "image_url": (run_dir / "images" / "checkout.png").resolve().as_uri(),
                    "rank": 1,
                    "score": 1.0,
                    "candidate_class": "screenshot",
                    "local_artifact_path": "images/checkout.png",
                    "mime_type": "image/png",
                    "artifact_size_bytes": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "hash": image_hash,
                    "phash": "route-angle-phash",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "candidate_status": "fetched",
                    "rejection_reason": None,
                    "estimated_cost_usd": 0.01,
                    "actual_cost_usd": 0.01,
                    "visual_tasks": ["layout_review"],
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
                    "plan_id": stale_plan_id,
                    "task_id": task_id,
                    "angle_id": stale_angle_id,
                    "route": stale_route,
                    "provider": "browser-screenshot",
                    "provider_kind": "screenshot",
                    "provider_mode": "real",
                    "provider_run_id": "browser-screenshot:stale-lineage",
                    "provider_provenance": provider_provenance,
                    "fetch_status": "fetched",
                    "http_status": 200,
                    "mime_type": "image/png",
                    "byte_size": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "hash": image_hash,
                    "phash": "route-angle-phash",
                    "local_artifact_path": "images/checkout.png",
                    "evidence_image_id": image_id,
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "failure_code": None,
                    "estimated_cost_usd": 0.01,
                    "actual_cost_usd": 0.01,
                }
            ],
        )
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "id": image_id,
                    "image_id": image_id,
                    "evidence_image_id": image_id,
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "image_url": (run_dir / "images" / "checkout.png").resolve().as_uri(),
                    "page_url": "https://example.com/checkout",
                    "local_artifact_path": "images/checkout.png",
                    "mime_type": "image/png",
                    "artifact_size_bytes": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "hash": image_hash,
                    "phash": "route-angle-phash",
                    "candidate_id": candidate_id,
                    "fetch_id": fetch_id,
                    "plan_id": stale_plan_id,
                    "task_id": task_id,
                    "angle_id": stale_angle_id,
                    "route": stale_route,
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "provider_run_id": "codex-child:stale-lineage",
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "observations": ["The child VLM observation identifies the official card."],
                    "inferences": ["The image supports the visual task."],
                    "visual_tasks": ["layout_review"],
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                    "caveats": [],
                    "codex_interactive_handoff": True,
                    "external_vlm_call": False,
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "provider_run_id": "codex-child:stale-lineage",
                        "codex_interactive_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            ],
        )

        result = ingest_vision_observations(run=run_dir, provider="codex-interactive")

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertTrue(
            result["visual_artifact_validation"]["valid"],
            result["visual_artifact_validation"],
        )
        evidence = self.assert_valid_run(run_dir)
        image = next(item for item in evidence["images"] if item["id"] == image_id)
        self.assertEqual(image["plan_id"], expected_plan_id)
        self.assertEqual(image["angle_id"], actual_angle_id)
        self.assertEqual(image["route"], actual_route)
        plan = self.load_json(run_dir / "visual_search_plan.json")
        plan_record = next(item for item in plan["tasks"] if item["plan_id"] == expected_plan_id)
        self.assertEqual(plan_record["task_id"], task_id)
        self.assertEqual(plan_record["angle_id"], actual_angle_id)
        self.assertEqual(plan_record["route"], actual_route)
        candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        candidate = next(item for item in candidates if item["candidate_id"] == candidate_id)
        fetch = next(item for item in fetches if item["fetch_id"] == fetch_id)
        for record in (candidate, fetch):
            self.assertEqual(record["plan_id"], expected_plan_id)
            self.assertEqual(record["angle_id"], actual_angle_id)
            self.assertEqual(record["route"], actual_route)
        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(observations[0]["plan_id"], expected_plan_id)
        self.assertEqual(observations[0]["angle_id"], actual_angle_id)
        self.assertEqual(observations[0]["route"], actual_route)

    def test_codex_interactive_lineage_reconciles_claim_support_route_and_angle(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        image_id = "img_route_angle_claim_support"
        candidate_id = "cand_route_angle_claim_support"
        fetch_id = "fetch_route_angle_claim_support"
        task_id = "task_visual_015"
        actual_angle_id = "angle_004"
        actual_route = "visual_optional"
        stale_angle_id = "angle_999"
        stale_route = "visual_required"
        stale_plan_id = "plan_task_visual_015_angle_999_visual_required"
        expected_plan_id = "plan_task_visual_015_angle_004_visual_optional"
        image_hash = "sha256:" + hashlib.sha256(PNG_1X1).hexdigest()
        claim_id = "claim_route_angle_visual_support"

        self.write_json(
            run_dir / "visual_tasks.json",
            {
                "schema_version": "codex-deepresearch.visual-tasks.v0",
                "tasks": [
                    {
                        "id": task_id,
                        "angle_id": actual_angle_id,
                        "route": actual_route,
                        "query": "Inspect the official visual handwashing card.",
                    }
                ],
            },
        )
        self.write_json(
            run_dir / "visual_search_plan.json",
            {
                "schema_version": "codex-deepresearch.visual-artifacts.v0",
                "run_id": run_dir.name,
                "created_at": "2026-06-25T00:00:00Z",
                "tasks": [
                    {
                        "plan_id": stale_plan_id,
                        "task_id": task_id,
                        "angle_id": stale_angle_id,
                        "route": stale_route,
                        "target_evidence_type": "screenshot",
                        "query": "stale generated visual task",
                        "providers": ["browser-screenshot"],
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
        provider_provenance = {
            "provider": "browser-screenshot",
            "provider_kind": "screenshot",
            "provider_mode": "real",
            "provider_run_id": "browser-screenshot:stale-claim-lineage",
            "external_network_call": False,
            "external_vlm_call": False,
        }
        self.write_jsonl(
            run_dir / "visual_candidates.jsonl",
            [
                {
                    "candidate_id": candidate_id,
                    "plan_id": stale_plan_id,
                    "task_id": task_id,
                    "angle_id": stale_angle_id,
                    "route": stale_route,
                    "provider": "browser-screenshot",
                    "provider_kind": "screenshot",
                    "provider_mode": "real",
                    "provider_run_id": "browser-screenshot:stale-claim-lineage",
                    "provider_provenance": provider_provenance,
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "page_url": "https://example.com/checkout",
                    "image_url": (run_dir / "images" / "checkout.png").resolve().as_uri(),
                    "rank": 1,
                    "score": 1.0,
                    "candidate_class": "screenshot",
                    "local_artifact_path": "images/checkout.png",
                    "mime_type": "image/png",
                    "artifact_size_bytes": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "hash": image_hash,
                    "phash": "route-angle-claim-support-phash",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "candidate_status": "fetched",
                    "rejection_reason": None,
                    "estimated_cost_usd": 0.01,
                    "actual_cost_usd": 0.01,
                    "visual_tasks": ["layout_review"],
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
                    "plan_id": stale_plan_id,
                    "task_id": task_id,
                    "angle_id": stale_angle_id,
                    "route": stale_route,
                    "provider": "browser-screenshot",
                    "provider_kind": "screenshot",
                    "provider_mode": "real",
                    "provider_run_id": "browser-screenshot:stale-claim-lineage",
                    "provider_provenance": provider_provenance,
                    "fetch_status": "fetched",
                    "http_status": 200,
                    "mime_type": "image/png",
                    "byte_size": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "hash": image_hash,
                    "phash": "route-angle-claim-support-phash",
                    "local_artifact_path": "images/checkout.png",
                    "evidence_image_id": image_id,
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "failure_code": None,
                    "estimated_cost_usd": 0.01,
                    "actual_cost_usd": 0.01,
                }
            ],
        )
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "id": image_id,
                    "image_id": image_id,
                    "evidence_image_id": image_id,
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "image_url": (run_dir / "images" / "checkout.png").resolve().as_uri(),
                    "page_url": "https://example.com/checkout",
                    "local_artifact_path": "images/checkout.png",
                    "mime_type": "image/png",
                    "artifact_size_bytes": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "hash": image_hash,
                    "phash": "route-angle-claim-support-phash",
                    "candidate_id": candidate_id,
                    "fetch_id": fetch_id,
                    "plan_id": stale_plan_id,
                    "task_id": task_id,
                    "angle_id": stale_angle_id,
                    "route": stale_route,
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "provider_run_id": "codex-child:stale-claim-lineage",
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "observations": ["The child VLM observation identifies the official card."],
                    "inferences": ["The image supports the visual task."],
                    "visual_tasks": ["layout_review"],
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                    "caveats": [],
                    "verifier_links": [],
                    "report_links": [],
                    "codex_interactive_handoff": True,
                    "external_vlm_call": False,
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "provider_run_id": "codex-child:stale-claim-lineage",
                        "codex_interactive_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            ],
        )
        evidence = self.load_json(run_dir / "evidence.json")
        evidence.setdefault("claims", []).append(
            {
                "id": claim_id,
                "text": "The official card is visible in the screenshot.",
                "claim_type": "visual",
                "supporting_sources": ["src_checkout"],
                "supporting_images": [image_id],
                "visual_supports": [
                    {
                        "image_id": image_id,
                        "evidence_image_id": image_id,
                        "observation_index": 0,
                        "observation_ref": f"images.{image_id}.observations[0]",
                        "observation_text": "The child VLM observation identifies the official card.",
                        "relation_type": "screenshot_support",
                        "provider": "codex-interactive",
                        "rationale": "Regression fixture for route and angle lineage reconciliation.",
                        "plan_id": stale_plan_id,
                        "task_id": task_id,
                        "angle_id": stale_angle_id,
                        "route": stale_route,
                        "candidate_id": "cand_img_route_angle_claim_support",
                        "fetch_id": "fetch_img_route_angle_claim_support",
                        "confidence": 0.74,
                    }
                ],
                "quote_spans": [],
                "votes": [],
                "verification_status": "unverified",
                "review_status": "not_reviewed",
                "promotion_status": "not_eligible",
                "confidence": "low",
                "caveats": [],
            }
        )
        self.write_json(run_dir / "evidence.json", evidence)

        result = ingest_vision_observations(run=run_dir, provider="codex-interactive")

        self.assertEqual(result["status"], "visual_evidence_ingested")
        evidence = self.assert_valid_run(run_dir)
        claim = next(item for item in evidence["claims"] if item["id"] == claim_id)
        support = claim["visual_supports"][0]
        expected_lineage = {
            "plan_id": expected_plan_id,
            "task_id": task_id,
            "angle_id": actual_angle_id,
            "route": actual_route,
            "candidate_id": candidate_id,
            "fetch_id": fetch_id,
            "evidence_image_id": image_id,
        }
        for field, value in expected_lineage.items():
            self.assertEqual(support[field], value)

    def test_codex_interactive_rerun_reconciles_stale_visual_support_links(self) -> None:
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

        evidence = self.load_json(run_dir / "evidence.json")
        claim = next(
            item
            for item in evidence["claims"]
            if item.get("claim_type") == "visual"
            and "img_checkout_001" in item.get("supporting_images", [])
        )
        support = claim["visual_supports"][0]
        stale_lineage = {
            "plan_id": "plan_task_001_angle_999_visual_optional",
            "task_id": "task_001",
            "angle_id": "angle_999",
            "route": "visual_optional",
            "candidate_id": "cand_img_checkout_001",
            "fetch_id": "fetch_img_checkout_001",
        }
        support.update(stale_lineage)
        self.write_json(run_dir / "evidence.json", evidence)

        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for field, value in stale_lineage.items():
            observations[0]["verifier_links"][0][field] = value
            observations[0]["report_links"][0][field] = value
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)

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

        evidence = self.assert_valid_run(run_dir)
        claim = next(item for item in evidence["claims"] if item["id"] == claim["id"])
        support = claim["visual_supports"][0]
        expected_lineage = {
            "plan_id": "plan_task_visual_001",
            "task_id": "task_visual_001",
            "angle_id": "angle_001",
            "route": "visual_required",
            "candidate_id": "cand_checkout_001",
            "fetch_id": "fetch_checkout_001",
            "evidence_image_id": "img_checkout_001",
        }
        for field, value in expected_lineage.items():
            self.assertEqual(support[field], value)

        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        verifier_link = observations[0]["verifier_links"][0]
        report_link = observations[0]["report_links"][0]
        for link in (verifier_link, report_link):
            for field, value in expected_lineage.items():
                self.assertEqual(link[field], value)

        report_status = self.load_json(run_dir / "report_status.json")
        report_claim = next(
            item for item in report_status["included_claims"] if item["claim_id"] == claim["id"]
        )
        report_support = report_claim["visual_supports"][0]
        for field, value in expected_lineage.items():
            self.assertEqual(report_support[field], value)
        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(visual_validation.valid, [error.to_dict() for error in visual_validation.errors])

    def test_codex_interactive_rerun_removes_stale_visual_support_without_links(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        stale_image_id = "img_task_016_korea_2024_cardnews"
        evidence = self.load_json(run_dir / "evidence.json")
        evidence.setdefault("images", []).append(
            {
                "id": stale_image_id,
                "source_id": "src_checkout",
                "origin": "page_image",
                "source_url": "https://example.com/checkout",
                "image_url": "https://example.com/stale-card.png",
                "page_url": "https://example.com/checkout",
                "local_artifact_path": "evidence_shards/task_016/source_004.html",
                "mime_type": "text/html",
                "artifact_size_bytes": 120,
                "width": 0,
                "height": 0,
                "observations": ["Stale cardnews observation."],
                "inferences": [],
                "visual_tasks": ["stale_review"],
                "analysis_provider": "codex-interactive",
                "analysis_status": "analyzed",
                "task_id": "task_016",
                "angle_id": "angle_004",
                "route": "text_only",
                "provider": "codex-interactive",
                "provider_kind": "vlm",
                "provider_mode": "real",
                "policy_decision": "allowed",
                "policy_flags": [],
                "estimated_cost_usd": 0.0,
                "actual_cost_usd": 0.0,
            }
        )
        evidence.setdefault("claims", []).append(
            {
                "id": "claim_stale_cardnews_support",
                "text": "The stale cardnews support should not remain linked.",
                "claim_type": "mixed",
                "supporting_sources": ["src_checkout"],
                "supporting_images": [stale_image_id],
                "visual_supports": [
                    {
                        "image_id": stale_image_id,
                        "evidence_image_id": stale_image_id,
                        "observation_index": 0,
                        "observation_ref": f"images.{stale_image_id}.observations[0]",
                        "observation_text": "Stale cardnews observation.",
                        "relation_type": "ocr_support",
                        "provider": "codex-interactive",
                        "task_id": "task_016",
                        "angle_id": "angle_004",
                        "route": "text_only",
                        "confidence": 0.74,
                    }
                ],
                "quote_spans": [],
                "votes": [],
                "verification_status": "supported",
                "review_status": "not_reviewed",
                "promotion_status": "not_eligible",
                "confidence": "medium",
                "caveats": [],
            }
        )
        self.write_json(run_dir / "evidence.json", evidence)

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
        evidence = self.assert_valid_run(run_dir)
        self.assertNotIn(stale_image_id, {image["id"] for image in evidence["images"]})
        stale_claim = next(
            claim for claim in evidence["claims"] if claim["id"] == "claim_stale_cardnews_support"
        )
        self.assertNotIn(stale_image_id, stale_claim.get("supporting_images", []))
        self.assertFalse(
            any(
                support.get("image_id") == stale_image_id
                for support in stale_claim.get("visual_supports", [])
                if isinstance(support, dict)
            )
        )
        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(visual_validation.valid, [error.to_dict() for error in visual_validation.errors])

    def test_verification_prunes_visual_supporting_image_without_support(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)

        ingest = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            codex_client=FakeCodexInteractiveVisionClient(),
        )

        self.assertEqual(ingest["status"], "visual_evidence_ingested")
        evidence = self.load_json(run_dir / "evidence.json")
        claim = next(
            item
            for item in evidence["claims"]
            if item.get("claim_type") == "visual"
            and item.get("source_image_id") == "img_checkout_001"
        )
        extra_image_id = "img_unlinked_current_001"
        extra_image = dict(evidence["images"][0])
        extra_image.update(
            {
                "id": extra_image_id,
                "evidence_image_id": extra_image_id,
                "observations": ["The unrelated visual observation is current."],
                "source_id": "src_checkout",
                "candidate_id": "cand_unlinked_001",
                "fetch_id": "fetch_unlinked_001",
            }
        )
        evidence["images"].append(extra_image)
        claim["supporting_images"].append(extra_image_id)
        self.write_json(run_dir / "evidence.json", evidence)

        candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        extra_candidate = dict(candidates[0])
        extra_candidate["candidate_id"] = "cand_unlinked_001"
        candidates.append(extra_candidate)
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)

        fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        extra_fetch = dict(fetches[0])
        extra_fetch.update(
            {
                "fetch_id": "fetch_unlinked_001",
                "candidate_id": "cand_unlinked_001",
                "evidence_image_id": extra_image_id,
            }
        )
        fetches.append(extra_fetch)
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)

        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        extra_observation = dict(observations[0])
        extra_observation.update(
            {
                "id": extra_image_id,
                "observation_id": "obs_img_unlinked_current_001_001",
                "evidence_image_id": extra_image_id,
                "observations": ["The unrelated visual observation is current."],
                "verifier_links": [],
                "report_links": [],
                "candidate_id": "cand_unlinked_001",
                "fetch_id": "fetch_unlinked_001",
            }
        )
        observations.append(extra_observation)
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)

        verify = verify_claims(run=run_dir)
        report = synthesize_report(run=run_dir)

        self.assertEqual(verify["status"], "completed")
        self.assertEqual(report["status"], "completed")
        evidence = self.assert_valid_run(run_dir)
        claim = next(item for item in evidence["claims"] if item["id"] == claim["id"])
        self.assertNotIn(extra_image_id, claim["supporting_images"])
        self.assertTrue(
            any(
                support.get("image_id") == "img_checkout_001"
                for support in claim.get("visual_supports", [])
            )
        )
        self.assertEqual(
            evidence["verification_matrix"]["dangling_visual_supporting_images_pruned"],
            1,
        )
        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(visual_validation.valid, [error.to_dict() for error in visual_validation.errors])

    def test_codex_interactive_rerun_prunes_stale_image_vote_refs(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        valid_image_id = "img_checkout_001"
        stale_image_id = "img_task_016_korea_2024_cardnews"
        evidence = self.load_json(run_dir / "evidence.json")
        evidence.setdefault("images", []).append(
            {
                "id": stale_image_id,
                "source_id": "src_checkout",
                "origin": "page_image",
                "source_url": "https://example.com/checkout",
                "image_url": "https://example.com/stale-card.png",
                "page_url": "https://example.com/checkout",
                "local_artifact_path": "evidence_shards/task_016/source_004.html",
                "mime_type": "text/html",
                "artifact_size_bytes": 120,
                "width": 0,
                "height": 0,
                "observations": ["Stale cardnews observation."],
                "inferences": [],
                "visual_tasks": ["stale_review"],
                "analysis_provider": "codex-interactive",
                "analysis_status": "analyzed",
                "task_id": "task_016",
                "angle_id": "angle_004",
                "route": "text_only",
                "provider": "codex-interactive",
                "provider_kind": "vlm",
                "provider_mode": "real",
                "policy_decision": "allowed",
                "policy_flags": [],
                "estimated_cost_usd": 0.0,
                "actual_cost_usd": 0.0,
            }
        )
        evidence.setdefault("claims", []).append(
            {
                "id": "claim_stale_cardnews_vote_ref",
                "text": "The stale cardnews support should not remain vote evidence.",
                "claim_type": "mixed",
                "supporting_sources": ["src_checkout"],
                "supporting_images": [stale_image_id],
                "visual_supports": [
                    {
                        "image_id": stale_image_id,
                        "evidence_image_id": stale_image_id,
                        "observation_index": 0,
                        "observation_ref": f"images.{stale_image_id}.observations[0]",
                        "observation_text": "Stale cardnews observation.",
                        "relation_type": "ocr_support",
                        "provider": "codex-interactive",
                        "task_id": "task_016",
                        "angle_id": "angle_004",
                        "route": "text_only",
                        "confidence": 0.74,
                    }
                ],
                "quote_spans": [],
                "votes": [
                    {
                        "id": "vote_stale_cardnews_vote_ref",
                        "claim_id": "claim_stale_cardnews_vote_ref",
                        "verifier_type": "visual",
                        "agent_name": "codex-interactive",
                        "method": "codex-subagent",
                        "model_or_tool": "codex-interactive",
                        "vote": "support",
                        "confidence": 0.72,
                        "evidence_refs": ["src_checkout", stale_image_id, valid_image_id],
                        "rationale": "Regression fixture for stale visual vote evidence refs.",
                        "created_at": "2026-06-22T00:00:00Z",
                    }
                ],
                "verification_status": "supported",
                "review_status": "not_reviewed",
                "promotion_status": "not_eligible",
                "confidence": "medium",
                "caveats": [],
            }
        )
        self.write_json(run_dir / "evidence.json", evidence)

        ingest = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            codex_client=FakeCodexInteractiveVisionClient(),
        )

        self.assertEqual(ingest["status"], "visual_evidence_ingested")
        self.assertEqual(ingest["stale_visual_vote_evidence_refs_cleared"], 1)
        evidence = self.assert_valid_run(run_dir)
        self.assertEqual(
            evidence["vision_adapter"]["stale_visual_vote_evidence_refs_cleared"],
            1,
        )
        stale_claim = next(
            claim for claim in evidence["claims"] if claim["id"] == "claim_stale_cardnews_vote_ref"
        )
        self.assertEqual(stale_claim["votes"][0]["evidence_refs"], ["src_checkout", valid_image_id])
        for claim in evidence["claims"]:
            for vote in claim.get("votes", []):
                if isinstance(vote, dict):
                    self.assertNotIn(stale_image_id, vote.get("evidence_refs", []))
        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(visual_validation.valid, [error.to_dict() for error in visual_validation.errors])

    def test_codex_interactive_rerun_prunes_already_removed_stale_image_vote_refs(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        stale_image_id = "img_task_016_korea_2024_cardnews"
        evidence = self.load_json(run_dir / "evidence.json")
        evidence.setdefault("claims", []).append(
            {
                "id": "claim_already_removed_stale_cardnews_vote_ref",
                "text": "The stale cardnews vote ref should be removed after prior cleanup.",
                "claim_type": "text",
                "supporting_sources": ["src_checkout"],
                "supporting_images": [],
                "visual_supports": [],
                "quote_spans": [],
                "votes": [
                    {
                        "id": "vote_already_removed_stale_cardnews_vote_ref",
                        "claim_id": "claim_already_removed_stale_cardnews_vote_ref",
                        "verifier_type": "text",
                        "agent_name": "verification-agent",
                        "method": "codex-subagent",
                        "model_or_tool": "codex",
                        "vote": "support",
                        "confidence": 0.76,
                        "evidence_refs": ["src_checkout", stale_image_id],
                        "rationale": "Regression fixture for stale image refs after prior cleanup.",
                        "created_at": "2026-06-22T00:00:00Z",
                    }
                ],
                "verification_status": "supported",
                "review_status": "not_reviewed",
                "promotion_status": "not_eligible",
                "confidence": "medium",
                "caveats": [],
            }
        )
        self.write_json(run_dir / "evidence.json", evidence)

        ingest = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            codex_client=FakeCodexInteractiveVisionClient(),
        )

        self.assertEqual(ingest["status"], "visual_evidence_ingested")
        self.assertEqual(ingest["stale_visual_vote_evidence_refs_cleared"], 1)
        evidence = self.assert_valid_run(run_dir)
        stale_claim = next(
            claim
            for claim in evidence["claims"]
            if claim["id"] == "claim_already_removed_stale_cardnews_vote_ref"
        )
        self.assertEqual(stale_claim["votes"][0]["evidence_refs"], ["src_checkout"])
        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(visual_validation.valid, [error.to_dict() for error in visual_validation.errors])

    def test_codex_interactive_rerun_decanonicalizes_stale_placeholder_fetch_lineage(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)

        ingest = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            codex_client=FakeCodexInteractiveVisionClient(),
        )
        self.assertEqual(ingest["status"], "visual_evidence_ingested")

        evidence = self.load_json(run_dir / "evidence.json")
        evidence.setdefault("search_tasks", []).append(
            {
                "id": "task_001",
                "angle_id": "angle_001",
                "route": "visual_required",
                "modality": "visual_required",
            }
        )
        self.write_json(run_dir / "evidence.json", evidence)

        plan = self.load_json(run_dir / "visual_search_plan.json")
        plan["tasks"].append(
            {
                "plan_id": "plan_task_001_angle_001_visual_required",
                "task_id": "task_001",
                "semantic_plan_task_id": "task_001",
                "angle_id": "angle_001",
                "route": "visual_required",
                "target_evidence_type": "page_image",
                "query": "https://example.com/checkout",
                "providers": ["codex-native"],
                "caps": {
                    "max_candidates": 1,
                    "max_fetches": 1,
                    "max_vlm_images": 1,
                    "max_cost_usd": 0.0,
                },
                "policy_constraints": {},
                "estimated_cost_usd": 0.0,
                "state": "completed",
                "provider": "codex-native",
                "provider_mode": "real",
                "handoff_artifact": "visual_search_plan.json",
            }
        )
        self.write_json(run_dir / "visual_search_plan.json", plan)

        candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        candidates.append(
            {
                "candidate_id": "cand_img_checkout_001",
                "image_id": "img_checkout_001",
                "evidence_image_id": "img_checkout_001",
                "plan_id": "plan_task_001_angle_001_visual_required",
                "task_id": "task_001",
                "semantic_plan_task_id": "task_001",
                "angle_id": "angle_001",
                "route": "visual_required",
                "provider": "codex-native",
                "provider_kind": "screenshot",
                "provider_mode": "real",
                "provider_run_id": "task_001",
                "provider_provenance": {
                    "provider": "codex-native",
                    "provider_kind": "screenshot",
                    "provider_mode": "real",
                    "codex_native_handoff": True,
                },
                "source_id": "src_checkout",
                "origin": "screenshot",
                "page_url": "https://example.com/checkout",
                "image_url": "https://example.com/checkout",
                "rank": 1,
                "score": 1.0,
                "policy_decision": "allowed",
                "policy_flags": [],
                "candidate_status": "fetch_failed",
                "rejection_reason": "missing_local_artifact",
                "estimated_cost_usd": 0.0,
                "actual_cost_usd": 0.0,
                "handoff_artifact": "visual_candidates.jsonl",
            }
        )
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)

        fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        fetches.append(
            {
                "fetch_id": "fetch_img_checkout_001",
                "candidate_id": "cand_img_checkout_001",
                "image_id": "img_checkout_001",
                "evidence_image_id": "img_checkout_001",
                "plan_id": "plan_task_001_angle_001_visual_required",
                "task_id": "task_001",
                "semantic_plan_task_id": "task_001",
                "angle_id": "angle_001",
                "route": "visual_required",
                "provider": "codex-native",
                "provider_kind": "screenshot",
                "provider_mode": "real",
                "provider_run_id": "task_001",
                "provider_provenance": {
                    "provider": "codex-native",
                    "provider_kind": "screenshot",
                    "provider_mode": "real",
                    "codex_native_handoff": True,
                },
                "fetch_status": "failed",
                "http_status": None,
                "mime_type": "image/png",
                "byte_size": None,
                "width": 1,
                "height": 1,
                "hash": None,
                "phash": None,
                "local_artifact_path": "images/img_checkout_001.json",
                "policy_decision": "allowed",
                "policy_flags": [],
                "failure_code": "missing_local_artifact",
                "estimated_cost_usd": 0.0,
                "actual_cost_usd": 0.0,
                "handoff_artifact": "image_fetch_status.jsonl",
            }
        )
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)

        ingest = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            codex_client=FakeCodexInteractiveVisionClient(),
        )

        self.assertEqual(ingest["status"], "visual_evidence_ingested")
        evidence = self.assert_valid_run(run_dir)
        image = next(item for item in evidence["images"] if item["id"] == "img_checkout_001")
        self.assertEqual(image["candidate_id"], "cand_checkout_001")
        self.assertEqual(image["fetch_id"], "fetch_checkout_001")
        self.assertEqual(image["plan_id"], "plan_task_visual_001")

        candidates = [
            json.loads(line)
            for line in (run_dir / "visual_candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        plan = self.load_json(run_dir / "visual_search_plan.json")
        stale_candidate = next(
            item for item in candidates if item["candidate_id"] == "cand_img_checkout_001"
        )
        stale_fetch = next(item for item in fetches if item["fetch_id"] == "fetch_img_checkout_001")
        self.assertEqual(stale_candidate["candidate_status"], "fetch_failed")
        self.assertIsNone(stale_candidate["evidence_image_id"])
        self.assertIsNone(stale_candidate["image_id"])
        self.assertEqual(stale_fetch["fetch_status"], "failed")
        self.assertEqual(stale_fetch["failure_code"], "missing_local_artifact")
        self.assertIsNone(stale_fetch["evidence_image_id"])
        self.assertIsNone(stale_fetch["image_id"])
        self.assertTrue(
            any(
                item["plan_id"] == "plan_task_001_angle_001_visual_required"
                for item in plan["tasks"]
            )
        )
        visual_validation = validate_visual_artifacts(run_dir=run_dir)
        self.assertTrue(visual_validation.valid, [error.to_dict() for error in visual_validation.errors])

    def test_codex_interactive_metadata_only_child_lineage_does_not_count_as_fetched(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        existing_fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for fetch in existing_fetches:
            fetch["evidence_image_id"] = None
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", existing_fetches)
        image_id = "img_task_001_001"
        candidate_id = "cand_img_task_001_001"
        fetch_id = "fetch_img_task_001_001"
        task_id = "task_001"
        angle_id = "angle_001"
        route = "visual_required"
        plan_id = "plan_task_001_angle_001_visual_required"
        metadata_path = run_dir / "images" / "img_task_001_001.json"
        self.write_json(
            metadata_path,
            {
                "kind": "metadata-only visual record",
                "image_url": "https://example.com/checkout#poster",
            },
        )

        evidence = self.load_json(run_dir / "evidence.json")
        evidence.setdefault("search_tasks", []).append(
            {
                "id": task_id,
                "angle_id": angle_id,
                "route": route,
                "modality": route,
                "visual_tasks": ["layout_review"],
            }
        )
        evidence["images"] = [
            {
                "id": image_id,
                "source_id": "src_checkout",
                "origin": "page_image",
                "source_url": "https://example.com/checkout",
                "image_url": "https://example.com/checkout#poster",
                "page_url": "https://example.com/checkout",
                "local_artifact_path": "images/img_task_001_001.json",
                "mime_type": "image/jpeg",
                "artifact_size_bytes": None,
                "width": 0,
                "height": 0,
                "observations": ["Metadata-only Codex visual observation."],
                "inferences": ["This should not count as a fetched local image artifact."],
                "visual_tasks": ["layout_review"],
                "analysis_provider": "codex-interactive",
                "analysis_status": "analyzed",
                "candidate_id": candidate_id,
                "fetch_id": fetch_id,
                "plan_id": plan_id,
                "task_id": task_id,
                "angle_id": angle_id,
                "route": route,
                "provider": "codex-interactive",
                "provider_kind": "vlm",
                "provider_mode": "real",
                "provider_run_id": "codex-child:metadata-only",
                "provider_provenance": {
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "provider_run_id": "codex-child:metadata-only",
                    "codex_interactive_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "external_vlm_call": False,
                },
                "policy_decision": "allowed",
                "policy_flags": [],
                "estimated_cost_usd": 0.0,
                "actual_cost_usd": 0.0,
                "caveats": ["metadata-only visual record; no local image artifact was provided"],
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "id": image_id,
                    "evidence_image_id": image_id,
                    "source_id": "src_checkout",
                    "origin": "page_image",
                    "image_url": "https://example.com/checkout#poster",
                    "page_url": "https://example.com/checkout",
                    "local_artifact_path": "images/img_task_001_001.json",
                    "mime_type": "image/jpeg",
                    "width": 0,
                    "height": 0,
                    "candidate_id": candidate_id,
                    "fetch_id": fetch_id,
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "angle_id": angle_id,
                    "route": route,
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "provider_run_id": "codex-child:metadata-only",
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "observations": ["Metadata-only Codex visual observation."],
                    "inferences": ["This should not count as a fetched local image artifact."],
                    "visual_tasks": ["layout_review"],
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                    "caveats": ["metadata-only visual record; no local image artifact was provided"],
                    "codex_interactive_handoff": True,
                    "external_vlm_call": False,
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "provider_run_id": "codex-child:metadata-only",
                        "codex_interactive_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            ],
        )

        result = ingest_vision_observations(run=run_dir, provider="codex-interactive")

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertTrue(
            result["visual_artifact_validation"]["valid"],
            result["visual_artifact_validation"],
        )
        post_fetches = [
            json.loads(line)
            for line in (run_dir / "image_fetch_status.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        child_fetch = next(item for item in post_fetches if item["fetch_id"] == fetch_id)
        self.assertEqual(child_fetch["fetch_status"], "failed")
        self.assertEqual(child_fetch["failure_code"], "missing_local_artifact")
        provider_status = self.load_json(run_dir / "visual_provider_status.json")
        provider = provider_status["providers"][-1]
        self.assertEqual(provider["artifacts_fetched"], 0)
        self.assertEqual(provider["vlm_images_analyzed"], 1)
        self.assertEqual(provider["diagnostics"]["metadata_only_records"], 1)
        minimums = visual_minimums_for_run(run_dir, required_vlm_images=1)
        self.assertEqual(minimums["vlm_images_analyzed"], 0)
        self.assertFalse(minimums["satisfied"])

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
        self.assertEqual(image["visual_acquisition_provider_kind"], "screenshot")
        self.assertEqual(image["visual_acquisition_provider_mode"], "real")
        self.assertEqual(
            image["visual_acquisition_provider_provenance"]["provider"],
            "browser-screenshot",
        )
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

    def test_codex_interactive_supplements_existing_metadata_only_observations(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        self.append_codex_vlm_handoff_artifact(run_dir, 2)
        self.append_codex_vlm_handoff_artifact(run_dir, 3)
        metadata_only_observations = []
        for index in (1, 2, 3):
            image_id = f"img_checkout_{index:03d}"
            metadata_only_observations.append(
                {
                    "id": image_id,
                    "image_id": image_id,
                    "evidence_image_id": image_id,
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "image_url": "https://example.com/checkout.png",
                    "page_url": "https://example.com/checkout",
                    "local_artifact_path": f"images/{image_id}.json",
                    "mime_type": "application/json",
                    "candidate_id": f"cand_checkout_{index:03d}",
                    "fetch_id": f"fetch_checkout_{index:03d}",
                    "plan_id": "plan_task_visual_001",
                    "task_id": "task_visual_001",
                    "angle_id": "angle_001",
                    "route": "visual_required",
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "observations": ["Metadata-only child visual note."],
                    "inferences": [],
                    "visual_tasks": ["layout_review"],
                    "caveats": [
                        "metadata-only visual record; no local image artifact was provided"
                    ],
                    "codex_interactive_handoff": True,
                    "codex_native_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "external_vlm_call": False,
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "codex_interactive_handoff": True,
                        "codex_native_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            )
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            metadata_only_observations,
        )
        before = visual_minimums_for_run(run_dir, required_vlm_images=3)
        self.assertEqual(before["fetched_artifacts"], 3)
        client = FakeCodexInteractiveVisionClient()

        result = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            codex_client=client,
        )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(
            {
                call["metadata"]["evidence_image_id"]
                for call in client.calls
            },
            {"img_checkout_001", "img_checkout_002", "img_checkout_003"},
        )
        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(observations), 3)
        self.assertNotIn(
            "images/img_checkout_001.json",
            {item.get("local_artifact_path") for item in observations},
        )
        self.assertNotIn(
            "images/img_checkout_002.json",
            {item.get("local_artifact_path") for item in observations},
        )
        self.assertNotIn(
            "images/img_checkout_003.json",
            {item.get("local_artifact_path") for item in observations},
        )
        minimums = visual_minimums_for_run(run_dir, required_vlm_images=3)
        self.assertEqual(minimums["fetched_artifacts"], 3)
        self.assertGreaterEqual(minimums["vlm_images_analyzed"], 3)
        self.assertNotEqual(minimums["shortfall_reason"], "vlm_failures")

    def test_codex_interactive_supplement_prioritizes_missing_semantic_obligations(self) -> None:
        run_dir = self.temp_runs_dir() / "supplement_missing_semantic_obligation"
        run_dir.mkdir()
        (run_dir / "images").mkdir()
        for image_name in ("task1_a", "task1_b", "task1_c", "task2_a"):
            (run_dir / "images" / f"{image_name}.png").write_bytes(PNG_1X1)

        semantic_tasks = [
            {
                "task_id": "task_001",
                "angle_id": "angle_001",
                "query": "covered task",
                "route": "visual_required",
                "expected_visual_targets": ["poster image"],
                "max_images": 3,
            },
            {
                "task_id": "task_002",
                "angle_id": "angle_002",
                "query": "missing task",
                "route": "visual_required",
                "expected_visual_targets": ["poster image"],
                "max_images": 1,
            },
        ]
        self.write_json(
            run_dir / "semantic_plan.json",
            {
                "schema_version": "codex-deepresearch.semantic-planner.v0",
                "artifact_type": "semantic_plan",
                "semantic_plan": {"bounded_tasks": semantic_tasks},
            },
        )
        self.write_json(
            run_dir / "evidence.json",
            {
                "run_id": run_dir.name,
                "sources": [
                    {
                        "id": "src_supplement",
                        "type": "web",
                        "url": "https://example.com/supplement",
                        "license_policy": "allowed",
                        "robots_policy": "allowed",
                        "policy_decision": "allowed",
                    }
                ],
                "routing": [
                    {"id": "angle_001", "modality": "visual_required", "max_images": 3},
                    {"id": "angle_002", "modality": "visual_required", "max_images": 1},
                ],
                "images": [],
                "claims": [],
            },
        )

        image_specs = [
            ("task_001", "angle_001", "task1_a", "img_task1_a"),
            ("task_001", "angle_001", "task1_b", "img_task1_b"),
            ("task_001", "angle_001", "task1_c", "img_task1_c"),
            ("task_002", "angle_002", "task2_a", "img_task2_a"),
        ]
        candidates = []
        fetches = []
        observations = []
        for index, (task_id, angle_id, image_name, image_id) in enumerate(image_specs, start=1):
            candidate_id = f"cand_{image_name}"
            fetch_id = f"fetch_{image_name}"
            local_artifact_path = f"images/{image_name}.png"
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "id": candidate_id,
                    "plan_id": f"plan_{task_id}",
                    "task_id": task_id,
                    "semantic_plan_task_id": task_id,
                    "angle_id": angle_id,
                    "route": "visual_required",
                    "source_id": "src_supplement",
                    "provider": "browser-screenshot",
                    "provider_kind": "screenshot",
                    "provider_mode": "real",
                    "provider_run_id": run_dir.name,
                    "provider_provenance": {
                        "provider": "browser-screenshot",
                        "provider_kind": "screenshot",
                        "provider_mode": "real",
                    },
                    "origin": "screenshot",
                    "page_url": "https://example.com/supplement",
                    "image_url": None,
                    "rank": index,
                    "score": 1.0 / index,
                    "candidate_class": "screenshot",
                    "local_artifact_path": local_artifact_path,
                    "mime_type": "image/png",
                    "width": 1,
                    "height": 1,
                    "hash": f"sha256:{image_name}",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "candidate_status": "fetched",
                    "rejection_reason": None,
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                }
            )
            fetches.append(
                {
                    "fetch_id": fetch_id,
                    "candidate_id": candidate_id,
                    "plan_id": f"plan_{task_id}",
                    "task_id": task_id,
                    "semantic_plan_task_id": task_id,
                    "angle_id": angle_id,
                    "route": "visual_required",
                    "provider": "browser-screenshot",
                    "provider_kind": "screenshot",
                    "provider_mode": "real",
                    "provider_run_id": run_dir.name,
                    "provider_provenance": {
                        "provider": "browser-screenshot",
                        "provider_kind": "screenshot",
                        "provider_mode": "real",
                    },
                    "fetch_status": "fetched",
                    "http_status": 200,
                    "mime_type": "image/png",
                    "byte_size": len(PNG_1X1),
                    "width": 1,
                    "height": 1,
                    "hash": f"sha256:{image_name}",
                    "phash": f"phash:{image_name}",
                    "local_artifact_path": local_artifact_path,
                    "evidence_image_id": image_id,
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "failure_code": None,
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                }
            )
            if task_id == "task_001":
                observations.append(
                    {
                        "id": image_id,
                        "image_id": image_id,
                        "evidence_image_id": image_id,
                        "source_id": "src_supplement",
                        "origin": "screenshot",
                        "local_artifact_path": local_artifact_path,
                        "mime_type": "image/png",
                        "candidate_id": candidate_id,
                        "fetch_id": fetch_id,
                        "plan_id": f"plan_{task_id}",
                        "task_id": task_id,
                        "semantic_plan_task_id": task_id,
                        "angle_id": angle_id,
                        "route": "visual_required",
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "observation_status": "analyzed",
                        "observations": ["Existing analyzed image."],
                        "inferences": [],
                        "policy_decision": "allowed",
                        "policy_flags": [],
                    }
                )

        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)

        tasks = _codex_interactive_supplemental_vision_tasks(
            run_dir=run_dir,
            evidence=self.load_json(run_dir / "evidence.json"),
            records=observations,
            codex_config={"max_images": 1},
            provider_mode="real",
        )

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["semantic_plan_task_id"], "task_002")
        self.assertEqual(tasks[0]["evidence_image_id"], "img_task2_a")

    def test_codex_interactive_supplements_child_observations_without_artifacts(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        self.append_codex_vlm_handoff_artifact(run_dir, 2)
        self.append_codex_vlm_handoff_artifact(run_dir, 3)
        stale_observations = []
        for index in (1, 2, 3):
            image_id = f"img_checkout_{index:03d}"
            stale_observations.append(
                {
                    "id": image_id,
                    "image_id": image_id,
                    "evidence_image_id": image_id,
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "image_url": "https://example.com/checkout.png",
                    "page_url": "https://example.com/checkout",
                    "local_artifact_path": f"images/missing_child_{index}.png",
                    "mime_type": "image/png",
                    "candidate_id": f"cand_checkout_{index:03d}",
                    "fetch_id": f"fetch_checkout_{index:03d}",
                    "plan_id": "plan_task_visual_001",
                    "task_id": "task_visual_001",
                    "angle_id": "angle_001",
                    "route": "visual_required",
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "observations": ["Stale child observation without an artifact."],
                    "inferences": [],
                    "caveats": [],
                    "codex_interactive_handoff": True,
                    "codex_native_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "external_vlm_call": False,
                }
            )
        self.write_jsonl(run_dir / "visual_observations.jsonl", stale_observations)
        client = FakeCodexInteractiveVisionClient()

        result = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            codex_client=client,
        )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(result["dropped_invalid_observation_records"], 3)
        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(observations), 3)
        self.assertFalse(
            {
                item.get("local_artifact_path")
                for item in observations
                if str(item.get("local_artifact_path", "")).startswith("images/missing_child_")
            }
        )
        minimums = visual_minimums_for_run(run_dir, required_vlm_images=3)
        self.assertEqual(minimums["fetched_artifacts"], 3)
        self.assertEqual(minimums["vlm_images_analyzed"], 3)

    def test_codex_interactive_drops_invalid_child_observations_when_supplemented(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        self.append_codex_vlm_handoff_artifact(run_dir, 2)
        self.append_codex_vlm_handoff_artifact(run_dir, 3)
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "id": "img_checkout_001",
                    "image_id": "img_checkout_001",
                    "evidence_image_id": "img_checkout_001",
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "image_url": "https://example.com/checkout.png",
                    "local_artifact_path": "images/missing_child.png",
                    "mime_type": "image/png",
                    "candidate_id": "cand_checkout_001",
                    "fetch_id": "fetch_checkout_001",
                    "plan_id": "plan_task_visual_001",
                    "task_id": "task_visual_001",
                    "angle_id": "angle_001",
                    "route": "visual_required",
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "observations": ["Invalid child observation without a file."],
                }
            ],
        )

        result = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            codex_config={"max_images": 1},
            codex_client=FakeCodexInteractiveVisionClient(),
        )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertEqual(result["dropped_invalid_observation_records"], 1)
        diagnostics = result["dropped_invalid_observation_diagnostics"]
        self.assertEqual(diagnostics[0]["code"], "normalization_failed")
        self.assertIn("does not reference an existing run-local file", diagnostics[0]["detail"])
        evidence = self.assert_valid_run(run_dir)
        self.assertEqual(evidence["vision_adapter"]["dropped_invalid_observation_records"], 1)
        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["local_artifact_path"], "images/checkout.png")

    def test_codex_interactive_release_minimum_ignores_invalid_child_observations(self) -> None:
        run_dir = self.prepared_visual_run(provider="codex-interactive")
        self.write_codex_vlm_handoff(run_dir)
        self.append_codex_vlm_handoff_artifact(run_dir, 2)
        self.append_codex_vlm_handoff_artifact(run_dir, 3)
        invalid_observations = []
        for index in (1, 2, 3):
            image_id = f"img_checkout_{index:03d}"
            invalid_observations.append(
                {
                    "id": image_id,
                    "image_id": image_id,
                    "evidence_image_id": image_id,
                    "source_id": "src_checkout",
                    "origin": "screenshot",
                    "image_url": "https://example.com/checkout.png",
                    "local_artifact_path": f"images/missing_release_{index}.png",
                    "mime_type": "image/png",
                    "candidate_id": f"cand_checkout_{index:03d}",
                    "fetch_id": f"fetch_checkout_{index:03d}",
                    "plan_id": "plan_task_visual_001",
                    "task_id": "task_visual_001",
                    "angle_id": "angle_001",
                    "route": "visual_required",
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "analysis_status": "analyzed",
                    "observation_status": "analyzed",
                    "observations": ["Invalid child observation must not satisfy release."],
                }
            )
        self.write_jsonl(run_dir / "visual_observations.jsonl", invalid_observations)

        result = ingest_vision_observations(
            run=run_dir,
            provider="codex-interactive",
            codex_config={"max_images": 1},
            codex_client=FakeCodexInteractiveVisionClient(),
        )

        self.assertEqual(result["status"], "visual_evidence_ingested")
        self.assertEqual(result["vlm_images_analyzed"], 1)
        self.assertEqual(result["dropped_invalid_observation_records"], 3)
        minimums = visual_minimums_for_run(run_dir, required_vlm_images=3)
        self.assertEqual(minimums["fetched_artifacts"], 3)
        self.assertEqual(minimums["vlm_images_analyzed"], 1)
        self.assertFalse(minimums["satisfied"])
        self.assertEqual(minimums["shortfall_reason"], "vlm_failures")

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

    def test_missing_non_metadata_local_artifact_fails_normalization(self) -> None:
        run_dir = self.prepared_visual_run()
        observations_path = run_dir / "missing_artifact.jsonl"
        self.write_jsonl(
            observations_path,
            [
                {
                    "image_id": "missing-artifact",
                    "source_id": "src_checkout",
                    "local_artifact_path": "images/missing.png",
                    "image_url": "https://example.com/checkout#missing-image",
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
