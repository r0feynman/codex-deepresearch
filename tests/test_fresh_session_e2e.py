from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
        self.assertEqual(result["skill_transcript_gate"]["status"], "passed")
        self.assertIn("codex-deepresearch invoke", result["skill_transcript_gate"]["route_command"])
        self.assertEqual(result["runner_artifact_gate"]["status"], "passed")
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
        self.assertIn("SKILL INSTRUCTIONS LOADED", transcript)
        self.assertIn("Transcript kind: skill-invocation", transcript)
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

    def test_broken_skill_instructions_fail_user_facing_gate(self) -> None:
        skill_dir = self.temp_runs_dir()
        broken_skill = skill_dir / "SKILL.md"
        broken_skill.write_text(
            "---\nname: deep-research\n---\n\n"
            "# Deep Research\n\n"
            "For a normal invocation, answer directly in chat.\n",
            encoding="utf-8",
        )

        with self.assertRaises(FreshSessionE2EError) as raised:
            run_fresh_session_e2e(
                runs_dir=self.temp_runs_dir(),
                suite_id="broken-skill",
                clean=True,
                real_codex_exec="skip",
                skill_path=broken_skill,
            )

        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["skill_transcript_gate"]["status"], "failed")
        checks = payload["skill_transcript_gate"]["checks"]
        self.assertFalse(checks["skill_routes_normal_invocation_through_runner"])
        self.assertFalse(checks["skill_forbids_normal_chat_only"])

    def test_chat_only_runner_response_fails_transcript_gate(self) -> None:
        runner = self._write_chat_only_runner()

        with self.assertRaises(FreshSessionE2EError) as raised:
            run_fresh_session_e2e(
                runs_dir=self.temp_runs_dir(),
                suite_id="chat-only",
                clean=True,
                real_codex_exec="skip",
                runner_path=runner,
            )

        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "failed")
        first_scenario = payload["scenarios"][0]
        checks = {failure["check"] for failure in first_scenario["failures"]}
        self.assertIn("chat_only", checks)
        self.assertIn("missing_run_dir_without_blocked_status", checks)

    def test_auto_timeout_records_explicit_blocked_diagnostic(self) -> None:
        runner = self._write_timeout_fake_runner()

        with mock.patch("deepresearch.fresh_session_e2e.shutil.which", return_value="/tmp/fake-codex"):
            result = run_fresh_session_e2e(
                runs_dir=self.temp_runs_dir(),
                suite_id="timeout-auto",
                clean=True,
                real_codex_exec="auto",
                runner_path=runner,
                scenario_timeout_seconds=0.1,
            )

        self.assertEqual(result["status"], "passed")
        real = [
            scenario
            for scenario in result["scenarios"]
            if scenario["id"] == "real_codex_exec"
        ][0]
        self.assertTrue(real["timed_out"])
        self.assertEqual(real["terminal_outcome"], "blocked_explicit")
        self.assertIn(
            "timed out",
            real["diagnostics"]["actionable_cause"].lower(),
        )

    def test_require_timeout_fails_clearly(self) -> None:
        runner = self._write_timeout_fake_runner()

        with mock.patch("deepresearch.fresh_session_e2e.shutil.which", return_value="/tmp/fake-codex"):
            with self.assertRaises(FreshSessionE2EError) as raised:
                run_fresh_session_e2e(
                    runs_dir=self.temp_runs_dir(),
                    suite_id="timeout-require",
                    clean=True,
                    real_codex_exec="require",
                    runner_path=runner,
                    scenario_timeout_seconds=0.1,
                )

        payload = self.read_json(raised.exception.results_path)
        real = [
            scenario
            for scenario in payload["scenarios"]
            if scenario["id"] == "real_codex_exec"
        ][0]
        self.assertEqual(payload["status"], "failed")
        self.assertTrue(real["timed_out"])
        self.assertEqual(real["terminal_outcome"], "blocked_explicit")
        checks = {failure["check"] for failure in real["failures"]}
        self.assertIn("unexpected_terminal_outcome", checks)

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

    def _write_chat_only_runner(self) -> Path:
        runner_dir = self.temp_runs_dir()
        runner = runner_dir / "chat-only-runner"
        payload = {
            "schema_version": "codex-deepresearch.run-status.v0",
            "run_id": None,
            "run_dir": None,
            "selected_mode": "quick-chat",
            "status": "quick_chat_only",
            "ok": True,
            "terminal": True,
            "provenance": {"type": "quick_chat"},
            "diagnostics": {"actionable_cause": "fake chat-only response"},
            "artifacts": {},
        }
        runner.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"print(json.dumps({payload!r}))\n",
            encoding="utf-8",
        )
        runner.chmod(0o755)
        return runner

    def _write_timeout_fake_runner(self) -> Path:
        runner_dir = self.temp_runs_dir()
        runner = runner_dir / "timeout-runner"
        runner.write_text(
            """#!/usr/bin/env python3
import json
import sys
import time
from pathlib import Path

args = sys.argv[1:]
adapter = args[args.index("--adapter") + 1] if "--adapter" in args else ""
runs_dir = Path(args[args.index("--runs-dir") + 1])
run_dir = runs_dir / "fake-run"
run_dir.mkdir(parents=True, exist_ok=True)

if adapter == "codex-exec":
    time.sleep(5)

def write(path, text):
    path.write_text(text, encoding="utf-8")

artifacts = {"run_status": str(run_dir / "run_status.json")}
if adapter == "fixture":
    for name in ("report.md", "evidence.json", "report_status.json"):
        write(run_dir / name, "{}\\n" if name.endswith(".json") else "# Report\\n")
    artifacts.update({
        "report": str(run_dir / "report.md"),
        "evidence": str(run_dir / "evidence.json"),
        "report_status": str(run_dir / "report_status.json"),
    })
    payload = {
        "schema_version": "codex-deepresearch.run-status.v0",
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "selected_mode": "full-runner",
        "status": "completed_fixture",
        "ok": True,
        "terminal": True,
        "provenance": {"type": "fixture", "fixture_only": True, "accepted_shards": 1},
        "diagnostics": {"actionable_cause": "fake fixture success"},
        "artifacts": artifacts,
        "stages": {"synthesize": {"status": "completed"}},
        "shard_summary": {
            "planned_task_count": 1,
            "accepted_shard_count": 1,
            "merged_shard_count": 1,
            "failed_task_count": 0,
            "blocked_task_count": 0,
            "rejected_shard_count": 0,
            "discarded_task_count": 0,
        },
        "fallback": {"parallel_degraded": False, "needs_serial_handoff": False},
    }
else:
    payload = {
        "schema_version": "codex-deepresearch.run-status.v0",
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "selected_mode": "full-runner",
        "status": "blocked_parallel_execution",
        "ok": False,
        "terminal": True,
        "provenance": {"type": "serial_handoff", "adapter": "serial-degraded"},
        "diagnostics": {"actionable_cause": "fake serial blocked"},
        "artifacts": artifacts,
        "shard_summary": {
            "planned_task_count": 1,
            "accepted_shard_count": 0,
            "merged_shard_count": 0,
            "failed_task_count": 0,
            "blocked_task_count": 1,
            "rejected_shard_count": 0,
            "discarded_task_count": 0,
        },
        "fallback": {"parallel_degraded": False, "needs_serial_handoff": True},
    }

write(run_dir / "run_status.json", json.dumps(payload) + "\\n")
print(json.dumps(payload))
""",
            encoding="utf-8",
        )
        runner.chmod(0o755)
        return runner


if __name__ == "__main__":
    unittest.main()
