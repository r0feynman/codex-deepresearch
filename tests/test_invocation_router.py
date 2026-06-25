from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch.invocation_router import run_skill_invocation  # noqa: E402


class InvocationRouterTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_default_deep_research_invocation_runs_full_runner_fixture(self) -> None:
        result = run_skill_invocation(
            "$deep-research: investigate deterministic router fixture",
            runs_dir=self.temp_runs_dir(),
            adapter_name="fixture",
            route="text_only",
            budget_preset="quick",
            min_tasks=2,
            max_tasks=2,
        )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["terminal"])
        self.assertEqual(result["selected_mode"], "full-runner")
        self.assertEqual(result["status"], "completed_fixture")
        self.assertEqual(result["provenance"]["type"], "fixture")
        self.assertTrue(result["provenance"]["fixture_only"])
        self.assertIn("run_status", result["artifacts"])
        self.assertIn("parallel_orchestration_status", result["artifacts"])
        self.assertIn("evidence", result["artifacts"])
        self.assertIn("report", result["artifacts"])
        self.assertIn("report_status", result["artifacts"])
        self.assertGreaterEqual(result["parallel"]["accepted_shard_count"], 1)
        self.assertEqual(result["artifact_handoff"]["run_dir"], result["run_dir"])
        self.assertIn("report_status", result["artifact_handoff"]["artifact_paths"])
        self.assertEqual(
            result["shard_summary"]["accepted_shard_count"],
            result["parallel"]["accepted_shard_count"],
        )
        self.assertFalse(result["fallback"]["parallel_degraded"])
        self.assertFalse(result["fallback"]["needs_serial_handoff"])

        persisted = self.read_json(Path(result["artifacts"]["run_status"]))
        self.assertEqual(persisted["status"], "completed_fixture")
        self.assertEqual(persisted["provenance"]["type"], "fixture")
        self.assertIn("report_status", persisted["artifact_handoff"]["artifact_paths"])

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

    def test_negated_quick_answer_with_full_pipeline_intent_runs_full_runner(self) -> None:
        result = run_skill_invocation(
            "$deep-research: do not give me a quick answer about cache eviction; run the full pipeline",
            runs_dir=self.temp_runs_dir(),
            adapter_name="fixture",
            route="text_only",
            budget_preset="quick",
            min_tasks=1,
            max_tasks=1,
        )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["terminal"])
        self.assertEqual(result["selected_mode"], "full-runner")
        self.assertEqual(result["status"], "completed_fixture")
        self.assertIn("run_status", result["artifacts"])
        self.assertIn("evidence", result["artifacts"])
        self.assertIn("report_status", result["artifacts"])

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

    def test_visual_required_without_provider_blocks_with_visual_status_artifact(self) -> None:
        result = run_skill_invocation(
            "$deep-research: inspect product screenshots for evidence",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            budget_preset="quick",
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["status"], "blocked_missing_visual_provider")
        self.assertIn("actionable_cause", result["diagnostics"])
        self.assertIn("explicit real visual acquisition provider", result["diagnostics"]["actionable_cause"])
        self.assertIn("run_status", result["artifacts"])
        self.assertIn("visual_provider_status", result["artifacts"])

        run_status = self.read_json(Path(result["artifacts"]["run_status"]))
        self.assertFalse(run_status["ok"])
        self.assertTrue(run_status["terminal"])
        self.assertEqual(run_status["status"], "blocked_missing_visual_provider")
        self.assertEqual(
            run_status["diagnostics"]["actionable_cause"],
            result["diagnostics"]["actionable_cause"],
        )

        visual_provider_status = self.read_json(Path(result["artifacts"]["visual_provider_status"]))
        self.assertFalse(visual_provider_status["ok"])
        self.assertTrue(visual_provider_status["terminal"])
        self.assertEqual(visual_provider_status["status"], "blocked_missing_visual_provider")
        self.assertEqual(
            visual_provider_status["diagnostics"]["actionable_cause"],
            result["diagnostics"]["actionable_cause"],
        )
        self.assertFalse(visual_provider_status["providers"][0]["configured"])

    def test_serial_fallback_provenance_is_distinguishable_when_no_shards_are_accepted(self) -> None:
        result = run_skill_invocation(
            "$deep-research: force serial fallback provenance",
            runs_dir=self.temp_runs_dir(),
            adapter_name="serial-degraded",
            route="text_only",
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
