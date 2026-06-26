#!/usr/bin/env python3
"""Shared GitHub Project verification and sync logic."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_POLICY_PATH = SCRIPT_DIR / "github_project_policy.json"
DEFAULT_LIMIT = 500

FIELD_ALIASES = {
    "status": "status",
    "workflow status": "workflow_status",
    "workflow_status": "workflow_status",
    "phase": "phase",
    "work type": "work_type",
    "work_type": "work_type",
    "component": "component",
    "priority": "priority",
}

CANONICAL_FIELD_TITLES = {
    "status": "Status",
    "workflow_status": "Workflow Status",
    "phase": "Phase",
    "work_type": "Work Type",
    "component": "Component",
    "priority": "Priority",
}

COMPONENT_LABEL_MAP = {
    "component:plugin": "Plugin",
    "component:skill": "Skill",
    "component:search": "Search",
    "component:visual": "Visual",
    "component:vlm": "VLM",
    "component:evidence": "Evidence",
    "type:docs": "Docs",
}


@dataclass(frozen=True)
class Policy:
    repository: str
    current_ready: set[int]
    deferred_backlog: set[int]
    active_blocked: set[int]
    intentionally_blank_fields: dict[int, set[str]]
    or_dependency_groups: dict[int, list[set[int]]]
    safe_apply_fields: set[str]


@dataclass(frozen=True)
class IssueRecord:
    number: int
    title: str
    body: str
    state: str
    labels: set[str]
    milestone: str | None
    url: str
    repository: str | None = None


@dataclass(frozen=True)
class PullRequestRecord:
    number: int
    title: str
    state: str
    merged_at: str | None
    url: str
    repository: str | None = None

    @property
    def merged(self) -> bool:
        return self.state == "MERGED" or self.merged_at is not None


@dataclass
class ProjectItem:
    item_id: str
    issue: IssueRecord
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class ProjectPullRequestItem:
    item_id: str
    pull_request: PullRequestRecord
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectField:
    field_id: str
    name: str
    options: dict[str, str]


@dataclass(frozen=True)
class DependencyState:
    blocked_by: dict[int, set[int]]
    blocking: dict[int, set[int]]


@dataclass
class ProjectState:
    project_id: str | None
    items: dict[int, ProjectItem]
    fields: dict[str, ProjectField]
    dependencies: DependencyState
    issues: dict[int, IssueRecord] = field(default_factory=dict)
    pull_requests: dict[int, ProjectPullRequestItem] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding:
    check: str
    issue_number: int
    title: str
    field_name: str | None
    current: str | None
    expected: str | None
    message: str
    safe_fix: bool = False
    content_type: str = "Issue"


@dataclass(frozen=True)
class PlannedChange:
    issue_number: int
    item_id: str
    field_key: str
    field_name: str
    before: str | None
    after: str | None
    reason: str
    content_type: str = "Issue"


class GitHubError(RuntimeError):
    """Raised when a gh command fails."""


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> Policy:
    raw = json.loads(path.read_text())
    return Policy(
        repository=raw["repository"],
        current_ready={int(number) for number in raw.get("current_ready", [])},
        deferred_backlog={int(number) for number in raw.get("deferred_backlog", [])},
        active_blocked={int(number) for number in raw.get("active_blocked", [])},
        intentionally_blank_fields={
            int(number): {canonical_field_name(field_name) for field_name in fields}
            for number, fields in raw.get("intentionally_blank_fields", {}).items()
        },
        or_dependency_groups={
            int(number): [
                {int(candidate) for candidate in group}
                for group in groups
            ]
            for number, groups in raw.get("or_dependency_groups", {}).items()
        },
        safe_apply_fields={
            canonical_field_name(field_name)
            for field_name in raw.get("safe_apply_fields", [])
        },
    )


def canonical_field_name(name: str) -> str:
    normalized = re.sub(r"\s+", " ", name.strip().replace("_", " ")).lower()
    return FIELD_ALIASES.get(normalized, normalized.replace(" ", "_"))


def gh_json(args: list[str]) -> Any:
    result = subprocess.run(
        ["gh", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        if "missing required scopes" in message and "project" in message:
            message = (
                f"{message}\n\n"
                "Run `gh auth refresh -s project` and retry. The scripts use the "
                "existing gh session and do not read token files."
            )
        raise GitHubError(message)
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise GitHubError(f"failed to parse gh JSON output: {exc}") from exc


def list_items(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        items = payload.get(key)
        if isinstance(items, list):
            return items
    return []


def labels_from_raw(raw: Any) -> set[str]:
    if not isinstance(raw, list):
        return set()
    names: set[str] = set()
    for label in raw:
        if isinstance(label, str):
            names.add(label)
        elif isinstance(label, dict) and label.get("name"):
            names.add(str(label["name"]))
    return names


def milestone_title(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        title = raw.get("title")
        if title:
            return str(title)
    return None


def issue_from_raw(raw: dict[str, Any], fallback_repo: str | None = None) -> IssueRecord:
    return IssueRecord(
        number=int(raw["number"]),
        title=str(raw.get("title") or ""),
        body=str(raw.get("body") or ""),
        state=str(raw.get("state") or "OPEN").upper(),
        labels=labels_from_raw(raw.get("labels")),
        milestone=milestone_title(raw.get("milestone")),
        url=str(raw.get("url") or ""),
        repository=str(raw.get("repository") or fallback_repo or ""),
    )


def pull_request_from_raw(
    raw: dict[str, Any],
    fallback_repo: str | None = None,
) -> PullRequestRecord:
    return PullRequestRecord(
        number=int(raw["number"]),
        title=str(raw.get("title") or ""),
        state=str(raw.get("state") or "OPEN").upper(),
        merged_at=str(raw["mergedAt"]) if raw.get("mergedAt") else None,
        url=str(raw.get("url") or ""),
        repository=str(raw.get("repository") or fallback_repo or ""),
    )


def item_fields_from_raw(raw: dict[str, Any]) -> dict[str, str]:
    ignored = {
        "content",
        "id",
        "labels",
        "linked pull requests",
        "milestone",
        "repository",
        "title",
    }
    fields: dict[str, str] = {}
    for key, value in raw.items():
        if key in ignored:
            continue
        canonical = canonical_field_name(key)
        if canonical not in CANONICAL_FIELD_TITLES:
            continue
        if value in (None, ""):
            continue
        fields[canonical] = str(value)
    return fields


def fields_from_raw(payload: Any) -> dict[str, ProjectField]:
    fields: dict[str, ProjectField] = {}
    for raw in list_items(payload, "fields"):
        if not isinstance(raw, dict):
            continue
        canonical = canonical_field_name(str(raw.get("name") or ""))
        if canonical not in CANONICAL_FIELD_TITLES:
            continue
        options = {
            str(option.get("name")): str(option.get("id"))
            for option in raw.get("options", [])
            if option.get("name") and option.get("id")
        }
        fields[canonical] = ProjectField(
            field_id=str(raw.get("id") or ""),
            name=str(raw.get("name") or CANONICAL_FIELD_TITLES[canonical]),
            options=options,
        )
    return fields


def dependencies_from_raw(payload: Any) -> DependencyState:
    if not isinstance(payload, dict):
        return DependencyState(blocked_by={}, blocking={})
    return DependencyState(
        blocked_by={
            int(number): {int(blocker) for blocker in blockers}
            for number, blockers in payload.get("blocked_by", {}).items()
        },
        blocking={
            int(number): {int(blocked) for blocked in blocked_issues}
            for number, blocked_issues in payload.get("blocking", {}).items()
        },
    )


def load_fixture_state(fixture_dir: Path, policy: Policy) -> ProjectState:
    project_payload = read_fixture_json(fixture_dir / "project.json", default={})
    fields_payload = read_fixture_json(fixture_dir / "project_fields.json", default={"fields": []})
    items_payload = read_fixture_json(fixture_dir / "project_items.json", default={"items": []})
    issues_payload = read_fixture_json(fixture_dir / "issues.json", default=None)
    dependencies_payload = read_fixture_json(fixture_dir / "dependencies.json", default={})

    issue_overrides: dict[int, IssueRecord] = {}
    if issues_payload is not None:
        for raw_issue in list_items(issues_payload, "issues"):
            issue = issue_from_raw(raw_issue, fallback_repo=policy.repository)
            issue_overrides[issue.number] = issue

    items: dict[int, ProjectItem] = {}
    pull_requests: dict[int, ProjectPullRequestItem] = {}
    for raw_item in list_items(items_payload, "items"):
        content = raw_item.get("content") or {}
        if not isinstance(content, dict):
            continue
        if "number" not in content:
            continue
        content_type = content.get("type")
        if content_type in (None, "Issue"):
            issue = issue_overrides.get(
                int(content["number"]),
                issue_from_raw(content, fallback_repo=policy.repository),
            )
            items[issue.number] = ProjectItem(
                item_id=str(raw_item.get("id") or ""),
                issue=issue,
                fields=item_fields_from_raw(raw_item),
            )
        elif content_type == "PullRequest":
            pull_request = pull_request_from_raw(content, fallback_repo=policy.repository)
            pull_requests[pull_request.number] = ProjectPullRequestItem(
                item_id=str(raw_item.get("id") or ""),
                pull_request=pull_request,
                fields=item_fields_from_raw(raw_item),
            )

    issues = dict(issue_overrides)
    issues.update({number: item.issue for number, item in items.items()})
    return ProjectState(
        project_id=project_payload.get("id") if isinstance(project_payload, dict) else None,
        items=items,
        fields=fields_from_raw(fields_payload),
        dependencies=dependencies_from_raw(dependencies_payload),
        issues=issues,
        pull_requests=pull_requests,
    )


def read_fixture_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def load_live_state(
    *,
    project_owner: str,
    project_number: int,
    repository: str,
    limit: int = DEFAULT_LIMIT,
) -> ProjectState:
    project_payload = gh_json(
        ["project", "view", str(project_number), "--owner", project_owner, "--format", "json"]
    )
    fields_payload = gh_json(
        [
            "project",
            "field-list",
            str(project_number),
            "--owner",
            project_owner,
            "--format",
            "json",
            "--limit",
            str(limit),
        ]
    )
    items_payload = gh_json(
        [
            "project",
            "item-list",
            str(project_number),
            "--owner",
            project_owner,
            "--format",
            "json",
            "--limit",
            str(limit),
        ]
    )
    issues_payload = gh_json(
        [
            "issue",
            "list",
            "--repo",
            repository,
            "--state",
            "all",
            "--limit",
            str(limit),
            "--json",
            "number,title,body,state,labels,milestone,url",
        ]
    )
    issues = {
        issue.number: issue
        for issue in (
            issue_from_raw(raw_issue, fallback_repo=repository)
            for raw_issue in list_items(issues_payload, "issues")
        )
    }

    items: dict[int, ProjectItem] = {}
    pull_requests: dict[int, ProjectPullRequestItem] = {}
    for raw_item in list_items(items_payload, "items"):
        content = raw_item.get("content") or {}
        if not isinstance(content, dict):
            continue
        if "number" not in content:
            continue
        content_type = content.get("type")
        if content_type == "Issue":
            issue_number = int(content["number"])
            issue = issues.get(issue_number)
            if issue is None:
                issue = issue_from_raw(
                    gh_json(
                        [
                            "issue",
                            "view",
                            str(issue_number),
                            "--repo",
                            repository,
                            "--json",
                            "number,title,body,state,labels,milestone,url",
                        ]
                    ),
                    fallback_repo=repository,
                )
            issues[issue.number] = issue
            items[issue.number] = ProjectItem(
                item_id=str(raw_item.get("id") or ""),
                issue=issue,
                fields=item_fields_from_raw(raw_item),
            )
        elif content_type == "PullRequest":
            pull_request_number = int(content["number"])
            pull_request_repo = str(content.get("repository") or repository)
            pull_request = pull_request_from_raw(
                gh_json(
                    [
                        "pr",
                        "view",
                        str(pull_request_number),
                        "--json",
                        "number,title,state,mergedAt,url",
                        "--repo",
                        pull_request_repo,
                    ]
                ),
                fallback_repo=pull_request_repo,
            )
            pull_requests[pull_request.number] = ProjectPullRequestItem(
                item_id=str(raw_item.get("id") or ""),
                pull_request=pull_request,
                fields=item_fields_from_raw(raw_item),
            )

    dependencies = load_live_dependencies(repository, items.keys())
    return ProjectState(
        project_id=str(project_payload.get("id") or ""),
        items=items,
        fields=fields_from_raw(fields_payload),
        dependencies=dependencies,
        issues=issues,
        pull_requests=pull_requests,
    )


def load_live_dependencies(repository: str, issue_numbers: Iterable[int]) -> DependencyState:
    blocked_by: dict[int, set[int]] = {}
    blocking: dict[int, set[int]] = {}
    for issue_number in sorted(issue_numbers):
        blocked_by[issue_number] = set(
            dependency_numbers(
                gh_json(
                    [
                        "api",
                        f"repos/{repository}/issues/{issue_number}/dependencies/blocked_by",
                    ]
                )
            )
        )
        blocking[issue_number] = set(
            dependency_numbers(
                gh_json(
                    [
                        "api",
                        f"repos/{repository}/issues/{issue_number}/dependencies/blocking",
                    ]
                )
            )
        )
    return DependencyState(blocked_by=blocked_by, blocking=blocking)


def dependency_numbers(payload: Any) -> list[int]:
    if not isinstance(payload, list):
        return []
    numbers: list[int] = []
    for item in payload:
        if isinstance(item, dict) and item.get("number") is not None:
            numbers.append(int(item["number"]))
    return numbers


def parse_project_metadata(body: str) -> dict[str, str | None]:
    metadata: dict[str, str | None] = {}
    for line in body.splitlines():
        match = re.match(r"^\s*-\s*Project\s+([^:]+):\s*(.+?)\s*$", line)
        if not match:
            continue
        field_key = canonical_field_name(match.group(1))
        raw_value = match.group(2).strip()
        value_without_ticks = raw_value.replace("`", "")
        if "intentionally unset" in value_without_ticks.lower():
            metadata[field_key] = None
            continue
        value_match = re.search(r"`([^`]+)`", raw_value)
        if value_match:
            metadata[field_key] = value_match.group(1)
        else:
            metadata[field_key] = raw_value.rstrip(".")
    return metadata


def section_text(body: str, heading: str) -> str:
    lines = body.splitlines()
    collected: list[str] = []
    in_section = False
    target = heading.lower()
    start_level = 0
    for line in lines:
        heading_match = re.match(r"^(#{2,6})\s+(.+?)\s*$", line)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip().lower()
            if in_section and level <= start_level:
                break
            if title == target:
                in_section = True
                start_level = level
                continue
        if in_section:
            collected.append(line)
    return "\n".join(collected).strip()


def parse_hard_blockers(body: str) -> set[int]:
    hard_blocker_text = section_text(body, "Hard Blockers")
    if not hard_blocker_text:
        inline = re.search(
            r"^\s*(?:-\s*)?hard blockers?\s*:\s*([^\n]+)",
            body,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        hard_blocker_text = inline.group(1) if inline else ""
    if not hard_blocker_text or re.search(r"\bnone\b", hard_blocker_text, flags=re.IGNORECASE):
        return set()
    blockers: set[int] = set()
    for line in hard_blocker_text.splitlines():
        if re.search(r"\bor\b", line, flags=re.IGNORECASE):
            continue
        if re.search(r"blocked by|hard blocker|^-", line, flags=re.IGNORECASE):
            blockers.update(int(number) for number in re.findall(r"#(\d+)", line))
    return blockers


def unresolved_blockers(item: ProjectItem, state: ProjectState) -> set[int]:
    blockers = parse_hard_blockers(item.issue.body)
    unresolved: set[int] = set()
    for blocker in blockers:
        blocker_item = state.items.get(blocker)
        if blocker_item is None or blocker_item.issue.state != "CLOSED":
            unresolved.add(blocker)
    return unresolved


def expected_metadata_fields(item: ProjectItem, policy: Policy) -> dict[str, str | None]:
    issue = item.issue
    metadata = parse_project_metadata(issue.body)
    intentionally_blank = policy.intentionally_blank_fields.get(issue.number, set())
    if not metadata and not intentionally_blank:
        return {}
    expected: dict[str, str | None] = {}

    if "phase" in metadata:
        expected["phase"] = metadata["phase"]
    elif issue.milestone:
        expected["phase"] = issue.milestone

    expected["work_type"] = metadata.get("work_type") or label_work_type(issue.labels)
    if "component" in metadata:
        expected["component"] = metadata["component"]
    else:
        expected_component = label_component(issue.labels)
        if expected_component:
            expected["component"] = expected_component
    expected_priority = metadata.get("priority") or label_priority(issue.labels)
    if expected_priority:
        expected["priority"] = expected_priority

    for field_key in intentionally_blank:
        expected[field_key] = None
    return expected


def label_work_type(labels: set[str]) -> str:
    if "type:epic" in labels:
        return "Epic"
    if "type:research" in labels:
        return "Research"
    if "type:docs" in labels:
        return "Docs"
    return "Task"


def label_component(labels: set[str]) -> str | None:
    for label, component in COMPONENT_LABEL_MAP.items():
        if label in labels:
            return component
    return None


def label_priority(labels: set[str]) -> str | None:
    for priority in ("P0", "P1", "P2"):
        if f"priority:{priority}" in labels:
            return priority
    return None


def expected_workflow_status(item: ProjectItem, state: ProjectState, policy: Policy) -> str:
    issue = item.issue
    current_workflow = item.fields.get("workflow_status")
    if issue.state == "CLOSED":
        return "Done"
    if current_workflow == "In Progress":
        return "In Progress"
    if issue.number in policy.active_blocked:
        return "Blocked"
    if unresolved_blockers(item, state):
        return "Backlog"
    if current_workflow == "Blocked":
        return "Backlog"
    if issue.number in policy.current_ready:
        return "Ready"
    if issue.number in policy.deferred_backlog:
        return "Backlog"

    metadata = parse_project_metadata(issue.body)
    expected = metadata.get("workflow_status")
    return expected or "Backlog"


def expected_status(workflow_status: str, issue_state: str) -> str:
    if issue_state == "CLOSED" or workflow_status == "Done":
        return "Done"
    if workflow_status == "In Progress":
        return "In Progress"
    return "Todo"


def verify_project_state(state: ProjectState, policy: Policy) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(verify_policy_issue_coverage(state, policy))
    findings.extend(verify_or_dependency_groups(state, policy))
    for item in sorted(state.items.values(), key=lambda project_item: project_item.issue.number):
        findings.extend(verify_dependencies(item, state))
        findings.extend(verify_workflow_fields(item, state, policy))
        findings.extend(verify_metadata_fields(item, state, policy))
    for item in sorted(
        state.pull_requests.values(),
        key=lambda project_item: project_item.pull_request.number,
    ):
        findings.extend(verify_pull_request_lifecycle_fields(item, state))
    return findings


def policy_issue_numbers(policy: Policy) -> set[int]:
    numbers = set(policy.current_ready)
    numbers.update(policy.deferred_backlog)
    numbers.update(policy.active_blocked)
    numbers.update(policy.intentionally_blank_fields)
    for dependent, groups in policy.or_dependency_groups.items():
        numbers.add(dependent)
        for group in groups:
            numbers.update(group)
    return numbers


def verify_policy_issue_coverage(state: ProjectState, policy: Policy) -> list[Finding]:
    findings: list[Finding] = []
    for issue_number in sorted(policy_issue_numbers(policy)):
        if issue_number in state.items:
            continue
        issue = state.issues.get(issue_number)
        findings.append(
            Finding(
                check="policy_issue_missing_from_project",
                issue_number=issue_number,
                title=issue.title if issue else "",
                field_name=None,
                current="missing",
                expected="Project item",
                message=(
                    f"Policy lists #{issue_number}, but that issue is absent from "
                    "the GitHub Project items."
                ),
                safe_fix=False,
            )
        )
    return findings


def verify_or_dependency_groups(state: ProjectState, policy: Policy) -> list[Finding]:
    findings: list[Finding] = []
    for dependent, groups in sorted(policy.or_dependency_groups.items()):
        dependent_item = state.items.get(dependent)
        dependent_issue = state.issues.get(dependent)
        title = (
            dependent_item.issue.title
            if dependent_item is not None
            else dependent_issue.title if dependent_issue is not None else ""
        )
        if not groups:
            findings.append(
                Finding(
                    check="or_dependency_group_invalid",
                    issue_number=dependent,
                    title=title,
                    field_name=None,
                    current="no OR groups",
                    expected="at least one OR group",
                    message=f"Policy OR dependency entry for #{dependent} has no candidate groups.",
                    safe_fix=False,
                )
            )
            continue

        actual_hard_blockers = state.dependencies.blocked_by.get(dependent, set())
        for index, group in enumerate(groups, start=1):
            if len(group) < 2:
                findings.append(
                    Finding(
                        check="or_dependency_group_invalid",
                        issue_number=dependent,
                        title=title,
                        field_name=None,
                        current=format_numbers(group),
                        expected="two or more candidate issues",
                        message=(
                            f"Policy OR dependency group {index} for #{dependent} "
                            "must contain at least two candidate issues."
                        ),
                        safe_fix=False,
                    )
                )
            if dependent in group:
                findings.append(
                    Finding(
                        check="or_dependency_group_invalid",
                        issue_number=dependent,
                        title=title,
                        field_name=None,
                        current=format_numbers(group),
                        expected=f"candidates excluding #{dependent}",
                        message=(
                            f"Policy OR dependency group {index} for #{dependent} "
                            "must not include the dependent issue itself."
                        ),
                        safe_fix=False,
                    )
                )
            for candidate in sorted(group):
                if candidate not in state.issues:
                    findings.append(
                        Finding(
                            check="or_dependency_candidate_missing",
                            issue_number=dependent,
                            title=title,
                            field_name=None,
                            current=f"#{candidate}",
                            expected="known GitHub issue",
                            message=(
                                f"Policy OR dependency group {index} for #{dependent} "
                                f"references #{candidate}, but that issue was not found "
                                "in GitHub issue state."
                            ),
                            safe_fix=False,
                        )
                    )

            false_hard_blockers = group & actual_hard_blockers
            for candidate in sorted(false_hard_blockers):
                findings.append(
                    Finding(
                        check="or_dependency_hard_blocker",
                        issue_number=dependent,
                        title=title,
                        field_name=None,
                        current=format_numbers(actual_hard_blockers),
                        expected="no OR candidates as hard blockers",
                        message=(
                            f"#{candidate} is an OR dependency candidate for #{dependent}, "
                            "so it must not be present as a GitHub hard blocker."
                        ),
                        safe_fix=False,
                    )
                )
    return findings


def verify_dependencies(item: ProjectItem, state: ProjectState) -> list[Finding]:
    if item.issue.state == "CLOSED":
        return []
    expected = parse_hard_blockers(item.issue.body)
    actual = state.dependencies.blocked_by.get(item.issue.number, set())
    reverse = state.dependencies.blocking.get(item.issue.number, set())
    findings: list[Finding] = []

    for blocker in sorted(expected - actual):
        reversed_link = blocker in reverse
        check = "dependency_reversed" if reversed_link else "dependency_missing"
        message = (
            f"#{item.issue.number} should be blocked by #{blocker}, "
            "but the API link is reversed."
            if reversed_link
            else f"#{item.issue.number} should be blocked by #{blocker}, but the API link is missing."
        )
        findings.append(
            Finding(
                check=check,
                issue_number=item.issue.number,
                title=item.issue.title,
                field_name=None,
                current=format_numbers(actual),
                expected=format_numbers(expected),
                message=message,
                safe_fix=False,
            )
        )

    for blocker in sorted(actual - expected):
        findings.append(
            Finding(
                check="dependency_extra",
                issue_number=item.issue.number,
                title=item.issue.title,
                field_name=None,
                current=format_numbers(actual),
                expected=format_numbers(expected),
                message=(
                    f"#{item.issue.number} has extra API hard blocker #{blocker}; "
                    "OR-shaped or soft-order dependencies should stay out of GitHub hard blockers."
                ),
                safe_fix=False,
            )
        )
    return findings


def verify_workflow_fields(item: ProjectItem, state: ProjectState, policy: Policy) -> list[Finding]:
    expected_workflow = expected_workflow_status(item, state, policy)
    expected_project_status = expected_status(expected_workflow, item.issue.state)
    findings: list[Finding] = []
    findings.extend(
        field_findings(
            item=item,
            state=state,
            field_key="workflow_status",
            expected=expected_workflow,
            check="workflow_status",
            reason=workflow_reason(item, state, policy, expected_workflow),
        )
    )
    findings.extend(
        field_findings(
            item=item,
            state=state,
            field_key="status",
            expected=expected_project_status,
            check="project_status",
            reason=f"Project Status should match Workflow Status={expected_workflow}.",
        )
    )
    return findings


def verify_metadata_fields(item: ProjectItem, state: ProjectState, policy: Policy) -> list[Finding]:
    findings: list[Finding] = []
    for field_key, expected in sorted(expected_metadata_fields(item, policy).items()):
        if field_key not in CANONICAL_FIELD_TITLES:
            continue
        if expected is not None and not field_supports(state, field_key, expected):
            continue
        findings.extend(
            field_findings(
                item=item,
                state=state,
                field_key=field_key,
                expected=expected,
                check="metadata_field",
                reason=(
                    f"{CANONICAL_FIELD_TITLES[field_key]} should match issue metadata."
                    if expected is not None
                    else f"{CANONICAL_FIELD_TITLES[field_key]} is intentionally blank for this issue."
                ),
            )
        )
    return findings


def verify_pull_request_lifecycle_fields(
    item: ProjectPullRequestItem,
    state: ProjectState,
) -> list[Finding]:
    if not item.pull_request.merged:
        return []

    findings: list[Finding] = []
    findings.extend(
        pull_request_field_findings(
            item=item,
            state=state,
            field_key="workflow_status",
            expected="Done",
            check="pull_request_workflow_status",
            reason="Merged pull requests must have Workflow Status=Done.",
        )
    )
    findings.extend(
        pull_request_field_findings(
            item=item,
            state=state,
            field_key="status",
            expected="Done",
            check="pull_request_status",
            reason="Merged pull requests must have Status=Done.",
        )
    )
    return findings


def pull_request_field_findings(
    *,
    item: ProjectPullRequestItem,
    state: ProjectState,
    field_key: str,
    expected: str,
    check: str,
    reason: str,
) -> list[Finding]:
    current = item.fields.get(field_key)
    if current == expected:
        return []
    return [
        Finding(
            check=check,
            issue_number=item.pull_request.number,
            title=item.pull_request.title,
            field_name=CANONICAL_FIELD_TITLES[field_key],
            current=current,
            expected=expected,
            message=reason,
            safe_fix=field_supports(state, field_key, expected),
            content_type="PullRequest",
        )
    ]


def field_findings(
    *,
    item: ProjectItem,
    state: ProjectState,
    field_key: str,
    expected: str | None,
    check: str,
    reason: str,
) -> list[Finding]:
    current = item.fields.get(field_key)
    if current == expected or (expected is None and current is None):
        return []
    safe_fix = is_safe_field_fix(item, state, field_key, expected)
    return [
        Finding(
            check=check,
            issue_number=item.issue.number,
            title=item.issue.title,
            field_name=CANONICAL_FIELD_TITLES[field_key],
            current=current,
            expected=expected,
            message=reason,
            safe_fix=safe_fix,
        )
    ]


def is_safe_field_fix(
    item: ProjectItem,
    state: ProjectState,
    field_key: str,
    expected: str | None,
) -> bool:
    if field_key not in CANONICAL_FIELD_TITLES:
        return False
    if expected is not None and not field_supports(state, field_key, expected):
        return False
    if field_key == "status" and expected == "In Progress":
        return False
    if field_key == "workflow_status" and expected == "In Progress":
        return False
    if (
        item.issue.state != "CLOSED"
        and item.fields.get("workflow_status") == "In Progress"
        and expected != "In Progress"
    ):
        return False
    return True


def field_supports(state: ProjectState, field_key: str, value: str) -> bool:
    field_definition = state.fields.get(field_key)
    if field_definition is None:
        return False
    return value in field_definition.options


def workflow_reason(
    item: ProjectItem,
    state: ProjectState,
    policy: Policy,
    expected_workflow: str,
) -> str:
    issue = item.issue
    if issue.state == "CLOSED":
        return "Closed issues must have Workflow Status=Done."
    if expected_workflow == "In Progress":
        return "Selected work already marked In Progress is left In Progress."
    if issue.number in policy.active_blocked:
        return "Policy marks this issue as actively blocked after selection or start."
    blockers = unresolved_blockers(item, state)
    if blockers:
        return (
            "Open issue has unresolved hard blockers "
            f"{format_numbers(blockers)}; unstarted dependency-gated work stays Backlog."
        )
    if item.fields.get("workflow_status") == "Blocked":
        return "Blocked is reserved for active impediments after selection or start."
    if issue.number in policy.current_ready:
        return "Issue is in the policy current safe wave."
    if issue.number in policy.deferred_backlog:
        return "Issue is in a later or deliberately deferred policy wave."
    return "Issue is not selected for the current safe wave."


def format_numbers(numbers: Iterable[int]) -> str:
    sorted_numbers = sorted(numbers)
    if not sorted_numbers:
        return "none"
    return ", ".join(f"#{number}" for number in sorted_numbers)


def planned_changes(findings: list[Finding], state: ProjectState, policy: Policy) -> list[PlannedChange]:
    changes: list[PlannedChange] = []
    for finding in findings:
        if not finding.safe_fix or finding.field_name is None:
            continue
        field_key = canonical_field_name(finding.field_name)
        if field_key not in policy.safe_apply_fields:
            continue
        item = project_item_for_finding(state, finding)
        changes.append(
            PlannedChange(
                issue_number=finding.issue_number,
                item_id=item.item_id,
                field_key=field_key,
                field_name=finding.field_name,
                before=finding.current,
                after=finding.expected,
                reason=finding.message,
                content_type=finding.content_type,
            )
        )
    return changes


def project_item_for_finding(
    state: ProjectState,
    finding: Finding,
) -> ProjectItem | ProjectPullRequestItem:
    if finding.content_type == "PullRequest":
        return state.pull_requests[finding.issue_number]
    return state.items[finding.issue_number]


def project_item_for_change(
    state: ProjectState,
    change: PlannedChange,
) -> ProjectItem | ProjectPullRequestItem:
    if change.content_type == "PullRequest":
        return state.pull_requests[change.issue_number]
    return state.items[change.issue_number]


def apply_changes(changes: list[PlannedChange], state: ProjectState, fixture_mode: bool) -> None:
    if fixture_mode:
        for change in changes:
            item = project_item_for_change(state, change)
            if change.after is None:
                item.fields.pop(change.field_key, None)
            else:
                item.fields[change.field_key] = change.after
        return
    if not state.project_id:
        raise GitHubError("project id is required to edit live Project items")
    for change in changes:
        field_definition = state.fields.get(change.field_key)
        if field_definition is None:
            raise GitHubError(f"missing Project field: {change.field_name}")
        args = [
            "project",
            "item-edit",
            "--id",
            change.item_id,
            "--project-id",
            state.project_id,
            "--field-id",
            field_definition.field_id,
            "--format",
            "json",
        ]
        if change.after is None:
            args.append("--clear")
        else:
            option_id = field_definition.options.get(change.after)
            if option_id is None:
                raise GitHubError(
                    f"missing option {change.after!r} for Project field {change.field_name}"
                )
            args.extend(["--single-select-option-id", option_id])
        gh_json(args)


def print_verification_report(findings: list[Finding], state: ProjectState) -> None:
    print(
        "Checked "
        f"{len(state.items) + len(state.pull_requests)} GitHub Project item(s) "
        f"({len(state.items)} issue, {len(state.pull_requests)} pull request)."
    )
    if not findings:
        print("No Project policy mismatches found.")
        return
    print(f"Found {len(findings)} Project policy mismatch(es):")
    for finding in findings:
        location = format_project_location(finding.content_type, finding.issue_number)
        field_part = f" {finding.field_name}" if finding.field_name else ""
        before_after = ""
        if finding.current != finding.expected:
            before_after = f" current={format_value(finding.current)} expected={format_value(finding.expected)}"
        fix_part = " safe-fix" if finding.safe_fix else " manual"
        print(f"- {finding.check}{field_part} {location}:{before_after} [{fix_part}] {finding.message}")


def format_project_location(content_type: str, number: int) -> str:
    if content_type == "PullRequest":
        return f"PR #{number}"
    return f"#{number}"


def print_sync_report(
    before: list[Finding],
    changes: list[PlannedChange],
    after: list[Finding] | None,
    *,
    applied: bool,
) -> None:
    mode = "apply" if applied else "dry-run"
    print(f"Sync mode: {mode}")
    print(f"Before: {len(before)} mismatch(es), {len(changes)} safe field fix(es).")
    if changes:
        print("Safe field fixes:")
        for change in changes:
            print(
                f"- {format_project_location(change.content_type, change.issue_number)} "
                f"{change.field_name}: "
                f"{format_value(change.before)} -> {format_value(change.after)}"
            )
    else:
        print("No safe field fixes planned.")
    manual = [finding for finding in before if not finding.safe_fix]
    if manual:
        print(f"Manual-only mismatch(es): {len(manual)}")
        for finding in manual:
            print(
                f"- {format_project_location(finding.content_type, finding.issue_number)} "
                f"{finding.check}: {finding.message}"
            )
    if after is not None:
        print(f"After: {len(after)} mismatch(es) remain.")


def format_value(value: str | None) -> str:
    return "<blank>" if value is None else repr(value)


def build_parser(description: str, *, sync: bool) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--project-owner", required=True)
    parser.add_argument("--project-number", type=int, required=True)
    parser.add_argument("--repo", default=None, help="GitHub repository, default from policy JSON.")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--fixture-dir", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    if sync:
        parser.add_argument("--apply", action="store_true", help="Apply safe field fixes.")
    return parser


def load_state_from_args(args: argparse.Namespace, policy: Policy) -> tuple[ProjectState, bool]:
    if args.fixture_dir:
        return load_fixture_state(args.fixture_dir, policy), True
    return (
        load_live_state(
            project_owner=args.project_owner,
            project_number=args.project_number,
            repository=args.repo or policy.repository,
            limit=args.limit,
        ),
        False,
    )


def reload_state_after_apply(
    args: argparse.Namespace,
    policy: Policy,
    state: ProjectState,
    fixture_mode: bool,
) -> ProjectState:
    if fixture_mode:
        return state
    reloaded_state, _fixture_mode = load_state_from_args(args, policy)
    return reloaded_state


def verify_main(argv: list[str] | None = None) -> int:
    parser = build_parser("Verify GitHub Project policy without writing.", sync=False)
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        state, _fixture_mode = load_state_from_args(args, policy)
        findings = verify_project_state(state, policy)
        print_verification_report(findings, state)
        return 1 if findings else 0
    except GitHubError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def sync_main(argv: list[str] | None = None) -> int:
    parser = build_parser("Sync documented safe GitHub Project field fixes.", sync=True)
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        state, fixture_mode = load_state_from_args(args, policy)
        before = verify_project_state(state, policy)
        changes = planned_changes(before, state, policy)
        after = None
        if args.apply:
            if changes:
                apply_changes(changes, state, fixture_mode=fixture_mode)
                state = reload_state_after_apply(args, policy, state, fixture_mode)
            after = verify_project_state(state, policy)
        print_sync_report(before, changes, after, applied=args.apply)
        if args.apply:
            return 1 if after else 0
        return 1 if before else 0
    except GitHubError as exc:
        print(str(exc), file=sys.stderr)
        return 2
