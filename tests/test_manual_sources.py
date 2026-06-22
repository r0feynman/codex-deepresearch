from __future__ import annotations

import base64
import json
import socket
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

from deepresearch import ingest_manual_sources, validate_artifacts


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class ManualSourcesTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def load_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_png(self, directory: Path, name: str = "manual.png") -> Path:
        path = directory / name
        path.write_bytes(PNG_1X1)
        return path

    def test_manual_url_creates_source_without_search_artifacts(self) -> None:
        runs_dir = self.temp_runs_dir()

        with (
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("network called")),
            mock.patch.object(socket, "create_connection", side_effect=AssertionError("network called")),
            mock.patch(
                "deepresearch.search_handoff.prepare_run",
                side_effect=AssertionError("search handoff called"),
            ),
        ):
            result = ingest_manual_sources(
                question="manual source question",
                runs_dir=runs_dir,
                urls=["https://example.com/manual-source"],
                labels=["Example Manual Source"],
            )

        self.assertEqual(result["status"], "manual_sources_ingested")
        self.assertFalse(result["external_search"])
        self.assertFalse(result["body_fetch"])
        run_dir = Path(result["run_dir"])
        evidence = self.load_json(run_dir / "evidence.json")

        self.assertEqual(evidence["mode"], "manual-sources")
        self.assertEqual(evidence["search_provider"], "manual")
        self.assertEqual(evidence["sources"][0]["url"], "https://example.com/manual-source")
        self.assertEqual(evidence["sources"][0]["title"], "Example Manual Source")
        self.assertEqual(evidence["sources"][0]["retrieval_status"], "manual")
        self.assertEqual(evidence["sources"][0]["origin"], "manual")
        self.assertEqual(evidence["sources"][0]["policy_decision"], "manual_review")
        self.assertFalse((run_dir / "search_results.jsonl").exists())
        self.assertFalse((run_dir / "search_tasks.json").exists())
        self.assertFalse((run_dir / "fetch_queue.json").exists())
        self.assertTrue((run_dir / evidence["sources"][0]["local_artifact_path"]).exists())
        self.assertTrue(result["validation"]["valid"], result["validation"]["errors"])

    def test_manual_local_image_creates_visual_evidence(self) -> None:
        runs_dir = self.temp_runs_dir()
        image_path = self.write_png(runs_dir)

        result = ingest_manual_sources(
            question="manual image question",
            runs_dir=runs_dir,
            local_images=[image_path],
            labels=["Uploaded Diagram"],
        )

        self.assertEqual(result["status"], "manual_sources_ingested")
        run_dir = Path(result["run_dir"])
        evidence = self.load_json(run_dir / "evidence.json")
        self.assertEqual(len(evidence["sources"]), 1)
        self.assertEqual(len(evidence["images"]), 1)
        source = evidence["sources"][0]
        image = evidence["images"][0]

        self.assertEqual(source["type"], "image")
        self.assertEqual(source["title"], "Uploaded Diagram")
        self.assertEqual(image["source_id"], source["id"])
        self.assertEqual(image["origin"], "user_upload")
        self.assertEqual(image["mime_type"], "image/png")
        self.assertEqual(image["width"], 1)
        self.assertEqual(image["height"], 1)
        self.assertEqual(image["analysis_status"], "skipped")
        self.assertTrue(image["hash"].startswith("sha256:"))
        self.assertEqual((run_dir / image["local_artifact_path"]).read_bytes(), PNG_1X1)
        validation = validate_artifacts(evidence_path=run_dir / "evidence.json")
        self.assertTrue(validation.valid, [error.to_dict() for error in validation.errors])

    def test_manual_image_url_creates_source_and_metadata_visual_evidence(self) -> None:
        runs_dir = self.temp_runs_dir()

        result = ingest_manual_sources(
            question="manual image URL question",
            runs_dir=runs_dir,
            image_urls=["https://example.com/image.png"],
        )

        run_dir = Path(result["run_dir"])
        evidence = self.load_json(run_dir / "evidence.json")
        source = evidence["sources"][0]
        image = evidence["images"][0]

        self.assertEqual(source["type"], "image")
        self.assertEqual(source["url"], "https://example.com/image.png")
        self.assertEqual(image["source_id"], source["id"])
        self.assertEqual(image["image_url"], "https://example.com/image.png")
        self.assertEqual(image["mime_type"], "image/png")
        self.assertEqual(image["width"], 0)
        self.assertEqual(image["height"], 0)
        self.assertIn("dimensions unavailable", image["caveats"][0])
        self.assertTrue((run_dir / image["local_artifact_path"]).exists())
        self.assertTrue(result["validation"]["valid"], result["validation"]["errors"])

    def test_cli_ingest_manual_creates_valid_run(self) -> None:
        runs_dir = self.temp_runs_dir()
        image_path = self.write_png(runs_dir, "cli.png")
        ingest = subprocess.run(
            [
                str(RUNNER),
                "ingest-manual",
                "--question",
                "manual CLI question",
                "--runs-dir",
                str(runs_dir),
                "--url",
                "https://example.com/manual-source",
                "--local-image",
                str(image_path),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(ingest.returncode, 0, ingest.stderr)
        payload = json.loads(ingest.stdout)
        self.assertEqual(payload["status"], "manual_sources_ingested")
        self.assertEqual(payload["sources_ingested"], 2)
        self.assertEqual(payload["images_ingested"], 1)
        self.assertTrue(payload["validation"]["valid"], payload["validation"]["errors"])
        evidence_path = Path(payload["artifacts"]["evidence"])
        evidence = self.load_json(evidence_path)
        self.assertEqual(evidence["search_provider"], "manual")
        self.assertEqual(len(evidence["sources"]), 2)
        self.assertEqual(len(evidence["images"]), 1)


if __name__ == "__main__":
    unittest.main()
