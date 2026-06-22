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
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import ingest_run, prepare_run, validate_artifacts


class SearchHandoffTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

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

    def load_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")

    def manual_source(self) -> dict:
        return {
            "id": "src_manual",
            "type": "web",
            "url": "https://example.com/manual",
            "title": "Manual Source",
            "published_at": None,
            "accessed_at": "2026-06-22T00:00:00Z",
            "quality": "unknown",
            "retrieval_status": "manual",
            "local_artifact_path": "sources/manual.json",
            "license_policy": "unknown",
            "robots_policy": "unknown",
        }

    def test_prepare_creates_valid_handoff_artifacts(self) -> None:
        result = prepare_run(
            question="example search question",
            runs_dir=self.temp_runs_dir(),
        )
        run_dir = Path(result["run_dir"])

        self.assertTrue((run_dir / "search_tasks.json").exists())
        self.assertTrue((run_dir / "search_results.jsonl").exists())
        self.assertTrue((run_dir / "visual_tasks.json").exists())
        self.assertEqual((run_dir / "search_results.jsonl").read_text(encoding="utf-8"), "")

        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(evidence["mode"], "codex-plugin")
        self.assertEqual(evidence["search_provider"], "codex-native")
        self.assertEqual(evidence["vlm_provider"], "codex-interactive")
        self.assertEqual(evidence["search_tasks"][0]["query"], "example search question")

        validation = validate_artifacts(evidence_path=run_dir / "evidence.json")
        self.assertTrue(validation.valid, [error.to_dict() for error in validation.errors])

    def test_ingest_search_results_into_sources_and_fetch_queue(self) -> None:
        prepared = prepare_run(
            question="example search question",
            runs_dir=self.temp_runs_dir(),
        )
        run_dir = Path(prepared["run_dir"])
        self.write_search_results(
            run_dir,
            [
                self.base_search_result(
                    policy_flags=["requires_attribution", "paywall_possible"],
                    raw_provider_metadata={"source_quality": "secondary"},
                )
            ],
        )

        result = ingest_run(run=run_dir)

        self.assertEqual(result["status"], "ingested")
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(len(evidence["sources"]), 1)
        source = evidence["sources"][0]
        self.assertEqual(source["id"], "src_sr_001")
        self.assertEqual(source["retrieval_status"], "partial")
        self.assertEqual(source["quality"], "secondary")
        self.assertEqual(source["policy_decision"], "allowed")
        self.assertEqual(source["policy_flags"], ["requires_attribution", "paywall_possible"])
        self.assertTrue((run_dir / source["local_artifact_path"]).exists())

        fetch_queue = self.load_json(run_dir / "fetch_queue.json")
        self.assertEqual(len(fetch_queue["entries"]), 1)
        self.assertEqual(fetch_queue["entries"][0]["source_id"], "src_sr_001")
        self.assertEqual(
            fetch_queue["entries"][0]["policy_flags"],
            ["requires_attribution", "paywall_possible"],
        )

    def test_invalid_urls_are_rejected_with_failed_status(self) -> None:
        prepared = prepare_run(
            question="example search question",
            runs_dir=self.temp_runs_dir(),
        )
        run_dir = Path(prepared["run_dir"])
        self.write_search_results(
            run_dir,
            [self.base_search_result(id="sr_bad_url", url="not a valid url")],
        )

        result = ingest_run(run=run_dir)

        self.assertEqual(result["status"], "ingested_with_rejections")
        self.assertEqual(result["errors"][0]["code"], "invalid_url")
        evidence = self.load_json(run_dir / "evidence.json")
        source = evidence["sources"][0]
        self.assertEqual(source["retrieval_status"], "failed")
        self.assertEqual(source["ingest_status"], "rejected_invalid_url")
        self.assertEqual(source["retrieval_error"], "invalid_url")
        fetch_queue = self.load_json(run_dir / "fetch_queue.json")
        self.assertEqual(fetch_queue["entries"], [])

    def test_malformed_urls_are_rejected_without_escaping_ingest(self) -> None:
        prepared = prepare_run(
            question="example search question",
            runs_dir=self.temp_runs_dir(),
        )
        run_dir = Path(prepared["run_dir"])
        self.write_search_results(
            run_dir,
            [
                self.base_search_result(id="sr_bad_bracket", url="https://[::1"),
                self.base_search_result(id="sr_bad_port", url="https://example.com:bad/path"),
            ],
        )

        result = ingest_run(run=run_dir)

        self.assertEqual(result["status"], "ingested_with_rejections")
        self.assertEqual([error["code"] for error in result["errors"]], ["invalid_url", "invalid_url"])
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(
            {source["url"]: source["retrieval_status"] for source in evidence["sources"]},
            {
                "https://[::1": "failed",
                "https://example.com:bad/path": "failed",
            },
        )
        fetch_queue = self.load_json(run_dir / "fetch_queue.json")
        self.assertEqual(fetch_queue["entries"], [])

    def test_cli_ingest_rejects_malformed_url_without_traceback(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepared = prepare_run(
            question="example search question",
            runs_dir=runs_dir,
        )
        run_dir = Path(prepared["run_dir"])
        self.write_search_results(
            run_dir,
            [self.base_search_result(id="sr_bad_bracket", url="https://[::1")],
        )

        ingest = subprocess.run(
            [str(RUNNER), "ingest", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(ingest.returncode, 0, ingest.stderr)
        self.assertNotIn("Traceback", ingest.stderr)
        payload = json.loads(ingest.stdout)
        self.assertEqual(payload["status"], "ingested_with_rejections")
        self.assertEqual(payload["errors"][0]["code"], "invalid_url")

    def test_blocked_policy_decision_and_flags_are_preserved(self) -> None:
        prepared = prepare_run(
            question="example search question",
            runs_dir=self.temp_runs_dir(),
        )
        run_dir = Path(prepared["run_dir"])
        self.write_search_results(
            run_dir,
            [
                self.base_search_result(
                    id="sr_blocked",
                    policy_decision="blocked",
                    policy_flags=["robots_disallowed", "copyright_restricted"],
                )
            ],
        )

        result = ingest_run(run=run_dir)

        self.assertEqual(result["status"], "ingested_with_rejections")
        evidence = self.load_json(run_dir / "evidence.json")
        source = evidence["sources"][0]
        self.assertEqual(source["policy_decision"], "blocked")
        self.assertEqual(source["policy_flags"], ["robots_disallowed", "copyright_restricted"])
        self.assertEqual(source["retrieval_status"], "failed")
        self.assertEqual(source["license_policy"], "restricted")
        self.assertEqual(source["robots_policy"], "disallowed")
        fetch_queue = self.load_json(run_dir / "fetch_queue.json")
        self.assertEqual(fetch_queue["entries"], [])

    def test_reingest_replaces_prior_handoff_sources_and_preserves_manual_sources(self) -> None:
        prepared = prepare_run(
            question="example search question",
            runs_dir=self.temp_runs_dir(),
        )
        run_dir = Path(prepared["run_dir"])
        self.write_search_results(
            run_dir,
            [
                self.base_search_result(id="sr_001", url="https://example.com/one"),
                self.base_search_result(id="sr_002", url="https://example.com/two"),
            ],
        )
        first = ingest_run(run=run_dir)
        self.assertEqual(first["fetch_queue_count"], 2)

        evidence_path = run_dir / "evidence.json"
        evidence = self.load_json(evidence_path)
        evidence["sources"].append(self.manual_source())
        self.write_json(evidence_path, evidence)

        self.write_search_results(
            run_dir,
            [self.base_search_result(id="sr_001", url="https://example.com/one")],
        )
        second = ingest_run(run=run_dir)

        self.assertEqual(second["fetch_queue_count"], 1)
        self.assertTrue(second["validation"]["valid"], second["validation"]["errors"])
        evidence = self.load_json(evidence_path)
        source_ids = {source["id"] for source in evidence["sources"]}
        self.assertEqual(source_ids, {"src_manual", "src_sr_001"})
        self.assertNotIn("src_sr_002", source_ids)
        fetch_queue = self.load_json(run_dir / "fetch_queue.json")
        self.assertEqual([entry["source_id"] for entry in fetch_queue["entries"]], ["src_sr_001"])

    def test_cli_prepare_and_ingest_use_handoff_artifacts(self) -> None:
        runs_dir = self.temp_runs_dir()
        prepare = subprocess.run(
            [
                str(RUNNER),
                "prepare",
                "example search question",
                "--runs-dir",
                str(runs_dir),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(prepare.returncode, 0, prepare.stderr)
        prepared = json.loads(prepare.stdout)
        run_dir = Path(prepared["run_dir"])
        self.write_search_results(run_dir, [self.base_search_result()])

        ingest = subprocess.run(
            [str(RUNNER), "ingest", "--run", str(run_dir)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(ingest.returncode, 0, ingest.stderr)
        payload = json.loads(ingest.stdout)
        self.assertEqual(payload["status"], "ingested")
        self.assertEqual(payload["fetch_queue_count"], 1)


if __name__ == "__main__":
    unittest.main()
