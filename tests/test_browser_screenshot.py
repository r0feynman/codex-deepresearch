from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import acquire_visual_candidates, prepare_run  # noqa: E402
from deepresearch.browser_screenshot import BrowserScreenshotCapture  # noqa: E402


class FakeBrowserTransport:
    name = "fake-browser"
    provider_mode = "fixture"

    def __init__(self, *, available: bool = True, http_status: int = 200) -> None:
        self._available = available
        self._http_status = http_status
        self.calls: list[dict[str, Any]] = []

    def availability(self) -> tuple[bool, str | None]:
        if self._available:
            return True, None
        return False, "fake_browser_unavailable"

    def capture(
        self,
        *,
        url: str,
        output_path: Path,
        viewport: Mapping[str, int],
        full_page: bool,
        timeout_ms: int,
    ) -> BrowserScreenshotCapture:
        self.calls.append(
            {
                "url": url,
                "output_path": output_path,
                "viewport": dict(viewport),
                "full_page": full_page,
                "timeout_ms": timeout_ms,
            }
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + f"{url}|{output_path.name}|{full_page}".encode("utf-8")
        )
        return BrowserScreenshotCapture(
            width=int(viewport["width"]),
            height=1800 if full_page else int(viewport["height"]),
            http_status=self._http_status,
            final_url=url,
            provider_metadata={"fake": True},
        )


