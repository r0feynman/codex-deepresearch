from __future__ import annotations

import json
import os
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

from deepresearch import MvpSmokeError, run_mvp_smoke
import deepresearch.mvp_smoke as mvp_smoke


class MvpSmokeTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def fake_codex_bin(self, runs_dir: Path) -> Path:
        bin_dir = runs_dir / "bin"
        bin_dir.mkdir()
        codex = bin_dir / "codex"
        codex.write_text("#!/bin/sh\nprintf 'codex test fixture\\n'\n", encoding="utf-8")
        codex.chmod(0o755)
        return bin_dir

    def test_mvp_smoke_generates_release_gate_fixtures(self) -> None:
        with mock.patch.object(mvp_smoke.shutil, "which", return_value="/tmp/fake-codex"):
            result = run_mvp_smoke(
                runs_dir=self.temp_runs_dir(),
                suite_id="suite",
                clean=True,
                invoke="$deep-research: deterministic MVP smoke",
            )

        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["invocation_validation"]["invocation_valid"])
        self.assertTrue(result["install_update_smoke"]["checks"]["codex_cli_available"])
        self.assertEqual(result["totals"]["text_only"], 3)
        self.assertEqual(result["totals"]["visual_required"], 3)
        self.assertEqual(result["totals"]["visual_optional"], 2)
        self.assertTrue(all(result["acceptance"].values()))
        self.assertEqual(result["install_update_smoke"]["status"], "passed")

        text_invocation = result["fixtures"]["text_only"][0]
        text_run = Path(text_invocation["run_dir"])
        self.assertTrue((text_run / "evidence.json").is_file())
        self.assertTrue((text_run / "report.md").is_file())
        text_evidence = self.read_json(text_run / "evidence.json")
        self.assertEqual(text_evidence["routing"][0]["modality"], "text_only")
        self.assertEqual(text_evidence["images"], [])
        self.assertEqual(self.read_jsonl(text_run / "visual_observations.jsonl"), [])

        visual_required = result["fixtures"]["visual_required"][0]
        visual_required_run = Path(visual_required["run_dir"])
        visual_votes = [
            vote
            for vote in self.read_jsonl(visual_required_run / "verifier_votes.jsonl")
            if vote["verifier_type"] == "visual"
        ]
        self.assertGreaterEqual(len(visual_votes), 1)
        self.assertEqual(visual_required["vision_adapter_status"], "visual_evidence_ingested")

        optional_pruned = result["fixtures"]["visual_optional"][1]
        optional_run = Path(optional_pruned["run_dir"])
        optional_evidence = self.read_json(optional_run / "evidence.json")
        self.assertEqual(optional_pruned["optional_visual_mode"], "budget_pruned_no_visual")
        self.assertEqual(optional_evidence["claims"][0]["verification_status"], "budget_pruned")
        self.assertEqual(self.read_jsonl(optional_run / "verifier_votes.jsonl"), [])

    def test_cli_mvp_smoke_outputs_machine_readable_results(self) -> None:
        runs_dir = self.temp_runs_dir()
        fake_bin = self.fake_codex_bin(runs_dir)
        command = subprocess.run(
            [
                str(RUNNER),
                "mvp-smoke",
                "--runs-dir",
                str(runs_dir),
                "--suite-id",
                "cli-suite",
                "--clean",
                "--invoke",
                "$deep-research: CLI MVP smoke",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"},
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["status"], "passed")
        results_path = Path(payload["artifacts"]["results"])
        self.assertTrue(results_path.is_file())
        persisted = self.read_json(results_path)
        self.assertEqual(persisted["schema_version"], "codex-deepresearch.mvp-smoke.v0")
        self.assertEqual(persisted["totals"]["run_artifacts"], 8)

    def test_invalid_invocation_fails_and_writes_results(self) -> None:
        runs_dir = self.temp_runs_dir()

        with mock.patch.object(mvp_smoke.shutil, "which", return_value="/tmp/fake-codex"):
            with self.assertRaises(MvpSmokeError) as raised:
                run_mvp_smoke(
                    runs_dir=runs_dir,
                    suite_id="bad-invoke",
                    clean=True,
                    invoke="not a deep research invocation",
                )

        results_path = raised.exception.results_path
        self.assertIsNotNone(results_path)
        payload = self.read_json(results_path)
        self.assertEqual(payload["status"], "failed")
        self.assertFalse(payload["invocation_validation"]["invocation_valid"])
        self.assertFalse(payload["acceptance"]["deep_research_invocation_valid"])
        self.assertEqual(payload["failures"][0]["check"], "invocation_validation")

    def test_missing_codex_cli_fails_install_update_acceptance(self) -> None:
        runs_dir = self.temp_runs_dir()

        with mock.patch.object(mvp_smoke.shutil, "which", return_value=None):
            with self.assertRaises(MvpSmokeError) as raised:
                run_mvp_smoke(
                    runs_dir=runs_dir,
                    suite_id="missing-codex",
                    clean=True,
                    invoke="$deep-research: missing codex CLI",
                )

        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["install_update_smoke"]["status"], "failed")
        self.assertFalse(payload["install_update_smoke"]["checks"]["codex_cli_available"])
        self.assertFalse(payload["acceptance"]["plugin_install_update_smoke_passes"])

    def test_skipped_codex_cli_check_is_not_reported_as_pass(self) -> None:
        runs_dir = self.temp_runs_dir()

        with mock.patch.object(mvp_smoke.shutil, "which", return_value=None):
            with self.assertRaises(MvpSmokeError) as raised:
                run_mvp_smoke(
                    runs_dir=runs_dir,
                    suite_id="skipped-codex",
                    clean=True,
                    invoke="$deep-research: skipped codex CLI",
                    skip_codex_cli_install_check=True,
                )

        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["install_update_smoke"]["status"], "skipped")
        self.assertTrue(payload["skips"]["codex_cli_install_check"])
        self.assertFalse(payload["acceptance"]["plugin_install_update_smoke_passes"])

    def test_fixture_failure_writes_failed_results_with_stage(self) -> None:
        runs_dir = self.temp_runs_dir()

        with mock.patch.object(mvp_smoke.shutil, "which", return_value="/tmp/fake-codex"):
            with mock.patch.object(mvp_smoke, "verify_claims", side_effect=RuntimeError("forced verify failure")):
                with self.assertRaises(MvpSmokeError) as raised:
                    run_mvp_smoke(
                        runs_dir=runs_dir,
                        suite_id="fixture-failure",
                        clean=True,
                        invoke="$deep-research: fixture failure",
                    )

        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "failed")
        self.assertTrue(payload["failures"])
        first_failure = payload["failures"][0]
        self.assertEqual(first_failure["fixture_id"], "text_001_deep_research_invocation")
        self.assertEqual(first_failure["stage"], "verify_claims")
        failed_fixture = payload["fixtures"]["text_only"][0]
        self.assertEqual(failed_fixture["status"], "failed")
        self.assertEqual(failed_fixture["failed_stage"], "verify_claims")


if __name__ == "__main__":
    unittest.main()
