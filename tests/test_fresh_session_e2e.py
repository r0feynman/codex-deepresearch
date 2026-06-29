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

from deepresearch import fresh_session_e2e as fresh_session_module  # noqa: E402
from deepresearch.fresh_session_e2e import (  # noqa: E402
    FreshSessionE2EError,
    render_final_response,
    run_fresh_session_e2e,
    run_fresh_session_visual_e2e,
    validate_final_response,
)
from deepresearch.sanitized_real_visual_e2e import (  # noqa: E402
    run_sanitized_real_visual_e2e,
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

    def test_visual_gate_records_public_safe_blocked_provider_without_release_pass(self) -> None:
        result = run_fresh_session_visual_e2e(
            runs_dir=self.temp_runs_dir(),
            suite_id="fresh-session-visual",
            clean=True,
            real_codex_interactive="skip",
        )

        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["release_gate_passed"])
        self.assertEqual(result["release_gate_status"], "blocked_public_safe")
        scenarios = {scenario["id"]: scenario for scenario in result["scenarios"]}
        blocked = scenarios["visual_required_provider_blocked"]

        self.assertEqual(blocked["terminal_outcome"], "blocked_explicit")
        self.assertEqual(blocked["run_status"], "blocked_missing_visual_provider")
        gate = blocked["visual_release_gate"]
        self.assertFalse(gate["release_gate_passed"])
        self.assertEqual(gate["blocked_capability"], "visual_provider")
        self.assertIn("real visual acquisition provider", gate["blocked_detail"])
        self.assertTrue(Path(blocked["artifacts"]["visual_provider_status"]).is_file())
        self.assertTrue(result["acceptance"]["blocked_runs_name_missing_capability"])
        self.assertTrue(result["acceptance"]["blocked_runs_do_not_count_as_release_passes"])

    def test_visual_fixture_evidence_cannot_satisfy_release_gate(self) -> None:
        result = run_fresh_session_visual_e2e(
            runs_dir=self.temp_runs_dir(),
            suite_id="fresh-session-visual-fixture",
            clean=True,
            real_codex_interactive="skip",
        )
        fixture = {
            scenario["id"]: scenario for scenario in result["scenarios"]
        }["visual_fixture_not_release_pass"]

        self.assertEqual(fixture["terminal_outcome"], "completed_fixture")
        self.assertEqual(fixture["provenance_class"], "fixture_only")
        self.assertFalse(fixture["visual_release_gate"]["release_gate_passed"])
        self.assertEqual(fixture["visual_release_gate"]["codex_interactive_analyzed_images"], 0)
        self.assertTrue(result["acceptance"]["fixture_manual_user_evidence_excluded"])

    def test_visual_final_transcript_exposes_artifacts_and_status_summary(self) -> None:
        result = run_fresh_session_visual_e2e(
            runs_dir=self.temp_runs_dir(),
            suite_id="fresh-session-visual-transcript",
            clean=True,
            real_codex_interactive="skip",
        )
        blocked = {
            scenario["id"]: scenario for scenario in result["scenarios"]
        }["visual_required_provider_blocked"]
        transcript = Path(blocked["transcript"]).read_text(encoding="utf-8")

        self.assertIn("Visual summary:", transcript)
        self.assertIn("blocked_capability=visual_provider", transcript)
        self.assertIn("run_status.json", transcript)
        self.assertIn("visual_provider_status.json", transcript)
        self.assertIn(blocked["artifacts"]["run_status"], transcript)
        self.assertIn(blocked["artifacts"]["visual_provider_status"], transcript)
        gate_checks = blocked["visual_release_gate"]["checks"]
        self.assertTrue(gate_checks["final_transcript_exposes_required_artifacts"])
        self.assertTrue(gate_checks["final_transcript_exposes_status_summary"])

    def test_completed_auto_visual_requires_three_real_codex_images_and_report_citation(self) -> None:
        run_dir = self._deterministic_sanitized_real_visual_run_dir(image_count=3)
        run_status = self._visual_run_status(run_dir, status="completed_auto_visual")
        gate = self._visual_release_gate_with_response(run_status)

        self.assertTrue(gate["release_gate_passed"], gate)
        self.assertEqual(gate["real_candidate_count"], 10)
        self.assertEqual(gate["real_fetched_artifacts"], 3)
        self.assertEqual(gate["codex_interactive_analyzed_images"], 3)
        self.assertEqual(gate["report_cited_visual_or_mixed_claims"], 1)
        self.assertTrue(gate["visual_minimums"]["satisfied"])
        self.assertTrue(gate["checks"]["visual_provider_minimums_satisfied"])
        self.assertTrue(
            gate["visual_artifact_validation"]["valid"],
            gate["visual_artifact_validation"],
        )

    def test_completed_auto_visual_requires_ten_real_visual_candidates(self) -> None:
        run_dir = self._deterministic_sanitized_real_visual_run_dir(
            image_count=3,
            candidate_count=3,
        )
        run_status = self._visual_run_status(run_dir, status="completed_auto_visual")

        gate = self._visual_release_gate_with_response(run_status)

        self.assertFalse(gate["release_gate_passed"], gate)
        self.assertEqual(gate["real_candidate_count"], 3)
        self.assertEqual(gate["real_fetched_artifacts"], 3)
        self.assertEqual(gate["codex_interactive_analyzed_images"], 3)
        self.assertFalse(gate["checks"]["real_automatic_visual_candidates_at_least_10"])
        self.assertFalse(gate["checks"]["visual_provider_minimums_satisfied"])

    def test_sanitized_real_no_user_image_completed_run_satisfies_visual_gate(self) -> None:
        result = run_sanitized_real_visual_e2e(
            runs_dir=self.temp_runs_dir(),
            suite_id="fresh-session-sanitized-real",
            clean=True,
        )

        self.assertEqual(result["status"], "passed", result.get("failures"))
        self.assertTrue(result["release_gate_passed"])
        self.assertTrue(result["no_user_image"])
        self.assertTrue(result["sanitized_real_artifact"])
        self.assertFalse(result["live_web_fetch"])
        self.assertFalse(result["live_codex_vlm_session"])
        self.assertTrue(result["deterministic_codex_interactive_test_double"])
        self.assertEqual(result["counts"]["candidate_count"], 10)
        self.assertGreaterEqual(result["counts"]["fetched_artifacts"], 3)
        self.assertGreaterEqual(result["counts"]["codex_interactive_observations"], 3)
        self.assertGreaterEqual(
            result["counts"]["report_cited_visual_or_mixed_claims"],
            1,
        )
        self.assertTrue(result["visual_minimums"]["satisfied"])
        self.assertTrue(
            result["lineage"]["non_fixture_non_manual_non_user_provided"],
            result["lineage"],
        )

        provider_status = self.read_json(
            Path(result["artifacts"]["visual_provider_status"])
        )
        self.assertEqual(provider_status["status"], "completed_auto_visual")
        self.assertTrue(provider_status["minimums"]["satisfied"])
        self.assertEqual(provider_status["minimums"]["candidate_count"], 10)
        self.assertGreaterEqual(provider_status["minimums"]["fetched_artifacts"], 3)
        self.assertGreaterEqual(provider_status["minimums"]["vlm_images_analyzed"], 3)
        self.assertGreaterEqual(provider_status["minimums"]["report_cited_images"], 1)

    def test_completed_auto_visual_rejects_mixed_fixture_image_evidence(self) -> None:
        run_dir = self._deterministic_sanitized_real_visual_run_dir(image_count=3)
        run_status = self._visual_run_status(run_dir, status="completed_auto_visual")
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["images"].append(
            {
                "id": "img_fixture",
                "provider_mode": "fixture",
                "analysis_provider": "fixture",
                "analysis_status": "analyzed",
                "observations": ["fixture-only observation"],
            }
        )
        self._write_json(run_dir / "evidence.json", evidence)

        gate = self._visual_release_gate_with_response(run_status)

        self.assertFalse(gate["release_gate_passed"], gate)
        self.assertEqual(gate["non_release_visual_images"], 1)
        self.assertFalse(gate["checks"]["fixture_manual_user_evidence_excluded"])
        self.assertFalse(gate["checks"]["forbidden_visual_lineage_excluded"])
        self.assertIn(
            "evidence_images_has_fixture_manual_or_user_lineage",
            gate["forbidden_visual_lineage"]["failures"],
        )

    def test_supplied_completed_visual_run_rejects_fixture_candidate_lineage(self) -> None:
        run_dir = self._deterministic_sanitized_real_visual_run_dir(image_count=3)
        self._visual_run_status(run_dir, status="completed_auto_visual")
        candidates = self._read_jsonl(run_dir / "visual_candidates.jsonl")
        fixture_candidate = dict(candidates[0])
        fixture_candidate.update(
            {
                "candidate_id": "cand_fixture_999",
                "provider": "fixture-image-search",
                "provider_kind": "fixture",
                "provider_mode": "fixture",
                "provider_provenance": {
                    "provider": "fixture-image-search",
                    "provider_kind": "fixture",
                    "provider_mode": "fixture",
                },
                "rank": 999,
            }
        )
        candidates.append(fixture_candidate)
        self._write_jsonl(run_dir / "visual_candidates.jsonl", candidates)

        gate = self._failed_supplied_visual_release_gate(
            run_dir,
            suite_id="fresh-session-visual-fixture-candidate",
        )

        self._assert_forbidden_visual_lineage_failure(
            gate,
            "visual_candidates_has_fixture_manual_or_user_lineage",
        )

    def test_supplied_completed_visual_run_rejects_manual_fetch_lineage(self) -> None:
        run_dir = self._deterministic_sanitized_real_visual_run_dir(image_count=3)
        self._visual_run_status(run_dir, status="completed_auto_visual")
        fetches = self._read_jsonl(run_dir / "image_fetch_status.jsonl")
        fetches[0]["manual"] = True
        self._write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)

        gate = self._failed_supplied_visual_release_gate(
            run_dir,
            suite_id="fresh-session-visual-manual-fetch",
        )

        self._assert_forbidden_visual_lineage_failure(
            gate,
            "image_fetch_status_has_fixture_manual_or_user_lineage",
        )

    def test_supplied_completed_visual_run_rejects_user_observation_lineage(self) -> None:
        run_dir = self._deterministic_sanitized_real_visual_run_dir(image_count=3)
        self._visual_run_status(run_dir, status="completed_auto_visual")
        observations = self._read_jsonl(run_dir / "visual_observations.jsonl")
        observations[0]["user_provided"] = True
        self._write_jsonl(run_dir / "visual_observations.jsonl", observations)

        gate = self._failed_supplied_visual_release_gate(
            run_dir,
            suite_id="fresh-session-visual-user-observation",
        )

        self._assert_forbidden_visual_lineage_failure(
            gate,
            "visual_observations_has_fixture_manual_or_user_lineage",
        )

    def test_completed_auto_visual_rejects_fixture_report_citation_lineage(self) -> None:
        run_dir = self._deterministic_sanitized_real_visual_run_dir(image_count=3)
        run_status = self._visual_run_status(run_dir, status="completed_auto_visual")
        report_status = self.read_json(run_dir / "report_status.json")
        report_status["included_claims"][0]["provider_mode"] = "fixture"
        self._write_json(run_dir / "report_status.json", report_status)

        gate = self._visual_release_gate_with_response(run_status)

        self._assert_forbidden_visual_lineage_failure(
            gate,
            "report_status_included_claims_has_fixture_manual_or_user_lineage",
        )

    def test_completed_auto_visual_counts_only_real_vlm_observation_records(self) -> None:
        run_dir = self._deterministic_sanitized_real_visual_run_dir(image_count=1)
        run_status = self._visual_run_status(run_dir, status="completed_auto_visual")
        evidence = self.read_json(run_dir / "evidence.json")
        for index in (2, 3):
            image = dict(evidence["images"][0])
            image["id"] = f"img_padded_{index:03d}"
            image["analysis_provider"] = "codex-interactive"
            image["analysis_status"] = "analyzed"
            image["provider_mode"] = "real"
            image["observations"] = ["evidence-only observation should not count"]
            evidence["images"].append(image)
        self._write_json(run_dir / "evidence.json", evidence)

        gate = self._visual_release_gate_with_response(run_status)

        self.assertFalse(gate["release_gate_passed"], gate)
        self.assertEqual(gate["codex_interactive_analyzed_images"], 1)
        self.assertFalse(
            gate["checks"]["codex_interactive_analyzed_non_fixture_images_at_least_3"]
        )

    def test_completed_auto_visual_requires_linked_evidence_image_records(self) -> None:
        run_dir = self._deterministic_sanitized_real_visual_run_dir(image_count=3)
        run_status = self._visual_run_status(run_dir, status="completed_auto_visual")
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["images"] = []
        self._write_json(run_dir / "evidence.json", evidence)

        gate = self._visual_release_gate_with_response(run_status)

        self.assertFalse(gate["release_gate_passed"], gate)
        self.assertEqual(gate["codex_interactive_analyzed_images"], 0)
        self.assertFalse(
            gate["checks"]["codex_interactive_analyzed_non_fixture_images_at_least_3"]
        )

    def test_completed_auto_visual_rejects_failed_observation_statuses(self) -> None:
        run_dir = self._deterministic_sanitized_real_visual_run_dir(image_count=3)
        run_status = self._visual_run_status(run_dir, status="completed_auto_visual")
        observations = [
            json.loads(line)
            for line in (run_dir / "visual_observations.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        observations[1]["observation_status"] = "failed"
        observations[2]["observation_status"] = "needs_manual_review"
        self._write_jsonl(run_dir / "visual_observations.jsonl", observations)

        gate = self._visual_release_gate_with_response(run_status)

        self.assertFalse(gate["release_gate_passed"], gate)
        self.assertEqual(gate["codex_interactive_analyzed_images"], 1)
        self.assertFalse(
            gate["checks"]["codex_interactive_analyzed_non_fixture_images_at_least_3"]
        )

    def test_completed_auto_visual_report_citation_must_use_codex_observed_image(self) -> None:
        run_dir = self._deterministic_sanitized_real_visual_run_dir(image_count=3)
        run_status = self._visual_run_status(run_dir, status="completed_auto_visual")
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["images"].append(
            {
                "id": "img_fixture",
                "provider_mode": "fixture",
                "analysis_provider": "fixture",
                "analysis_status": "analyzed",
                "observations": ["fixture-only observation"],
            }
        )
        evidence["claims"].append(
            {
                "id": "claim_fixture_visual",
                "text": "Fixture visual evidence should not release the gate.",
                "claim_type": "visual",
                "supporting_sources": [],
                "supporting_images": ["img_fixture"],
                "quote_spans": [],
                "votes": [],
                "verification_status": "supported",
                "review_status": "human_accepted",
                "promotion_status": "not_eligible",
                "confidence": "high",
                "caveats": [],
            }
        )
        self._write_json(run_dir / "evidence.json", evidence)
        self._write_json(
            run_dir / "report_status.json",
            {
                "status": "completed",
                "report_path": "report.md",
                "used_images": ["img_fixture"],
                "included_claims": [
                    {
                        "claim_id": "claim_fixture_visual",
                        "claim_type": "visual",
                        "image_ids": ["img_fixture"],
                    }
                ],
            },
        )

        gate = self._visual_release_gate_with_response(run_status)

        self.assertFalse(gate["release_gate_passed"], gate)
        self.assertEqual(gate["report_cited_visual_or_mixed_claims"], 0)
        self.assertFalse(gate["checks"]["report_cited_visual_or_mixed_claim_at_least_1"])

    def test_completed_auto_visual_requires_report_markdown_visual_claim_citation(self) -> None:
        run_dir = self._deterministic_sanitized_real_visual_run_dir(image_count=3)
        run_status = self._visual_run_status(run_dir, status="completed_auto_visual")
        (run_dir / "report.md").write_text("# Report\n\n## Visual Findings\n", encoding="utf-8")

        gate = self._visual_release_gate_with_response(run_status)

        self.assertFalse(gate["release_gate_passed"], gate)
        self.assertEqual(gate["report_cited_visual_or_mixed_claims"], 0)
        self.assertFalse(gate["checks"]["report_cited_visual_or_mixed_claim_at_least_1"])

    def test_completed_auto_visual_requires_completed_report_status(self) -> None:
        run_dir = self._deterministic_sanitized_real_visual_run_dir(image_count=3)
        run_status = self._visual_run_status(run_dir, status="completed_auto_visual")
        report_status = self.read_json(run_dir / "report_status.json")
        report_status["status"] = "failed_visual_evidence_unused"
        self._write_json(run_dir / "report_status.json", report_status)

        gate = self._visual_release_gate_with_response(run_status)

        self.assertFalse(gate["release_gate_passed"], gate)
        self.assertFalse(gate["checks"]["report_status_completed"])

    def test_completed_auto_visual_requires_matching_visual_provider_status(self) -> None:
        run_dir = self._deterministic_sanitized_real_visual_run_dir(image_count=3)
        run_status = self._visual_run_status(run_dir, status="completed_auto_visual")
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        provider_status["status"] = "partial_auto_visual"
        provider_status["ok"] = False
        provider_status["metric_classification"] = "included_failure"
        self._write_json(run_dir / "visual_provider_status.json", provider_status)

        gate = self._visual_release_gate_with_response(run_status)

        self.assertFalse(gate["release_gate_passed"], gate)
        self.assertFalse(gate["checks"]["visual_provider_status_completed_auto_visual"])

    def test_visual_gate_accepts_supplied_completed_auto_visual_run_for_release_pass(self) -> None:
        completed_run_dir = self._deterministic_sanitized_real_visual_run_dir(
            image_count=3
        )
        self._visual_run_status(completed_run_dir, status="completed_auto_visual")

        result = run_fresh_session_visual_e2e(
            runs_dir=self.temp_runs_dir(),
            suite_id="fresh-session-visual-completed",
            clean=True,
            real_codex_interactive="require",
            completed_auto_visual_run=completed_run_dir,
        )

        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["release_gate_passed"])
        self.assertEqual(result["release_gate_status"], "passed")
        self.assertEqual(
            result["completed_auto_visual_run"],
            str(completed_run_dir.resolve()),
        )
        completed = {
            scenario["id"]: scenario for scenario in result["scenarios"]
        }["visual_completed_auto_release_candidate"]
        self.assertEqual(completed["terminal_outcome"], "completed_auto_visual")
        self.assertTrue(completed["visual_release_gate"]["release_gate_passed"])
        self.assertEqual(
            completed["visual_release_gate"]["codex_interactive_analyzed_images"],
            3,
        )
        self.assertGreaterEqual(
            completed["visual_release_gate"]["real_candidate_count"],
            10,
        )
        self.assertGreaterEqual(
            completed["visual_release_gate"]["real_fetched_artifacts"],
            3,
        )
        self.assertGreaterEqual(
            completed["visual_release_gate"]["report_cited_visual_or_mixed_claims"],
            1,
        )
        self.assertTrue(
            completed["visual_release_gate"]["visual_minimums"]["satisfied"]
        )
        self.assertTrue(
            completed["visual_release_gate"]["checks"]["visual_provider_minimums_satisfied"]
        )

    def test_visual_gate_rejects_supplied_completed_run_below_candidate_floor(self) -> None:
        completed_run_dir = self._deterministic_sanitized_real_visual_run_dir(
            image_count=3,
            candidate_count=3,
        )
        self._visual_run_status(completed_run_dir, status="completed_auto_visual")

        with self.assertRaises(FreshSessionE2EError) as raised:
            run_fresh_session_visual_e2e(
                runs_dir=self.temp_runs_dir(),
                suite_id="fresh-session-visual-low-candidates",
                clean=True,
                real_codex_interactive="require",
                completed_auto_visual_run=completed_run_dir,
            )

        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "failed")
        self.assertFalse(payload["release_gate_passed"])
        completed = {
            scenario["id"]: scenario for scenario in payload["scenarios"]
        }["visual_completed_auto_release_candidate"]
        gate = completed["visual_release_gate"]
        self.assertFalse(gate["release_gate_passed"], gate)
        self.assertEqual(gate["real_candidate_count"], 3)
        self.assertEqual(gate["real_fetched_artifacts"], 3)
        self.assertEqual(gate["codex_interactive_analyzed_images"], 3)
        self.assertFalse(
            gate["checks"]["real_automatic_visual_candidates_at_least_10"]
        )
        self.assertFalse(gate["checks"]["visual_provider_minimums_satisfied"])
        self.assertIn(
            "real_automatic_visual_candidates_at_least_10",
            {failure["check"] for failure in completed["failures"]},
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

    def _deterministic_sanitized_real_visual_run_dir(
        self,
        *,
        image_count: int,
        candidate_count: int = 10,
    ) -> Path:
        """Create a public-safe replay with real-mode provenance and no live fetch."""

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        run_dir = Path(temp_dir.name)
        created_at = "2026-06-25T00:00:00Z"
        task_id = "task_visual_001"
        angle_id = "angle_001"
        claim_id = "claim_visual_001"
        vote_id = "vote_visual_001"

        candidates = []
        fetches = []
        observations = []
        images = []
        for index in range(1, candidate_count + 1):
            candidate = self._visual_candidate(
                candidate_id=f"cand_real_{index:03d}",
                task_id=task_id,
                angle_id=angle_id,
                rank=index,
                candidate_status="analyzed" if index <= image_count else "ranked",
            )
            candidates.append(candidate)
            if index <= image_count:
                image_id = f"img_real_{index:03d}"
                fetch = self._visual_fetch(
                    candidate=candidate,
                    fetch_id=f"fetch_real_{index:03d}",
                    evidence_image_id=image_id,
                )
                fetches.append(fetch)
                observations.append(
                    self._visual_observation(
                        candidate=candidate,
                        fetch=fetch,
                        evidence_image_id=image_id,
                        claim_id=claim_id if index == 1 else None,
                        vote_id=vote_id if index == 1 else None,
                        created_at=created_at,
                    )
                )
                images.append(
                    self._visual_evidence_image(
                        candidate=candidate,
                        fetch=fetch,
                        evidence_image_id=image_id,
                    )
                )

        self._write_json(
            run_dir / "visual_tasks.json",
            {"tasks": [{"id": task_id, "angle_id": angle_id, "route": "visual_required"}]},
        )
        self._write_json(
            run_dir / "visual_search_plan.json",
            {
                "schema_version": "codex-deepresearch.visual-artifacts.v0",
                "run_id": run_dir.name,
                "created_at": created_at,
                "tasks": [
                    {
                        "plan_id": "plan_visual_001",
                        "task_id": task_id,
                        "angle_id": angle_id,
                        "route": "visual_required",
                        "target_evidence_type": "web_image",
                        "query": "codex interactive sanitized-real visual release replay",
                        "providers": ["page-image-extractor", "codex-interactive"],
                        "source_search_result_ids": [],
                        "caps": {
                            "max_candidates": candidate_count,
                            "max_fetches": image_count,
                            "max_vlm_images": image_count,
                            "max_cost_usd": 1.0,
                        },
                        "policy_constraints": {"robots": "allowed"},
                        "estimated_cost_usd": 0.1,
                        "state": "completed",
                    }
                ],
            },
        )
        self._write_jsonl(run_dir / "visual_candidates.jsonl", candidates)
        self._write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)
        self._write_jsonl(run_dir / "visual_observations.jsonl", observations)
        self._write_json(
            run_dir / "visual_provider_status.json",
            {
                "schema_version": "codex-deepresearch.visual-provider-status.v0",
                "run_id": run_dir.name,
                "status": "completed_auto_visual",
                "ok": True,
                "terminal": True,
                "metric_classification": "success",
                "minimums": {
                    "required_vlm_images": 3,
                    "candidate_count": candidate_count,
                    "selected_candidates": image_count,
                    "fetched_artifacts": image_count,
                    "vlm_images_analyzed": image_count,
                    "report_cited_images": 1 if image_count >= 3 else 0,
                    "satisfied": image_count >= 3,
                    "shortfall_reason": "none"
                    if image_count >= 3
                    else "insufficient_candidates",
                },
                "providers": [
                    self._visual_provider(
                        provider="page-image-extractor",
                        provider_kind="page_extractor",
                        invocations=1,
                        candidates_discovered=candidate_count,
                        artifacts_fetched=image_count,
                        vlm_images_analyzed=0,
                    ),
                    self._visual_provider(
                        provider="codex-interactive",
                        provider_kind="vlm",
                        invocations=1,
                        candidates_discovered=0,
                        artifacts_fetched=image_count,
                        vlm_images_analyzed=image_count,
                    ),
                ],
            },
        )
        self._write_json(
            run_dir / "evidence.json",
            {
                "schema_version": "0.1.0",
                "run_id": run_dir.name,
                "created_at": created_at,
                "question": "Visual release sanitized-real replay",
                "mode": "codex-plugin",
                "search_provider": "codex-native",
                "vlm_provider": "codex-interactive",
                "search_tasks": [],
                "images": images,
                "claims": [
                    {
                        "id": claim_id,
                        "text": (
                            "The first sanitized-real replay image contains "
                            "report-cited UI evidence."
                        ),
                        "claim_type": "visual",
                        "supporting_sources": [],
                        "supporting_images": ["img_real_001"],
                        "visual_supports": [
                            {
                                "image_id": "img_real_001",
                                "observation_ref": "images.img_real_001.observations[0]",
                                "observation_index": 0,
                                "observation_text": images[0]["observations"][0],
                                "provider": "codex-interactive",
                                "confidence": 0.92,
                            }
                        ],
                        "quote_spans": [],
                        "votes": [{"id": vote_id}],
                        "verification_status": "supported",
                        "review_status": "human_accepted",
                        "promotion_status": "not_eligible",
                        "confidence": "high",
                        "caveats": [],
                    }
                ],
            },
        )
        self._write_json(
            run_dir / "report_status.json",
            {
                "status": "completed",
                "report_path": "report.md",
                "used_images": ["img_real_001"],
                "included_claims": [
                    {
                        "claim_id": claim_id,
                        "claim_type": "visual",
                        "image_ids": ["img_real_001"],
                        "visual_supports": [
                            {
                                "image_id": "img_real_001",
                                "observation_ref": "images.img_real_001.observations[0]",
                            }
                        ],
                    }
                ],
            },
        )
        (run_dir / "report.md").write_text(
            "# Report\n\n"
            "## Visual Findings\n"
            f"- Claim `{claim_id}`: The first sanitized-real replay image contains "
            "report-cited UI evidence.\n"
            "- Image `img_real_001` (visual_match; provider `codex-interactive`): "
            f"{images[0]['observations'][0]}\n",
            encoding="utf-8",
        )
        return run_dir

    def _visual_run_status(self, run_dir: Path, *, status: str) -> dict:
        payload = {
            "schema_version": "codex-deepresearch.run-status.v0",
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "selected_mode": "full-runner",
            "status": status,
            "ok": True,
            "terminal": True,
            "provenance": {
                "type": "real_child_execution",
                "adapter": "codex-exec",
                "fixture_only": False,
                "manual_handoff": False,
                "real_child_execution": True,
                "accepted_shards": 1,
            },
            "diagnostics": {
                "actionable_cause": (
                    "completed sanitized-real visual replay; no live web fetch "
                    "or live Codex VLM call"
                )
            },
            "artifacts": {
                "run_status": str(run_dir / "run_status.json"),
                "evidence": str(run_dir / "evidence.json"),
                "report": str(run_dir / "report.md"),
                "report_status": str(run_dir / "report_status.json"),
                "visual_tasks": str(run_dir / "visual_tasks.json"),
                "visual_observations": str(run_dir / "visual_observations.jsonl"),
                "visual_provider_status": str(run_dir / "visual_provider_status.json"),
                "visual_candidates": str(run_dir / "visual_candidates.jsonl"),
                "image_fetch_status": str(run_dir / "image_fetch_status.jsonl"),
            },
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
        self._write_json(run_dir / "run_status.json", payload)
        return payload

    def _visual_release_gate_with_response(self, run_status: dict) -> dict:
        initial_gate = fresh_session_module._visual_release_gate(
            run_status,
            final_response="",
            scenario_id="completed_visual",
            check_final_response=False,
        )
        response_status = dict(run_status)
        response_status["visual_release_gate"] = fresh_session_module._visual_release_summary(
            initial_gate
        )
        response = render_final_response(response_status, scenario_id="completed_visual")
        return fresh_session_module._visual_release_gate(
            run_status,
            final_response=response,
            scenario_id="completed_visual",
            check_final_response=True,
        )

    def _failed_supplied_visual_release_gate(
        self,
        run_dir: Path,
        *,
        suite_id: str,
    ) -> dict:
        with self.assertRaises(FreshSessionE2EError) as raised:
            run_fresh_session_visual_e2e(
                runs_dir=self.temp_runs_dir(),
                suite_id=suite_id,
                clean=True,
                real_codex_interactive="require",
                completed_auto_visual_run=run_dir,
            )

        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "failed")
        self.assertFalse(payload["release_gate_passed"])
        completed = {
            scenario["id"]: scenario for scenario in payload["scenarios"]
        }["visual_completed_auto_release_candidate"]
        return completed["visual_release_gate"]

    def _assert_forbidden_visual_lineage_failure(
        self,
        gate: dict,
        expected_detail: str,
    ) -> None:
        self.assertFalse(gate["release_gate_passed"], gate)
        self.assertFalse(gate["checks"]["forbidden_visual_lineage_excluded"])
        self.assertFalse(gate["checks"]["fixture_manual_user_evidence_excluded"])
        self.assertIn(expected_detail, gate["forbidden_visual_lineage"]["failures"])
        details = [
            failure["detail"]
            for failure in gate["failures"]
            if failure["check"] == "forbidden_visual_lineage_excluded"
        ]
        self.assertTrue(
            any(expected_detail in detail for detail in details),
            gate["failures"],
        )

    def _read_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _visual_candidate(
        self,
        *,
        candidate_id: str,
        task_id: str,
        angle_id: str,
        rank: int,
        candidate_status: str = "analyzed",
    ) -> dict:
        return {
            "candidate_id": candidate_id,
            "plan_id": "plan_visual_001",
            "task_id": task_id,
            "angle_id": angle_id,
            "provider": "page-image-extractor",
            "provider_kind": "page_extractor",
            "provider_mode": "real",
            "provider_run_id": "run_page_image_extractor_001",
            "provider_provenance": {
                "provider": "page-image-extractor",
                "provider_kind": "page_extractor",
                "provider_mode": "real",
            },
            "origin": "page_image",
            "page_url": "https://example.com/page",
            "image_url": f"https://example.com/{candidate_id}.png",
            "rank": rank,
            "score": 1.0,
            "policy_decision": "allowed",
            "policy_flags": [],
            "candidate_status": candidate_status,
            "rejection_reason": None,
            "estimated_cost_usd": 0.01,
            "actual_cost_usd": 0.01,
        }

    def _visual_fetch(self, *, candidate: dict, fetch_id: str, evidence_image_id: str) -> dict:
        return {
            "fetch_id": fetch_id,
            "candidate_id": candidate["candidate_id"],
            "task_id": candidate["task_id"],
            "angle_id": candidate["angle_id"],
            "provider": candidate["provider"],
            "provider_kind": candidate["provider_kind"],
            "provider_mode": candidate["provider_mode"],
            "provider_run_id": candidate["provider_run_id"],
            "provider_provenance": dict(candidate["provider_provenance"]),
            "fetch_status": "fetched",
            "http_status": 200,
            "mime_type": "image/png",
            "byte_size": 128,
            "width": 640,
            "height": 360,
            "hash": f"sha256:{evidence_image_id}",
            "phash": f"phash:{evidence_image_id}",
            "local_artifact_path": f"images/{evidence_image_id}.png",
            "evidence_image_id": evidence_image_id,
            "policy_decision": "allowed",
            "policy_flags": [],
            "failure_code": None,
            "estimated_cost_usd": 0.01,
            "actual_cost_usd": 0.01,
        }

    def _visual_observation(
        self,
        *,
        candidate: dict,
        fetch: dict,
        evidence_image_id: str,
        claim_id: str | None,
        vote_id: str | None,
        created_at: str,
    ) -> dict:
        verifier_links = []
        report_links = []
        if claim_id:
            verifier_links.append(
                {
                    "claim_id": claim_id,
                    "visual_support_ref": f"images.{evidence_image_id}.observations[0]",
                    "verifier_vote_id": vote_id,
                }
            )
            report_links.append(
                {
                    "claim_id": claim_id,
                    "report_section_id": "visual-findings",
                    "citation_id": f"img:{evidence_image_id}",
                }
            )
        return {
            "observation_id": f"obs_{evidence_image_id}",
            "evidence_image_id": evidence_image_id,
            "task_id": candidate["task_id"],
            "angle_id": candidate["angle_id"],
            "candidate_id": candidate["candidate_id"],
            "fetch_id": fetch["fetch_id"],
            "provider": "codex-interactive",
            "provider_kind": "vlm",
            "provider_mode": "real",
            "provider_run_id": "run_codex_interactive_001",
            "provider_provenance": {
                "provider": "codex-interactive",
                "provider_kind": "vlm",
                "provider_mode": "real",
            },
            "model_or_tool": "codex-interactive",
            "observation_status": "analyzed",
            "observations": [f"Codex-interactive visual observation for {evidence_image_id}."],
            "inferences": [],
            "confidence": 0.93,
            "policy_decision": "allowed",
            "policy_flags": [],
            "caveats": [],
            "verifier_links": verifier_links,
            "report_links": report_links,
            "estimated_cost_usd": 0.02,
            "actual_cost_usd": 0.02,
            "created_at": created_at,
        }

    def _visual_evidence_image(self, *, candidate: dict, fetch: dict, evidence_image_id: str) -> dict:
        return {
            "id": evidence_image_id,
            "source_id": "src_visual_001",
            "origin": "page_image",
            "image_url": candidate["image_url"],
            "page_url": candidate["page_url"],
            "local_artifact_path": fetch["local_artifact_path"],
            "mime_type": fetch["mime_type"],
            "width": fetch["width"],
            "height": fetch["height"],
            "observations": [f"Codex-interactive visual observation for {evidence_image_id}."],
            "inferences": [],
            "visual_tasks": [candidate["task_id"]],
            "analysis_provider": "codex-interactive",
            "analysis_status": "analyzed",
            "policy_flags": [],
            "caveats": [],
            "task_id": candidate["task_id"],
            "angle_id": candidate["angle_id"],
            "candidate_id": candidate["candidate_id"],
            "fetch_id": fetch["fetch_id"],
            "hash": fetch["hash"],
            "provider": candidate["provider"],
            "provider_kind": candidate["provider_kind"],
            "provider_mode": candidate["provider_mode"],
            "provider_provenance": dict(candidate["provider_provenance"]),
            "policy_decision": "allowed",
            "estimated_cost_usd": 0.03,
            "actual_cost_usd": 0.03,
        }

    def _visual_provider(
        self,
        *,
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
            "estimated_cost_usd": 0.1,
            "actual_cost_usd": 0.1,
            "last_error": None,
        }

    def _write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _write_jsonl(self, path: Path, records: list[dict]) -> None:
        path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

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
