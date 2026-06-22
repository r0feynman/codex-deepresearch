#!/usr/bin/env python3
"""Validate the public-safe Codex DeepResearch repository scaffold."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


REQUIRED_FILES = [
    "README.md",
    "AGENTS.md",
    "docs/codex-deepresearch-prd.md",
    "docs/codex-deepresearch-project-management.md",
    "docs/codex-deepresearch-project-management.html",
    ".agents/plugins/marketplace.json",
    "plugins/codex-deepresearch/.codex-plugin/plugin.json",
    "plugins/codex-deepresearch/scripts/README.md",
    "plugins/codex-deepresearch/scripts/codex-deepresearch",
    "plugins/codex-deepresearch/src/deepresearch/__init__.py",
    "plugins/codex-deepresearch/src/deepresearch/execution_mode.py",
    "plugins/codex-deepresearch/skills/deep-research/SKILL.md",
    "scripts/bootstrap_github.py",
    "scripts/bootstrap_project_board.py",
    "tests/test_execution_mode.py",
]


FORBIDDEN_ROOT_ENTRIES = [
    ".codex",
    ".claude",
]


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_json(relative_path: str) -> dict:
    path = ROOT / relative_path
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"{relative_path} is not valid JSON: {exc}")


def main() -> None:
    for relative_path in REQUIRED_FILES:
        if not (ROOT / relative_path).exists():
            fail(f"missing required file: {relative_path}")

    for entry in FORBIDDEN_ROOT_ENTRIES:
        if (ROOT / entry).exists():
            fail(f"forbidden public-repo entry exists: {entry}")

    plugin = read_json("plugins/codex-deepresearch/.codex-plugin/plugin.json")
    if plugin.get("name") != "codex-deepresearch":
        fail("plugin.json name must be codex-deepresearch")
    if plugin.get("skills") != "./skills/":
        fail("plugin.json must expose ./skills/")

    runner = ROOT / "plugins/codex-deepresearch/scripts/codex-deepresearch"
    if not os.access(runner, os.X_OK):
        fail("runner script must be executable: plugins/codex-deepresearch/scripts/codex-deepresearch")
    help_result = subprocess.run(
        [str(runner), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if help_result.returncode != 0:
        fail("runner script --help must exit 0")

    config_result = subprocess.run(
        [
            str(runner),
            "resolve-config",
            "--mode",
            "codex-plugin",
            "--search-provider",
            "codex-native",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if config_result.returncode != 0:
        fail("runner resolve-config must accept codex-plugin + codex-native")
    try:
        resolved_config = json.loads(config_result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"runner resolve-config must output valid JSON: {exc}")
    if resolved_config.get("mode") != "codex-plugin":
        fail("runner resolve-config did not normalize mode")
    if resolved_config.get("search_provider") != "codex-native":
        fail("runner resolve-config did not normalize search provider")

    marketplace = read_json(".agents/plugins/marketplace.json")
    entries = marketplace.get("plugins", [])
    matching = [entry for entry in entries if entry.get("name") == "codex-deepresearch"]
    if len(matching) != 1:
        fail("marketplace must contain exactly one codex-deepresearch entry")
    source = matching[0].get("source", {})
    if source.get("source") != "local":
        fail("marketplace source.source must be local")
    if source.get("path") != "./plugins/codex-deepresearch":
        fail("marketplace source.path must be ./plugins/codex-deepresearch")
    policy = matching[0].get("policy", {})
    if policy.get("installation") != "AVAILABLE":
        fail("marketplace policy.installation must be AVAILABLE")
    if policy.get("authentication") != "ON_INSTALL":
        fail("marketplace policy.authentication must be ON_INSTALL")
    if not matching[0].get("category"):
        fail("marketplace entry must include category")

    skill_text = (ROOT / "plugins/codex-deepresearch/skills/deep-research/SKILL.md").read_text(
        encoding="utf-8"
    )
    if "name: deep-research" not in skill_text:
        fail("deep-research skill frontmatter is missing")
    if "visual_required" not in skill_text:
        fail("deep-research skill must include modality routing guidance")

    scripts_readme = (ROOT / "plugins/codex-deepresearch/scripts/README.md").read_text(
        encoding="utf-8"
    )
    for required_word in ["Install", "Update", "Remove"]:
        if required_word not in scripts_readme:
            fail(f"scripts README must document {required_word.lower()} flow")

    print("Repository scaffold validation passed.")


if __name__ == "__main__":
    main()
