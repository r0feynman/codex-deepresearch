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
    visual_release_minimums,
)


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

    def candidate_record(
        self,
        *,
        candidate_id: str,
        task_id: str,
        angle_id: str,
        provider: str,
        provider_kind: str,
        provider_mode: str,
    ) -> dict:
        return {
            "candidate_id": candidate_id,
            "plan_id": "plan_visual_001",
            "task_id": task_id,
            "angle_id": angle_id,
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
            "candidate_status": "analyzed",
            "rejection_reason": None,
            "estimated_cost_usd": 0.01,
            "actual_cost_usd": 0.01,
        }

    def fetch_record(self, *, candidate: dict, fetch_id: str, evidence_image_id: str) -> dict:
        return {
            "fetch_id": fetch_id,
            "candidate_id": candidate["candidate_id"],
            "task_id": candidate["task_id"],
            "angle_id": candidate["angle_id"],
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
            verifier_links.append(
                {
                    "claim_id": claim_id,
                    "visual_support_ref": f"images.{evidence_image_id}.observations[0]",
                    "verifier_vote_id": verifier_vote_id,
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
            "fetch_id": fetch_id,
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
            "task_id": candidate["task_id"],
            "angle_id": candidate["angle_id"],
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
