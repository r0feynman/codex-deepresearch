from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "plugins" / "codex-deepresearch" / "scripts" / "codex-deepresearch"
PLUGIN_SRC = ROOT / "plugins" / "codex-deepresearch" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from deepresearch import resolve_config


class ExecutionModeResolverTests(unittest.TestCase):
    def run_resolve_config(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(RUNNER), "resolve-config", *args],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_codex_plugin_codex_native_is_valid(self) -> None:
        result = self.run_resolve_config(
            "--mode",
            "codex-plugin",
            "--search-provider",
            "codex-native",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        self.assertEqual(config["mode"], "codex-plugin")
        self.assertEqual(config["search_provider"], "codex-native")
        self.assertEqual(config["vlm_provider"], "codex-interactive")
        self.assertEqual(config["budget_preset"], "standard")

    def test_automated_cli_codex_native_is_rejected(self) -> None:
        result = self.run_resolve_config(
            "--mode",
            "automated-cli",
            "--search-provider",
            "codex-native",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "search provider 'codex-native' is not valid for mode 'automated-cli'",
            result.stderr,
        )

    def test_manual_sources_external_search_provider_is_rejected(self) -> None:
        result = self.run_resolve_config(
            "--mode",
            "manual-sources",
            "--search-provider",
            "brave",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "search provider 'brave' is not valid for mode 'manual-sources'",
            result.stderr,
        )

    def test_invalid_vlm_provider_for_mode_returns_clear_error(self) -> None:
        result = self.run_resolve_config(
            "--mode",
            "automated-cli",
            "--vlm-provider",
            "codex-interactive",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "VLM provider 'codex-interactive' is not valid for mode 'automated-cli'",
            result.stderr,
        )

    def test_budget_and_provider_names_are_normalized(self) -> None:
        config = resolve_config(
            mode="CODEX_PLUGIN",
            search_provider="CODEX_NATIVE",
            vlm_provider="CODEX_INTERACTIVE",
            budget_preset="QUICK",
        )

        self.assertEqual(config.mode, "codex-plugin")
        self.assertEqual(config.search_provider, "codex-native")
        self.assertEqual(config.vlm_provider, "codex-interactive")
        self.assertEqual(config.budget_preset, "quick")
        self.assertEqual(config.budget.max_sources, 8)


if __name__ == "__main__":
    unittest.main()
