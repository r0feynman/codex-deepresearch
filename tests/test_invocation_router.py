from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

import deepresearch.invocation_router as invocation_router  # noqa: E402
from deepresearch.invocation_router import run_skill_invocation  # noqa: E402
from deepresearch.page_image_extraction import FetchResponse  # noqa: E402
from deepresearch.visual_artifacts import VISUAL_PROVIDER_STATUS_FILENAME  # noqa: E402
from deepresearch.vision_adapter import OpenAIResponsesVisionResult  # noqa: E402


class InvocationRouterTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def read_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def write_jsonl(self, path: Path, records: list[dict]) -> None:
        path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

    def write_visual_lineage_fixture(
        self,
        run_dir: Path,
        *,
        duplicate_plan_id: bool = False,
    ) -> None:
        created_at = "2026-06-30T00:00:00Z"
        tasks = [
            {
                "id": f"task_visual_{index:03d}",
                "angle_id": f"angle_{index:03d}",
                "route": "visual_required",
            }
            for index in range(1, 4)
        ]
        self.write_json(
            run_dir / "research_tasks.json",
            {
                "schema_version": "codex-deepresearch.parallel.v0",
                "run_id": run_dir.name,
                "created_at": created_at,
                "tasks": tasks,
            },
        )
        plans = []
        candidates = []
        fetches = []
        observations = []
        images = []
        image_ids = []
        candidate_index = 1
        for task_index, task in enumerate(tasks, start=1):
            plan_id = self.visual_plan_id(task["id"], task["angle_id"], task["route"])
            plans.append(
                {
                    "plan_id": plan_id,
                    "task_id": task["id"],
                    "angle_id": task["angle_id"],
                    "route": task["route"],
                    "target_evidence_type": "web_image",
                    "query": f"visual lineage {task_index}",
                    "providers": ["child-discovered-image-url"],
                    "source_search_result_ids": [],
                    "caps": {
                        "max_candidates": 4,
                        "max_fetches": 1,
                        "max_vlm_images": 1,
                        "max_cost_usd": 0.25,
                    },
                    "policy_constraints": {"robots": "allowed"},
                    "estimated_cost_usd": 0.03,
                    "state": "completed",
                }
            )
            for local_index in range(1, 5 if task_index == 1 else 4):
                candidate_id = f"cand_lineage_{candidate_index:03d}"
                candidate = {
                    "candidate_id": candidate_id,
                    "plan_id": plan_id,
                    "task_id": task["id"],
                    "angle_id": task["angle_id"],
                    "route": task["route"],
                    "provider": "child-discovered-image-url",
                    "provider_kind": "web_image_search",
                    "provider_mode": "real",
                    "provider_run_id": "run_lineage_real",
                    "provider_provenance": {
                        "provider": "child-discovered-image-url",
                        "provider_kind": "web_image_search",
                        "provider_mode": "real",
                    },
                    "origin": "image_search",
                    "page_url": f"https://example.com/page-{candidate_index}",
                    "image_url": f"https://example.com/image-{candidate_index}.png",
                    "rank": candidate_index,
                    "score": 1.0 / candidate_index,
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "candidate_status": "analyzed" if local_index == 1 else "discovered",
                    "rejection_reason": None,
                    "estimated_cost_usd": 0.01,
                    "actual_cost_usd": 0.01,
                }
                candidates.append(candidate)
                if local_index == 1:
                    image_id = f"img_lineage_{task_index:03d}"
                    fetch_id = f"fetch_lineage_{task_index:03d}"
                    image_ids.append(image_id)
                    fetch = {
                        "fetch_id": fetch_id,
                        "candidate_id": candidate_id,
                        "plan_id": plan_id,
                        "task_id": task["id"],
                        "angle_id": task["angle_id"],
                        "route": task["route"],
                        "provider": "child-discovered-image-url",
                        "provider_kind": "web_image_search",
                        "provider_mode": "real",
                        "provider_run_id": "run_lineage_real",
                        "provider_provenance": dict(candidate["provider_provenance"]),
                        "fetch_status": "fetched",
                        "http_status": 200,
                        "mime_type": "image/png",
                        "byte_size": 128,
                        "width": 640,
                        "height": 360,
                        "hash": f"sha256:lineage:{task_index}",
                        "phash": f"phash:lineage:{task_index}",
                        "local_artifact_path": f"images/{image_id}.png",
                        "evidence_image_id": image_id,
                        "policy_decision": "allowed",
                        "policy_flags": [],
                        "failure_code": None,
                        "estimated_cost_usd": 0.01,
                        "actual_cost_usd": 0.01,
                    }
                    fetches.append(fetch)
                    images.append(self.evidence_image_from_fetch(fetch))
                    link_lineage = {
                        "plan_id": plan_id,
                        "task_id": task["id"],
                        "angle_id": task["angle_id"],
                        "route": task["route"],
                        "candidate_id": candidate_id,
                        "fetch_id": fetch_id,
                        "evidence_image_id": image_id,
                    }
                    observations.append(
                        {
                            "observation_id": f"obs_{image_id}",
                            "evidence_image_id": image_id,
                            "plan_id": plan_id,
                            "task_id": task["id"],
                            "angle_id": task["angle_id"],
                            "route": task["route"],
                            "candidate_id": candidate_id,
                            "fetch_id": fetch_id,
                            "provider": "codex-interactive",
                            "provider_kind": "vlm",
                            "provider_mode": "real",
                            "provider_run_id": "run_lineage_vlm",
                            "provider_provenance": {
                                "provider": "codex-interactive",
                                "provider_kind": "vlm",
                                "provider_mode": "real",
                                "codex_native_handoff": True,
                            },
                            "model_or_tool": "codex-interactive",
                            "observation_status": "analyzed",
                            "observations": ["The image shows public visual evidence."],
                            "inferences": ["The image can support the report claim."],
                            "confidence": 0.87,
                            "policy_decision": "allowed",
                            "policy_flags": [],
                            "caveats": [],
                            "verifier_links": [
                                {
                                    "claim_id": "claim_lineage_visual",
                                    "visual_support_ref": f"images.{image_id}.observations[0]",
                                    "verifier_vote_id": f"vote_lineage_{task_index:03d}",
                                    **link_lineage,
                                }
                            ],
                            "report_links": [
                                {
                                    "claim_id": "claim_lineage_visual",
                                    "report_section_id": "visual-findings",
                                    "citation_id": f"img:{image_id}",
                                    **link_lineage,
                                }
                            ],
                            "estimated_cost_usd": 0.0,
                            "actual_cost_usd": 0.0,
                            "created_at": created_at,
                        }
                    )
                candidate_index += 1
        if duplicate_plan_id:
            plans[1]["plan_id"] = plans[0]["plan_id"]
        self.write_json(
            run_dir / "visual_search_plan.json",
            {
                "schema_version": "codex-deepresearch.visual-artifacts.v0",
                "run_id": run_dir.name,
                "created_at": created_at,
                "tasks": plans,
            },
        )
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)
        visual_supports = [
            {
                "image_id": image["id"],
                "evidence_image_id": image["id"],
                "observation_ref": f"images.{image['id']}.observations[0]",
                "observation_index": 0,
                "observation_text": image["observations"][0],
                "relation_type": "visual_match",
                "provider": "codex-interactive",
                "plan_id": image["plan_id"],
                "task_id": image["task_id"],
                "angle_id": image["angle_id"],
                "route": image["route"],
                "candidate_id": image["candidate_id"],
                "fetch_id": image["fetch_id"],
            }
            for image in images
        ]
        evidence = {
            "schema_version": "0.1.0",
            "run_id": run_dir.name,
            "question": "Visual lineage finalizer fixture",
            "mode": "codex-plugin",
            "vlm_provider": "codex-interactive",
            "routing": [
                {"id": task["angle_id"], "modality": task["route"], "max_images": 4}
                for task in tasks
            ],
            "images": images,
            "claims": [
                {
                    "id": "claim_lineage_visual",
                    "text": "The visual lineage fixture includes cited image evidence.",
                    "claim_type": "mixed",
                    "supporting_sources": [],
                    "supporting_images": image_ids,
                    "visual_supports": visual_supports,
                    "quote_spans": [],
                    "votes": [{"id": f"vote_lineage_{index:03d}"} for index in range(1, 4)],
                    "verification_status": "supported",
                    "review_status": "human_accepted",
                    "promotion_status": "promoted_memory",
                    "confidence": "high",
                    "caveats": [],
                }
            ],
        }
        self.write_json(run_dir / "evidence.json", evidence)
        self.write_json(
            run_dir / VISUAL_PROVIDER_STATUS_FILENAME,
            {
                "schema_version": "codex-deepresearch.visual-provider-status.v0",
                "run_id": run_dir.name,
                "run_dir": str(run_dir),
                "status": "visual_evidence_ingested",
                "ok": True,
                "terminal": False,
                "metric_classification": "codex_native_visual_worker",
                "providers": [
                    self.provider_record("child-discovered-image-url", "web_image_search", 3, 10, 3, 0),
                    self.provider_record("codex-interactive", "vlm", 1, 0, 3, 3),
                ],
                "diagnostics": {"actionable_cause": "fixture visual lineage artifacts ready"},
            },
        )

    def evidence_image_from_fetch(self, fetch: dict) -> dict:
        return {
            "id": fetch["evidence_image_id"],
            "plan_id": fetch["plan_id"],
            "task_id": fetch["task_id"],
            "angle_id": fetch["angle_id"],
            "route": fetch["route"],
            "candidate_id": fetch["candidate_id"],
            "fetch_id": fetch["fetch_id"],
            "local_artifact_path": fetch["local_artifact_path"],
            "hash": fetch["hash"],
            "provider": fetch["provider"],
            "provider_kind": fetch["provider_kind"],
            "provider_mode": fetch["provider_mode"],
            "provider_provenance": dict(fetch["provider_provenance"]),
            "policy_decision": "allowed",
            "estimated_cost_usd": 0.0,
            "actual_cost_usd": 0.0,
            "analysis_status": "analyzed",
            "observations": ["The image shows public visual evidence."],
        }

    def report_status_payload(self, image_id: str) -> dict:
        return {
            "status": "completed",
            "used_images": [image_id],
            "included_claims": [
                {
                    "claim_id": "claim_lineage_visual",
                    "claim_type": "mixed",
                    "verification_status": "supported",
                    "image_ids": [image_id],
                    "visual_supports": [
                        {
                            "image_id": image_id,
                            "evidence_image_id": image_id,
                            "observation_ref": f"images.{image_id}.observations[0]",
                            "observation_index": 0,
                            "observation_text": "The image shows public visual evidence.",
                            "relation_type": "visual_match",
                            "provider": "codex-interactive",
                            "plan_id": "plan_task_visual_001_angle_001_visual_required",
                            "task_id": "task_visual_001",
                            "angle_id": "angle_001",
                            "route": "visual_required",
                            "candidate_id": "cand_lineage_001",
                            "fetch_id": "fetch_lineage_001",
                        }
                    ],
                }
            ],
        }

    def provider_record(
        self,
        provider: str,
        provider_kind: str,
        invocations: int,
        candidates_discovered: int,
        artifacts_fetched: int,
        vlm_images_analyzed: int,
    ) -> dict:
        return {
            "provider": provider,
            "provider_kind": provider_kind,
            "provider_mode": "real",
            "configured": True,
            "available": True,
            "blocked_reason": None,
            "invocations": invocations,
            "candidates_discovered": candidates_discovered,
            "artifacts_fetched": artifacts_fetched,
            "vlm_images_analyzed": vlm_images_analyzed,
            "estimated_cost_usd": 0.0,
            "actual_cost_usd": 0.0,
            "last_error": None,
        }

    def visual_plan_id(self, task_id: str, angle_id: str, route: str) -> str:
        return "plan_" + "_".join((task_id, angle_id, route))

    def test_default_deep_research_invocation_blocks_when_semantic_planner_unavailable(self) -> None:
        result = run_skill_invocation(
            "$deep-research: investigate deterministic router fixture",
            runs_dir=self.temp_runs_dir(),
            adapter_name="fixture",
            route="text_only",
            budget_preset="quick",
            min_tasks=2,
            max_tasks=2,
        )

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["terminal"])
        self.assertEqual(result["selected_mode"], "blocked")
        self.assertEqual(result["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(result["provenance"]["type"], "blocked_semantic_planner_unavailable")
        self.assertIn("run_status", result["artifacts"])
        self.assertIn("evidence", result["artifacts"])
        self.assertIn("semantic_planner_validation", result["artifacts"])
        self.assertEqual(result["artifact_handoff"]["run_dir"], result["run_dir"])
        self.assertNotIn("parallel_orchestration_status", result["artifacts"])
        self.assertNotIn("report_status", result["artifacts"])
        self.assertEqual(result["planner_mode"], "blocked")
        self.assertFalse(result["semantic_release_eligible"])
        self.assertEqual(
            result["semantic_planning"]["review_verdict"],
            "release_ineligible",
        )
        self.assertFalse(result["semantic_planning"]["validation_ok"])
        self.assertIn("semantic_planning", result["diagnostics"])

        persisted = self.read_json(Path(result["artifacts"]["run_status"]))
        self.assertEqual(persisted["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(persisted["provenance"]["type"], "blocked_semantic_planner_unavailable")
        self.assertEqual(persisted["planner_mode"], "blocked")
        self.assertFalse(persisted["semantic_release_eligible"])
        self.assertFalse(persisted["semantic_planning"]["validation_ok"])
        self.assertEqual(
            persisted["semantic_planning"]["review_verdict"],
            "release_ineligible",
        )
        self.assertIn("semantic_planning", persisted["diagnostics"])
        self.assertIn("semantic_plan", persisted["artifact_handoff"]["artifact_paths"])
        self.assertIn("semantic_planning", persisted["artifact_handoff"])
        self.assertNotIn("report_status", persisted["artifact_handoff"]["artifact_paths"])

    def test_full_runner_forwards_codex_exec_timeout_override(self) -> None:
        captured_kwargs: list[dict] = []

        def fake_parallel(*, run, **kwargs):
            captured_kwargs.append(dict(kwargs))
            return {
                "status": "blocked_parallel_execution",
                "ok": False,
                "adapter": "codex-exec",
                "parallel_degraded": False,
                "needs_serial_handoff": True,
                "planned_task_count": 1,
                "failure_counts": {},
                "diagnostics": {"actionable_cause": "fake blocked parallel status"},
                "evidence_source": {
                    "type": "blocked_parallel_execution",
                    "adapter": "codex-exec",
                },
                "merge": {"accepted_shards": []},
                "artifacts": {},
            }

        with mock.patch(
            "deepresearch.invocation_router.run_parallel_orchestration",
            side_effect=fake_parallel,
        ):
            result = run_skill_invocation(
                "$deep-research: investigate codex exec timeout forwarding",
                runs_dir=self.temp_runs_dir(),
                adapter_name="codex-exec",
                route="text_only",
                angles=["primary source discovery"],
                codex_exec_timeout_seconds=900,
                min_tasks=1,
                max_tasks=1,
            )

        self.assertEqual(result["status"], "blocked_parallel_execution")
        self.assertEqual(captured_kwargs[0]["codex_exec_timeout_seconds"], 900)

    def test_release_validation_identity_is_written_before_parallel_orchestration(self) -> None:
        runs_dir = self.temp_runs_dir()
        question = "Investigate release validation identity handoff."
        expected_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()
        inspected = False

        def fake_parallel(*, run, **_kwargs):
            nonlocal inspected
            inspected = True
            run_dir = Path(run)
            run_status = self.read_json(run_dir / "run_status.json")
            evidence = self.read_json(run_dir / "evidence.json")
            for payload in (run_status, evidence):
                self.assertEqual(payload["run_id"], run_dir.name)
                self.assertEqual(payload["prompt_id"], "pb-text-001")
                self.assertEqual(payload["suite_id"], "issue-118-suite")
                self.assertEqual(payload["prompt_hash"], expected_hash)
                self.assertEqual(payload["original_question"], question)
                self.assertEqual(payload["execution_mode"], "codex-plugin")
                self.assertEqual(payload["runner_mode"], "full-runner")
            self.assertEqual(run_status["selected_mode"], "full-runner")
            self.assertIn("updated_at", run_status)
            self.assertIn("created_at", evidence)
            return {
                "status": "blocked_parallel_execution",
                "ok": False,
                "adapter": "codex-exec",
                "parallel_degraded": False,
                "needs_serial_handoff": True,
                "planned_task_count": 1,
                "failure_counts": {},
                "diagnostics": {"actionable_cause": "fake blocked parallel status"},
                "evidence_source": {
                    "type": "blocked_parallel_execution",
                    "adapter": "codex-exec",
                },
                "merge": {"accepted_shards": []},
                "artifacts": {},
            }

        with mock.patch(
            "deepresearch.invocation_router.run_parallel_orchestration",
            side_effect=fake_parallel,
        ):
            result = run_skill_invocation(
                f"$deep-research: {question}",
                runs_dir=runs_dir,
                adapter_name="codex-exec",
                route="text_only",
                angles=["primary source discovery"],
                budget_preset="quick",
                min_tasks=1,
                max_tasks=1,
                prompt_id="pb-text-001",
                suite_id="issue-118-suite",
            )

        self.assertTrue(inspected)
        self.assertEqual(result["status"], "blocked_parallel_execution")
        self.assertEqual(result["prompt_id"], "pb-text-001")
        self.assertEqual(result["suite_id"], "issue-118-suite")
        self.assertEqual(result["prompt_hash"], expected_hash)
        self.assertEqual(result["execution_mode"], "codex-plugin")
        self.assertEqual(result["runner_mode"], "full-runner")

    def test_quick_chat_is_explicit_and_declares_no_evidence_bundle(self) -> None:
        result = run_skill_invocation(
            "$deep-research: quick answer about cache eviction policies",
            runs_dir=self.temp_runs_dir(),
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["selected_mode"], "quick-chat")
        self.assertEqual(result["status"], "quick_chat_only")
        self.assertTrue(result["no_evidence_bundle"])
        self.assertIn("no DeepResearch evidence bundle was produced", result["response_notice"])
        self.assertEqual(result["artifacts"], {})

    def test_no_full_pipeline_with_quick_answer_routes_quick_chat(self) -> None:
        result = run_skill_invocation(
            "$deep-research: do not run the full pipeline; give me a quick answer about cache eviction",
            runs_dir=self.temp_runs_dir(),
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["selected_mode"], "quick-chat")
        self.assertEqual(result["status"], "quick_chat_only")
        self.assertTrue(result["no_evidence_bundle"])
        self.assertEqual(result["artifacts"], {})

    def test_negated_quick_answer_with_full_pipeline_intent_blocks_without_semantic_planner(self) -> None:
        result = run_skill_invocation(
            "$deep-research: do not give me a quick answer about cache eviction; run the full pipeline",
            runs_dir=self.temp_runs_dir(),
            adapter_name="fixture",
            route="text_only",
            budget_preset="quick",
            min_tasks=1,
            max_tasks=1,
        )

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["terminal"])
        self.assertEqual(result["selected_mode"], "blocked")
        self.assertEqual(result["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(result["planner_mode"], "blocked")
        self.assertIn("run_status", result["artifacts"])
        self.assertIn("evidence", result["artifacts"])
        self.assertIn("semantic_planner_validation", result["artifacts"])
        self.assertNotIn("parallel_orchestration_status", result["artifacts"])

    def test_quick_chat_flag_overrides_negated_text_marker(self) -> None:
        result = run_skill_invocation(
            "$deep-research: do not give me a quick answer about cache eviction; run the full pipeline",
            runs_dir=self.temp_runs_dir(),
            quick_chat=True,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["selected_mode"], "quick-chat")
        self.assertEqual(result["status"], "quick_chat_only")
        self.assertTrue(result["no_evidence_bundle"])

    def test_blocked_preflight_writes_terminal_run_status(self) -> None:
        with mock.patch("deepresearch.invocation_router.shutil.which", return_value=None):
            result = run_skill_invocation(
                "$deep-research: requires codex child execution",
                runs_dir=self.temp_runs_dir(),
                require_codex_exec=True,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["selected_mode"], "blocked")
        self.assertEqual(result["status"], "blocked_preflight")
        self.assertEqual(
            result["diagnostics"]["actionable_cause"],
            "codex exec is not available on PATH",
        )
        run_status = Path(result["artifacts"]["run_status"])
        self.assertTrue(run_status.is_file())
        persisted = self.read_json(run_status)
        self.assertFalse(persisted["ok"])
        self.assertTrue(persisted["terminal"])
        self.assertEqual(persisted["diagnostics"]["actionable_cause"], result["diagnostics"]["actionable_cause"])
        self.assertNotIn("report_status", result["artifacts"])
        self.assertEqual(result["artifact_handoff"]["status"], "blocked_preflight")
        self.assertEqual(
            result["artifact_handoff"]["diagnostics"]["actionable_cause"],
            "codex exec is not available on PATH",
        )

    def test_manual_handoff_provenance_is_explicit_in_run_status(self) -> None:
        result = run_skill_invocation(
            "$deep-research: use this supplied source",
            runs_dir=self.temp_runs_dir(),
            manual_handoff=True,
            urls=["https://example.com/manual-source"],
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["selected_mode"], "manual-handoff")
        self.assertEqual(result["status"], "manual_sources_ingested")
        self.assertEqual(result["provenance"]["type"], "manual_handoff")
        self.assertTrue(result["provenance"]["manual_handoff"])
        self.assertFalse(result["provenance"]["real_use_e2e_eligible"])
        self.assertIn("run_status", result["artifacts"])
        self.assertIn("manual_ingest_status", result["artifacts"])
        self.assertTrue(result["manual_handoff"]["ok"])

    def test_visual_required_without_angles_blocks_before_visual_provider_preflight(self) -> None:
        with (
            mock.patch("deepresearch.invocation_router.shutil.which", return_value=None),
            mock.patch("deepresearch.invocation_router.run_parallel_orchestration") as parallel_mock,
        ):
            result = run_skill_invocation(
                "$deep-research: inspect product screenshots for evidence",
                runs_dir=self.temp_runs_dir(),
                route="visual_required",
                budget_preset="standard",
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(result["selected_mode"], "blocked")
        self.assertEqual(result["planner_mode"], "blocked")
        self.assertIn("actionable_cause", result["diagnostics"])
        self.assertIn("True semantic decomposition did not run", result["diagnostics"]["actionable_cause"])
        self.assertIn("run_status", result["artifacts"])
        self.assertNotIn("visual_provider_status", result["artifacts"])
        self.assertNotIn("search_tasks", result["artifacts"])
        self.assertNotIn("visual_tasks", result["artifacts"])
        self.assertNotIn("parallel_orchestration_status", result["artifacts"])
        parallel_mock.assert_not_called()

        run_status = self.read_json(Path(result["artifacts"]["run_status"]))
        self.assertFalse(run_status["ok"])
        self.assertTrue(run_status["terminal"])
        self.assertEqual(run_status["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(
            run_status["diagnostics"]["actionable_cause"],
            result["diagnostics"]["actionable_cause"],
        )

        trace = self.read_jsonl(Path(result["artifacts"]["run_trace"]))
        self.assertEqual(trace[-1]["event_type"], "semantic_planner_blocked")
        self.assertEqual(trace[-1]["status"], "blocked_semantic_planner_unavailable")

    def test_visual_required_with_codex_worker_available_reaches_parallel_handoff(self) -> None:
        runs_dir = self.temp_runs_dir()
        parallel_called = False

        def fake_parallel(*, run, **_kwargs):
            nonlocal parallel_called
            parallel_called = True
            run_dir = Path(run)
            payload = {
                "status": "blocked_parallel_execution",
                "ok": False,
                "adapter": "codex-exec",
                "parallel_degraded": False,
                "needs_serial_handoff": False,
                "planned_task_count": 1,
                "failure_counts": {"blocked_tasks": 1},
                "diagnostics": {
                    "actionable_cause": (
                        "Codex visual worker handoff was attempted, but no image artifacts "
                        "were available for analysis"
                    )
                },
                "evidence_source": {
                    "type": "real_child_execution",
                    "adapter": "codex-exec",
                    "accepted_shards": 0,
                    "fixture_only": False,
                    "manual_handoff": False,
                    "attempted_real_child_execution": True,
                    "real_child_execution": False,
                    "real_use_e2e_eligible": False,
                },
                "merge": {"accepted_shards": []},
                "artifacts": {
                    "parallel_orchestration_status": str(run_dir / "parallel_orchestration_status.json")
                },
            }
            (run_dir / "parallel_orchestration_status.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return payload

        with (
            mock.patch("deepresearch.invocation_router.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.invocation_router.run_parallel_orchestration", side_effect=fake_parallel),
        ):
            result = run_skill_invocation(
                "$deep-research: inspect product screenshots for evidence",
                runs_dir=runs_dir,
                route="visual_required",
                angles=["primary source discovery"],
                budget_preset="quick",
                min_tasks=1,
                max_tasks=1,
            )

        self.assertTrue(parallel_called)
        self.assertFalse(result["ok"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["status"], "blocked_parallel_execution")
        self.assertIn("no image artifacts", result["diagnostics"]["actionable_cause"])
        self.assertIn("visual_provider_status", result["artifacts"])

        visual_provider_status = self.read_json(Path(result["artifacts"]["visual_provider_status"]))
        self.assertTrue(visual_provider_status["ok"])
        self.assertFalse(visual_provider_status["terminal"])
        self.assertEqual(visual_provider_status["status"], "codex_native_visual_worker_available")
        provider = visual_provider_status["providers"][0]
        self.assertEqual(provider["provider"], "codex-interactive")
        self.assertEqual(provider["provider_kind"], "vlm")
        self.assertTrue(provider["configured"])
        self.assertTrue(provider["available"])
        self.assertEqual(provider["adapter"], "codex-exec")
        self.assertTrue(provider["codex_native_handoff"])
        self.assertTrue(provider["codex_interactive_handoff"])
        self.assertFalse(provider["hidden_codex_api_call"])

        trace = self.read_jsonl(Path(result["artifacts"]["run_trace"]))
        preflight_events = [
            record for record in trace if record["event_type"] == "visual_provider_preflight"
        ]
        self.assertEqual(len(preflight_events), 1)
        self.assertEqual(preflight_events[0]["status"], "codex_native_visual_worker_available")
        self.assertEqual(preflight_events[0]["provider"], "codex-interactive")
        self.assertEqual(preflight_events[0]["adapter"], "codex-exec")

    def test_visual_required_codex_full_runner_runs_acquisition_before_ingest_and_synthesis(self) -> None:
        runs_dir = self.temp_runs_dir()
        call_order: list[str] = []

        class PassingVisualValidation:
            valid = True

            def to_dict(self) -> dict:
                return {"valid": True, "errors": []}

        def fake_parallel(*, run, **_kwargs):
            call_order.append("parallel")
            run_dir = Path(run)
            evidence = self.read_json(run_dir / "evidence.json")
            evidence["sources"] = [
                {
                    "id": "src_auto_visual",
                    "type": "web",
                    "url": "https://example.com/public-product",
                    "title": "Public product page",
                    "published_at": None,
                    "accessed_at": "2026-06-26T00:00:00Z",
                    "quality": "primary",
                    "retrieval_status": "fetched",
                    "local_artifact_path": "sources/src_auto_visual.html",
                    "license_policy": "allowed",
                    "robots_policy": "allowed",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "route": "visual_required",
                    "angle_id": "angle_001",
                }
            ]
            self.write_json(run_dir / "evidence.json", evidence)
            payload = {
                "status": "completed_parallel",
                "ok": True,
                "adapter": "codex-exec",
                "parallel_degraded": False,
                "needs_serial_handoff": False,
                "planned_task_count": 1,
                "failure_counts": {},
                "evidence_source": {
                    "type": "real_child_execution",
                    "adapter": "codex-exec",
                    "accepted_shards": 1,
                    "fixture_only": False,
                    "manual_handoff": False,
                    "attempted_real_child_execution": True,
                    "real_child_execution": True,
                    "real_use_e2e_eligible": True,
                },
                "merge": {"accepted_shards": [{"task_id": "task_research_001"}]},
                "artifacts": {
                    "parallel_orchestration_status": str(run_dir / "parallel_orchestration_status.json")
                },
            }
            self.write_json(run_dir / "parallel_orchestration_status.json", payload)
            return payload

        def fake_acquire(*, run, providers, **_kwargs):
            call_order.append("acquire")
            self.assertEqual(
                tuple(providers),
                (
                    "child-discovered-image-url",
                    "brave-image-search",
                    "page-image-extractor",
                    "browser-screenshot",
                ),
            )
            run_dir = Path(run)
            self.write_json(
                run_dir / "visual_search_plan.json",
                {
                    "schema_version": "codex-deepresearch.visual-artifacts.v0",
                    "run_id": run_dir.name,
                    "created_at": "2026-06-26T00:00:00Z",
                    "tasks": [],
                },
            )
            self.write_jsonl(
                run_dir / "visual_candidates.jsonl",
                [
                    {
                        "candidate_id": f"cand_auto_visual_{index:03d}",
                        "task_id": "task_visual_001",
                        "angle_id": "angle_001",
                        "provider": "browser-screenshot",
                        "provider_kind": "screenshot",
                        "provider_mode": "real",
                        "candidate_status": "fetched",
                    }
                    for index in range(1, 11)
                ],
            )
            self.write_jsonl(
                run_dir / "image_fetch_status.jsonl",
                [
                    {
                        "fetch_id": f"fetch_auto_visual_{index:03d}",
                        "candidate_id": f"cand_auto_visual_{index:03d}",
                        "task_id": "task_visual_001",
                        "angle_id": "angle_001",
                        "provider": "browser-screenshot",
                        "provider_kind": "screenshot",
                        "provider_mode": "real",
                        "fetch_status": "fetched",
                        "local_artifact_path": f"screenshots/src_auto_visual_{index:03d}.png",
                        "evidence_image_id": f"img_auto_visual_{index:03d}",
                        "hash": f"sha256:auto{index}",
                    }
                    for index in range(1, 4)
                ],
            )
            self.write_jsonl(run_dir / "visual_observations.jsonl", [])
            provider_status = {
                "schema_version": "codex-deepresearch.visual-provider-status.v0",
                "run_id": run_dir.name,
                "run_dir": str(run_dir),
                "status": "real_image_search_candidates_collected",
                "ok": True,
                "terminal": False,
                "created_at": "2026-06-26T00:00:00Z",
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
                        "candidates_discovered": 10,
                        "artifacts_fetched": 3,
                        "vlm_images_analyzed": 0,
                    }
                ],
                "diagnostics": {"actionable_cause": "captured public screenshot"},
                "artifacts": {
                    "visual_candidates": "visual_candidates.jsonl",
                    "image_fetch_status": "image_fetch_status.jsonl",
                    "visual_observations": "visual_observations.jsonl",
                    "visual_provider_status": "visual_provider_status.json",
                },
            }
            self.write_json(run_dir / "visual_provider_status.json", provider_status)
            self.write_json(
                run_dir / "visual_acquisition_status.json",
                {
                    "status": "real_image_search_candidates_collected",
                    "ok": True,
                    "artifacts": {
                        "visual_provider_status": str(run_dir / "visual_provider_status.json")
                    },
                },
            )
            return {"status": "real_image_search_candidates_collected", "ok": True}

        def fake_ingest(*, run, provider, provider_mode, **_kwargs):
            call_order.append("ingest_vision")
            self.assertEqual(provider, "codex-interactive")
            self.assertEqual(provider_mode, "real")
            run_dir = Path(run)
            self.assertTrue((run_dir / "image_fetch_status.jsonl").is_file())
            self.write_jsonl(
                run_dir / "visual_observations.jsonl",
                [
                    {
                        "id": f"img_auto_visual_{index:03d}",
                        "evidence_image_id": f"img_auto_visual_{index:03d}",
                        "candidate_id": f"cand_auto_visual_{index:03d}",
                        "fetch_id": f"fetch_auto_visual_{index:03d}",
                        "task_id": "task_visual_001",
                        "angle_id": "angle_001",
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "observation_status": "analyzed",
                    }
                    for index in range(1, 4)
                ],
            )
            evidence = self.read_json(run_dir / "evidence.json")
            evidence["images"] = [
                {
                    "id": f"img_auto_visual_{index:03d}",
                    "source_id": "src_auto_visual",
                    "origin": "screenshot",
                    "page_url": "https://example.com/public-product",
                    "local_artifact_path": f"screenshots/src_auto_visual_{index:03d}.png",
                    "mime_type": "image/png",
                    "observations": [f"The screenshot {index} shows the public product UI."],
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "analyzed",
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "candidate_id": f"cand_auto_visual_{index:03d}",
                    "fetch_id": f"fetch_auto_visual_{index:03d}",
                    "task_id": "task_visual_001",
                    "angle_id": "angle_001",
                    "policy_flags": [],
                    "policy_decision": "allowed",
                }
                for index in range(1, 4)
            ]
            evidence["claims"] = [
                {
                    "id": "claim_auto_visual_001",
                    "text": "The public product UI is visible in the captured screenshot.",
                    "claim_type": "mixed",
                    "supporting_sources": ["src_auto_visual"],
                    "supporting_images": [
                        "img_auto_visual_001",
                        "img_auto_visual_002",
                        "img_auto_visual_003",
                    ],
                    "visual_supports": [
                        {
                            "image_id": "img_auto_visual_001",
                            "observation_ref": "images.img_auto_visual_001.observations[0]",
                            "observation_index": 0,
                            "observation_text": "The screenshot 1 shows the public product UI.",
                            "relation_type": "screenshot_support",
                            "provider": "codex-interactive",
                            "confidence": 0.8,
                        },
                        {
                            "image_id": "img_auto_visual_002",
                            "observation_ref": "images.img_auto_visual_002.observations[0]",
                            "observation_index": 0,
                            "observation_text": "The screenshot 2 shows the public product UI.",
                            "relation_type": "screenshot_support",
                            "provider": "codex-interactive",
                            "confidence": 0.8,
                        },
                        {
                            "image_id": "img_auto_visual_003",
                            "observation_ref": "images.img_auto_visual_003.observations[0]",
                            "observation_index": 0,
                            "observation_text": "The screenshot 3 shows the public product UI.",
                            "relation_type": "screenshot_support",
                            "provider": "codex-interactive",
                            "confidence": 0.8,
                        },
                    ],
                    "quote_spans": [],
                    "votes": [],
                    "verification_status": "supported",
                    "review_status": "human_accepted",
                    "promotion_status": "not_eligible",
                    "confidence": "high",
                    "caveats": [],
                }
            ]
            self.write_json(run_dir / "evidence.json", evidence)
            provider_status = self.read_json(run_dir / "visual_provider_status.json")
            provider_status["status"] = "codex_interactive_visual_worker_analyzed"
            provider_status["providers"].append(
                {
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "configured": True,
                    "available": True,
                    "blocked_reason": None,
                    "invocations": 1,
                    "candidates_discovered": 0,
                    "artifacts_fetched": 3,
                    "vlm_images_analyzed": 3,
                    "external_vlm_call": False,
                    "hidden_codex_api_call": False,
                    "codex_native_handoff": True,
                }
            )
            self.write_json(run_dir / "visual_provider_status.json", provider_status)
            self.write_json(run_dir / "vision_ingest_status.json", {"status": "visual_evidence_ingested", "ok": True})
            return {"status": "visual_evidence_ingested", "ok": True}

        def fake_guardrails(*, run):
            call_order.append("guardrails")
            run_dir = Path(run)
            self.write_json(run_dir / "guardrails_status.json", {"status": "completed", "ok": True})
            return {"status": "completed", "ok": True}

        def fake_verify(*, run):
            call_order.append("verify")
            run_dir = Path(run)
            self.write_json(run_dir / "verification_matrix_status.json", {"status": "completed", "ok": True})
            return {"status": "completed", "ok": True}

        def fake_synthesize(*, run):
            call_order.append("synthesize")
            run_dir = Path(run)
            (run_dir / "report.md").write_text(
                "Report cites claim_auto_visual_001 with img_auto_visual_001, "
                "img_auto_visual_002, and img_auto_visual_003.\n",
                encoding="utf-8",
            )
            status = {
                "status": "completed",
                "ok": True,
                "used_images": [
                    "img_auto_visual_001",
                    "img_auto_visual_002",
                    "img_auto_visual_003",
                ],
                "included_claims": [
                    {
                        "claim_id": "claim_auto_visual_001",
                        "claim_type": "mixed",
                        "verification_status": "supported",
                        "image_ids": [
                            "img_auto_visual_001",
                            "img_auto_visual_002",
                            "img_auto_visual_003",
                        ],
                        "visual_supports": [
                            {
                                "image_id": f"img_auto_visual_{index:03d}",
                                "evidence_image_id": f"img_auto_visual_{index:03d}",
                                "plan_id": "plan_task_visual_001_angle_001_visual_required",
                                "task_id": "task_visual_001",
                                "angle_id": "angle_001",
                                "route": "visual_required",
                                "candidate_id": f"cand_auto_visual_{index:03d}",
                                "fetch_id": f"fetch_auto_visual_{index:03d}",
                            }
                            for index in range(1, 4)
                        ],
                    }
                ],
            }
            self.write_json(run_dir / "report_status.json", status)
            return status

        def fake_validate(*, run_dir, **_kwargs):
            call_order.append("validate_visual")
            provider_status = self.read_json(Path(run_dir) / "visual_provider_status.json")
            self.assertEqual(provider_status["status"], "completed_auto_visual")
            return PassingVisualValidation()

        with (
            mock.patch("deepresearch.invocation_router.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.invocation_router.run_parallel_orchestration", side_effect=fake_parallel),
            mock.patch("deepresearch.invocation_router.acquire_visual_candidates", side_effect=fake_acquire),
            mock.patch("deepresearch.invocation_router.ingest_vision_observations", side_effect=fake_ingest),
            mock.patch("deepresearch.invocation_router.enforce_guardrails", side_effect=fake_guardrails),
            mock.patch("deepresearch.invocation_router.verify_claims", side_effect=fake_verify),
            mock.patch("deepresearch.invocation_router.synthesize_report", side_effect=fake_synthesize),
            mock.patch("deepresearch.invocation_router.validate_visual_artifacts", side_effect=fake_validate),
        ):
            result = run_skill_invocation(
                "$deep-research: inspect public product screenshots for visual evidence",
                runs_dir=runs_dir,
                route="visual_required",
                angles=["primary source discovery"],
                budget_preset="quick",
                min_tasks=1,
                max_tasks=1,
            )

        self.assertEqual(
            call_order,
            [
                "parallel",
                "acquire",
                "ingest_vision",
                "guardrails",
                "verify",
                "synthesize",
                "validate_visual",
            ],
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed_auto_visual")
        self.assertIn("visual_search_plan", result["artifacts"])
        self.assertIn("visual_candidates", result["artifacts"])
        self.assertIn("image_fetch_status", result["artifacts"])
        self.assertIn("visual_provider_status", result["artifacts"])
        self.assertEqual(result["visual_summary"]["status"], "completed_auto_visual")
        self.assertEqual(result["visual_summary"]["candidate_count"], 10)
        self.assertEqual(result["visual_summary"]["fetched_artifact_count"], 3)
        self.assertEqual(result["visual_summary"]["vlm_analyzed_image_count"], 3)
        self.assertEqual(
            result["visual_summary"]["used_images"],
            [
                "img_auto_visual_001",
                "img_auto_visual_002",
                "img_auto_visual_003",
            ],
        )

        visual_provider_status = self.read_json(Path(result["artifacts"]["visual_provider_status"]))
        self.assertEqual(visual_provider_status["status"], "completed_auto_visual")
        self.assertTrue(visual_provider_status["ok"])
        self.assertTrue(visual_provider_status["terminal"])
        self.assertEqual(visual_provider_status["metric_classification"], "success")

    def test_completed_auto_visual_finalization_requires_ten_real_candidates(self) -> None:
        class PassingVisualValidation:
            valid = True

            def to_dict(self) -> dict:
                return {"valid": True, "errors": []}

        run_dir = self.temp_runs_dir() / "run_quant_gate"
        run_dir.mkdir()
        self.write_json(
            run_dir / "evidence.json",
            {
                "run_id": run_dir.name,
                "routing": [
                    {"id": "angle_001", "modality": "visual_required", "max_images": 12}
                ],
                "images": [
                    {
                        "id": f"img_auto_visual_{index:03d}",
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "observations": ["The image supports the visual claim."],
                        "policy_decision": "allowed",
                    }
                    for index in range(1, 4)
                ],
                "claims": [
                    {
                        "id": "claim_visual_gate",
                        "text": "The automatically acquired image supports the report.",
                        "claim_type": "mixed",
                        "supporting_images": ["img_auto_visual_001"],
                        "verification_status": "supported",
                    }
                ],
            },
        )
        self.write_jsonl(
            run_dir / "visual_candidates.jsonl",
            [
                {
                    "candidate_id": f"cand_auto_visual_{index:03d}",
                    "provider": "page-image-extractor",
                    "provider_kind": "page_extractor",
                    "provider_mode": "real",
                }
                for index in range(1, 10)
            ],
        )
        self.write_jsonl(
            run_dir / "image_fetch_status.jsonl",
            [
                {
                    "fetch_id": f"fetch_auto_visual_{index:03d}",
                    "candidate_id": f"cand_auto_visual_{index:03d}",
                    "provider": "page-image-extractor",
                    "provider_kind": "page_extractor",
                    "provider_mode": "real",
                    "fetch_status": "fetched",
                    "evidence_image_id": f"img_auto_visual_{index:03d}",
                }
                for index in range(1, 4)
            ],
        )
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "evidence_image_id": f"img_auto_visual_{index:03d}",
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "observation_status": "analyzed",
                }
                for index in range(1, 4)
            ],
        )
        self.write_json(
            run_dir / "report_status.json",
            {"status": "completed", "used_images": ["img_auto_visual_001"]},
        )
        self.write_json(
            run_dir / "visual_provider_status.json",
            {
                "status": "codex_interactive_visual_worker_analyzed",
                "providers": [
                    {
                        "provider": "page-image-extractor",
                        "provider_kind": "page_extractor",
                        "provider_mode": "real",
                        "invocations": 1,
                        "candidates_discovered": 9,
                        "artifacts_fetched": 3,
                        "vlm_images_analyzed": 0,
                    },
                    {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "invocations": 1,
                        "candidates_discovered": 0,
                        "artifacts_fetched": 3,
                        "vlm_images_analyzed": 3,
                    },
                ],
            },
        )

        with mock.patch(
            "deepresearch.invocation_router.validate_visual_artifacts",
            return_value=PassingVisualValidation(),
        ):
            result = invocation_router._finalize_automatic_visual_completion(
                run_dir=run_dir,
                visual_stage_status={},
            )

        self.assertEqual(result["status"], "partial_auto_visual")
        self.assertFalse(result["visual_release_gate"]["valid"])
        self.assertIn(
            "at_least_10_real_image_centric_candidates",
            result["visual_release_gate"]["failures"],
        )
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        self.assertEqual(provider_status["status"], "partial_auto_visual")

    def test_visual_lineage_failure_wins_over_satisfied_minimums(self) -> None:
        runs_dir = self.temp_runs_dir()

        def fake_parallel(*, run, **_kwargs):
            run_dir = Path(run)
            payload = {
                "status": "completed_parallel",
                "ok": True,
                "adapter": "codex-exec",
                "parallel_degraded": False,
                "needs_serial_handoff": False,
                "planned_task_count": 3,
                "failure_counts": {},
                "evidence_source": {
                    "type": "real_child_execution",
                    "adapter": "codex-exec",
                    "accepted_shards": 3,
                    "fixture_only": False,
                    "manual_handoff": False,
                    "attempted_real_child_execution": True,
                    "real_child_execution": True,
                    "real_use_e2e_eligible": True,
                },
                "merge": {"accepted_shards": [{"task_id": "task_visual_001"}]},
                "artifacts": {
                    "parallel_orchestration_status": str(run_dir / "parallel_orchestration_status.json")
                },
            }
            self.write_json(run_dir / "parallel_orchestration_status.json", payload)
            return payload

        def fake_acquire(*, run, **_kwargs):
            self.write_visual_lineage_fixture(Path(run), duplicate_plan_id=True)
            return {"status": "real_image_search_candidates_collected", "ok": True}

        def fake_ingest(*, run, **_kwargs):
            run_dir = Path(run)
            self.write_json(
                run_dir / "vision_ingest_status.json",
                {"status": "visual_evidence_ingested", "ok": True},
            )
            return {"status": "visual_evidence_ingested", "ok": True}

        def fake_synthesize(*, run):
            run_dir = Path(run)
            evidence = self.read_json(run_dir / "evidence.json")
            image_id = evidence["images"][0]["id"]
            report_status = self.report_status_payload(image_id)
            self.write_json(run_dir / "report_status.json", report_status)
            (run_dir / "report.md").write_text(
                f"Report cites claim_lineage_visual with image {image_id}.\n",
                encoding="utf-8",
            )
            return report_status

        with (
            mock.patch("deepresearch.invocation_router.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.invocation_router.run_parallel_orchestration", side_effect=fake_parallel),
            mock.patch("deepresearch.invocation_router.acquire_visual_candidates", side_effect=fake_acquire),
            mock.patch("deepresearch.invocation_router.ingest_vision_observations", side_effect=fake_ingest),
            mock.patch("deepresearch.invocation_router.enforce_guardrails", return_value={"status": "completed", "ok": True}),
            mock.patch("deepresearch.invocation_router.verify_claims", return_value={"status": "completed", "ok": True}),
            mock.patch("deepresearch.invocation_router.synthesize_report", side_effect=fake_synthesize),
        ):
            result = run_skill_invocation(
                "$deep-research: inspect public images for lineage",
                runs_dir=runs_dir,
                route="visual_required",
                angles=["primary source discovery"],
                budget_preset="quick",
                min_tasks=3,
                max_tasks=3,
            )

        self.assertEqual(result["status"], "partial_auto_visual")
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["diagnostics"]["failure_code"],
            "visual_artifact_lineage_invalid",
        )
        self.assertNotEqual(
            result["diagnostics"].get("failure_code"),
            "visual_minimum_shortfall",
        )
        self.assertNotEqual(
            result["diagnostics"].get("failure_code"),
            "visual_report_linkage_missing",
        )

        run_status = self.read_json(Path(result["artifacts"]["run_status"]))
        self.assertFalse(run_status["ok"])
        self.assertEqual(run_status["diagnostics"]["failure_code"], "visual_artifact_lineage_invalid")
        self.assertTrue(run_status["visual_release_gate"]["valid"])
        self.assertFalse(run_status["visual_artifact_validation"]["valid"])

        provider_status = self.read_json(Path(result["artifacts"]["visual_provider_status"]))
        self.assertEqual(provider_status["status"], "partial_auto_visual")
        self.assertEqual(
            provider_status["diagnostics"]["failure_code"],
            "visual_artifact_lineage_invalid",
        )
        self.assertTrue(provider_status["minimums"]["satisfied"])
        self.assertEqual(provider_status["minimums"]["shortfall_reason"], "none")

    def test_visual_release_gate_requires_included_claim_lineage(self) -> None:
        run_dir = self.temp_runs_dir() / "visual-release-missing-included-claims"
        run_dir.mkdir()
        self.write_visual_lineage_fixture(run_dir)
        image_id = self.read_json(run_dir / "evidence.json")["images"][0]["id"]
        self.write_json(
            run_dir / "report_status.json",
            {
                "status": "completed",
                "used_images": [image_id],
                "included_claims": [],
            },
        )
        (run_dir / "report.md").write_text(
            f"Report cites claim_lineage_visual with image {image_id}.\n",
            encoding="utf-8",
        )

        result = invocation_router._finalize_automatic_visual_completion(
            run_dir=run_dir,
            visual_stage_status={},
        )

        self.assertEqual(result["status"], "partial_auto_visual")
        self.assertFalse(result["visual_release_gate"]["valid"])
        self.assertEqual(result["visual_release_gate"]["report_cited_images"], 0)
        self.assertEqual(
            result["diagnostics"]["failure_code"],
            "visual_report_linkage_missing",
        )
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        self.assertEqual(provider_status["status"], "partial_auto_visual")
        self.assertEqual(provider_status["minimums"]["report_cited_images"], 0)

    def test_visual_release_gate_requires_report_markdown_image_citation(self) -> None:
        run_dir = self.temp_runs_dir() / "visual-release-missing-report-citation"
        run_dir.mkdir()
        self.write_visual_lineage_fixture(run_dir)
        image_id = self.read_json(run_dir / "evidence.json")["images"][0]["id"]
        self.write_json(run_dir / "report_status.json", self.report_status_payload(image_id))
        (run_dir / "report.md").write_text(
            "Report text intentionally omits the visual evidence identifiers.\n",
            encoding="utf-8",
        )

        result = invocation_router._finalize_automatic_visual_completion(
            run_dir=run_dir,
            visual_stage_status={},
        )

        self.assertEqual(result["status"], "partial_auto_visual")
        self.assertFalse(result["visual_release_gate"]["valid"])
        self.assertEqual(result["visual_release_gate"]["report_cited_images"], 0)
        self.assertEqual(
            result["diagnostics"]["failure_code"],
            "visual_report_linkage_missing",
        )
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        self.assertEqual(provider_status["status"], "partial_auto_visual")
        self.assertEqual(provider_status["minimums"]["report_cited_images"], 0)

    def test_partial_auto_visual_final_run_status_exposes_visual_shortfall_diagnostics(self) -> None:
        runs_dir = self.temp_runs_dir()

        class PassingVisualValidation:
            valid = True

            def to_dict(self) -> dict:
                return {"valid": True, "errors": []}

        def fake_parallel(*, run, **_kwargs):
            run_dir = Path(run)
            evidence = self.read_json(run_dir / "evidence.json")
            evidence["routing"] = [
                {"id": "angle_001", "modality": "visual_required", "max_images": 12}
            ]
            evidence["claims"] = [
                {
                    "id": "claim_visual_shortfall",
                    "text": "The cited public image supports the visual finding.",
                    "claim_type": "visual",
                    "supporting_images": ["img_shortfall_001"],
                    "verification_status": "supported",
                }
            ]
            self.write_json(run_dir / "evidence.json", evidence)
            return {
                "status": "completed_parallel",
                "ok": True,
                "adapter": "codex-exec",
                "parallel_degraded": False,
                "needs_serial_handoff": False,
                "planned_task_count": 1,
                "failure_counts": {},
                "evidence_source": {"type": "real_child_execution", "adapter": "codex-exec"},
                "merge": {"accepted_shards": [{"task_id": "task_research_001"}]},
                "artifacts": {},
            }

        def fake_acquire(*, run, **_kwargs):
            run_dir = Path(run)
            self.write_json(run_dir / "visual_search_plan.json", {"tasks": []})
            self.write_jsonl(
                run_dir / "visual_candidates.jsonl",
                [
                    {
                        "candidate_id": f"cand_shortfall_{index:03d}",
                        "provider": "page-image-extractor",
                        "provider_kind": "page_extractor",
                        "provider_mode": "real",
                        "candidate_status": "fetched",
                    }
                    for index in range(1, 4)
                ],
            )
            self.write_jsonl(
                run_dir / "image_fetch_status.jsonl",
                [
                    {
                        "candidate_id": f"cand_shortfall_{index:03d}",
                        "fetch_id": f"fetch_shortfall_{index:03d}",
                        "provider": "page-image-extractor",
                        "provider_kind": "page_extractor",
                        "provider_mode": "real",
                        "fetch_status": "fetched",
                        "local_artifact_path": f"images/img_shortfall_{index:03d}.png",
                        "evidence_image_id": f"img_shortfall_{index:03d}",
                    }
                    for index in range(1, 4)
                ],
            )
            self.write_jsonl(run_dir / "visual_observations.jsonl", [])
            self.write_json(
                run_dir / "visual_provider_status.json",
                {
                    "status": "real_image_search_candidates_collected",
                    "ok": True,
                    "terminal": False,
                    "providers": [
                        {
                            "provider": "page-image-extractor",
                            "provider_kind": "page_extractor",
                            "provider_mode": "real",
                            "invocations": 1,
                            "candidates_discovered": 3,
                            "artifacts_fetched": 3,
                            "vlm_images_analyzed": 0,
                        }
                    ],
                    "diagnostics": {"actionable_cause": "acquisition ready"},
                },
            )
            return {"status": "real_image_search_candidates_collected", "ok": True}

        def fake_ingest(*, run, **_kwargs):
            run_dir = Path(run)
            observations = [
                {
                    "candidate_id": f"cand_shortfall_{index:03d}",
                    "fetch_id": f"fetch_shortfall_{index:03d}",
                    "evidence_image_id": f"img_shortfall_{index:03d}",
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "observation_status": "analyzed",
                }
                for index in range(1, 3)
            ]
            self.write_jsonl(run_dir / "visual_observations.jsonl", observations)
            self.write_json(
                run_dir / "visual_provider_status.json",
                {
                    "status": "visual_evidence_ingested",
                    "ok": True,
                    "terminal": False,
                    "providers": [
                        {
                            "provider": "page-image-extractor",
                            "provider_kind": "page_extractor",
                            "provider_mode": "real",
                            "invocations": 1,
                            "candidates_discovered": 3,
                            "artifacts_fetched": 3,
                            "vlm_images_analyzed": 0,
                        },
                        {
                            "provider": "codex-interactive",
                            "provider_kind": "vlm",
                            "provider_mode": "real",
                            "invocations": 1,
                            "candidates_discovered": 0,
                            "artifacts_fetched": 3,
                            "vlm_images_analyzed": 2,
                        },
                    ],
                    "diagnostics": {"actionable_cause": "two images analyzed"},
                },
            )
            self.write_json(
                run_dir / "vision_ingest_status.json",
                {"status": "visual_evidence_ingested", "ok": True},
            )
            return {"status": "visual_evidence_ingested", "ok": True}

        def fake_synthesize(*, run):
            run_dir = Path(run)
            image_id = "img_shortfall_001"
            self.write_json(
                run_dir / "report_status.json",
                {
                    "status": "completed",
                    "used_images": [image_id],
                    "included_claims": [
                        {
                            "claim_id": "claim_visual_shortfall",
                            "claim_type": "visual",
                            "verification_status": "supported",
                            "image_ids": [image_id],
                            "visual_supports": [
                                {
                                    "image_id": image_id,
                                    "evidence_image_id": image_id,
                                    "plan_id": "plan_task_visual_001_angle_001_visual_required",
                                    "task_id": "task_visual_001",
                                    "angle_id": "angle_001",
                                    "route": "visual_required",
                                    "candidate_id": "cand_shortfall_001",
                                    "fetch_id": "fetch_shortfall_001",
                                }
                            ],
                        }
                    ],
                },
            )
            (run_dir / "report.md").write_text(
                f"Report cites claim_visual_shortfall with image {image_id}.\n",
                encoding="utf-8",
            )
            return {"status": "completed", "ok": True}

        with (
            mock.patch("deepresearch.invocation_router.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.invocation_router.run_parallel_orchestration", side_effect=fake_parallel),
            mock.patch("deepresearch.invocation_router.acquire_visual_candidates", side_effect=fake_acquire),
            mock.patch("deepresearch.invocation_router.ingest_vision_observations", side_effect=fake_ingest),
            mock.patch("deepresearch.invocation_router.enforce_guardrails", return_value={"status": "completed", "ok": True}),
            mock.patch("deepresearch.invocation_router.verify_claims", return_value={"status": "completed", "ok": True}),
            mock.patch("deepresearch.invocation_router.synthesize_report", side_effect=fake_synthesize),
            mock.patch(
                "deepresearch.invocation_router.validate_visual_artifacts",
                return_value=PassingVisualValidation(),
            ),
        ):
            result = run_skill_invocation(
                "$deep-research: inspect public product screenshots for visual evidence",
                runs_dir=runs_dir,
                route="visual_required",
                angles=["primary source discovery"],
                budget_preset="quick",
                min_tasks=1,
                max_tasks=1,
            )

        self.assertEqual(result["status"], "partial_auto_visual")
        self.assertFalse(result["ok"])
        run_status = self.read_json(Path(result["artifacts"]["run_status"]))
        release_gate = run_status["visual_summary"]["release_gate"]
        self.assertFalse(release_gate["valid"])
        self.assertEqual(release_gate["required_vlm_images"], 3)
        self.assertEqual(release_gate["candidate_count"], 3)
        self.assertEqual(release_gate["selected_candidates"], 3)
        self.assertEqual(release_gate["fetched_artifacts"], 3)
        self.assertEqual(release_gate["vlm_images_analyzed"], 2)
        self.assertEqual(release_gate["report_cited_images"], 1)
        self.assertFalse(release_gate["minimums"]["satisfied"])
        self.assertEqual(release_gate["shortfall_reason"], "vlm_failures")
        self.assertEqual(
            release_gate["diagnostics"]["failure_code"],
            "visual_minimum_shortfall",
        )
        self.assertEqual(release_gate["diagnostics"]["failure_category"], "vlm_failures")

        provider_status = self.read_json(Path(result["artifacts"]["visual_provider_status"]))
        self.assertEqual(provider_status["status"], "partial_auto_visual")
        self.assertEqual(provider_status["minimums"]["required_vlm_images"], 3)
        self.assertEqual(provider_status["minimums"]["candidate_count"], 3)
        self.assertEqual(provider_status["minimums"]["selected_candidates"], 3)
        self.assertEqual(provider_status["minimums"]["fetched_artifacts"], 3)
        self.assertEqual(provider_status["minimums"]["vlm_images_analyzed"], 2)
        self.assertEqual(provider_status["minimums"]["report_cited_images"], 1)
        self.assertFalse(provider_status["minimums"]["satisfied"])
        self.assertEqual(provider_status["diagnostics"]["shortfall_reason"], "vlm_failures")
        self.assertEqual(provider_status["diagnostics"]["failure_category"], "vlm_failures")
        self.assertEqual(
            provider_status["diagnostics"]["failure_code"],
            "visual_minimum_shortfall",
        )

    def test_visual_required_full_runner_uses_real_acquire_ingest_verify_synthesize_stack(self) -> None:
        runs_dir = self.temp_runs_dir()

        class FakeCodexClient:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def analyze_image(self, *, image_path, mime_type, prompt, config, metadata):
                self.calls.append(
                    {
                        "image_path": image_path,
                        "mime_type": mime_type,
                        "prompt": prompt,
                        "metadata": dict(metadata),
                    }
                )
                ordinal = len(self.calls)
                return OpenAIResponsesVisionResult(
                    observations=(
                        f"Automatic product visual {ordinal} shows a public chart or UI state.",
                        f"OCR text for automatic visual {ordinal} is visible.",
                    ),
                    inferences=("The image can support a visual research claim.",),
                    caveats=(),
                    ocr_text=f"Automatic visual {ordinal}",
                    confidence=0.86,
                    response_id=f"codex_auto_visual_{ordinal:03d}",
                    model=config.model,
                    usage={"events": 2},
                    raw_provider_metadata={"response_id": f"codex_auto_visual_{ordinal:03d}"},
                    actual_cost_usd=0.0,
                )

        def png_bytes(index: int) -> bytes:
            return (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
                b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00\xef\xbf\xa7\xdb"
                b"\x00\x00\x00\x00IEND\xaeB`\x82"
                + f"auto-visual-{index}".encode("ascii")
            )

        def image_url(index: int) -> str:
            return f"https://images.example.com/auto-visual-{index}.png"

        def fake_default_fetch_image(url: str, *, timeout_seconds: float, max_image_bytes: int):
            index = int(url.rsplit("-", 1)[1].split(".", 1)[0])
            return FetchResponse(
                content=png_bytes(index),
                mime_type="image/png",
                status_code=200,
                final_url=url,
            )

        def fake_parallel(*, run, **_kwargs):
            run_dir = Path(run)
            sources_dir = run_dir / "sources"
            sources_dir.mkdir(exist_ok=True)
            page_path = sources_dir / "auto-visual-page.html"
            image_tags = "\n".join(
                (
                    f'<figure><img src="{image_url(index)}" '
                    f'alt="Automatic visual chart {index}" '
                    f'data-phash="auto-visual-{index}" width="640" height="360">'
                    f"<figcaption>Automatic visual chart {index}</figcaption></figure>"
                )
                for index in range(1, 11)
            )
            page_path.write_text(
                f"<html><body><main>{image_tags}</main></body></html>",
                encoding="utf-8",
            )
            evidence = self.read_json(run_dir / "evidence.json")
            evidence["sources"] = [
                {
                    "id": "src_auto_visual_page",
                    "type": "web",
                    "url": "https://example.com/auto-visual-page",
                    "title": "Public-safe automatic visual fixture page",
                    "published_at": None,
                    "accessed_at": "2026-06-26T00:00:00Z",
                    "quality": "primary",
                    "retrieval_status": "fetched",
                    "local_artifact_path": "sources/auto-visual-page.html",
                    "license_policy": "allowed",
                    "robots_policy": "allowed",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "route": "visual_required",
                    "angle_id": "angle_001",
                    "task_id": "task_search_001",
                    "search_result_id": "search_auto_visual_page",
                }
            ]
            evidence.setdefault("budget", {})["max_images"] = 12
            self.write_json(run_dir / "evidence.json", evidence)
            payload = {
                "status": "completed_parallel",
                "ok": True,
                "adapter": "codex-exec",
                "parallel_degraded": False,
                "needs_serial_handoff": False,
                "planned_task_count": 1,
                "failure_counts": {},
                "evidence_source": {
                    "type": "real_child_execution",
                    "adapter": "codex-exec",
                    "accepted_shards": 1,
                    "fixture_only": False,
                    "manual_handoff": False,
                    "attempted_real_child_execution": True,
                    "real_child_execution": True,
                    "real_use_e2e_eligible": True,
                },
                "merge": {"accepted_shards": [{"task_id": "task_research_001"}]},
                "artifacts": {
                    "parallel_orchestration_status": str(run_dir / "parallel_orchestration_status.json")
                },
            }
            self.write_json(run_dir / "parallel_orchestration_status.json", payload)
            return payload

        codex_client = FakeCodexClient()
        with (
            mock.patch("deepresearch.invocation_router.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.vision_adapter.shutil.which", return_value="/usr/bin/codex"),
            mock.patch(
                "deepresearch.page_image_extraction._default_fetch_image",
                side_effect=fake_default_fetch_image,
            ),
            mock.patch(
                "deepresearch.page_image_extraction._default_fetch_source_html",
                return_value=FetchResponse(
                    content=b"<html><body>No page images in deterministic unit fixture.</body></html>",
                    mime_type="text/html",
                    status_code=200,
                    final_url="https://en.wikipedia.org/wiki/Apollo_11",
                ),
            ),
            mock.patch(
                "deepresearch.page_image_extraction._is_private_or_reserved_http_url",
                return_value=False,
            ),
            mock.patch("deepresearch.invocation_router.run_parallel_orchestration", side_effect=fake_parallel),
            mock.patch(
                "deepresearch.vision_adapter._SubprocessCodexInteractiveVisionClient",
                return_value=codex_client,
            ),
        ):
            result = run_skill_invocation(
                "$deep-research: inspect public product screenshots and chart images for visual evidence",
                runs_dir=runs_dir,
                route="visual_required",
                angles=["primary source discovery"],
                budget_preset="quick",
                max_images=12,
                min_tasks=1,
                max_tasks=1,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed_auto_visual")
        self.assertTrue(result["visual_release_gate"]["valid"], result["visual_release_gate"])
        self.assertGreaterEqual(result["visual_release_gate"]["counts"]["real_candidates"], 10)
        self.assertGreaterEqual(
            result["visual_release_gate"]["counts"]["codex_interactive_real_analyzed_images"],
            3,
        )
        self.assertGreaterEqual(len(codex_client.calls), 3)

        run_dir = Path(result["run_dir"])
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        evidence = self.read_json(run_dir / "evidence.json")
        report_status = self.read_json(run_dir / "report_status.json")
        provider_status = self.read_json(run_dir / "visual_provider_status.json")

        self.assertIn("page-image-extractor", {candidate["provider"] for candidate in candidates})
        self.assertGreaterEqual(
            len(
                [
                    candidate
                    for candidate in candidates
                    if candidate["provider"] == "page-image-extractor"
                    and candidate["provider_mode"] == "real"
                ]
            ),
            10,
        )
        self.assertGreaterEqual(
            len([fetch for fetch in fetches if fetch["fetch_status"] == "fetched"]),
            3,
        )
        self.assertGreaterEqual(
            len(
                [
                    observation
                    for observation in observations
                    if observation["provider"] == "codex-interactive"
                    and observation["provider_mode"] == "real"
                    and observation["observation_status"] == "analyzed"
                ]
            ),
            3,
        )
        self.assertTrue(report_status["used_images"])
        self.assertTrue(
            any(
                claim.get("claim_type") in {"visual", "mixed"}
                and claim.get("verification_status") == "supported"
                and set(claim.get("supporting_images", [])) & set(report_status["used_images"])
                for claim in evidence["claims"]
                if isinstance(claim, dict)
            )
        )
        self.assertEqual(provider_status["status"], "completed_auto_visual")
        provider_names = {provider["provider"] for provider in provider_status["providers"]}
        self.assertIn("child-discovered-image-url", provider_names)
        self.assertIn("brave-image-search", provider_names)
        self.assertIn("page-image-extractor", provider_names)
        self.assertIn("browser-screenshot", provider_names)
        self.assertIn("codex-interactive", provider_names)
        codex_provider = next(
            provider
            for provider in provider_status["providers"]
            if provider["provider"] == "codex-interactive"
        )
        self.assertEqual(codex_provider["vlm_images_analyzed"], len(codex_client.calls))
        self.assertTrue(codex_provider["codex_native_handoff"])
        self.assertFalse(codex_provider["hidden_codex_api_call"])

    def test_visual_required_full_runner_fetches_child_discovered_image_urls(self) -> None:
        runs_dir = self.temp_runs_dir()

        class FakeCodexClient:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def analyze_image(self, *, image_path, mime_type, prompt, config, metadata):
                self.calls.append(
                    {
                        "image_path": image_path,
                        "mime_type": mime_type,
                        "prompt": prompt,
                        "metadata": dict(metadata),
                    }
                )
                ordinal = len(self.calls)
                return OpenAIResponsesVisionResult(
                    observations=(
                        f"Apollo 11 public image {ordinal} shows mission visual evidence.",
                        f"The image contains a visible Apollo 11 spacecraft or lunar scene {ordinal}.",
                    ),
                    inferences=("The public image can support an Apollo 11 visual claim.",),
                    caveats=(),
                    ocr_text=f"Apollo 11 visual {ordinal}",
                    confidence=0.88,
                    response_id=f"codex_apollo_visual_{ordinal:03d}",
                    model=config.model,
                    usage={"events": 2},
                    raw_provider_metadata={"response_id": f"codex_apollo_visual_{ordinal:03d}"},
                    actual_cost_usd=0.0,
                )

        def png_bytes(index: int) -> bytes:
            return (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                b"\x00\x00\x02\x80\x00\x00\x01\xe0\x08\x04\x00\x00\x00"
                b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00"
                b"\x00\x00\x00\x00IEND\xaeB`\x82"
                + f"apollo-visual-{index}".encode("ascii")
            )

        def image_url(index: int) -> str:
            return f"https://commons.wikimedia.org/wiki/Special:FilePath/Apollo_11_visual_{index}.png"

        def fake_default_fetch_image(url: str, *, timeout_seconds: float, max_image_bytes: int):
            index = int(url.rsplit("_", 1)[1].split(".", 1)[0])
            return FetchResponse(
                content=png_bytes(index),
                mime_type="image/png",
                status_code=200,
                final_url=url,
            )

        def fake_parallel(*, run, **_kwargs):
            run_dir = Path(run)
            sources_dir = run_dir / "sources"
            sources_dir.mkdir(exist_ok=True)
            page_path = sources_dir / "apollo-child-notes.html"
            page_path.write_text(
                "<html><body><p>Codex child notes list Apollo 11 image URLs separately.</p></body></html>",
                encoding="utf-8",
            )
            evidence = self.read_json(run_dir / "evidence.json")
            evidence["sources"] = [
                {
                    "id": "src_apollo_child_notes",
                    "type": "web",
                    "url": "https://en.wikipedia.org/wiki/Apollo_11",
                    "title": "Apollo 11 public source",
                    "published_at": None,
                    "accessed_at": "2026-06-26T00:00:00Z",
                    "quality": "primary",
                    "retrieval_status": "fetched",
                    "local_artifact_path": "sources/apollo-child-notes.html",
                    "license_policy": "allowed",
                    "robots_policy": "allowed",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "route": "visual_required",
                    "angle_id": "angle_001",
                    "task_id": "task_search_001",
                    "search_result_id": "search_apollo_child_notes",
                }
            ]
            evidence["images"] = [
                {
                    "id": f"img_apollo_child_{index:03d}",
                    "source_id": "src_apollo_child_notes",
                    "origin": "image_search",
                    "image_url": image_url(index),
                    "page_url": "https://en.wikipedia.org/wiki/Apollo_11",
                    "local_artifact_path": f"evidence_shards/task_research_001/apollo_{index:03d}.json",
                    "mime_type": "image/png",
                    "width": 640,
                    "height": 480,
                    "observations": [f"Child-discovered Apollo 11 image URL {index}."],
                    "inferences": [],
                    "visual_tasks": ["image_claim_alignment"],
                    "analysis_provider": "codex-interactive",
                    "analysis_status": "skipped",
                    "policy_flags": [],
                    "caveats": [],
                    "task_id": "task_search_001",
                    "angle_id": "angle_001",
                    "source_search_result_id": "search_apollo_child_notes",
                }
                for index in range(1, 11)
            ]
            evidence.setdefault("budget", {})["max_images"] = 12
            self.write_json(run_dir / "evidence.json", evidence)
            payload = {
                "status": "completed_parallel",
                "ok": True,
                "adapter": "codex-exec",
                "parallel_degraded": False,
                "needs_serial_handoff": False,
                "planned_task_count": 1,
                "failure_counts": {},
                "evidence_source": {
                    "type": "real_child_execution",
                    "adapter": "codex-exec",
                    "accepted_shards": 1,
                    "fixture_only": False,
                    "manual_handoff": False,
                    "attempted_real_child_execution": True,
                    "real_child_execution": True,
                    "real_use_e2e_eligible": True,
                },
                "merge": {"accepted_shards": [{"task_id": "task_research_001"}]},
                "artifacts": {
                    "parallel_orchestration_status": str(run_dir / "parallel_orchestration_status.json")
                },
            }
            self.write_json(run_dir / "parallel_orchestration_status.json", payload)
            return payload

        codex_client = FakeCodexClient()
        with (
            mock.patch("deepresearch.invocation_router.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.vision_adapter.shutil.which", return_value="/usr/bin/codex"),
            mock.patch(
                "deepresearch.page_image_extraction._default_fetch_image",
                side_effect=fake_default_fetch_image,
            ),
            mock.patch(
                "deepresearch.page_image_extraction._default_fetch_source_html",
                return_value=FetchResponse(
                    content=b"<html><body>No page images in deterministic unit fixture.</body></html>",
                    mime_type="text/html",
                    status_code=200,
                    final_url="https://en.wikipedia.org/wiki/Apollo_11",
                ),
            ),
            mock.patch(
                "deepresearch.page_image_extraction._is_private_or_reserved_http_url",
                return_value=False,
            ),
            mock.patch("deepresearch.invocation_router.run_parallel_orchestration", side_effect=fake_parallel),
            mock.patch(
                "deepresearch.vision_adapter._SubprocessCodexInteractiveVisionClient",
                return_value=codex_client,
            ),
        ):
            result = run_skill_invocation(
                "$deep-research: find and cite at least ten public Apollo 11 images",
                runs_dir=runs_dir,
                route="visual_required",
                angles=["primary source discovery"],
                budget_preset="standard",
                max_images=12,
                min_tasks=1,
                max_tasks=1,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed_auto_visual")
        self.assertTrue(result["visual_release_gate"]["valid"], result["visual_release_gate"])
        self.assertGreaterEqual(result["visual_release_gate"]["counts"]["real_candidates"], 10)
        self.assertGreaterEqual(
            result["visual_release_gate"]["counts"]["codex_interactive_real_analyzed_images"],
            3,
        )
        self.assertGreaterEqual(len(codex_client.calls), 3)

        run_dir = Path(result["run_dir"])
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        evidence = self.read_json(run_dir / "evidence.json")
        report_status = self.read_json(run_dir / "report_status.json")
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        child_candidates = [
            candidate
            for candidate in candidates
            if candidate["provider"] == "child-discovered-image-url"
        ]
        child_fetches = [
            fetch
            for fetch in fetches
            if fetch["provider"] == "child-discovered-image-url"
            and fetch["fetch_status"] == "fetched"
        ]

        self.assertGreaterEqual(len(child_candidates), 10)
        self.assertGreaterEqual(len(child_fetches), 3)
        self.assertFalse(
            [
                candidate
                for candidate in candidates
                if candidate["provider"] == "page-image-extractor"
            ],
            "page-image-extractor should not fabricate candidates from child notes without img tags",
        )
        self.assertTrue(
            all((run_dir / fetch["local_artifact_path"]).is_file() for fetch in child_fetches)
        )
        self.assertGreaterEqual(
            len(
                [
                    observation
                    for observation in observations
                    if observation["provider"] == "codex-interactive"
                    and observation["provider_mode"] == "real"
                    and observation["observation_status"] == "analyzed"
                ]
            ),
            3,
        )
        self.assertTrue(report_status["used_images"])
        self.assertTrue(
            any(
                claim.get("claim_type") in {"visual", "mixed"}
                and claim.get("verification_status") == "supported"
                and set(claim.get("supporting_images", [])) & set(report_status["used_images"])
                for claim in evidence["claims"]
                if isinstance(claim, dict)
            )
        )
        self.assertEqual(provider_status["status"], "completed_auto_visual")
        provider_names = {provider["provider"] for provider in provider_status["providers"]}
        self.assertIn("child-discovered-image-url", provider_names)
        child_provider = next(
            provider
            for provider in provider_status["providers"]
            if provider["provider"] == "child-discovered-image-url"
        )
        self.assertEqual(child_provider["provider_mode"], "real")
        self.assertGreaterEqual(child_provider["candidates_discovered"], 10)
        self.assertGreaterEqual(child_provider["artifacts_fetched"], 3)
        self.assertTrue(
            all(call["metadata"]["candidate_id"].startswith("cand_apollo_child") for call in codex_client.calls)
        )

    def test_visual_required_blocks_before_synthesis_when_codex_vlm_provider_is_missing(self) -> None:
        runs_dir = self.temp_runs_dir()

        def fake_parallel(*, run, **_kwargs):
            run_dir = Path(run)
            evidence = self.read_json(run_dir / "evidence.json")
            evidence["sources"] = [
                {
                    "id": "src_auto_visual",
                    "type": "web",
                    "url": "https://example.com/public-product",
                    "title": "Public product page",
                    "published_at": None,
                    "accessed_at": "2026-06-26T00:00:00Z",
                    "quality": "primary",
                    "retrieval_status": "fetched",
                    "local_artifact_path": "sources/src_auto_visual.html",
                    "license_policy": "allowed",
                    "robots_policy": "allowed",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "route": "visual_required",
                    "angle_id": "angle_001",
                }
            ]
            self.write_json(run_dir / "evidence.json", evidence)
            return {
                "status": "completed_parallel",
                "ok": True,
                "adapter": "codex-exec",
                "parallel_degraded": False,
                "needs_serial_handoff": False,
                "planned_task_count": 1,
                "failure_counts": {},
                "evidence_source": {"type": "real_child_execution", "adapter": "codex-exec"},
                "merge": {"accepted_shards": [{"task_id": "task_research_001"}]},
                "artifacts": {},
            }

        def fake_acquire(*, run, **_kwargs):
            run_dir = Path(run)
            self.write_jsonl(run_dir / "visual_candidates.jsonl", [])
            self.write_jsonl(run_dir / "image_fetch_status.jsonl", [])
            self.write_jsonl(run_dir / "visual_observations.jsonl", [])
            self.write_json(run_dir / "visual_search_plan.json", {"tasks": []})
            self.write_json(
                run_dir / "visual_provider_status.json",
                {
                    "status": "real_image_search_candidates_collected",
                    "ok": True,
                    "terminal": False,
                    "providers": [],
                    "diagnostics": {"actionable_cause": "acquisition ready"},
                },
            )
            return {"status": "real_image_search_candidates_collected", "ok": True}

        def fake_ingest(*, run, **_kwargs):
            run_dir = Path(run)
            self.write_json(
                run_dir / "visual_provider_status.json",
                {
                    "status": "blocked_missing_vlm_provider",
                    "ok": False,
                    "terminal": True,
                    "providers": [
                        {
                            "provider": "codex-interactive",
                            "provider_kind": "vlm",
                            "provider_mode": "real",
                            "blocked_reason": "codex_exec_unavailable",
                        }
                    ],
                    "diagnostics": {
                        "actionable_cause": "codex-interactive visual worker is unavailable"
                    },
                },
            )
            self.write_json(run_dir / "vision_ingest_status.json", {"status": "blocked_missing_vlm_provider", "ok": False})
            return {
                "status": "blocked_missing_vlm_provider",
                "ok": False,
                "terminal": True,
                "blocked_reason": "codex_exec_unavailable",
            }

        with (
            mock.patch("deepresearch.invocation_router.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.invocation_router.run_parallel_orchestration", side_effect=fake_parallel),
            mock.patch("deepresearch.invocation_router.acquire_visual_candidates", side_effect=fake_acquire),
            mock.patch("deepresearch.invocation_router.ingest_vision_observations", side_effect=fake_ingest),
            mock.patch("deepresearch.invocation_router.synthesize_report") as synthesize_mock,
        ):
            result = run_skill_invocation(
                "$deep-research: inspect public product screenshots for visual evidence",
                runs_dir=runs_dir,
                route="visual_required",
                angles=["primary source discovery"],
                budget_preset="quick",
                min_tasks=1,
                max_tasks=1,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["status"], "blocked_missing_vlm_provider")
        self.assertNotIn("failure_code", result["diagnostics"])
        self.assertNotIn("shortfall_reason", result["diagnostics"])
        synthesize_mock.assert_not_called()
        self.assertIn("visual_provider_status", result["artifacts"])
        run_status = self.read_json(Path(result["artifacts"]["run_status"]))
        release_gate = run_status["visual_summary"]["release_gate"]
        release_gate_diagnostics = release_gate["diagnostics"]
        self.assertFalse(release_gate["valid"])
        self.assertEqual(release_gate["shortfall_reason"], "none")
        self.assertEqual(release_gate_diagnostics["shortfall_reason"], "none")
        self.assertEqual(
            release_gate_diagnostics["blocked_status"],
            "blocked_missing_vlm_provider",
        )
        self.assertNotIn("failure_code", release_gate_diagnostics)
        self.assertNotIn("failure_category", release_gate_diagnostics)

    def test_text_only_codex_full_runner_does_not_run_visual_acquisition_or_ingest(self) -> None:
        with (
            mock.patch("deepresearch.invocation_router.acquire_visual_candidates") as acquire_mock,
            mock.patch("deepresearch.invocation_router.ingest_vision_observations") as ingest_mock,
        ):
            result = run_skill_invocation(
                "$deep-research: investigate deterministic router fixture",
                runs_dir=self.temp_runs_dir(),
                adapter_name="fixture",
                route="text_only",
                budget_preset="quick",
                min_tasks=1,
                max_tasks=1,
            )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "blocked_semantic_planner_unavailable")
        self.assertEqual(result["planner_mode"], "blocked")
        acquire_mock.assert_not_called()
        ingest_mock.assert_not_called()

    def test_visual_terminal_status_reports_minimum_shortfall_for_two_real_images(self) -> None:
        run_dir = self.temp_runs_dir() / "visual_minimum_shortfall"
        run_dir.mkdir()
        self.write_json(
            run_dir / "evidence.json",
            {
                "run_id": run_dir.name,
                "routing": [
                    {"id": "angle_001", "modality": "visual_required", "max_images": 12}
                ],
                "claims": [],
            },
        )
        self.write_jsonl(
            run_dir / "visual_candidates.jsonl",
            [
                {
                    "candidate_id": f"cand_real_{index:03d}",
                    "provider": "page-image-extractor",
                    "provider_kind": "page_extractor",
                    "provider_mode": "real",
                    "candidate_status": "fetched",
                }
                for index in range(1, 4)
            ],
        )
        self.write_jsonl(
            run_dir / "image_fetch_status.jsonl",
            [
                {
                    "candidate_id": f"cand_real_{index:03d}",
                    "fetch_id": f"fetch_real_{index:03d}",
                    "provider": "page-image-extractor",
                    "provider_kind": "page_extractor",
                    "provider_mode": "real",
                    "fetch_status": "fetched",
                    "local_artifact_path": f"images/img_real_{index:03d}.png",
                    "evidence_image_id": f"img_real_{index:03d}",
                }
                for index in range(1, 4)
            ],
        )
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "candidate_id": f"cand_real_{index:03d}",
                    "fetch_id": f"fetch_real_{index:03d}",
                    "evidence_image_id": f"img_real_{index:03d}",
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "observation_status": "analyzed",
                }
                for index in range(1, 3)
            ],
        )

        status = invocation_router._visual_pipeline_terminal_status(
            run_dir=run_dir,
            status="partial_auto_visual",
            actionable_cause="visual minimum shortfall",
            acquisition_status=None,
            ingest_status=None,
        )

        self.assertEqual(status["status"], "partial_auto_visual")
        self.assertFalse(status["ok"])
        self.assertEqual(status["diagnostics"]["failure_code"], "visual_minimum_shortfall")
        self.assertNotEqual(status["minimums"]["shortfall_reason"], "none")

        invocation_router._write_visual_completion_status(
            run_dir=run_dir,
            provider_status={
                "schema_version": "codex-deepresearch.visual-provider-status.v0",
                "run_id": run_dir.name,
                "run_dir": str(run_dir),
                "providers": [],
                "diagnostics": {},
                "artifacts": {},
            },
            status="partial_auto_visual",
            actionable_cause="visual minimum shortfall",
            validation=None,
        )
        provider_status = self.read_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME)
        self.assertEqual(provider_status["diagnostics"]["failure_code"], "visual_minimum_shortfall")
        self.assertNotEqual(provider_status["minimums"]["shortfall_reason"], "none")

    def test_visual_terminal_status_reports_missing_report_linkage_after_three_real_images(self) -> None:
        run_dir = self.temp_runs_dir() / "visual_report_linkage_missing"
        run_dir.mkdir()
        image_ids = [f"img_real_{index:03d}" for index in range(1, 4)]
        self.write_json(
            run_dir / "evidence.json",
            {
                "run_id": run_dir.name,
                "routing": [
                    {"id": "angle_001", "modality": "visual_required", "max_images": 12}
                ],
                "claims": [
                    {
                        "id": "claim_visual_real",
                        "claim_type": "visual",
                        "supporting_images": image_ids,
                        "verification_status": "supported",
                    }
                ],
            },
        )
        self.write_json(run_dir / "report_status.json", {"used_images": []})
        self.write_jsonl(
            run_dir / "visual_candidates.jsonl",
            [
                {
                    "candidate_id": f"cand_real_{index:03d}",
                    "provider": "child-discovered-image-url",
                    "provider_kind": "web_image_search",
                    "provider_mode": "real",
                    "candidate_status": "analyzed",
                }
                for index in range(1, 4)
            ],
        )
        self.write_jsonl(
            run_dir / "image_fetch_status.jsonl",
            [
                {
                    "candidate_id": f"cand_real_{index:03d}",
                    "fetch_id": f"fetch_real_{index:03d}",
                    "provider": "child-discovered-image-url",
                    "provider_kind": "web_image_search",
                    "provider_mode": "real",
                    "fetch_status": "fetched",
                    "local_artifact_path": f"images/{image_ids[index - 1]}.png",
                    "evidence_image_id": image_ids[index - 1],
                }
                for index in range(1, 4)
            ],
        )
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "candidate_id": f"cand_real_{index:03d}",
                    "fetch_id": f"fetch_real_{index:03d}",
                    "evidence_image_id": image_ids[index - 1],
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "observation_status": "analyzed",
                }
                for index in range(1, 4)
            ],
        )

        status = invocation_router._visual_pipeline_terminal_status(
            run_dir=run_dir,
            status="partial_auto_visual",
            actionable_cause="report linkage missing",
            acquisition_status=None,
            ingest_status=None,
        )

        self.assertEqual(status["status"], "partial_auto_visual")
        self.assertFalse(status["ok"])
        self.assertEqual(
            status["diagnostics"]["failure_code"],
            "visual_report_linkage_missing",
        )
        self.assertEqual(
            status["minimums"]["shortfall_reason"],
            "report_linkage_missing",
        )

    def test_visual_completion_diagnostics_do_not_add_minimum_failure_codes_to_blocked_vlm_status(self) -> None:
        diagnostics = invocation_router._visual_completion_diagnostics(
            {
                "status": "blocked_missing_vlm_provider",
                "diagnostics": {
                    "actionable_cause": "codex-interactive visual worker is unavailable"
                },
                "minimums": {
                    "required_vlm_images": 3,
                    "candidate_count": 3,
                    "selected_candidates": 3,
                    "fetched_artifacts": 3,
                    "vlm_images_analyzed": 0,
                    "report_cited_images": 0,
                    "satisfied": False,
                    "shortfall_reason": "vlm_failures",
                },
            },
            fallback_actionable_cause="fallback blocked cause",
        )

        self.assertEqual(
            diagnostics["actionable_cause"],
            "codex-interactive visual worker is unavailable",
        )
        self.assertNotIn("failure_code", diagnostics)
        self.assertNotIn("shortfall_reason", diagnostics)

    def test_serial_fallback_provenance_is_distinguishable_when_no_shards_are_accepted(self) -> None:
        result = run_skill_invocation(
            "$deep-research: force serial fallback provenance",
            runs_dir=self.temp_runs_dir(),
            adapter_name="serial-degraded",
            route="text_only",
            angles=["primary source discovery"],
            budget_preset="quick",
            min_tasks=1,
            max_tasks=1,
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["selected_mode"], "full-runner")
        self.assertEqual(result["status"], "blocked_parallel_execution")
        self.assertEqual(result["provenance"]["type"], "serial_handoff")
        self.assertTrue(result["parallel"]["needs_serial_handoff"])
        self.assertIn("parallel_orchestration_status", result["artifacts"])
        persisted = self.read_json(Path(result["artifacts"]["run_status"]))
        self.assertEqual(persisted["provenance"]["type"], "serial_handoff")
        self.assertNotIn("report_status", result["artifacts"])
        self.assertTrue(result["fallback"]["needs_serial_handoff"])
        self.assertEqual(result["shard_summary"]["accepted_shard_count"], 0)

    def test_visual_attempted_success_includes_visual_provider_status_artifact(self) -> None:
        result = run_skill_invocation(
            "$deep-research: inspect product screenshots for evidence",
            runs_dir=self.temp_runs_dir(),
            adapter_name="fixture",
            route="visual_required",
            angles=["primary source discovery"],
            budget_preset="quick",
            min_tasks=1,
            max_tasks=1,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed_fixture")
        self.assertIn("visual_provider_status", result["artifacts"])
        self.assertIn("visual_provider_status", result["artifact_handoff"]["artifact_paths"])

        visual_provider_status = self.read_json(Path(result["artifacts"]["visual_provider_status"]))
        self.assertTrue(visual_provider_status["ok"])
        self.assertEqual(visual_provider_status["status"], "fixture_visual_provider")

    def test_real_parallel_provenance_is_preserved_in_final_status(self) -> None:
        runs_dir = self.temp_runs_dir()

        def fake_parallel(*, run, **_kwargs):
            run_dir = Path(run)
            payload = {
                "status": "completed_parallel",
                "ok": True,
                "adapter": "codex-exec",
                "parallel_degraded": False,
                "needs_serial_handoff": False,
                "planned_task_count": 1,
                "failure_counts": {},
                "evidence_source": {
                    "type": "real_child_execution",
                    "adapter": "codex-exec",
                    "accepted_shards": 1,
                    "fixture_only": False,
                    "manual_handoff": False,
                    "attempted_real_child_execution": True,
                    "real_child_execution": True,
                    "real_use_e2e_eligible": True,
                },
                "merge": {"accepted_shards": [{"task_id": "task_research_001"}]},
                "artifacts": {
                    "parallel_orchestration_status": str(run_dir / "parallel_orchestration_status.json")
                },
            }
            (run_dir / "parallel_orchestration_status.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return payload

        def fake_synthesize(*, run, **_kwargs):
            run_dir = Path(run)
            (run_dir / "report.md").write_text("# Report\n", encoding="utf-8")
            payload = {
                "status": "completed",
                "artifacts": {
                    "report": str(run_dir / "report.md"),
                    "report_status": str(run_dir / "report_status.json"),
                },
            }
            (run_dir / "report_status.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return payload

        with (
            mock.patch("deepresearch.invocation_router.run_parallel_orchestration", side_effect=fake_parallel),
            mock.patch("deepresearch.invocation_router.enforce_guardrails", return_value={"status": "completed"}),
            mock.patch("deepresearch.invocation_router.verify_claims", return_value={"status": "completed"}),
            mock.patch("deepresearch.invocation_router.synthesize_report", side_effect=fake_synthesize),
        ):
            result = run_skill_invocation(
                "$deep-research: preserve real parallel provenance",
                runs_dir=runs_dir,
                route="text_only",
                angles=["primary source discovery"],
                budget_preset="quick",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed_parallel")
        self.assertEqual(result["provenance"]["type"], "real_child_execution")
        self.assertTrue(result["provenance"]["real_child_execution"])
        self.assertTrue(result["provenance"]["real_use_e2e_eligible"])
        self.assertIn("run_status", result["artifacts"])
        self.assertIn("report_status", result["artifacts"])
        self.assertEqual(result["shard_summary"]["accepted_shard_count"], 1)
        self.assertFalse(result["fallback"]["parallel_degraded"])

    def test_partial_degraded_parallel_continues_to_synthesis_and_syncs_terminal_artifacts(self) -> None:
        runs_dir = self.temp_runs_dir()

        def fake_parallel(*, run, **_kwargs):
            run_dir = Path(run)
            merge = {
                "status": "completed",
                "accepted_shard_count": 1,
                "accepted_shards": [{"task_id": "task_research_001"}],
                "failed_tasks": [{"task_id": "task_research_002"}],
                "rejected_shards": [{"task_id": "task_research_003"}],
                "discarded_tasks": [{"task_id": "task_research_004"}],
                "blocked_tasks": [{"task_id": "task_research_005"}],
                "failure_counts": {
                    "failed_tasks": 1,
                    "rejected_shards": 1,
                    "discarded_tasks": 1,
                    "blocked_tasks": 1,
                },
                "diagnostics": {
                    "shard_counts": {
                        "accepted_shards": 1,
                        "rejected_shards": 1,
                        "failed_tasks": 1,
                        "discarded_tasks": 1,
                        "blocked_tasks": 1,
                    }
                },
            }
            self.write_json(run_dir / "merge_status.json", merge)
            payload = {
                "status": "degraded_serial_handoff_required",
                "ok": True,
                "adapter": "serial-degraded",
                "parallel_degraded": True,
                "degraded_reason": "codex_child_sandbox_blocked",
                "needs_serial_handoff": False,
                "planned_task_count": 5,
                "accepted_shard_count": 1,
                "failure_counts": merge["failure_counts"],
                "diagnostics": merge["diagnostics"],
                "evidence_source": {
                    "type": "real_child_execution",
                    "adapter": "serial-degraded",
                    "accepted_shards": 1,
                    "fixture_only": False,
                    "manual_handoff": False,
                    "attempted_real_child_execution": True,
                    "real_child_execution": True,
                    "real_use_e2e_eligible": False,
                },
                "merge": merge,
                "artifacts": {
                    "parallel_orchestration_status": str(run_dir / "parallel_orchestration_status.json"),
                    "merge_status": str(run_dir / "merge_status.json"),
                },
            }
            self.write_json(run_dir / "parallel_orchestration_status.json", payload)
            return payload

        def fake_synthesize(*, run, **_kwargs):
            run_dir = Path(run)
            (run_dir / "report.md").write_text("# Partial report\n", encoding="utf-8")
            payload = {
                "status": "completed",
                "ok": True,
                "artifacts": {
                    "report": str(run_dir / "report.md"),
                    "report_status": str(run_dir / "report_status.json"),
                },
            }
            self.write_json(run_dir / "report_status.json", payload)
            return payload

        with (
            mock.patch("deepresearch.invocation_router.run_parallel_orchestration", side_effect=fake_parallel),
            mock.patch("deepresearch.invocation_router.enforce_guardrails", return_value={"status": "completed", "ok": True}),
            mock.patch("deepresearch.invocation_router.verify_claims", return_value={"status": "completed", "ok": True}),
            mock.patch("deepresearch.invocation_router.synthesize_report", side_effect=fake_synthesize),
        ):
            result = run_skill_invocation(
                "$deep-research: synthesize from partial degraded evidence",
                runs_dir=runs_dir,
                route="text_only",
                angles=["primary source discovery"],
                budget_preset="quick",
                min_tasks=5,
                max_tasks=5,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed_partial_parallel")
        self.assertNotEqual(result["status"], "blocked_parallel_execution")
        self.assertEqual(result["parallel"]["status"], "degraded_serial_handoff_required")
        self.assertIn("report", result["artifacts"])
        self.assertIn("report_status", result["artifacts"])
        self.assertEqual(result["diagnostics"]["shard_counts"]["accepted_shards"], 1)
        self.assertEqual(result["parallel"]["accepted_shard_count"], 1)
        self.assertEqual(result["shard_summary"]["accepted_shard_count"], 1)
        self.assertNotIn("no accepted shards", json.dumps(result["diagnostics"]))

        run_dir = Path(result["run_dir"])
        run_status = self.read_json(run_dir / "run_status.json")
        parallel_status = self.read_json(run_dir / "parallel_orchestration_status.json")
        merge_status = self.read_json(run_dir / "merge_status.json")
        run_steps = self.read_json(run_dir / "run_steps.json")
        for payload in (run_status, parallel_status, merge_status, run_steps):
            self.assertEqual(payload["terminal_status"], "completed_partial_parallel")
            self.assertEqual(payload["accepted_shard_count"], 1)
            self.assertIsNone(payload["next_safe_stage"])
        self.assertEqual(run_status["status"], "completed_partial_parallel")

    def test_partial_parallel_failed_synthesis_diagnostics_include_shard_counts(self) -> None:
        runs_dir = self.temp_runs_dir()

        def fake_parallel(*, run, **_kwargs):
            run_dir = Path(run)
            merge = {
                "status": "completed",
                "accepted_shard_count": 1,
                "accepted_shards": [{"task_id": "task_research_001"}],
                "failed_tasks": [{"task_id": "task_research_002"}],
                "rejected_shards": [{"task_id": "task_research_003"}],
                "discarded_tasks": [],
                "blocked_tasks": [],
                "failure_counts": {
                    "failed_tasks": 1,
                    "rejected_shards": 1,
                    "discarded_tasks": 0,
                    "blocked_tasks": 0,
                },
            }
            self.write_json(run_dir / "merge_status.json", merge)
            payload = {
                "status": "completed_partial_parallel",
                "ok": True,
                "adapter": "codex-exec",
                "parallel_degraded": False,
                "needs_serial_handoff": False,
                "planned_task_count": 3,
                "accepted_shard_count": 1,
                "failure_counts": merge["failure_counts"],
                "evidence_source": {
                    "type": "real_child_execution",
                    "adapter": "codex-exec",
                    "accepted_shards": 1,
                },
                "merge": merge,
                "artifacts": {},
            }
            self.write_json(run_dir / "parallel_orchestration_status.json", payload)
            return payload

        with (
            mock.patch("deepresearch.invocation_router.run_parallel_orchestration", side_effect=fake_parallel),
            mock.patch("deepresearch.invocation_router.enforce_guardrails", return_value={"status": "completed", "ok": True}),
            mock.patch("deepresearch.invocation_router.verify_claims", return_value={"status": "completed", "ok": True}),
            mock.patch(
                "deepresearch.invocation_router.synthesize_report",
                side_effect=invocation_router.ReportGenerationError("partial evidence is insufficient for synthesis"),
            ),
        ):
            result = run_skill_invocation(
                "$deep-research: fail synthesis from insufficient partial evidence",
                runs_dir=runs_dir,
                route="text_only",
                angles=["primary source discovery"],
                budget_preset="quick",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_synthesis")
        self.assertEqual(
            result["diagnostics"]["actionable_cause"],
            "partial evidence is insufficient for synthesis",
        )
        self.assertEqual(
            result["diagnostics"]["shard_counts"],
            {
                "accepted_shards": 1,
                "rejected_shards": 1,
                "failed_tasks": 1,
                "discarded_tasks": 0,
                "blocked_tasks": 0,
            },
        )
        self.assertEqual(result["shard_summary"]["accepted_shard_count"], 1)
        self.assertNotIn("no accepted shards", json.dumps(result["diagnostics"]))

    def test_successful_synthesis_without_report_status_fails_handoff_validation(self) -> None:
        runs_dir = self.temp_runs_dir()

        def fake_parallel(*, run, **_kwargs):
            run_dir = Path(run)
            payload = {
                "status": "completed_parallel",
                "ok": True,
                "adapter": "codex-exec",
                "parallel_degraded": False,
                "needs_serial_handoff": False,
                "planned_task_count": 1,
                "failure_counts": {},
                "evidence_source": {
                    "type": "real_child_execution",
                    "adapter": "codex-exec",
                    "accepted_shards": 1,
                    "fixture_only": False,
                    "manual_handoff": False,
                    "attempted_real_child_execution": True,
                    "real_child_execution": True,
                    "real_use_e2e_eligible": True,
                },
                "merge": {"accepted_shards": [{"task_id": "task_research_001"}]},
                "artifacts": {
                    "parallel_orchestration_status": str(run_dir / "parallel_orchestration_status.json")
                },
            }
            (run_dir / "parallel_orchestration_status.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return payload

        def fake_synthesize_without_report_status(*, run, **_kwargs):
            run_dir = Path(run)
            (run_dir / "report.md").write_text("# Report\n", encoding="utf-8")
            return {
                "status": "completed",
                "artifacts": {"report": str(run_dir / "report.md")},
            }

        with (
            mock.patch("deepresearch.invocation_router.run_parallel_orchestration", side_effect=fake_parallel),
            mock.patch("deepresearch.invocation_router.enforce_guardrails", return_value={"status": "completed"}),
            mock.patch("deepresearch.invocation_router.verify_claims", return_value={"status": "completed"}),
            mock.patch(
                "deepresearch.invocation_router.synthesize_report",
                side_effect=fake_synthesize_without_report_status,
            ),
        ):
            result = run_skill_invocation(
                "$deep-research: missing report status regression",
                runs_dir=runs_dir,
                route="text_only",
                angles=["primary source discovery"],
                budget_preset="quick",
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["status"], "failed_synthesis")
        self.assertIn("report_status", result["diagnostics"]["missing_required_artifacts"])
        self.assertIn("report_status", result["artifact_handoff"]["missing_required_artifacts"])
        self.assertIn("report", result["artifacts"])
        self.assertIn("evidence", result["artifacts"])
        self.assertIn("run_status", result["artifacts"])
        self.assertNotIn("report_status", result["artifacts"])

        persisted = self.read_json(Path(result["artifacts"]["run_status"]))
        self.assertFalse(persisted["ok"])
        self.assertEqual(persisted["status"], "failed_synthesis")
        self.assertIn("report_status", persisted["diagnostics"]["missing_required_artifacts"])


if __name__ == "__main__":
    unittest.main()
