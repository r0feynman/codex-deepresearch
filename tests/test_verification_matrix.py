from __future__ import annotations

import importlib
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

from deepresearch import validate_artifacts, verify_claims

verification_matrix_module = importlib.import_module("deepresearch.verification_matrix")


class VerificationMatrixTests(unittest.TestCase):
    def temp_run(self, route: str = "text_only") -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        run_dir = Path(temp_dir.name) / "matrix-run"
        run_dir.mkdir()
        self.write_json(run_dir / "evidence.json", self.base_evidence(route=route))
        return run_dir

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def load_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def load_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def base_evidence(self, route: str = "text_only") -> dict:
        return {
            "schema_version": "0.1.0",
            "run_id": "matrix-run",
            "created_at": "2026-06-22T00:00:00Z",
            "question": "Verification matrix test",
            "mode": "codex-plugin",
            "search_provider": "codex-native",
            "vlm_provider": "codex-interactive",
            "routing": [
                {
                    "id": "angle_001",
                    "angle": "primary source check",
                    "modality": route,
                    "reason": "test route",
                    "visual_tasks": ["image_claim_alignment"] if route != "text_only" else [],
                    "max_images": 1 if route != "text_only" else 0,
                }
            ],
            "search_tasks": [
                {
                    "id": "task_search_001",
                    "angle_id": "angle_001",
                    "query": "Verification matrix test",
                    "freshness_requirement": "any",
                    "route": route,
                    "max_results": 5,
                    "source_policy": "allowed",
                }
            ],
            "sources": [self.source(route=route)],
            "images": [],
            "claims": [],
        }

    def source(self, *, source_id: str = "src_001", route: str = "text_only") -> dict:
        return {
            "id": source_id,
            "type": "web",
            "url": "https://example.com/source",
            "title": "Example Source",
            "published_at": None,
            "accessed_at": "2026-06-22T00:00:00Z",
            "quality": "primary",
            "retrieval_status": "fetched",
            "local_artifact_path": f"sources/{source_id}.html",
            "license_policy": "allowed",
            "robots_policy": "allowed",
            "policy_decision": "allowed",
            "policy_flags": [],
            "angle_id": "angle_001",
            "route": route,
        }

    def image(
        self,
        *,
        image_id: str = "img_001",
        origin: str = "screenshot",
        image_url: str | None = None,
        local_artifact_path: str | None = None,
    ) -> dict:
        return {
            "id": image_id,
            "source_id": "src_001",
            "origin": origin,
            "image_url": image_url,
            "page_url": "https://example.com/source",
            "local_artifact_path": local_artifact_path or f"images/{image_id}.png",
            "mime_type": "image/png",
            "width": 640,
            "height": 480,
            "hash": "sha256:test",
            "phash": None,
            "ocr_text": "Visible Example text",
            "observations": ["Visible Example text is present."],
            "inferences": ["The image supports the visual claim."],
            "visual_tasks": ["image_claim_alignment"],
            "analysis_provider": "codex-interactive",
            "analysis_status": "analyzed",
            "policy_flags": [],
            "caveats": [],
        }

    def claim(
        self,
        *,
        claim_id: str = "claim_001",
        claim_type: str = "text",
        supporting_sources: list[str] | None = None,
        supporting_images: list[str] | None = None,
        quote_spans: list[dict] | None = None,
        votes: list[dict] | None = None,
        verification_status: str = "unverified",
        review_status: str = "not_reviewed",
        promotion_status: str = "not_eligible",
    ) -> dict:
        return {
            "id": claim_id,
            "text": "The source says Example.",
            "claim_type": claim_type,
            "supporting_sources": supporting_sources if supporting_sources is not None else ["src_001"],
            "supporting_images": supporting_images if supporting_images is not None else [],
            "quote_spans": quote_spans
            if quote_spans is not None
            else [
                {
                    "source_id": "src_001",
                    "quote": "Example",
                    "location": "paragraph 1",
                }
            ],
            "votes": votes if votes is not None else [],
            "verification_status": verification_status,
            "review_status": review_status,
            "promotion_status": promotion_status,
            "confidence": "low",
            "caveats": [],
            "angle_id": "angle_001",
        }

    def assert_valid_run(self, run_dir: Path) -> dict:
        result = validate_artifacts(
            evidence_path=run_dir / "evidence.json",
            verifier_votes_path=run_dir / "verifier_votes.jsonl",
        )
        self.assertTrue(result.valid, [error.to_dict() for error in result.errors])
        return self.load_json(run_dir / "evidence.json")

    def votes_for(self, run_dir: Path, claim_id: str) -> list[dict]:
        return [vote for vote in self.load_jsonl(run_dir / "verifier_votes.jsonl") if vote["claim_id"] == claim_id]

    def test_text_only_claim_gets_two_text_votes_and_policy_vote(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [self.claim(claim_id="claim_text")]
        self.write_json(run_dir / "evidence.json", evidence)

        result = verify_claims(run=run_dir)

        self.assertEqual(result["status"], "completed")
        evidence = self.assert_valid_run(run_dir)
        claim = evidence["claims"][0]
        votes = self.votes_for(run_dir, "claim_text")
        self.assertEqual([vote["verifier_type"] for vote in votes].count("text"), 2)
        self.assertEqual([vote["verifier_type"] for vote in votes].count("policy"), 1)
        self.assertNotIn("visual", [vote["verifier_type"] for vote in votes])
        self.assertEqual(claim["verification_status"], "supported")
        self.assertEqual(claim["confidence"], "medium")

    def test_visual_required_claim_gets_visual_vote(self) -> None:
        run_dir = self.temp_run(route="visual_required")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["images"] = [self.image()]
        evidence["claims"] = [
            self.claim(
                claim_id="claim_visual",
                claim_type="visual",
                supporting_images=["img_001"],
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        verify_claims(run=run_dir)

        evidence = self.assert_valid_run(run_dir)
        claim = evidence["claims"][0]
        votes = self.votes_for(run_dir, "claim_visual")
        self.assertGreaterEqual([vote["verifier_type"] for vote in votes].count("visual"), 1)
        self.assertEqual(claim["verification_status"], "supported")
        self.assertEqual(claim["confidence"], "medium")

    def test_two_refute_votes_set_verification_status_refuted(self) -> None:
        refute_votes = [
            {
                "id": f"manual_refute_{index}",
                "claim_id": "claim_refuted",
                "verifier_type": "text",
                "agent_name": f"manual_refuter_{index}",
                "method": "manual-review",
                "model_or_tool": "human",
                "vote": "refute",
                "confidence": 0.9,
                "evidence_refs": ["src_001"],
                "rationale": "Manual review refutes the claim.",
                "created_at": "2026-06-22T00:00:00Z",
            }
            for index in (1, 2)
        ]
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [self.claim(claim_id="claim_refuted", votes=refute_votes)]
        self.write_json(run_dir / "evidence.json", evidence)

        verify_claims(run=run_dir)

        evidence = self.assert_valid_run(run_dir)
        claim = evidence["claims"][0]
        self.assertEqual(claim["verification_status"], "refuted")
        self.assertEqual(claim["promotion_status"], "not_eligible")
        self.assertFalse(claim["include_in_final_report"])
        self.assertEqual(claim["report_exclusion_reason"], "refuted")

    def test_budget_pruned_claim_is_excluded_from_final_report(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [
            self.claim(
                claim_id="claim_pruned",
                verification_status="budget_pruned",
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        result = verify_claims(run=run_dir)

        evidence = self.assert_valid_run(run_dir)
        claim = evidence["claims"][0]
        self.assertEqual(claim["verification_status"], "budget_pruned")
        self.assertFalse(claim["include_in_final_report"])
        self.assertEqual(claim["report_exclusion_reason"], "budget_pruned")
        self.assertEqual(self.votes_for(run_dir, "claim_pruned"), [])
        status = result["claim_statuses"][0]
        self.assertFalse(status["include_in_final_report"])
        self.assertEqual(status["generated_vote_count"], 0)

    def test_visual_required_without_usable_image_needs_visual_evidence(self) -> None:
        run_dir = self.temp_run(route="visual_required")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [
            self.claim(
                claim_id="claim_missing_visual",
                claim_type="visual",
                supporting_images=[],
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        verify_claims(run=run_dir)

        evidence = self.assert_valid_run(run_dir)
        claim = evidence["claims"][0]
        votes = self.votes_for(run_dir, "claim_missing_visual")
        visual_votes = [vote for vote in votes if vote["verifier_type"] == "visual"]
        self.assertEqual(claim["verification_status"], "needs_visual_evidence")
        self.assertEqual(claim["confidence"], "low")
        self.assertFalse(claim["include_in_final_report"])
        self.assertEqual(claim["report_exclusion_reason"], "needs_visual_evidence")
        self.assertEqual(len(visual_votes), 1)
        self.assertEqual(visual_votes[0]["vote"], "uncertain")

    def test_quote_less_text_claim_becomes_insufficient_evidence(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [self.claim(claim_id="claim_no_quote", quote_spans=[])]
        self.write_json(run_dir / "evidence.json", evidence)

        verify_claims(run=run_dir)

        evidence = self.assert_valid_run(run_dir)
        claim = evidence["claims"][0]
        text_votes = [vote for vote in self.votes_for(run_dir, "claim_no_quote") if vote["verifier_type"] == "text"]
        self.assertEqual(claim["verification_status"], "insufficient_evidence")
        self.assertEqual(claim["confidence"], "low")
        self.assertFalse(claim["include_in_final_report"])
        self.assertEqual(claim["report_exclusion_reason"], "insufficient_evidence")
        self.assertTrue(all(vote["vote"] == "uncertain" for vote in text_votes))

    def test_visual_optional_visual_claim_without_image_is_not_supported(self) -> None:
        run_dir = self.temp_run(route="visual_optional")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [
            self.claim(
                claim_id="claim_optional_missing_image",
                claim_type="visual",
                supporting_images=[],
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        verify_claims(run=run_dir)

        evidence = self.assert_valid_run(run_dir)
        claim = evidence["claims"][0]
        self.assertEqual(claim["verification_status"], "needs_visual_evidence")
        self.assertEqual(claim["promotion_status"], "not_eligible")
        self.assertFalse(claim["include_in_final_report"])

    def test_page_image_without_image_url_or_screenshot_capture_is_not_usable(self) -> None:
        run_dir = self.temp_run(route="visual_required")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["images"] = [self.image(origin="page_image", image_url=None)]
        evidence["claims"] = [
            self.claim(
                claim_id="claim_page_image_missing_asset",
                claim_type="visual",
                supporting_images=["img_001"],
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        verify_claims(run=run_dir)

        evidence = self.assert_valid_run(run_dir)
        claim = evidence["claims"][0]
        visual_votes = [
            vote
            for vote in self.votes_for(run_dir, "claim_page_image_missing_asset")
            if vote["verifier_type"] == "visual"
        ]
        self.assertEqual(claim["verification_status"], "needs_visual_evidence")
        self.assertEqual(claim["promotion_status"], "not_eligible")
        self.assertFalse(claim["include_in_final_report"])
        self.assertEqual(visual_votes[0]["vote"], "uncertain")

    def test_human_or_promotion_rejected_claim_is_not_repromoted(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [
            self.claim(
                claim_id="claim_rejected",
                review_status="human_rejected",
                promotion_status="promotion_rejected",
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        verify_claims(run=run_dir)

        evidence = self.assert_valid_run(run_dir)
        claim = evidence["claims"][0]
        self.assertEqual(claim["verification_status"], "supported")
        self.assertEqual(claim["review_status"], "human_rejected")
        self.assertEqual(claim["promotion_status"], "promotion_rejected")
        self.assertFalse(claim["include_in_final_report"])
        self.assertEqual(claim["report_exclusion_reason"], "human_rejected")

    def test_promotion_rejected_claim_without_human_rejection_is_not_auto_reviewed(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [
            self.claim(
                claim_id="claim_promotion_rejected",
                promotion_status="promotion_rejected",
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        verify_claims(run=run_dir)

        evidence = self.assert_valid_run(run_dir)
        claim = evidence["claims"][0]
        self.assertEqual(claim["verification_status"], "supported")
        self.assertEqual(claim["review_status"], "needs_more_evidence")
        self.assertEqual(claim["promotion_status"], "promotion_rejected")
        self.assertFalse(claim["include_in_final_report"])
        self.assertEqual(claim["report_exclusion_reason"], "promotion_rejected")

    def test_human_accepted_promotion_rejected_claim_preserves_promotion_rejection(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [
            self.claim(
                claim_id="claim_human_accepted_promotion_rejected",
                review_status="human_accepted",
                promotion_status="promotion_rejected",
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        verify_claims(run=run_dir)

        evidence = self.assert_valid_run(run_dir)
        claim = evidence["claims"][0]
        self.assertEqual(claim["verification_status"], "supported")
        self.assertEqual(claim["review_status"], "human_accepted")
        self.assertEqual(claim["promotion_status"], "promotion_rejected")
        self.assertFalse(claim["include_in_final_report"])
        self.assertEqual(claim["report_exclusion_reason"], "promotion_rejected")

    def test_verify_claims_is_idempotent_for_matrix_votes(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [self.claim(claim_id="claim_repeat")]
        self.write_json(run_dir / "evidence.json", evidence)

        with mock.patch.object(
            verification_matrix_module,
            "_utc_now",
            return_value="2026-06-22T00:00:00Z",
        ):
            verify_claims(run=run_dir)
        first_votes = self.votes_for(run_dir, "claim_repeat")

        with mock.patch.object(
            verification_matrix_module,
            "_utc_now",
            return_value="2026-06-22T00:05:00Z",
        ):
            result = verify_claims(run=run_dir)
        second_votes = self.votes_for(run_dir, "claim_repeat")

        first_vote_ids = [vote["id"] for vote in first_votes]
        second_vote_ids = [vote["id"] for vote in second_votes]
        self.assertEqual(first_vote_ids, second_vote_ids)
        self.assertEqual(len(second_vote_ids), len(set(second_vote_ids)))
        self.assertEqual({vote["created_at"] for vote in second_votes}, {"2026-06-22T00:00:00Z"})
        self.assertEqual(result["claims_reused"], 1)
        self.assertTrue(result["claim_statuses"][0]["cache_hit"])
        evidence = self.assert_valid_run(run_dir)
        self.assertEqual(len(evidence["claims"][0]["votes"]), 3)

    def test_changed_claim_text_invalidates_verification_cache(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [self.claim(claim_id="claim_changed_text")]
        self.write_json(run_dir / "evidence.json", evidence)

        with mock.patch.object(
            verification_matrix_module,
            "_utc_now",
            return_value="2026-06-22T00:00:00Z",
        ):
            verify_claims(run=run_dir)

        evidence = self.load_json(run_dir / "evidence.json")
        old_cache_key = evidence["claims"][0]["verification_cache_key"]
        evidence["claims"][0]["text"] = "The changed source says Example."
        self.write_json(run_dir / "evidence.json", evidence)

        with mock.patch.object(
            verification_matrix_module,
            "_utc_now",
            return_value="2026-06-22T00:10:00Z",
        ):
            result = verify_claims(run=run_dir)

        evidence = self.assert_valid_run(run_dir)
        claim = evidence["claims"][0]
        self.assertNotEqual(claim["verification_cache_key"], old_cache_key)
        self.assertEqual(result["claims_reused"], 0)
        self.assertFalse(result["claim_statuses"][0]["cache_hit"])
        self.assertEqual(
            {vote["created_at"] for vote in self.votes_for(run_dir, "claim_changed_text")},
            {"2026-06-22T00:10:00Z"},
        )

    def test_changed_source_policy_invalidates_verification_cache(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [self.claim(claim_id="claim_changed_policy")]
        self.write_json(run_dir / "evidence.json", evidence)

        with mock.patch.object(
            verification_matrix_module,
            "_utc_now",
            return_value="2026-06-22T00:00:00Z",
        ):
            verify_claims(run=run_dir)

        evidence = self.load_json(run_dir / "evidence.json")
        old_cache_key = evidence["claims"][0]["verification_cache_key"]
        evidence["sources"][0]["policy_decision"] = "blocked"
        evidence["sources"][0]["policy_flags"] = ["robots_disallowed"]
        self.write_json(run_dir / "evidence.json", evidence)

        with mock.patch.object(
            verification_matrix_module,
            "_utc_now",
            return_value="2026-06-22T00:15:00Z",
        ):
            result = verify_claims(run=run_dir)

        evidence = self.assert_valid_run(run_dir)
        claim = evidence["claims"][0]
        self.assertNotEqual(claim["verification_cache_key"], old_cache_key)
        self.assertEqual(claim["verification_status"], "policy_blocked")
        self.assertEqual(result["claims_reused"], 0)
        self.assertEqual(
            {vote["created_at"] for vote in self.votes_for(run_dir, "claim_changed_policy")},
            {"2026-06-22T00:15:00Z"},
        )

    def test_changed_visual_evidence_invalidates_verification_cache(self) -> None:
        run_dir = self.temp_run(route="visual_required")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["images"] = [self.image()]
        evidence["claims"] = [
            self.claim(
                claim_id="claim_changed_image",
                claim_type="visual",
                supporting_images=["img_001"],
            )
        ]
        self.write_json(run_dir / "evidence.json", evidence)

        with mock.patch.object(
            verification_matrix_module,
            "_utc_now",
            return_value="2026-06-22T00:00:00Z",
        ):
            verify_claims(run=run_dir)

        evidence = self.load_json(run_dir / "evidence.json")
        old_cache_key = evidence["claims"][0]["verification_cache_key"]
        evidence["images"][0]["hash"] = "sha256:changed"
        self.write_json(run_dir / "evidence.json", evidence)

        with mock.patch.object(
            verification_matrix_module,
            "_utc_now",
            return_value="2026-06-22T00:20:00Z",
        ):
            result = verify_claims(run=run_dir)

        evidence = self.assert_valid_run(run_dir)
        self.assertNotEqual(evidence["claims"][0]["verification_cache_key"], old_cache_key)
        self.assertEqual(result["claims_reused"], 0)
        self.assertEqual(
            {vote["created_at"] for vote in self.votes_for(run_dir, "claim_changed_image")},
            {"2026-06-22T00:20:00Z"},
        )

    def test_cli_verify_claims_outputs_status_json(self) -> None:
        run_dir = self.temp_run(route="text_only")
        evidence = self.load_json(run_dir / "evidence.json")
        evidence["claims"] = [self.claim(claim_id="claim_cli")]
        self.write_json(run_dir / "evidence.json", evidence)

        command = subprocess.run(
            [str(RUNNER), "verify-claims", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["votes_written"], 3)
        self.assert_valid_run(run_dir)


if __name__ == "__main__":
    unittest.main()
