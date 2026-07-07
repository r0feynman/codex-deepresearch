from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "plugins" / "codex-deepresearch" / "scripts" / "codex-deepresearch"
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch.public_beta_validation import (  # noqa: E402
    DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST,
    DEFAULT_BLIND_HOLDOUT_MANIFEST,
    DEFAULT_MANUAL_TRACE_AUDIT_MANIFEST,
    DEFAULT_PUBLIC_BETA_SEMANTIC_MANIFEST,
    DEFAULT_SEMANTIC_REGRESSION_MANIFEST,
    SEMANTIC_RELEASE_VALIDATION_RESULTS_FILENAME,
    EXTERNAL_GATE_REQUIREMENTS,
    PublicBetaValidationError,
    _AUTOMATED_VISUAL_REQUIRED_ACCEPTANCE,
    _AUTOMATED_VISUAL_REQUIRED_ARTIFACTS,
    _AUTOMATED_VISUAL_REQUIRED_SCENARIOS,
    _FRESH_SESSION_REQUIRED_ACCEPTANCE,
    _FRESH_SESSION_VISUAL_REQUIRED_ACCEPTANCE,
    evaluate_public_beta_prompt_run,
    load_blind_holdout_manifest,
    load_manual_trace_audits,
    load_public_beta_prompt_manifest,
    load_public_beta_semantic_manifest,
    load_semantic_regression_manifest,
    run_public_beta_validation,
    run_semantic_anti_overfit_scan,
    run_semantic_release_validation,
    semantic_manifest_execution_gate,
)
from deepresearch.semantic_planner import write_semantic_materialization_diff  # noqa: E402
from deepresearch.visual_artifacts import visual_release_minimums  # noqa: E402


