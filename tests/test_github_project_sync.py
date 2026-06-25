from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "github_project"
COMMON = ROOT / "scripts" / "github_project_common.py"
VERIFY_SCRIPT = ROOT / "scripts" / "verify_github_project.py"
SYNC_SCRIPT = ROOT / "scripts" / "sync_github_project.py"


def load_common_module():
    spec = importlib.util.spec_from_file_location("github_project_common", COMMON)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load github_project_common.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


common = load_common_module()


class GitHubProjectSyncFixtureTests(unittest.TestCase):
    def load_state(self, fixture_dir: Path = FIXTURE_DIR):
        policy = common.load_policy(ROOT / "scripts" / "github_project_policy.json")
        return policy, common.load_fixture_state(fixture_dir, policy)

    def copy_fixture(self) -> tempfile.TemporaryDirectory[str]:
        tmp = tempfile.TemporaryDirectory()
        shutil.copytree(FIXTURE_DIR, Path(tmp.name), dirs_exist_ok=True)
        return tmp

    def mutate_project_item(self, fixture_dir: Path, number: int, **updates: str) -> None:
        path = fixture_dir / "project_items.json"
        payload = json.loads(path.read_text())
        for item in payload["items"]:
            if item["content"]["number"] == number:
                item.update(updates)
                path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                return
        raise AssertionError(f"missing fixture issue #{number}")

    def mutate_dependencies(self, fixture_dir: Path, payload: dict) -> None:
        (fixture_dir / "dependencies.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )

    def finding_checks(self, fixture_dir: Path = FIXTURE_DIR) -> set[tuple[str, int]]:
        policy, state = self.load_state(fixture_dir)
        return {
            (finding.check, finding.issue_number)
            for finding in common.verify_project_state(state, policy)
        }

    def test_noop_fixture_verifies_and_sync_dry_run_exits_zero(self) -> None:
        policy, state = self.load_state()
        findings = common.verify_project_state(state, policy)
        self.assertEqual([], findings)
        self.assertEqual([], common.planned_changes(findings, state, policy))

        verify = subprocess.run(
            [
                sys.executable,
                str(VERIFY_SCRIPT),
                "--project-owner",
                "r0feynman",
                "--project-number",
                "1",
                "--fixture-dir",
                str(FIXTURE_DIR),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, verify.returncode, verify.stdout + verify.stderr)

        sync = subprocess.run(
            [
                sys.executable,
                str(SYNC_SCRIPT),
                "--project-owner",
                "r0feynman",
                "--project-number",
                "1",
                "--fixture-dir",
                str(FIXTURE_DIR),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, sync.returncode, sync.stdout + sync.stderr)
        self.assertIn("No safe field fixes planned.", sync.stdout)

    def test_closed_issue_not_done_is_detected_and_safe_to_sync(self) -> None:
        with self.copy_fixture() as tmp:
            fixture_dir = Path(tmp)
            self.mutate_project_item(
                fixture_dir,
                6,
                status="Todo",
                **{"workflow Status": "Backlog"},
            )
            policy, state = self.load_state(fixture_dir)
            findings = common.verify_project_state(state, policy)
            checks = {(finding.check, finding.field_name) for finding in findings}
            self.assertIn(("workflow_status", "Workflow Status"), checks)
            self.assertIn(("project_status", "Status"), checks)
            changes = common.planned_changes(findings, state, policy)
            self.assertEqual(2, len(changes))
            common.apply_changes(changes, state, fixture_mode=True)
            self.assertEqual([], common.verify_project_state(state, policy))

    def test_open_issue_with_unresolved_blocker_cannot_be_ready(self) -> None:
        with self.copy_fixture() as tmp:
            fixture_dir = Path(tmp)
            self.mutate_project_item(fixture_dir, 64, **{"workflow Status": "Ready"})
            policy, state = self.load_state(fixture_dir)
            findings = common.verify_project_state(state, policy)
            self.assertTrue(
                any(
                    finding.issue_number == 64
                    and finding.field_name == "Workflow Status"
                    and finding.expected == "Backlog"
                    and finding.safe_fix
                    for finding in findings
                )
            )

    def test_later_wave_issue_cannot_be_ready(self) -> None:
        with self.copy_fixture() as tmp:
            fixture_dir = Path(tmp)
            self.mutate_project_item(fixture_dir, 68, **{"workflow Status": "Ready"})
            checks = self.finding_checks(fixture_dir)
            self.assertIn(("workflow_status", 68), checks)

    def test_blocked_is_reserved_for_active_impediments(self) -> None:
        with self.copy_fixture() as tmp:
            fixture_dir = Path(tmp)
            self.mutate_project_item(fixture_dir, 68, **{"workflow Status": "Blocked"})
            policy, state = self.load_state(fixture_dir)
            findings = common.verify_project_state(state, policy)
            blocked_findings = [
                finding for finding in findings
                if finding.issue_number == 68 and finding.field_name == "Workflow Status"
            ]
            self.assertEqual(1, len(blocked_findings))
            self.assertEqual("Backlog", blocked_findings[0].expected)
            self.assertTrue(blocked_findings[0].safe_fix)

    def test_dependency_mismatches_cover_missing_extra_and_reversed_links(self) -> None:
        with self.copy_fixture() as tmp:
            fixture_dir = Path(tmp)
            dependencies = json.loads((fixture_dir / "dependencies.json").read_text())
            dependencies["blocked_by"]["69"] = []
            dependencies["blocked_by"]["68"] = [64]
            dependencies["blocked_by"]["75"] = [71]
            dependencies["blocking"]["75"] = [70]
            self.mutate_dependencies(fixture_dir, dependencies)
            checks = self.finding_checks(fixture_dir)
            self.assertIn(("dependency_missing", 69), checks)
            self.assertIn(("dependency_extra", 68), checks)
            self.assertIn(("dependency_reversed", 75), checks)

    def test_or_dependency_note_does_not_create_false_hard_blocker(self) -> None:
        checks = self.finding_checks()
        self.assertNotIn(("dependency_missing", 68), checks)
        self.assertNotIn(("dependency_extra", 68), checks)

    def test_intentionally_blank_phase_and_component_are_supported(self) -> None:
        with self.copy_fixture() as tmp:
            fixture_dir = Path(tmp)
            self.mutate_project_item(
                fixture_dir,
                76,
                phase="Phase 3 - Public Beta",
                component="Skill",
            )
            policy, state = self.load_state(fixture_dir)
            findings = common.verify_project_state(state, policy)
            blank_fields = {
                finding.field_name
                for finding in findings
                if finding.issue_number == 76 and finding.expected is None
            }
            self.assertEqual({"Phase", "Component"}, blank_fields)


if __name__ == "__main__":
    unittest.main()
