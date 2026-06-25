from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import (  # noqa: E402
    acquire_visual_candidates,
    ingest_vision_observations,
    prepare_run,
    validate_artifacts,
    validate_visual_artifacts,
)


class PdfRasterizerTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def read_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def write_pdf(self, run_dir: Path, name: str, body: bytes = b"") -> Path:
        path = run_dir / "sources" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n"
            + body
            + b"\n%%EOF\n"
        )
        return path

    def pdf_source(self, run_dir: Path, source_id: str, pdf_path: Path, **overrides: object) -> dict:
        source = {
            "id": source_id,
            "type": "pdf",
            "url": pdf_path.resolve().as_uri(),
            "title": f"PDF fixture {source_id}",
            "published_at": None,
            "accessed_at": "2026-06-25T00:00:00Z",
            "quality": "primary",
            "retrieval_status": "fetched",
            "local_artifact_path": pdf_path.relative_to(run_dir).as_posix(),
            "license_policy": "allowed",
            "robots_policy": "allowed",
            "policy_decision": "allowed",
            "policy_flags": [],
            "route": "visual_required",
            "angle_id": "angle_001",
            "search_result_id": f"sr_{source_id}",
            "pdf_pages": [1],
        }
        source.update(overrides)
        return source

    def test_allowed_pdf_pages_and_figures_link_to_visual_evidence(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepared = prepare_run(
            question="Extract public PDF chart figures",
            runs_dir=runs_dir,
            route="visual_required",
            max_images=4,
        )
        run_dir = Path(prepared["run_dir"])
        pdf_path = self.write_pdf(run_dir, "sample-public.pdf")
        source = self.pdf_source(
            run_dir,
            "src_pdf_public",
            pdf_path,
            pdf_pages=[1, 2],
            figure_hints=[
                {
                    "page_number": 2,
                    "label": "Figure 1",
                    "caption": "Public-safe fixture chart",
                }
            ],
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [source]
        evidence["budget"]["max_images"] = 4
        self.write_json(run_dir / "evidence.json", evidence)

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["local-pdf-rasterizer"],
        )

        self.assertEqual(result["status"], "visual_candidates_collected")
        self.assertTrue(result["validation"]["valid"], result["validation"]["errors"])
        self.assertTrue(result["visual_artifact_validation"]["valid"], result["visual_artifact_validation"]["errors"])
        self.assertEqual(result["candidate_counts"], {"pdf_page": 2, "pdf_figure": 1})
        self.assertEqual(result["pdf_rasterization"]["pages_rasterized"], 3)

        plan = self.read_json(run_dir / "visual_search_plan.json")
        self.assertEqual(plan["tasks"][0]["target_evidence_type"], "pdf_figure")
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        self.assertEqual({candidate["origin"] for candidate in candidates}, {"pdf_page", "pdf_figure"})
        self.assertEqual({fetch["fetch_status"] for fetch in fetches}, {"fetched"})
        self.assertTrue(all(fetch["hash"].startswith("sha256:") for fetch in fetches))
        self.assertTrue(all(fetch["pdf_url"] == source["url"] for fetch in fetches))
        self.assertEqual(
            next(fetch for fetch in fetches if fetch.get("figure_hint"))["figure_hint"]["label"],
            "Figure 1",
        )

        ingest = ingest_vision_observations(run=run_dir, provider="codex-interactive")
        self.assertEqual(ingest["status"], "visual_evidence_ingested")
        validation = validate_artifacts(
            evidence_path=run_dir / "evidence.json",
            visual_observations_path=run_dir / "visual_observations.jsonl",
        )
        self.assertTrue(validation.valid, [error.to_dict() for error in validation.errors])

        evidence = self.read_json(run_dir / "evidence.json")
        self.assertEqual({image["origin"] for image in evidence["images"]}, {"pdf_page", "pdf_figure"})
        figure = next(image for image in evidence["images"] if image["origin"] == "pdf_figure")
        self.assertEqual(figure["page_number"], 2)
        self.assertEqual(figure["figure_hint"]["label"], "Figure 1")
        self.assertEqual(figure["provider_kind"], "pdf_rasterizer")
        self.assertEqual(figure["pdf_url"], source["url"])
        self.assertTrue(figure["hash"].startswith("sha256:"))

    def test_pdf_diagnostics_are_explicit_for_blocked_and_unsupported_sources(self) -> None:
        prepared = prepare_run(
            question="Diagnose PDF rasterization blockers",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
            max_images=8,
        )
        run_dir = Path(prepared["run_dir"])
        sources_dir = run_dir / "sources"
        blocked_pdf = self.write_pdf(run_dir, "blocked.pdf")
        paywalled_pdf = self.write_pdf(run_dir, "paywalled.pdf")
        encrypted_pdf = self.write_pdf(run_dir, "encrypted.pdf", b"/Encrypt <<>>\n")
        too_large_pdf = self.write_pdf(run_dir, "too-large.pdf", b"x" * 1024)
        unsupported_pdf = sources_dir / "unsupported.pdf"
        unsupported_pdf.write_bytes(b"not a pdf\n")

        sources = [
            self.pdf_source(
                run_dir,
                "src_policy_blocked",
                blocked_pdf,
                policy_decision="blocked",
                policy_flags=["robots_disallowed"],
            ),
            self.pdf_source(
                run_dir,
                "src_paywalled",
                paywalled_pdf,
                policy_flags=["paywalled"],
            ),
            self.pdf_source(run_dir, "src_encrypted", encrypted_pdf),
            self.pdf_source(run_dir, "src_too_large", too_large_pdf),
            self.pdf_source(run_dir, "src_unsupported", unsupported_pdf),
        ]
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = sources
        evidence["budget"]["max_images"] = 8
        self.write_json(run_dir / "evidence.json", evidence)

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["local-pdf-rasterizer"],
            max_pdf_bytes=300,
        )

        self.assertTrue(result["visual_artifact_validation"]["valid"], result["visual_artifact_validation"]["errors"])
        self.assertTrue(result["validation"]["valid"], result["validation"]["errors"])
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        by_code = {fetch["failure_code"]: fetch for fetch in fetches}
        self.assertEqual(
            set(by_code),
            {
                "policy_blocked_pdf",
                "paywalled_pdf",
                "encrypted_pdf",
                "too_large_pdf",
                "unsupported_pdf",
            },
        )
        self.assertEqual(by_code["policy_blocked_pdf"]["fetch_status"], "policy_blocked")
        self.assertEqual(by_code["paywalled_pdf"]["fetch_status"], "policy_blocked")
        self.assertEqual(by_code["too_large_pdf"]["fetch_status"], "too_large")
        self.assertEqual(by_code["encrypted_pdf"]["fetch_status"], "failed")
        self.assertEqual(by_code["unsupported_pdf"]["fetch_status"], "failed")
        diagnostics = result["pdf_rasterization"]["diagnostics"]
        self.assertEqual({item["failure_code"] for item in diagnostics}, set(by_code))

    def test_pdf_budget_pruning_records_skipped_pages(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepared = prepare_run(
            question="Budget-pruned PDF page rasterization",
            runs_dir=runs_dir,
            route="visual_required",
            max_images=1,
        )
        run_dir = Path(prepared["run_dir"])
        pdf_path = self.write_pdf(run_dir, "budget.pdf")
        source = self.pdf_source(
            run_dir,
            "src_pdf_budget",
            pdf_path,
            pdf_pages=[1, 2, 3],
        )
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = [source]
        evidence["budget"]["max_images"] = 1
        self.write_json(run_dir / "evidence.json", evidence)

        result = acquire_visual_candidates(
            run=run_dir,
            providers=["local-pdf-rasterizer"],
        )

        self.assertEqual(result["selected_observations"], 1)
        self.assertTrue(result["validation"]["valid"], result["validation"]["errors"])
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        self.assertEqual(
            [fetch["fetch_status"] for fetch in fetches],
            ["fetched", "budget_pruned", "budget_pruned"],
        )
        self.assertEqual(
            [fetch["failure_code"] for fetch in fetches],
            [None, "budget_pruned", "budget_pruned"],
        )
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        pruned_pages = [
            candidate["page_number"]
            for candidate in candidates
            if candidate["candidate_status"] == "budget_pruned"
        ]
        self.assertEqual(pruned_pages, [2, 3])
        self.assertEqual(result["pdf_rasterization"]["pages_skipped"], 2)
        visual_validation = validate_visual_artifacts(run_dir=run_dir, evidence_path=None)
        self.assertTrue(visual_validation.valid, [error.to_dict() for error in visual_validation.errors])


if __name__ == "__main__":
    unittest.main()
