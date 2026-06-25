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

from deepresearch.fresh_session_e2e import (  # noqa: E402
    FreshSessionE2EError,
    render_final_response,
    run_fresh_session_e2e,
    validate_final_response,
)


class FreshSessionE2ETests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_gate_runs_fixture_serial_and_explicit_real_skip_scenarios(self) -> None:
        result = run_fresh_session_e2e(
            runs_dir=self.temp_runs_dir(),
            suite_id="fresh-session",
            clean=True,
            real_codex_exec="skip",
        )

        self.assertEqual(result["status"], "passed")
        self.assertTrue(all(result["acceptance"].values()), result["acceptance"])
        scenarios = {scenario["id"]: scenario for scenario in result["scenarios"]}

        fixture = scenarios["fixture_full_runner"]
        self.assertEqual(fixture["terminal_outcome"], "completed_fixture")
        self.assertEqual(fixture["provenance_class"], "fixture_only")
        self.assertIn("run_status", fixture["artifacts"])
        self.assertIn("report_status", fixture["artifacts"])
        self.assertTrue(Path(fixture["artifacts"]["run_status"]).is_file())
        self.assertTrue(Path(fixture["artifacts"]["report_status"]).is_file())
        self.assertGreater(fixture["shard_summary"]["accepted_shard_count"], 0)
        transcript = Path(fixture["transcript"]).read_text(encoding="utf-8")
        self.assertIn("$deep-research:", transcript)
        self.assertIn("run_status.json", transcript)
        self.assertIn("report_status.json", transcript)

        serial = scenarios["serial_fallback_blocked"]
        self.assertEqual(serial["terminal_outcome"], "blocked_explicit")
        self.assertEqual(serial["provenance_class"], "serial_fallback")
        self.assertIn("run_status", serial["artifacts"])
        self.assertNotIn("report_status", serial["artifacts"])
        self.assertEqual(serial["shard_summary"]["accepted_shard_count"], 0)

        real = scenarios["real_codex_exec_skipped"]
        self.assertEqual(real["terminal_outcome"], "blocked_explicit")
        self.assertEqual(real["provenance_class"], "blocked")
        self.assertIn("blocked", Path(real["transcript"]).read_text(encoding="utf-8").lower())

    def test_cli_fresh_session_e2e_outputs_machine_readable_results(self) -> None:
        runs_dir = self.temp_runs_dir()
        command = subprocess.run(
            [
                str(RUNNER),
                "fresh-session-e2e",
                "--runs-dir",
                str(runs_dir),
                "--suite-id",
                "cli-suite",
                "--clean",
                "--real-codex-exec",
                "skip",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["status"], "passed")
        results_path = Path(payload["artifacts"]["results"])
        self.assertTrue(results_path.is_file())
        persisted = self.read_json(results_path)
        self.assertEqual(
            persisted["schema_version"],
            "codex-deepresearch.fresh-session-e2e.v0",
        )

    def test_successful_looking_response_without_report_status_fails(self) -> None:
        run_dir = self._complete_run_dir()
        run_status = self._run_status(
            run_dir,
            status="completed_parallel",
            provenance={
                "type": "real_child_execution",
                "adapter": "codex-exec",
                "accepted_shards": 1,
                "real_child_execution": True,
                "fixture_only": False,
                "manual_handoff": False,
            },
            include_report_status=False,
        )
        response = f"Done. Completed DeepResearch report.\nRun directory: {run_dir}\n"

        validation = validate_final_response(
            run_status=run_status,
            final_response=response,
            scenario_id="bad_success",
        )

        self.assertEqual(validation["status"], "failed")
        checks = {failure["check"] for failure in validation["failures"]}
        self.assertIn("missing_report_status_artifact_path", checks)
        self.assertIn("successful_response_missing_report_status", checks)

    def test_real_parallel_requires_accepted_shards(self) -> None:
        run_dir = self._complete_run_dir()
        run_status = self._run_status(
            run_dir,
            status="completed_parallel",
            provenance={
                "type": "real_child_execution",
                "adapter": "codex-exec",
                "accepted_shards": 0,
                "real_child_execution": True,
                "fixture_only": False,
                "manual_handoff": False,
            },
        )
        response = render_final_response(run_status, scenario_id="real_without_shards")

        validation = validate_final_response(
            run_status=run_status,
            final_response=response,
            scenario_id="real_without_shards",
        )

        self.assertEqual(validation["status"], "failed")
        checks = {failure["check"] for failure in validation["failures"]}
        self.assertIn("real_parallel_without_accepted_shards", checks)

    def test_require_mode_fails_when_real_codex_exec_is_only_skipped(self) -> None:
        runner_dir = self.temp_runs_dir()
        fake_runner = runner_dir / "fake-runner"
        fake_payload = {
            "schema_version": "codex-deepresearch.run-status.v0",
            "run_id": None,
            "run_dir": None,
            "selected_mode": "blocked",
            "status": "blocked_preflight",
            "ok": False,
            "terminal": True,
            "provenance": {
                "type": "blocked_preflight",
                "adapter": "codex-exec",
                "fixture_only": False,
                "manual_handoff": False,
                "attempted_real_child_execution": False,
                "real_child_execution": False,
            },
            "diagnostics": {"actionable_cause": "fake runner blocked real child execution"},
            "artifacts": {},
        }
        fake_runner.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"print(json.dumps({fake_payload!r}))\n",
            encoding="utf-8",
        )
        fake_runner.chmod(0o755)

        with self.assertRaises(FreshSessionE2EError) as raised:
            run_fresh_session_e2e(
                runs_dir=self.temp_runs_dir(),
                suite_id="strict",
                clean=True,
                real_codex_exec="require",
                runner_path=fake_runner,
            )

        self.assertIsNotNone(raised.exception.results_path)
        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "failed")

    def _complete_run_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        run_dir = Path(temp_dir.name)
        for filename, content in {
            "report.md": "# Report\n",
            "evidence.json": "{}\n",
            "report_status.json": "{}\n",
        }.items():
            (run_dir / filename).write_text(content, encoding="utf-8")
        return run_dir

    def _run_status(
        self,
        run_dir: Path,
        *,
        status: str,
        provenance: dict,
        include_report_status: bool = True,
    ) -> dict:
        artifacts = {
            "run_status": str(run_dir / "run_status.json"),
            "report": str(run_dir / "report.md"),
            "evidence": str(run_dir / "evidence.json"),
        }
        if include_report_status:
            artifacts["report_status"] = str(run_dir / "report_status.json")
        payload = {
            "schema_version": "codex-deepresearch.run-status.v0",
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "selected_mode": "full-runner",
            "status": status,
            "ok": True,
            "terminal": True,
            "provenance": provenance,
            "diagnostics": {"actionable_cause": "test payload"},
            "artifacts": artifacts,
            "stages": {"synthesize": {"status": "completed"}},
            "shard_summary": {
                "planned_task_count": 1,
                "accepted_shard_count": provenance.get("accepted_shards", 0),
                "merged_shard_count": provenance.get("accepted_shards", 0),
                "failed_task_count": 0,
                "blocked_task_count": 0,
                "rejected_shard_count": 0,
                "discarded_task_count": 0,
            },
            "fallback": {
                "parallel_degraded": False,
                "needs_serial_handoff": False,
                "degraded_reason": None,
            },
        }
        (run_dir / "run_status.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return payload


if __name__ == "__main__":
    unittest.main()
