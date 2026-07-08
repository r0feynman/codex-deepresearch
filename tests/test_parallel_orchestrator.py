from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "plugins" / "codex-deepresearch" / "scripts" / "codex-deepresearch"
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import (  # noqa: E402
    CodexExecAdapter,
    FixtureAdapter,
    ParallelOrchestrationError,
    ingest_vision_observations,
    inspect_run_state,
    merge_evidence_shards,
    plan_research_tasks,
    prepare_run as prepare_search_handoff_run,
    read_trace_records,
    run_parallel_orchestration,
    synthesize_report,
    validate_artifacts,
    validate_trace_file,
)
from deepresearch import parallel_orchestrator  # noqa: E402
from deepresearch.visual_artifacts import visual_minimums_for_run  # noqa: E402


TEST_MANUAL_ANGLES = ("primary source discovery",)
DISABLE_DEFAULT_SEMANTIC_ADAPTER_ENV = "CODEX_DEEPRESEARCH_DISABLE_DEFAULT_SEMANTIC_ADAPTER"


def prepare_run(*args, **kwargs):
    kwargs.setdefault("angles", list(TEST_MANUAL_ANGLES))
    kwargs.setdefault("_allow_release_ineligible_materialization_for_tests", True)
    return prepare_search_handoff_run(*args, **kwargs)


class ParallelOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        previous = os.environ.get(DISABLE_DEFAULT_SEMANTIC_ADAPTER_ENV)
        os.environ[DISABLE_DEFAULT_SEMANTIC_ADAPTER_ENV] = "1"

        def restore() -> None:
            if previous is None:
                os.environ.pop(DISABLE_DEFAULT_SEMANTIC_ADAPTER_ENV, None)
            else:
                os.environ[DISABLE_DEFAULT_SEMANTIC_ADAPTER_ENV] = previous

        self.addCleanup(restore)

    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def load_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def write_jsonl(self, path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

    def load_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def prepare(self, *, route: str = "text_only", budget: str = "standard") -> Path:
        prepared = prepare_run(
            question="Research a deterministic orchestration fixture.",
            runs_dir=self.temp_runs_dir(),
            route=route,
            budget_preset=budget,
        )
        return Path(prepared["run_dir"])

    def shard(
        self,
        run_dir: Path,
        task: dict,
        *,
        duplicate: bool = False,
        common_ids: bool = False,
    ) -> dict:
        evidence = self.load_json(run_dir / "evidence.json")
        source_id = "src_001" if common_ids else f"src_{task['id']}"
        image_id = "img_001" if common_ids else f"img_{task['id']}"
        claim_id = "claim_001" if common_ids else f"claim_{task['id']}"
        url = "https://example.com/duplicate" if duplicate else f"https://example.com/{task['id']}"
        claim_text = (
            "The duplicate claim text is equivalent."
            if duplicate
            else f"Unique claim for {task['id']}."
        )
        return {
            "schema_version": "0.1.0",
            "run_id": f"{evidence['run_id']}-{task['id']}",
            "created_at": "2026-06-23T00:00:00Z",
            "question": evidence["question"],
            "mode": evidence["mode"],
            "search_provider": evidence["search_provider"],
            "vlm_provider": evidence["vlm_provider"],
            "sources": [
                {
                    "id": source_id,
                    "type": "web",
                    "url": url,
                    "title": "Shard source",
                    "published_at": None,
                    "accessed_at": "2026-06-23T00:00:00Z",
                    "quality": "primary",
                    "retrieval_status": "fetched",
                    "local_artifact_path": f"evidence_shards/{task['id']}/source.html",
                    "license_policy": "allowed",
                    "robots_policy": "allowed",
                    "policy_decision": "allowed",
                    "policy_flags": [],
                }
            ],
            "images": [
                {
                    "id": image_id,
                    "source_id": source_id,
                    "origin": "screenshot",
                    "page_url": url,
                    "image_url": url + "/image.png",
                    "local_artifact_path": f"evidence_shards/{task['id']}/image.png",
                    "mime_type": "image/png",
                    "width": 640,
                    "height": 360,
                    "observations": ["A visible fixture image."],
                    "inferences": [],
                    "visual_tasks": [task["id"]],
                    "analysis_provider": evidence["vlm_provider"],
                    "analysis_status": "analyzed",
                    "policy_flags": [],
                    "caveats": [],
                    "content_hash": "duplicate-image-hash" if duplicate else f"hash-{task['id']}",
                }
            ],
            "claims": [
                {
                    "id": claim_id,
                    "text": claim_text,
                    "claim_type": "mixed",
                    "supporting_sources": [source_id],
                    "supporting_images": [image_id],
                    "visual_supports": [
                        {
                            "image_id": image_id,
                            "observation_ref": f"images.{image_id}.observations[0]",
                            "observation_index": 0,
                            "observation_text": "A visible fixture image.",
                            "relation_type": "screenshot_support",
                            "provider": evidence["vlm_provider"],
                            "rationale": "Linked because shard claim and image cite the same source.",
                            "confidence": 0.74,
                        }
                    ],
                    "quote_spans": [
                        {
                            "source_id": source_id,
                            "quote": claim_text,
                            "location": "paragraph 1",
                        }
                    ],
                    "votes": [],
                    "verification_status": "supported",
                    "review_status": "human_accepted",
                    "promotion_status": "not_eligible",
                    "confidence": "medium",
                    "caveats": [],
                }
            ],
        }

    def release_search_result(self, task: dict, **overrides: object) -> dict:
        record = {
            "id": f"sr_{task['id']}",
            "task_id": task.get("search_task_id") or task["id"],
            "angle_id": task["angle_id"],
            "route": task["route"],
            "provider": "codex-native",
            "provider_mode": "real",
            "query": task["query"],
            "url": f"https://example.com/search/{task['id']}",
            "title": "Release search result",
            "snippet": "A release validation search result.",
            "result_type": "web",
            "rank": 1,
            "accessed_at": "2026-06-23T00:00:00Z",
            "retrieval_status": "fetched",
            "policy_decision": "allowed",
            "prompt_id": task["prompt_id"],
            "suite_id": task["suite_id"],
            "prompt_hash": task["prompt_hash"],
            "handoff_artifact": "search_results.jsonl",
        }
        record.update(overrides)
        return record

    def test_planner_output_expands_to_twenty_bounded_research_tasks(self) -> None:
        run_dir = self.prepare()

        result = plan_research_tasks(run=run_dir, min_tasks=20)

        self.assertEqual(result["task_count"], 20)
        tasks = self.load_json(run_dir / "research_tasks.json")
        self.assertEqual(len(tasks["tasks"]), 20)
        self.assertEqual(tasks["max_concurrent_codex_subagents"], 8)
        self.assertTrue(all(task["state"] == "queued" for task in tasks["tasks"]))
        self.assertTrue(all(task["output_shard_path"].startswith("evidence_shards/") for task in tasks["tasks"]))

    def test_codex_exec_adapter_builds_required_json_command(self) -> None:
        run_dir = self.prepare()
        task = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"][0]

        command = CodexExecAdapter(project_root=ROOT).build_command(task, max_threads=8, run_dir=run_dir)

        self.assertEqual(command[:3], ["codex", "exec", "--json"])
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("-C", command)
        self.assertEqual(command[command.index("-C") + 1], str(ROOT))
        self.assertIn("--add-dir", command)
        self.assertEqual(command[command.index("--add-dir") + 1], str(run_dir.resolve()))
        self.assertNotIn("--skip-git-repo-check", command)
        self.assertIn("agents.max_threads=8", command)
        self.assertIn("sandbox_mode=workspace-write", command)
        self.assertIn("approval_policy=never", command)
        self.assertIn(str(run_dir / "evidence_shards/task_research_001/evidence_shard.json"), command[-1])
        shard_dir = run_dir / "evidence_shards/task_research_001"
        self.assertIn(str(shard_dir / "search_results.jsonl"), command[-1])
        self.assertIn(str(shard_dir / "visual_observations.jsonl"), command[-1])
        self.assertIn(str(shard_dir / "verifier_votes.jsonl"), command[-1])
        self.assertIn("Evidence Schema v0 JSON envelope", command[-1])
        self.assertIn("set `schema_version` exactly `0.1.0`", command[-1])
        for field in ("run_id", "created_at", "mode", "search_provider", "vlm_provider"):
            self.assertIn(f"`{field}`", command[-1])
        self.assertIn(f"Set top-level `run_id` exactly `{run_dir.name}`", command[-1])
        self.assertIn("`mode` exactly `codex-plugin`", command[-1])
        self.assertIn("`search_provider` exactly `codex-native`", command[-1])
        self.assertIn("`vlm_provider` exactly `codex-interactive`", command[-1])
        self.assertIn("codex-deepresearch.evidence-shard.v0", command[-1])
        self.assertIn("write a minimal valid `evidence_shard.json`", command[-1])
        self.assertIn("before any optional sidecars", command[-1])
        self.assertIn("invoke them with `python3`, not `python`", command[-1])
        self.assertIn(f"Do not write sidecars outside {shard_dir}", command[-1])
        self.assertIn("Write claim text, caveats, rationales, and synthesized source snippets in English", command[-1])
        self.assertIn("Every source must include a non-empty `local_artifact_path`", command[-1])
        self.assertIn("Verifier vote `method` must be one of", command[-1])
        self.assertIn("`evidence_refs` must reference only source or image IDs present in the same shard", command[-1])

        visual_task = dict(task)
        visual_task["route"] = "visual_required"
        visual_task["max_images"] = 10
        visual_command = CodexExecAdapter(project_root=ROOT).build_command(
            visual_task,
            max_threads=8,
            run_dir=run_dir,
        )
        self.assertIn("discover and write as many public HTTP(S) image_url records", visual_command[-1])
        self.assertIn("targeting 10 when available", visual_command[-1])
        self.assertIn("do not fabricate VLM-derived analysis", visual_command[-1])
        self.assertIn("write release-grade visual observation records", visual_command[-1])
        self.assertNotIn("leave observations and inferences empty", visual_command[-1])

        korean_task = dict(task)
        korean_task["query"] = "한국어 질문은 한국어 claim으로 작성해야 한다."
        korean_command = CodexExecAdapter(project_root=ROOT).build_command(
            korean_task,
            max_threads=8,
            run_dir=run_dir,
        )
        self.assertIn("Write claim text, caveats, rationales, and synthesized source snippets in Korean", korean_command[-1])
        self.assertIn("translate/summarize English source findings into Korean", korean_command[-1])
        self.assertIn("Only direct quote_spans.quote values should remain verbatim", korean_command[-1])
        self.assertIn("Prioritize a compact shard", korean_command[-1])

    def test_codex_exec_adapter_default_timeout_remains_300_seconds(self) -> None:
        adapter = CodexExecAdapter(project_root=ROOT)

        self.assertEqual(adapter.timeout_seconds, 300.0)

    def test_visual_research_tasks_inherit_image_budget(self) -> None:
        run_dir = self.prepare(route="visual_required")

        tasks = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"]

        self.assertEqual(tasks[0]["route"], "visual_required")
        self.assertGreaterEqual(tasks[0]["max_images"], 10)
        command = CodexExecAdapter(project_root=ROOT).build_command(
            tasks[0],
            max_threads=8,
            run_dir=run_dir,
        )
        self.assertIn("targeting", command[-1])
        self.assertIn("public HTTP(S) image_url records", command[-1])

    def test_invalid_output_shard_paths_are_rejected_before_child_execution(self) -> None:
        run_dir = self.prepare()
        base_task = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"][0]
        adapter = CodexExecAdapter(project_root=ROOT, timeout_seconds=12)

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.subprocess.run") as run_mock,
        ):
            for raw_path in ("/tmp/escape/evidence_shard.json", "../escape/evidence_shard.json"):
                task = dict(base_task)
                task["output_shard_path"] = raw_path

                result = adapter.run_task(task, run_dir=run_dir, max_threads=3)

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.failure_category, "missing_shard")
                self.assertIsNotNone(result.message)
                assert result.message is not None
                self.assertIn("invalid output_shard_path", result.message)
                self.assertIn(raw_path, result.message)
                self.assertIn("must be relative and stay under run_dir", result.message)
                self.assertEqual(result.events[-1]["raw_event"]["output_shard_path"], raw_path)

            run_mock.assert_not_called()

    def test_merge_discards_escaped_output_shard_path_without_reading_outside_run_dir(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=1)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        task = tasks_artifact["tasks"][0]
        task["state"] = "completed"
        task["output_shard_path"] = "../outside/evidence_shard.json"
        outside_shard = run_dir.parent / "outside" / "evidence_shard.json"
        outside_shard.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(outside_shard, self.shard(run_dir, task))
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        self.assertEqual(merge["accepted_shards"], [])
        self.assertEqual(merge["rejected_shards"][0]["reason"], "invalid_output_shard_path")
        self.assertIn("must be relative and stay under run_dir", merge["rejected_shards"][0]["diagnostic"])
        tasks = self.load_json(run_dir / "research_tasks.json")["tasks"]
        self.assertEqual(tasks[0]["state"], "discarded")
        self.assertEqual(tasks[0]["discard_reason"], "invalid_output_shard_path")
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["claims"], [])

    def test_release_validation_child_search_sidecar_requires_complete_record(self) -> None:
        prepared = prepare_run(
            question="Release validation child sidecar fixture.",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
            prompt_id="pb-text-001",
            suite_id="issue-118-suite",
        )
        run_dir = Path(prepared["run_dir"])
        plan_research_tasks(run=run_dir, min_tasks=1)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        task = tasks_artifact["tasks"][0]
        task["state"] = "completed"
        task["last_adapter"] = "codex-exec"
        shard_path = run_dir / task["output_shard_path"]
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(shard_path, self.shard(run_dir, task))
        invalid_record = self.release_search_result(
            task,
            url="https://example.com/incomplete",
        )
        invalid_record.pop("retrieval_status")
        self.write_jsonl(
            shard_path.parent / "search_results.jsonl",
            [invalid_record],
        )
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        self.assertEqual(merge["status"], "completed", merge)
        self.assertEqual(merge["accepted_shards"], [])
        failed_task = merge["failed_tasks"][0]
        self.assertEqual(failed_task["failure_category"], "invalid_release_search_handoff")
        self.assertEqual(
            failed_task["child_failure_code"],
            "codex_child_release_handoff_invalid",
        )
        self.assertIn("missing_required_release_field:retrieval_status", failed_task["diagnostic"])
        self.assertIn(
            "missing_required_release_field:retrieval_status",
            failed_task["release_search_handoff_validation"]["rejections"][0]["reason"],
        )
        handoff = merge["codex_native_search_handoff"]
        self.assertEqual(handoff["records"], 0)
        self.assertEqual(len(handoff["rejections"]), 1)
        self.assertIn(
            "missing_required_release_field:retrieval_status",
            handoff["rejections"][0]["reason"],
        )
        self.assertEqual((run_dir / "search_results.jsonl").read_text(encoding="utf-8"), "")

    def test_release_validation_invalid_child_search_sidecar_retries_and_recovers(self) -> None:
        prepared = prepare_run(
            question="Release validation sidecar retry fixture.",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
            prompt_id="pb-text-001",
            suite_id="issue-122-suite",
        )
        run_dir = Path(prepared["run_dir"])
        call_count = 0

        def fake_codex_exec(command, **_kwargs):
            nonlocal call_count
            call_count += 1
            task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
            shard_path = run_dir / task["output_shard_path"]
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_json(shard_path, self.shard(run_dir, task))
            record = self.release_search_result(
                task,
                freshness_requirement="latest",
                query=f"executed provider query attempt {call_count}",
                url=f"https://example.com/retry-{call_count}",
            )
            if call_count == 1:
                record.pop("retrieval_status")
            self.write_jsonl(shard_path.parent / "search_results.jsonl", [record])
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"type":"message","status":"completed","message":"wrote shard"}\n',
                stderr="",
            )

        def validate_artifacts_after_release_sidecar(*args, **kwargs):
            evidence_path = kwargs.get("evidence_path")
            if evidence_path and call_count == 1:
                sidecar_path = Path(evidence_path).parent / "search_results.jsonl"
                if sidecar_path.exists():
                    sidecar_records = [
                        json.loads(line)
                        for line in sidecar_path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    if any("retrieval_status" not in record for record in sidecar_records):
                        raise AssertionError(
                            "release sidecar validation must run before generic artifact validation"
                        )
            return validate_artifacts(*args, **kwargs)

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.random.uniform", return_value=0.0),
            mock.patch("deepresearch.parallel_orchestrator._sleep_for_retry", return_value=0.0) as sleep_mock,
            mock.patch(
                "deepresearch.parallel_orchestrator.validate_artifacts",
                side_effect=validate_artifacts_after_release_sidecar,
            ),
            mock.patch(
                "deepresearch.parallel_orchestrator.subprocess.run",
                side_effect=fake_codex_exec,
            ) as run_mock,
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                codex_exec_timeout_seconds=120,
                min_tasks=1,
                max_tasks=1,
                allow_degraded=False,
            )

        self.assertEqual(result["status"], "completed_parallel")
        self.assertTrue(result["ok"])
        self.assertEqual(call_count, 2)
        self.assertEqual(run_mock.call_count, 2)
        sleep_mock.assert_called_once_with(5.0)
        task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
        self.assertEqual(task["state"], "merged")
        attempts = task["attempt_diagnostics"]
        self.assertEqual([attempt["retry_decision"] for attempt in attempts], ["retry", "do_not_retry"])
        self.assertEqual(
            attempts[0]["child_failure_code"],
            "codex_child_release_handoff_invalid",
        )
        self.assertIn(
            "missing_required_release_field:retrieval_status",
            attempts[0]["release_search_handoff_validation"]["rejections"][0]["reason"],
        )
        self.assertIsNone(attempts[1]["child_failure_code"])
        handoff = result["merge"]["codex_native_search_handoff"]
        self.assertEqual(handoff["records"], 1)
        self.assertEqual(handoff["rejections"], [])
        records = [
            json.loads(line)
            for line in (run_dir / "search_results.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["retrieval_status"], "fetched")
        self.assertEqual(records[0]["query"], "executed provider query attempt 2")
        self.assertEqual(records[0]["freshness_requirement"], "latest")
        self.assertEqual(records[0]["semantic_task_query"], task["query"])
        self.assertEqual(
            records[0]["semantic_task_freshness_requirement"],
            task["freshness_requirement"],
        )
        self.assertEqual(records[0]["url"], "https://example.com/retry-2")
        self.assertEqual(records[0]["semantic_plan_task_id"], task["semantic_plan_task_id"])
        self.assertEqual(records[0]["semantic_plan_hash"], task["semantic_plan_hash"])
        self.assertEqual(records[0]["approved_delta_id"], task["approved_delta_id"])
        assignments = [
            json.loads(line)
            for line in (run_dir / "subagent_assignments.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertGreaterEqual(len(assignments), 2)
        latest_assignment = assignments[-1]
        self.assertEqual(latest_assignment["semantic_plan_task_id"], task["semantic_plan_task_id"])
        self.assertEqual(latest_assignment["semantic_plan_hash"], task["semantic_plan_hash"])
        self.assertEqual(latest_assignment["approved_delta_id"], task["approved_delta_id"])

    def test_release_visual_merge_preserves_child_lineage_and_materializes_handoffs(self) -> None:
        prepared = prepare_run(
            question="Release validation visual handoff fixture.",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            prompt_id="pb-visual-001",
            suite_id="issue-133-suite",
        )
        run_dir = Path(prepared["run_dir"])
        plan_research_tasks(run=run_dir, min_tasks=1)
        for artifact in (
            "visual_search_plan.json",
            "visual_candidates.jsonl",
            "image_fetch_status.jsonl",
        ):
            path = run_dir / artifact
            if path.exists():
                path.unlink()
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        task = tasks_artifact["tasks"][0]
        task["state"] = "completed"
        task["last_adapter"] = "codex-exec"
        shard_path = run_dir / task["output_shard_path"]
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        shard = self.shard(run_dir, task)
        for image in shard["images"]:
            image.pop("task_id", None)
            image.pop("semantic_plan_task_id", None)
            image.pop("semantic_plan_hash", None)
            image.pop("approved_delta_id", None)
            image.pop("angle_id", None)
            image.pop("route", None)
        self.write_json(shard_path, shard)
        self.write_jsonl(
            shard_path.parent / "search_results.jsonl",
            [self.release_search_result(task)],
        )
        self.write_jsonl(
            shard_path.parent / "visual_observations.jsonl",
            [
                {
                    "image_id": shard["images"][0]["id"],
                    "evidence_image_id": shard["images"][0]["id"],
                    "candidate_id": "child_candidate_001",
                    "fetch_id": "child_fetch_001",
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "analysis_provider": "codex-interactive",
                    "codex_interactive_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "observation_status": "analyzed",
                    "observations": ["A child shard visual observation."],
                    "inferences": ["The image directly supports the visual claim."],
                    "policy_decision": "allowed",
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "codex_interactive_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            ],
        )
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            [
                {
                    "observation_id": f"obs_{task['id']}_001",
                    "image_id": shard["images"][0]["id"],
                    "evidence_image_id": shard["images"][0]["id"],
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "codex_interactive_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "observation_status": "analyzed",
                    "observations": ["A stale root visual observation without lineage metadata."],
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "codex_interactive_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            ],
        )
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        self.assertEqual(merge["status"], "completed", merge)
        evidence = self.load_json(run_dir / "evidence.json")
        image = evidence["images"][0]
        self.assertEqual(image["semantic_plan_task_id"], task["semantic_plan_task_id"])
        self.assertEqual(image["semantic_plan_hash"], task["semantic_plan_hash"])
        self.assertEqual(image["approved_delta_id"], task["approved_delta_id"])
        self.assertEqual(image["angle_id"], task["angle_id"])
        self.assertEqual(image["task_id"], task["id"])
        visual_plan = self.load_json(run_dir / "visual_search_plan.json")
        self.assertEqual(
            visual_plan["tasks"][0]["semantic_plan_task_id"],
            task["semantic_plan_task_id"],
        )
        candidates = self.load_jsonl(run_dir / "visual_candidates.jsonl")
        fetch_status = self.load_jsonl(run_dir / "image_fetch_status.jsonl")
        observations = self.load_jsonl(run_dir / "visual_observations.jsonl")
        for record in (candidates[0], fetch_status[0], observations[0]):
            self.assertEqual(record["semantic_plan_task_id"], task["semantic_plan_task_id"])
            self.assertEqual(record["semantic_plan_hash"], task["semantic_plan_hash"])
            self.assertEqual(record["approved_delta_id"], task["approved_delta_id"])
            self.assertEqual(record["angle_id"], task["angle_id"])
        self.assertEqual(candidates[0]["candidate_status"], "selected")
        self.assertEqual(candidates[0]["provider_kind"], "web_image_search")
        self.assertEqual(candidates[0]["provider_provenance"]["provider"], "codex-native")
        self.assertEqual(fetch_status[0]["fetch_status"], "fetched")
        self.assertEqual(fetch_status[0]["candidate_id"], candidates[0]["candidate_id"])
        self.assertEqual(fetch_status[0]["evidence_image_id"], image["id"])
        self.assertEqual(observations[0]["provider"], "codex-interactive")
        self.assertEqual(observations[0]["provider_kind"], "vlm")
        self.assertEqual(observations[0]["provider_mode"], "real")
        self.assertEqual(observations[0]["observation_status"], "analyzed")
        self.assertEqual(observations[0]["candidate_id"], candidates[0]["candidate_id"])
        self.assertEqual(observations[0]["fetch_id"], fetch_status[0]["fetch_id"])
        self.assertEqual(observations[0]["raw_child_candidate_id"], "child_candidate_001")
        self.assertEqual(observations[0]["raw_child_fetch_id"], "child_fetch_001")
        self.assertEqual(observations[0]["linked_candidate_id"], candidates[0]["candidate_id"])
        self.assertEqual(observations[0]["linked_fetch_id"], fetch_status[0]["fetch_id"])
        self.assertEqual(
            observations[0]["raw_child_evidence_image_id"],
            shard["images"][0]["id"],
        )
        self.assertEqual(observations[0]["evidence_image_id"], image["id"])
        self.assertEqual(observations[0]["image_id"], image["id"])
        self.assertEqual(observations[0]["source_id"], image["source_id"])
        self.assertEqual(observations[0]["image_url"], image["image_url"])
        self.assertEqual(observations[0]["page_url"], image["page_url"])
        self.assertEqual(observations[0]["origin"], image["origin"])
        self.assertEqual(observations[0]["mime_type"], image["mime_type"])
        self.assertEqual(observations[0]["width"], image["width"])
        self.assertEqual(observations[0]["height"], image["height"])
        self.assertEqual(observations[0]["visual_tasks"], image["visual_tasks"])
        self.assertEqual(observations[0]["analysis_status"], image["analysis_status"])
        self.assertNotIn("local_artifact_path", observations[0])
        self.assertEqual(
            observations[0]["raw_child_local_artifact_path"],
            image["local_artifact_path"],
        )
        ingest_status = ingest_vision_observations(run=run_dir, provider="codex-interactive")
        self.assertEqual(ingest_status["status"], "visual_evidence_ingested", ingest_status)
        self.assertEqual(ingest_status["images_ingested"], 1)
        minimums = visual_minimums_for_run(run_dir, required_vlm_images=1)
        self.assertEqual(minimums["vlm_images_analyzed"], 1)
        materialization_diff = self.load_json(run_dir / "semantic_materialization_diff.json")
        self.assertNotIn(
            "visual_search_plan",
            materialization_diff["missing_required_artifacts"],
        )
        self.assertNotIn(
            "visual_candidates",
            materialization_diff["missing_required_artifacts"],
        )
        self.assertNotIn(
            "image_fetch_status",
            materialization_diff["missing_required_artifacts"],
        )
        image_check = next(
            check
            for check in materialization_diff["artifact_checks"]
            if check["artifact"] == "evidence.images"
        )
        self.assertEqual(image_check["lineage_failures"], [])

    def test_release_visual_merge_reconciles_child_vlm_to_existing_acquisition_lineage_for_report(self) -> None:
        prepared = prepare_run(
            question="Release validation visual acquisition lineage fixture.",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            prompt_id="pb-visual-001",
            suite_id="issue-133-suite",
        )
        run_dir = Path(prepared["run_dir"])
        plan_research_tasks(run=run_dir, min_tasks=1)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        task = tasks_artifact["tasks"][0]
        task["state"] = "completed"
        task["last_adapter"] = "codex-exec"
        shard_path = run_dir / task["output_shard_path"]
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        shard = self.shard(run_dir, task)
        shard["claims"][0]["promotion_status"] = "eligible"
        image = shard["images"][0]
        (run_dir / "images").mkdir(exist_ok=True)
        metadata_path = run_dir / "images" / "child-metadata.json"
        metadata_path.write_text(
            json.dumps({"kind": "metadata-only child visual record"}, sort_keys=True),
            encoding="utf-8",
        )
        fetched_path = run_dir / "images" / "root-fetched.png"
        fetched_bytes = b"\x89PNG\r\n\x1a\nroot-fetched-visual-artifact"
        fetched_path.write_bytes(fetched_bytes)
        image["local_artifact_path"] = "images/child-metadata.json"
        image["mime_type"] = "image/jpeg"
        image.pop("estimated_cost_usd", None)
        image.pop("actual_cost_usd", None)
        canonical_plan_id = "plan_existing_visual_acquisition"
        canonical_task_id = task["id"]
        canonical_candidate_id = "cand_existing_visual_acquisition"
        canonical_fetch_id = "fetch_existing_visual_acquisition"
        self.write_json(shard_path, shard)
        self.write_jsonl(
            shard_path.parent / "search_results.jsonl",
            [self.release_search_result(task)],
        )
        self.write_jsonl(
            shard_path.parent / "visual_observations.jsonl",
            [
                {
                    "image_id": image["id"],
                    "evidence_image_id": image["id"],
                    "candidate_id": "child_candidate_unmatched",
                    "fetch_id": "child_fetch_unmatched",
                    "plan_id": "child_plan_unmatched",
                    "task_id": "child_task_unmatched",
                    "angle_id": "child_angle_unmatched",
                    "route": "visual_optional",
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "analysis_provider": "codex-interactive",
                    "codex_interactive_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "observation_status": "analyzed",
                    "observations": ["A child shard visual observation."],
                    "inferences": ["The image directly supports the visual claim."],
                    "policy_decision": "allowed",
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "codex_interactive_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            ],
        )
        self.write_json(
            run_dir / "visual_search_plan.json",
            {
                "schema_version": "codex-deepresearch.parallel.v0",
                "run_id": run_dir.name,
                "created_at": "2026-06-23T00:00:00Z",
                "status": "completed",
                "provider": "codex-native",
                "provider_mode": "real",
                "tasks": [
                    {
                        "plan_id": canonical_plan_id,
                        "task_id": canonical_task_id,
                        "semantic_plan_task_id": canonical_task_id,
                        "angle_id": task["angle_id"],
                        "route": "text_only",
                        "target_evidence_type": "image",
                        "query": task["query"],
                        "providers": ["codex-native"],
                        "state": "completed",
                        "provider": "codex-native",
                        "provider_mode": "real",
                    }
                ],
            },
        )
        self.write_jsonl(
            run_dir / "visual_candidates.jsonl",
            [
                {
                    "candidate_id": canonical_candidate_id,
                    "evidence_image_id": image["id"],
                    "image_id": image["id"],
                    "source_id": image["source_id"],
                    "page_url": image["page_url"],
                    "image_url": image["image_url"],
                    "origin": image["origin"],
                    "local_artifact_path": "images/root-fetched.png",
                    "candidate_status": "selected",
                    "rank": 1,
                    "score": 1.0,
                    "provider": "codex-native",
                    "provider_kind": "web_image_search",
                    "provider_mode": "real",
                    "codex_native_handoff": True,
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "plan_id": canonical_plan_id,
                    "task_id": canonical_task_id,
                    "angle_id": task["angle_id"],
                    "route": "text_only",
                    "estimated_cost_usd": 0.123,
                    "actual_cost_usd": 0.045,
                    "provider_provenance": {
                        "provider": "codex-native",
                        "provider_kind": "web_image_search",
                        "provider_mode": "real",
                        "codex_native_handoff": True,
                        "external_network_call": False,
                    },
                }
            ],
        )
        self.write_jsonl(
            run_dir / "image_fetch_status.jsonl",
            [
                {
                    "fetch_id": canonical_fetch_id,
                    "candidate_id": canonical_candidate_id,
                    "evidence_image_id": image["id"],
                    "image_id": image["id"],
                    "source_id": image["source_id"],
                    "page_url": image["page_url"],
                    "image_url": image["image_url"],
                    "local_artifact_path": "images/root-fetched.png",
                    "mime_type": "image/png",
                    "byte_size": len(fetched_bytes),
                    "width": image["width"],
                    "height": image["height"],
                    "hash": "sha256:root-fetched",
                    "phash": "root-phash",
                    "fetch_status": "fetched",
                    "retrieval_status": "fetched",
                    "provider": "codex-native",
                    "provider_kind": "web_image_search",
                    "provider_mode": "real",
                    "codex_native_handoff": True,
                    "policy_decision": "allowed",
                    "policy_flags": [],
                    "plan_id": canonical_plan_id,
                    "task_id": canonical_task_id,
                    "angle_id": task["angle_id"],
                    "route": "text_only",
                    "estimated_cost_usd": 0.123,
                    "actual_cost_usd": 0.045,
                    "provider_provenance": {
                        "provider": "codex-native",
                        "provider_kind": "web_image_search",
                        "provider_mode": "real",
                        "codex_native_handoff": True,
                        "external_network_call": False,
                    },
                }
            ],
        )
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)
        self.assertEqual(merge["status"], "completed", merge)
        merge_observations = self.load_jsonl(run_dir / "visual_observations.jsonl")
        self.assertEqual(merge_observations[0]["candidate_id"], canonical_candidate_id)
        self.assertEqual(merge_observations[0]["fetch_id"], canonical_fetch_id)
        self.assertEqual(merge_observations[0]["plan_id"], canonical_plan_id)
        self.assertEqual(merge_observations[0]["task_id"], canonical_task_id)
        self.assertEqual(merge_observations[0]["route"], task["route"])
        self.assertEqual(merge_observations[0]["local_artifact_path"], "images/root-fetched.png")
        self.assertEqual(
            merge_observations[0]["raw_child_local_artifact_path"],
            "images/child-metadata.json",
        )
        post_merge_image = self.load_json(run_dir / "evidence.json")["images"][0]
        self.assertEqual(post_merge_image["local_artifact_path"], "images/root-fetched.png")
        self.assertEqual(
            post_merge_image["raw_child_local_artifact_path"],
            "images/child-metadata.json",
        )
        self.assertEqual(post_merge_image["estimated_cost_usd"], 0.123)
        self.assertEqual(post_merge_image["actual_cost_usd"], 0.045)
        self.assertEqual(
            merge_observations[0]["raw_child_candidate_id"],
            "child_candidate_unmatched",
        )
        self.assertEqual(merge_observations[0]["raw_child_fetch_id"], "child_fetch_unmatched")
        ingest_status = ingest_vision_observations(run=run_dir, provider="codex-interactive")
        self.assertEqual(ingest_status["status"], "visual_evidence_ingested", ingest_status)
        report_status = synthesize_report(run=run_dir)
        self.assertEqual(report_status["status"], "completed", report_status)

        evidence = self.load_json(run_dir / "evidence.json")
        merged_image = evidence["images"][0]
        self.assertEqual(merged_image["candidate_id"], canonical_candidate_id)
        self.assertEqual(merged_image["fetch_id"], canonical_fetch_id)
        self.assertEqual(merged_image["plan_id"], canonical_plan_id)
        self.assertEqual(merged_image["task_id"], canonical_task_id)
        self.assertEqual(merged_image["route"], task["route"])
        self.assertEqual(merged_image["local_artifact_path"], "images/root-fetched.png")
        self.assertEqual(merged_image["mime_type"], "image/png")
        self.assertEqual(merged_image["estimated_cost_usd"], 0.123)
        self.assertEqual(merged_image["actual_cost_usd"], 0.045)
        self.assertEqual(
            self.load_json(run_dir / "visual_search_plan.json")["tasks"][0]["route"],
            task["route"],
        )
        self.assertEqual(
            self.load_jsonl(run_dir / "visual_candidates.jsonl")[0]["route"],
            task["route"],
        )
        observations = self.load_jsonl(run_dir / "visual_observations.jsonl")
        self.assertEqual(observations[0]["candidate_id"], canonical_candidate_id)
        self.assertEqual(observations[0]["fetch_id"], canonical_fetch_id)
        self.assertEqual(observations[0]["plan_id"], canonical_plan_id)
        self.assertEqual(observations[0]["task_id"], canonical_task_id)
        self.assertEqual(observations[0]["local_artifact_path"], "images/root-fetched.png")
        included_claim = next(
            claim
            for claim in report_status["included_claims"]
            if image["id"] in claim["image_ids"]
        )
        support = next(
            support
            for support in included_claim["visual_supports"]
            if support["image_id"] == image["id"]
        )
        self.assertEqual(support["candidate_id"], canonical_candidate_id)
        self.assertEqual(support["fetch_id"], canonical_fetch_id)
        self.assertEqual(support["plan_id"], canonical_plan_id)
        report_text = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn(included_claim["claim_id"], report_text)
        self.assertIn(image["id"], report_text)
        minimums = visual_minimums_for_run(run_dir, required_vlm_images=1)
        self.assertEqual(minimums["vlm_images_analyzed"], 1)
        self.assertEqual(minimums["report_cited_images"], 1)
        self.assertTrue(minimums["satisfied"], minimums)

    def test_release_visual_merge_normalizes_codex_interactive_external_vlm_mislabel(self) -> None:
        prepared = prepare_run(
            question="Release validation visual handoff external VLM mislabel fixture.",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            prompt_id="pb-visual-001",
            suite_id="issue-133-suite",
        )
        run_dir = Path(prepared["run_dir"])
        plan_research_tasks(run=run_dir, min_tasks=1)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        task = tasks_artifact["tasks"][0]
        task["state"] = "completed"
        task["last_adapter"] = "codex-exec"
        shard_path = run_dir / task["output_shard_path"]
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        shard = self.shard(run_dir, task)
        self.write_json(shard_path, shard)
        self.write_jsonl(
            shard_path.parent / "search_results.jsonl",
            [self.release_search_result(task)],
        )
        self.write_jsonl(
            shard_path.parent / "visual_observations.jsonl",
            [
                {
                    "image_id": shard["images"][0]["id"],
                    "evidence_image_id": shard["images"][0]["id"],
                    "candidate_id": "child_candidate_001",
                    "fetch_id": "child_fetch_001",
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "analysis_provider": "codex-interactive",
                    "codex_interactive_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "observation_status": "analyzed",
                    "observations": ["A child shard visual observation."],
                    "inferences": ["The image directly supports the visual claim."],
                    "policy_decision": "allowed",
                    "external_vlm_call": True,
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "codex_interactive_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": True,
                    },
                }
            ],
        )
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        self.assertEqual(merge["status"], "completed", merge)
        self.assertEqual(merge["failed_tasks"], [])
        observations = self.load_jsonl(run_dir / "visual_observations.jsonl")
        self.assertEqual(observations[0]["external_vlm_call"], False)
        self.assertTrue(observations[0]["child_reported_external_vlm_call"])
        self.assertTrue(observations[0]["external_vlm_call_normalized_by_parent"])
        provenance = observations[0]["provider_provenance"]
        self.assertEqual(provenance["external_vlm_call"], False)
        self.assertTrue(provenance["child_reported_external_vlm_call"])
        self.assertTrue(provenance["external_vlm_call_normalized_by_parent"])

    def test_release_visual_merge_rejects_image_shard_without_visual_observations(self) -> None:
        prepared = prepare_run(
            question="Release validation visual missing observations fixture.",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            prompt_id="pb-visual-002",
            suite_id="issue-133-suite",
        )
        run_dir = Path(prepared["run_dir"])
        plan_research_tasks(run=run_dir, min_tasks=1)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        task = tasks_artifact["tasks"][0]
        task["state"] = "completed"
        task["last_adapter"] = "codex-exec"
        shard_path = run_dir / task["output_shard_path"]
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(shard_path, self.shard(run_dir, task))
        self.write_jsonl(
            shard_path.parent / "search_results.jsonl",
            [self.release_search_result(task)],
        )
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        self.assertEqual(merge["status"], "completed", merge)
        self.assertEqual(merge["accepted_shards"], [])
        failed_task = merge["failed_tasks"][0]
        self.assertEqual(failed_task["failure_category"], "invalid_release_visual_handoff")
        self.assertEqual(
            failed_task["child_failure_code"],
            "codex_child_release_handoff_invalid",
        )
        self.assertEqual(
            failed_task["release_visual_handoff_validation"]["reason"],
            "missing_child_visual_observations",
        )
        self.assertIn(
            "missing_child_visual_observations",
            failed_task["diagnostic"],
        )
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["images"], [])

    def test_release_visual_merge_rejects_non_release_grade_observation(self) -> None:
        prepared = prepare_run(
            question="Release validation weak visual observation fixture.",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            prompt_id="pb-visual-003",
            suite_id="issue-133-suite",
        )
        run_dir = Path(prepared["run_dir"])
        plan_research_tasks(run=run_dir, min_tasks=1)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        task = tasks_artifact["tasks"][0]
        task["state"] = "completed"
        task["last_adapter"] = "codex-exec"
        shard_path = run_dir / task["output_shard_path"]
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        shard = self.shard(run_dir, task)
        self.write_json(shard_path, shard)
        self.write_jsonl(
            shard_path.parent / "search_results.jsonl",
            [self.release_search_result(task)],
        )
        self.write_jsonl(
            shard_path.parent / "visual_observations.jsonl",
            [
                {
                    "image_id": shard["images"][0]["id"],
                    "evidence_image_id": shard["images"][0]["id"],
                    "candidate_id": "child_candidate_weak",
                    "fetch_id": "child_fetch_weak",
                    "observation_text": "Weak observation without VLM provenance.",
                    "provider": "codex-native",
                }
            ],
        )
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        self.assertEqual(merge["accepted_shards"], [])
        failed_task = merge["failed_tasks"][0]
        self.assertEqual(failed_task["failure_category"], "invalid_release_visual_handoff")
        self.assertEqual(
            failed_task["release_visual_handoff_validation"]["reason"],
            "child_visual_observations_not_release_grade",
        )
        invalid_record = failed_task["release_visual_handoff_validation"]["invalid_records"][0]
        self.assertEqual(invalid_record["reason"], "provider_must_be_codex_interactive")

    def test_release_visual_merge_rejects_child_observation_missing_raw_lineage(self) -> None:
        prepared = prepare_run(
            question="Release validation missing visual lineage fixture.",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            prompt_id="pb-visual-004",
            suite_id="issue-133-suite",
        )
        run_dir = Path(prepared["run_dir"])
        plan_research_tasks(run=run_dir, min_tasks=1)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        task = tasks_artifact["tasks"][0]
        task["state"] = "completed"
        task["last_adapter"] = "codex-exec"
        shard_path = run_dir / task["output_shard_path"]
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        shard = self.shard(run_dir, task)
        self.write_json(shard_path, shard)
        self.write_jsonl(
            shard_path.parent / "search_results.jsonl",
            [self.release_search_result(task)],
        )
        self.write_jsonl(
            shard_path.parent / "visual_observations.jsonl",
            [
                {
                    "image_id": shard["images"][0]["id"],
                    "evidence_image_id": shard["images"][0]["id"],
                    "provider": "codex-interactive",
                    "provider_kind": "vlm",
                    "provider_mode": "real",
                    "analysis_provider": "codex-interactive",
                    "codex_interactive_handoff": True,
                    "handoff_artifact": "visual_observations.jsonl",
                    "observation_status": "analyzed",
                    "observations": ["A child shard visual observation."],
                    "inferences": ["The image directly supports the visual claim."],
                    "policy_decision": "allowed",
                    "provider_provenance": {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "codex_interactive_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "external_vlm_call": False,
                    },
                }
            ],
        )
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        self.assertEqual(merge["accepted_shards"], [])
        failed_task = merge["failed_tasks"][0]
        self.assertEqual(failed_task["failure_category"], "invalid_release_visual_handoff")
        self.assertEqual(
            failed_task["release_visual_handoff_validation"]["reason"],
            "child_visual_observations_not_release_grade",
        )
        invalid_record = failed_task["release_visual_handoff_validation"]["invalid_records"][0]
        self.assertEqual(invalid_record["reason"], "candidate_id_missing")

    def test_release_validation_invalid_child_search_sidecar_retry_exhaustion_fails(self) -> None:
        prepared = prepare_run(
            question="Release validation sidecar retry exhaustion fixture.",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
            prompt_id="pb-text-001",
            suite_id="issue-122-suite",
        )
        run_dir = Path(prepared["run_dir"])

        def fake_codex_exec(command, **_kwargs):
            task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
            shard_path = run_dir / task["output_shard_path"]
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_json(shard_path, self.shard(run_dir, task))
            record = self.release_search_result(task)
            record.pop("retrieval_status")
            self.write_jsonl(shard_path.parent / "search_results.jsonl", [record])
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"type":"message","status":"completed","message":"wrote shard"}\n',
                stderr="",
            )

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.random.uniform", return_value=0.0),
            mock.patch("deepresearch.parallel_orchestrator._sleep_for_retry", return_value=0.0) as sleep_mock,
            mock.patch(
                "deepresearch.parallel_orchestrator.subprocess.run",
                side_effect=fake_codex_exec,
            ) as run_mock,
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                codex_exec_timeout_seconds=120,
                min_tasks=1,
                max_tasks=1,
                allow_degraded=False,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_parallel_no_accepted_shards")
        self.assertEqual(run_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(5.0), mock.call(10.0)])
        self.assertEqual(result["merge"]["accepted_shards"], [])
        self.assertEqual((run_dir / "search_results.jsonl").read_text(encoding="utf-8"), "")
        task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
        self.assertEqual(task["state"], "failed")
        self.assertTrue(task["retry_exhausted"])
        self.assertEqual(task["retry_exhausted_reason"], "max_attempts_reached")
        self.assertEqual(task["failure_category"], "invalid_release_search_handoff")
        self.assertEqual(task["child_failure_code"], "codex_child_release_handoff_invalid")
        attempts = task["attempt_diagnostics"]
        self.assertEqual(
            [attempt["retry_decision"] for attempt in attempts],
            ["retry", "retry", "retry_exhausted"],
        )
        self.assertTrue(
            all(
                attempt["child_failure_code"] == "codex_child_release_handoff_invalid"
                for attempt in attempts
            )
        )
        failed_task = result["merge"]["failed_tasks"][0]
        self.assertIn(
            "missing_required_release_field:retrieval_status",
            failed_task["diagnostic"],
        )
        self.assertIn(
            "missing_required_release_field:retrieval_status",
            result["diagnostics"]["first_failed_diagnostic"],
        )
        self.assertEqual(result["retry_summary"]["retry_exhausted_count"], 1)

    def test_release_validation_partial_success_with_exhausted_handoff_is_non_passing(self) -> None:
        prepared = prepare_run(
            question="Release validation partial handoff exhaustion fixture.",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
            prompt_id="pb-text-001",
            suite_id="issue-122-suite",
        )
        run_dir = Path(prepared["run_dir"])
        attempts_by_task: dict[str, int] = {}

        class PartialReleaseCodexAdapter(CodexExecAdapter):
            name = "codex-exec"
            timeout_seconds = 120.0

            def available(inner_self) -> bool:
                return True

            def run_task(inner_self, task, *, run_dir, max_threads):
                task_id = str(task["id"])
                attempts_by_task[task_id] = attempts_by_task.get(task_id, 0) + 1
                shard_path = run_dir / task["output_shard_path"]
                shard_path.parent.mkdir(parents=True, exist_ok=True)
                self.write_json(shard_path, self.shard(run_dir, task))
                record = self.release_search_result(
                    task,
                    url=f"https://example.com/{task_id}/attempt-{attempts_by_task[task_id]}",
                )
                if task_id.endswith("002"):
                    record.pop("retrieval_status")
                self.write_jsonl(shard_path.parent / "search_results.jsonl", [record])
                return type("Result", (), {
                    "task_id": task_id,
                    "status": "completed",
                    "child_thread_id": f"codex-{task_id}-{attempts_by_task[task_id]}",
                    "events": (),
                    "shard_path": str(shard_path),
                    "failure_category": None,
                    "message": None,
                })()

        with (
            mock.patch("deepresearch.parallel_orchestrator.random.uniform", return_value=0.0),
            mock.patch("deepresearch.parallel_orchestrator._sleep_for_retry", return_value=0.0) as sleep_mock,
            mock.patch(
                "deepresearch.parallel_orchestrator._adapter",
                return_value=PartialReleaseCodexAdapter(),
            ),
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                codex_exec_timeout_seconds=120,
                min_tasks=2,
                max_tasks=2,
                allow_degraded=False,
            )

        self.assertEqual(attempts_by_task, {"task_research_001": 1, "task_research_002": 3})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_release_handoff_invalid")
        self.assertTrue(result["needs_serial_handoff"])
        self.assertFalse(result["parallel_degraded"])
        self.assertEqual(sleep_mock.call_args_list, [mock.call(5.0), mock.call(10.0)])
        self.assertEqual(len(result["merge"]["accepted_shards"]), 1)
        self.assertEqual(result["merge"]["accepted_shards"][0]["task_id"], "task_research_001")
        self.assertEqual(result["failure_counts"]["failed_tasks"], 1)
        self.assertEqual(
            result["failure_counts"]["by_category"]["invalid_release_search_handoff"],
            1,
        )
        failed_task = result["merge"]["failed_tasks"][0]
        self.assertEqual(failed_task["task_id"], "task_research_002")
        self.assertEqual(failed_task["child_failure_code"], "codex_child_release_handoff_invalid")
        self.assertFalse(failed_task["retryable"])
        self.assertIn("missing_required_release_field:retrieval_status", failed_task["diagnostic"])
        self.assertEqual(result["retry_summary"]["retry_exhausted_count"], 1)
        self.assertEqual(
            result["retry_summary"]["child_failure_counts"],
            {"codex_child_release_handoff_invalid": 3},
        )
        self.assertEqual(
            result["diagnostics"]["actionable_cause"],
            "release validation search handoff retries exhausted",
        )
        self.assertEqual(
            result["diagnostics"]["first_child_failure_code"],
            "codex_child_release_handoff_invalid",
        )
        records = [
            json.loads(line)
            for line in (run_dir / "search_results.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["task_id"], "task_search_001")
        self.assertEqual(records[0]["retrieval_status"], "fetched")

    def test_passing_partial_parallel_exposes_stable_reason_summary(self) -> None:
        run_dir = self.prepare()

        class PartialCodexAdapter:
            name = "codex-exec"

            def run_task(inner_self, task, *, run_dir, max_threads):
                task_id = str(task["id"])
                if task_id.endswith("002"):
                    return type("Result", (), {
                        "task_id": task_id,
                        "status": "failed",
                        "child_thread_id": f"codex-{task_id}",
                        "events": (),
                        "shard_path": None,
                        "failure_category": "invalid_shard",
                        "message": "synthetic public-safe invalid shard",
                    })()
                shard_path = run_dir / task["output_shard_path"]
                shard_path.parent.mkdir(parents=True, exist_ok=True)
                self.write_json(shard_path, self.shard(run_dir, task))
                return type("Result", (), {
                    "task_id": task_id,
                    "status": "completed",
                    "child_thread_id": f"codex-{task_id}",
                    "events": (),
                    "shard_path": str(shard_path),
                    "failure_category": None,
                    "message": None,
                })()

        with mock.patch(
            "deepresearch.parallel_orchestrator._adapter",
            return_value=PartialCodexAdapter(),
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                min_tasks=2,
                max_tasks=2,
                allow_degraded=False,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed_partial_parallel")
        self.assertFalse(result["needs_serial_handoff"])
        summary = result["partial_parallel_summary"]
        self.assertTrue(summary["partial"])
        self.assertEqual(summary["reason_category"], "failed_tasks")
        self.assertEqual(summary["accepted_shard_count"], 1)
        self.assertEqual(summary["omitted_task_count"], 1)
        self.assertEqual(summary["failed_task_count"], 1)
        self.assertEqual(result["partial_reason_category"], "failed_tasks")
        merge = self.load_json(run_dir / "merge_status.json")
        self.assertEqual(
            merge["partial_parallel_summary"]["reason_category"],
            "failed_tasks",
        )
        self.assertNotEqual(
            merge["partial_parallel_summary"]["reason_category"],
            "no_accepted_shards",
        )

    def test_codex_exec_adapter_runs_from_project_root_and_reports_trust_errors(self) -> None:
        run_dir = self.prepare()
        task = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"][0]
        stderr = "Not inside a trusted directory and --skip-git-repo-check was not specified."
        adapter = CodexExecAdapter(project_root=ROOT, timeout_seconds=12)

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.subprocess.run") as run_mock,
        ):
            run_mock.return_value = subprocess.CompletedProcess(
                args=["codex"],
                returncode=1,
                stdout='{"type":"message","status":"error","message":"trust failed"}\n',
                stderr=stderr,
            )

            result = adapter.run_task(task, run_dir=run_dir, max_threads=3)

        command = run_mock.call_args.args[0]
        kwargs = run_mock.call_args.kwargs
        self.assertEqual(kwargs["cwd"], ROOT)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(command[command.index("-C") + 1], str(ROOT))
        self.assertEqual(command[command.index("--add-dir") + 1], str(run_dir.resolve()))
        self.assertNotIn("--skip-git-repo-check", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn(str(run_dir / task["output_shard_path"]), command[-1])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_category, "codex_exec_failed")
        self.assertIsNotNone(result.message)
        assert result.message is not None
        self.assertIn(stderr, result.message)
        self.assertIn(f"cwd={ROOT}", result.message)
        self.assertIn(f"run_dir={run_dir.resolve()}", result.message)
        self.assertIn("repo_check_bypass_used=False", result.message)
        self.assertIn("do not count --skip-git-repo-check bypass runs", result.message)
        spawn_context = result.events[0]["raw_event"]
        self.assertEqual(spawn_context["trusted_project_root"], str(ROOT))
        self.assertEqual(spawn_context["run_dir"], str(run_dir.resolve()))
        self.assertFalse(spawn_context["repo_check_bypass_used"])

    def test_codex_exec_nonzero_persists_child_diagnostics(self) -> None:
        run_dir = self.prepare()
        task = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"][0]
        adapter = CodexExecAdapter(project_root=ROOT, timeout_seconds=12)
        child_events = [
            {
                "type": "tool_call",
                "status": "started",
                "tool_name": "shell",
                "call_id": "call_001",
                "command": "python3 -m unittest tests.test_parallel_orchestrator",
            },
            {
                "type": "item.completed",
                "status": "failed",
                "item": {
                    "type": "tool_call",
                    "name": "shell",
                    "call_id": "call_001",
                    "arguments": {"cmd": "python3 -m unittest tests.test_parallel_orchestrator"},
                    "status": "failed",
                },
            },
        ]
        stdout = "\n".join(json.dumps(event, sort_keys=True) for event in child_events) + "\nnot-json\n"
        stderr = "child command failed"

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.subprocess.run") as run_mock,
        ):
            run_mock.return_value = subprocess.CompletedProcess(
                args=["codex"],
                returncode=17,
                stdout=stdout,
                stderr=stderr,
            )

            result = adapter.run_task(task, run_dir=run_dir, max_threads=3)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_category, "codex_exec_failed")
        artifacts = result.events[0]["raw_event"]["child_event_artifacts"]
        stdout_path = Path(artifacts["stdout_jsonl_path"])
        stderr_path = Path(artifacts["stderr_path"])
        last_event_path = Path(artifacts["last_child_event_path"])
        self.assertEqual(
            stdout_path.relative_to(run_dir),
            Path("child_events") / task["id"] / "codex_exec_stdout.jsonl",
        )
        self.assertEqual(stdout_path.read_text(encoding="utf-8"), stdout)
        self.assertEqual(stderr_path.read_text(encoding="utf-8"), stderr)
        summary = self.load_json(last_event_path)
        self.assertEqual(summary["returncode"], 17)
        self.assertFalse(summary["timeout"])
        self.assertEqual(summary["total_json_events"], 2)
        self.assertEqual(summary["parse_errors"], 1)
        self.assertEqual(summary["last_event_type"], "item.completed")
        self.assertEqual(summary["last_item_type"], "tool_call")
        self.assertEqual(summary["last_command"], "python3 -m unittest tests.test_parallel_orchestrator")
        self.assertEqual(summary["last_command_status"], "failed")
        self.assertEqual(summary["last_tool_name"], "shell")
        self.assertEqual(summary["last_tool_call_id"], "call_001")
        self.assertEqual(result.events[1]["raw_event"]["child_event_artifacts"], artifacts)
        self.assertIn("child_event_artifacts=", result.message or "")

    def test_codex_exec_child_summary_extracts_function_call_names(self) -> None:
        run_dir = self.prepare()
        task = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"][0]
        adapter = CodexExecAdapter(project_root=ROOT, timeout_seconds=12)
        stdout = (
            json.dumps(
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call_function_001",
                    "arguments": {"cmd": "python3 -m unittest tests.test_parallel_orchestrator"},
                },
                sort_keys=True,
            )
            + "\n"
            + json.dumps(
                {
                    "type": "custom_tool_call",
                    "name": "view_image",
                    "call_id": "call_custom_001",
                },
                sort_keys=True,
            )
            + "\n"
        )

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.subprocess.run") as run_mock,
        ):
            run_mock.return_value = subprocess.CompletedProcess(
                args=["codex"],
                returncode=1,
                stdout=stdout,
                stderr="child command failed",
            )

            result = adapter.run_task(task, run_dir=run_dir, max_threads=3)

        artifacts = result.events[0]["raw_event"]["child_event_artifacts"]
        summary = self.load_json(Path(artifacts["last_child_event_path"]))
        self.assertEqual(summary["last_tool_name"], "view_image")
        self.assertEqual(summary["last_tool_call_id"], "call_custom_001")
        self.assertEqual(summary["last_tool_event_type"], "custom_tool_call")
        self.assertEqual(
            summary["last_command"],
            "python3 -m unittest tests.test_parallel_orchestrator",
        )

    def test_codex_exec_timeout_persists_partial_child_diagnostics(self) -> None:
        run_dir = self.prepare()
        task = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"][0]
        adapter = CodexExecAdapter(project_root=ROOT, timeout_seconds=12)
        stdout = json.dumps(
            {
                "type": "message",
                "status": "running",
                "message": "partial progress before timeout",
            },
            sort_keys=True,
        ) + "\n"

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.subprocess.run") as run_mock,
        ):
            run_mock.side_effect = subprocess.TimeoutExpired(
                cmd="codex",
                timeout=12,
                output=stdout,
                stderr=b"partial stderr",
            )

            result = adapter.run_task(task, run_dir=run_dir, max_threads=3)

        self.assertEqual(result.status, "failed")
        artifacts = result.events[0]["raw_event"]["child_event_artifacts"]
        self.assertEqual(Path(artifacts["stdout_jsonl_path"]).read_text(encoding="utf-8"), stdout)
        self.assertEqual(Path(artifacts["stderr_path"]).read_text(encoding="utf-8"), "partial stderr")
        summary = self.load_json(Path(artifacts["last_child_event_path"]))
        self.assertTrue(summary["timeout"])
        self.assertEqual(summary["timeout_seconds"], 12)
        self.assertIsNone(summary["returncode"])
        self.assertEqual(summary["total_json_events"], 1)
        self.assertEqual(summary["last_event_type"], "message")
        self.assertEqual(summary["last_message_text_preview"], "partial progress before timeout")
        wait_events = [event for event in result.events if event["event_type"] == "wait"]
        self.assertEqual(wait_events[-1]["raw_event"]["child_event_artifacts"], artifacts)

    def test_codex_exec_timeout_without_shard_records_attempt_probe(self) -> None:
        run_dir = self.prepare()
        stdout = json.dumps(
            {
                "type": "message",
                "status": "running",
                "message": "still collecting sources",
            },
            sort_keys=True,
        ) + "\n"

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.subprocess.run") as run_mock,
        ):
            run_mock.side_effect = subprocess.TimeoutExpired(
                cmd="codex",
                timeout=12,
                output=stdout,
                stderr="timeout stderr",
            )

            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                codex_exec_timeout_seconds=12,
                min_tasks=1,
                max_tasks=1,
                allow_degraded=False,
            )

        self.assertEqual(result["status"], "failed_parallel_no_accepted_shards")
        task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
        attempt = task["attempt_diagnostics"][0]
        self.assertTrue(attempt["timeout"])
        self.assertEqual(attempt["child_failure_code"], "codex_child_timeout")
        probe = attempt["attempt_probe"]
        self.assertEqual(probe["child_failure_code"], "codex_child_timeout")
        self.assertTrue(probe["timeout"])
        self.assertIsNotNone(probe["child_started_at"])
        self.assertIsNotNone(probe["child_timed_out_at"])
        self.assertEqual(probe["child_timeout_at"], probe["child_timed_out_at"])
        self.assertIsNone(probe["child_finished_at"])
        self.assertGreaterEqual(probe["elapsed_seconds"], 0)
        self.assertEqual(probe["child_elapsed_seconds"], probe["elapsed_seconds"])
        self.assertEqual(probe["timeout_seconds"], 12)
        self.assertFalse(probe["shard_exists"])
        self.assertIsNone(probe["shard_exists_at_timeout"])
        self.assertTrue(probe["parent_probe_after_timeout"])
        self.assertIsNone(probe["parent_probe_observed_shard_at"])
        self.assertIn(
            "no_direct_timeout_instant_shard_observation_available",
            probe["unobservable_reasons"]["shard_exists_at_timeout"],
        )
        self.assertFalse(probe["shard_parent_valid"])
        self.assertIn("run_id", probe["top_level_missing_fields"])
        self.assertEqual(probe["last_validation_result"]["state"], "invalid")
        self.assertIn("first_shard_observed_at", {item["field"] for item in probe["unknowns"]})
        self.assertEqual(
            probe["candidate_causes"][0]["cause"],
            "no_shard_observed_during_parent_probe_after_timeout",
        )
        self.assertEqual(probe["candidate_causes"][0]["confidence"], "medium")
        self.assertEqual(probe["candidate_cause_confidence"], "medium")
        self.assertIn("parent_probe_observed_shard_at", probe["candidate_cause_basis"])
        self.assertIn("search_results", probe["sidecars"])
        self.assertIn("search_results", probe["sidecar_status"])
        self.assertIn("visual_observations", probe["sidecars"])
        self.assertIn("verifier_votes", probe["sidecars"])
        self.assertIn("last_tool_or_command_call", probe["unobservable_reasons"])
        self.assertIsNone(probe["root_cause"])

    def test_codex_exec_timeout_with_legacy_invalid_shard_records_schema_probe(self) -> None:
        run_dir = self.prepare()

        def timeout_after_legacy_shard(command, **_kwargs):
            task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
            shard_path = run_dir / task["output_shard_path"]
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_json(
                shard_path,
                {
                    "schema_version": "codex-deepresearch.evidence-shard.v0",
                    "sources": [],
                },
            )
            raise subprocess.TimeoutExpired(
                cmd=command,
                timeout=12,
                output=json.dumps(
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": {"cmd": "OPENAI_API_KEY=FAKE_TEST_VALUE python3 write_shard.py --token FAKE_FLAG_VALUE"},
                    },
                    sort_keys=True,
                )
                + "\n",
                stderr="timeout after legacy shard",
            )

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch(
                "deepresearch.parallel_orchestrator.subprocess.run",
                side_effect=timeout_after_legacy_shard,
            ),
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                codex_exec_timeout_seconds=12,
                min_tasks=1,
                max_tasks=1,
                allow_degraded=False,
            )

        self.assertEqual(result["status"], "failed_parallel_no_accepted_shards")
        task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
        attempt = task["attempt_diagnostics"][0]
        self.assertTrue(attempt["timeout"])
        self.assertEqual(attempt["child_failure_code"], "codex_child_schema_invalid")
        probe = attempt["attempt_probe"]
        self.assertTrue(probe["timeout"])
        self.assertEqual(probe["child_failure_code"], "codex_child_schema_invalid")
        self.assertTrue(probe["shard_exists"])
        self.assertIsNone(probe["shard_exists_at_timeout"])
        self.assertTrue(probe["parent_probe_after_timeout"])
        self.assertIsNotNone(probe["parent_probe_observed_shard_at"])
        self.assertIsNotNone(probe["parent_probe_validation_attempt_at"])
        self.assertIsNone(probe["parent_probe_validated_shard_at"])
        self.assertEqual(
            probe["shard_schema_version"],
            "codex-deepresearch.evidence-shard.v0",
        )
        self.assertFalse(probe["shard_parent_valid"])
        self.assertIn("run_id", probe["missing_required_fields"])
        self.assertIn("run_id", probe["top_level_missing_fields"])
        self.assertEqual(probe["last_tool_or_command_kind"], "command")
        self.assertIn("python3 write_shard.py", probe["last_tool_or_command_preview"])
        self.assertNotIn("FAKE_TEST_VALUE", probe["last_tool_or_command_preview"])
        self.assertNotIn("FAKE_FLAG_VALUE", probe["last_tool_or_command_preview"])
        self.assertIn("<redacted-secret>", probe["last_tool_or_command_preview"])
        self.assertEqual(probe["last_tool_or_command_call"]["kind"], "command")
        self.assertEqual(
            probe["candidate_causes"][0]["cause"],
            "invalid_shard_observed_during_parent_probe_after_timeout",
        )
        self.assertEqual(probe["candidate_causes"][0]["confidence"], "medium")
        self.assertEqual(probe["candidate_cause_confidence"], "medium")
        self.assertIn("top_level_missing_fields", probe["candidate_cause_basis"])
        failed_task = result["merge"]["failed_tasks"][0]
        self.assertEqual(failed_task["child_failure_code"], "codex_child_schema_invalid")
        self.assertEqual(
            failed_task["attempt_diagnostics"][0]["attempt_probe"]["last_validation_result"]["state"],
            "invalid",
        )

    def test_codex_exec_context_includes_child_artifacts_without_prompt_leak(self) -> None:
        run_dir = self.prepare()
        task = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"][0]
        task["query"] = "private prompt text that must stay redacted"
        adapter = CodexExecAdapter(project_root=ROOT, timeout_seconds=12)
        stdout = json.dumps({"type": "message", "message": "child done"}, sort_keys=True) + "\n"

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.subprocess.run") as run_mock,
        ):
            run_mock.return_value = subprocess.CompletedProcess(
                args=["codex"],
                returncode=1,
                stdout=stdout,
                stderr="failed",
            )

            result = adapter.run_task(task, run_dir=run_dir, max_threads=3)

        spawn_context = result.events[0]["raw_event"]
        wait_context = [event for event in result.events if event["event_type"] == "wait"][-1]["raw_event"]
        parsed_raw_event = result.events[1]["raw_event"]
        self.assertEqual(spawn_context["command"][-1], "<prompt>")
        self.assertIn("<prompt>", spawn_context["command_string"])
        self.assertNotIn(task["query"], json.dumps(spawn_context, sort_keys=True))
        self.assertNotIn(task["query"], json.dumps(wait_context, sort_keys=True))
        self.assertEqual(
            parsed_raw_event["child_event_artifacts"],
            spawn_context["child_event_artifacts"],
        )
        for artifact_path in spawn_context["child_event_artifacts"].values():
            self.assertTrue(Path(artifact_path).is_relative_to(run_dir))

    def test_codex_exec_timeout_after_valid_shard_keeps_completed_output(self) -> None:
        run_dir = self.prepare()
        task = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"][0]
        shard_path = run_dir / task["output_shard_path"]
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(shard_path, self.shard(run_dir, task))
        adapter = CodexExecAdapter(project_root=ROOT, timeout_seconds=12)

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.subprocess.run") as run_mock,
        ):
            run_mock.side_effect = subprocess.TimeoutExpired(
                cmd="codex",
                timeout=12,
                output='{"type":"thread.started"}\n',
                stderr="Auth(AuthorizationRequired)",
            )

            result = adapter.run_task(task, run_dir=run_dir, max_threads=3)

        self.assertEqual(run_mock.call_args.kwargs["cwd"], ROOT)
        self.assertEqual(run_mock.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(result.status, "completed")
        self.assertIsNone(result.failure_category)
        self.assertEqual(result.shard_path, str(shard_path))
        wait_events = [event for event in result.events if event["event_type"] == "wait"]
        self.assertEqual(wait_events[-1]["child_status"], "completed")
        self.assertIn("timed out after writing a valid shard", wait_events[-1]["child_message"])
        timeout_context = wait_events[-1]["raw_event"]
        self.assertTrue(timeout_context["timeout_after_valid_shard"])
        self.assertTrue(timeout_context["valid_evidence_shard_exists"])
        self.assertEqual(timeout_context["valid_evidence_shard_path"], str(shard_path))
        self.assertEqual(
            timeout_context["missing_expected_sidecars"],
            ["search_results.jsonl", "visual_observations.jsonl", "verifier_votes.jsonl"],
        )
        self.assertEqual(
            sorted(timeout_context["expected_sidecars"].keys()),
            ["search_results", "verifier_votes", "visual_observations"],
        )
        self.assertFalse(timeout_context["expected_sidecars"]["search_results"]["exists"])
        self.assertIn("missing_expected_sidecars", wait_events[-1]["child_message"])
        self.assertIn("search_results.jsonl", result.message or "")

    def test_timeout_after_valid_shard_sidecars_surface_in_final_status(self) -> None:
        run_dir = self.prepare()
        task = plan_research_tasks(run=run_dir, min_tasks=1)["tasks"][0]
        shard_path = run_dir / task["output_shard_path"]
        expected_missing = [
            "search_results.jsonl",
            "visual_observations.jsonl",
            "verifier_votes.jsonl",
        ]

        def timeout_after_writing_valid_shard(*args, **kwargs):
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_json(shard_path, self.shard(run_dir, task))
            raise subprocess.TimeoutExpired(
                cmd=args[0],
                timeout=12,
                output='{"type":"thread.started"}\n',
                stderr="Auth(AuthorizationRequired)",
            )

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch(
                "deepresearch.parallel_orchestrator.subprocess.run",
                side_effect=timeout_after_writing_valid_shard,
            ),
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                codex_exec_timeout_seconds=12,
                min_tasks=1,
                allow_degraded=False,
            )

        self.assertEqual(result["status"], "completed_parallel")
        accepted = result["merge"]["accepted_shards"][0]
        diagnostics = accepted["diagnostics"]
        self.assertTrue(diagnostics["timeout_after_valid_shard"])
        self.assertTrue(diagnostics["valid_evidence_shard_exists"])
        self.assertEqual(diagnostics["missing_expected_sidecars"], expected_missing)
        self.assertEqual(
            sorted(diagnostics["expected_sidecars"].keys()),
            ["search_results", "verifier_votes", "visual_observations"],
        )

        merge_diagnostics = result["merge"]["diagnostics"]
        self.assertEqual(result["diagnostics"], merge_diagnostics)
        self.assertEqual(merge_diagnostics["accepted_shard_warning_count"], 1)
        warning = merge_diagnostics["accepted_shard_warnings"][0]
        self.assertEqual(warning["task_id"], task["id"])
        self.assertEqual(warning["warning"], "timeout_after_valid_shard")
        self.assertEqual(warning["missing_expected_sidecars"], expected_missing)

        merge_status = self.load_json(run_dir / "merge_status.json")
        orchestration_status = self.load_json(run_dir / "parallel_orchestration_status.json")
        self.assertEqual(
            merge_status["accepted_shards"][0]["diagnostics"]["missing_expected_sidecars"],
            expected_missing,
        )
        self.assertEqual(
            orchestration_status["merge"]["accepted_shards"][0]["diagnostics"][
                "missing_expected_sidecars"
            ],
            expected_missing,
        )
        self.assertEqual(
            orchestration_status["diagnostics"]["accepted_shard_warnings"][0][
                "missing_expected_sidecars"
            ],
            expected_missing,
        )
        task_record = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
        probe = task_record["attempt_diagnostics"][0]["attempt_probe"]
        self.assertTrue(probe["timeout"])
        self.assertTrue(probe["shard_exists"])
        self.assertIsNone(probe["shard_exists_at_timeout"])
        self.assertTrue(probe["parent_probe_after_timeout"])
        self.assertIsNotNone(probe["parent_probe_observed_shard_at"])
        self.assertIsNotNone(probe["parent_probe_validation_attempt_at"])
        self.assertIsNotNone(probe["parent_probe_validated_shard_at"])
        self.assertTrue(probe["shard_parent_valid"])
        self.assertTrue(probe["runner_recoverable_valid_shard"])
        self.assertEqual(probe["runner_recoverability"]["state"], "recoverable_valid_shard")
        self.assertIn("parent_probe_validated_shard_at", probe["runner_recoverability"]["basis"])
        self.assertIsNone(probe["first_parent_valid_shard_at"])
        self.assertEqual(
            probe["unobservable_reasons"]["first_parent_valid_shard_at"],
            "parent_valid_shard_was_observed_only_during_parent_probe_after_timeout",
        )
        self.assertEqual(
            probe["candidate_causes"][0]["cause"],
            "valid_shard_recoverable_during_parent_probe_after_timeout",
        )
        self.assertIn("parent_probe_validated_shard_at", probe["candidate_cause_basis"])
        self.assertEqual(
            result["merge"]["accepted_shards"][0]["diagnostics"]["attempt_diagnostics"][0][
                "attempt_probe"
            ]["runner_recoverable_valid_shard"],
            True,
        )

    def test_codex_exec_capacity_failure_retries_and_recovers_with_attempt_diagnostics(self) -> None:
        run_dir = self.prepare()
        capacity_message = "Selected model is at capacity. Please try a different model."
        call_count = 0

        def fake_codex_exec(command, **_kwargs):
            nonlocal call_count
            call_count += 1
            tasks = self.load_json(run_dir / "research_tasks.json")["tasks"]
            task = tasks[0]
            if call_count == 1:
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=1,
                    stdout=json.dumps(
                        {
                            "type": "message",
                            "status": "error",
                            "message": capacity_message,
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    stderr="",
                )
            shard_path = run_dir / task["output_shard_path"]
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_json(shard_path, self.shard(run_dir, task))
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"type":"message","status":"completed","message":"wrote shard"}\n',
                stderr="",
            )

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.random.uniform", return_value=0.0),
            mock.patch("deepresearch.parallel_orchestrator._sleep_for_retry", return_value=0.0) as sleep_mock,
            mock.patch(
                "deepresearch.parallel_orchestrator.subprocess.run",
                side_effect=fake_codex_exec,
            ) as run_mock,
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                codex_exec_timeout_seconds=120,
                min_tasks=1,
                max_tasks=1,
                allow_degraded=False,
            )

        self.assertEqual(result["status"], "completed_parallel")
        self.assertEqual(run_mock.call_count, 2)
        sleep_mock.assert_called_once_with(5.0)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        task = tasks_artifact["tasks"][0]
        self.assertEqual(task["state"], "merged")
        self.assertEqual(task["attempt"], 2)
        self.assertEqual(task["max_attempts"], 3)
        attempts = task["attempt_diagnostics"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["attempt"], 1)
        self.assertEqual(attempts[0]["child_failure_code"], "codex_child_model_capacity")
        self.assertFalse(attempts[0]["timeout"])
        self.assertEqual(attempts[0]["returncode"], 1)
        self.assertEqual(attempts[0]["last_message_text_preview"], capacity_message)
        self.assertIn("attempt_001", attempts[0]["raw_child_event_artifacts"]["stdout_jsonl_path"])
        self.assertEqual(attempts[0]["computed_backoff_seconds"], 5.0)
        self.assertEqual(attempts[0]["actual_sleep_seconds"], 0.0)
        self.assertEqual(attempts[0]["retry_decision"], "retry")
        self.assertEqual(attempts[1]["attempt"], 2)
        self.assertIsNone(attempts[1]["child_failure_code"])
        self.assertEqual(attempts[1]["returncode"], 0)
        self.assertIn("attempt_002", attempts[1]["raw_child_event_artifacts"]["stdout_jsonl_path"])
        self.assertEqual(attempts[1]["retry_decision"], "do_not_retry")
        self.assertEqual(tasks_artifact["retry_summary"]["retry_count"], 1)
        self.assertEqual(tasks_artifact["retry_summary"]["recovered_after_capacity_count"], 1)

        accepted = result["merge"]["accepted_shards"][0]
        self.assertEqual(
            accepted["diagnostics"]["attempt_diagnostics"][0]["child_failure_code"],
            "codex_child_model_capacity",
        )
        self.assertTrue(accepted["diagnostics"]["retry_summary"]["recovered_after_capacity"])
        self.assertEqual(result["retry_summary"]["retry_count"], 1)
        self.assertEqual(result["diagnostics"]["recovered_after_capacity_count"], 1)
        status = self.load_json(run_dir / "parallel_orchestration_status.json")
        self.assertEqual(status["retry_summary"]["recovered_after_capacity_count"], 1)
        self.assertEqual(status["codex_exec_retry_policy"]["max_attempts"], 3)
        self.assertEqual(status["codex_exec_retry_policy"]["max_retry_elapsed_seconds"], 60.0)
        retry_trace_events = [
            record
            for record in read_trace_records(run_dir / "run_trace.jsonl")
            if record.get("event_type") == "retry_decision"
        ]
        self.assertEqual(
            [record["raw_event"]["retry_decision"] for record in retry_trace_events],
            ["retry", "do_not_retry"],
        )
        self.assertEqual(retry_trace_events[0]["raw_event"]["computed_backoff_seconds"], 5.0)
        self.assertEqual(retry_trace_events[0]["raw_event"]["actual_sleep_seconds"], 0.0)
        self.assertEqual(retry_trace_events[0]["raw_event"]["child_failure_code"], "codex_child_model_capacity")
        self.assertEqual(retry_trace_events[1]["raw_event"]["returncode"], 0)
        self.assertTrue(validate_trace_file(run_dir / "run_trace.jsonl").valid)

    def test_codex_exec_schema_invalid_shard_retries_and_recovers(self) -> None:
        run_dir = self.prepare()
        call_count = 0

        def fake_codex_exec(command, **_kwargs):
            nonlocal call_count
            call_count += 1
            tasks = self.load_json(run_dir / "research_tasks.json")["tasks"]
            task = tasks[0]
            shard_path = run_dir / task["output_shard_path"]
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            shard = self.shard(run_dir, task)
            if call_count == 1:
                shard["images"][0].pop("policy_flags", None)
            self.write_json(shard_path, shard)
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"type":"message","status":"completed","message":"wrote shard"}\n',
                stderr="",
            )

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.random.uniform", return_value=0.0),
            mock.patch("deepresearch.parallel_orchestrator._sleep_for_retry", return_value=0.0),
            mock.patch(
                "deepresearch.parallel_orchestrator.subprocess.run",
                side_effect=fake_codex_exec,
            ) as run_mock,
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                codex_exec_timeout_seconds=120,
                min_tasks=1,
                max_tasks=1,
                allow_degraded=False,
            )

        self.assertEqual(result["status"], "completed_parallel")
        self.assertEqual(run_mock.call_count, 2)
        task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
        self.assertEqual(task["state"], "merged")
        self.assertEqual(task["attempt"], 2)
        attempts = task["attempt_diagnostics"]
        self.assertEqual(attempts[0]["child_failure_code"], "codex_child_schema_invalid")
        self.assertEqual(attempts[0]["retry_decision"], "retry")
        self.assertIsNone(attempts[1]["child_failure_code"])
        self.assertEqual(attempts[1]["retry_decision"], "do_not_retry")

    def test_timeout_release_handoff_invalid_can_capacity_retry(self) -> None:
        task = {
            "id": "task_research_001",
            "attempt": 1,
            "max_attempts": 3,
            "state": "failed",
            "attempt_diagnostics": [
                {
                    "attempt": 1,
                    "max_attempts": 3,
                    "status": "failed",
                    "timeout": True,
                    "child_failure_code": "codex_child_release_handoff_invalid",
                    "failure_category": "invalid_release_search_handoff",
                    "release_search_handoff_validation": {
                        "reason": "missing_child_search_results",
                    },
                }
            ],
        }

        retry_plan = parallel_orchestrator._maybe_retry_capacity_failure(
            task,
            capacity_retry_policy={
                "max_attempts": 3,
                "initial_delay_seconds": 5.0,
                "backoff_multiplier": 2.0,
                "max_delay_seconds": 30.0,
                "jitter_ratio": 0.0,
                "max_retry_elapsed_seconds": 60.0,
            },
        )

        self.assertEqual(retry_plan["retry_decision"], "retry")
        self.assertTrue(retry_plan["should_retry"])
        self.assertEqual(retry_plan["computed_backoff_seconds"], 5.0)
        self.assertEqual(task["state"], "retryable")
        self.assertEqual(task["capacity_retry_computed_elapsed_seconds"], 5.0)
        self.assertEqual(task["attempt_diagnostics"][0]["retry_decision"], "retry")
        self.assertEqual(task["attempt_diagnostics"][0]["computed_backoff_seconds"], 5.0)

    def test_timeout_non_retry_child_failure_does_not_capacity_retry(self) -> None:
        task = {
            "id": "task_research_001",
            "attempt": 1,
            "max_attempts": 3,
            "state": "failed",
            "attempt_diagnostics": [
                {
                    "attempt": 1,
                    "max_attempts": 3,
                    "status": "failed",
                    "timeout": True,
                    "child_failure_code": "codex_child_sandbox_blocked",
                    "failure_category": "codex_child_sandbox_blocked",
                }
            ],
        }

        retry_plan = parallel_orchestrator._maybe_retry_capacity_failure(
            task,
            capacity_retry_policy={
                "max_attempts": 3,
                "initial_delay_seconds": 5.0,
                "backoff_multiplier": 2.0,
                "max_delay_seconds": 30.0,
                "jitter_ratio": 0.0,
                "max_retry_elapsed_seconds": 60.0,
            },
        )

        self.assertEqual(retry_plan, {"retry_decision": "do_not_retry", "should_retry": False})
        self.assertEqual(task["state"], "failed")
        self.assertNotIn("capacity_retry_computed_elapsed_seconds", task)
        self.assertEqual(task["attempt_diagnostics"][0]["retry_decision"], "do_not_retry")

    def test_codex_exec_non_capacity_child_failures_do_not_capacity_retry(self) -> None:
        cases = (
            ("auth", "Invalid auth credential; please login.", "codex_child_auth_blocked"),
            ("sandbox", "Sandbox approval blocked before child could write shard.", "codex_child_sandbox_blocked"),
            ("quota", "Quota exhausted for this account.", "codex_child_quota_exhausted"),
            ("billing", "Billing disabled for this workspace.", "codex_child_billing_disabled"),
            ("policy", "Policy blocked this request.", "codex_child_policy_blocked"),
            (
                "model-mismatch",
                "Model gpt-4.1-mini is not available for this account. Try a different model.",
                "codex_child_exec_failed",
            ),
        )
        for _name, message, expected_code in cases:
            with self.subTest(message=message):
                run_dir = self.prepare()

                def fake_codex_exec(command, **_kwargs):
                    return subprocess.CompletedProcess(
                        args=command,
                        returncode=1,
                        stdout=json.dumps(
                            {"type": "message", "status": "error", "message": message},
                            sort_keys=True,
                        )
                        + "\n",
                        stderr=message,
                    )

                with (
                    mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
                    mock.patch("deepresearch.parallel_orchestrator._sleep_for_retry", return_value=0.0) as sleep_mock,
                    mock.patch(
                        "deepresearch.parallel_orchestrator.subprocess.run",
                        side_effect=fake_codex_exec,
                    ) as run_mock,
                ):
                    result = run_parallel_orchestration(
                        run=run_dir,
                        adapter_name="codex-exec",
                        codex_exec_timeout_seconds=120,
                        min_tasks=1,
                        max_tasks=1,
                        allow_degraded=False,
                    )

                self.assertFalse(result["ok"])
                self.assertIn(
                    result["status"],
                    {"blocked_parallel_execution", "failed_parallel_no_accepted_shards"},
                )
                run_mock.assert_called_once()
                sleep_mock.assert_not_called()
                task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
                attempt = task["attempt_diagnostics"][0]
                self.assertEqual(task["state"], "failed")
                self.assertEqual(attempt["child_failure_code"], expected_code)
                self.assertEqual(attempt["retry_decision"], "do_not_retry")
                self.assertNotEqual(attempt["child_failure_code"], "codex_child_model_capacity")
                failed_task = result["merge"]["failed_tasks"][0]
                self.assertFalse(failed_task["retryable"])
                retry_trace_events = [
                    record
                    for record in read_trace_records(run_dir / "run_trace.jsonl")
                    if record.get("event_type") == "retry_decision"
                ]
                self.assertEqual(len(retry_trace_events), 1)
                self.assertEqual(retry_trace_events[0]["raw_event"]["retry_decision"], "do_not_retry")
                self.assertEqual(retry_trace_events[0]["raw_event"]["child_failure_code"], expected_code)

    def test_codex_exec_non_capacity_do_not_retry_does_not_reenter_degraded_serial_fallback(self) -> None:
        run_dir = self.prepare()
        message = "Sandbox approval blocked before child could write shard."

        def fake_codex_exec(command, **_kwargs):
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout=json.dumps(
                    {"type": "message", "status": "error", "message": message},
                    sort_keys=True,
                )
                + "\n",
                stderr=message,
            )

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator._sleep_for_retry", return_value=0.0) as sleep_mock,
            mock.patch(
                "deepresearch.parallel_orchestrator.subprocess.run",
                side_effect=fake_codex_exec,
            ) as run_mock,
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                codex_exec_timeout_seconds=120,
                min_tasks=1,
                max_tasks=1,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["parallel_degraded"])
        self.assertEqual(result["status"], "degraded_serial_handoff_required")
        run_mock.assert_called_once()
        sleep_mock.assert_not_called()
        task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
        self.assertEqual(task["attempt"], 1)
        self.assertEqual(task["state"], "failed")
        self.assertEqual(task["attempt_diagnostics"][0]["retry_decision"], "do_not_retry")
        self.assertEqual(task["attempt_diagnostics"][0]["child_failure_code"], "codex_child_sandbox_blocked")
        self.assertFalse(task["parallel_failure"]["retryable"])
        failed_task = result["merge"]["failed_tasks"][0]
        self.assertFalse(failed_task["retryable"])
        self.assertEqual(failed_task["attempt"], 1)

    def test_codex_exec_invalid_schema_child_failure_does_not_capacity_retry(self) -> None:
        run_dir = self.prepare()

        def fake_codex_exec(command, **_kwargs):
            task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
            shard_path = run_dir / task["output_shard_path"]
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_json(
                shard_path,
                {
                    "schema_version": "codex-deepresearch.evidence-shard.v0",
                    "sources": [],
                    "claims": [],
                },
            )
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"type":"message","status":"completed","message":"wrote invalid shard"}\n',
                stderr="",
            )

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator._sleep_for_retry", return_value=0.0) as sleep_mock,
            mock.patch(
                "deepresearch.parallel_orchestrator.subprocess.run",
                side_effect=fake_codex_exec,
            ) as run_mock,
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                codex_exec_timeout_seconds=120,
                min_tasks=1,
                max_tasks=1,
                allow_degraded=False,
            )

        self.assertEqual(result["status"], "failed_parallel_no_accepted_shards")
        run_mock.assert_called_once()
        sleep_mock.assert_not_called()
        task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
        self.assertEqual(task["state"], "failed")
        attempt = task["attempt_diagnostics"][0]
        self.assertEqual(attempt["child_failure_code"], "codex_child_schema_invalid")
        self.assertEqual(attempt["retry_decision"], "do_not_retry")
        self.assertFalse(attempt["timeout"])
        probe = attempt["attempt_probe"]
        self.assertFalse(probe["timeout"])
        self.assertTrue(probe["shard_exists"])
        self.assertIsNone(probe["shard_exists_at_timeout"])
        self.assertEqual(
            probe["shard_schema_version"],
            "codex-deepresearch.evidence-shard.v0",
        )
        self.assertFalse(probe["shard_parent_valid"])
        self.assertIn("shard_exists_at_timeout", probe["unobservable_reasons"])
        self.assertEqual(probe["child_failure_code"], "codex_child_schema_invalid")
        self.assertIn("run_id", probe["missing_required_fields"])
        self.assertIn("run_id", probe["top_level_missing_fields"])
        self.assertIn("mode", probe["missing_required_fields"])
        self.assertEqual(probe["last_validation_result"]["state"], "invalid")
        self.assertTrue(probe["candidate_causes"])
        self.assertFalse(result["merge"]["failed_tasks"][0]["retryable"])
        failed_task = result["merge"]["failed_tasks"][0]
        self.assertTrue(
            any(
                error["code"] == "invalid_enum"
                for error in failed_task["validation"]["errors"]
            ),
            failed_task["validation"],
        )

    def test_codex_exec_failed_child_with_invalid_legacy_shard_is_schema_invalid(self) -> None:
        run_dir = self.prepare()

        def fake_codex_exec(command, **_kwargs):
            task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
            shard_path = run_dir / task["output_shard_path"]
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_json(
                shard_path,
                {
                    "schema_version": "codex-deepresearch.evidence-shard.v0",
                    "sources": [],
                    "claims": [],
                },
            )
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout='{"type":"message","status":"error","message":"wrote invalid shard then failed"}\n',
                stderr="child exited after invalid shard",
            )

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator._sleep_for_retry", return_value=0.0) as sleep_mock,
            mock.patch(
                "deepresearch.parallel_orchestrator.subprocess.run",
                side_effect=fake_codex_exec,
            ) as run_mock,
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                codex_exec_timeout_seconds=120,
                min_tasks=1,
                max_tasks=1,
                allow_degraded=False,
            )

        self.assertEqual(result["status"], "failed_parallel_no_accepted_shards")
        run_mock.assert_called_once()
        sleep_mock.assert_not_called()
        task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
        self.assertEqual(task["state"], "failed")
        self.assertEqual(task["failure_category"], "invalid_shard")
        self.assertEqual(task["child_failure_code"], "codex_child_schema_invalid")
        attempt = task["attempt_diagnostics"][0]
        self.assertEqual(attempt["status"], "failed")
        self.assertEqual(attempt["failure_category"], "invalid_shard")
        self.assertEqual(attempt["child_failure_code"], "codex_child_schema_invalid")
        self.assertEqual(attempt["retry_decision"], "do_not_retry")
        self.assertFalse(attempt["timeout"])
        self.assertIn("validation", attempt)
        probe = attempt["attempt_probe"]
        self.assertFalse(probe["timeout"])
        self.assertTrue(probe["shard_exists"])
        self.assertIsNone(probe["shard_exists_at_timeout"])
        self.assertEqual(
            probe["shard_schema_version"],
            "codex-deepresearch.evidence-shard.v0",
        )
        self.assertFalse(probe["shard_parent_valid"])
        self.assertEqual(probe["child_failure_code"], "codex_child_schema_invalid")
        self.assertEqual(probe["candidate_causes"][0]["cause"], "child_shard_schema_invalid")
        self.assertEqual(probe["last_validation_result"]["state"], "invalid")
        self.assertTrue(
            any(
                error["code"] == "invalid_enum"
                for error in probe["validation_errors"]
            ),
            probe["validation_errors"],
        )
        failed_task = result["merge"]["failed_tasks"][0]
        self.assertEqual(failed_task["failure_category"], "invalid_shard")
        self.assertEqual(failed_task["child_failure_code"], "codex_child_schema_invalid")
        self.assertFalse(failed_task["retryable"])

    def test_codex_exec_capacity_retry_exhaustion_fails_without_timeout(self) -> None:
        run_dir = self.prepare()
        capacity_message = "Selected model is at capacity. Please try a different model."

        def fake_codex_exec(command, **_kwargs):
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout=json.dumps(
                    {"type": "message", "status": "error", "message": capacity_message},
                    sort_keys=True,
                )
                + "\n",
                stderr="",
            )

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.random.uniform", return_value=0.0),
            mock.patch("deepresearch.parallel_orchestrator._sleep_for_retry", return_value=0.0) as sleep_mock,
            mock.patch(
                "deepresearch.parallel_orchestrator.subprocess.run",
                side_effect=fake_codex_exec,
            ) as run_mock,
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                codex_exec_timeout_seconds=300,
                min_tasks=1,
                max_tasks=1,
                allow_degraded=False,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_parallel_no_accepted_shards")
        self.assertFalse(result["parallel_degraded"])
        self.assertEqual(run_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(5.0), mock.call(10.0)])
        task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
        self.assertEqual(task["state"], "failed")
        self.assertTrue(task["retry_exhausted"])
        self.assertEqual(task["retry_exhausted_reason"], "max_attempts_reached")
        self.assertEqual(task["failure_category"], "codex_child_model_capacity")
        self.assertEqual(task["child_failure_code"], "codex_child_model_capacity")
        attempts = task["attempt_diagnostics"]
        self.assertEqual([attempt["attempt"] for attempt in attempts], [1, 2, 3])
        self.assertEqual(
            [attempt["retry_decision"] for attempt in attempts],
            ["retry", "retry", "retry_exhausted"],
        )
        self.assertTrue(all(attempt["timeout"] is False for attempt in attempts))
        self.assertEqual(attempts[0]["computed_backoff_seconds"], 5.0)
        self.assertEqual(attempts[1]["computed_backoff_seconds"], 10.0)
        self.assertEqual(attempts[2]["computed_backoff_seconds"], None)

        failed_task = result["merge"]["failed_tasks"][0]
        self.assertEqual(failed_task["failure_category"], "codex_child_model_capacity")
        self.assertEqual(failed_task["child_failure_code"], "codex_child_model_capacity")
        self.assertFalse(failed_task["timeout"])
        self.assertEqual(failed_task["returncode"], 1)
        self.assertEqual(
            failed_task["attempt_diagnostics"][2]["retry_decision"],
            "retry_exhausted",
        )
        self.assertTrue(result["diagnostics"]["retry_exhausted"])
        self.assertEqual(result["retry_summary"]["retry_exhausted_count"], 1)
        self.assertEqual(result["retry_summary"]["capacity_failure_count"], 3)
        retry_trace_events = [
            record
            for record in read_trace_records(run_dir / "run_trace.jsonl")
            if record.get("event_type") == "retry_decision"
        ]
        self.assertEqual(
            [record["raw_event"]["retry_decision"] for record in retry_trace_events],
            ["retry", "retry", "retry_exhausted"],
        )
        self.assertEqual(
            retry_trace_events[2]["raw_event"]["retry_exhausted_reason"],
            "max_attempts_reached",
        )

    def test_codex_exec_capacity_retries_sleep_once_per_batch(self) -> None:
        run_dir = self.prepare()
        capacity_message = "Selected model is at capacity. Please try a different model."
        task_attempt_counts: dict[str, int] = {}

        def fake_codex_exec(command, **_kwargs):
            prompt = command[-1]
            match = re.search(r'"id": "([^"]+)"', prompt)
            self.assertIsNotNone(match)
            assert match is not None
            task_id = match.group(1)
            task_attempt_counts[task_id] = task_attempt_counts.get(task_id, 0) + 1
            tasks = self.load_json(run_dir / "research_tasks.json")["tasks"]
            task = next(item for item in tasks if item["id"] == task_id)
            if task_attempt_counts[task_id] == 1:
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=1,
                    stdout=json.dumps(
                        {"type": "message", "status": "error", "message": capacity_message},
                        sort_keys=True,
                    )
                    + "\n",
                    stderr="",
                )
            shard_path = run_dir / task["output_shard_path"]
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_json(shard_path, self.shard(run_dir, task))
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"type":"message","status":"completed","message":"wrote shard"}\n',
                stderr="",
            )

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch("deepresearch.parallel_orchestrator.random.uniform", return_value=0.0),
            mock.patch("deepresearch.parallel_orchestrator._sleep_for_retry", return_value=0.0) as sleep_mock,
            mock.patch(
                "deepresearch.parallel_orchestrator.subprocess.run",
                side_effect=fake_codex_exec,
            ) as run_mock,
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                codex_exec_timeout_seconds=120,
                min_tasks=2,
                max_tasks=2,
                allow_degraded=False,
            )

        self.assertEqual(result["status"], "completed_parallel")
        self.assertEqual(run_mock.call_count, 4)
        sleep_mock.assert_called_once_with(5.0)
        self.assertEqual(sorted(task_attempt_counts.values()), [2, 2])
        tasks = self.load_json(run_dir / "research_tasks.json")["tasks"]
        self.assertTrue(all(task["state"] == "merged" for task in tasks))

    def test_fixture_runner_records_codex_events_and_schema_valid_shards(self) -> None:
        run_dir = self.prepare(route="visual_optional")

        result = run_parallel_orchestration(run=run_dir, adapter_name="fixture", min_tasks=3)

        self.assertEqual(result["status"], "completed_fixture")
        self.assertFalse(result["parallel_degraded"])
        self.assertFalse(result["needs_serial_handoff"])
        self.assertEqual(result["adapter"], "fixture")
        self.assertEqual(result["evidence_source"]["type"], "fixture")
        self.assertTrue(result["evidence_source"]["fixture_only"])
        self.assertFalse(result["evidence_source"]["real_child_execution"])
        self.assertFalse(result["evidence_source"]["real_use_e2e_eligible"])
        self.assertEqual(result["evidence_source"]["accepted_shards"], 3)
        self.assertEqual(result["merge"]["evidence_source"]["type"], "fixture")
        self.assertEqual(result["max_scheduled_concurrency"], 8)
        self.assertTrue((run_dir / "subagent_assignments.jsonl").is_file())
        tasks = self.load_json(run_dir / "research_tasks.json")["tasks"]
        self.assertTrue(all(task["state"] == "merged" for task in tasks))
        for task in tasks:
            validation = validate_artifacts(evidence_path=run_dir / task["output_shard_path"])
            self.assertTrue(validation.valid, validation.to_dict())

        records = read_trace_records(run_dir / "run_trace.jsonl")
        event_types = [record.get("event_type") for record in records]
        self.assertIn("spawn_agent", event_types)
        self.assertIn("wait", event_types)
        self.assertIn("close_agent", event_types)
        self.assertTrue(any(record.get("child_thread_id") for record in records))
        self.assertTrue(validate_trace_file(run_dir / "run_trace.jsonl").valid)

        evidence_validation = validate_artifacts(evidence_path=run_dir / "evidence.json")
        self.assertTrue(evidence_validation.valid, evidence_validation.to_dict())
        state = inspect_run_state(run_dir)
        stages = {stage["stage"]: stage for stage in state["stages"]}
        self.assertEqual(stages["parallel_orchestration"]["status"], "completed")
        self.assertEqual(
            stages["parallel_orchestration"]["evidence_source"]["type"],
            "fixture",
        )
        self.assertEqual(stages["parallel_orchestration"]["stage_status"], "completed_fixture")
        self.assertFalse(stages["parallel_orchestration"]["parallel_degraded"])
        self.assertFalse(stages["parallel_orchestration"]["needs_serial_handoff"])
        self.assertEqual(stages["ingest"]["status"], "skipped")
        self.assertEqual(stages["fetch_claims"]["status"], "skipped")
        self.assertEqual(stages["ingest_vision"]["status"], "skipped")
        self.assertEqual(state["next_safe_stage"], "enforce_guardrails")

    def test_fixture_cli_smoke_materializes_non_release_tasks_after_blocked_prepare(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepare_result = subprocess.run(
            [
                str(RUNNER),
                "prepare",
                "Parallel orchestration validation",
                "--runs-dir",
                str(runs_dir),
                "--route",
                "text_only",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(prepare_result.returncode, 0, prepare_result.stderr)
        run_dir = Path(json.loads(prepare_result.stdout)["run_dir"])
        evidence_before = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence_before["search_tasks"], [])
        self.assertEqual(
            evidence_before["semantic_planner"]["status"],
            "blocked_semantic_planner_unavailable",
        )

        orchestrate_result = subprocess.run(
            [
                str(RUNNER),
                "orchestrate-parallel",
                "--run",
                str(run_dir),
                "--adapter",
                "fixture",
                "--min-tasks",
                "3",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(orchestrate_result.returncode, 0, orchestrate_result.stderr)
        payload = json.loads(orchestrate_result.stdout)
        self.assertEqual(payload["status"], "completed_fixture")
        self.assertEqual(payload["accepted_shard_count"], 3)
        self.assertTrue(payload["evidence_source"]["fixture_only"])
        self.assertFalse(payload["evidence_source"]["real_use_e2e_eligible"])
        evidence_after = self.load_json(run_dir / "evidence.json")
        self.assertFalse(evidence_after["semantic_planner"]["semantic_release_eligible"])
        fixture_materialization = evidence_after["semantic_planner"]["diagnostics"][
            "fixture_materialization"
        ]
        self.assertEqual(
            fixture_materialization["status"],
            "materialized_non_release_fixture_tasks",
        )
        self.assertEqual(len(evidence_after["search_tasks"]), 3)
        self.assertTrue(all(task["fixture_only"] for task in evidence_after["search_tasks"]))
        self.assertTrue(
            all(
                task["semantic_release_eligible"] is False
                for task in evidence_after["search_tasks"]
            )
        )
        search_tasks = self.load_json(run_dir / "search_tasks.json")
        self.assertTrue(search_tasks["fixture_only"])
        self.assertFalse(search_tasks["semantic_release_eligible"])
        materialization_diff = self.load_json(run_dir / "semantic_materialization_diff.json")
        self.assertFalse(materialization_diff["valid"])
        self.assertEqual(materialization_diff["status"], "failed")

    def test_codex_exec_success_status_is_completed_parallel(self) -> None:
        run_dir = self.prepare()

        class SuccessfulCodexAdapter:
            name = "codex-exec"

            def run_task(inner_self, task, *, run_dir, max_threads):
                shard_path = run_dir / task["output_shard_path"]
                shard_path.parent.mkdir(parents=True, exist_ok=True)
                self.write_json(shard_path, self.shard(run_dir, task))
                return type("Result", (), {
                    "task_id": task["id"],
                    "status": "completed",
                    "child_thread_id": f"codex-{task['id']}",
                    "events": (),
                    "shard_path": str(shard_path),
                    "failure_category": None,
                    "message": None,
                })()

        with mock.patch("deepresearch.parallel_orchestrator._adapter", return_value=SuccessfulCodexAdapter()):
            result = run_parallel_orchestration(run=run_dir, adapter_name="codex-exec", min_tasks=2)

        self.assertEqual(result["status"], "completed_parallel")
        self.assertEqual(result["adapter"], "codex-exec")
        self.assertFalse(result["parallel_degraded"])
        self.assertFalse(result["needs_serial_handoff"])
        self.assertEqual(result["evidence_source"]["type"], "real_child_execution")
        self.assertTrue(result["evidence_source"]["real_use_e2e_eligible"])
        self.assertEqual(result["failure_counts"]["failed_tasks"], 0)

    def test_codex_exec_visual_parallel_keeps_ingest_vision_runnable(self) -> None:
        run_dir = self.prepare(route="visual_required")

        class SuccessfulCodexAdapter:
            name = "codex-exec"

            def run_task(inner_self, task, *, run_dir, max_threads):
                shard_path = run_dir / task["output_shard_path"]
                shard_path.parent.mkdir(parents=True, exist_ok=True)
                self.write_json(shard_path, self.shard(run_dir, task))
                return type("Result", (), {
                    "task_id": task["id"],
                    "status": "completed",
                    "child_thread_id": f"codex-{task['id']}",
                    "events": (),
                    "shard_path": str(shard_path),
                    "failure_category": None,
                    "message": None,
                })()

        with mock.patch("deepresearch.parallel_orchestrator._adapter", return_value=SuccessfulCodexAdapter()):
            result = run_parallel_orchestration(run=run_dir, adapter_name="codex-exec", min_tasks=1)

        self.assertEqual(result["status"], "completed_parallel")
        state = inspect_run_state(run_dir)
        stages = {stage["stage"]: stage for stage in state["stages"]}
        self.assertEqual(stages["ingest"]["status"], "skipped")
        self.assertEqual(stages["fetch_claims"]["status"], "skipped")
        self.assertEqual(stages["ingest_vision"]["status"], "pending")
        self.assertEqual(state["next_safe_stage"], "ingest_vision")

    def test_partial_codex_exec_success_status_is_completed_partial_parallel(self) -> None:
        run_dir = self.prepare()

        class PartialCodexAdapter:
            name = "codex-exec"

            def run_task(inner_self, task, *, run_dir, max_threads):
                if str(task["id"]).endswith("001"):
                    shard_path = run_dir / task["output_shard_path"]
                    shard_path.parent.mkdir(parents=True, exist_ok=True)
                    self.write_json(shard_path, self.shard(run_dir, task))
                    return type("Result", (), {
                        "task_id": task["id"],
                        "status": "completed",
                        "child_thread_id": f"codex-{task['id']}",
                        "events": (),
                        "shard_path": str(shard_path),
                        "failure_category": None,
                        "message": None,
                    })()
                return type("Result", (), {
                    "task_id": task["id"],
                    "status": "failed",
                    "child_thread_id": f"codex-{task['id']}",
                    "events": (),
                    "shard_path": None,
                    "failure_category": "codex_exec_failed",
                    "message": "codex exec exited 1; stderr=deterministic child failure; stdout=<empty>",
                })()

        with mock.patch("deepresearch.parallel_orchestrator._adapter", return_value=PartialCodexAdapter()):
            result = run_parallel_orchestration(run=run_dir, adapter_name="codex-exec", min_tasks=2)

        self.assertEqual(result["status"], "completed_partial_parallel")
        self.assertTrue(result["ok"])
        self.assertFalse(result["parallel_degraded"])
        self.assertFalse(result["needs_serial_handoff"])
        self.assertEqual(len(result["merge"]["accepted_shards"]), 1)
        self.assertEqual(result["failure_counts"]["failed_tasks"], 1)
        failed_task = result["merge"]["failed_tasks"][0]
        self.assertEqual(failed_task["task_id"], "task_research_002")
        self.assertEqual(failed_task["adapter"], "codex-exec")
        self.assertEqual(failed_task["failure_category"], "codex_exec_failed")
        self.assertEqual(failed_task["stdout_stderr_summary"]["stderr"], "deterministic child failure")

    def test_partial_degraded_codex_exec_with_accepted_shard_is_synthesizable_partial(self) -> None:
        run_dir = self.prepare()

        class PartialDegradingCodexAdapter(CodexExecAdapter):
            name = "codex-exec"

            def __init__(self):
                super().__init__(project_root=ROOT, timeout_seconds=120)

            def available(self) -> bool:
                return True

            def run_task(self, task, *, run_dir, max_threads):
                child_thread_id = f"codex-{task['id']}"
                if str(task["id"]).endswith("001"):
                    shard_path = run_dir / task["output_shard_path"]
                    shard_path.parent.mkdir(parents=True, exist_ok=True)
                    self_outer.write_json(shard_path, self_outer.shard(run_dir, task))
                    return type("Result", (), {
                        "task_id": task["id"],
                        "status": "completed",
                        "child_thread_id": child_thread_id,
                        "events": (),
                        "shard_path": str(shard_path),
                        "failure_category": None,
                        "message": None,
                    })()
                diagnostic = (
                    "codex exec exited 1; adapter=codex-exec; "
                    f"task_id={task['id']}; cwd={ROOT}; "
                    "stderr=Auth sandbox approval blocked; stdout=<empty>"
                )
                return type("Result", (), {
                    "task_id": task["id"],
                    "status": "failed",
                    "child_thread_id": child_thread_id,
                    "events": ({
                        "event_type": "wait",
                        "task_id": task["id"],
                        "child_thread_id": child_thread_id,
                        "child_status": "failed",
                        "child_message": diagnostic,
                        "failure_category": "codex_exec_failed",
                        "raw_event": {
                            "adapter": "codex-exec",
                            "task_id": task["id"],
                            "cwd": str(ROOT),
                            "run_dir": str(run_dir),
                            "output_shard_path": str(run_dir / task["output_shard_path"]),
                            "command": ["codex", "exec", "--json", "<prompt>"],
                            "command_string": "codex exec --json <prompt>",
                        },
                    },),
                    "shard_path": None,
                    "failure_category": "codex_exec_failed",
                    "message": diagnostic,
                })()

        self_outer = self
        with mock.patch(
            "deepresearch.parallel_orchestrator._adapter",
            return_value=PartialDegradingCodexAdapter(),
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                min_tasks=2,
                max_tasks=2,
                allow_degraded=True,
            )

        self.assertEqual(result["status"], "completed_partial_parallel")
        self.assertTrue(result["ok"])
        self.assertTrue(result["parallel_degraded"])
        self.assertFalse(result["needs_serial_handoff"])
        self.assertEqual(result["accepted_shard_count"], 1)
        self.assertEqual(result["evidence_source"]["type"], "real_child_execution")
        self.assertTrue(result["evidence_source"]["real_child_execution"])
        self.assertFalse(result["evidence_source"]["real_use_e2e_eligible"])
        self.assertEqual(len(result["merge"]["accepted_shards"]), 1)
        self.assertEqual(result["failure_counts"]["failed_tasks"], 1)
        self.assertEqual(result["diagnostics"]["shard_counts"]["accepted_shards"], 1)
        self.assertNotIn("no evidence shards were accepted", json.dumps(result["diagnostics"]))
        state = inspect_run_state(run_dir)
        self.assertEqual(state["next_safe_stage"], "enforce_guardrails")

    def test_shard_merge_deduplicates_sources_images_and_claim_text(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=2)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        for task in tasks_artifact["tasks"]:
            task["state"] = "completed"
            shard_path = run_dir / task["output_shard_path"]
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_json(shard_path, self.shard(run_dir, task, duplicate=True))
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(len(evidence["sources"]), 1)
        self.assertEqual(len(evidence["images"]), 1)
        self.assertEqual(len(evidence["claims"]), 1)
        self.assertTrue(merge["source_dedupe"])
        self.assertTrue(merge["image_dedupe"])
        self.assertTrue(merge["claim_dedupe"])
        self.assertTrue(validate_artifacts(evidence_path=run_dir / "evidence.json").valid)

    def test_shard_merge_preserves_duplicate_image_visual_observation_supports(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=2)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        tasks = tasks_artifact["tasks"]

        first_task = tasks[0]
        first_task["state"] = "completed"
        first_shard_path = run_dir / first_task["output_shard_path"]
        first_shard_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(first_shard_path, self.shard(run_dir, first_task, duplicate=True))

        second_task = tasks[1]
        second_task["state"] = "completed"
        second_shard_path = run_dir / second_task["output_shard_path"]
        second_shard_path.parent.mkdir(parents=True, exist_ok=True)
        second_shard = self.shard(run_dir, second_task, duplicate=True)
        second_observation = "A second shard visual observation for the same image."
        second_shard["images"][0]["observations"] = [second_observation]
        second_shard["claims"][0]["id"] = "claim_duplicate_image_observation"
        second_shard["claims"][0]["text"] = "The duplicate image also supports a distinct observation."
        second_shard["claims"][0]["quote_spans"][0]["quote"] = second_shard["claims"][0]["text"]
        second_shard["claims"][0]["visual_supports"][0]["observation_text"] = second_observation
        self.write_json(second_shard_path, second_shard)
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        self.assertEqual(merge["status"], "completed", merge["validation"])
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(len(evidence["images"]), 1)
        self.assertIn(second_observation, evidence["images"][0]["observations"])
        distinct_claim = next(
            claim
            for claim in evidence["claims"]
            if claim["id"] == "claim_duplicate_image_observation"
        )
        support = distinct_claim["visual_supports"][0]
        self.assertEqual(support["image_id"], evidence["images"][0]["id"])
        self.assertEqual(
            support["observation_index"],
            evidence["images"][0]["observations"].index(second_observation),
        )
        self.assertEqual(support["observation_text"], second_observation)
        self.assertEqual(
            support["observation_ref"],
            f"images.{evidence['images'][0]['id']}.observations[{support['observation_index']}]",
        )
        self.assertTrue(validate_artifacts(evidence_path=run_dir / "evidence.json").valid)

    def test_shard_merge_scopes_duplicate_image_observation_remaps_per_task(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=3)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        tasks = tasks_artifact["tasks"]

        first_task = tasks[0]
        first_task["state"] = "completed"
        first_shard_path = run_dir / first_task["output_shard_path"]
        first_shard_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(first_shard_path, self.shard(run_dir, first_task, duplicate=True, common_ids=True))

        second_task = tasks[1]
        second_task["state"] = "completed"
        second_shard_path = run_dir / second_task["output_shard_path"]
        second_shard_path.parent.mkdir(parents=True, exist_ok=True)
        second_shard = self.shard(run_dir, second_task, duplicate=True, common_ids=True)
        second_observation = "A second task duplicate image observation."
        second_shard["images"][0]["observations"] = [second_observation]
        second_shard["claims"][0]["id"] = "claim_second_duplicate_image"
        second_shard["claims"][0]["text"] = "The duplicate image has a second task observation."
        second_shard["claims"][0]["quote_spans"][0]["quote"] = second_shard["claims"][0]["text"]
        second_shard["claims"][0]["visual_supports"][0]["observation_text"] = second_observation
        self.write_json(second_shard_path, second_shard)

        third_task = tasks[2]
        third_task["state"] = "completed"
        third_shard_path = run_dir / third_task["output_shard_path"]
        third_shard_path.parent.mkdir(parents=True, exist_ok=True)
        third_shard = self.shard(run_dir, third_task, common_ids=True)
        third_observation = "A separate third task image observation."
        third_shard["images"][0]["observations"] = [third_observation]
        third_shard["claims"][0]["id"] = "claim_third_nonduplicate_image"
        third_shard["claims"][0]["text"] = "A separate image keeps its local observation index."
        third_shard["claims"][0]["quote_spans"][0]["quote"] = third_shard["claims"][0]["text"]
        third_shard["claims"][0]["visual_supports"][0]["observation_text"] = third_observation
        self.write_json(third_shard_path, third_shard)
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        self.assertEqual(merge["status"], "completed", merge["validation"])
        evidence = self.load_json(run_dir / "evidence.json")
        third_claim = next(
            claim for claim in evidence["claims"] if claim["id"] == "claim_third_nonduplicate_image"
        )
        support = third_claim["visual_supports"][0]
        self.assertEqual(support["observation_index"], 0)
        self.assertEqual(support["observation_text"], third_observation)
        third_image = next(image for image in evidence["images"] if image["id"] == support["image_id"])
        self.assertEqual(third_image["observations"], [third_observation])
        self.assertTrue(validate_artifacts(evidence_path=run_dir / "evidence.json").valid)

    def test_plan_research_tasks_does_not_reinsert_visual_evidence_for_text_only_helper_task(self) -> None:
        run_dir = self.prepare(route="visual_required")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["semantic_planner"]["bounded_tasks"] = []
        evidence["search_tasks"] = [
            {
                "id": "task_005",
                "task_id": "task_005",
                "angle_id": "angle_001",
                "route": "text_only",
                "evidence_need": "visual_example",
                "expected_evidence": ["primary_source"],
                "expected_visual_targets": [],
                "expected_artifacts": ["deduplication table"],
                "success_criteria": ["Use previously collected official images only."],
                "max_sources": 3,
                "max_images": 0,
                "query": "Deduplicate collected official poster candidates.",
            }
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        planned = plan_research_tasks(run=run_dir, min_tasks=1)

        task = planned["tasks"][0]
        self.assertEqual(task["route"], "text_only")
        self.assertEqual(task["max_images"], 0)
        self.assertEqual(task["expected_evidence"], ["primary_source"])

    def test_shard_merge_namespaces_colliding_local_ids_and_remaps_refs(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=2)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        for task in tasks_artifact["tasks"]:
            task["state"] = "completed"
            shard_path = run_dir / task["output_shard_path"]
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            shard = self.shard(run_dir, task, common_ids=True)
            shard["claims"][0]["votes"] = [
                {
                    "id": f"vote_{task['id']}_001",
                    "claim_id": "claim_001",
                    "verifier_type": "text",
                    "agent_name": "merge-test-verifier",
                    "method": "runner-agent",
                    "model_or_tool": "unittest",
                    "vote": "support",
                    "confidence": 0.84,
                    "rationale": "The source and image support the claim.",
                    "evidence_refs": ["src_001", "img_001"],
                    "created_at": "2026-06-23T00:00:00Z",
                }
            ]
            self.write_json(shard_path, shard)
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        self.assertEqual(merge["status"], "completed", merge)
        evidence = self.load_json(run_dir / "evidence.json")
        source_ids = [source["id"] for source in evidence["sources"]]
        image_ids = [image["id"] for image in evidence["images"]]
        claim_ids = [claim["id"] for claim in evidence["claims"]]
        self.assertEqual(len(source_ids), len(set(source_ids)))
        self.assertEqual(len(image_ids), len(set(image_ids)))
        self.assertEqual(len(claim_ids), len(set(claim_ids)))
        self.assertEqual(len(evidence["sources"]), 2)
        self.assertEqual(len(evidence["claims"]), 2)
        for claim in evidence["claims"]:
            self.assertIn(claim["supporting_sources"][0], source_ids)
            self.assertEqual(claim["quote_spans"][0]["source_id"], claim["supporting_sources"][0])
            self.assertIn(claim["supporting_images"][0], image_ids)
            self.assertIn(claim["votes"][0]["evidence_refs"][0], source_ids)
            self.assertIn(claim["votes"][0]["evidence_refs"][1], image_ids)
        self.assertTrue(validate_artifacts(evidence_path=run_dir / "evidence.json").valid)

    def test_shard_merge_backfills_missing_angle_metadata_before_report_counts(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=1)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        task = tasks_artifact["tasks"][0]
        task["state"] = "completed"
        shard = self.shard(run_dir, task)
        shard["claims"][0]["promotion_status"] = "eligible"
        for record in [*shard["sources"], *shard["images"], *shard["claims"]]:
            record.pop("angle_id", None)
            record.pop("route", None)
            record.pop("source_task_id", None)
        shard_path = run_dir / task["output_shard_path"]
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(shard_path, shard)
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)
        report = synthesize_report(run=run_dir)

        self.assertEqual(merge["status"], "completed", merge)
        self.assertEqual(report["status"], "completed", report)
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["sources"][0]["angle_id"], task["angle_id"])
        self.assertEqual(evidence["images"][0]["angle_id"], task["angle_id"])
        self.assertEqual(evidence["claims"][0]["angle_id"], task["angle_id"])
        validation = self.load_json(run_dir / "semantic_planner_validation.json")
        self.assertEqual(
            validation["report_angle_claim_counts"].get(task["angle_id"]),
            1,
        )

    def test_completed_task_requires_schema_valid_shard(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=1)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        task = tasks_artifact["tasks"][0]
        shard_path = run_dir / task["output_shard_path"]
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(shard_path, {"schema_version": "0.1.0", "sources": []})

        class InvalidShardAdapter:
            name = "invalid-shard"

            def run_task(self, task, *, run_dir, max_threads):
                return type("Result", (), {
                    "task_id": task["id"],
                    "status": "completed",
                    "child_thread_id": "invalid-shard-thread",
                    "events": (),
                    "shard_path": str(run_dir / task["output_shard_path"]),
                    "failure_category": None,
                    "message": None,
                })()

        with mock.patch("deepresearch.parallel_orchestrator._adapter", return_value=InvalidShardAdapter()):
            run_parallel_orchestration(run=run_dir, adapter_name="fixture", min_tasks=1)

        task = self.load_json(run_dir / "research_tasks.json")["tasks"][0]
        self.assertEqual(task["state"], "failed")
        self.assertEqual(task["failure_category"], "invalid_shard")

    def test_fixture_adapter_uses_bounded_parallel_scheduler(self) -> None:
        run_dir = self.prepare()
        lock = threading.Lock()
        in_flight = 0
        max_seen = 0

        class InstrumentedFixture(FixtureAdapter):
            name = "instrumented-fixture"

            def run_task(self, task, *, run_dir, max_threads):
                nonlocal in_flight, max_seen
                with lock:
                    in_flight += 1
                    max_seen = max(max_seen, in_flight)
                try:
                    time.sleep(0.03)
                    return super().run_task(task, run_dir=run_dir, max_threads=max_threads)
                finally:
                    with lock:
                        in_flight -= 1

        with mock.patch("deepresearch.parallel_orchestrator._adapter", return_value=InstrumentedFixture()):
            result = run_parallel_orchestration(run=run_dir, adapter_name="fixture", min_tasks=6)

        self.assertEqual(result["max_scheduled_concurrency"], 8)
        self.assertGreater(max_seen, 1)

    def test_retry_safe_failed_task_does_not_rerun_completed_tasks(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=2)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        tasks_artifact["tasks"][0]["state"] = "completed"
        first_shard_path = run_dir / tasks_artifact["tasks"][0]["output_shard_path"]
        first_shard_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(first_shard_path, self.shard(run_dir, tasks_artifact["tasks"][0]))
        tasks_artifact["tasks"][1]["state"] = "failed"
        tasks_artifact["tasks"][1]["failure_category"] = "missing_shard"
        tasks_artifact["tasks"][1]["attempt"] = 1
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)
        before = 0

        run_parallel_orchestration(
            run=run_dir,
            adapter_name="fixture",
            min_tasks=2,
            retry_failed=True,
        )

        after = len((run_dir / "subagent_assignments.jsonl").read_text(encoding="utf-8").splitlines())
        self.assertEqual(after, before + 1)
        tasks = self.load_json(run_dir / "research_tasks.json")["tasks"]
        self.assertEqual([task["state"] for task in tasks], ["merged", "merged"])

    def test_blocked_and_discarded_tasks_are_preserved_in_merge_status(self) -> None:
        run_dir = self.prepare()
        plan_research_tasks(run=run_dir, min_tasks=3)
        tasks_artifact = self.load_json(run_dir / "research_tasks.json")
        tasks_artifact["tasks"][0]["state"] = "blocked"
        tasks_artifact["tasks"][0]["blocked_reason"] = "policy_blocked"
        tasks_artifact["tasks"][1]["state"] = "discarded"
        tasks_artifact["tasks"][1]["discard_reason"] = "budget_pruned"
        tasks_artifact["tasks"][2]["state"] = "completed"
        shard_path = run_dir / tasks_artifact["tasks"][2]["output_shard_path"]
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(shard_path, self.shard(run_dir, tasks_artifact["tasks"][2]))
        self.write_json(run_dir / "research_tasks.json", tasks_artifact)

        merge = merge_evidence_shards(run=run_dir)

        self.assertEqual(merge["blocked_tasks"][0]["reason"], "policy_blocked")
        self.assertEqual(merge["discarded_tasks"][0]["reason"], "budget_pruned")
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(len(evidence["claims"]), 1)
        self.assertNotIn("policy_blocked", json.dumps(evidence["claims"]))

    def test_codex_exec_unavailable_records_degraded_serial_execution(self) -> None:
        run_dir = self.prepare()

        with mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value=None):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                min_tasks=2,
            )

        self.assertTrue(result["parallel_degraded"])
        self.assertEqual(result["degraded_reason"], "codex_exec_unavailable")
        self.assertEqual(result["adapter"], "serial-degraded")
        self.assertEqual(result["evidence_source"]["type"], "serial_handoff")
        self.assertFalse(result["evidence_source"]["fixture_only"])
        self.assertFalse(result["evidence_source"]["real_child_execution"])
        self.assertFalse(result["evidence_source"]["real_use_e2e_eligible"])
        self.assertEqual(result["max_scheduled_concurrency"], 1)
        self.assertEqual(result["status"], "degraded_serial_handoff_required")
        self.assertTrue(result["needs_serial_handoff"])
        tasks = self.load_json(run_dir / "research_tasks.json")
        self.assertTrue(tasks["parallel_degraded"])
        self.assertTrue(all(task["state"] == "blocked" for task in tasks["tasks"]))
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["claims"], [])
        merge = self.load_json(run_dir / "merge_status.json")
        self.assertEqual(merge["evidence_source"]["type"], "serial_handoff")
        self.assertEqual(len(merge["blocked_tasks"]), 2)
        self.assertEqual(merge["accepted_shards"], [])
        state = inspect_run_state(run_dir)
        stages = {stage["stage"]: stage for stage in state["stages"]}
        self.assertEqual(stages["parallel_orchestration"]["status"], "completed")
        self.assertEqual(stages["ingest"]["status"], "pending")
        self.assertEqual(state["next_safe_stage"], "ingest")

    def test_degraded_serial_fallback_preserves_codex_exec_failure_diagnostics(self) -> None:
        run_dir = self.prepare()

        class MissingCapabilityCodex(CodexExecAdapter):
            name = "codex-exec"

            def available(self) -> bool:
                return True

            def run_task(self, task, *, run_dir, max_threads):
                child_thread_id = f"codex-{task['id']}"
                command_context = {
                    "adapter": self.name,
                    "task_id": task["id"],
                    "cwd": str(ROOT),
                    "trusted_project_root": str(ROOT),
                    "run_dir": str(run_dir.resolve()),
                    "output_shard_path": str(run_dir / task["output_shard_path"]),
                    "command": ["codex", "exec", "--json", "<prompt>"],
                    "command_string": "codex exec --json '<prompt>'",
                    "repo_check_bypass_used": False,
                    "retryable": True,
                }
                diagnostic = (
                    "codex exec exited 1; adapter=codex-exec; "
                    f"task_id={task['id']}; cwd={ROOT}; "
                    "stderr=Auth sandbox unavailable; stdout=<empty>"
                )
                return type("Result", (), {
                    "task_id": task["id"],
                    "status": "failed",
                    "child_thread_id": child_thread_id,
                    "events": ({
                        "event_type": "wait",
                        "task_id": task["id"],
                        "child_thread_id": child_thread_id,
                        "child_status": "failed",
                        "child_message": diagnostic,
                        "failure_category": "codex_exec_failed",
                        "raw_event": command_context,
                    },),
                    "shard_path": None,
                    "failure_category": "codex_exec_failed",
                    "message": diagnostic,
                })()

        with mock.patch("deepresearch.parallel_orchestrator._adapter", return_value=MissingCapabilityCodex()):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                min_tasks=2,
            )

        self.assertTrue(result["parallel_degraded"])
        self.assertEqual(result["status"], "degraded_serial_handoff_required")
        self.assertEqual(result["failure_counts"]["failed_tasks"], 2)
        self.assertEqual(result["failure_counts"]["blocked_tasks"], 0)
        self.assertEqual(result["failure_counts"]["by_category"]["codex_exec_failed"], 2)
        failed_task = result["merge"]["failed_tasks"][0]
        self.assertEqual(failed_task["task_id"], "task_research_001")
        self.assertEqual(failed_task["adapter"], "codex-exec")
        self.assertEqual(failed_task["failure_category"], "codex_exec_failed")
        self.assertFalse(failed_task["retryable"])
        self.assertEqual(failed_task["attempt"], 1)
        self.assertEqual(failed_task["working_dir"], str(ROOT))
        self.assertEqual(failed_task["command_context"]["cwd"], str(ROOT))
        self.assertEqual(failed_task["stdout_stderr_summary"]["stderr"], "Auth sandbox unavailable")
        self.assertEqual(
            result["diagnostics"]["first_failure_category"],
            "codex_exec_failed",
        )
        self.assertEqual(result["diagnostics"]["first_failed_adapter"], "codex-exec")
        self.assertIn("Auth sandbox unavailable", result["diagnostics"]["first_failed_diagnostic"])
        state = inspect_run_state(run_dir)
        parallel_stage = {
            stage["stage"]: stage for stage in state["stages"]
        }["parallel_orchestration"]
        self.assertEqual(parallel_stage["failure_counts"]["by_category"]["codex_exec_failed"], 2)
        self.assertEqual(
            parallel_stage["diagnostics"]["first_failure_category"],
            "codex_exec_failed",
        )

    def test_codex_exec_unavailable_no_degrade_returns_blocked_json_envelope(self) -> None:
        run_dir = self.prepare()

        with mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value=None):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                min_tasks=2,
                allow_degraded=False,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked_parallel_execution")
        self.assertEqual(result["adapter"], "codex-exec")
        self.assertFalse(result["parallel_degraded"])
        self.assertTrue(result["needs_serial_handoff"])
        self.assertEqual(result["failure_counts"]["blocked_tasks"], 2)
        self.assertEqual(result["failure_counts"]["by_category"]["adapter_unavailable"], 2)
        self.assertEqual(
            result["diagnostics"]["actionable_cause"],
            "no evidence shards were accepted because child tasks were blocked",
        )
        blocked_task = result["merge"]["blocked_tasks"][0]
        self.assertEqual(blocked_task["task_id"], "task_research_001")
        self.assertEqual(blocked_task["adapter"], "codex-exec")
        self.assertEqual(blocked_task["failure_category"], "adapter_unavailable")
        self.assertTrue(blocked_task["retryable"])
        state = inspect_run_state(run_dir)
        stages = {stage["stage"]: stage for stage in state["stages"]}
        self.assertEqual(stages["parallel_orchestration"]["status"], "failed")
        self.assertEqual(stages["parallel_orchestration"]["stage_status"], "blocked_parallel_execution")
        self.assertFalse(stages["parallel_orchestration"]["ok"])
        self.assertTrue(stages["parallel_orchestration"]["needs_serial_handoff"])

    def test_direct_serial_degraded_without_evidence_is_blocked_not_completed(self) -> None:
        run_dir = self.prepare()

        result = run_parallel_orchestration(
            run=run_dir,
            adapter_name="serial-degraded",
            min_tasks=2,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked_parallel_execution")
        self.assertEqual(result["adapter"], "serial-degraded")
        self.assertFalse(result["parallel_degraded"])
        self.assertTrue(result["needs_serial_handoff"])
        self.assertEqual(result["evidence_source"]["type"], "serial_handoff")
        self.assertEqual(result["failure_counts"]["blocked_tasks"], 2)
        state = inspect_run_state(run_dir)
        stages = {stage["stage"]: stage for stage in state["stages"]}
        self.assertEqual(stages["parallel_orchestration"]["status"], "failed")
        self.assertEqual(stages["parallel_orchestration"]["stage_status"], "blocked_parallel_execution")
        self.assertEqual(state["next_safe_stage"], "parallel_orchestration")

    def test_run_parallel_orchestration_wires_codex_exec_timeout_override(self) -> None:
        run_dir = self.prepare()

        def fake_codex_exec(command, **kwargs):
            self.assertEqual(kwargs["timeout"], 900)
            tasks = self.load_json(run_dir / "research_tasks.json")["tasks"]
            task = tasks[0]
            shard_path = run_dir / task["output_shard_path"]
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_json(shard_path, self.shard(run_dir, task))
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"type":"message","status":"completed","message":"wrote shard"}\n',
                stderr="",
            )

        with (
            mock.patch("deepresearch.parallel_orchestrator.shutil.which", return_value="/usr/bin/codex"),
            mock.patch(
                "deepresearch.parallel_orchestrator.subprocess.run",
                side_effect=fake_codex_exec,
            ) as run_mock,
        ):
            result = run_parallel_orchestration(
                run=run_dir,
                adapter_name="codex-exec",
                codex_exec_timeout_seconds=900,
                min_tasks=1,
                max_tasks=1,
                allow_degraded=False,
            )

        self.assertEqual(result["status"], "completed_parallel")
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 900)

    def test_cli_codex_exec_no_degrade_fails_when_children_accept_no_shards(self) -> None:
        runs_dir = self.temp_runs_dir()
        bin_dir = self.temp_runs_dir()
        fake_codex = bin_dir / "codex"
        fake_codex.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' '{\"type\":\"message\",\"status\":\"error\",\"message\":\"child failed\"}'\n"
            "printf '%s\\n' 'fake codex child failed before writing shard' >&2\n"
            "exit 1\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        prepare = subprocess.run(
            [
                str(RUNNER),
                "prepare",
                "real codex child failure regression",
                "--angle",
                "primary source discovery",
                "--runs-dir",
                str(runs_dir),
                "--route",
                "text_only",
                "--allow-release-ineligible-materialization-for-tests",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(prepare.returncode, 0, prepare.stderr)
        run_dir = Path(json.loads(prepare.stdout)["run_dir"])
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

        result = subprocess.run(
            [
                str(RUNNER),
                "orchestrate-parallel",
                "--run",
                str(run_dir),
                "--adapter",
                "codex-exec",
                "--no-degrade",
                "--min-tasks",
                "2",
            ],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2, result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed_parallel_no_accepted_shards")
        self.assertEqual(payload["adapter"], "codex-exec")
        self.assertTrue(payload["needs_serial_handoff"])
        self.assertFalse(payload["parallel_degraded"])
        self.assertEqual(payload["evidence_source"]["type"], "failed_real_child_execution")
        self.assertTrue(payload["evidence_source"]["attempted_real_child_execution"])
        self.assertFalse(payload["evidence_source"]["real_child_execution"])
        self.assertFalse(payload["evidence_source"]["real_use_e2e_eligible"])
        self.assertEqual(payload["merge"]["accepted_shards"], [])
        self.assertEqual(payload["failure_counts"]["failed_tasks"], 2)
        self.assertEqual(payload["failure_counts"]["by_category"]["codex_exec_failed"], 2)
        failed_task = payload["merge"]["failed_tasks"][0]
        self.assertEqual(failed_task["adapter"], "codex-exec")
        self.assertEqual(failed_task["failure_category"], "codex_exec_failed")
        self.assertFalse(failed_task["retryable"])
        self.assertIn("fake codex child failed", failed_task["stdout_stderr_summary"]["stderr"])
        self.assertEqual(failed_task["command_context"]["trusted_project_root"], str(ROOT))
        self.assertEqual(failed_task["command_context"]["run_dir"], str(run_dir.resolve()))
        self.assertIn(" -C ", failed_task["command_context"]["command_string"])
        self.assertEqual(
            payload["diagnostics"]["actionable_cause"],
            "no evidence shards were accepted because child tasks failed",
        )
        run_status = subprocess.run(
            [
                str(RUNNER),
                "run-status",
                "--run",
                str(run_dir),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(run_status.returncode, 0, run_status.stderr)
        run_status_payload = json.loads(run_status.stdout)
        run_status_stage = {
            stage["stage"]: stage for stage in run_status_payload["stages"]
        }["parallel_orchestration"]
        for key in (
            "stage_status",
            "adapter",
            "parallel_degraded",
            "needs_serial_handoff",
            "failure_counts",
            "diagnostics",
        ):
            status_key = "status" if key == "stage_status" else key
            self.assertEqual(run_status_stage[key], payload[status_key])
        state = inspect_run_state(run_dir)
        stages = {stage["stage"]: stage for stage in state["stages"]}
        self.assertEqual(stages["parallel_orchestration"]["status"], "failed")
        self.assertEqual(
            stages["parallel_orchestration"]["stage_status"],
            "failed_parallel_no_accepted_shards",
        )

    def test_cli_codex_exec_no_degrade_reports_blocker_status_for_auth_sandbox_failures(self) -> None:
        runs_dir = self.temp_runs_dir()
        bin_dir = self.temp_runs_dir()
        fake_codex = bin_dir / "codex"
        fake_codex.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' '{\"type\":\"message\",\"status\":\"error\",\"message\":\"Auth sandbox approval blocked\"}'\n"
            "printf '%s\\n' 'Auth sandbox approval blocked before child could write shard' >&2\n"
            "exit 1\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        prepare = subprocess.run(
            [
                str(RUNNER),
                "prepare",
                "real codex auth blocker regression",
                "--angle",
                "primary source discovery",
                "--runs-dir",
                str(runs_dir),
                "--route",
                "text_only",
                "--allow-release-ineligible-materialization-for-tests",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(prepare.returncode, 0, prepare.stderr)
        run_dir = Path(json.loads(prepare.stdout)["run_dir"])
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

        result = subprocess.run(
            [
                str(RUNNER),
                "orchestrate-parallel",
                "--run",
                str(run_dir),
                "--adapter",
                "codex-exec",
                "--no-degrade",
                "--min-tasks",
                "1",
            ],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2, result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked_parallel_execution")
        self.assertFalse(payload["parallel_degraded"])
        self.assertTrue(payload["needs_serial_handoff"])
        self.assertEqual(payload["diagnostics"]["first_failure_category"], "codex_exec_failed")
        self.assertIn("Auth sandbox approval blocked", payload["diagnostics"]["first_failed_diagnostic"])

    def test_exhaustive_plan_requires_confirmation_and_cost_cap(self) -> None:
        run_dir = self.prepare()
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["budget"]["preset"] = "exhaustive"
        evidence["budget"].pop("max_cost_usd", None)
        self.write_json(run_dir / "evidence.json", evidence)
        (run_dir / "budget_estimate.json").unlink()

        with self.assertRaises(ParallelOrchestrationError):
            plan_research_tasks(run=run_dir, min_tasks=100)
        with self.assertRaises(ParallelOrchestrationError):
            plan_research_tasks(run=run_dir, min_tasks=100, confirm_exhaustive=True)

        result = plan_research_tasks(
            run=run_dir,
            min_tasks=100,
            confirm_exhaustive=True,
            max_cost_usd=1.0,
        )
        self.assertEqual(result["task_count"], 100)

    def test_cli_m18_fixture_smoke(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepare = subprocess.run(
            [
                str(RUNNER),
                "prepare",
                "M18 CLI fixture smoke",
                "--angle",
                "primary source discovery",
                "--runs-dir",
                str(runs_dir),
                "--route",
                "text_only",
                "--allow-release-ineligible-materialization-for-tests",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(prepare.returncode, 0, prepare.stderr)
        run_dir = Path(json.loads(prepare.stdout)["run_dir"])

        smoke = subprocess.run(
            [
                str(RUNNER),
                "orchestrate-parallel",
                "--run",
                str(run_dir),
                "--adapter",
                "fixture",
                "--min-tasks",
                "3",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        payload = json.loads(smoke.stdout)
        self.assertEqual(payload["status"], "completed_fixture")
        self.assertEqual(payload["adapter"], "fixture")
        self.assertEqual(payload["evidence_source"]["type"], "fixture")
        self.assertTrue(payload["evidence_source"]["fixture_only"])
        self.assertFalse(payload["evidence_source"]["real_use_e2e_eligible"])
        self.assertTrue((run_dir / "merge_status.json").is_file())


if __name__ == "__main__":
    unittest.main()