class BrowserScreenshotTests(unittest.TestCase):
    def temp_runs_dir(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return Path(temp_dir.name)

    def read_json(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def prepared_visual_run(self, sources: list[dict[str, Any]] | None = None) -> Path:
        prepared = prepare_run(
            question="Compare public web page screenshots",
            runs_dir=self.temp_runs_dir(),
            route="visual_required",
        )
        run_dir = Path(prepared["run_dir"])
        evidence = self.read_json(run_dir / "evidence.json")
        evidence["sources"] = sources if sources is not None else [self.public_source()]
        self.write_json(run_dir / "evidence.json", evidence)
        return run_dir

    def public_source(self, **overrides: Any) -> dict[str, Any]:
        source = {
            "id": "src_public_page",
            "type": "web",
            "url": "https://example.test/public-page",
            "title": "Public page",
            "published_at": None,
            "accessed_at": "2026-06-25T00:00:00Z",
            "quality": "primary",
            "retrieval_status": "fetched",
            "local_artifact_path": "sources/public-page.html",
            "license_policy": "allowed",
            "robots_policy": "allowed",
            "policy_decision": "allowed",
            "policy_flags": [],
            "route": "visual_required",
            "angle_id": "angle_001",
        }
        source.update(overrides)
        return source

    def test_allowed_pages_write_screenshot_candidates_and_fetch_records_without_observations(
        self,
    ) -> None:
        run_dir = self.prepared_visual_run()
        transport = FakeBrowserTransport()

        result = acquire_visual_candidates(
            run=run_dir,
            providers=("browser-screenshot",),
            screenshot_modes=("first_viewport", "full_page"),
            browser_transport=transport,
        )

        self.assertEqual(result["status"], "visual_candidates_collected")
        self.assertTrue(result["external_network_call"])
        self.assertTrue(result["visual_artifact_validation"]["valid"])
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(result["candidate_counts"], {"screenshot": 2})
        self.assertEqual(result["selected_observations"], 0)
        self.assertEqual(result["screenshot_capture_requests"], 2)

        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        observations = self.read_jsonl(run_dir / "visual_observations.jsonl")
        provider_status = self.read_json(run_dir / "visual_provider_status.json")
        plan = self.read_json(run_dir / "visual_search_plan.json")

        self.assertEqual(plan["tasks"][0]["target_evidence_type"], "screenshot")
        self.assertEqual({candidate["candidate_status"] for candidate in candidates}, {"fetched"})
        self.assertEqual({fetch["fetch_status"] for fetch in fetches}, {"fetched"})
        self.assertEqual(observations, [])
        self.assertEqual(provider_status["providers"][0]["provider_kind"], "screenshot")
        self.assertEqual(provider_status["providers"][0]["provider_mode"], "fixture")
        self.assertEqual(provider_status["providers"][0]["artifacts_fetched"], 2)
        self.assertEqual(provider_status["providers"][0]["vlm_images_analyzed"], 0)

        for candidate in candidates:
            self.assertEqual(candidate["policy_decision"], "allowed")
            self.assertFalse(candidate["supportable_evidence"])
            self.assertTrue(candidate["requires_vlm_observation"])
            screenshot = candidate["screenshot"]
            self.assertIn(screenshot["mode"], {"first_viewport", "full_page"})
            self.assertEqual(screenshot["viewport"], {"width": 1280, "height": 720})
            self.assertTrue((run_dir / candidate["local_artifact_path"]).is_file())
            self.assertEqual(candidate["provider_provenance"]["provider_kind"], "screenshot")
            self.assertEqual(candidate["provider_provenance"]["transport"], "fake-browser")
            self.assertTrue(candidate["provider_provenance"]["external_network_call"])

        for fetch in fetches:
            self.assertIsNone(fetch["evidence_image_id"])
            self.assertTrue(fetch["local_artifact_path"].startswith("screenshots/"))
            self.assertTrue(fetch["hash"].startswith("sha256:"))

        evidence = self.read_json(run_dir / "evidence.json")
        self.assertEqual(evidence["images"], [])
        self.assertTrue(evidence["visual_acquisition"]["external_network_call"])
        self.assertEqual(
            [source["id"] for source in evidence["sources"]],
            ["src_public_page"],
        )

    def test_standard_access_control_policy_flags_block_capture(self) -> None:
        blocked_flags = [
            "access_controlled",
            "captcha_protected",
            "login_gated",
            "copyright_restricted",
            "pii_detected",
            "robots_disallowed",
        ]
        sources = [
            self.public_source(
                id=f"src_{flag}",
                url=f"https://example.test/{flag}",
                policy_flags=[flag],
            )
            for flag in blocked_flags
        ]
        run_dir = self.prepared_visual_run(sources=sources)
        transport = FakeBrowserTransport()

        result = acquire_visual_candidates(
            run=run_dir,
            providers=("browser-screenshot",),
            screenshot_modes=("first_viewport",),
            browser_transport=transport,
        )

        self.assertEqual(len(transport.calls), 0)
        self.assertFalse(result["external_network_call"])
        self.assertEqual(result["selected_observations"], 0)
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        self.assertEqual(len(candidates), len(blocked_flags))
        self.assertEqual({candidate["candidate_status"] for candidate in candidates}, {"policy_blocked"})
        self.assertEqual({candidate["policy_decision"] for candidate in candidates}, {"blocked"})
        self.assertEqual({fetch["fetch_status"] for fetch in fetches}, {"policy_blocked"})
        self.assertEqual([fetch["local_artifact_path"] for fetch in fetches], [None] * len(blocked_flags))

    def test_access_denied_http_status_after_navigation_is_policy_blocked(self) -> None:
        run_dir = self.prepared_visual_run()
        transport = FakeBrowserTransport(http_status=403)

        result = acquire_visual_candidates(
            run=run_dir,
            providers=("browser-screenshot",),
            screenshot_modes=("first_viewport",),
            browser_transport=transport,
        )

        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(result["external_network_call"])
        self.assertEqual(result["selected_observations"], 0)
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["candidate_status"], "policy_blocked")
        self.assertEqual(candidates[0]["policy_decision"], "blocked")
        self.assertEqual(candidates[0]["rejection_reason"], "access_denied")
        self.assertIn("access_denied", candidates[0]["removal_reasons"])
        self.assertEqual(candidates[0]["http_status"], 403)
        self.assertEqual(candidates[0]["screenshot"]["http_status"], 403)
        self.assertFalse((run_dir / candidates[0]["local_artifact_path"]).exists())
        self.assertEqual(fetches[0]["fetch_status"], "policy_blocked")
        self.assertEqual(fetches[0]["http_status"], 403)
        self.assertIsNone(fetches[0]["local_artifact_path"])
        self.assertEqual(self.read_jsonl(run_dir / "visual_observations.jsonl"), [])

    def test_server_error_http_status_after_navigation_is_failed_retrieval(self) -> None:
        run_dir = self.prepared_visual_run()
        transport = FakeBrowserTransport(http_status=500)

        result = acquire_visual_candidates(
            run=run_dir,
            providers=("browser-screenshot",),
            screenshot_modes=("first_viewport",),
            browser_transport=transport,
        )

        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(result["external_network_call"])
        self.assertEqual(result["selected_observations"], 0)
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["candidate_status"], "fetch_failed")
        self.assertEqual(candidates[0]["policy_decision"], "allowed")
        self.assertEqual(candidates[0]["rejection_reason"], "retrieval_failed")
        self.assertIn("retrieval_failed", candidates[0]["removal_reasons"])
        self.assertEqual(candidates[0]["http_status"], 500)
        self.assertEqual(candidates[0]["screenshot"]["http_status"], 500)
        self.assertFalse((run_dir / candidates[0]["local_artifact_path"]).exists())
        self.assertEqual(fetches[0]["fetch_status"], "failed")
        self.assertEqual(fetches[0]["http_status"], 500)
        self.assertIsNone(fetches[0]["local_artifact_path"])

    def test_unsupported_scroll_and_interaction_modes_are_explicit_skips(self) -> None:
        run_dir = self.prepared_visual_run()
        transport = FakeBrowserTransport()

        result = acquire_visual_candidates(
            run=run_dir,
            providers=("browser-screenshot",),
            screenshot_modes=("scroll", "interaction"),
            browser_transport=transport,
        )

        self.assertEqual(len(transport.calls), 0)
        self.assertEqual(result["selected_observations"], 0)
        unsupported_modes = {
            item["mode"] for item in result["screenshot_capture"]["unsupported"]
        }
        self.assertEqual(unsupported_modes, {"scroll", "interaction"})
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        self.assertEqual(
            {candidate["rejection_reason"] for candidate in candidates},
            {"unsupported_screenshot_mode"},
        )
        self.assertEqual({fetch["fetch_status"] for fetch in fetches}, {"skipped"})
        self.assertEqual(
            {fetch["failure_code"] for fetch in fetches},
            {"unsupported_screenshot_mode"},
        )

    def test_policy_blocked_pages_do_not_capture_or_create_supportable_evidence(self) -> None:
        sources = [
            {
                "id": "src_robots_blocked",
                "type": "web",
                "url": "https://example.test/robots-blocked",
                "title": "Robots blocked",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "secondary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/robots.html",
                "license_policy": "allowed",
                "robots_policy": "disallowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "route": "visual_required",
                "angle_id": "angle_001",
            },
            {
                "id": "src_paywalled",
                "type": "web",
                "url": "https://example.test/paywalled",
                "title": "Paywalled",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "secondary",
                "retrieval_status": "paywalled",
                "local_artifact_path": "sources/paywall.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": [],
                "route": "visual_required",
                "angle_id": "angle_001",
            },
            {
                "id": "src_login_captcha",
                "type": "web",
                "url": "https://example.test/login-captcha",
                "title": "Login CAPTCHA",
                "published_at": None,
                "accessed_at": "2026-06-25T00:00:00Z",
                "quality": "secondary",
                "retrieval_status": "fetched",
                "local_artifact_path": "sources/login.html",
                "license_policy": "allowed",
                "robots_policy": "allowed",
                "policy_decision": "allowed",
                "policy_flags": ["login_required", "captcha_required"],
                "route": "visual_required",
                "angle_id": "angle_001",
            },
        ]
        run_dir = self.prepared_visual_run(sources=sources)
        transport = FakeBrowserTransport()

        result = acquire_visual_candidates(
            run=run_dir,
            providers=("browser-screenshot",),
            screenshot_modes=("first_viewport",),
            browser_transport=transport,
        )

        self.assertEqual(len(transport.calls), 0)
        self.assertEqual(result["selected_observations"], 0)
        candidates = self.read_jsonl(run_dir / "visual_candidates.jsonl")
        fetches = self.read_jsonl(run_dir / "image_fetch_status.jsonl")
        self.assertEqual(len(candidates), 3)
        self.assertEqual({candidate["candidate_status"] for candidate in candidates}, {"policy_blocked"})
        self.assertEqual({candidate["policy_decision"] for candidate in candidates}, {"blocked"})
        self.assertEqual({fetch["fetch_status"] for fetch in fetches}, {"policy_blocked"})
        self.assertEqual([fetch["local_artifact_path"] for fetch in fetches], [None, None, None])
        self.assertEqual(self.read_jsonl(run_dir / "visual_observations.jsonl"), [])

    def test_browser_screenshot_provider_is_noop_for_text_only_routes(self) -> None:
        prepared = prepare_run(
            question="Text-only browser screenshot gating",
            runs_dir=self.temp_runs_dir(),
            route="text_only",
        )
        run_dir = Path(prepared["run_dir"])
        transport = FakeBrowserTransport()

        result = acquire_visual_candidates(
            run=run_dir,
            providers=("browser-screenshot",),
            screenshot_modes=("first_viewport", "full_page"),
            browser_transport=transport,
        )

        self.assertEqual(result["status"], "no_visual_tasks")
        self.assertEqual(len(transport.calls), 0)
        self.assertEqual(result["candidate_records"], 0)
        self.assertEqual(result["screenshot_capture_requests"], 0)
        self.assertEqual(self.read_jsonl(run_dir / "visual_candidates.jsonl"), [])
        self.assertEqual(self.read_jsonl(run_dir / "image_fetch_status.jsonl"), [])

    def test_browser_only_run_without_sources_does_not_inject_fixture_sources(self) -> None:
        run_dir = self.prepared_visual_run(sources=[])
        transport = FakeBrowserTransport()

        result = acquire_visual_candidates(
            run=run_dir,
            providers=("browser-screenshot",),
            screenshot_modes=("first_viewport",),
            browser_transport=transport,
        )

        self.assertEqual(result["status"], "visual_candidates_collected")
        self.assertEqual(len(transport.calls), 0)
        self.assertEqual(result["candidate_records"], 0)
        self.assertFalse(result["external_network_call"])
        evidence = self.read_json(run_dir / "evidence.json")
        self.assertEqual(evidence["sources"], [])
        self.assertEqual(self.read_jsonl(run_dir / "visual_candidates.jsonl"), [])
        self.assertEqual(self.read_jsonl(run_dir / "image_fetch_status.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
