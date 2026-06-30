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
    inspect_run_state,
    merge_evidence_shards,
    plan_research_tasks,
    prepare_run,
    read_trace_records,
    run_parallel_orchestration,
    synthesize_report,
    validate_artifacts,
    validate_trace_file,
)


class ParallelOrchestratorTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def load_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

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
        self.assertIn("write a minimal valid `evidence_shard.json`", command[-1])
        self.assertIn("before any optional sidecars", command[-1])
        self.assertIn("invoke them with `python3`, not `python`", command[-1])
        self.assertIn(f"Do not write sidecars outside {shard_dir}", command[-1])
        self.assertIn("Write claim text, caveats, rationales, and synthesized source snippets in English", command[-1])
        self.assertIn("Use `vlm_provider` exactly `codex-interactive`", command[-1])
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
        self.assertIn("analysis_status `skipped`", visual_command[-1])
        self.assertIn("later runner VLM analysis", visual_command[-1])

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
            self.write_json(shard_path, {"schema_version": "0.1.0", "sources": []})
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
        self.assertFalse(result["merge"]["failed_tasks"][0]["retryable"])

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
                "--runs-dir",
                str(runs_dir),
                "--route",
                "text_only",
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
                "--runs-dir",
                str(runs_dir),
                "--route",
                "text_only",
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
                "--runs-dir",
                str(runs_dir),
                "--route",
                "text_only",
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
