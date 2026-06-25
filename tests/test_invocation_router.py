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

        persisted = self.read_json(Path(result["artifacts"]["run_status"]))
        self.assertEqual(persisted["status"], "completed_fixture")
        self.assertEqual(persisted["provenance"]["type"], "fixture")

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


if __name__ == "__main__":
    unittest.main()