class PublicBetaValidationTests(unittest.TestCase):
    def temp_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def assert_semantic_artifact_integrity_failure(
        self,
        result: dict[str, Any],
        *,
        artifact: str,
        field: str,
    ) -> None:
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["metric_classification"], "included_failure")
        failures = result["semantic_release_checks"]["failures"]
        self.assertTrue(
            any(
                failure.get("check") == "semantic_artifact_integrity"
                and failure.get("artifact") == artifact
                and failure.get("field") == field
                for failure in failures
            ),
            failures,
        )

    def read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_manual_trace_audit_manifest(
        self,
        root: Path,
        *,
        live: bool = True,
        fixture_only: bool = False,
    ) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        artifact_root = root / "manual_trace_artifacts"
        chain_steps = [
            "original_question",
            "locked_oracle",
            "semantic_plan",
            "semantic_review",
            "materialization_diff",
            "research_tasks",
            "search_tasks",
            "visual_tasks",
            "accepted_shards",
            "final_report",
        ]
        audits = []
        for index in range(1, 6):
            audit_id = f"audit_{index:03d}"
            run_id = f"manual-audit-run-{index:03d}"
            prompt_id = f"manual-audit-{index:03d}"
            route = "visual_required" if index <= 2 else "text_only"
            categories = []
            if index <= 2:
                categories.extend(["korean", "visual"])
            elif index == 3:
                categories.append("korean")
            else:
                categories.append("policy_regulatory_domain")
            oracle_hash = hashlib.sha256(f"{audit_id}:oracle".encode("utf-8")).hexdigest()
            semantic_plan_hash = hashlib.sha256(f"{audit_id}:plan".encode("utf-8")).hexdigest()
            materialization_diff_hash = hashlib.sha256(f"{audit_id}:diff".encode("utf-8")).hexdigest()
            final_report_hash = hashlib.sha256(f"{audit_id}:report".encode("utf-8")).hexdigest()
            chain_proof = {}
            for step_index, step in enumerate(chain_steps):
                artifact_path = artifact_root / audit_id / f"{step}.json"
                previous_step = chain_steps[step_index - 1] if step_index > 0 else None
                next_step = chain_steps[step_index + 1] if step_index + 1 < len(chain_steps) else None
                payload = {
                    "schema_version": "codex-deepresearch.manual-trace-step.v0",
                    "audit_id": audit_id,
                    "run_id": run_id,
                    "prompt_id": prompt_id,
                    "step": step,
                    "status": "verified",
                    "public_safe": True,
                    "fixture_only": fixture_only,
                    "previous_step": previous_step,
                    "next_step": next_step,
                    "alignment_subject": "sanitized semantic trace audit",
                    "provenance": {
                        "public_safe": True,
                        "fixture_only": fixture_only,
                        "release_eligible": live and not fixture_only,
                    },
                }
                if step in {"locked_oracle", "semantic_plan", "semantic_review"}:
                    payload["oracle_hash"] = oracle_hash
                if step in {
                    "semantic_plan",
                    "semantic_review",
                    "materialization_diff",
                    "research_tasks",
                    "search_tasks",
                    "visual_tasks",
                    "accepted_shards",
                    "final_report",
                }:
                    payload["semantic_plan_hash"] = semantic_plan_hash
                if step in {"materialization_diff", "final_report"}:
                    payload["materialization_diff_hash"] = materialization_diff_hash
                if step == "accepted_shards":
                    payload["accepted_shard_refs"] = [
                        {
                            "task_id": "task_search_001",
                            "shard_path": f"evidence_shards/{audit_id}/evidence_shard.json",
                            "artifact_hash": hashlib.sha256(
                                f"{audit_id}:accepted-shard".encode("utf-8")
                            ).hexdigest(),
                        }
                    ]
                if step == "final_report":
                    payload["final_report_hash"] = final_report_hash
                    payload["final_report_alignment"] = {
                        "original_question": True,
                        "locked_oracle": True,
                        "semantic_plan": True,
                        "semantic_review": True,
                        "materialization_diff": True,
                        "accepted_shards": True,
                        "final_answer": True,
                    }
                self.write_json(artifact_path, payload)
                chain_proof[step] = {
                    "status": "verified",
                    "artifact_path": str(artifact_path.relative_to(root)),
                    "artifact_hash": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                }
            audits.append(
                {
                    "id": audit_id,
                    "run_id": run_id,
                    "prompt_id": prompt_id,
                    "oracle_hash": oracle_hash,
                    "semantic_plan_hash": semantic_plan_hash,
                    "materialization_diff_hash": materialization_diff_hash,
                    "final_report_hash": final_report_hash,
                    "route": route,
                    "public_safe": True,
                    "release_eligible": live and not fixture_only,
                    "fixture_only": fixture_only,
                    "categories": categories,
                    "risk_rank": index,
                    "risk_rationale": "Selected as a high-risk semantic alignment trace.",
                    "chain_proof": chain_proof,
                }
            )
        manifest_path = root / "manual_trace_audits.json"
        self.write_json(
            manifest_path,
            {
                "schema_version": "codex-deepresearch.manual-trace-audits.v0",
                "public_safe": True,
                "live_release_audits_performed": live,
                "risk_selection": {
                    "hardest_highest_risk": True,
                    "selection_rationale": "Covers highest-risk Korean, visual, and policy traces.",
                },
                "audits": audits,
            },
        )
        return manifest_path

    def mutate_manual_trace_audit_step(
        self,
        manifest_path: Path,
        *,
        step: str,
        audit_index: int = 0,
        replacement: dict[str, Any] | None = None,
        mutator: Any = None,
    ) -> None:
        manifest = self.read_json(manifest_path)
        proof = manifest["audits"][audit_index]["chain_proof"][step]
        artifact_path = Path(proof["artifact_path"])
        if not artifact_path.is_absolute():
            artifact_path = manifest_path.parent / artifact_path
        payload = replacement if replacement is not None else self.read_json(artifact_path)
        if mutator is not None:
            mutator(payload)
        self.write_json(artifact_path, payload)
        proof["artifact_hash"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        self.write_json(manifest_path, manifest)

    def test_prompt_manifest_covers_public_safe_real_use_set(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompts = manifest["prompts"]
        visual_prompts = [
            prompt for prompt in prompts if prompt["route"] in {"visual_required", "visual_optional"}
        ]

        self.assertGreaterEqual(len(prompts), 20)
        self.assertGreaterEqual(len(visual_prompts), 8)
        self.assertTrue(all(prompt["public_safe"] is True for prompt in prompts))
        self.assertTrue(
            all("fresh_session_full_runner_artifact_handoff" in prompt["gate_tags"] for prompt in prompts if prompt["route"] == "text_only")
        )
        self.assertTrue(
            all("automatic_web_visual_e2e" in prompt["gate_tags"] for prompt in visual_prompts)
        )
        self.assertTrue(
            all("automated_cli_real_provider_visual_e2e" not in prompt["gate_tags"] for prompt in visual_prompts)
        )

    def test_semantic_regression_manifest_covers_required_quotas(self) -> None:
        manifest = load_semantic_regression_manifest(DEFAULT_SEMANTIC_REGRESSION_MANIFEST)
        counts = manifest["category_counts"]

        self.assertEqual(len(manifest["prompts"]), 30)
        self.assertGreaterEqual(counts["korean"], 10)
        self.assertGreaterEqual(counts["visual"], 8)
        self.assertGreaterEqual(counts["policy_regulatory_domain"], 8)
        self.assertGreaterEqual(counts["software_implementation"], 5)
        self.assertGreaterEqual(counts["ambiguous_non_software"], 5)
        self.assertTrue(all(prompt["release_counted"] is True for prompt in manifest["prompts"]))

    def test_public_beta_semantic_manifest_covers_required_quotas_and_ev_failure(self) -> None:
        manifest = load_public_beta_semantic_manifest(
            DEFAULT_PUBLIC_BETA_SEMANTIC_MANIFEST
        )
        counts = manifest["category_counts"]

        self.assertEqual(len(manifest["prompts"]), 20)
        self.assertGreaterEqual(counts["korean"], 8)
        self.assertGreaterEqual(counts["visual"], 8)
        self.assertGreaterEqual(counts["official_regulatory_source_quality"], 8)
        self.assertGreaterEqual(counts["ambiguous_domain"], 5)
        self.assertGreaterEqual(counts["non_software_domain"], 5)
        self.assertTrue(
            any(
                "historical_failure" in prompt["categories"]
                and "배터리 화재" in prompt["prompt"]
                for prompt in manifest["prompts"]
            )
        )
        self.assertTrue(all(prompt["oracle_hash_verified"] for prompt in manifest["prompts"]))

    def test_semantic_manifest_rejects_wrong_oracle_hash(self) -> None:
        temp = self.temp_dir()
        manifest_path = temp / "public_beta_semantic_prompts.json"
        oracle_path = temp / "semantic_oracles.json"
        manifest = self.read_json(DEFAULT_PUBLIC_BETA_SEMANTIC_MANIFEST)
        oracles = self.read_json(
            DEFAULT_PUBLIC_BETA_SEMANTIC_MANIFEST.parent / "semantic_oracles.json"
        )
        manifest["prompts"][0]["oracle_hash"] = "0" * 64
        self.write_json(manifest_path, manifest)
        self.write_json(oracle_path, oracles)

        with self.assertRaisesRegex(PublicBetaValidationError, "oracle_hash"):
            load_public_beta_semantic_manifest(manifest_path)

    def test_manual_trace_audit_gate_accepts_sanitized_live_audits(self) -> None:
        manifest_path = self.write_manual_trace_audit_manifest(self.temp_dir())

        gate = load_manual_trace_audits(manifest_path)

        self.assertTrue(gate["valid"], gate["failures"])
        self.assertEqual(gate["audit_count"], 5)
        self.assertGreaterEqual(gate["category_counts"]["korean"], 2)
        self.assertGreaterEqual(gate["category_counts"]["visual"], 2)
        self.assertEqual(
            len(gate["audits"][0]["chain_steps_verified"]),
            10,
        )

    def test_manual_trace_audit_gate_rejects_fixture_only_audits(self) -> None:
        manifest_path = self.write_manual_trace_audit_manifest(
            self.temp_dir(),
            live=False,
            fixture_only=True,
        )

        gate = load_manual_trace_audits(manifest_path)

        self.assertFalse(gate["valid"])
        self.assertIn("live_release_audits_not_performed", gate["failures"])
        self.assertTrue(
            any(failure.endswith(":fixture_only") for failure in gate["failures"]),
            gate["failures"],
        )

    def test_manual_trace_audit_gate_rejects_placeholder_artifacts(self) -> None:
        manifest_path = self.write_manual_trace_audit_manifest(self.temp_dir())
        self.mutate_manual_trace_audit_step(
            manifest_path,
            step="semantic_plan",
            replacement={"status": "verified"},
        )

        gate = load_manual_trace_audits(manifest_path)

        self.assertFalse(gate["valid"])
        self.assertTrue(
            any("semantic_plan_artifact_placeholder_or_wrong_schema" in failure for failure in gate["failures"]),
            gate["failures"],
        )

    def test_manual_trace_audit_gate_rejects_wrong_prompt_id(self) -> None:
        manifest_path = self.write_manual_trace_audit_manifest(self.temp_dir())
        self.mutate_manual_trace_audit_step(
            manifest_path,
            step="locked_oracle",
            mutator=lambda payload: payload.update({"prompt_id": "wrong-prompt"}),
        )

        gate = load_manual_trace_audits(manifest_path)

        self.assertFalse(gate["valid"])
        self.assertTrue(
            any("locked_oracle_prompt_id_mismatch" in failure for failure in gate["failures"]),
            gate["failures"],
        )

    def test_manual_trace_audit_gate_rejects_missing_accepted_shard_refs(self) -> None:
        manifest_path = self.write_manual_trace_audit_manifest(self.temp_dir())
        self.mutate_manual_trace_audit_step(
            manifest_path,
            step="accepted_shards",
            mutator=lambda payload: payload.pop("accepted_shard_refs", None),
        )

        gate = load_manual_trace_audits(manifest_path)

        self.assertFalse(gate["valid"])
        self.assertTrue(
            any("accepted_shards_refs_missing" in failure for failure in gate["failures"]),
            gate["failures"],
        )

    def test_manual_trace_audit_gate_rejects_missing_final_report_alignment(self) -> None:
        manifest_path = self.write_manual_trace_audit_manifest(self.temp_dir())
        self.mutate_manual_trace_audit_step(
            manifest_path,
            step="final_report",
            mutator=lambda payload: payload.pop("final_report_alignment", None),
        )

        gate = load_manual_trace_audits(manifest_path)

        self.assertFalse(gate["valid"])
        self.assertTrue(
            any("final_report_alignment_missing" in failure for failure in gate["failures"]),
            gate["failures"],
        )

    def test_manual_trace_audit_gate_rejects_fixture_only_step_artifacts(self) -> None:
        manifest_path = self.write_manual_trace_audit_manifest(self.temp_dir())

        def mark_fixture(payload: dict[str, Any]) -> None:
            payload["fixture_only"] = True
            payload.setdefault("provenance", {})["fixture_only"] = True

        self.mutate_manual_trace_audit_step(
            manifest_path,
            step="search_tasks",
            mutator=mark_fixture,
        )

        gate = load_manual_trace_audits(manifest_path)

        self.assertFalse(gate["valid"])
        self.assertTrue(
            any("search_tasks_fixture_only" in failure for failure in gate["failures"]),
            gate["failures"],
        )

    def test_semantic_manifest_execution_gate_requires_every_prompt_id(self) -> None:
        manifest = load_semantic_regression_manifest(DEFAULT_SEMANTIC_REGRESSION_MANIFEST)
        runs = [
            {
                "id": prompt["id"],
                "status": "passed",
                "terminal_status": "completed_parallel",
                "metric_classification": "success",
                "semantic_release_checks": {"valid": True},
            }
            for prompt in manifest["prompts"][:-1]
        ]

        gate = semantic_manifest_execution_gate(
            manifest,
            runs,
            suite_label="semantic_regression_30",
        )

        self.assertFalse(gate["valid"])
        self.assertEqual(gate["missing_prompt_ids"], [manifest["prompts"][-1]["id"]])
        self.assertIn(
            "missing_run_entries:" + manifest["prompts"][-1]["id"],
            gate["failures"],
        )

    def test_semantic_manifest_execution_gate_counts_fixture_as_failure(self) -> None:
        manifest = load_public_beta_semantic_manifest(
            DEFAULT_PUBLIC_BETA_SEMANTIC_MANIFEST
        )
        runs = []
        for prompt in manifest["prompts"]:
            runs.append(
                {
                    "id": prompt["id"],
                    "status": "passed",
                    "terminal_status": "completed_parallel",
                    "metric_classification": "success",
                    "semantic_release_checks": {"valid": True},
                }
            )
        runs[0].update(
            {
                "terminal_status": "completed_fixture",
                "metric_classification": "excluded_fixture",
            }
        )

        gate = semantic_manifest_execution_gate(
            manifest,
            runs,
            suite_label="public_beta_semantic_20",
        )

        self.assertFalse(gate["valid"])
        self.assertEqual(gate["failed_prompt_count"], 1)
        self.assertEqual(gate["runs"][0]["release_counted_status"], "denominator_failure")

    def test_semantic_release_validation_fails_missing_runs_and_fixture_holdout(self) -> None:
        with self.assertRaises(PublicBetaValidationError) as raised:
            run_semantic_release_validation(
                runs_dir=self.temp_dir(),
                suite_id="semantic-release-missing",
                clean=True,
                manual_audit_manifest=None,
            )

        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(
            Path(payload["artifacts"]["results"]).name,
            SEMANTIC_RELEASE_VALIDATION_RESULTS_FILENAME,
        )
        self.assertFalse(payload["semantic_suite_gates"]["semantic_regression_30"]["valid"])
        self.assertEqual(
            payload["semantic_suite_gates"]["semantic_regression_30"]["expected_prompt_count"],
            30,
        )
        self.assertIn(
            "selector_not_release_eligible",
            payload["manifests"]["blind_holdout"]["selector_failures"],
        )
        self.assertIn(
            "manual_trace_audit_manifest_not_supplied",
            payload["manual_trace_audit_gate"]["failures"],
        )

    def test_semantic_release_validation_cli_reports_not_ready_without_artifacts(self) -> None:
        runs_dir = self.temp_dir()

        completed = subprocess.run(
            [
                str(RUNNER),
                "semantic-release-validation",
                "--runs-dir",
                str(runs_dir),
                "--suite-id",
                "semantic-release-cli",
                "--clean",
                "--allow-blocked",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertFalse(payload["release_gate_ready"])
        self.assertFalse(payload["semantic_suite_gates"]["blind_holdout_12"]["valid"])

    def test_blind_holdout_fixture_flow_fails_release_readiness_honestly(self) -> None:
        known = [
            load_semantic_regression_manifest(DEFAULT_SEMANTIC_REGRESSION_MANIFEST),
            load_public_beta_semantic_manifest(DEFAULT_PUBLIC_BETA_SEMANTIC_MANIFEST),
        ]
        holdout = load_blind_holdout_manifest(
            DEFAULT_BLIND_HOLDOUT_MANIFEST,
            known_manifests=known,
        )

        self.assertFalse(holdout["release_gate_ready"])
        self.assertIn("selector_not_release_eligible", holdout["failures"])
        self.assertEqual(len(holdout["prompts"]), 12)
        self.assertGreaterEqual(holdout["category_counts"]["korean"], 4)
        self.assertGreaterEqual(holdout["category_counts"]["visual"], 4)
        self.assertFalse(
            any(failure.startswith("holdout_prompt_overlap") for failure in holdout["failures"])
        )

    def test_semantic_anti_overfit_default_scan_passes_current_tree(self) -> None:
        scan = run_semantic_anti_overfit_scan()

        self.assertEqual(scan["status"], "passed", scan["findings"])

    def test_semantic_anti_overfit_scan_rejects_malicious_planner_file(self) -> None:
        temp = self.temp_dir()
        bad = temp / "semantic_planner_bad.py"
        holdout = load_blind_holdout_manifest(DEFAULT_BLIND_HOLDOUT_MANIFEST)
        prompt = holdout["prompts"][0]["prompt"]
        hash_value = holdout["prompts"][0]["prompt_hash"]
        hash_symbol = "prompt" + "_hash"
        plan_symbol = "expected" + "_plan"
        branch_keyword = "i" + "f"
        bounded_key = "bounded" + "_tasks"
        bad.write_text(
            "\n".join(
                [
                    f"HOLDOUT = {prompt!r}",
                    f"{branch_keyword} {hash_symbol} == {hash_value!r}:",
                    f"    {plan_symbol} = {{{bounded_key!r}: []}}",
                ]
            ),
            encoding="utf-8",
        )

        scan = run_semantic_anti_overfit_scan(scan_paths=[bad])

        self.assertEqual(scan["status"], "failed")
        self.assertIn(
            "known_holdout_string",
            {finding["code"] for finding in scan["findings"]},
        )
        self.assertIn(
            "prompt" + "_hash" + "_routing",
            {finding["code"] for finding in scan["findings"]},
        )
        self.assertIn(
            "canned" + "_expected" + "_plan",
            {finding["code"] for finding in scan["findings"]},
        )

    def test_semantic_anti_overfit_scan_rejects_prompt_hash_alias_routing(self) -> None:
        temp = self.temp_dir()
        bad = temp / "semantic_planner_alias_bad.py"
        hash_symbol = "prompt" + "_hash"
        route_symbol = "route"
        bad.write_text(
            "\n".join(
                [
                    f"routing_key = {hash_symbol}",
                    "if routing_key == 'abc':",
                    f"    {route_symbol} = 'visual_required'",
                ]
            ),
            encoding="utf-8",
        )

        scan = run_semantic_anti_overfit_scan(scan_paths=[bad])

        self.assertEqual(scan["status"], "failed")
        self.assertIn(
            "prompt" + "_hash" + "_alias" + "_routing",
            {finding["code"] for finding in scan["findings"]},
        )

    def test_semantic_anti_overfit_scan_rejects_prompt_sha256_digest_routing(self) -> None:
        temp = self.temp_dir()
        bad = temp / "semantic_planner_digest_bad.py"
        sha_name = "sha" + "256"
        prompt_name = "prompt"
        plan_symbol = "semantic" + "_plan"
        bad.write_text(
            "\n".join(
                [
                    "import hashlib",
                    f"digest = hashlib.{sha_name}({prompt_name}.encode()).hexdigest()",
                    "if digest == 'abc':",
                    f"    {plan_symbol} = {'{}'}",
                ]
            ),
            encoding="utf-8",
        )

        scan = run_semantic_anti_overfit_scan(scan_paths=[bad])

        self.assertEqual(scan["status"], "failed")
        self.assertIn(
            "prompt" + "_hash" + "_alias" + "_routing",
            {finding["code"] for finding in scan["findings"]},
        )

    def test_semantic_release_report_lists_hashes_and_required_artifacts(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        runs_dir = self.temp_dir()
        suite_id = "semantic-release-report"
        prompt_runs = self.write_all_passing_prompt_runs(
            manifest,
            runs_dir=runs_dir / "prompt-runs",
            suite_id=suite_id,
        )
        manual_audit_manifest = self.write_manual_trace_audit_manifest(
            runs_dir / "manual-audits"
        )

        payload = run_public_beta_validation(
            runs_dir=self.temp_dir(),
            suite_id=suite_id,
            clean=True,
            prompt_runs=prompt_runs,
            manual_audit_manifest=manual_audit_manifest,
        )

        report = payload["semantic_release_report"]
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["release_counted_run_count"], 20)
        first = report["runs"][0]
        for artifact in (
            "semantic_expectation_oracle",
            "semantic_plan",
            "semantic_plan_review",
            "semantic_materialization_diff",
            "research_tasks",
            "search_tasks",
            "visual_tasks",
            "subagent_assignments",
            "report",
        ):
            self.assertIn(artifact, first["artifact_paths"])
        self.assertRegex(
            first["artifact_hashes"]["semantic_materialization_diff"],
            r"^[0-9a-f]{64}$",
        )

    def test_semantic_release_report_requires_manual_trace_audits_by_default(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        runs_dir = self.temp_dir()
        suite_id = "semantic-release-report-manual-audit-default-required"
        prompt_runs = self.write_all_passing_prompt_runs(
            manifest,
            runs_dir=runs_dir / "prompt-runs",
            suite_id=suite_id,
        )

        with self.assertRaises(PublicBetaValidationError) as raised:
            run_public_beta_validation(
                runs_dir=self.temp_dir(),
                suite_id=suite_id,
                clean=True,
                prompt_runs=prompt_runs,
                manual_audit_manifest=None,
            )

        payload = self.read_json(raised.exception.results_path)
        manual_gate = payload["semantic_release_report"]["manual_trace_audit_gate"]
        self.assertFalse(manual_gate["valid"])
        self.assertIn("manual_trace_audit_manifest_not_supplied", manual_gate["failures"])
        self.assertFalse(payload["release_gate_ready"])

    def test_semantic_release_checks_require_materialization_diff_artifact(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = manifest["prompts"][0]
        run_dir = self.write_text_run(
            self.temp_dir() / "missing-diff",
            prompt=prompt,
            suite_id="missing-diff",
            status="completed_parallel",
        )
        (run_dir / "semantic_materialization_diff.json").unlink()

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="missing-diff",
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("semantic_materialization_diff", result["missing_artifacts"])
        failures = result["semantic_release_checks"]["failures"]
        self.assertTrue(
            any(failure.get("check") == "required_semantic_artifact_present" for failure in failures),
            failures,
        )

    def test_semantic_release_checks_bind_locked_oracle_to_manifest_hash(self) -> None:
        manifest = load_public_beta_semantic_manifest(DEFAULT_PUBLIC_BETA_SEMANTIC_MANIFEST)
        prompt = next(prompt for prompt in manifest["prompts"] if prompt["route"] == "text_only")
        run_dir = self.write_text_run(
            self.temp_dir() / "oracle-binding",
            prompt=prompt,
            suite_id="semantic-oracle-binding",
            status="completed_parallel",
        )
        mismatched_prompt = dict(prompt)
        mismatched_prompt["oracle_hash"] = "f" * 64

        result = evaluate_public_beta_prompt_run(
            mismatched_prompt,
            run_dir,
            suite_id="semantic-oracle-binding",
        )

        self.assertEqual(result["status"], "failed", result)
        failures = result["semantic_release_checks"]["failures"]
        self.assertTrue(
            any(failure.get("check") == "manifest_oracle_binding" for failure in failures),
            failures,
        )

    def test_default_suite_records_blocked_runs_separately_from_failures(self) -> None:
        with self.assertRaises(PublicBetaValidationError) as raised:
            run_public_beta_validation(
                runs_dir=self.temp_dir(),
                suite_id="public-beta",
                clean=True,
            )

        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["release_gate_ready"])
        self.assertEqual(payload["prompt_coverage"]["total_prompts"], 20)
        self.assertEqual(payload["prompt_coverage"]["visual_prompts"], 10)
        self.assertEqual(
            payload["outcome_counts"],
            {"blocked": 20, "excluded": 0, "failed": 0, "passed": 0},
        )
        self.assertEqual(payload["classification_counts"]["excluded_blocked"], 20)
        self.assertFalse(payload["release_gate_ready"])
        self.assertFalse(payload["issue_75_completion_ready"])
        self.assertEqual(payload["completion_mode"], "codex-native")
        self.assertEqual(payload["validation_mode"], "diagnostic_harness")
        self.assertFalse(
            payload["acceptance"]["all_prompt_runs_are_supplied_sanitized_real_runs"]
        )
        harness_checks = {
            key: value
            for key, value in payload["acceptance"].items()
            if key != "all_prompt_runs_are_supplied_sanitized_real_runs"
        }
        self.assertTrue(all(harness_checks.values()), payload["acceptance"])
        self.assertTrue(Path(payload["artifacts"]["summary"]).is_file())
        self.assertTrue(
            all(run["failure_category"] for run in payload["runs"] if run["status"] != "passed")
        )

        visual = {run["id"]: run for run in payload["runs"]}["pb-visual-001"]
        self.assertEqual(visual["terminal_status"], "blocked_missing_visual_provider")
        self.assertIn("visual_provider_status", visual["status_artifacts"])
        self.assertTrue(Path(visual["status_artifacts"]["visual_provider_status"]).is_file())

    def test_supplied_runs_count_pass_fail_and_blocked_metric_buckets(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompts = {prompt["id"]: prompt for prompt in manifest["prompts"]}
        runs_dir = self.temp_dir()
        suite_id = "public-beta-supplied"
        passing_run = self.write_text_run(
            runs_dir / "passing",
            prompt=prompts["pb-text-001"],
            suite_id=suite_id,
            status="completed_parallel",
        )
        failed_run = self.write_text_run(
            runs_dir / "failed",
            prompt=prompts["pb-text-002"],
            suite_id=suite_id,
            status="failed_synthesis",
            ok=False,
        )

        with self.assertRaises(PublicBetaValidationError) as raised:
            run_public_beta_validation(
                runs_dir=self.temp_dir(),
                suite_id=suite_id,
                clean=True,
                prompt_runs={
                    "pb-text-001": passing_run,
                    "pb-text-002": failed_run,
                },
            )

        payload = self.read_json(raised.exception.results_path)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["outcome_counts"]["passed"], 1)
        self.assertEqual(payload["outcome_counts"]["failed"], 1)
        self.assertEqual(payload["outcome_counts"]["blocked"], 18)
        self.assertEqual(
            payload["failure_category_counts"]["synthesis_shape_failure"],
            1,
        )
        metric = payload["prompt_metrics"]["fresh_session_full_runner_artifact_handoff"]
        self.assertEqual(metric["passed"], 1)
        self.assertEqual(metric["failed_non_blocked"], 1)
        self.assertEqual(metric["blocked"], 8)
        self.assertEqual(metric["denominator_completed_non_blocked"], 2)
        self.assertEqual(metric["pass_rate"], 0.5)

    def test_manual_planner_fallback_counts_as_release_failure_not_pass(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompts = {prompt["id"]: prompt for prompt in manifest["prompts"]}
        runs_dir = self.temp_dir()
        suite_id = "public-beta-manual-planner-fallback"
        manual_run = self.write_text_run(
            runs_dir / "manual-fallback",
            prompt=prompts["pb-text-001"],
            suite_id=suite_id,
            status="completed_manual_planner_fallback",
            ok=True,
        )

        with self.assertRaises(PublicBetaValidationError) as raised:
            run_public_beta_validation(
                runs_dir=self.temp_dir(),
                suite_id=suite_id,
                clean=True,
                prompt_runs={"pb-text-001": manual_run},
            )

        payload = self.read_json(raised.exception.results_path)
        run = {item["id"]: item for item in payload["runs"]}["pb-text-001"]
        self.assertEqual(run["metric_classification"], "included_failure")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_category"], "artifact_handoff_failure")
        self.assertIn("cannot satisfy semantic planner", run["failure_detail"])
        metric = payload["prompt_metrics"]["fresh_session_full_runner_artifact_handoff"]
        self.assertEqual(metric["passed"], 0)
        self.assertEqual(metric["failed_non_blocked"], 1)
        self.assertEqual(metric["blocked"], 9)
        self.assertEqual(metric["denominator_completed_non_blocked"], 1)
        self.assertEqual(metric["pass_rate"], 0.0)

    def test_blocked_semantic_planner_counts_as_release_failure_not_excluded(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompts = {prompt["id"]: prompt for prompt in manifest["prompts"]}
        runs_dir = self.temp_dir()
        suite_id = "public-beta-blocked-semantic-planner"
        blocked_run = self.write_text_run(
            runs_dir / "blocked-semantic-planner",
            prompt=prompts["pb-text-001"],
            suite_id=suite_id,
            status="blocked_semantic_planner_unavailable",
            ok=False,
        )

        with self.assertRaises(PublicBetaValidationError) as raised:
            run_public_beta_validation(
                runs_dir=self.temp_dir(),
                suite_id=suite_id,
                clean=True,
                prompt_runs={"pb-text-001": blocked_run},
            )

        payload = self.read_json(raised.exception.results_path)
        run = {item["id"]: item for item in payload["runs"]}["pb-text-001"]
        self.assertEqual(run["metric_classification"], "included_failure")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_category"], "artifact_handoff_failure")
        self.assertIn("semantic planner denominator failure", run["failure_detail"])
        metric = payload["prompt_metrics"]["fresh_session_full_runner_artifact_handoff"]
        self.assertEqual(metric["failed_non_blocked"], 1)
        self.assertEqual(metric["blocked"], 9)
        self.assertEqual(metric["denominator_completed_non_blocked"], 1)

    def test_fixture_completion_counts_as_semantic_release_failure_not_excluded(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompts = {prompt["id"]: prompt for prompt in manifest["prompts"]}
        runs_dir = self.temp_dir()
        suite_id = "public-beta-fixture-semantic-failure"
        fixture_run = self.write_text_run(
            runs_dir / "fixture-completion",
            prompt=prompts["pb-text-001"],
            suite_id=suite_id,
            status="completed_fixture",
            ok=True,
        )

        with self.assertRaises(PublicBetaValidationError) as raised:
            run_public_beta_validation(
                runs_dir=self.temp_dir(),
                suite_id=suite_id,
                clean=True,
                prompt_runs={"pb-text-001": fixture_run},
            )

        payload = self.read_json(raised.exception.results_path)
        run = {item["id"]: item for item in payload["runs"]}["pb-text-001"]
        self.assertEqual(run["metric_classification"], "included_failure")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_category"], "artifact_handoff_failure")
        self.assertIn("fixture-only completion", run["failure_detail"])
        metric = payload["prompt_metrics"]["fresh_session_full_runner_artifact_handoff"]
        self.assertEqual(metric["failed_non_blocked"], 1)
        self.assertEqual(metric["blocked"], 9)
        self.assertEqual(metric["denominator_completed_non_blocked"], 1)

    def test_release_ineligible_completed_parallel_text_run_fails_public_beta(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompts = {prompt["id"]: prompt for prompt in manifest["prompts"]}
        runs_dir = self.temp_dir()
        suite_id = "public-beta-semantic-release-ineligible"
        fallback_run = self.write_text_run(
            runs_dir / "heuristic-fallback",
            prompt=prompts["pb-text-001"],
            suite_id=suite_id,
            status="completed_parallel",
            ok=True,
            semantic_planning="heuristic_fallback",
        )

        with self.assertRaises(PublicBetaValidationError) as raised:
            run_public_beta_validation(
                runs_dir=self.temp_dir(),
                suite_id=suite_id,
                clean=True,
                prompt_runs={"pb-text-001": fallback_run},
            )

        payload = self.read_json(raised.exception.results_path)
        run = {item["id"]: item for item in payload["runs"]}["pb-text-001"]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["terminal_status"], "completed_parallel")
        self.assertEqual(run["metric_classification"], "included_failure")
        self.assertEqual(run["failure_category"], "artifact_handoff_failure")
        self.assertIn("semantic planner release gate failed", run["failure_detail"])
        checks = run["semantic_release_checks"]
        self.assertFalse(checks["valid"])
        failure_checks = {failure["check"] for failure in checks["failures"]}
        self.assertIn("semantic_planner_mode", failure_checks)
        self.assertIn("semantic_release_eligible", failure_checks)
        self.assertIn("semantic_planner_validation_ok", failure_checks)
        metric = payload["prompt_metrics"]["fresh_session_full_runner_artifact_handoff"]
        self.assertEqual(metric["passed"], 0)
        self.assertEqual(metric["failed_non_blocked"], 1)
        self.assertEqual(metric["denominator_completed_non_blocked"], 1)

    def test_codex_semantic_text_run_with_required_artifacts_can_pass(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        run_dir = self.write_text_run(
            self.temp_dir() / "codex-semantic",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_serial_handoff",
            ok=True,
            semantic_planning="eligible",
        )

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "passed", result)
        checks = result["semantic_release_checks"]
        self.assertTrue(checks["valid"], checks)
        self.assertEqual(checks["planner_modes"]["run_status"], "codex_semantic")
        self.assertTrue(checks["semantic_release_eligible"]["run_status"])
        self.assertTrue(checks["validation_ok"])
        for artifact_name in checks["required_artifacts"]:
            self.assertIn(artifact_name, result["status_artifacts"])

    def test_codex_semantic_text_run_requires_semantic_trace_index_proof(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        run_dir = self.write_text_run(
            self.temp_dir() / "codex-semantic-bad-index",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_serial_handoff",
            ok=True,
            semantic_planning="eligible",
        )
        trace_path = run_dir / "run_trace.jsonl"
        records = self.read_jsonl(trace_path)
        for record in records:
            if record.get("event_type") == "semantic_planner_request_created":
                record["semantic_event_index"] = 2
                record["order_validation"]["semantic_event_index"] = 2
                break
        self.write_jsonl(trace_path, records)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed", result)
        failures = result["semantic_release_checks"]["failures"]
        self.assertTrue(
            any(
                failure.get("check") == "semantic_trace_ordering"
                and failure.get("event") == "semantic_planner_request_created"
                and failure.get("expected_semantic_event_index") == 3
                for failure in failures
            ),
            failures,
        )

    def test_codex_semantic_text_run_requires_strict_semantic_trace_timestamps(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        run_dir = self.write_text_run(
            self.temp_dir() / "codex-semantic-bad-timestamps",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_serial_handoff",
            ok=True,
            semantic_planning="eligible",
        )
        trace_path = run_dir / "run_trace.jsonl"
        records = self.read_jsonl(trace_path)
        shared_timestamp = records[0]["timestamp"]
        for record in records:
            if str(record.get("event_type", "")).startswith("semantic_"):
                record["timestamp"] = shared_timestamp
        self.write_jsonl(trace_path, records)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed", result)
        failures = result["semantic_release_checks"]["failures"]
        self.assertTrue(
            any(
                failure.get("check") == "semantic_trace_ordering"
                and "timestamps are not strictly increasing" in failure.get("detail", "")
                for failure in failures
            ),
            failures,
        )

    def test_codex_semantic_text_run_requires_complete_review_evidence(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        cases = (
            ("missing-score", "pop", "semantic_fit_score", "semantic_fit_score"),
            ("string-score", "set", "semantic_fit_score", "semantic_fit_score"),
            ("low-score", "low", "semantic_fit_score", "semantic_fit_score"),
            ("nan-score", "nan", "semantic_fit_score", "semantic_fit_score"),
            ("infinite-score", "inf", "semantic_fit_score", "semantic_fit_score"),
            ("negative-infinite-score", "-inf", "semantic_fit_score", "semantic_fit_score"),
            ("missing-blockers", "pop", "blockers", "semantic_review_blockers"),
            ("invalid-blockers", "set", "blockers", "semantic_review_blockers"),
            ("nonempty-blockers", "nonempty", "blockers", "semantic_review_blockers"),
            (
                "missing-reviewer-independence",
                "pop",
                "reviewer_independence",
                "reviewer_independence",
            ),
            (
                "failed-reviewer-independence",
                "failed",
                "reviewer_independence",
                "reviewer_independence",
            ),
            (
                "missing-substitute-check",
                "pop",
                "substitute_implementation_check",
                "substitute_implementation_check",
            ),
            (
                "failed-substitute-check",
                "failed",
                "substitute_implementation_check",
                "substitute_implementation_check",
            ),
        )
        for case_name, action, field, expected_check in cases:
            with self.subTest(case=case_name):
                run_dir = self.write_text_run(
                    self.temp_dir() / case_name,
                    prompt=prompt,
                    suite_id="public-beta-validation",
                    status="completed_serial_handoff",
                    ok=True,
                    semantic_planning="eligible",
                )
                review = self.read_json(run_dir / "semantic_plan_review.json")
                if action == "pop":
                    review.pop(field, None)
                elif action == "set" and field == "semantic_fit_score":
                    review[field] = "9.4"
                elif action == "low":
                    review[field] = 8.99
                elif action == "nan":
                    review[field] = float("nan")
                elif action == "inf":
                    review[field] = float("inf")
                elif action == "-inf":
                    review[field] = float("-inf")
                elif action == "set" and field == "blockers":
                    review[field] = {"count": 0}
                elif action == "nonempty":
                    review[field] = [{"code": "semantic_gap"}]
                elif action == "failed" and field == "reviewer_independence":
                    review[field] = {"independent": False, "status": "shared"}
                elif action == "failed" and field == "substitute_implementation_check":
                    review[field] = {"passed": False, "checked": True}
                else:
                    raise AssertionError(f"unhandled review mutation: {case_name}")
                self.write_json(run_dir / "semantic_plan_review.json", review)

                result = evaluate_public_beta_prompt_run(
                    prompt,
                    run_dir,
                    suite_id="public-beta-validation",
                )

                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(result["metric_classification"], "included_failure")
                failure_checks = {
                    failure["check"]
                    for failure in result["semantic_release_checks"]["failures"]
                }
                self.assertIn(expected_check, failure_checks)

    def test_codex_semantic_text_run_requires_codex_semantic_source_provenance(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        cases = (
            ("missing-evidence-source", "evidence", None, "evidence.semantic_planner"),
            ("raw-plan-source", "semantic_plan", "raw_fixture", "semantic_plan.semantic_plan"),
        )
        for case_name, artifact, source_value, expected_source in cases:
            with self.subTest(case=case_name):
                run_dir = self.write_text_run(
                    self.temp_dir() / case_name,
                    prompt=prompt,
                    suite_id="public-beta-validation",
                    status="completed_parallel",
                    ok=True,
                    semantic_planning="eligible",
                )
                if artifact == "evidence":
                    evidence = self.read_json(run_dir / "evidence.json")
                    evidence["semantic_planner"].pop("source", None)
                    self.write_json(run_dir / "evidence.json", evidence)
                elif artifact == "semantic_plan":
                    semantic_plan = self.read_json(run_dir / "semantic_plan.json")
                    semantic_plan["semantic_plan"]["source"] = source_value
                    self.write_json(run_dir / "semantic_plan.json", semantic_plan)
                else:
                    raise AssertionError(f"unhandled provenance mutation: {case_name}")

                result = evaluate_public_beta_prompt_run(
                    prompt,
                    run_dir,
                    suite_id="public-beta-validation",
                )

                self.assertEqual(result["status"], "failed", result)
                failures = result["semantic_release_checks"]["failures"]
                source_failures = [
                    failure
                    for failure in failures
                    if failure["check"] == "semantic_codex_source"
                ]
                self.assertTrue(source_failures, failures)
                self.assertTrue(
                    any(
                        failure.get("source") == expected_source
                        or expected_source in failure.get("missing_sources", [])
                        for failure in source_failures
                    ),
                    source_failures,
                )

    def test_codex_semantic_text_run_requires_oracle_requirement_map_integrity(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        cases = (
            ("missing-oracle-requirement-map", "pop"),
            ("empty-oracle-requirement-map", "empty"),
        )
        for case_name, action in cases:
            with self.subTest(case=case_name):
                run_dir = self.write_text_run(
                    self.temp_dir() / case_name,
                    prompt=prompt,
                    suite_id="public-beta-validation",
                    status="completed_serial_handoff",
                    ok=True,
                    semantic_planning="eligible",
                )
                oracle = self.read_json(run_dir / "semantic_expectation_oracle.json")
                if action == "pop":
                    oracle.pop("oracle_requirement_map", None)
                elif action == "empty":
                    oracle["oracle_requirement_map"] = []
                else:
                    raise AssertionError(f"unhandled oracle mutation: {case_name}")
                self.write_json(run_dir / "semantic_expectation_oracle.json", oracle)

                result = evaluate_public_beta_prompt_run(
                    prompt,
                    run_dir,
                    suite_id="public-beta-validation",
                )

                self.assert_semantic_artifact_integrity_failure(
                    result,
                    artifact="semantic_expectation_oracle",
                    field="oracle_requirement_map",
                )

    def test_codex_semantic_text_run_requires_review_integrity_fields(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        fields = (
            "question_scope",
            "raw_request_path",
            "raw_response_path",
            "raw_request_hash",
            "raw_response_hash",
            "provenance",
            "template_use",
            "session_id_unavailable_reason",
        )
        for field in fields:
            with self.subTest(field=field):
                run_dir = self.write_text_run(
                    self.temp_dir() / f"missing-review-{field}",
                    prompt=prompt,
                    suite_id="public-beta-validation",
                    status="completed_serial_handoff",
                    ok=True,
                    semantic_planning="eligible",
                )
                review = self.read_json(run_dir / "semantic_plan_review.json")
                review.pop(field, None)
                self.write_json(run_dir / "semantic_plan_review.json", review)

                result = evaluate_public_beta_prompt_run(
                    prompt,
                    run_dir,
                    suite_id="public-beta-validation",
                )

                self.assert_semantic_artifact_integrity_failure(
                    result,
                    artifact="semantic_plan_review",
                    field=field,
                )

    def test_codex_semantic_text_run_requires_plan_integrity_fields(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        cases = (
            ("missing-semantic-plan", "pop", "semantic_plan", "semantic_plan"),
            ("missing-plan-angles", "pop", "angles", "angles"),
            ("empty-plan-angles", "empty", "angles", "angles"),
            (
                "missing-nested-plan-angles",
                "nested-pop",
                "angles",
                "semantic_plan.angles",
            ),
            (
                "empty-nested-plan-angles",
                "nested-empty",
                "angles",
                "semantic_plan.angles",
            ),
            (
                "missing-requirement-coverage-map",
                "pop",
                "requirement_coverage_map",
                "requirement_coverage_map",
            ),
            (
                "empty-requirement-coverage-map",
                "empty",
                "requirement_coverage_map",
                "requirement_coverage_map",
            ),
        )
        for case_name, action, field, expected_field in cases:
            with self.subTest(case=case_name):
                run_dir = self.write_text_run(
                    self.temp_dir() / case_name,
                    prompt=prompt,
                    suite_id="public-beta-validation",
                    status="completed_serial_handoff",
                    ok=True,
                    semantic_planning="eligible",
                )
                plan = self.read_json(run_dir / "semantic_plan.json")
                if action == "pop":
                    plan.pop(field, None)
                elif action == "empty":
                    plan[field] = []
                elif action == "nested-pop":
                    plan["semantic_plan"].pop(field, None)
                elif action == "nested-empty":
                    plan["semantic_plan"][field] = []
                else:
                    raise AssertionError(f"unhandled plan mutation: {case_name}")
                self.write_json(run_dir / "semantic_plan.json", plan)

                result = evaluate_public_beta_prompt_run(
                    prompt,
                    run_dir,
                    suite_id="public-beta-validation",
                )

                self.assert_semantic_artifact_integrity_failure(
                    result,
                    artifact="semantic_plan",
                    field=expected_field,
                )

    def test_codex_semantic_text_run_rejects_generic_one_angle_plan(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        run_dir = self.write_text_run(
            self.temp_dir() / "generic-one-angle",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_serial_handoff",
            ok=True,
            semantic_planning="eligible",
        )
        generic_angle = {
            "angle_id": "angle_001",
            "title": "Primary source discovery",
            "research_question": (
                "Find authoritative sources that directly answer the research question"
            ),
            "question_context": "Generic source discovery context.",
            "route": "text_only",
            "evidence_need": "primary_source",
            "expected_artifacts": ["source list"],
            "success_criteria": ["Find one source."],
            "report_section": "Primary Sources",
        }
        plan = self.read_json(run_dir / "semantic_plan.json")
        plan["angles"] = [generic_angle]
        plan["semantic_plan"]["angles"] = [dict(generic_angle)]
        plan["requirement_coverage_map"] = [
            {
                "requirement_id": "req_001",
                "angle_id": "angle_001",
                "coverage_status": "covered",
            }
        ]
        self.write_json(run_dir / "semantic_plan.json", plan)
        oracle = self.read_json(run_dir / "semantic_expectation_oracle.json")
        oracle["oracle_requirement_map"] = [
            {
                "requirement_id": "req_001",
                "description": "Answer the prompt from a primary source.",
                "covered_by_angle_ids": ["angle_001"],
            }
        ]
        self.write_json(run_dir / "semantic_expectation_oracle.json", oracle)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assert_semantic_artifact_integrity_failure(
            result,
            artifact="semantic_plan",
            field="angles",
        )

    def test_codex_semantic_text_run_rejects_generic_angle_text_with_valid_count_and_coverage(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        cases = (
            ("generic-title", "title", "Primary source discovery"),
            (
                "generic-research-question",
                "research_question",
                "Find authoritative sources that directly answer the research question",
            ),
        )
        evidence_needs = ("primary_source", "comparative_analysis")
        for case_name, generic_field, generic_value in cases:
            with self.subTest(case=case_name):
                run_dir = self.write_text_run(
                    self.temp_dir() / case_name,
                    prompt=prompt,
                    suite_id="public-beta-validation",
                    status="completed_serial_handoff",
                    ok=True,
                    semantic_planning="eligible",
                )
                angles = []
                for index, evidence_need in enumerate(evidence_needs, start=1):
                    scope_label = evidence_need.replace("_", " ")
                    angle = {
                        "angle_id": f"angle_{index:03d}",
                        "title": (
                            f"deterministic release validation {scope_label} scope"
                        ),
                        "research_question": (
                            "Which deterministic software release validation "
                            f"tradeoffs require {scope_label} review?"
                        ),
                        "question_context": (
                            "Scope deterministic release validation evidence for "
                            f"public beta prompt {prompt['id']} and {scope_label}."
                        ),
                        "route": "text_only",
                        "evidence_need": evidence_need,
                        "expected_artifacts": ["source list", "supporting quotes"],
                        "success_criteria": ["Claims remain tied to source spans."],
                        "report_section": f"Validation Angle {index}",
                    }
                    angle[generic_field] = generic_value
                    angles.append(angle)
                plan = self.read_json(run_dir / "semantic_plan.json")
                plan["angles"] = angles
                plan["semantic_plan"]["angles"] = [dict(angle) for angle in angles]
                plan["requirement_coverage_map"] = [
                    {
                        "requirement_id": f"req_{index:03d}",
                        "angle_id": angle["angle_id"],
                        "coverage_status": "covered",
                    }
                    for index, angle in enumerate(angles, start=1)
                ]
                self.write_json(run_dir / "semantic_plan.json", plan)
                oracle = self.read_json(run_dir / "semantic_expectation_oracle.json")
                oracle["oracle_requirement_map"] = [
                    {
                        "requirement_id": f"req_{index:03d}",
                        "description": f"Resolve angle {index} for the prompt.",
                        "covered_by_angle_ids": [angle["angle_id"]],
                    }
                    for index, angle in enumerate(angles, start=1)
                ]
                self.write_json(run_dir / "semantic_expectation_oracle.json", oracle)

                result = evaluate_public_beta_prompt_run(
                    prompt,
                    run_dir,
                    suite_id="public-beta-validation",
                )

                self.assert_semantic_artifact_integrity_failure(
                    result,
                    artifact="semantic_plan",
                    field="angles",
                )

    def test_codex_semantic_text_run_rejects_low_specificity_software_evidence_angles(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        run_dir = self.write_text_run(
            self.temp_dir() / "low-specificity-software-evidence",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_serial_handoff",
            ok=True,
            semantic_planning="eligible",
        )
        evidence_needs = ("primary_source", "comparative_analysis")
        angles = [
            {
                "angle_id": f"angle_{index:03d}",
                "title": f"software evidence angle {index}",
                "research_question": f"How does software evidence support angle {index}?",
                "question_context": (
                    "Low-specificity software evidence scope for public beta "
                    f"prompt {prompt['id']}."
                ),
                "route": "text_only",
                "evidence_need": evidence_need,
                "expected_artifacts": ["source list", "supporting quotes"],
                "success_criteria": ["Claims remain tied to source spans."],
                "report_section": f"Software Evidence {index}",
            }
            for index, evidence_need in enumerate(evidence_needs, start=1)
        ]
        plan = self.read_json(run_dir / "semantic_plan.json")
        plan["angles"] = angles
        plan["semantic_plan"]["angles"] = [dict(angle) for angle in angles]
        plan["requirement_coverage_map"] = [
            {
                "requirement_id": f"req_{index:03d}",
                "angle_id": angle["angle_id"],
                "coverage_status": "covered",
            }
            for index, angle in enumerate(angles, start=1)
        ]
        self.write_json(run_dir / "semantic_plan.json", plan)
        oracle = self.read_json(run_dir / "semantic_expectation_oracle.json")
        oracle["oracle_requirement_map"] = [
            {
                "requirement_id": f"req_{index:03d}",
                "description": f"Resolve software evidence angle {index}.",
                "covered_by_angle_ids": [angle["angle_id"]],
            }
            for index, angle in enumerate(angles, start=1)
        ]
        self.write_json(run_dir / "semantic_expectation_oracle.json", oracle)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assert_semantic_artifact_integrity_failure(
            result,
            artifact="semantic_plan",
            field="angles",
        )

    def test_codex_semantic_text_run_rejects_duplicate_token_rich_angles(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        run_dir = self.write_text_run(
            self.temp_dir() / "duplicate-token-rich-angles",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_serial_handoff",
            ok=True,
            semantic_planning="eligible",
        )
        evidence_needs = ("primary_source", "comparative_analysis")
        angles = [
            {
                "angle_id": f"angle_{index:03d}",
                "title": "deterministic software release validation tradeoffs",
                "research_question": (
                    "Which deterministic software release validation criteria "
                    "shape evidence tradeoffs?"
                ),
                "question_context": (
                    "Evaluate deterministic software release validation evidence "
                    f"for public beta prompt {prompt['id']}."
                ),
                "route": "text_only",
                "evidence_need": evidence_need,
                "expected_artifacts": ["source list", "supporting quotes"],
                "success_criteria": ["Claims remain tied to source spans."],
                "report_section": f"Release Validation {index}",
            }
            for index, evidence_need in enumerate(evidence_needs, start=1)
        ]
        plan = self.read_json(run_dir / "semantic_plan.json")
        plan["angles"] = angles
        plan["semantic_plan"]["angles"] = [dict(angle) for angle in angles]
        plan["requirement_coverage_map"] = [
            {
                "requirement_id": f"req_{index:03d}",
                "angle_id": angle["angle_id"],
                "coverage_status": "covered",
            }
            for index, angle in enumerate(angles, start=1)
        ]
        self.write_json(run_dir / "semantic_plan.json", plan)
        oracle = self.read_json(run_dir / "semantic_expectation_oracle.json")
        oracle["oracle_requirement_map"] = [
            {
                "requirement_id": f"req_{index:03d}",
                "description": f"Resolve release validation angle {index}.",
                "covered_by_angle_ids": [angle["angle_id"]],
            }
            for index, angle in enumerate(angles, start=1)
        ]
        self.write_json(run_dir / "semantic_expectation_oracle.json", oracle)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assert_semantic_artifact_integrity_failure(
            result,
            artifact="semantic_plan",
            field="angles",
        )

    def test_codex_semantic_text_run_rejects_template_use_fallback_claims(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        run_dir = self.write_text_run(
            self.temp_dir() / "template-use-fallback",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_serial_handoff",
            ok=True,
            semantic_planning="eligible",
        )
        artifact_files = (
            "semantic_expectation_oracle.json",
            "semantic_plan.json",
            "semantic_plan_review.json",
            "semantic_planner_validation.json",
        )
        for filename in artifact_files:
            path = run_dir / filename
            payload = self.read_json(path)
            template_use = dict(payload["template_use"])
            template_use["uses_preselected_template"] = True
            template_use["template_source"] = "heuristic_template_planner"
            template_use["template_angle_titles"] = ["Primary source discovery"]
            payload["template_use"] = template_use
            self.write_json(path, payload)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        failures = result["semantic_release_checks"]["failures"]
        template_failures = {
            (failure.get("artifact"), failure.get("field"))
            for failure in failures
            if failure.get("check") == "semantic_artifact_integrity"
        }
        self.assertTrue(
            {
                ("semantic_expectation_oracle", "template_use"),
                ("semantic_plan", "template_use"),
                ("semantic_plan_review", "template_use"),
                ("semantic_planner_validation", "template_use"),
            }.issubset(template_failures),
            failures,
        )

    def test_codex_semantic_text_run_rejects_heuristic_planner_provenance(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        run_dir = self.write_text_run(
            self.temp_dir() / "heuristic-planner-provenance",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_serial_handoff",
            ok=True,
            semantic_planning="eligible",
        )
        artifact_files = (
            "semantic_expectation_oracle.json",
            "semantic_plan.json",
            "semantic_plan_review.json",
            "semantic_planner_validation.json",
        )
        for filename in artifact_files:
            path = run_dir / filename
            payload = self.read_json(path)
            provenance = dict(payload["provenance"])
            provenance["planner_source"] = "heuristic_template_planner"
            payload["provenance"] = provenance
            self.write_json(path, payload)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        failures = result["semantic_release_checks"]["failures"]
        provenance_failures = {
            (failure.get("artifact"), failure.get("field"))
            for failure in failures
            if failure.get("check") == "semantic_artifact_integrity"
        }
        self.assertTrue(
            {
                ("semantic_expectation_oracle", "provenance"),
                ("semantic_plan", "provenance"),
                ("semantic_plan_review", "provenance"),
                ("semantic_planner_validation", "provenance"),
            }.issubset(provenance_failures),
            failures,
        )

    def test_codex_semantic_text_run_rejects_fabricated_raw_artifacts_and_identity(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        run_dir = self.write_text_run(
            self.temp_dir() / "fabricated-semantic-release",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_serial_handoff",
            ok=True,
            semantic_planning="eligible",
        )
        fake_hash = "0" * 64

        def strip_release_identity(provenance: dict[str, Any]) -> dict[str, Any]:
            for field in (
                "child_session_id",
                "session_id",
                "raw_response_id",
                "codex_event_id",
                "response_id",
            ):
                provenance.pop(field, None)
            provenance["session_id_unavailable_reason"] = "fabricated unavailable reason"
            return provenance

        for filename in (
            "semantic_expectation_oracle.json",
            "semantic_plan.json",
            "semantic_plan_review.json",
            "semantic_planner_validation.json",
        ):
            path = run_dir / filename
            payload = self.read_json(path)
            payload["provenance"] = strip_release_identity(dict(payload["provenance"]))
            payload["session_id"] = None
            self.write_json(path, payload)

        oracle_path = run_dir / "semantic_expectation_oracle.json"
        oracle = self.read_json(oracle_path)
        oracle_provenance = strip_release_identity(dict(oracle["oracle_provenance"]))
        oracle_provenance.update(
            {
                "raw_request_path": "missing/oracle_request.json",
                "raw_response_path": "missing/oracle_response.json",
                "raw_request_hash": fake_hash,
                "raw_request_artifact_hash": fake_hash,
                "raw_response_hash": fake_hash,
                "raw_response_artifact_hash": fake_hash,
            }
        )
        oracle["oracle_provenance"] = oracle_provenance
        oracle["raw_request_path"] = "missing/oracle_request.json"
        oracle["raw_response_path"] = "missing/oracle_response.json"
        oracle["raw_request_hash"] = fake_hash
        oracle["raw_request_artifact_hash"] = fake_hash
        oracle["raw_response_hash"] = fake_hash
        oracle["raw_response_artifact_hash"] = fake_hash
        self.write_json(oracle_path, oracle)

        review_path = run_dir / "semantic_plan_review.json"
        review = self.read_json(review_path)
        reviewer_provenance = strip_release_identity(dict(review["reviewer_provenance"]))
        reviewer_provenance.update(
            {
                "raw_request_path": "missing/reviewer_request.json",
                "raw_response_path": "missing/reviewer_response.json",
                "raw_request_hash": fake_hash,
                "raw_request_artifact_hash": fake_hash,
                "raw_response_hash": fake_hash,
                "raw_response_artifact_hash": fake_hash,
            }
        )
        review["semantic_fit_score"] = 9.2
        review["blockers"] = []
        review["substitute_implementation_check"] = {"passed": True, "checked": True}
        review["verdict"] = "pass"
        review["final_verdict"] = "pass"
        review["non_negotiable_coverage_complete"] = True
        review["reviewer_independence"] = {"independent": True, "status": "passed"}
        review["reviewer_provenance"] = reviewer_provenance
        review["reviewer_raw_request_path"] = "missing/reviewer_request.json"
        review["reviewer_raw_response_path"] = "missing/reviewer_response.json"
        review["reviewer_raw_request_hash"] = fake_hash
        review["reviewer_raw_request_artifact_hash"] = fake_hash
        review["reviewer_raw_response_hash"] = fake_hash
        review["reviewer_raw_response_artifact_hash"] = fake_hash
        self.write_json(review_path, review)

        trace_path = run_dir / "run_trace.jsonl"
        trace = self.read_jsonl(trace_path)
        for record in trace:
            if str(record.get("event_type", "")).startswith("semantic_"):
                paths = record.get("semantic_artifact_paths")
                hashes = record.get("artifact_hashes")
                if isinstance(paths, dict) and isinstance(hashes, dict):
                    for key in list(hashes):
                        paths[key] = f"missing/{key}.json"
                        hashes[key] = fake_hash
        self.write_jsonl(trace_path, trace)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed", result)
        failures = result["semantic_release_checks"]["failures"]
        failure_checks = {failure.get("check") for failure in failures}
        self.assertIn("semantic_raw_artifact", failure_checks)
        self.assertIn("semantic_trace_ordering", failure_checks)
        self.assertIn("oracle_provenance", failure_checks)
        self.assertIn("reviewer_provenance", failure_checks)
        provenance_failures = {
            failure.get("artifact")
            for failure in failures
            if failure.get("check") == "semantic_artifact_integrity"
            and failure.get("field") == "provenance"
        }
        self.assertTrue(
            {
                "semantic_expectation_oracle",
                "semantic_plan",
                "semantic_plan_review",
                "semantic_planner_validation",
            }.issubset(provenance_failures),
            failures,
        )

    def test_codex_semantic_text_run_requires_oracle_and_coverage_angle_match(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        cases = (
            (
                "oracle-partial-coverage",
                "oracle-partial",
                "semantic_expectation_oracle",
                "oracle_requirement_map",
            ),
            (
                "requirement-coverage-partial",
                "coverage-partial",
                "semantic_plan",
                "requirement_coverage_map",
            ),
            (
                "requirement-coverage-mismatch",
                "coverage-mismatch",
                "semantic_plan",
                "requirement_coverage_map",
            ),
        )
        for case_name, mutation, expected_artifact, expected_field in cases:
            with self.subTest(case=case_name):
                run_dir = self.write_text_run(
                    self.temp_dir() / case_name,
                    prompt=prompt,
                    suite_id="public-beta-validation",
                    status="completed_serial_handoff",
                    ok=True,
                    semantic_planning="eligible",
                )
                if mutation == "oracle-partial":
                    oracle = self.read_json(run_dir / "semantic_expectation_oracle.json")
                    oracle["oracle_requirement_map"] = oracle["oracle_requirement_map"][:2]
                    self.write_json(run_dir / "semantic_expectation_oracle.json", oracle)
                else:
                    plan = self.read_json(run_dir / "semantic_plan.json")
                    if mutation == "coverage-partial":
                        plan["requirement_coverage_map"] = plan[
                            "requirement_coverage_map"
                        ][:2]
                    elif mutation == "coverage-mismatch":
                        plan["requirement_coverage_map"][0]["angle_id"] = "angle_unknown"
                    else:
                        raise AssertionError(f"unhandled coverage mutation: {case_name}")
                    self.write_json(run_dir / "semantic_plan.json", plan)

                result = evaluate_public_beta_prompt_run(
                    prompt,
                    run_dir,
                    suite_id="public-beta-validation",
                )

                self.assert_semantic_artifact_integrity_failure(
                    result,
                    artifact=expected_artifact,
                    field=expected_field,
                )

    def test_semantic_release_failure_paths_remain_included_failures(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-001"]
        cases = (
            ("heuristic", "completed_parallel", True, "heuristic_fallback"),
            ("manual", "completed_manual_planner_fallback", True, "missing"),
            ("fixture", "completed_fixture", True, "missing"),
            ("blocked", "blocked_semantic_planner_unavailable", False, "missing"),
        )
        for case_name, status, ok, semantic_planning in cases:
            with self.subTest(case=case_name):
                run_dir = self.write_text_run(
                    self.temp_dir() / case_name,
                    prompt=prompt,
                    suite_id="public-beta-validation",
                    status=status,
                    ok=ok,
                    semantic_planning=semantic_planning,
                )

                result = evaluate_public_beta_prompt_run(
                    prompt,
                    run_dir,
                    suite_id="public-beta-validation",
                )

                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(result["metric_classification"], "included_failure")
                self.assertEqual(result["failure_category"], "artifact_handoff_failure")

    def test_partial_parallel_reliability_counts_passing_text_and_visual_runs(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompts = {prompt["id"]: prompt for prompt in manifest["prompts"]}
        runs_dir = self.temp_dir()
        suite_id = "public-beta-partial-reliability"
        prompt_runs = self.write_all_passing_prompt_runs(
            manifest,
            runs_dir=runs_dir,
            suite_id=suite_id,
        )
        manual_audit_manifest = self.write_manual_trace_audit_manifest(
            runs_dir / "manual-audits"
        )
        for prompt in manifest["prompts"]:
            run_dir = prompt_runs[prompt["id"]]
            if prompt["route"] in {"visual_required", "visual_optional"}:
                self.write_parallel_status(run_dir, status="completed_parallel", planned=5, accepted=5)
        text_partial = prompt_runs["pb-text-003"]
        text_status = self.read_json(text_partial / "run_status.json")
        text_status["status"] = "completed_partial_parallel"
        self.write_json(text_partial / "run_status.json", text_status)
        self.write_parallel_status(
            text_partial,
            status="completed_partial_parallel",
            planned=6,
            accepted=5,
            failed=1,
        )
        self.write_parallel_status(
            prompt_runs["pb-visual-010"],
            status="completed_partial_parallel",
            planned=5,
            accepted=4,
            failed=1,
        )

        payload = run_public_beta_validation(
            runs_dir=self.temp_dir(),
            suite_id=suite_id,
            clean=True,
            prompt_runs=prompt_runs,
            manual_audit_manifest=manual_audit_manifest,
        )

        self.assertEqual(payload["status"], "passed")
        reliability = payload["reliability"]["partial_parallel"]
        self.assertEqual(reliability["partial_parallel_runs"], 2)
        self.assertEqual(reliability["completed_real_parallel_stage_runs"], 20)
        self.assertEqual(reliability["partial_parallel_rate"], 0.1)
        self.assertTrue(reliability["enforcement_active"])
        self.assertEqual(reliability["threshold_status"], "warning")
        runs = {run["id"]: run for run in payload["runs"]}
        self.assertEqual(
            runs["pb-text-003"]["partial_parallel_summary"]["reason_category"],
            "failed_tasks",
        )
        self.assertTrue(
            runs["pb-text-003"]["partial_parallel_summary"]["final_artifact_gate_passed"]
        )
        self.assertEqual(
            runs["pb-visual-010"]["partial_parallel_summary"]["accepted_shard_count"],
            4,
        )
        summary_text = Path(payload["artifacts"]["summary"]).read_text(encoding="utf-8")
        self.assertIn("Partial parallel rate: 10.0% (2 / 20)", summary_text)
        self.assertIn("Threshold status: warning; enforcement_active=true", summary_text)

    def test_partial_parallel_regression_band_fails_when_denominator_is_enforced(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        runs_dir = self.temp_dir()
        suite_id = "public-beta-partial-regression"
        prompt_runs = self.write_all_passing_prompt_runs(
            manifest,
            runs_dir=runs_dir,
            suite_id=suite_id,
        )
        for prompt in manifest["prompts"]:
            run_dir = prompt_runs[prompt["id"]]
            if prompt["route"] in {"visual_required", "visual_optional"}:
                self.write_parallel_status(run_dir, status="completed_parallel", planned=5, accepted=5)
        for prompt_id in ("pb-text-003", "pb-text-004", "pb-visual-010"):
            run_dir = prompt_runs[prompt_id]
            if prompt_id.startswith("pb-text"):
                run_status = self.read_json(run_dir / "run_status.json")
                run_status["status"] = "completed_partial_parallel"
                self.write_json(run_dir / "run_status.json", run_status)
            self.write_parallel_status(
                run_dir,
                status="completed_partial_parallel",
                planned=5,
                accepted=4,
                failed=1,
            )

        with self.assertRaises(PublicBetaValidationError) as raised:
            run_public_beta_validation(
                runs_dir=self.temp_dir(),
                suite_id=suite_id,
                clean=True,
                prompt_runs=prompt_runs,
            )

        payload = self.read_json(raised.exception.results_path)
        reliability = payload["reliability"]["partial_parallel"]
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(reliability["partial_parallel_runs"], 3)
        self.assertEqual(reliability["completed_real_parallel_stage_runs"], 20)
        self.assertEqual(reliability["threshold_status"], "regression_failure")
        self.assertTrue(reliability["release_gate_blocking"])
        self.assertTrue(
            any("partial-parallel reliability regression" in gap for gap in payload["remaining_gaps"]),
            payload["remaining_gaps"],
        )

    def test_non_passing_partial_parallel_keeps_artifact_gate_failure(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = {prompt["id"]: prompt for prompt in manifest["prompts"]}["pb-text-003"]
        run_dir = self.write_text_run(
            self.temp_dir() / "partial-missing-report",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_partial_parallel",
        )
        self.write_parallel_status(
            run_dir,
            status="completed_partial_parallel",
            planned=6,
            accepted=5,
            failed=1,
        )
        (run_dir / "report.md").unlink()

        result = evaluate_public_beta_prompt_run(prompt, run_dir)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_category"], "artifact_handoff_failure")
        self.assertFalse(result["partial_parallel_summary"]["final_artifact_gate_passed"])
        self.assertEqual(
            result["partial_parallel_summary"]["reason_category"],
            "failed_tasks",
        )

    def test_visual_prompt_requires_completed_auto_visual_to_pass(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = next(
            prompt for prompt in manifest["prompts"] if prompt["route"] == "visual_required"
        )
        run_dir = self.write_visual_run(
            self.temp_dir() / "visual-text-terminal",
            run_status="completed_parallel",
            provider_status="blocked_missing_visual_provider",
        )

        result = evaluate_public_beta_prompt_run(prompt, run_dir)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["metric_classification"], "included_failure")
        self.assertEqual(result["failure_category"], "provider_failure")

    def test_visual_prompt_rejects_shallow_completed_placeholder_files(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = next(
            prompt for prompt in manifest["prompts"] if prompt["route"] == "visual_required"
        )
        run_dir = self.write_visual_run(
            self.temp_dir() / "visual-placeholder",
            prompt=prompt,
            suite_id="public-beta-validation",
            run_status="completed_auto_visual",
            provider_status="completed_auto_visual",
            release_grade=False,
        )

        result = evaluate_public_beta_prompt_run(prompt, run_dir)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_category"], "vlm_failure")
        checks = {
            failure["check"]
            for failure in result["visual_release_checks"]["failures"]
        }
        self.assertIn("codex_native_visual_acquisition_evidence", checks)
        self.assertIn("codex_interactive_vlm_handoff_observations", checks)
        self.assertIn("report_cited_visual_or_mixed_claim", checks)

    def test_supplied_run_must_match_prompt_suite_and_fresh_timestamp(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompts = {prompt["id"]: prompt for prompt in manifest["prompts"]}
        reused_run = self.write_text_run(
            self.temp_dir() / "reused",
            prompt=prompts["pb-text-002"],
            suite_id="other-suite",
            status="completed_parallel",
            created_at="2020-01-01T00:00:00Z",
        )

        result = evaluate_public_beta_prompt_run(
            prompts["pb-text-001"],
            reused_run,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        failures = result["supplied_run_binding"]["failures"]
        self.assertTrue(any("prompt_id" in failure for failure in failures), failures)
        self.assertTrue(any("suite_id" in failure for failure in failures), failures)
        self.assertTrue(any("older than" in failure for failure in failures), failures)

    def test_supplied_run_rejects_stale_evidence_when_run_status_is_fresh(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = manifest["prompts"][0]
        run_dir = self.write_text_run(
            self.temp_dir() / "stale-evidence",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_parallel",
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["created_at"] = "2020-01-01T00:00:00Z"
        evidence["completed_at"] = "2020-01-01T00:00:00Z"
        self.write_json(run_dir / "evidence.json", evidence)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        failures = result["supplied_run_binding"]["failures"]
        self.assertTrue(any("evidence:" in failure for failure in failures), failures)
        self.assertTrue(any("older than" in failure for failure in failures), failures)

    def test_supplied_run_rejects_mismatched_report_status_run_id(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = manifest["prompts"][0]
        run_dir = self.write_text_run(
            self.temp_dir() / "mismatched-report",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_parallel",
        )
        report_status = self.read_json(run_dir / "report_status.json")
        report_status["run_id"] = "other-run"
        self.write_json(run_dir / "report_status.json", report_status)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        failures = result["supplied_run_binding"]["failures"]
        self.assertTrue(any("run_id values disagree" in failure for failure in failures), failures)
        self.assertTrue(any("report_status=other-run" in failure for failure in failures), failures)

    def test_missing_canonical_identity_still_fails_artifact_handoff(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = manifest["prompts"][0]

        for missing_field in ("prompt_id", "suite_id", "execution_mode", "runner_mode"):
            with self.subTest(missing_field=missing_field):
                run_dir = self.write_text_run(
                    self.temp_dir() / f"missing-{missing_field}",
                    prompt=prompt,
                    suite_id="public-beta-validation",
                    status="completed_parallel",
                )
                for artifact_name in (
                    "run_status.json",
                    "evidence.json",
                    "report_status.json",
                    "search_tasks.json",
                ):
                    payload = self.read_json(run_dir / artifact_name)
                    payload.pop(missing_field, None)
                    self.write_json(run_dir / artifact_name, payload)
                search_results = self.read_jsonl(run_dir / "search_results.jsonl")
                search_results[0].pop(missing_field, None)
                self.write_jsonl(run_dir / "search_results.jsonl", search_results)

                result = evaluate_public_beta_prompt_run(
                    prompt,
                    run_dir,
                    suite_id="public-beta-validation",
                )

                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["failure_category"], "artifact_handoff_failure")
                failures = result["supplied_run_binding"]["failures"]
                self.assertTrue(
                    any(missing_field in failure for failure in failures),
                    failures,
                )

    def test_legacy_mode_field_does_not_satisfy_execution_mode_contract(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = manifest["prompts"][0]
        run_dir = self.write_text_run(
            self.temp_dir() / "legacy-mode-only",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_parallel",
        )
        for artifact_name in ("run_status.json", "evidence.json", "report_status.json"):
            payload = self.read_json(run_dir / artifact_name)
            payload.pop("execution_mode", None)
            payload["mode"] = "codex-plugin"
            self.write_json(run_dir / artifact_name, payload)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_category"], "artifact_handoff_failure")
        self.assertTrue(
            any(
                "execution_mode is missing" in failure
                for failure in result["supplied_run_binding"]["failures"]
            ),
            result["supplied_run_binding"]["failures"],
        )
        self.assertIn(
            "execution_mode must be codex-plugin for Codex-native completion",
            result["codex_native_handoff_checks"]["failures"],
        )

    def test_non_full_runner_mode_remains_rejected(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = manifest["prompts"][0]
        run_dir = self.write_text_run(
            self.temp_dir() / "quick-runner-mode",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_parallel",
        )
        for artifact_name in ("run_status.json", "evidence.json", "report_status.json"):
            payload = self.read_json(run_dir / artifact_name)
            payload["runner_mode"] = "quick-chat"
            self.write_json(run_dir / artifact_name, payload)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_category"], "artifact_handoff_failure")
        self.assertTrue(
            any(
                "runner_mode must be full-runner" in failure
                for failure in result["supplied_run_binding"]["failures"]
            ),
            result["supplied_run_binding"]["failures"],
        )

    def test_completed_codex_plugin_full_runner_identity_envelope_passes_contract(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = manifest["prompts"][0]
        run_dir = self.write_text_run(
            self.temp_dir() / "full-runner-contract",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_parallel",
        )

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(result["supplied_run_binding"]["valid"])
        checks = result["codex_native_handoff_checks"]
        self.assertTrue(checks["valid"], checks)
        self.assertEqual(checks["execution_mode"], "codex-plugin")
        self.assertEqual(checks["runner_mode"], "full-runner")
        self.assertEqual(checks["selected_mode"], "full-runner")
        self.assertGreaterEqual(checks["matching_codex_native_search_results"], 1)
        search_result = self.read_jsonl(run_dir / "search_results.jsonl")[0]
        for field in (
            "id",
            "task_id",
            "angle_id",
            "route",
            "query",
            "url",
            "title",
            "snippet",
            "result_type",
            "rank",
            "accessed_at",
            "policy_decision",
            "provider",
            "provider_mode",
            "retrieval_status",
            "prompt_id",
            "suite_id",
            "prompt_hash",
            "handoff_artifact",
        ):
            self.assertIn(field, search_result)
        self.assertEqual(search_result["provider"], "codex-native")
        self.assertEqual(search_result["provider_mode"], "real")
        self.assertEqual(search_result["retrieval_status"], "fetched")
        self.assertEqual(search_result["policy_decision"], "allowed")
        self.assertNotIn("hidden_codex_api_call", search_result)
        self.assertNotIn("codex_native_api_call", search_result)

    def test_non_codex_plugin_execution_mode_remains_rejected(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = manifest["prompts"][0]
        run_dir = self.write_text_run(
            self.temp_dir() / "manual-mode",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_parallel",
        )
        for artifact_name in ("run_status.json", "evidence.json"):
            payload = self.read_json(run_dir / artifact_name)
            payload["execution_mode"] = "manual"
            payload["mode"] = "manual"
            self.write_json(run_dir / artifact_name, payload)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_category"], "artifact_handoff_failure")
        checks = result["codex_native_handoff_checks"]
        self.assertFalse(checks["valid"])
        self.assertIn(
            "execution_mode must be codex-plugin for Codex-native completion",
            checks["failures"],
        )

    def test_non_release_search_handoff_markers_remain_rejected(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = manifest["prompts"][0]
        cases = (
            {"provider_mode": "fixture"},
            {"provider_mode": "manual"},
            {"provider_mode": "user_provided"},
            {"provider_mode": "post_hoc"},
            {"codex_native_api_call": True},
            {"hidden_codex_api_call": True},
            {"hidden_codex_api_call": False},
        )

        for overrides in cases:
            with self.subTest(overrides=overrides):
                run_dir = self.write_text_run(
                    self.temp_dir() / ("search-marker-" + str(len(overrides))),
                    prompt=prompt,
                    suite_id="public-beta-validation",
                    status="completed_parallel",
                )
                search_results = self.read_jsonl(run_dir / "search_results.jsonl")
                search_results[0].update(overrides)
                self.write_jsonl(run_dir / "search_results.jsonl", search_results)

                result = evaluate_public_beta_prompt_run(
                    prompt,
                    run_dir,
                    suite_id="public-beta-validation",
                )

                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["failure_category"], "artifact_handoff_failure")
                self.assertFalse(result["codex_native_handoff_checks"]["valid"])

    def test_incomplete_codex_native_search_result_is_rejected(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = manifest["prompts"][0]
        run_dir = self.write_text_run(
            self.temp_dir() / "incomplete-search-result",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_parallel",
        )
        search_results = self.read_jsonl(run_dir / "search_results.jsonl")
        search_results[0].pop("angle_id")
        self.write_jsonl(run_dir / "search_results.jsonl", search_results)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_category"], "artifact_handoff_failure")
        checks = result["codex_native_handoff_checks"]
        self.assertFalse(checks["valid"])
        self.assertIn(
            "search_results.jsonl contains incomplete or non-release Codex-native "
            "search handoff records for this prompt",
            checks["failures"],
        )

    def test_supplied_run_rejects_hidden_codex_native_api_assumption(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = manifest["prompts"][0]
        run_dir = self.write_text_run(
            self.temp_dir() / "hidden-api",
            prompt=prompt,
            suite_id="public-beta-validation",
            status="completed_parallel",
        )
        run_status = self.read_json(run_dir / "run_status.json")
        run_status["hidden_codex_api_call"] = True
        self.write_json(run_dir / "run_status.json", run_status)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_category"], "artifact_handoff_failure")
        self.assertIn("hidden Codex-native API call", result["failure_detail"])

    def test_visual_identity_status_artifacts_have_matching_run_id_and_fresh_timestamp(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = next(
            prompt for prompt in manifest["prompts"] if prompt["route"] == "visual_required"
        )
        run_dir = self.write_visual_run(
            self.temp_dir() / "visual-identity",
            prompt=prompt,
            suite_id="public-beta-validation",
            run_status="completed_auto_visual",
            provider_status="completed_auto_visual",
        )

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(result["visual_release_checks"]["valid"], result)
        self.assertGreaterEqual(
            result["visual_release_checks"]["counts"]["real_candidates"],
            10,
        )
        self.assertGreaterEqual(
            result["visual_release_checks"]["counts"]["real_vlm_images_analyzed"],
            3,
        )
        self.assertGreaterEqual(
            result["visual_release_checks"]["counts"][
                "report_cited_visual_or_mixed_claims"
            ],
            1,
        )
        for artifact_name in (
            "report_status.json",
            "visual_provider_status.json",
            "visual_search_plan.json",
        ):
            with self.subTest(artifact_name=artifact_name):
                payload = self.read_json(run_dir / artifact_name)
                self.assertEqual(payload["run_id"], run_dir.name)
                self.assertEqual(payload["prompt_id"], prompt["id"])
                self.assertEqual(payload["suite_id"], "public-beta-validation")
                self.assertEqual(payload["prompt_hash"], self.prompt_hash(prompt["prompt"]))
                self.assertEqual(payload["original_question"], prompt["prompt"])
                self.assertEqual(payload["execution_mode"], "codex-plugin")
                self.assertEqual(payload["runner_mode"], "full-runner")
                timestamp = (
                    payload.get("completed_at")
                    or payload.get("generated_at")
                    or payload.get("created_at")
                    or payload.get("updated_at")
                )
                self.assertIsInstance(timestamp, str)
                self.assertTrue(timestamp.endswith("Z"), timestamp)

    def test_visual_partial_supplied_run_binding_uses_present_identity_artifacts(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = next(
            prompt for prompt in manifest["prompts"] if prompt["route"] == "visual_required"
        )
        run_dir = self.write_visual_run(
            self.temp_dir() / "partial-visual-binding",
            prompt=prompt,
            suite_id="public-beta-validation",
            run_status="partial_auto_visual",
            provider_status="partial_auto_visual",
            candidate_count=10,
            analyzed_image_count=1,
        )

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["terminal_status"], "partial_auto_visual")
        binding = result["supplied_run_binding"]
        self.assertTrue(binding["valid"], binding)
        self.assertIn("evidence", binding["bound_artifacts"])
        self.assertIn("report_status", binding["bound_artifacts"])
        self.assertIn("visual_provider_status", binding["bound_artifacts"])
        self.assertFalse(
            any("prompt_id is missing" in failure for failure in binding["failures"]),
            binding["failures"],
        )
        self.assertFalse(
            any("suite_id is missing" in failure for failure in binding["failures"]),
            binding["failures"],
        )

    def test_visual_supplied_run_rejects_mismatched_visual_provider_run_id(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = next(
            prompt for prompt in manifest["prompts"] if prompt["route"] == "visual_required"
        )
        run_dir = self.write_visual_run(
            self.temp_dir() / "mismatched-provider",
            prompt=prompt,
            suite_id="public-beta-validation",
            run_status="completed_auto_visual",
            provider_status="completed_auto_visual",
        )
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        provider_status["run_id"] = "other-run"
        self.write_json(run_dir / "visual_provider_status.json", provider_status)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        failures = result["supplied_run_binding"]["failures"]
        self.assertTrue(any("run_id values disagree" in failure for failure in failures), failures)
        self.assertTrue(
            any("visual_provider_status=other-run" in failure for failure in failures),
            failures,
        )

    def test_visual_gate_rejects_one_eligible_vlm_image_even_with_report_citation(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = next(
            prompt for prompt in manifest["prompts"] if prompt["route"] == "visual_required"
        )
        run_dir = self.write_visual_run(
            self.temp_dir() / "one-vlm-image",
            prompt=prompt,
            suite_id="public-beta-validation",
            run_status="completed_auto_visual",
            provider_status="completed_auto_visual",
            candidate_count=10,
            analyzed_image_count=1,
        )

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_category"], "vlm_failure")
        checks = {
            failure["check"]
            for failure in result["visual_release_checks"]["failures"]
        }
        self.assertIn("at_least_3_codex_interactive_real_analyzed_images", checks)
        counts = result["visual_release_checks"]["counts"]
        self.assertEqual(counts["real_candidates"], 10)
        self.assertEqual(counts["real_vlm_images_analyzed"], 1)
        self.assertEqual(counts["report_cited_visual_or_mixed_claims"], 1)

    def test_visual_gate_rejects_loose_vlm_observations_without_handoff_markers(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = next(
            prompt for prompt in manifest["prompts"] if prompt["route"] == "visual_required"
        )
        run_dir = self.write_visual_run(
            self.temp_dir() / "loose-vlm-observations",
            prompt=prompt,
            suite_id="public-beta-validation",
            run_status="completed_auto_visual",
            provider_status="completed_auto_visual",
        )
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        for observation in observations[1:]:
            for key in (
                "codex_native_handoff",
                "codex_interactive_handoff",
                "handoff_recorded",
                "handoff_artifact",
                "explicit_artifact_handoff",
            ):
                observation.pop(key, None)
            provenance = dict(observation.get("provider_provenance") or {})
            for key in (
                "codex_native_handoff",
                "codex_interactive_handoff",
                "handoff_recorded",
                "handoff_artifact",
                "explicit_artifact_handoff",
            ):
                provenance.pop(key, None)
            observation["provider_provenance"] = provenance
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_category"], "vlm_failure")
        checks = {
            failure["check"]
            for failure in result["visual_release_checks"]["failures"]
        }
        self.assertIn("at_least_3_codex_interactive_real_analyzed_images", checks)
        counts = result["visual_release_checks"]["counts"]
        self.assertEqual(counts["real_vlm_observations"], 1)
        self.assertEqual(counts["real_vlm_images_analyzed"], 1)
        self.assertEqual(counts["report_cited_visual_or_mixed_claims"], 1)

    def test_visual_gate_rejects_budget_pruned_vlm_observations(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = next(
            prompt for prompt in manifest["prompts"] if prompt["route"] == "visual_required"
        )
        run_dir = self.write_visual_run(
            self.temp_dir() / "budget-pruned-vlm-observations",
            prompt=prompt,
            suite_id="public-beta-validation",
            run_status="completed_auto_visual",
            provider_status="completed_auto_visual",
        )
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        for observation in observations[1:]:
            observation["policy_decision"] = "budget_pruned"
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_category"], "vlm_failure")
        checks = {
            failure["check"]
            for failure in result["visual_release_checks"]["failures"]
        }
        self.assertIn("at_least_3_codex_interactive_real_analyzed_images", checks)
        counts = result["visual_release_checks"]["counts"]
        self.assertEqual(counts["real_vlm_observations"], 1)
        self.assertEqual(counts["real_vlm_images_analyzed"], 1)
        self.assertEqual(counts["report_cited_visual_or_mixed_claims"], 1)

    def test_visual_gate_counts_codex_interactive_images_with_real_candidate_fetch_lineage(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = next(
            prompt for prompt in manifest["prompts"] if prompt["route"] == "visual_required"
        )
        run_dir = self.write_visual_run(
            self.temp_dir() / "codex-interactive-image-lineage",
            prompt=prompt,
            suite_id="public-beta-validation",
            run_status="completed_auto_visual",
            provider_status="completed_auto_visual",
        )

        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        for record in [*candidates, *fetches]:
            record["provider"] = "child-discovered-image-url"
            record["provider_run_id"] = "child-discovered-image-url:real-001"
            record.pop("search_provider", None)
            provenance = dict(record.get("provider_provenance") or {})
            provenance["provider"] = "child-discovered-image-url"
            provenance["provider_run_id"] = "child-discovered-image-url:real-001"
            provenance.pop("search_provider", None)
            record["provider_provenance"] = provenance
        provider_status["providers"][0]["provider"] = "child-discovered-image-url"
        provider_status["providers"][0]["provider_run_id"] = "child-discovered-image-url:real-001"

        evidence = self.read_json(run_dir / "evidence.json")
        for image in evidence["images"]:
            image["provider"] = "codex-interactive"
            image["provider_kind"] = "vlm"
            image["analysis_provider"] = "codex-interactive"
            image["handoff_artifact"] = "visual_observations.jsonl"
            image["codex_interactive_handoff"] = True
            image["codex_native_handoff"] = True
            image["provider_provenance"] = {
                "provider": "codex-interactive",
                "provider_kind": "vlm",
                "provider_mode": "real",
                "codex_interactive_handoff": True,
                "codex_native_handoff": True,
                "handoff_artifact": "visual_observations.jsonl",
                "external_vlm_call": False,
            }
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)
        self.write_json(run_dir / "visual_provider_status.json", provider_status)
        self.write_json(run_dir / "evidence.json", evidence)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "passed")
        counts = result["visual_release_checks"]["counts"]
        self.assertGreaterEqual(counts["real_candidates"], 10)
        self.assertGreaterEqual(counts["real_vlm_images_analyzed"], 3)
        self.assertEqual(counts["report_cited_visual_or_mixed_claims"], 1)

    def test_visual_gate_ignores_budget_pruned_surplus_records(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = next(
            prompt for prompt in manifest["prompts"] if prompt["route"] == "visual_required"
        )
        run_dir = self.write_visual_run(
            self.temp_dir() / "budget-pruned-surplus",
            prompt=prompt,
            suite_id="public-beta-validation",
            run_status="completed_auto_visual",
            provider_status="completed_auto_visual",
        )
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        pruned_candidate = dict(candidates[0])
        pruned_candidate["candidate_id"] = "cand_pruned_surplus"
        pruned_candidate["policy_decision"] = "budget_pruned"
        pruned_candidate["candidate_status"] = "budget_pruned"
        pruned_fetch = dict(fetches[0])
        pruned_fetch["fetch_id"] = "fetch_pruned_surplus"
        pruned_fetch["candidate_id"] = "cand_pruned_surplus"
        pruned_fetch["policy_decision"] = "budget_pruned"
        pruned_fetch["fetch_status"] = "budget_pruned"
        fetches.append(pruned_fetch)
        candidates.append(pruned_candidate)
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["visual_release_checks"]["valid"])

    def test_visual_gate_rejects_policy_denied_surplus_records(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = next(
            prompt for prompt in manifest["prompts"] if prompt["route"] == "visual_required"
        )
        run_dir = self.write_visual_run(
            self.temp_dir() / "policy-denied-surplus",
            prompt=prompt,
            suite_id="public-beta-validation",
            run_status="completed_auto_visual",
            provider_status="completed_auto_visual",
        )
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        denied_candidate = dict(candidates[0])
        denied_candidate["candidate_id"] = "cand_policy_denied_surplus"
        denied_candidate["policy_decision"] = "disallowed"
        candidates.append(denied_candidate)
        self.write_jsonl(run_dir / "visual_candidates.jsonl", candidates)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        checks = {
            failure["check"]
            for failure in result["visual_release_checks"]["failures"]
        }
        self.assertIn("policy_allows_release_counting", checks)

    def test_visual_run_rejects_hidden_codex_interactive_api_assumption(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = next(
            prompt for prompt in manifest["prompts"] if prompt["route"] == "visual_required"
        )
        run_dir = self.write_visual_run(
            self.temp_dir() / "hidden-vlm-api",
            prompt=prompt,
            suite_id="public-beta-validation",
            run_status="completed_auto_visual",
            provider_status="completed_auto_visual",
        )
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        observations[0]["hidden_codex_api_call"] = True
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_category"], "vlm_failure")
        checks = {
            failure["check"]
            for failure in result["visual_release_checks"]["failures"]
        }
        self.assertIn("codex_interactive_hidden_api_rejected", checks)

    def test_visual_observation_must_keep_handoff_flags_with_report_links(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = next(
            prompt for prompt in manifest["prompts"] if prompt["route"] == "visual_required"
        )
        run_dir = self.write_visual_run(
            self.temp_dir() / "split-handoff-links",
            prompt=prompt,
            suite_id="public-beta-validation",
            run_status="completed_auto_visual",
            provider_status="completed_auto_visual",
        )
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        for observation in observations:
            observation.pop("codex_native_handoff", None)
            observation.pop("codex_interactive_handoff", None)
            observation.pop("handoff_recorded", None)
            observation.pop("handoff_artifact", None)
            observation.pop("explicit_artifact_handoff", None)
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)

        result = evaluate_public_beta_prompt_run(
            prompt,
            run_dir,
            suite_id="public-beta-validation",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_category"], "vlm_failure")
        checks = {
            failure["check"]
            for failure in result["visual_release_checks"]["failures"]
        }
        self.assertIn("codex_interactive_vlm_handoff_observations", checks)
        self.assertEqual(
            result["visual_release_checks"]["counts"]["real_vlm_images_analyzed"],
            0,
        )
        self.assertEqual(
            result["visual_release_checks"]["counts"]["real_vlm_observations"],
            0,
        )

    def test_codex_native_runs_can_complete_without_external_gate_results(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        runs_dir = self.temp_dir()
        suite_id = "codex-native-no-external-gates"
        prompt_runs = {}
        for prompt in manifest["prompts"]:
            if prompt["route"] in {"visual_required", "visual_optional"}:
                prompt_runs[prompt["id"]] = self.write_visual_run(
                    runs_dir / prompt["id"],
                    prompt=prompt,
                    suite_id=suite_id,
                    run_status="completed_auto_visual",
                    provider_status="completed_auto_visual",
                )
            else:
                prompt_runs[prompt["id"]] = self.write_text_run(
                    runs_dir / prompt["id"],
                    prompt=prompt,
                    suite_id=suite_id,
                    status="completed_parallel",
                )
        manual_audit_manifest = self.write_manual_trace_audit_manifest(
            runs_dir / "manual-audits"
        )

        payload = run_public_beta_validation(
            runs_dir=self.temp_dir(),
            suite_id=suite_id,
            clean=True,
            prompt_runs=prompt_runs,
            manual_audit_manifest=manual_audit_manifest,
        )
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["outcome_counts"]["passed"], 20)
        self.assertTrue(payload["release_gate_ready"])
        self.assertTrue(payload["issue_75_completion_ready"])
        self.assertTrue(payload["release_gate_components"]["prompt_metrics_ready"])
        self.assertFalse(payload["release_gate_components"]["external_gates_required"])
        self.assertTrue(payload["release_gate_components"]["external_gates_ready"])
        self.assertEqual(
            payload["external_gate_results"][
                "automated_cli_real_provider_visual_e2e"
            ]["status"],
            "not_supplied",
        )
        self.assertEqual(
            payload["remaining_gaps"],
            ["No remaining release-gate gaps were detected."],
        )

    def test_external_gated_completion_accepts_automated_cli_as_external_json_only(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        runs_dir = self.temp_dir()
        suite_id = "external-gated-real-use"
        prompt_runs = self.write_all_passing_prompt_runs(
            manifest,
            runs_dir=runs_dir / "prompt-runs",
            suite_id=suite_id,
        )
        manual_audit_manifest = self.write_manual_trace_audit_manifest(
            runs_dir / "manual-audits"
        )
        gate_results = {}
        for gate_id in EXTERNAL_GATE_REQUIREMENTS:
            gate_path = runs_dir / f"{gate_id}.json"
            self.write_json(gate_path, self.passing_external_gate_payload(gate_id))
            gate_results[gate_id] = gate_path

        payload = run_public_beta_validation(
            runs_dir=self.temp_dir(),
            suite_id=suite_id,
            clean=True,
            prompt_runs=prompt_runs,
            gate_results=gate_results,
            completion_mode="external-gated",
            manual_audit_manifest=manual_audit_manifest,
        )

        automated_cli_gate = "automated_cli_real_provider_visual_e2e"
        components = payload["release_gate_components"]
        self.assertEqual(payload["status"], "passed")
        self.assertTrue(payload["release_gate_ready"])
        self.assertTrue(payload["issue_75_completion_ready"])
        self.assertTrue(components["prompt_metrics_ready"])
        self.assertTrue(components["external_gates_required"])
        self.assertTrue(components["external_gates_ready"])
        self.assertNotIn(automated_cli_gate, components["required_prompt_metric_gate_ids"])
        self.assertNotIn(automated_cli_gate, components["required_codex_native_gate_ids"])
        self.assertIn(automated_cli_gate, components["required_external_gate_ids"])
        self.assertEqual(components["optional_diagnostic_gate_ids"], [])
        self.assertEqual(components["failed_external_gate_ids"], [])
        self.assertEqual(
            payload["prompt_metrics"][automated_cli_gate]["prompt_count"],
            0,
        )
        self.assertFalse(
            payload["prompt_metrics"][automated_cli_gate]["completion_required"]
        )
        self.assertTrue(
            all(
                result["status"] == "passed"
                for result in payload["external_gate_results"].values()
            )
        )
        self.assertEqual(
            payload["external_gate_results"][automated_cli_gate]["status"],
            "passed",
        )
        self.assertEqual(
            payload["remaining_gaps"],
            ["No remaining release-gate gaps were detected."],
        )

    def test_minimal_spoofed_external_gate_results_are_rejected_as_diagnostics(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        runs_dir = self.temp_dir()
        suite_id = "spoofed-external-gates"
        prompt_runs = self.write_all_passing_prompt_runs(
            manifest,
            runs_dir=runs_dir / "prompt-runs",
            suite_id=suite_id,
        )
        manual_audit_manifest = self.write_manual_trace_audit_manifest(
            runs_dir / "manual-audits"
        )
        gate_results = {}
        for gate_id in EXTERNAL_GATE_REQUIREMENTS:
            spoofed_gate = runs_dir / f"{gate_id}.json"
            self.write_json(spoofed_gate, self.minimal_spoofed_gate_payload(gate_id))
            gate_results[gate_id] = spoofed_gate

        payload = run_public_beta_validation(
            runs_dir=self.temp_dir(),
            suite_id=suite_id,
            clean=True,
            prompt_runs=prompt_runs,
            gate_results=gate_results,
            manual_audit_manifest=manual_audit_manifest,
        )

        self.assertEqual(payload["status"], "passed")
        self.assertTrue(payload["release_gate_ready"])
        self.assertTrue(payload["issue_75_completion_ready"])
        for gate_id, gate in payload["external_gate_results"].items():
            self.assertEqual(gate["status"], "failed", gate_id)
            self.assertFalse(gate["release_gate_ready"], gate_id)
            self.assertTrue(
                any(
                    "acceptance" in failure
                    or "scenarios" in failure
                    or "skill_transcript_gate" in failure
                    for failure in gate["failures"]
                ),
                (gate_id, gate["failures"]),
            )
        self.assertCountEqual(
            payload["release_gate_components"]["optional_diagnostic_gates_failed"],
            list(EXTERNAL_GATE_REQUIREMENTS),
        )

    def test_summary_redacts_private_absolute_run_paths(self) -> None:
        manifest = load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)
        prompt = manifest["prompts"][0]
        suite_id = "summary-redaction"
        private_shaped_run = self.write_text_run(
            self.temp_dir() / "Users" / "alice" / "public-beta-run",
            prompt=prompt,
            suite_id=suite_id,
            status="completed_parallel",
        )

        with self.assertRaises(PublicBetaValidationError) as raised:
            run_public_beta_validation(
                runs_dir=self.temp_dir(),
                suite_id=suite_id,
                clean=True,
                prompt_runs={prompt["id"]: private_shaped_run},
            )

        payload = self.read_json(raised.exception.results_path)
        summary = Path(payload["artifacts"]["summary"]).read_text(encoding="utf-8")
        self.assertNotIn(str(private_shaped_run), summary)
        self.assertNotIn("/Users/alice", summary)
        self.assertIn(f"supplied-run:{prompt['id']}", summary)
        self.assertTrue(payload["acceptance"]["summary_artifact_public_safe"])

    def test_cli_allow_blocked_outputs_sanitized_results_and_exits_zero(self) -> None:
        runs_dir = self.temp_dir()
        command = subprocess.run(
            [
                str(RUNNER),
                "public-beta-validation",
                "--runs-dir",
                str(runs_dir),
                "--suite-id",
                "cli-public-beta",
                "--clean",
                "--allow-blocked",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["completion_mode"], "codex-native")
        self.assertFalse(payload["raw_run_bundles_copied"])
        self.assertTrue(Path(payload["artifacts"]["results"]).is_file())
        self.assertTrue(Path(payload["artifacts"]["summary"]).is_file())
        self.assertFalse(payload["issue_75_completion_ready"])

    def semantic_bounded_tasks(self, prompt: dict[str, Any]) -> list[dict[str, Any]]:
        is_visual = prompt["route"] in {"visual_required", "visual_optional"}
        return [
            {
                "task_id": f"task_search_{index:03d}",
                "angle_id": f"angle_{index:03d}",
                "query": f"{prompt['prompt']} bounded semantic task {index}",
                "route": prompt["route"],
                "freshness_requirement": "any",
                "source_policy": {"decision": "allowed", "flags": []},
                "expected_source_types": ["web"],
                "expected_visual_targets": [f"visual target {index}"] if is_visual else [],
                "expected_artifacts": ["source list", "supporting quotes"],
                "success_criteria": ["Claims remain tied to source spans."],
                "done_condition": "At least one source-backed claim is ready for synthesis.",
                "max_sources": 3,
                "max_images": 3 if is_visual else 0,
            }
            for index in range(1, 4)
        ]

    def search_task_from_bounded_task(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": task["task_id"],
            "task_id": task["task_id"],
            "semantic_plan_task_id": task["task_id"],
            "semantic_plan_hash": "joinable-at-validation-time",
            "angle_id": task["angle_id"],
            "query": task["query"],
            "route": task["route"],
            "freshness_requirement": task["freshness_requirement"],
            "source_policy": task["source_policy"],
            "expected_source_types": task["expected_source_types"],
            "expected_visual_targets": task["expected_visual_targets"],
            "expected_artifacts": task["expected_artifacts"],
            "success_criteria": task["success_criteria"],
            "done_condition": task["done_condition"],
            "max_sources": task["max_sources"],
            "max_images": task["max_images"],
            "provider": "codex-native",
            "approved_delta_id": "base_plan",
        }

    def research_task_from_bounded_task(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            **self.search_task_from_bounded_task(task),
            "state": "merged",
            "assigned_subagent_id": f"codex-exec-{task['task_id']}",
            "output_shard_path": f"evidence_shards/{task['task_id']}/evidence_shard.json",
        }

    def visual_task_from_bounded_task(
        self,
        task: dict[str, Any],
        *,
        index: int,
    ) -> dict[str, Any]:
        return {
            "id": f"task_visual_{index:03d}",
            **self.search_task_from_bounded_task(task),
            "visual_tasks": task["expected_visual_targets"],
            "status": "planned",
        }

    def subagent_assignment_record(
        self,
        task: dict[str, Any],
        *,
        index: int,
    ) -> dict[str, Any]:
        return {
            "assignment_id": f"assign_fixture_{index:03d}",
            "task_id": task["task_id"],
            "semantic_plan_task_id": task["task_id"],
            "angle_id": task["angle_id"],
            "route": task["route"],
            "assigned_subagent_id": f"codex-exec-{task['task_id']}",
            "adapter": "codex-exec",
            "approved_delta_id": "base_plan",
        }

    def stamp_semantic_materialization_lineage(
        self,
        run_dir: Path,
        *,
        semantic_plan_hash: str,
    ) -> None:
        def stamp_record(record: dict[str, Any]) -> None:
            task_id = (
                record.get("semantic_plan_task_id")
                or record.get("task_id")
                or record.get("search_task_id")
                or record.get("id")
            )
            if not isinstance(task_id, str) or not task_id.strip():
                return
            record["semantic_plan_task_id"] = task_id.strip()
            record["semantic_plan_hash"] = semantic_plan_hash
            record["approved_delta_id"] = str(record.get("approved_delta_id") or "base_plan")

        for filename in (
            "search_tasks.json",
            "research_tasks.json",
            "visual_tasks.json",
            "visual_search_plan.json",
        ):
            path = run_dir / filename
            if not path.exists():
                continue
            payload = self.read_json(path)
            tasks = payload.get("tasks")
            if isinstance(tasks, list):
                for task in tasks:
                    if isinstance(task, dict):
                        stamp_record(task)
                self.write_json(path, payload)

        evidence_path = run_dir / "evidence.json"
        if evidence_path.exists():
            evidence = self.read_json(evidence_path)
            for key in ("search_tasks", "images"):
                records = evidence.get(key)
                if isinstance(records, list):
                    for record in records:
                        if isinstance(record, dict):
                            stamp_record(record)
            self.write_json(evidence_path, evidence)

        for filename in (
            "search_results.jsonl",
            "subagent_assignments.jsonl",
            "visual_candidates.jsonl",
            "image_fetch_status.jsonl",
            "visual_observations.jsonl",
        ):
            path = run_dir / filename
            if not path.exists():
                continue
            records = self.read_jsonl(path)
            for record in records:
                if isinstance(record, dict):
                    stamp_record(record)
            self.write_jsonl(path, records)

    def write_text_run(
        self,
        run_dir: Path,
        *,
        prompt: dict[str, Any],
        suite_id: str,
        status: str,
        ok: bool = True,
        created_at: str | None = None,
        semantic_planning: str = "eligible",
    ) -> Path:
        run_dir.mkdir(parents=True)
        terminal = True
        timestamp = created_at or self.now()
        semantic_release_statuses = {
            "completed_parallel",
            "completed_partial_parallel",
            "completed_serial_handoff",
        }
        semantic_metadata = self.semantic_planning_metadata(
            semantic_planning,
            enabled=status in semantic_release_statuses,
        )
        run_status_payload = {
            "schema_version": "codex-deepresearch.run-status.v0",
            "run_id": run_dir.name,
            "prompt_id": prompt["id"],
            "prompt_hash": self.prompt_hash(prompt["prompt"]),
            "suite_id": suite_id,
            "original_question": prompt["prompt"],
            "execution_mode": "codex-plugin",
            "runner_mode": "full-runner",
            "question": prompt["prompt"],
            "status": status,
            "ok": ok,
            "terminal": terminal,
            "created_at": timestamp,
            "completed_at": timestamp,
            "selected_mode": "full-runner",
            "search_provider": "codex-native",
            "adapter": "codex-exec",
        }
        if semantic_metadata:
            run_status_payload.update(
                {
                    "planner_mode": semantic_metadata["planner_mode"],
                    "semantic_release_eligible": semantic_metadata[
                        "semantic_release_eligible"
                    ],
                    "semantic_planning": semantic_metadata["summary"],
                }
            )
        self.write_json(
            run_dir / "run_status.json",
            run_status_payload,
        )
        bounded_tasks = self.semantic_bounded_tasks(prompt)
        search_tasks = [self.search_task_from_bounded_task(task) for task in bounded_tasks]
        research_tasks = [self.research_task_from_bounded_task(task) for task in bounded_tasks]
        self.write_json(
            run_dir / "search_tasks.json",
            {
                "schema_version": "codex-deepresearch.search-handoff.v0",
                "run_id": run_dir.name,
                "prompt_id": prompt["id"],
                "prompt_hash": self.prompt_hash(prompt["prompt"]),
                "suite_id": suite_id,
                "original_question": prompt["prompt"],
                "execution_mode": "codex-plugin",
                "runner_mode": "full-runner",
                "created_at": timestamp,
                "tasks": search_tasks,
            },
        )
        self.write_json(
            run_dir / "research_tasks.json",
            {
                "schema_version": "codex-deepresearch.parallel-orchestration.v0",
                "run_id": run_dir.name,
                "created_at": timestamp,
                "status": "completed",
                "tasks": research_tasks,
            },
        )
        self.write_json(
            run_dir / "visual_tasks.json",
            {
                "schema_version": "codex-deepresearch.search-handoff.v0",
                "run_id": run_dir.name,
                "created_at": timestamp,
                "tasks": [
                    self.visual_task_from_bounded_task(task, index=index)
                    for index, task in enumerate(bounded_tasks, start=1)
                    if task["route"] != "text_only"
                ],
            },
        )
        self.write_jsonl(
            run_dir / "search_results.jsonl",
            [
                self.codex_native_search_result(
                    prompt,
                    suite_id=suite_id,
                    task=task,
                    rank=index,
                )
                for index, task in enumerate(bounded_tasks, start=1)
            ],
        )
        self.write_jsonl(
            run_dir / "subagent_assignments.jsonl",
            [
                self.subagent_assignment_record(task, index=index)
                for index, task in enumerate(bounded_tasks, start=1)
            ],
        )
        if status.startswith("completed"):
            evidence_payload = {
                "schema_version": "0.1.0",
                "run_id": run_dir.name,
                "prompt_id": prompt["id"],
                "prompt_hash": self.prompt_hash(prompt["prompt"]),
                "suite_id": suite_id,
                "original_question": prompt["prompt"],
                "execution_mode": "codex-plugin",
                "runner_mode": "full-runner",
                "question": prompt["prompt"],
                "created_at": timestamp,
                "mode": "codex-plugin",
                "search_provider": "codex-native",
            }
            if semantic_metadata:
                evidence_payload["semantic_planner"] = {
                    "schema_version": "codex-deepresearch.semantic-planner.v0",
                    "question_class": "general",
                    "broad_question": False,
                    "source": semantic_metadata["source"],
                    "expected_evidence_needs": ["primary_source"],
                    "planner_mode": semantic_metadata["planner_mode"],
                    "semantic_release_eligible": semantic_metadata[
                        "semantic_release_eligible"
                    ],
                    "status": semantic_metadata["summary"]["status"],
                    "diagnostics": dict(semantic_metadata["diagnostics"]),
                    "bounded_tasks": bounded_tasks,
                }
                evidence_payload["search_tasks"] = search_tasks
            self.write_json(
                run_dir / "evidence.json",
                evidence_payload,
            )
            self.write_json(
                run_dir / "report_status.json",
                {
                    "schema_version": "codex-deepresearch.report-status.v0",
                    "run_id": run_dir.name,
                    "prompt_id": prompt["id"],
                    "prompt_hash": self.prompt_hash(prompt["prompt"]),
                    "suite_id": suite_id,
                    "original_question": prompt["prompt"],
                    "execution_mode": "codex-plugin",
                    "runner_mode": "full-runner",
                    "status": "completed",
                    "created_at": timestamp,
                    "generated_at": timestamp,
                    "used_images": [],
                },
            )
            (run_dir / "report.md").write_text("# Public-safe report\n", encoding="utf-8")
            if semantic_metadata:
                self.write_semantic_release_artifacts(
                    run_dir,
                    prompt=prompt,
                    suite_id=suite_id,
                    timestamp=timestamp,
                    metadata=semantic_metadata,
                    bounded_tasks=bounded_tasks,
                )
                write_semantic_materialization_diff(
                    run_dir=run_dir,
                    require_research_tasks=True,
                    require_downstream=True,
                    created_at=timestamp,
                )
        return run_dir

    def semantic_planning_metadata(
        self,
        mode: str,
        *,
        enabled: bool,
    ) -> dict[str, Any] | None:
        if not enabled or mode == "missing":
            return None
        if mode == "eligible":
            planner_mode = "codex_semantic"
            eligible = True
            status = "accepted_codex_semantic"
            source = "codex_semantic"
            diagnostics: dict[str, Any] = {}
            failures: list[dict[str, Any]] = []
            blockers: list[dict[str, Any]] = []
            score: float | None = 9.4
            verdict = "pass"
        elif mode == "heuristic_fallback":
            planner_mode = "heuristic_template_fallback"
            eligible = False
            status = "prepared_heuristic_template_fallback"
            source = "heuristic_template_planner"
            diagnostics = {
                "semantic_release_eligible": False,
                "planner_mode": planner_mode,
                "fallback_kind": "keyword/template fallback planner",
                "user_visible_diagnostic": (
                    "True semantic decomposition did not run; this path is useful "
                    "only as a release-ineligible fallback and cannot satisfy "
                    "semantic planner gates."
                ),
            }
            failures = [
                {"code": "semantic_release_ineligible"},
                {"code": "release_ineligible_planner_mode"},
            ]
            blockers = [
                {
                    "code": "release_ineligible_planner_mode",
                    "planner_mode": planner_mode,
                }
            ]
            score = None
            verdict = "release_ineligible"
        else:
            raise AssertionError(f"unknown semantic_planning fixture mode: {mode}")
        return {
            "planner_mode": planner_mode,
            "semantic_release_eligible": eligible,
            "source": source,
            "diagnostics": diagnostics,
            "failures": failures,
            "blockers": blockers,
            "semantic_fit_score": score,
            "final_verdict": verdict,
            "summary": {
                "schema_version": "codex-deepresearch.semantic-planning-summary.v0",
                "status": status,
                "planner_mode": planner_mode,
                "semantic_release_eligible": eligible,
                "validation_ok": not failures,
                "user_visible_diagnostic": diagnostics.get("user_visible_diagnostic"),
            },
        }

    def write_semantic_release_artifacts(
        self,
        run_dir: Path,
        *,
        prompt: dict[str, Any],
        suite_id: str,
        timestamp: str,
        metadata: dict[str, Any],
        bounded_tasks: list[dict[str, Any]],
    ) -> None:
        generic_terms = {
            "and",
            "across",
            "cite",
            "compare",
            "for",
            "from",
            "guidance",
            "public",
            "research",
            "the",
            "visual",
            "evidence",
        }
        prompt_terms: list[str] = []
        for raw_token in prompt["prompt"].replace("-", " ").replace("/", " ").split():
            token = raw_token.strip(".,:;!?()[]{}\"'").lower()
            if len(token) <= 2 or token in generic_terms or token in prompt_terms:
                continue
            prompt_terms.append(token)
        subject = " ".join(prompt_terms[:4]) or "public beta validation"
        angles = [
            {
                "angle_id": "angle_001",
                "title": f"{subject} source evidence map",
                "research_question": (
                    f"Which primary sources define {subject} evidence-quality tradeoffs?"
                ),
                "question_context": (
                    f"Scope prompt terms: {subject}; public beta prompt {prompt['id']}."
                ),
                "route": prompt["route"],
                "evidence_need": "primary_source",
                "expected_artifacts": ["source list", "supporting quotes"],
                "success_criteria": ["Claims remain tied to source spans."],
                "report_section": "Primary Sources",
            },
            {
                "angle_id": "angle_002",
                "title": f"{subject} comparison criteria",
                "research_question": (
                    f"How do {subject} approaches differ in validation criteria?"
                ),
                "question_context": (
                    f"Compare public evidence for {subject} without using fixtures."
                ),
                "route": prompt["route"],
                "evidence_need": "comparative_analysis",
                "expected_artifacts": ["comparison matrix", "difference notes"],
                "success_criteria": ["Comparisons cite distinct source-backed criteria."],
                "report_section": "Comparison",
            },
            {
                "angle_id": "angle_003",
                "title": f"{subject} caveats and limits",
                "research_question": (
                    f"What caveats limit claims about {subject} release readiness?"
                ),
                "question_context": (
                    f"Identify caveats that affect the {subject} evidence record."
                ),
                "route": prompt["route"],
                "evidence_need": "risk_or_guardrail",
                "expected_artifacts": ["risk register", "guardrail checklist"],
                "success_criteria": ["Caveats are explicit and source-linked."],
                "report_section": "Caveats",
            },
        ]
        semantic_plan = {
            "schema_version": "codex-deepresearch.semantic-planner.v0",
            "question_class": "general",
            "broad_question": False,
            "source": metadata["source"],
            "expected_evidence_needs": [
                "primary_source",
                "comparative_analysis",
                "risk_or_guardrail",
            ],
            "planner_mode": metadata["planner_mode"],
            "semantic_release_eligible": metadata["semantic_release_eligible"],
            "status": metadata["summary"]["status"],
            "diagnostics": dict(metadata["diagnostics"]),
            "angles": angles,
            "bounded_tasks": bounded_tasks,
        }
        question_scope = {
            "original_question": prompt["prompt"],
            "question_hash": self.prompt_hash(prompt["prompt"]),
            "question_class": "general",
            "planner_mode": metadata["planner_mode"],
            "angle_count": len(angles),
        }
        uses_template = metadata["planner_mode"] != "codex_semantic"
        template_use = {
            "uses_preselected_template": uses_template,
            "template_source": metadata["source"] if uses_template else None,
            "template_release_eligible": False,
            "template_angle_titles": [
                angle["title"] for angle in angles
            ] if uses_template else [],
        }
        session_id_unavailable_reason = (
            "Public beta test fixture records deterministic semantic artifacts "
            "without a live Codex session id."
        )
        manifest_oracle_path = str(prompt.get("oracle_path") or "").strip()
        manifest_oracle_hash = str(prompt.get("oracle_hash") or "").strip()
        manifest_oracle_fragment_id = (
            manifest_oracle_path.split("#", 1)[1].strip()
            if "#" in manifest_oracle_path
            else ""
        )
        manifest_oracle_binding = (
            {
                "manifest_oracle_hash": manifest_oracle_hash,
                "manifest_oracle_path": manifest_oracle_path,
                "manifest_oracle_fragment_id": manifest_oracle_fragment_id,
            }
            if manifest_oracle_hash
            else {}
        )
        provenance = {
            "planner_mode": metadata["planner_mode"],
            "planner_source": metadata["source"],
            "raw_request_required": True,
            "raw_response_required": True,
            "session_id": None,
            "session_id_unavailable_reason": session_id_unavailable_reason,
            "semantic_release_eligible": metadata["semantic_release_eligible"],
        }
        oracle_raw_dir = run_dir / "semantic_oracle_raw"
        planner_raw_dir = run_dir / "semantic_raw"
        reviewer_raw_dir = run_dir / "semantic_reviewer_raw"
        oracle_raw_request_path = oracle_raw_dir / "oracle_request.json"
        oracle_raw_response_path = oracle_raw_dir / "oracle_response.json"
        planner_raw_request_path = planner_raw_dir / "semantic_request.json"
        planner_raw_response_path = planner_raw_dir / "semantic_response.json"
        reviewer_raw_request_path = reviewer_raw_dir / "reviewer_request.json"
        reviewer_raw_response_path = reviewer_raw_dir / "reviewer_response.json"
        oracle_session_id = f"{run_dir.name}-oracle-session"
        planner_session_id = f"{run_dir.name}-planner-session"
        reviewer_session_id = f"{run_dir.name}-reviewer-session"

        def payload_hash(payload: dict[str, Any]) -> str:
            return hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
            ).hexdigest()

        def write_raw_request(
            path: Path,
            payload: dict[str, Any],
        ) -> tuple[str, str]:
            content_hash = payload_hash(payload)
            payload = {
                **payload,
                "raw_request_content_hash": content_hash,
                "raw_request_hash": content_hash,
            }
            self.write_json(path, payload)
            artifact_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            return content_hash, artifact_hash

        oracle_requirements = [
            {
                "requirement_id": f"req_{index:03d}",
                "description": f"Resolve {angle['title']} for the prompt.",
                "covered_by_angle_ids": [angle["angle_id"]],
                "non_negotiable": True,
            }
            for index, angle in enumerate(angles, start=1)
        ]
        expected_modalities = (
            ["text", "visual"] if prompt["route"] == "visual_required" else ["text"]
        )
        oracle_contract = {
            "oracle_requirement_map": oracle_requirements,
            "question_scope": question_scope,
            "bounded_task_range": {"min_tasks": 3, "max_tasks": 6},
            "expected_entities": prompt_terms[:6] or [subject],
            "expected_constraints": [
                "Use public-safe evidence.",
                "Cite source-backed claims.",
            ],
            "expected_modalities": expected_modalities,
            "required_angles": [angle["title"] for angle in angles],
            "forbidden_angles": [
                "generic web search only",
                "Codex implementation internals",
            ],
            "forbidden_internal_implementation_terms": [
                "hidden template",
                "keyword fallback",
                "fixed angle inventory",
            ],
            "expected_report_shape": [
                "source-grounded findings",
                "comparison criteria",
                "caveats and limits",
            ],
            "language": "en",
        }
        oracle_raw_request_content_hash, oracle_raw_request_artifact_hash = write_raw_request(
            oracle_raw_request_path,
            {
                "schema_version": "codex-deepresearch.semantic-oracle.v0",
                "artifact_type": "semantic_oracle_raw_request",
                "run_id": run_dir.name,
                "created_at": timestamp,
                "question": prompt["prompt"],
                "question_scope": question_scope,
                **manifest_oracle_binding,
                "provided_sources": [],
                "provided_images": [],
                "oracle_instructions": [
                    "Create expectations from the raw user question only.",
                    "Do not inspect the planner output.",
                ],
            },
        )
        self.write_json(
            oracle_raw_response_path,
            {
                "schema_version": "codex-deepresearch.semantic-oracle.v0",
                "artifact_type": "semantic_oracle_raw_response",
                "run_id": run_dir.name,
                "created_at": timestamp,
                "raw_request_content_hash": oracle_raw_request_content_hash,
                "raw_request_artifact_hash": oracle_raw_request_artifact_hash,
                "raw_request_hash": oracle_raw_request_content_hash,
                **manifest_oracle_binding,
                "oracle": oracle_contract,
                "provenance": {
                    "adapter_kind": "codex_semantic_oracle",
                    "child_session_id": oracle_session_id,
                    "raw_request_content_hash": oracle_raw_request_content_hash,
                    "raw_request_artifact_hash": oracle_raw_request_artifact_hash,
                    "raw_request_hash": oracle_raw_request_content_hash,
                    **manifest_oracle_binding,
                    "independent_of_planner": True,
                    "non_release_fixture": False,
                },
            },
        )
        oracle_raw_response_hash = hashlib.sha256(
            oracle_raw_response_path.read_bytes()
        ).hexdigest()
        planner_raw_request_content_hash, planner_raw_request_artifact_hash = write_raw_request(
            planner_raw_request_path,
            {
                "schema_version": "codex-deepresearch.semantic-planner.v0",
                "artifact_type": "semantic_planner_raw_request",
                "run_id": run_dir.name,
                "created_at": timestamp,
                "planner_mode": metadata["planner_mode"],
                "semantic_release_eligible": metadata["semantic_release_eligible"],
                "question": prompt["prompt"],
                "question_scope": question_scope,
                "template_use": template_use,
                "provenance": provenance,
            },
        )
        self.write_json(
            planner_raw_response_path,
            {
                "schema_version": "codex-deepresearch.semantic-planner.v0",
                "artifact_type": "semantic_planner_raw_response",
                "run_id": run_dir.name,
                "created_at": timestamp,
                "raw_request_content_hash": planner_raw_request_content_hash,
                "raw_request_artifact_hash": planner_raw_request_artifact_hash,
                "raw_request_hash": planner_raw_request_content_hash,
                "planner_mode": metadata["planner_mode"],
                "semantic_release_eligible": metadata["semantic_release_eligible"],
                "semantic_plan": semantic_plan,
                "diagnostics": dict(metadata["diagnostics"]),
                "provenance": provenance,
            },
        )
        planner_raw_response_hash = hashlib.sha256(
            planner_raw_response_path.read_bytes()
        ).hexdigest()
        planner_provenance = {
            **provenance,
            "session_id": planner_session_id,
            "child_session_id": planner_session_id,
            "raw_request_content_hash": planner_raw_request_content_hash,
            "raw_request_artifact_hash": planner_raw_request_artifact_hash,
            "raw_request_hash": planner_raw_request_artifact_hash,
            "raw_response_artifact_hash": planner_raw_response_hash,
            "raw_response_hash": planner_raw_response_hash,
        }
        oracle_provenance = {
            **provenance,
            "adapter_kind": "codex_semantic_oracle",
            "oracle_source": "codex_semantic",
            "session_id": oracle_session_id,
            "child_session_id": oracle_session_id,
            "raw_request_path": str(oracle_raw_request_path),
            "raw_response_path": str(oracle_raw_response_path),
            "raw_request_content_hash": oracle_raw_request_content_hash,
            "raw_request_artifact_hash": oracle_raw_request_artifact_hash,
            "raw_request_hash": oracle_raw_request_artifact_hash,
            "raw_response_artifact_hash": oracle_raw_response_hash,
            "raw_response_hash": oracle_raw_response_hash,
            "plan_visible_to_oracle": False,
            "used_production_planner_output": False,
            "used_hidden_template_class": False,
            "used_fixed_angle_inventory": False,
            "independent_of_planner": True,
            "non_release_fixture": False,
            **manifest_oracle_binding,
        }

        def artifact_base(
            *,
            raw_request_path: Path,
            raw_response_path: Path,
            raw_request_hash: str,
            raw_response_hash: str,
            artifact_provenance: dict[str, Any],
        ) -> dict[str, Any]:
            return {
                "schema_version": "codex-deepresearch.semantic-planner.v0",
                "run_id": run_dir.name,
                "prompt_id": prompt["id"],
                "prompt_hash": self.prompt_hash(prompt["prompt"]),
                "suite_id": suite_id,
                "original_question": prompt["prompt"],
                "execution_mode": "codex-plugin",
                "runner_mode": "full-runner",
                "created_at": timestamp,
                "planner_mode": metadata["planner_mode"],
                "semantic_release_eligible": metadata["semantic_release_eligible"],
                "question_scope": question_scope,
                "raw_request_path": str(raw_request_path),
                "raw_response_path": str(raw_response_path),
                "raw_request_content_hash": artifact_provenance.get(
                    "raw_request_content_hash"
                ),
                "raw_request_artifact_hash": artifact_provenance.get(
                    "raw_request_artifact_hash",
                    raw_request_hash,
                ),
                "raw_request_hash": raw_request_hash,
                "raw_response_artifact_hash": artifact_provenance.get(
                    "raw_response_artifact_hash",
                    raw_response_hash,
                ),
                "raw_response_hash": raw_response_hash,
                "provenance": artifact_provenance,
                "template_use": template_use,
                "session_id": artifact_provenance.get("session_id"),
                "session_id_unavailable_reason": session_id_unavailable_reason,
            }

        oracle_base = artifact_base(
            raw_request_path=oracle_raw_request_path,
            raw_response_path=oracle_raw_response_path,
            raw_request_hash=oracle_raw_request_artifact_hash,
            raw_response_hash=oracle_raw_response_hash,
            artifact_provenance=oracle_provenance,
        )
        planner_base = artifact_base(
            raw_request_path=planner_raw_request_path,
            raw_response_path=planner_raw_response_path,
            raw_request_hash=planner_raw_request_artifact_hash,
            raw_response_hash=planner_raw_response_hash,
            artifact_provenance=planner_provenance,
        )
        oracle_payload = {
            **oracle_base,
            "artifact_type": "semantic_expectation_oracle",
            **oracle_contract,
            **manifest_oracle_binding,
            "oracle_provenance": oracle_provenance,
            "plan_visible_to_oracle": False,
            "used_production_planner_output": False,
            "used_hidden_template_class": False,
            "used_fixed_angle_inventory": False,
        }
        plan_payload = {
            **planner_base,
            "artifact_type": "semantic_plan",
            "semantic_plan": semantic_plan,
            "angles": angles,
            "requirement_coverage_map": [
                {
                    "requirement_id": f"req_{index:03d}",
                    "angle_id": angle["angle_id"],
                    "coverage_status": "covered",
                    "non_negotiable": True,
                }
                for index, angle in enumerate(angles, start=1)
            ],
        }
        self.write_json(run_dir / "semantic_expectation_oracle.json", oracle_payload)
        self.write_json(run_dir / "semantic_plan.json", plan_payload)
        oracle_hash = hashlib.sha256(
            (run_dir / "semantic_expectation_oracle.json").read_bytes()
        ).hexdigest()
        semantic_plan_hash = hashlib.sha256(
            (run_dir / "semantic_plan.json").read_bytes()
        ).hexdigest()
        self.stamp_semantic_materialization_lineage(
            run_dir,
            semantic_plan_hash=semantic_plan_hash,
        )
        reviewer_raw_request_content_hash, reviewer_raw_request_artifact_hash = write_raw_request(
            reviewer_raw_request_path,
            {
                "schema_version": "codex-deepresearch.semantic-reviewer.v0",
                "artifact_type": "semantic_reviewer_raw_request",
                "run_id": run_dir.name,
                "created_at": timestamp,
                "question": prompt["prompt"],
                "oracle_path": str(run_dir / "semantic_expectation_oracle.json"),
                "oracle_hash": oracle_hash,
                **manifest_oracle_binding,
                "semantic_plan_path": str(run_dir / "semantic_plan.json"),
                "semantic_plan_hash": semantic_plan_hash,
                "review_instructions": [
                    "Review the plan against the locked oracle.",
                    "Reject substitute implementation or subject drift.",
                ],
            },
        )
        self.write_json(
            reviewer_raw_response_path,
            {
                "schema_version": "codex-deepresearch.semantic-reviewer.v0",
                "artifact_type": "semantic_reviewer_raw_response",
                "run_id": run_dir.name,
                "created_at": timestamp,
                "raw_request_content_hash": reviewer_raw_request_content_hash,
                "raw_request_artifact_hash": reviewer_raw_request_artifact_hash,
                "raw_request_hash": reviewer_raw_request_content_hash,
                "semantic_fit_score": metadata["semantic_fit_score"],
                "blockers": list(metadata["blockers"]),
                "warnings": [],
                "verdict": metadata["final_verdict"],
                "provenance": {
                    "adapter_kind": "codex_semantic_reviewer",
                    "child_session_id": reviewer_session_id,
                    "raw_request_content_hash": reviewer_raw_request_content_hash,
                    "raw_request_artifact_hash": reviewer_raw_request_artifact_hash,
                    "raw_request_hash": reviewer_raw_request_content_hash,
                    "independent_of_oracle": True,
                    "independent_of_planner": True,
                    "non_release_fixture": False,
                },
            },
        )
        reviewer_raw_response_hash = hashlib.sha256(
            reviewer_raw_response_path.read_bytes()
        ).hexdigest()
        reviewer_provenance = {
            **provenance,
            "adapter_kind": "codex_semantic_reviewer",
            "reviewer_source": "codex_semantic",
            "session_id": reviewer_session_id,
            "child_session_id": reviewer_session_id,
            "raw_request_path": str(reviewer_raw_request_path),
            "raw_response_path": str(reviewer_raw_response_path),
            "raw_request_content_hash": reviewer_raw_request_content_hash,
            "raw_request_artifact_hash": reviewer_raw_request_artifact_hash,
            "raw_request_hash": reviewer_raw_request_artifact_hash,
            "raw_response_artifact_hash": reviewer_raw_response_hash,
            "raw_response_hash": reviewer_raw_response_hash,
            "oracle_hash": oracle_hash,
            "semantic_plan_hash": semantic_plan_hash,
            "independent_of_oracle": True,
            "independent_of_planner": True,
            "non_release_fixture": False,
        }
        reviewer_base = artifact_base(
            raw_request_path=reviewer_raw_request_path,
            raw_response_path=reviewer_raw_response_path,
            raw_request_hash=reviewer_raw_request_artifact_hash,
            raw_response_hash=reviewer_raw_response_hash,
            artifact_provenance=reviewer_provenance,
        )
        review_payload = {
            **reviewer_base,
            "artifact_type": "semantic_plan_review",
            "semantic_fit_score": metadata["semantic_fit_score"],
            "blockers": list(metadata["blockers"]),
            "warnings": [],
            "substitute_implementation_check": {
                "passed": metadata["semantic_release_eligible"],
                "checked": True,
            },
            "oracle_hash": oracle_hash,
            "semantic_expectation_oracle_hash": oracle_hash,
            "semantic_plan_hash": semantic_plan_hash,
            **manifest_oracle_binding,
            "reviewer_raw_request_path": str(reviewer_raw_request_path),
            "reviewer_raw_response_path": str(reviewer_raw_response_path),
            "reviewer_raw_request_content_hash": reviewer_raw_request_content_hash,
            "reviewer_raw_request_artifact_hash": reviewer_raw_request_artifact_hash,
            "reviewer_raw_request_hash": reviewer_raw_request_artifact_hash,
            "reviewer_raw_response_artifact_hash": reviewer_raw_response_hash,
            "reviewer_raw_response_hash": reviewer_raw_response_hash,
            "reviewer_provenance": reviewer_provenance,
            "verdict": metadata["final_verdict"],
            "final_verdict": metadata["final_verdict"],
            "non_negotiable_coverage_complete": metadata["semantic_release_eligible"],
            "reviewer_independence": {
                "independent": metadata["semantic_release_eligible"],
                "status": (
                    "passed"
                    if metadata["semantic_release_eligible"]
                    else "release_ineligible"
                ),
                "oracle_child_session_id": oracle_session_id,
                "planner_child_session_id": planner_session_id,
                "reviewer_child_session_id": reviewer_session_id,
            },
            "prd_score_dimensions": {
                "intent_preservation": 9.6,
                "required_entities_constraints": 9.4,
                "angle_relevance_diversity": 9.3,
                "modality_visual_routing": 9.2,
                "forbidden_drift_avoidance": 9.5,
                "executable_bounded_tasks": 9.4,
            },
        }
        self.write_json(run_dir / "semantic_plan_review.json", review_payload)
        review_hash = hashlib.sha256(
            (run_dir / "semantic_plan_review.json").read_bytes()
        ).hexdigest()
        validation_payload = {
            **planner_base,
            "artifact_type": "semantic_planner_validation",
            "fixture_id": run_dir.name,
            "question_class": "general",
            "broad_question": False,
            "angle_count": len(angles),
            "task_count": len(angles),
            "failures": list(metadata["failures"]),
            "ok": not metadata["failures"],
            "semantic_review": {
                "semantic_fit_score": metadata["semantic_fit_score"],
                "verdict": metadata["final_verdict"],
                "blocker_count": len(metadata["blockers"]),
                "oracle_hash": oracle_hash,
                "review_hash": review_hash,
            },
        }
        self.write_json(run_dir / "semantic_planner_validation.json", validation_payload)
        validation_hash = hashlib.sha256(
            (run_dir / "semantic_planner_validation.json").read_bytes()
        ).hexdigest()

        required_order = [
            "semantic_oracle_request_created",
            "semantic_oracle_locked",
            "semantic_planner_request_created",
            "semantic_plan_created",
            "semantic_reviewer_request_created",
            "semantic_review_completed",
        ]

        def semantic_trace_timestamp(event_index: int) -> str:
            raw = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
            base = datetime.fromisoformat(raw)
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
            return (
                base.astimezone(timezone.utc)
                + timedelta(milliseconds=event_index)
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z")

        def trace_record(
            event_type: str,
            *,
            event_index: int,
            artifact_hashes: dict[str, str],
            semantic_artifact_paths: dict[str, str],
        ) -> dict[str, Any]:
            return {
                "schema_version": "codex-deepresearch.run-trace.v0",
                "run_id": run_dir.name,
                "event_id": f"{run_dir.name}-{event_index:03d}-{event_type}",
                "event_type": event_type,
                "timestamp": semantic_trace_timestamp(event_index),
                "stage": "planning",
                "agent_role": "semantic_gate",
                "status": event_type,
                "prompt_summary": prompt["prompt"][:200],
                "output_preview": event_type,
                "artifacts": {"run_trace": str(run_dir / "run_trace.jsonl")},
                "semantic_event_index": event_index,
                "artifact_hashes": artifact_hashes,
                "semantic_artifact_paths": semantic_artifact_paths,
                "order_validation": {
                    "required_order": required_order,
                    "current_event": event_type,
                    "semantic_event_index": event_index,
                },
            }

        self.write_jsonl(
            run_dir / "run_trace.jsonl",
            [
                trace_record(
                    "semantic_oracle_request_created",
                    event_index=1,
                    artifact_hashes={
                        "semantic_oracle_raw_request": oracle_raw_request_artifact_hash,
                    },
                    semantic_artifact_paths={
                        "semantic_oracle_raw_request": str(oracle_raw_request_path),
                    },
                ),
                trace_record(
                    "semantic_oracle_locked",
                    event_index=2,
                    artifact_hashes={
                        "semantic_expectation_oracle": oracle_hash,
                        "semantic_oracle_raw_response": oracle_raw_response_hash,
                    },
                    semantic_artifact_paths={
                        "semantic_expectation_oracle": str(
                            run_dir / "semantic_expectation_oracle.json"
                        ),
                        "semantic_oracle_raw_response": str(oracle_raw_response_path),
                    },
                ),
                trace_record(
                    "semantic_planner_request_created",
                    event_index=3,
                    artifact_hashes={
                        "semantic_planner_raw_request": planner_raw_request_artifact_hash,
                    },
                    semantic_artifact_paths={
                        "semantic_planner_raw_request": str(planner_raw_request_path),
                    },
                ),
                trace_record(
                    "semantic_plan_created",
                    event_index=4,
                    artifact_hashes={
                        "semantic_plan": semantic_plan_hash,
                        "semantic_planner_raw_response": planner_raw_response_hash,
                    },
                    semantic_artifact_paths={
                        "semantic_plan": str(run_dir / "semantic_plan.json"),
                        "semantic_planner_raw_response": str(planner_raw_response_path),
                    },
                ),
                trace_record(
                    "semantic_reviewer_request_created",
                    event_index=5,
                    artifact_hashes={
                        "semantic_reviewer_raw_request": reviewer_raw_request_artifact_hash,
                    },
                    semantic_artifact_paths={
                        "semantic_reviewer_raw_request": str(reviewer_raw_request_path),
                    },
                ),
                trace_record(
                    "semantic_review_completed",
                    event_index=6,
                    artifact_hashes={
                        "semantic_plan_review": review_hash,
                        "semantic_reviewer_raw_response": reviewer_raw_response_hash,
                        "semantic_planner_validation": validation_hash,
                    },
                    semantic_artifact_paths={
                        "semantic_plan_review": str(
                            run_dir / "semantic_plan_review.json"
                        ),
                        "semantic_reviewer_raw_response": str(
                            reviewer_raw_response_path
                        ),
                        "semantic_planner_validation": str(
                            run_dir / "semantic_planner_validation.json"
                        ),
                    },
                ),
            ],
        )
        return

    def write_visual_run(
        self,
        run_dir: Path,
        *,
        prompt: dict[str, Any] | None = None,
        suite_id: str = "public-beta-validation",
        run_status: str,
        provider_status: str,
        release_grade: bool = True,
        candidate_count: int = 10,
        analyzed_image_count: int = 3,
    ) -> Path:
        run_dir.mkdir(parents=True)
        prompt = prompt or next(
            prompt
            for prompt in load_public_beta_prompt_manifest(DEFAULT_PUBLIC_BETA_PROMPT_MANIFEST)["prompts"]
            if prompt["route"] == "visual_required"
        )
        timestamp = self.now()
        semantic_metadata = self.semantic_planning_metadata(
            "eligible",
            enabled=run_status == "completed_auto_visual",
        )
        bounded_tasks = self.semantic_bounded_tasks(prompt)
        search_tasks = [self.search_task_from_bounded_task(task) for task in bounded_tasks]
        research_tasks = [self.research_task_from_bounded_task(task) for task in bounded_tasks]
        run_status_payload = {
            "schema_version": "codex-deepresearch.run-status.v0",
            "run_id": run_dir.name,
            "prompt_id": prompt["id"],
            "prompt_hash": self.prompt_hash(prompt["prompt"]),
            "suite_id": suite_id,
            "original_question": prompt["prompt"],
            "execution_mode": "codex-plugin",
            "runner_mode": "full-runner",
            "question": prompt["prompt"],
            "status": run_status,
            "ok": run_status == "completed_auto_visual",
            "terminal": True,
            "created_at": timestamp,
            "completed_at": timestamp,
            "selected_mode": "full-runner",
            "search_provider": "codex-native",
            "vlm_provider": "codex-interactive",
        }
        if semantic_metadata:
            run_status_payload.update(
                {
                    "planner_mode": semantic_metadata["planner_mode"],
                    "semantic_release_eligible": semantic_metadata[
                        "semantic_release_eligible"
                    ],
                    "semantic_planning": semantic_metadata["summary"],
                }
            )
        self.write_json(
            run_dir / "run_status.json",
            run_status_payload,
        )
        self.write_json(
            run_dir / "search_tasks.json",
            {
                "schema_version": "codex-deepresearch.search-handoff.v0",
                "run_id": run_dir.name,
                "prompt_id": prompt["id"],
                "prompt_hash": self.prompt_hash(prompt["prompt"]),
                "suite_id": suite_id,
                "original_question": prompt["prompt"],
                "execution_mode": "codex-plugin",
                "runner_mode": "full-runner",
                "created_at": timestamp,
                "tasks": search_tasks,
            },
        )
        self.write_json(
            run_dir / "research_tasks.json",
            {
                "schema_version": "codex-deepresearch.parallel-orchestration.v0",
                "run_id": run_dir.name,
                "created_at": timestamp,
                "status": "completed",
                "tasks": research_tasks,
            },
        )
        self.write_json(
            run_dir / "visual_tasks.json",
            {
                "schema_version": "codex-deepresearch.search-handoff.v0",
                "run_id": run_dir.name,
                "created_at": timestamp,
                "tasks": [
                    self.visual_task_from_bounded_task(task, index=index)
                    for index, task in enumerate(bounded_tasks, start=1)
                    if task["route"] != "text_only"
                ],
            },
        )
        self.write_jsonl(
            run_dir / "search_results.jsonl",
            [
                self.codex_native_search_result(
                    prompt,
                    suite_id=suite_id,
                    task=task,
                    rank=index,
                )
                for index, task in enumerate(bounded_tasks, start=1)
            ],
        )
        self.write_jsonl(
            run_dir / "subagent_assignments.jsonl",
            [
                self.subagent_assignment_record(task, index=index)
                for index, task in enumerate(bounded_tasks, start=1)
            ],
        )
        candidates = [
            self.visual_candidate_record(
                index=index,
                analyzed=release_grade and index <= analyzed_image_count,
                task=bounded_tasks[(index - 1) % len(bounded_tasks)],
            )
            for index in range(1, candidate_count + 1)
        ] if release_grade else []
        fetches = []
        observations = []
        image_ids = []
        images = []
        for index, candidate in enumerate(candidates[:analyzed_image_count], start=1):
            image_id = f"img_real_{index:03d}"
            image_ids.append(image_id)
            fetch = self.visual_fetch_record(candidate, image_id=image_id, index=index)
            fetches.append(fetch)
            observations.append(
                self.visual_observation_record(
                    candidate,
                    fetch,
                    image_id=image_id,
                    index=index,
                )
            )
            images.append(
                {
                    "id": image_id,
                    "candidate_id": candidate["candidate_id"],
                    "fetch_id": fetch["fetch_id"],
                    "task_id": candidate["task_id"],
                    "semantic_plan_task_id": candidate["task_id"],
                    "angle_id": candidate["angle_id"],
                    "route": candidate["route"],
                    "plan_id": candidate["plan_id"],
                    "local_artifact_path": f"images/{image_id}.png",
                    "provider": candidate["provider"],
                    "provider_kind": candidate["provider_kind"],
                    "provider_mode": "real",
                    "provider_provenance": candidate["provider_provenance"],
                    "policy_decision": "allowed",
                    "approved_delta_id": "base_plan",
                }
            )
        cited_image_id = image_ids[0] if image_ids else "img_001"
        visual_support = {
            "image_id": cited_image_id,
            "evidence_image_id": cited_image_id,
            "observation_ref": f"images.{cited_image_id}.observations[0]",
            "plan_id": candidates[0]["plan_id"] if candidates else "plan_visual_001",
            "task_id": candidates[0]["task_id"] if candidates else "task_visual_001",
            "angle_id": candidates[0]["angle_id"] if candidates else "angle_001",
            "route": candidates[0]["route"] if candidates else "visual_required",
            "candidate_id": candidates[0]["candidate_id"] if candidates else "cand_001",
            "fetch_id": fetches[0]["fetch_id"] if fetches else "fetch_001",
        }
        evidence_payload = {
                "schema_version": "0.1.0",
                "run_id": run_dir.name,
                "prompt_id": prompt["id"],
                "prompt_hash": self.prompt_hash(prompt["prompt"]),
                "suite_id": suite_id,
                "original_question": prompt["prompt"],
                "execution_mode": "codex-plugin",
                "runner_mode": "full-runner",
                "question": prompt["prompt"],
                "created_at": timestamp,
                "mode": "codex-plugin",
                "search_provider": "codex-native",
                "vlm_provider": "codex-interactive",
                "images": images if release_grade else [],
                "claims": [
                    {
                        "id": "claim_visual_001",
                        "text": "The real visual provider image supports the claim.",
                        "claim_type": "visual",
                        "supporting_sources": [],
                        "supporting_images": [cited_image_id],
                        "visual_supports": [visual_support],
                        "verification_status": "supported",
                        "confidence": "high",
                    }
                ]
                if release_grade
                else [],
            }
        if semantic_metadata:
            evidence_payload["semantic_planner"] = {
                "schema_version": "codex-deepresearch.semantic-planner.v0",
                "question_class": "visual_style",
                "broad_question": True,
                "source": semantic_metadata["source"],
                "expected_evidence_needs": ["visual_observation"],
                "planner_mode": semantic_metadata["planner_mode"],
                "semantic_release_eligible": semantic_metadata[
                    "semantic_release_eligible"
                ],
                "status": semantic_metadata["summary"]["status"],
                "diagnostics": dict(semantic_metadata["diagnostics"]),
                "bounded_tasks": bounded_tasks,
            }
            evidence_payload["search_tasks"] = search_tasks
        self.write_json(run_dir / "evidence.json", evidence_payload)
        if semantic_metadata:
            self.write_semantic_release_artifacts(
                run_dir,
                prompt=prompt,
                suite_id=suite_id,
                timestamp=timestamp,
                metadata=semantic_metadata,
                bounded_tasks=bounded_tasks,
            )
        self.write_json(
            run_dir / "report_status.json",
            {
                "schema_version": "codex-deepresearch.report-generation.v0",
                "run_id": run_dir.name,
                "prompt_id": prompt["id"],
                "prompt_hash": self.prompt_hash(prompt["prompt"]),
                "suite_id": suite_id,
                "original_question": prompt["prompt"],
                "execution_mode": "codex-plugin",
                "runner_mode": "full-runner",
                "status": "completed",
                "created_at": timestamp,
                "generated_at": timestamp,
                "used_images": [cited_image_id] if release_grade else ["img_001"],
                "included_claims": [
                    {
                        "claim_id": "claim_visual_001",
                        "claim_type": "visual",
                        "verification_status": "supported",
                        "image_ids": [cited_image_id],
                        "visual_supports": [visual_support],
                    }
                ]
                if release_grade
                else [],
            },
        )
        minimums = visual_release_minimums(
            candidates=candidates,
            fetches=fetches,
            observations=observations,
            evidence={
                "routing": [{"id": "angle_001", "modality": "visual_required"}],
                "images": images,
                "claims": [
                    {
                        "id": "claim_visual_001",
                        "claim_type": "visual",
                        "supporting_images": [cited_image_id],
                        "verification_status": "supported",
                    }
                ]
                if release_grade
                else [],
            },
            report_status={
                "used_images": [cited_image_id] if release_grade else [],
                "included_claims": [
                    {
                        "claim_id": "claim_visual_001",
                        "image_ids": [cited_image_id],
                        "visual_supports": [visual_support],
                    }
                ]
                if release_grade
                else [],
            },
            report_text=(
                f"Claim `claim_visual_001` is supported by Image `{cited_image_id}`.\n"
                if release_grade
                else ""
            ),
        )
        self.write_json(
            run_dir / "visual_provider_status.json",
            {
                "schema_version": "codex-deepresearch.visual-provider-status.v0",
                "run_id": run_dir.name,
                "prompt_id": prompt["id"],
                "prompt_hash": self.prompt_hash(prompt["prompt"]),
                "suite_id": suite_id,
                "original_question": prompt["prompt"],
                "execution_mode": "codex-plugin",
                "runner_mode": "full-runner",
                "status": provider_status,
                "ok": provider_status == "completed_auto_visual",
                "terminal": True,
                "created_at": timestamp,
                "minimums": minimums,
                "providers": [
                    {
                        "provider": "codex-native",
                        "provider_kind": "web_image_search",
                        "provider_mode": "real",
                        "codex_native_handoff": True,
                        "handoff_artifact": "visual_candidates.jsonl",
                        "configured": provider_status == "completed_auto_visual",
                        "available": provider_status == "completed_auto_visual",
                        "invocations": 1 if provider_status == "completed_auto_visual" else 0,
                        "candidates_discovered": len(candidates) if release_grade else 0,
                        "artifacts_fetched": len(fetches) if release_grade else 0,
                        "vlm_images_analyzed": 0,
                    },
                    {
                        "provider": "codex-interactive",
                        "provider_kind": "vlm",
                        "provider_mode": "real",
                        "codex_interactive_handoff": True,
                        "handoff_artifact": "visual_observations.jsonl",
                        "configured": provider_status == "completed_auto_visual",
                        "available": provider_status == "completed_auto_visual",
                        "invocations": 1 if provider_status == "completed_auto_visual" else 0,
                        "candidates_discovered": 0,
                        "artifacts_fetched": len(fetches) if release_grade else 0,
                        "vlm_images_analyzed": len(observations) if release_grade else 0,
                    },
                ],
            },
        )
        self.write_json(
            run_dir / "visual_search_plan.json",
            {
                "schema_version": "codex-deepresearch.visual-artifacts.v0",
                "run_id": run_dir.name,
                "prompt_id": prompt["id"],
                "prompt_hash": self.prompt_hash(prompt["prompt"]),
                "suite_id": suite_id,
                "original_question": prompt["prompt"],
                "execution_mode": "codex-plugin",
                "runner_mode": "full-runner",
                "created_at": timestamp,
                "tasks": [
                    self.visual_task_from_bounded_task(task, index=index)
                    for index, task in enumerate(bounded_tasks, start=1)
                    if task["route"] != "text_only"
                ],
            },
        )
        self.write_jsonl(
            run_dir / "visual_candidates.jsonl",
            candidates if release_grade else [{}],
        )
        self.write_jsonl(run_dir / "image_fetch_status.jsonl", fetches if release_grade else [{}])
        self.write_jsonl(
            run_dir / "visual_observations.jsonl",
            observations if release_grade else [{}],
        )
        self.write_jsonl(
            run_dir / "verifier_votes.jsonl",
            [{"id": f"vote_visual_{index:03d}"} for index in range(1, len(observations) + 1)]
            or [{"id": "vote_visual_001"}],
        )
        report = (
            f"Claim `claim_visual_001` is supported by Image `{cited_image_id}`.\n"
            if release_grade
            else "# Public-safe visual report\n"
        )
        (run_dir / "report.md").write_text(report, encoding="utf-8")
        if semantic_metadata:
            semantic_plan_hash = hashlib.sha256(
                (run_dir / "semantic_plan.json").read_bytes()
            ).hexdigest()
            self.stamp_semantic_materialization_lineage(
                run_dir,
                semantic_plan_hash=semantic_plan_hash,
            )
            write_semantic_materialization_diff(
                run_dir=run_dir,
                require_research_tasks=True,
                require_downstream=True,
                created_at=timestamp,
            )
        return run_dir

    def write_parallel_status(
        self,
        run_dir: Path,
        *,
        status: str,
        planned: int,
        accepted: int,
        failed: int = 0,
        blocked: int = 0,
        rejected: int = 0,
        discarded: int = 0,
    ) -> None:
        partial = status == "completed_partial_parallel" or accepted < planned or any(
            count > 0 for count in (failed, blocked, rejected, discarded)
        )
        reason = "none"
        if partial:
            if accepted == 0:
                reason = "no_accepted_shards"
            elif failed:
                reason = "failed_tasks"
            elif blocked:
                reason = "blocked_tasks"
            elif rejected:
                reason = "rejected_shards"
            elif discarded:
                reason = "discarded_tasks"
            else:
                reason = "omitted_tasks"
        failure_counts = {
            "failed_tasks": failed,
            "blocked_tasks": blocked,
            "rejected_shards": rejected,
            "discarded_tasks": discarded,
            "by_category": {"invalid_shard": failed} if failed else {},
        }
        summary = {
            "partial": partial,
            "reason_category": reason,
            "planned_task_count": planned,
            "accepted_shard_count": accepted,
            "omitted_task_count": max(0, planned - accepted),
            "failed_task_count": failed,
            "blocked_task_count": blocked,
            "rejected_shard_count": rejected,
            "discarded_task_count": discarded,
            "retried_task_count": 0,
            "retry_exhausted_task_count": 0,
            "parallel_degraded": False,
            "failure_category_counts": failure_counts["by_category"],
        }
        payload = {
            "schema_version": "codex-deepresearch.parallel-orchestration.v0",
            "run_id": run_dir.name,
            "status": status,
            "ok": True,
            "parallel_degraded": False,
            "adapter": "codex-exec",
            "planned_task_count": planned,
            "runnable_task_count": planned,
            "accepted_shard_count": accepted,
            "failure_counts": failure_counts,
            "retry_summary": {"retry_count": 0, "retry_exhausted_count": 0},
            "partial_parallel_summary": summary,
            "partial_reason_category": reason,
            "evidence_source": {
                "type": "real_child_execution",
                "adapter": "codex-exec",
                "accepted_shards": accepted,
                "real_child_execution": True,
                "fixture_only": False,
                "manual_handoff": False,
                "attempted_real_child_execution": True,
            },
        }
        self.write_json(run_dir / "parallel_orchestration_status.json", payload)
        self.write_json(
            run_dir / "merge_status.json",
            {
                **payload,
                "status": "completed",
                "accepted_shards": [
                    {"task_id": f"task_research_{index:03d}"}
                    for index in range(1, accepted + 1)
                ],
            },
        )

    def write_all_passing_prompt_runs(
        self,
        manifest: dict[str, Any],
        *,
        runs_dir: Path,
        suite_id: str,
    ) -> dict[str, Path]:
        prompt_runs = {}
        for prompt in manifest["prompts"]:
            if prompt["route"] in {"visual_required", "visual_optional"}:
                prompt_runs[prompt["id"]] = self.write_visual_run(
                    runs_dir / prompt["id"],
                    prompt=prompt,
                    suite_id=suite_id,
                    run_status="completed_auto_visual",
                    provider_status="completed_auto_visual",
                )
            else:
                prompt_runs[prompt["id"]] = self.write_text_run(
                    runs_dir / prompt["id"],
                    prompt=prompt,
                    suite_id=suite_id,
                    status="completed_parallel",
                )
        return prompt_runs

    def passing_external_gate_payload(self, gate_id: str) -> dict[str, Any]:
        payload = self.minimal_spoofed_gate_payload(gate_id)
        payload["release_gate_ready"] = True
        if gate_id == "fresh_session_full_runner_artifact_handoff":
            payload["acceptance"] = {
                key: True for key in _FRESH_SESSION_REQUIRED_ACCEPTANCE
            }
            payload["skill_transcript_gate"] = {
                "status": "passed",
                "route_command": "$deep-research: public beta text fixture",
            }
            payload["runner_artifact_gate"] = {"status": "passed"}
            payload["scenarios"] = [
                {
                    "id": "completed-real-parallel",
                    "status": "passed",
                    "terminal_outcome": "completed_real_parallel",
                    "provenance_class": "real_parallel",
                    "validation": {
                        "status": "passed",
                        "required_artifacts": ["run_status"],
                    },
                    "artifacts": {"run_status": "run_status.json"},
                }
            ]
        elif gate_id == "codex_plugin_interactive_visual_e2e":
            payload["release_gate_status"] = "passed"
            payload["acceptance"] = {
                key: True for key in _FRESH_SESSION_VISUAL_REQUIRED_ACCEPTANCE
            }
            payload["skill_transcript_gate"] = {"status": "passed"}
            payload["scenarios"] = [
                {
                    "id": "visual-release",
                    "visual_release_gate": {
                        "schema_version": "codex-deepresearch.fresh-session-visual-e2e.v0",
                        "release_gate_passed": True,
                        "status": "completed_auto_visual",
                        "codex_interactive_analyzed_images": 3,
                        "report_cited_visual_or_mixed_claims": 1,
                        "visual_artifact_validation": {"valid": True},
                        "checks": {
                            "codex_native_visual_acquisition_evidence": True,
                            "codex_interactive_vlm_handoff_observations": True,
                            "report_cited_visual_or_mixed_claim": True,
                        },
                        "required_response_artifacts": [
                            "run_status",
                            "evidence",
                            "visual_tasks",
                            "visual_observations",
                            "visual_provider_status",
                            "report",
                            "report_status",
                            "visual_candidates",
                            "image_fetch_status",
                        ],
                    },
                }
            ]
        elif gate_id in {
            "automated_cli_real_provider_visual_e2e",
            "automatic_web_visual_e2e",
        }:
            payload["acceptance"] = {
                key: True for key in _AUTOMATED_VISUAL_REQUIRED_ACCEPTANCE
            }
            payload["external_network_call"] = True
            payload["external_vlm_call"] = True
            payload["blockers"] = []
            payload["scenario_prompts"] = [
                {"id": scenario_id}
                for scenario_id in sorted(_AUTOMATED_VISUAL_REQUIRED_SCENARIOS)
            ]
            payload["scenarios"] = [
                {
                    "id": scenario_id,
                    "status": "passed",
                    "run_status": "completed_auto_visual",
                    "visual_provider_status": "completed_auto_visual",
                    "ok": True,
                    "terminal": True,
                    "external_network_call": True,
                    "external_vlm_call": True,
                    "visual_artifact_validation": {"valid": True},
                    "artifacts": {
                        name: f"{name}.json"
                        for name in sorted(_AUTOMATED_VISUAL_REQUIRED_ARTIFACTS)
                    },
                    "counts": {
                        "scenario_real_candidates": 10,
                        "real_openai_responses_vision_observations": 3,
                        "report_cited_visual_or_mixed_claims": 1,
                    },
                    "release_numerator_counts": {
                        "real_vlm_images_analyzed": 3,
                        "report_cited_visual_or_mixed_claims": 1,
                    },
                }
                for scenario_id in sorted(_AUTOMATED_VISUAL_REQUIRED_SCENARIOS)
            ]
        return payload

    def minimal_spoofed_gate_payload(self, gate_id: str) -> dict[str, Any]:
        requirements = EXTERNAL_GATE_REQUIREMENTS[gate_id]
        outcome_counts: dict[str, int] = {}
        for count_name, minimum in requirements.get("min_counts", {}).items():
            outcome_counts[count_name] = int(minimum)
        for count_name in requirements.get("zero_counts", set()):
            outcome_counts[count_name] = 0
        thresholds = {
            threshold_name: int(minimum)
            for threshold_name, minimum in requirements.get("thresholds", {}).items()
        }
        payload: dict[str, Any] = {
            "schema_version": next(iter(requirements["schemas"])),
            "status": "passed",
            "public_safe": True,
            "release_gate_passed": True,
            "suite_id": "spoofed-suite",
            "generated_at": self.now(),
            "outcome_counts": outcome_counts,
            "artifacts": {"results": "spoofed-results.json"},
            "failures": [],
        }
        if thresholds:
            payload["thresholds"] = thresholds
        return payload

    def write_jsonl(self, path: Path, records: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

    def visual_candidate_record(
        self,
        *,
        index: int = 1,
        analyzed: bool = True,
        task: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = task["task_id"] if task else "task_visual_001"
        angle_id = task["angle_id"] if task else "angle_001"
        route = task["route"] if task else "visual_required"
        return {
            "candidate_id": f"cand_real_{index:03d}",
            "plan_id": f"plan_{task_id}_{angle_id}_{route}",
            "task_id": task_id,
            "semantic_plan_task_id": task_id,
            "angle_id": angle_id,
            "route": route,
            "provider": "codex-native",
            "provider_kind": "web_image_search",
            "provider_mode": "real",
            "provider_run_id": "run_real_001",
            "search_provider": "codex-native",
            "codex_native_handoff": True,
            "handoff_artifact": "visual_candidates.jsonl",
            "provider_provenance": {
                "provider": "codex-native",
                "provider_kind": "web_image_search",
                "provider_mode": "real",
                "search_provider": "codex-native",
                "codex_native_handoff": True,
                "handoff_artifact": "visual_candidates.jsonl",
                "external_network_call": False,
            },
            "origin": "image_search",
            "policy_decision": "allowed",
            "candidate_status": "analyzed" if analyzed else "selected",
            "approved_delta_id": "base_plan",
        }

    def visual_fetch_record(
        self,
        candidate: dict[str, Any],
        *,
        image_id: str,
        index: int = 1,
    ) -> dict[str, Any]:
        return {
            "fetch_id": f"fetch_real_{index:03d}",
            "candidate_id": candidate["candidate_id"],
            "plan_id": candidate["plan_id"],
            "task_id": candidate["task_id"],
            "angle_id": candidate["angle_id"],
            "route": candidate["route"],
            "provider": candidate["provider"],
            "provider_kind": candidate["provider_kind"],
            "provider_mode": "real",
            "provider_run_id": candidate["provider_run_id"],
            "search_provider": "codex-native",
            "codex_native_handoff": True,
            "handoff_artifact": "image_fetch_status.jsonl",
            "provider_provenance": candidate["provider_provenance"],
            "origin": candidate["origin"],
            "fetch_status": "fetched",
            "evidence_image_id": image_id,
            "local_artifact_path": f"images/{image_id}.png",
            "policy_decision": "allowed",
        }

    def visual_observation_record(
        self,
        candidate: dict[str, Any],
        fetch: dict[str, Any],
        *,
        image_id: str,
        index: int = 1,
    ) -> dict[str, Any]:
        link_lineage = {
            "plan_id": candidate["plan_id"],
            "task_id": candidate["task_id"],
            "angle_id": candidate["angle_id"],
            "route": candidate["route"],
            "candidate_id": candidate["candidate_id"],
            "fetch_id": fetch["fetch_id"],
            "evidence_image_id": image_id,
        }
        return {
            "observation_id": f"obs_real_{index:03d}",
            "evidence_image_id": image_id,
            "plan_id": candidate["plan_id"],
            "task_id": candidate["task_id"],
            "angle_id": candidate["angle_id"],
            "route": candidate["route"],
            "candidate_id": candidate["candidate_id"],
            "fetch_id": fetch["fetch_id"],
            "provider": "codex-interactive",
            "provider_kind": "vlm",
            "provider_mode": "real",
            "provider_run_id": "codex-interactive-real-001",
            "analysis_provider": "codex-interactive",
            "codex_interactive_handoff": True,
            "handoff_artifact": "visual_observations.jsonl",
            "provider_provenance": {
                "provider": "codex-interactive",
                "provider_kind": "vlm",
                "provider_mode": "real",
                "codex_interactive_handoff": True,
                "handoff_artifact": "visual_observations.jsonl",
                "external_vlm_call": False,
            },
            "observation_status": "analyzed",
            "observations": ["The image contains visible evidence."],
            "inferences": ["The image supports the visual claim."],
            "policy_decision": "allowed",
            "verifier_links": [
                {
                    "claim_id": "claim_visual_001",
                    "visual_support_ref": f"images.{image_id}.observations[0]",
                    "verifier_vote_id": f"vote_visual_{index:03d}",
                    **link_lineage,
                }
            ],
            "report_links": [
                {
                    "claim_id": "claim_visual_001",
                    "report_section_id": "findings",
                    "citation_id": f"img:{image_id}",
                    **link_lineage,
                }
            ],
        }

    def codex_native_search_result(
        self,
        prompt: dict[str, Any],
        *,
        suite_id: str,
        task: dict[str, Any] | None = None,
        rank: int = 1,
    ) -> dict[str, Any]:
        task = task or self.semantic_bounded_tasks(prompt)[0]
        return {
            "id": f"search_{prompt['id']}_{rank:03d}",
            "task_id": task["task_id"],
            "semantic_plan_task_id": task["task_id"],
            "angle_id": task["angle_id"],
            "route": task["route"],
            "provider": "codex-native",
            "provider_mode": "real",
            "query": task["query"],
            "freshness_requirement": task["freshness_requirement"],
            "source_policy": task["source_policy"],
            "url": "https://example.com/public-beta-source",
            "title": "Public beta source",
            "snippet": "A public-safe source supports the validation prompt.",
            "result_type": "web",
            "rank": rank,
            "retrieval_status": "fetched",
            "policy_decision": "allowed",
            "policy_flags": [],
            "prompt_id": prompt["id"],
            "prompt_hash": self.prompt_hash(prompt["prompt"]),
            "suite_id": suite_id,
            "accessed_at": self.now(),
            "language": "en",
            "region": "US",
            "handoff_artifact": "search_results.jsonl",
            "approved_delta_id": "base_plan",
            "raw_provider_metadata": {},
        }

    def prompt_hash(self, prompt: str) -> str:
        normalized = " ".join(prompt.strip().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    unittest.main()
