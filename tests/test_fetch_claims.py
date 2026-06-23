from __future__ import annotations

import json
import importlib
import subprocess
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "plugins" / "codex-deepresearch" / "scripts" / "codex-deepresearch"
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import fetch_claims, ingest_run, prepare_run, validate_artifacts


fetch_claims_module = importlib.import_module("deepresearch.fetch_claims")


class FakeResponse:
    def __init__(
        self,
        content: bytes,
        *,
        mime_type: str,
        status: int = 200,
        url: str = "https://example.com/source",
    ) -> None:
        self._content = content
        self.status = status
        self.headers = Message()
        self.headers.set_type(mime_type)
        self._url = url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._content

    def geturl(self) -> str:
        return self._url


class FetchClaimsTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def load_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")

    def base_search_result(self, **overrides) -> dict:
        result = {
            "id": "sr_001",
            "task_id": "task_search_001",
            "angle_id": "angle_001",
            "route": "text_only",
            "provider": "codex-native",
            "query": "example search question",
            "url": "https://example.com/source",
            "title": "Example Source",
            "snippet": "Example snippet",
            "result_type": "web",
            "rank": 1,
            "freshness_requirement": "any",
            "published_at": None,
            "accessed_at": "2026-06-22T00:00:00Z",
            "language": "en",
            "region": "US",
            "policy_decision": "allowed",
            "policy_flags": [],
            "raw_provider_metadata": {},
        }
        result.update(overrides)
        return result

    def write_search_results(self, run_dir: Path, records: list[dict]) -> None:
        payload = "\n".join(json.dumps(record) for record in records) + "\n"
        (run_dir / "search_results.jsonl").write_text(payload, encoding="utf-8")

    def prepare_ingested_run(self, records: list[dict]) -> Path:
        prepared = prepare_run(
            question="example search question",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])
        self.write_search_results(run_dir, records)
        ingest = ingest_run(run=run_dir)
        self.assertTrue(ingest["validation"]["valid"], ingest["validation"]["errors"])
        return run_dir

    def prepare_queued_file_run(self, source_url: str) -> Path:
        prepared = prepare_run(
            question="example file question",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])
        evidence_path = run_dir / "evidence.json"
        evidence = self.load_json(evidence_path)
        source = {
            "id": "src_file",
            "type": "web",
            "url": source_url,
            "title": "Queued File",
            "published_at": None,
            "accessed_at": "2026-06-22T00:00:00Z",
            "quality": "unknown",
            "retrieval_status": "partial",
            "local_artifact_path": "sources/src_file.json",
            "license_policy": "allowed",
            "robots_policy": "allowed",
            "policy_decision": "allowed",
            "policy_flags": [],
        }
        evidence["sources"] = [source]
        self.write_json(evidence_path, evidence)
        (run_dir / "sources").mkdir(exist_ok=True)
        self.write_json(run_dir / "sources/src_file.json", source)
        self.write_json(
            run_dir / "fetch_queue.json",
            {
                "schema_version": "codex-deepresearch.fetch-queue.v0",
                "run_id": evidence["run_id"],
                "created_at": "2026-06-22T00:00:00Z",
                "entries": [
                    {
                        "source_id": "src_file",
                        "url": source_url,
                        "type": "web",
                        "title": "Queued File",
                        "policy_decision": "allowed",
                        "policy_flags": [],
                        "retrieval_status": "queued",
                    }
                ],
            },
        )
        return run_dir

    def test_allowed_html_fetches_artifact_quotes_and_low_confidence_claims(self) -> None:
        run_dir = self.prepare_ingested_run([self.base_search_result()])
        html = b"""
        <html>
          <head><title>Fetched HTML Title</title></head>
          <body>
            <p>The source explains that deterministic fetch tests should preserve source artifacts.</p>
            <p>Every extracted text claim must stay linked to the source record that produced it.</p>
          </body>
        </html>
        """

        with mock.patch.object(
            fetch_claims_module,
            "urlopen",
            return_value=FakeResponse(html, mime_type="text/html"),
        ):
            result = fetch_claims(run=run_dir)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["sources_fetched"], 1)
        self.assertEqual(result["high_confidence_claims_created"], 0)
        evidence = self.load_json(run_dir / "evidence.json")
        source = evidence["sources"][0]
        self.assertEqual(source["retrieval_status"], "fetched")
        self.assertEqual(source["title"], "Fetched HTML Title")
        self.assertIn("deterministic fetch tests", source["body_excerpt"])
        self.assertTrue(source["local_artifact_path"].endswith(".html"))
        self.assertTrue((run_dir / source["local_artifact_path"]).exists())
        self.assertGreaterEqual(len(evidence["quote_candidates"]), 1)
        self.assertGreaterEqual(len(evidence["claims"]), 1)

        source_ids = {source["id"] for source in evidence["sources"]}
        for claim in evidence["claims"]:
            self.assertEqual(claim["confidence"], "low")
            self.assertEqual(claim["verification_status"], "unverified")
            self.assertEqual(claim["review_status"], "not_reviewed")
            self.assertEqual(claim["promotion_status"], "not_eligible")
            self.assertTrue(set(claim["supporting_sources"]).issubset(source_ids))
            self.assertIn(claim["quote_spans"][0]["source_id"], source_ids)

        validation = validate_artifacts(evidence_path=run_dir / "evidence.json")
        self.assertTrue(validation.valid, [error.to_dict() for error in validation.errors])

    def test_allowed_pdf_can_be_preserved_as_partial_with_caveat(self) -> None:
        run_dir = self.prepare_ingested_run(
            [
                self.base_search_result(
                    id="sr_pdf",
                    url="https://example.com/report.pdf",
                    title="Example Report",
                    result_type="pdf",
                )
            ]
        )

        with mock.patch.object(
            fetch_claims_module,
            "urlopen",
            return_value=FakeResponse(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n", mime_type="application/pdf"),
        ):
            result = fetch_claims(run=run_dir)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["sources_partial"], 1)
        evidence = self.load_json(run_dir / "evidence.json")
        source = evidence["sources"][0]
        self.assertEqual(source["type"], "pdf")
        self.assertEqual(source["retrieval_status"], "partial")
        self.assertTrue(source["local_artifact_path"].endswith(".pdf"))
        self.assertTrue((run_dir / source["local_artifact_path"]).exists())
        self.assertIn("PDF text extraction was partial", source["caveats"][0])
        self.assertEqual(evidence["claims"], [])
        self.assertTrue(result["validation"]["valid"], result["validation"]["errors"])

    def test_blocked_and_failed_sources_do_not_create_high_confidence_claims(self) -> None:
        blocked_run = self.prepare_ingested_run(
            [
                self.base_search_result(
                    id="sr_blocked",
                    policy_decision="blocked",
                    policy_flags=["robots_disallowed"],
                )
            ]
        )

        with mock.patch.object(
            fetch_claims_module,
            "urlopen",
            side_effect=AssertionError("network called"),
        ):
            blocked = fetch_claims(run=blocked_run)

        blocked_evidence = self.load_json(blocked_run / "evidence.json")
        blocked_source = blocked_evidence["sources"][0]
        self.assertEqual(blocked_source["retrieval_status"], "failed")
        self.assertEqual(blocked_source["retrieval_error"], "policy_blocked")
        self.assertEqual(blocked_evidence["claims"], [])
        self.assertEqual(blocked["high_confidence_claims_created"], 0)

        failed_run = self.prepare_ingested_run([self.base_search_result(id="sr_failed")])
        with mock.patch.object(fetch_claims_module, "urlopen", side_effect=OSError("timeout")):
            failed = fetch_claims(run=failed_run)

        failed_evidence = self.load_json(failed_run / "evidence.json")
        failed_source = failed_evidence["sources"][0]
        self.assertEqual(failed["status"], "completed_with_errors")
        self.assertEqual(failed_source["retrieval_status"], "failed")
        self.assertIn("fetch_failed", failed_source["retrieval_error"])
        self.assertEqual(failed_evidence["claims"], [])
        self.assertEqual(failed["high_confidence_claims_created"], 0)

    def test_dangling_generated_claim_source_reference_fails_validation(self) -> None:
        run_dir = self.prepare_ingested_run([self.base_search_result()])
        html = b"<html><body><p>Every extracted claim should keep a valid source reference.</p></body></html>"
        with mock.patch.object(
            fetch_claims_module,
            "urlopen",
            return_value=FakeResponse(html, mime_type="text/html"),
        ):
            fetch_claims(run=run_dir)

        evidence_path = run_dir / "evidence.json"
        evidence = self.load_json(evidence_path)
        evidence["claims"][0]["supporting_sources"] = ["src_missing"]
        evidence["claims"][0]["quote_spans"][0]["source_id"] = "src_missing"
        self.write_json(evidence_path, evidence)

        validation = validate_artifacts(evidence_path=evidence_path)
        self.assertFalse(validation.valid)
        dangling_paths = {error.path for error in validation.errors if error.code == "dangling_reference"}
        self.assertIn("$.evidence.claims[0].supporting_sources", dangling_paths)
        self.assertIn("$.evidence.claims[0].quote_spans[0].source_id", dangling_paths)

    def test_cli_fetch_claims_uses_local_file_without_network(self) -> None:
        runs_dir = self.temp_runs_dir()
        page = runs_dir / "source.html"
        page.write_text(
            """
            <html>
              <head><title>Local HTML</title></head>
              <body><p>The local file smoke command extracts a source linked claim.</p></body>
            </html>
            """,
            encoding="utf-8",
        )
        run_dir = self.prepare_queued_file_run(page.resolve().as_uri())

        command = subprocess.run(
            [str(RUNNER), "fetch-claims", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["sources_fetched"], 1)
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["sources"][0]["title"], "Local HTML")
        self.assertEqual(evidence["claims"][0]["supporting_sources"], ["src_file"])
        self.assertTrue(payload["validation"]["valid"], payload["validation"]["errors"])


if __name__ == "__main__":
    unittest.main()
