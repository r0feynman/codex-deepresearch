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
FIXTURES = ROOT / "tests" / "fixtures" / "evidence_schema"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import validate_artifacts


class EvidenceSchemaValidatorTests(unittest.TestCase):
    def fixture(self, name: str) -> Path:
        return FIXTURES / name

    def load_valid_evidence(self) -> dict:
        return json.loads(self.fixture("valid_evidence.json").read_text(encoding="utf-8"))

    def load_search_result(self) -> dict:
        return json.loads(self.fixture("search_results.jsonl").read_text(encoding="utf-8"))

    def write_evidence(self, evidence: dict) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "evidence.json"
        path.write_text(json.dumps(evidence), encoding="utf-8")
        return path

    def write_jsonl(self, records: list) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "records.jsonl"
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        return path

    def assert_error_code(self, result, code: str) -> None:
        codes = {error.code for error in result.errors}
        self.assertIn(code, codes, [error.to_dict() for error in result.errors])

    def test_valid_fixture_and_adapter_records_pass(self) -> None:
        result = validate_artifacts(
            evidence_path=self.fixture("valid_evidence.json"),
            search_results_path=self.fixture("search_results.jsonl"),
            visual_observations_path=self.fixture("visual_observations.jsonl"),
            verifier_votes_path=self.fixture("verifier_votes.jsonl"),
        )

        self.assertTrue(result.valid, [error.to_dict() for error in result.errors])
        self.assertEqual(result.errors, ())

    def test_missing_required_state_fields_fails(self) -> None:
        evidence = self.load_valid_evidence()
        claim = evidence["claims"][0]
        del claim["verification_status"]
        del claim["review_status"]
        del claim["promotion_status"]

        result = validate_artifacts(evidence_path=self.write_evidence(evidence))

        self.assertFalse(result.valid)
        missing_paths = {error.path for error in result.errors}
        self.assertIn("$.evidence.claims[0].verification_status", missing_paths)
        self.assertIn("$.evidence.claims[0].review_status", missing_paths)
        self.assertIn("$.evidence.claims[0].promotion_status", missing_paths)

    def test_dangling_source_and_image_references_fail(self) -> None:
        evidence = self.load_valid_evidence()
        evidence["images"][0]["source_id"] = "src_missing"
        claim = evidence["claims"][1]
        claim["supporting_sources"] = ["src_missing"]
        claim["supporting_images"] = ["img_missing"]
        claim["quote_spans"] = [
            {
                "source_id": "src_missing",
                "quote": "Missing quote",
                "location": "paragraph 9",
            }
        ]
        claim["votes"][0]["evidence_refs"] = ["img_missing"]

        result = validate_artifacts(evidence_path=self.write_evidence(evidence))

        self.assertFalse(result.valid)
        self.assert_error_code(result, "dangling_reference")
        dangling_paths = {error.path for error in result.errors if error.code == "dangling_reference"}
        self.assertIn("$.evidence.images[0].source_id", dangling_paths)
        self.assertIn("$.evidence.claims[1].supporting_sources", dangling_paths)
        self.assertIn("$.evidence.claims[1].supporting_images", dangling_paths)
        self.assertIn("$.evidence.claims[1].quote_spans[0].source_id", dangling_paths)
        self.assertIn("$.evidence.claims[1].votes[0].evidence_refs", dangling_paths)

    def test_high_confidence_visual_claim_without_image_evidence_fails(self) -> None:
        evidence = self.load_valid_evidence()
        evidence["claims"][1]["supporting_images"] = []

        result = validate_artifacts(evidence_path=self.write_evidence(evidence))

        self.assertFalse(result.valid)
        self.assert_error_code(result, "missing_visual_evidence")

    def test_visual_support_observation_ref_must_match_image_observation(self) -> None:
        evidence = self.load_valid_evidence()
        support = evidence["claims"][1]["visual_supports"][0]
        support["observation_ref"] = "images.img_001.observations[1]"
        support["observation_index"] = 1
        support["observation_text"] = "Wrong observation"

        result = validate_artifacts(evidence_path=self.write_evidence(evidence))

        self.assertFalse(result.valid)
        self.assert_error_code(result, "invalid_observation_reference")

    def test_cli_outputs_json_for_pass_and_fail_cases(self) -> None:
        passing = subprocess.run(
            [
                str(RUNNER),
                "validate-evidence",
                "--evidence",
                str(self.fixture("valid_evidence.json")),
                "--search-results",
                str(self.fixture("search_results.jsonl")),
                "--visual-observations",
                str(self.fixture("visual_observations.jsonl")),
                "--verifier-votes",
                str(self.fixture("verifier_votes.jsonl")),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(passing.returncode, 0, passing.stderr)
        self.assertTrue(json.loads(passing.stdout)["valid"])

        evidence = self.load_valid_evidence()
        evidence["claims"][1]["supporting_images"] = []
        failing = subprocess.run(
            [
                str(RUNNER),
                "validate-evidence",
                "--evidence",
                str(self.write_evidence(evidence)),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(failing.returncode, 2)
        payload = json.loads(failing.stdout)
        self.assertFalse(payload["valid"])
        self.assertEqual(payload["errors"][0]["code"], "missing_visual_evidence")

    def test_search_result_task_id_must_reference_evidence_search_task(self) -> None:
        search_result = self.load_search_result()
        search_result["task_id"] = "task_missing"
        search_results_path = self.write_jsonl([search_result])

        standalone = validate_artifacts(search_results_path=search_results_path)
        self.assertTrue(standalone.valid, [error.to_dict() for error in standalone.errors])

        result = validate_artifacts(
            evidence_path=self.fixture("valid_evidence.json"),
            search_results_path=search_results_path,
        )

        self.assertFalse(result.valid)
        self.assert_error_code(result, "dangling_reference")
        self.assertIn(
            "$.search_results[0].task_id",
            {error.path for error in result.errors},
        )

    def test_search_result_angle_id_dangles_when_routing_empty(self) -> None:
        evidence = self.load_valid_evidence()
        evidence["routing"] = []

        result = validate_artifacts(
            evidence_path=self.write_evidence(evidence),
            search_results_path=self.fixture("search_results.jsonl"),
        )

        self.assertFalse(result.valid)
        self.assert_error_code(result, "dangling_reference")
        self.assertIn(
            "$.search_results[0].angle_id",
            {error.path for error in result.errors},
        )

    def test_top_level_verifier_vote_string_record_fails(self) -> None:
        votes_path = self.write_jsonl(["vote_text_001"])

        result = validate_artifacts(verifier_votes_path=votes_path)

        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0].path, "$.verifier_votes[0]")
        self.assertEqual(result.errors[0].code, "invalid_type")

        cli = subprocess.run(
            [str(RUNNER), "validate-evidence", "--verifier-votes", str(votes_path)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(cli.returncode, 2)
        payload = json.loads(cli.stdout)
        self.assertFalse(payload["valid"])
        self.assertEqual(payload["errors"][0]["path"], "$.verifier_votes[0]")
        self.assertEqual(payload["errors"][0]["code"], "invalid_type")

    def test_top_level_verifier_vote_claim_id_dangles_when_claims_empty(self) -> None:
        evidence = self.load_valid_evidence()
        evidence["claims"] = []

        standalone = validate_artifacts(verifier_votes_path=self.fixture("verifier_votes.jsonl"))
        self.assertTrue(standalone.valid, [error.to_dict() for error in standalone.errors])

        result = validate_artifacts(
            evidence_path=self.write_evidence(evidence),
            verifier_votes_path=self.fixture("verifier_votes.jsonl"),
        )

        self.assertFalse(result.valid)
        self.assert_error_code(result, "dangling_reference")
        self.assertIn(
            "$.verifier_votes[0].claim_id",
            {error.path for error in result.errors},
        )

    def test_verifier_vote_evidence_refs_dangle_when_evidence_refs_are_empty(self) -> None:
        evidence = self.load_valid_evidence()
        evidence["sources"] = []
        evidence["images"] = []
        del evidence["claims"]

        result = validate_artifacts(
            evidence_path=self.write_evidence(evidence),
            verifier_votes_path=self.fixture("verifier_votes.jsonl"),
        )

        self.assertFalse(result.valid)
        self.assert_error_code(result, "dangling_reference")
        dangling_paths = {error.path for error in result.errors if error.code == "dangling_reference"}
        self.assertIn("$.verifier_votes[0].evidence_refs", dangling_paths)
        self.assertIn("$.verifier_votes[1].evidence_refs", dangling_paths)

    def test_evidence_image_source_id_dangles_when_sources_empty(self) -> None:
        evidence = self.load_valid_evidence()
        evidence["sources"] = []
        evidence["claims"] = []

        result = validate_artifacts(evidence_path=self.write_evidence(evidence))

        self.assertFalse(result.valid)
        self.assert_error_code(result, "dangling_reference")
        self.assertIn(
            "$.evidence.images[0].source_id",
            {error.path for error in result.errors},
        )

    def test_embedded_vote_string_refs_resolve_against_top_level_votes(self) -> None:
        evidence = self.load_valid_evidence()
        evidence["claims"][0]["votes"] = ["vote_text_001"]
        evidence["claims"][1]["votes"] = ["vote_visual_001"]

        result = validate_artifacts(
            evidence_path=self.write_evidence(evidence),
            verifier_votes_path=self.fixture("verifier_votes.jsonl"),
        )

        self.assertTrue(result.valid, [error.to_dict() for error in result.errors])

    def test_missing_embedded_vote_string_ref_fails_when_votes_are_provided(self) -> None:
        evidence = self.load_valid_evidence()
        evidence["claims"][0]["votes"] = ["vote_missing"]
        evidence["claims"][1]["votes"] = ["vote_visual_001"]

        result = validate_artifacts(
            evidence_path=self.write_evidence(evidence),
            verifier_votes_path=self.fixture("verifier_votes.jsonl"),
        )

        self.assertFalse(result.valid)
        self.assert_error_code(result, "dangling_reference")
        self.assertIn(
            "$.evidence.claims[0].votes[0]",
            {error.path for error in result.errors},
        )

    def test_source_and_image_url_fields_require_strings(self) -> None:
        evidence = self.load_valid_evidence()
        evidence["sources"][0]["url"] = 123
        evidence["sources"][0]["title"] = ["not", "a", "string"]
        evidence["images"][0]["page_url"] = 123
        evidence["images"][0]["image_url"] = ["not", "a", "string"]
        evidence["claims"] = []

        result = validate_artifacts(evidence_path=self.write_evidence(evidence))

        self.assertFalse(result.valid)
        invalid_paths = {error.path for error in result.errors if error.code == "invalid_type"}
        self.assertIn("$.evidence.sources[0].url", invalid_paths)
        self.assertIn("$.evidence.sources[0].title", invalid_paths)
        self.assertIn("$.evidence.images[0].page_url", invalid_paths)
        self.assertIn("$.evidence.images[0].image_url", invalid_paths)
        self.assertIn(
            "$.evidence.images[0]",
            {error.path for error in result.errors if error.code == "missing_required_field"},
        )


if __name__ == "__main__":
    unittest.main()
