from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import (  # noqa: E402
    acquire_visual_candidates,
    ingest_vision_observations,
    prepare_run,
)
from deepresearch.visual_artifacts import (  # noqa: E402
    IMAGE_FETCH_STATUS_FILENAME,
    VISUAL_CANDIDATES_FILENAME,
    VISUAL_PROVIDER_STATUS_FILENAME,
    VISUAL_SEARCH_PLAN_FILENAME,
    automatic_visual_status_envelope,
    real_automatic_visual_release_counts,
    validate_visual_artifacts,
    visual_failure_code_for_minimums,
    visual_minimum_diagnostics,
    visual_minimums_for_run,
    visual_release_minimums,
)

prepare_search_handoff_run = prepare_run


def prepare_run(*args, **kwargs):
    kwargs.setdefault("angles", ["primary source discovery"])
    kwargs.setdefault("_allow_release_ineligible_materialization_for_tests", True)
    return prepare_search_handoff_run(*args, **kwargs)


class VisualArtifactTests(unittest.TestCase):
    def temp_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def read_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_jsonl(self, path: Path, records: list[dict]) -> None:
        path.write_text(
            "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
            encoding="utf-8",
        )

    def test_acquisition_writes_phase3_visual_artifacts_for_fixture_mechanics(self) -> None:
        prepared = prepare_run(
            question="Inspect deterministic visual artifacts",
            runs_dir=self.temp_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])

        result = acquire_visual_candidates(run=run_dir)

        self.assertEqual(result["status"], "visual_candidates_collected")
        self.assertTrue(
            result["visual_artifact_validation"]["valid"],
            result["visual_artifact_validation"]["errors"],
        )
        for filename in (
            VISUAL_SEARCH_PLAN_FILENAME,
            VISUAL_CANDIDATES_FILENAME,
            IMAGE_FETCH_STATUS_FILENAME,
            VISUAL_PROVIDER_STATUS_FILENAME,
        ):
            self.assertTrue((run_dir / filename).is_file(), filename)

        provider_status = self.read_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME)
        provider = provider_status["providers"][0]
        for field in (
            "available",
            "blocked_reason",
            "invocations",
            "candidates_discovered",
            "artifacts_fetched",
            "vlm_images_analyzed",
            "estimated_cost_usd",
            "actual_cost_usd",
        ):
            self.assertIn(field, provider)

        counts = real_automatic_visual_release_counts(
            candidates=self.read_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME),
            fetches=self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME),
            observations=self.read_jsonl(run_dir / "visual_observations.jsonl"),
            visual_provider_status=provider_status,
        )
        self.assertEqual(counts["real_candidates"], 0)
        self.assertEqual(counts["real_fetches"], 0)
        self.assertEqual(counts["real_observations"], 0)

    def test_ingest_vision_preserves_phase3_visual_observations(self) -> None:
        prepared = prepare_run(
            question="Inspect deterministic visual artifacts after ingest",
            runs_dir=self.temp_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        acquire_visual_candidates(run=run_dir)

        ingest = ingest_vision_observations(run=run_dir, provider="codex-interactive")
        result = validate_visual_artifacts(run_dir=run_dir)

        self.assertEqual(ingest["status"], "visual_evidence_ingested")
        self.assertTrue(result.valid, [error.to_dict() for error in result.errors])
        observation = self.read_jsonl(run_dir / "visual_observations.jsonl")[0]
        for field in (
            "evidence_image_id",
            "model_or_tool",
            "observation_status",
            "confidence",
            "verifier_links",
            "report_links",
            "created_at",
        ):
            self.assertIn(field, observation)

    def test_valid_phase3_visual_lineage_and_non_real_modes_validate(self) -> None:
        run_dir = self.write_phase3_fixture(include_non_real=True)

        result = validate_visual_artifacts(run_dir=run_dir)

        self.assertTrue(result.valid, [error.to_dict() for error in result.errors])
        counts = real_automatic_visual_release_counts(
            candidates=self.read_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME),
            fetches=self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME),
            observations=self.read_jsonl(run_dir / "visual_observations.jsonl"),
            visual_provider_status=self.read_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME),
        )
        self.assertEqual(counts["real_candidates"], 3)
        self.assertEqual(counts["real_fetches"], 3)
        self.assertEqual(counts["real_observations"], 3)
        self.assertGreaterEqual(counts["excluded_non_real_provider_records"], 3)

    def test_multi_angle_visual_lineage_fixture_validates_full_chain(self) -> None:
        run_dir = self.write_multi_angle_lineage_fixture()

        result = validate_visual_artifacts(run_dir=run_dir)

        self.assertTrue(result.valid, [error.to_dict() for error in result.errors])
        research_tasks = self.read_json(run_dir / "research_tasks.json")["tasks"]
        plans = self.read_json(run_dir / VISUAL_SEARCH_PLAN_FILENAME)["tasks"]
        candidates = self.read_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME)
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        evidence = self.read_json(run_dir / "evidence.json")
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        report_status = self.read_json(run_dir / "report_status.json")
        report = (run_dir / "report.md").read_text(encoding="utf-8")

        self.assertEqual(len(research_tasks), 3)
        self.assertEqual(len(plans), 3)
        self.assertEqual(len(candidates), 10)
        self.assertEqual(len(fetches), 3)
        self.assertEqual(len(evidence["images"]), 3)
        self.assertEqual(len(observations), 3)
        self.assertGreaterEqual(len(report_status["used_images"]), 1)
        self.assertIn(report_status["used_images"][0], report)

        self.assert_unique(plans, "plan_id")
        self.assert_unique(candidates, "candidate_id")
        self.assert_unique(fetches, "fetch_id")
        self.assert_unique(evidence["images"], "id")
        self.assert_unique(observations, "observation_id")

        plan_by_id = {record["plan_id"]: record for record in plans}
        candidate_by_id = {record["candidate_id"]: record for record in candidates}
        fetch_by_id = {record["fetch_id"]: record for record in fetches}
        image_by_id = {record["id"]: record for record in evidence["images"]}
        for candidate in candidates:
            plan = plan_by_id[candidate["plan_id"]]
            self.assert_lineage(candidate, plan)
        for fetch in fetches:
            candidate = candidate_by_id[fetch["candidate_id"]]
            self.assert_lineage(fetch, candidate)
            image = image_by_id[fetch["evidence_image_id"]]
            self.assert_lineage(image, fetch)
        for observation in observations:
            fetch = fetch_by_id[observation["fetch_id"]]
            self.assert_lineage(observation, fetch)
            self.assert_lineage(observation["verifier_links"][0], observation)
            self.assert_lineage(observation["report_links"][0], observation)

        included_claim = report_status["included_claims"][0]
        support = included_claim["visual_supports"][0]
        image = image_by_id[support["image_id"]]
        self.assert_lineage(support, image)

    def test_visual_minimums_detect_visual_required_from_plan_artifact(self) -> None:
        run_dir = self.temp_dir()
        self.write_json(
            run_dir / "evidence.json",
            {
                "schema_version": "0.1.0",
                "run_id": run_dir.name,
                "question": "Inspect plan-bound visual work",
                "routing": [{"id": "angle_001", "modality": "text_only"}],
                "sources": [],
                "images": [],
                "claims": [],
            },
        )
        self.write_json(
            run_dir / VISUAL_SEARCH_PLAN_FILENAME,
            {
                "schema_version": "codex-deepresearch.visual-artifacts.v0",
                "run_id": run_dir.name,
                "created_at": "2026-07-12T00:00:00Z",
                "tasks": [
                    {
                        "plan_id": "plan_task_016_angle_004_visual_required",
                        "task_id": "task_016",
                        "semantic_plan_task_id": "task_016",
                        "angle_id": "angle_004",
                        "route": "visual_required",
                        "target_evidence_type": "web_image",
                        "query": "visual consistency",
                        "providers": ["child-discovered-image-url"],
                        "source_search_result_ids": [],
                        "caps": {
                            "max_candidates": 10,
                            "max_fetches": 3,
                            "max_vlm_images": 3,
                            "max_cost_usd": 0.0,
                        },
                        "policy_constraints": {"policy_decision": "allowed"},
                        "estimated_cost_usd": 0.0,
                        "state": "completed",
                    }
                ],
            },
        )
        self.write_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME, [])
        self.write_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME, [])
        self.write_jsonl(run_dir / "visual_observations.jsonl", [])

        minimums = visual_minimums_for_run(run_dir)

        self.assertEqual(minimums["required_vlm_images"], 3)
        self.assertFalse(minimums["satisfied"])
        self.assertEqual(minimums["shortfall_reason"], "insufficient_candidates")

    def test_multi_angle_visual_lineage_reports_specific_error_classes(self) -> None:
        cases = {
            "duplicate_id": self.break_duplicate_plan_id,
            "angle_mismatch": self.break_fetch_angle,
            "route_mismatch": self.break_observation_route,
        }
        for expected_code, mutator in cases.items():
            with self.subTest(expected_code=expected_code):
                run_dir = self.write_multi_angle_lineage_fixture()
                mutator(run_dir)

                result = validate_visual_artifacts(run_dir=run_dir)

                self.assertFalse(result.valid)
                self.assertIn(expected_code, {error.code for error in result.errors})

    def test_observation_link_lineage_mismatches_fail_validation(self) -> None:
        cases = (
            ("verifier_links", "angle_id", "angle_999", "angle_mismatch"),
            ("report_links", "route", "text_only", "route_mismatch"),
        )
        for link_type, field, bad_value, expected_code in cases:
            with self.subTest(link_type=link_type, field=field):
                run_dir = self.write_multi_angle_lineage_fixture()
                observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
                observations[0][link_type][0][field] = bad_value
                self.write_jsonl(run_dir / "visual_observations.jsonl", observations)

                result = validate_visual_artifacts(run_dir=run_dir)

                self.assertFalse(result.valid)
                self.assertIn(expected_code, {error.code for error in result.errors})

    def test_verifier_link_missing_lineage_fails_validation(self) -> None:
        run_dir = self.write_multi_angle_lineage_fixture()
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        observations[0]["verifier_links"][0] = {
            "claim_id": "claim_multi_visual_001",
            "visual_support_ref": "images.img_multi_001.observations[0]",
            "verifier_vote_id": "vote_multi_001",
        }
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)

        result = validate_visual_artifacts(run_dir=run_dir)

        self.assertFalse(result.valid)
        missing = [error for error in result.errors if error.code == "missing_lineage"]
        self.assertTrue(missing, [error.to_dict() for error in result.errors])
        self.assertIn(
            "$.visual_observations[0].verifier_links[0].plan_id",
            {error.path for error in missing},
        )

    def test_run_dir_validation_requires_phase3_visual_artifacts(self) -> None:
        for filename, expected_path in (
            (VISUAL_SEARCH_PLAN_FILENAME, "$.visual_search_plan"),
            (VISUAL_CANDIDATES_FILENAME, "$.visual_candidates"),
            (IMAGE_FETCH_STATUS_FILENAME, "$.image_fetch_status"),
            (VISUAL_PROVIDER_STATUS_FILENAME, "$.visual_provider_status"),
        ):
            with self.subTest(filename=filename):
                run_dir = self.write_phase3_fixture()
                (run_dir / filename).unlink()

                result = validate_visual_artifacts(run_dir=run_dir)

                self.assertFalse(result.valid)
                errors = {error.path: error.code for error in result.errors}
                self.assertEqual(errors.get(expected_path), "missing_file")

    def test_run_dir_with_only_evidence_fails_required_visual_artifacts(self) -> None:
        run_dir = self.temp_dir() / "dr_missing_visual_artifacts"
        run_dir.mkdir()
        self.write_json(run_dir / "evidence.json", {"schema_version": "0.1.0"})

        result = validate_visual_artifacts(run_dir=run_dir)

        self.assertFalse(result.valid)
        missing_paths = {
            "$.visual_search_plan",
            "$.visual_candidates",
            "$.image_fetch_status",
            "$.visual_provider_status",
        }
        self.assertTrue(missing_paths.issubset({error.path for error in result.errors}))

    def test_explicit_single_file_validation_does_not_require_run_dir_artifacts(self) -> None:
        run_dir = self.write_phase3_fixture()

        result = validate_visual_artifacts(
            visual_provider_status_path=run_dir / VISUAL_PROVIDER_STATUS_FILENAME
        )

        self.assertTrue(result.valid, [error.to_dict() for error in result.errors])

    def test_completed_auto_visual_requires_real_non_fixture_prerequisites(self) -> None:
        run_dir = self.write_phase3_fixture()
        provider_status = self.read_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME)
        provider_status["providers"] = [
            {
                **provider,
                "provider_kind": "fixture",
                "provider_mode": "fixture",
            }
            for provider in provider_status["providers"]
        ]
        self.write_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME, provider_status)

        result = validate_visual_artifacts(run_dir=run_dir)

        self.assertFalse(result.valid)
        self.assertIn(
            "completed_auto_visual_prerequisites",
            {error.code for error in result.errors},
        )

    def test_completed_auto_visual_rejects_empty_run_dir_artifacts(self) -> None:
        run_dir = self.temp_dir() / "dr_empty_completed_visual"
        run_dir.mkdir()
        created_at = "2026-06-25T00:00:00Z"
        self.write_json(
            run_dir / VISUAL_SEARCH_PLAN_FILENAME,
            {
                "schema_version": "codex-deepresearch.visual-artifacts.v0",
                "run_id": run_dir.name,
                "created_at": created_at,
                "tasks": [],
            },
        )
        self.write_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME, [])
        self.write_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME, [])
        self.write_jsonl(run_dir / "visual_observations.jsonl", [])
        self.write_json(
            run_dir / VISUAL_PROVIDER_STATUS_FILENAME,
            {
                "schema_version": "codex-deepresearch.visual-provider-status.v0",
                "run_id": run_dir.name,
                "status": "completed_auto_visual",
                "ok": True,
                "terminal": True,
                "metric_classification": "success",
                "providers": [
                    self.provider_status(
                        provider="real-image-provider",
                        provider_kind="web_image_search",
                        provider_mode="real",
                        invocations=1,
                        candidates_discovered=0,
                        artifacts_fetched=0,
                        vlm_images_analyzed=0,
                    )
                ],
            },
        )

        result = validate_visual_artifacts(run_dir=run_dir)

        self.assertFalse(result.valid)
        errors = [error for error in result.errors if error.code == "completed_auto_visual_prerequisites"]
        self.assertEqual(len(errors), 1)
        self.assertIn("real_fetched_visual_artifact", errors[0].message)
        self.assertIn("real_vlm_observation", errors[0].message)
        self.assertIn("report_cited_supported_visual_claim", errors[0].message)

    def test_report_used_supported_visual_claim_requires_observation_links(self) -> None:
        run_dir = self.write_phase3_fixture()
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        observations[0]["verifier_links"] = []
        observations[0]["report_links"] = []
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)

        result = validate_visual_artifacts(run_dir=run_dir)

        self.assertFalse(result.valid)
        error_codes = {error.code for error in result.errors}
        self.assertIn("missing_verifier_link", error_codes)
        self.assertIn("missing_report_link", error_codes)

    def test_excluded_visual_claim_does_not_require_report_link_for_shared_image(self) -> None:
        run_dir = self.write_multi_angle_lineage_fixture()
        evidence = self.read_json(run_dir / "evidence.json")
        included_claim = evidence["claims"][0]
        support = dict(included_claim["visual_supports"][0])
        excluded_claim_id = "claim_excluded_shared_visual"
        evidence["claims"].append(
            {
                "id": excluded_claim_id,
                "text": "Excluded claim shares an image with an included visual claim.",
                "claim_type": "mixed",
                "supporting_sources": [],
                "supporting_images": [support["image_id"]],
                "visual_supports": [support],
                "quote_spans": [],
                "votes": [{"id": "vote_excluded_shared_visual"}],
                "verification_status": "supported",
                "review_status": "auto_reviewed",
                "promotion_status": "eligible",
                "confidence": "medium",
                "caveats": [],
            }
        )
        self.write_json(run_dir / "evidence.json", evidence)
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        verifier_link = dict(observations[0]["verifier_links"][0])
        verifier_link["claim_id"] = excluded_claim_id
        verifier_link["verifier_vote_id"] = "vote_excluded_shared_visual"
        observations[0]["verifier_links"].append(verifier_link)
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)
        report_status = self.read_json(run_dir / "report_status.json")
        report_status["excluded_claims"] = [
            {
                "claim_id": excluded_claim_id,
                "claim_type": "mixed",
                "verification_status": "supported",
                "image_ids": [support["image_id"]],
                "visual_supports": [support],
                "exclusion_reasons": ["missing_quote_source"],
            }
        ]
        self.write_json(run_dir / "report_status.json", report_status)

        result = validate_visual_artifacts(run_dir=run_dir)

        self.assertTrue(result.valid, [error.to_dict() for error in result.errors])

    def test_included_visual_claim_still_requires_report_link(self) -> None:
        run_dir = self.write_multi_angle_lineage_fixture()
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        observations[0]["report_links"] = []
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)

        result = validate_visual_artifacts(run_dir=run_dir)

        self.assertFalse(result.valid)
        self.assertIn("missing_report_link", {error.code for error in result.errors})

    def test_completed_auto_visual_rejects_zero_provider_counters(self) -> None:
        run_dir = self.write_phase3_fixture()
        provider_status = self.read_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME)
        for provider in provider_status["providers"]:
            provider["invocations"] = 0
            provider["candidates_discovered"] = 0
            provider["artifacts_fetched"] = 0
            provider["vlm_images_analyzed"] = 0
        self.write_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME, provider_status)

        result = validate_visual_artifacts(run_dir=run_dir)

        self.assertFalse(result.valid)
        errors = [
            error
            for error in result.errors
            if error.code == "completed_auto_visual_provider_counters"
        ]
        self.assertEqual(len(errors), 1)
        self.assertIn("real_acquisition_invocations", errors[0].message)
        self.assertIn("real_candidates_discovered", errors[0].message)
        self.assertIn("real_artifacts_fetched", errors[0].message)
        self.assertIn("real_vlm_images_analyzed", errors[0].message)

    def test_visual_release_minimums_keep_text_only_runs_at_zero_visual_work(self) -> None:
        minimums = visual_release_minimums(required_vlm_images=0)

        self.assertEqual(minimums["required_vlm_images"], 0)
        self.assertEqual(minimums["candidate_count"], 0)
        self.assertEqual(minimums["fetched_artifacts"], 0)
        self.assertEqual(minimums["vlm_images_analyzed"], 0)
        self.assertEqual(minimums["report_cited_images"], 0)
        self.assertTrue(minimums["satisfied"])
        self.assertEqual(minimums["shortfall_reason"], "none")
        self.assertIsNone(visual_failure_code_for_minimums(minimums))

    def test_visual_release_minimums_flag_shortfall_for_one_or_two_real_analyzed_images(self) -> None:
        candidates: list[dict] = []
        fetches: list[dict] = []
        observations: list[dict] = []
        for index in range(1, 4):
            candidate = self.candidate_record(
                candidate_id=f"cand_real_{index:03d}",
                task_id="task_visual_001",
                angle_id="angle_001",
                provider="page-image-extractor",
                provider_kind="page_extractor",
                provider_mode="real",
            )
            candidates.append(candidate)
            fetch = self.fetch_record(
                candidate=candidate,
                fetch_id=f"fetch_real_{index:03d}",
                evidence_image_id=f"img_real_{index:03d}",
            )
            fetches.append(fetch)
            if index <= 2:
                observations.append(
                    self.observation_record(
                        candidate=candidate,
                        fetch_id=fetch["fetch_id"],
                        evidence_image_id=fetch["evidence_image_id"],
                        claim_id=None,
                        verifier_vote_id=None,
                        provider="codex-interactive",
                        provider_kind="vlm",
                        provider_mode="real",
                    )
                )

        minimums = visual_release_minimums(
            candidates=candidates,
            fetches=fetches,
            observations=observations,
            evidence={"routing": [{"id": "angle_001", "modality": "visual_required"}]},
            report_status={},
        )

        self.assertEqual(minimums["required_vlm_images"], 3)
        self.assertEqual(minimums["fetched_artifacts"], 3)
        self.assertEqual(minimums["vlm_images_analyzed"], 2)
        self.assertFalse(minimums["satisfied"])
        self.assertNotEqual(minimums["shortfall_reason"], "none")
        self.assertEqual(
            visual_failure_code_for_minimums(minimums),
            "visual_minimum_shortfall",
        )

    def test_visual_minimum_diagnostics_preserve_specific_shortfall_category(self) -> None:
        expected_codes = {
            "insufficient_candidates": "visual_minimum_shortfall",
            "fetch_failures": "visual_minimum_shortfall",
            "vlm_failures": "visual_minimum_shortfall",
            "report_linkage_missing": "visual_report_linkage_missing",
            "policy_blocked": "visual_minimum_shortfall",
            "budget_pruned": "visual_minimum_shortfall",
        }
        for reason, failure_code in expected_codes.items():
            with self.subTest(reason=reason):
                diagnostics = visual_minimum_diagnostics(
                    {
                        "required_vlm_images": 3,
                        "candidate_count": 3,
                        "selected_candidates": 3,
                        "fetched_artifacts": 2,
                        "vlm_images_analyzed": 2,
                        "report_cited_images": 0,
                        "satisfied": False,
                        "shortfall_reason": reason,
                    }
                )

                self.assertEqual(diagnostics["shortfall_reason"], reason)
                self.assertEqual(diagnostics["failure_category"], reason)
                self.assertEqual(diagnostics["failure_code"], failure_code)

    def test_visual_release_minimums_flag_missing_report_linkage_after_three_real_images(self) -> None:
        candidates: list[dict] = []
        fetches: list[dict] = []
        observations: list[dict] = []
        image_ids: list[str] = []
        for index in range(1, 4):
            candidate = self.candidate_record(
                candidate_id=f"cand_real_{index:03d}",
                task_id="task_visual_001",
                angle_id="angle_001",
                provider="child-discovered-image-url",
                provider_kind="web_image_search",
                provider_mode="real",
            )
            candidates.append(candidate)
            image_id = f"img_real_{index:03d}"
            image_ids.append(image_id)
            fetch = self.fetch_record(
                candidate=candidate,
                fetch_id=f"fetch_real_{index:03d}",
                evidence_image_id=image_id,
            )
            fetches.append(fetch)
            observations.append(
                self.observation_record(
                    candidate=candidate,
                    fetch_id=fetch["fetch_id"],
                    evidence_image_id=image_id,
                    claim_id=None,
                    verifier_vote_id=None,
                    provider="codex-interactive",
                    provider_kind="vlm",
                    provider_mode="real",
                )
            )

        minimums = visual_release_minimums(
            candidates=candidates,
            fetches=fetches,
            observations=observations,
            evidence={
                "routing": [{"id": "angle_001", "modality": "visual_required"}],
                "claims": [
                    {
                        "id": "claim_visual_real",
                        "claim_type": "visual",
                        "supporting_images": image_ids,
                        "verification_status": "supported",
                    }
                ],
            },
            report_status={"used_images": []},
        )

        self.assertEqual(minimums["vlm_images_analyzed"], 3)
        self.assertEqual(minimums["report_cited_images"], 0)
        self.assertFalse(minimums["satisfied"])
        self.assertEqual(minimums["shortfall_reason"], "report_linkage_missing")
        self.assertEqual(
            visual_failure_code_for_minimums(minimums),
            "visual_report_linkage_missing",
        )

    def test_visual_release_minimums_deduplicate_fetches_and_observations_for_one_image(self) -> None:
        candidate = self.candidate_record(
            candidate_id="cand_real_001",
            task_id="task_visual_001",
            angle_id="angle_001",
            provider="page-image-extractor",
            provider_kind="page_extractor",
            provider_mode="real",
        )
        fetch = self.fetch_record(
            candidate=candidate,
            fetch_id="fetch_real_001",
            evidence_image_id="img_real_001",
        )
        fetches = [
            {**fetch, "fetch_id": f"fetch_real_dup_{index:03d}"}
            for index in range(1, 4)
        ]
        observations = [
            {
                **self.observation_record(
                    candidate=candidate,
                    fetch_id=fetches[0]["fetch_id"],
                    evidence_image_id=fetch["evidence_image_id"],
                    claim_id=None,
                    verifier_vote_id=None,
                    provider="codex-interactive",
                    provider_kind="vlm",
                    provider_mode="real",
                ),
                "observation_id": f"obs_real_dup_{index}",
            }
            for index in range(1, 4)
        ]

        minimums = visual_release_minimums(
            candidates=[candidate],
            fetches=fetches,
            observations=observations,
            evidence={"routing": [{"id": "angle_001", "modality": "visual_required"}]},
            report_status={},
        )

        self.assertEqual(minimums["fetched_artifacts"], 1)
        self.assertEqual(minimums["vlm_images_analyzed"], 1)
        self.assertFalse(minimums["satisfied"])
        self.assertEqual(
            visual_failure_code_for_minimums(minimums),
            "visual_minimum_shortfall",
        )

    def test_candidate_provider_kind_rejects_vlm_and_counts_do_not_inflate(self) -> None:
        run_dir = self.write_phase3_fixture()
        candidates = self.read_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME)
        candidates[0]["provider_kind"] = "vlm"
        self.write_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME, candidates)

        result = validate_visual_artifacts(run_dir=run_dir)

        self.assertFalse(result.valid)
        self.assertIn("$.visual_candidates[0].provider_kind", {error.path for error in result.errors})

        counts = real_automatic_visual_release_counts(
            candidates=[{"provider_kind": "vlm", "provider_mode": "real"}],
            fetches=[{"provider_kind": "vlm", "provider_mode": "real"}],
            observations=[{"provider_kind": "vlm", "provider_mode": "real"}],
            visual_provider_status={
                "providers": [{"provider_kind": "vlm", "provider_mode": "real"}]
            },
        )
        self.assertEqual(counts["real_candidates"], 0)
        self.assertEqual(counts["real_fetches"], 0)
        self.assertEqual(counts["real_observations"], 1)

    def test_completed_auto_visual_requires_minimums_object(self) -> None:
        run_dir = self.write_phase3_fixture()
        provider_status = self.read_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME)
        provider_status.pop("minimums", None)
        self.write_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME, provider_status)

        result = validate_visual_artifacts(run_dir=run_dir)

        self.assertFalse(result.valid)
        self.assertIn(
            ("$.visual_provider_status.minimums", "missing_required_field"),
            {(error.path, error.code) for error in result.errors},
        )

    def test_completed_auto_visual_rejects_unsatisfied_minimums(self) -> None:
        run_dir = self.write_phase3_fixture()
        provider_status = self.read_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME)
        provider_status["minimums"] = {
            **provider_status["minimums"],
            "vlm_images_analyzed": 2,
            "satisfied": False,
            "shortfall_reason": "vlm_failures",
        }
        self.write_json(run_dir / VISUAL_PROVIDER_STATUS_FILENAME, provider_status)

        result = validate_visual_artifacts(run_dir=run_dir)

        self.assertFalse(result.valid)
        self.assertIn(
            "completed_auto_visual_minimum_mismatch",
            {error.code for error in result.errors},
        )

    def test_validation_rejects_missing_required_automatic_fields(self) -> None:
        cases = (
            (VISUAL_CANDIDATES_FILENAME, 0, "task_id", "$.visual_candidates[0].task_id"),
            (VISUAL_CANDIDATES_FILENAME, 0, "angle_id", "$.visual_candidates[0].angle_id"),
            (
                VISUAL_CANDIDATES_FILENAME,
                0,
                "candidate_id",
                "$.visual_candidates[0].candidate_id",
            ),
            (
                VISUAL_CANDIDATES_FILENAME,
                0,
                "provider_provenance",
                "$.visual_candidates[0].provider_provenance",
            ),
            (
                VISUAL_CANDIDATES_FILENAME,
                0,
                "policy_decision",
                "$.visual_candidates[0].policy_decision",
            ),
            (
                VISUAL_CANDIDATES_FILENAME,
                0,
                "estimated_cost_usd",
                "$.visual_candidates[0].estimated_cost_usd",
            ),
            (IMAGE_FETCH_STATUS_FILENAME, 0, "fetch_id", "$.image_fetch_status[0].fetch_id"),
            (
                IMAGE_FETCH_STATUS_FILENAME,
                0,
                "actual_cost_usd",
                "$.image_fetch_status[0].actual_cost_usd",
            ),
            (
                "visual_observations.jsonl",
                0,
                "task_id",
                "$.visual_observations[0].task_id",
            ),
            (
                "visual_observations.jsonl",
                0,
                "candidate_id",
                "$.visual_observations[0].candidate_id",
            ),
            (
                "visual_observations.jsonl",
                0,
                "fetch_id",
                "$.visual_observations[0].fetch_id",
            ),
            (
                "visual_observations.jsonl",
                0,
                "evidence_image_id",
                "$.visual_observations[0].evidence_image_id",
            ),
            (
                "visual_observations.jsonl",
                0,
                "actual_cost_usd",
                "$.visual_observations[0].actual_cost_usd",
            ),
        )
        for filename, index, field, expected_path in cases:
            with self.subTest(field=field, filename=filename):
                run_dir = self.write_phase3_fixture()
                records = self.read_jsonl(run_dir / filename)
                records[index].pop(field)
                self.write_jsonl(run_dir / filename, records)

                result = validate_visual_artifacts(run_dir=run_dir)

                self.assertFalse(result.valid)
                self.assertIn(expected_path, {error.path for error in result.errors})

    def test_automatic_visual_status_envelopes(self) -> None:
        expected = {
            "completed_auto_visual": (True, True, "success"),
            "partial_auto_visual": (False, True, "included_failure"),
            "blocked_missing_visual_provider": (False, True, "excluded_blocked"),
            "blocked_missing_vlm_provider": (False, True, "excluded_blocked"),
            "policy_blocked_visual": (True, True, "excluded_policy_blocked"),
            "budget_pruned_visual": (False, True, "included_failure"),
        }
        for status, (ok, terminal, metric) in expected.items():
            with self.subTest(status=status):
                envelope = automatic_visual_status_envelope(status)
                self.assertEqual(envelope["ok"], ok)
                self.assertEqual(envelope["terminal"], terminal)
                self.assertEqual(envelope["metric_classification"], metric)

    def write_phase3_fixture(self, *, include_non_real: bool = False) -> Path:
        run_dir = self.temp_dir() / "dr_visual_artifact_fixture"
        run_dir.mkdir()
        run_id = run_dir.name
        task_id = "task_research_001"
        angle_id = "angle_001"
        created_at = "2026-06-25T00:00:00Z"
        self.write_json(
            run_dir / "research_tasks.json",
            {
                "schema_version": "codex-deepresearch.parallel.v0",
                "run_id": run_id,
                "created_at": created_at,
                "tasks": [
                    {
                        "id": task_id,
                        "angle_id": angle_id,
                        "route": "visual_required",
                        "query": "visual artifact fixture",
                    }
                ],
            },
        )
        self.write_json(
            run_dir / "visual_tasks.json",
            {
                "schema_version": "codex-deepresearch.search-handoff.v0",
                "run_id": run_id,
                "created_at": created_at,
                "tasks": [
                    {
                        "id": "task_visual_001",
                        "angle_id": angle_id,
                        "route": "visual_required",
                    }
                ],
            },
        )
        candidates = []
        fetches = []
        observations = []
        images = []
        real_image_ids = []
        for index in range(1, 4):
            candidate = self.candidate_record(
                candidate_id=f"cand_real_{index:03d}",
                task_id=task_id,
                angle_id=angle_id,
                provider="real-image-provider",
                provider_kind="web_image_search",
                provider_mode="real",
            )
            fetch = self.fetch_record(
                candidate=candidate,
                fetch_id=f"fetch_real_{index:03d}",
                evidence_image_id=f"img_real_{index:03d}",
            )
            candidates.append(candidate)
            fetches.append(fetch)
            real_image_ids.append(fetch["evidence_image_id"])
            observations.append(
                self.observation_record(
                    candidate=candidate,
                    fetch_id=fetch["fetch_id"],
                    evidence_image_id=fetch["evidence_image_id"],
                    claim_id="claim_visual_001",
                    verifier_vote_id=f"vote_visual_{index:03d}",
                    provider="codex-interactive",
                    provider_kind="vlm",
                    provider_mode="real",
                )
            )
            images.append(
                self.evidence_image(
                    candidate=candidate,
                    fetch=fetch,
                    evidence_image_id=fetch["evidence_image_id"],
                )
            )
        providers = [
            self.provider_status(
                provider="real-image-provider",
                provider_kind="web_image_search",
                provider_mode="real",
                invocations=1,
                candidates_discovered=3,
                artifacts_fetched=3,
                vlm_images_analyzed=3,
            )
        ]
        if include_non_real:
            for mode in ("fixture", "manual", "user_provided"):
                candidate_id = f"cand_{mode}_001"
                fetch_id = f"fetch_{mode}_001"
                image_id = f"img_{mode}_001"
                provider_kind = "fixture" if mode == "fixture" else "manual"
                candidate = self.candidate_record(
                    candidate_id=candidate_id,
                    task_id=task_id,
                    angle_id=angle_id,
                    provider=f"{mode}-provider",
                    provider_kind=provider_kind,
                    provider_mode=mode,
                )
                fetch = self.fetch_record(
                    candidate=candidate,
                    fetch_id=fetch_id,
                    evidence_image_id=image_id,
                )
                candidates.append(candidate)
                fetches.append(fetch)
                observations.append(
                    self.observation_record(
                        candidate=candidate,
                        fetch_id=fetch_id,
                        evidence_image_id=image_id,
                        claim_id=None,
                        verifier_vote_id=None,
                        provider=f"{mode}-vision",
                        provider_kind=provider_kind,
                        provider_mode=mode,
                    )
                )
                images.append(
                    self.evidence_image(
                        candidate=candidate,
                        fetch=fetch,
                        evidence_image_id=image_id,
                    )
                )
                providers.append(
                    self.provider_status(
                        provider=f"{mode}-provider",
                        provider_kind=provider_kind,
                        provider_mode=mode,
                        invocations=1,
                        candidates_discovered=1,
                        artifacts_fetched=1,
                        vlm_images_analyzed=1,
                    )
                )
        self.write_json(
            run_dir / VISUAL_SEARCH_PLAN_FILENAME,
            {
                "schema_version": "codex-deepresearch.visual-artifacts.v0",
                "run_id": run_id,
                "created_at": created_at,
                "tasks": [
                    {
                        "plan_id": "plan_visual_001",
                        "task_id": task_id,
                        "angle_id": angle_id,
                        "route": "visual_required",
                        "target_evidence_type": "web_image",
                        "query": "visual artifact fixture",
                        "providers": ["real-image-provider"],
                        "source_search_result_ids": [],
                        "caps": {
                            "max_candidates": 4,
                            "max_fetches": 4,
                            "max_vlm_images": 4,
                            "max_cost_usd": 0.25,
                        },
                        "policy_constraints": {"robots": "allowed"},
                        "estimated_cost_usd": 0.05,
                        "state": "completed",
                    }
                ],
            },
        )
        self.write_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME, candidates)
        self.write_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME, fetches)
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)
        evidence_payload = {
            "schema_version": "0.1.0",
            "run_id": run_id,
            "created_at": created_at,
            "question": "Visual artifact fixture",
            "mode": "automated-cli",
            "search_provider": "openai",
            "vlm_provider": "codex-interactive",
            "routing": [{"id": angle_id, "modality": "visual_required"}],
            "search_tasks": [],
            "images": images,
            "claims": [
                {
                    "id": "claim_visual_001",
                    "claim_type": "visual",
                    "supporting_images": real_image_ids,
                    "visual_supports": [
                        {
                            "image_id": image_id,
                            "observation_ref": f"images.{image_id}.observations[0]",
                        }
                        for image_id in real_image_ids
                    ],
                    "verification_status": "supported",
                    "votes": [
                        {"id": f"vote_visual_{index:03d}"}
                        for index in range(1, 4)
                    ],
                }
            ],
        }
        report_status_payload = {"used_images": [real_image_ids[0]]}
        minimums = visual_release_minimums(
            candidates=candidates,
            fetches=fetches,
            observations=observations,
            evidence=evidence_payload,
            report_status=report_status_payload,
        )
        self.write_json(
            run_dir / VISUAL_PROVIDER_STATUS_FILENAME,
            {
                "schema_version": "codex-deepresearch.visual-provider-status.v0",
                "run_id": run_id,
                "status": "completed_auto_visual",
                "ok": True,
                "terminal": True,
                "metric_classification": "success",
                "minimums": minimums,
                "providers": providers,
            },
        )
        self.write_json(run_dir / "evidence.json", evidence_payload)
        self.write_json(run_dir / "report_status.json", report_status_payload)
        return run_dir

    def write_multi_angle_lineage_fixture(self) -> Path:
        run_dir = self.temp_dir() / "dr_multi_angle_visual_lineage_fixture"
        run_dir.mkdir()
        run_id = run_dir.name
        created_at = "2026-06-30T00:00:00Z"
        tasks = [
            {
                "id": f"task_visual_{index:03d}",
                "angle_id": f"angle_{index:03d}",
                "route": "visual_required",
                "query": f"multi angle visual lineage {index}",
            }
            for index in range(1, 4)
        ]
        self.write_json(
            run_dir / "research_tasks.json",
            {
                "schema_version": "codex-deepresearch.parallel.v0",
                "run_id": run_id,
                "created_at": created_at,
                "tasks": tasks,
            },
        )
        self.write_json(
            run_dir / "visual_tasks.json",
            {
                "schema_version": "codex-deepresearch.search-handoff.v0",
                "run_id": run_id,
                "created_at": created_at,
                "tasks": [
                    {
                        "id": task["id"],
                        "angle_id": task["angle_id"],
                        "route": task["route"],
                    }
                    for task in tasks
                ],
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
            plan_id = self.plan_id(task["id"], task["angle_id"], task["route"])
            plans.append(
                {
                    "plan_id": plan_id,
                    "task_id": task["id"],
                    "angle_id": task["angle_id"],
                    "route": task["route"],
                    "target_evidence_type": "web_image",
                    "query": task["query"],
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
            candidates_for_task = 4 if task_index == 1 else 3
            for local_index in range(1, candidates_for_task + 1):
                candidate = self.candidate_record(
                    candidate_id=f"cand_multi_{candidate_index:03d}",
                    task_id=task["id"],
                    angle_id=task["angle_id"],
                    route=task["route"],
                    plan_id=plan_id,
                    provider="child-discovered-image-url",
                    provider_kind="web_image_search",
                    provider_mode="real",
                    status="analyzed" if local_index == 1 else "discovered",
                )
                candidates.append(candidate)
                if local_index == 1:
                    image_id = f"img_multi_{task_index:03d}"
                    image_ids.append(image_id)
                    fetch = self.fetch_record(
                        candidate=candidate,
                        fetch_id=f"fetch_multi_{task_index:03d}",
                        evidence_image_id=image_id,
                    )
                    fetches.append(fetch)
                    observations.append(
                        self.observation_record(
                            candidate=candidate,
                            fetch_id=fetch["fetch_id"],
                            evidence_image_id=image_id,
                            claim_id="claim_multi_visual_001",
                            verifier_vote_id=f"vote_multi_{task_index:03d}",
                            provider="codex-interactive",
                            provider_kind="vlm",
                            provider_mode="real",
                        )
                    )
                    images.append(
                        self.evidence_image(
                            candidate=candidate,
                            fetch=fetch,
                            evidence_image_id=image_id,
                        )
                    )
                candidate_index += 1
        self.write_json(
            run_dir / VISUAL_SEARCH_PLAN_FILENAME,
            {
                "schema_version": "codex-deepresearch.visual-artifacts.v0",
                "run_id": run_id,
                "created_at": created_at,
                "tasks": plans,
            },
        )
        self.write_jsonl(run_dir / VISUAL_CANDIDATES_FILENAME, candidates)
        self.write_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME, fetches)
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
                "confidence": 0.86,
            }
            for image in images
        ]
        evidence_payload = {
            "schema_version": "0.1.0",
            "run_id": run_id,
            "created_at": created_at,
            "question": "Multi-angle visual lineage fixture",
            "mode": "codex-plugin",
            "search_provider": "codex-native",
            "vlm_provider": "codex-interactive",
            "routing": [
                {
                    "id": task["angle_id"],
                    "modality": task["route"],
                    "task_id": task["id"],
                    "max_images": 4,
                }
                for task in tasks
            ],
            "search_tasks": [],
            "images": images,
            "claims": [
                {
                    "id": "claim_multi_visual_001",
                    "text": "The multi-angle visual fixture has cited image-backed evidence.",
                    "claim_type": "mixed",
                    "supporting_sources": [],
                    "supporting_images": image_ids,
                    "visual_supports": visual_supports,
                    "quote_spans": [],
                    "votes": [{"id": f"vote_multi_{index:03d}"} for index in range(1, 4)],
                    "verification_status": "supported",
                    "review_status": "human_accepted",
                    "promotion_status": "promoted_memory",
                    "confidence": "high",
                    "caveats": [],
                }
            ],
        }
        report_status_payload = {
            "status": "completed",
            "used_images": [image_ids[0]],
            "included_claims": [
                {
                    "claim_id": "claim_multi_visual_001",
                    "claim_type": "mixed",
                    "verification_status": "supported",
                    "image_ids": [image_ids[0]],
                    "visual_supports": [visual_supports[0]],
                }
            ],
        }
        minimums = visual_release_minimums(
            candidates=candidates,
            fetches=fetches,
            observations=observations,
            evidence=evidence_payload,
            report_status=report_status_payload,
        )
        self.write_json(
            run_dir / VISUAL_PROVIDER_STATUS_FILENAME,
            {
                "schema_version": "codex-deepresearch.visual-provider-status.v0",
                "run_id": run_id,
                "status": "completed_auto_visual",
                "ok": True,
                "terminal": True,
                "metric_classification": "success",
                "minimums": minimums,
                "providers": [
                    self.provider_status(
                        provider="child-discovered-image-url",
                        provider_kind="web_image_search",
                        provider_mode="real",
                        invocations=3,
                        candidates_discovered=10,
                        artifacts_fetched=3,
                        vlm_images_analyzed=0,
                    ),
                    self.provider_status(
                        provider="codex-interactive",
                        provider_kind="vlm",
                        provider_mode="real",
                        invocations=1,
                        candidates_discovered=0,
                        artifacts_fetched=3,
                        vlm_images_analyzed=3,
                    ),
                ],
            },
        )
        self.write_json(run_dir / "evidence.json", evidence_payload)
        self.write_json(run_dir / "report_status.json", report_status_payload)
        (run_dir / "report.md").write_text(
            f"Report cites claim_multi_visual_001 with image {image_ids[0]}.\n",
            encoding="utf-8",
        )
        return run_dir

    def break_duplicate_plan_id(self, run_dir: Path) -> None:
        plan = self.read_json(run_dir / VISUAL_SEARCH_PLAN_FILENAME)
        plan["tasks"][1]["plan_id"] = plan["tasks"][0]["plan_id"]
        self.write_json(run_dir / VISUAL_SEARCH_PLAN_FILENAME, plan)

    def break_fetch_angle(self, run_dir: Path) -> None:
        fetches = self.read_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME)
        fetches[0]["angle_id"] = "angle_999"
        self.write_jsonl(run_dir / IMAGE_FETCH_STATUS_FILENAME, fetches)

    def break_observation_route(self, run_dir: Path) -> None:
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        observations[0]["route"] = "text_only"
        self.write_jsonl(run_dir / "visual_observations.jsonl", observations)

    def plan_id(self, task_id: str, angle_id: str, route: str) -> str:
        return "plan_" + "_".join((task_id, angle_id, route))

    def assert_unique(self, records: list[dict], field: str) -> None:
        values = [record[field] for record in records]
        self.assertEqual(len(values), len(set(values)), field)

    def assert_lineage(self, record: dict, expected: dict) -> None:
        for field in ("plan_id", "task_id", "angle_id", "route"):
            self.assertEqual(record[field], expected[field], field)

    def candidate_record(
        self,
        *,
        candidate_id: str,
        task_id: str,
        angle_id: str,
        provider: str,
        provider_kind: str,
        provider_mode: str,
        plan_id: str = "plan_visual_001",
        route: str = "visual_required",
        status: str = "analyzed",
    ) -> dict:
        return {
            "candidate_id": candidate_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "angle_id": angle_id,
            "route": route,
            "provider": provider,
            "provider_kind": provider_kind,
            "provider_mode": provider_mode,
            "provider_run_id": f"run_{provider_mode}_001",
            "provider_provenance": {
                "provider": provider,
                "provider_mode": provider_mode,
                "provider_kind": provider_kind,
            },
            "origin": "image_search",
            "page_url": "https://example.com/page",
            "image_url": f"https://example.com/{candidate_id}.png",
            "rank": 1,
            "score": 0.99,
            "policy_decision": "allowed",
            "policy_flags": [],
            "candidate_status": status,
            "rejection_reason": None,
            "estimated_cost_usd": 0.01,
            "actual_cost_usd": 0.01,
        }

    def fetch_record(self, *, candidate: dict, fetch_id: str, evidence_image_id: str) -> dict:
        return {
            "fetch_id": fetch_id,
            "candidate_id": candidate["candidate_id"],
            "plan_id": candidate["plan_id"],
            "task_id": candidate["task_id"],
            "angle_id": candidate["angle_id"],
            "route": candidate["route"],
            "provider": candidate["provider"],
            "provider_kind": candidate["provider_kind"],
            "provider_mode": candidate["provider_mode"],
            "provider_run_id": candidate["provider_run_id"],
            "provider_provenance": deepcopy(candidate["provider_provenance"]),
            "fetch_status": "fetched",
            "http_status": 200,
            "mime_type": "image/png",
            "byte_size": 128,
            "width": 640,
            "height": 360,
            "hash": f"sha256:{candidate['candidate_id']}",
            "phash": f"phash:{candidate['candidate_id']}",
            "local_artifact_path": f"images/{evidence_image_id}.png",
            "evidence_image_id": evidence_image_id,
            "policy_decision": "allowed",
            "policy_flags": [],
            "failure_code": None,
            "estimated_cost_usd": 0.01,
            "actual_cost_usd": 0.01,
        }

    def observation_record(
        self,
        *,
        candidate: dict,
        fetch_id: str,
        evidence_image_id: str,
        claim_id: str | None,
        verifier_vote_id: str | None,
        provider: str,
        provider_kind: str,
        provider_mode: str,
    ) -> dict:
        verifier_links = []
        report_links = []
        if claim_id:
            lineage = {
                "plan_id": candidate["plan_id"],
                "task_id": candidate["task_id"],
                "angle_id": candidate["angle_id"],
                "route": candidate["route"],
                "candidate_id": candidate["candidate_id"],
                "fetch_id": fetch_id,
                "evidence_image_id": evidence_image_id,
            }
            verifier_links.append(
                {
                    "claim_id": claim_id,
                    "visual_support_ref": f"images.{evidence_image_id}.observations[0]",
                    "verifier_vote_id": verifier_vote_id,
                    **lineage,
                }
            )
            report_links.append(
                {
                    "claim_id": claim_id,
                    "report_section_id": "visual-findings",
                    "citation_id": f"img:{evidence_image_id}",
                    **lineage,
                }
            )
        return {
            "observation_id": f"obs_{evidence_image_id}",
            "evidence_image_id": evidence_image_id,
            "task_id": candidate["task_id"],
            "angle_id": candidate["angle_id"],
            "route": candidate["route"],
            "candidate_id": candidate["candidate_id"],
            "fetch_id": fetch_id,
            "plan_id": candidate["plan_id"],
            "provider": provider,
            "provider_kind": provider_kind,
            "provider_mode": provider_mode,
            "provider_run_id": f"run_{provider_mode}_vision_001",
            "provider_provenance": {
                "provider": provider,
                "provider_kind": provider_kind,
                "provider_mode": provider_mode,
            },
            "model_or_tool": provider,
            "observation_status": "analyzed",
            "observations": ["The fixture image contains visible visual evidence."],
            "inferences": ["The image supports the visual claim."],
            "confidence": 0.9,
            "policy_decision": "allowed",
            "policy_flags": [],
            "caveats": [],
            "verifier_links": verifier_links,
            "report_links": report_links,
            "estimated_cost_usd": 0.02,
            "actual_cost_usd": 0.02,
            "created_at": "2026-06-25T00:00:00Z",
        }

    def evidence_image(self, *, candidate: dict, fetch: dict, evidence_image_id: str) -> dict:
        return {
            "id": evidence_image_id,
            "plan_id": candidate["plan_id"],
            "task_id": candidate["task_id"],
            "angle_id": candidate["angle_id"],
            "route": candidate["route"],
            "candidate_id": candidate["candidate_id"],
            "fetch_id": fetch["fetch_id"],
            "local_artifact_path": fetch["local_artifact_path"],
            "hash": fetch["hash"],
            "provider": candidate["provider"],
            "provider_kind": candidate["provider_kind"],
            "provider_mode": candidate["provider_mode"],
            "provider_provenance": deepcopy(candidate["provider_provenance"]),
            "policy_decision": "allowed",
            "estimated_cost_usd": 0.03,
            "actual_cost_usd": 0.03,
            "observations": ["The fixture image contains visible visual evidence."],
        }

    def provider_status(
        self,
        *,
        provider: str,
        provider_kind: str,
        provider_mode: str,
        invocations: int,
        candidates_discovered: int,
        artifacts_fetched: int,
        vlm_images_analyzed: int,
    ) -> dict:
        return {
            "provider": provider,
            "provider_kind": provider_kind,
            "provider_mode": provider_mode,
            "configured": True,
            "available": True,
            "blocked_reason": None,
            "invocations": invocations,
            "candidates_discovered": candidates_discovered,
            "artifacts_fetched": artifacts_fetched,
            "vlm_images_analyzed": vlm_images_analyzed,
            "estimated_cost_usd": 0.01,
            "actual_cost_usd": 0.01,
            "last_error": None,
        }


if __name__ == "__main__":
    unittest.main()
