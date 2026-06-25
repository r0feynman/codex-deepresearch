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

    def remove_project_item(self, fixture_dir: Path, number: int) -> None:
        path = fixture_dir / "project_items.json"
        payload = json.loads(path.read_text())
        kept_items = [
            item for item in payload["items"]
            if item.get("content", {}).get("number") != number
        ]
        if len(kept_items) == len(payload["items"]):
            raise AssertionError(f"missing fixture issue #{number}")
        payload["items"] = kept_items
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

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
                status="In Progress",
                **{"workflow Status": "In Progress"},
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

    def test_apply_mode_manual_only_mismatch_exits_nonzero(self) -> None:
        with self.copy_fixture() as tmp:
            fixture_dir = Path(tmp)
            dependencies = json.loads((fixture_dir / "dependencies.json").read_text())
            dependencies["blocked_by"]["68"] = [64]
            self.mutate_dependencies(fixture_dir, dependencies)

            sync = subprocess.run(
                [
                    sys.executable,
                    str(SYNC_SCRIPT),
                    "--project-owner",
                    "r0feynman",
                    "--project-number",
                    "1",
                    "--fixture-dir",
                    str(fixture_dir),
                    "--apply",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(1, sync.returncode, sync.stdout + sync.stderr)
            self.assertIn("Manual-only mismatch(es):", sync.stdout)
            self.assertIn("After:", sync.stdout)

    def test_live_apply_after_reload_uses_fresh_state(self) -> None:
        policy, state = self.load_state()
        reloaded_state = common.ProjectState(
            project_id="PVT_reloaded",
            items={},
            fields={},
            dependencies=common.DependencyState(blocked_by={}, blocking={}),
            issues={},
        )
        calls = []
        args = object()
        original_load_state_from_args = common.load_state_from_args

        def fake_load_state_from_args(fake_args, fake_policy):
            calls.append((fake_args, fake_policy))
            return reloaded_state, False

        try:
            common.load_state_from_args = fake_load_state_from_args
            self.assertIs(
                reloaded_state,
                common.reload_state_after_apply(args, policy, state, fixture_mode=False),
            )
            self.assertIs(
                state,
                common.reload_state_after_apply(args, policy, state, fixture_mode=True),
            )
        finally:
            common.load_state_from_args = original_load_state_from_args

        self.assertEqual([(args, policy)], calls)

    def test_open_issue_with_unresolved_blocker_cannot_be_ready(self) -> None:
        with self.copy_fixture() as tmp:
            fixture_dir = Path(tmp)
            self.mutate_project_item(fixture_dir, 69, **{"workflow Status": "Ready"})
            policy, state = self.load_state(fixture_dir)
            findings = common.verify_project_state(state, policy)
            self.assertTrue(
                any(
                    finding.issue_number == 69
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

    def test_or_dependency_policy_catches_false_hard_blocker(self) -> None:
        with self.copy_fixture() as tmp:
            fixture_dir = Path(tmp)
            dependencies = json.loads((fixture_dir / "dependencies.json").read_text())
            dependencies["blocked_by"]["68"] = [64]
            self.mutate_dependencies(fixture_dir, dependencies)
            checks = self.finding_checks(fixture_dir)
            self.assertIn(("or_dependency_hard_blocker", 68), checks)

    def test_or_dependency_policy_catches_missing_candidate_issue(self) -> None:
        policy, state = self.load_state()
        policy = common.Policy(
            repository=policy.repository,
            current_ready=set(policy.current_ready),
            deferred_backlog=set(policy.deferred_backlog),
            active_blocked=set(policy.active_blocked),
            intentionally_blank_fields={
                issue_number: set(fields)
                for issue_number, fields in policy.intentionally_blank_fields.items()
            },
            or_dependency_groups={68: [{64, 999}]},
            safe_apply_fields=set(policy.safe_apply_fields),
        )
        checks = {
            (finding.check, finding.issue_number)
            for finding in common.verify_project_state(state, policy)
        }
        self.assertIn(("or_dependency_candidate_missing", 68), checks)

    def test_or_dependency_policy_catches_malformed_group(self) -> None:
        policy, state = self.load_state()
        policy = common.Policy(
            repository=policy.repository,
            current_ready=set(policy.current_ready),
            deferred_backlog=set(policy.deferred_backlog),
            active_blocked=set(policy.active_blocked),
            intentionally_blank_fields={
                issue_number: set(fields)
                for issue_number, fields in policy.intentionally_blank_fields.items()
            },
            or_dependency_groups={68: [{64}]},
            safe_apply_fields=set(policy.safe_apply_fields),
        )
        checks = {
            (finding.check, finding.issue_number)
            for finding in common.verify_project_state(state, policy)
        }
        self.assertIn(("or_dependency_group_invalid", 68), checks)

    def test_policy_listed_issue_missing_from_project_is_detected(self) -> None:
        with self.copy_fixture() as tmp:
            fixture_dir = Path(tmp)
            self.remove_project_item(fixture_dir, 59)
            checks = self.finding_checks(fixture_dir)
            self.assertIn(("policy_issue_missing_from_project", 59), checks)

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
