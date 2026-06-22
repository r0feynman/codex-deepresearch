#!/usr/bin/env python3
"""Create and populate the GitHub Projects board for Codex DeepResearch.

This script requires a GitHub CLI token with the `project` scope:

    gh auth refresh -s project
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


OWNER = "r0feynman"
REPO = "codex-deepresearch"
FULL_NAME = f"{OWNER}/{REPO}"
PROJECT_TITLE = "Codex DeepResearch"

PROJECT_DESCRIPTION = "Roadmap, implementation queue, and blocked-work tracker for Codex DeepResearch."
PROJECT_README = """# Codex DeepResearch

Use this project as the live operating board for the repository.

Recommended views:

- Current: open issues grouped by Workflow Status.
- Next: items where Workflow Status is Backlog or Ready.
- Roadmap: items grouped by Phase.
- Blocked: items where Workflow Status is Blocked.

Milestones remain the source of truth for release phases. Issues remain the source of truth for implementation detail.
"""

FIELDS = {
    "Phase": [
        "Phase 0 - Prototype",
        "Phase 1 - MVP",
        "Phase 2 - Private Alpha",
        "Phase 3 - Public Beta",
        "Phase 4 - Product v1",
        "Phase 5 - Team/Cloud",
    ],
    "Work Type": ["Epic", "Task", "Research", "Docs"],
    "Component": ["Plugin", "Skill", "Search", "Visual", "VLM", "Evidence", "Docs"],
    "Priority": ["P0", "P1", "P2"],
    "Workflow Status": ["Backlog", "Ready", "In Progress", "Blocked", "Done"],
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


@dataclass
class Project:
    number: int
    project_id: str
    url: str


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def gh_json(args: list[str]) -> Any:
    result = run(["gh", *args])
    if result.returncode != 0:
        handle_gh_error(result)
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        print(result.stdout, file=sys.stderr)
        raise SystemExit(f"Failed to parse gh JSON output: {exc}") from exc


def handle_gh_error(result: subprocess.CompletedProcess[str]) -> None:
    message = result.stderr.strip() or result.stdout.strip()
    if "missing required scopes" in message and "project" in message:
        print(
            "GitHub Projects 권한이 없습니다.\n"
            "먼저 아래 명령을 실행해 `project` scope를 승인한 뒤 다시 실행하세요:\n\n"
            "  gh auth refresh -s project\n",
            file=sys.stderr,
        )
    else:
        print(message, file=sys.stderr)
    raise SystemExit(result.returncode)


def project_list_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("projects", [])
    return []


def field_list_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("fields", [])
    return []


def issue_list_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    return []


def item_list_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("items", [])
    return []


def ensure_project() -> Project:
    data = gh_json(["project", "list", "--owner", OWNER, "--format", "json", "--limit", "100"])
    for project in project_list_items(data):
        if project.get("title") == PROJECT_TITLE:
            print(f"project exists: {PROJECT_TITLE} -> {project.get('url')}")
            return Project(
                number=int(project["number"]),
                project_id=project["id"],
                url=project["url"],
            )

    created = gh_json(
        [
            "project",
            "create",
            "--owner",
            OWNER,
            "--title",
            PROJECT_TITLE,
            "--format",
            "json",
        ]
    )
    print(f"created project: {PROJECT_TITLE} -> {created.get('url')}")
    return Project(number=int(created["number"]), project_id=created["id"], url=created["url"])


def configure_project(project: Project) -> None:
    result = run(
        [
            "gh",
            "project",
            "edit",
            str(project.number),
            "--owner",
            OWNER,
            "--description",
            PROJECT_DESCRIPTION,
            "--readme",
            PROJECT_README,
            "--visibility",
            "PUBLIC",
        ]
    )
    if result.returncode != 0:
        handle_gh_error(result)
    print("configured project metadata")

    result = run(["gh", "project", "link", str(project.number), "--owner", OWNER, "--repo", REPO])
    if result.returncode == 0:
        print(f"linked project to repo: {FULL_NAME}")
    elif "already linked" in (result.stderr + result.stdout).lower():
        print(f"project already linked to repo: {FULL_NAME}")
    else:
        handle_gh_error(result)


def ensure_fields(project: Project) -> dict[str, dict[str, Any]]:
    existing = get_fields(project)
    for name, options in FIELDS.items():
        if name in existing:
            print(f"field exists: {name}")
            continue
        created = gh_json(
            [
                "project",
                "field-create",
                str(project.number),
                "--owner",
                OWNER,
                "--name",
                name,
                "--data-type",
                "SINGLE_SELECT",
                "--single-select-options",
                ",".join(options),
                "--format",
                "json",
            ]
        )
        print(f"created field: {name}")
        existing[name] = created

    return get_fields(project)


def get_fields(project: Project) -> dict[str, dict[str, Any]]:
    data = gh_json(
        [
            "project",
            "field-list",
            str(project.number),
            "--owner",
            OWNER,
            "--format",
            "json",
            "--limit",
            "100",
        ]
    )
    return {field["name"]: field for field in field_list_items(data)}


def get_project_items(project: Project) -> dict[str, str]:
    data = gh_json(
        [
            "project",
            "item-list",
            str(project.number),
            "--owner",
            OWNER,
            "--format",
            "json",
            "--limit",
            "200",
        ]
    )
    items: dict[str, str] = {}
    for item in item_list_items(data):
        content = item.get("content") or {}
        url = content.get("url")
        item_id = item.get("id")
        if url and item_id:
            items[url] = item_id
    return items


def get_issues() -> list[dict[str, Any]]:
    data = gh_json(
        [
            "issue",
            "list",
            "--repo",
            FULL_NAME,
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            "number,title,url,labels,milestone,state",
        ]
    )
    return issue_list_items(data)


def option_id(field: dict[str, Any], option_name: str) -> str | None:
    for option in field.get("options", []):
        if option.get("name") == option_name:
            return option.get("id")
    return None


def edit_single_select(
    project: Project,
    item_id: str,
    field: dict[str, Any],
    value: str | None,
) -> None:
    if not value:
        return
    selected = option_id(field, value)
    if not selected:
        print(f"missing option {value!r} for field {field.get('name')!r}", file=sys.stderr)
        return
    result = run(
        [
            "gh",
            "project",
            "item-edit",
            "--id",
            item_id,
            "--project-id",
            project.project_id,
            "--field-id",
            field["id"],
            "--single-select-option-id",
            selected,
        ]
    )
    if result.returncode != 0:
        handle_gh_error(result)


def label_names(issue: dict[str, Any]) -> set[str]:
    return {label["name"] for label in issue.get("labels", [])}


def issue_phase(issue: dict[str, Any]) -> str | None:
    milestone = issue.get("milestone")
    if milestone:
        return milestone.get("title")
    return None


def issue_work_type(labels: set[str]) -> str:
    if "type:epic" in labels:
        return "Epic"
    if "type:research" in labels:
        return "Research"
    if "type:docs" in labels:
        return "Docs"
    return "Task"


def issue_component(labels: set[str]) -> str | None:
    for label, component in COMPONENT_LABEL_MAP.items():
        if label in labels:
            return component
    return None


def issue_priority(labels: set[str]) -> str | None:
    for priority in ["P0", "P1", "P2"]:
        if f"priority:{priority}" in labels:
            return priority
    return None


def issue_workflow_status(labels: set[str], issue: dict[str, Any]) -> str:
    if issue.get("state") == "CLOSED":
        return "Done"
    if "status:blocked" in labels:
        return "Blocked"
    if "status:ready-for-codex" in labels:
        return "Ready"
    return "Backlog"


def add_and_classify_issues(project: Project, fields: dict[str, dict[str, Any]]) -> None:
    existing_items = get_project_items(project)
    for issue in get_issues():
        url = issue["url"]
        if url in existing_items:
            item_id = existing_items[url]
            print(f"item exists: #{issue['number']} {issue['title']}")
        else:
            added = gh_json(
                [
                    "project",
                    "item-add",
                    str(project.number),
                    "--owner",
                    OWNER,
                    "--url",
                    url,
                    "--format",
                    "json",
                ]
            )
            item_id = added["id"]
            print(f"added item: #{issue['number']} {issue['title']}")

        labels = label_names(issue)
        values = {
            "Phase": issue_phase(issue),
            "Work Type": issue_work_type(labels),
            "Component": issue_component(labels),
            "Priority": issue_priority(labels),
            "Workflow Status": issue_workflow_status(labels, issue),
        }
        for field_name, value in values.items():
            if field_name in fields:
                edit_single_select(project, item_id, fields[field_name], value)
        print(f"classified item: #{issue['number']}")


def main() -> None:
    project = ensure_project()
    configure_project(project)
    fields = ensure_fields(project)
    add_and_classify_issues(project, fields)
    print(f"\nProject board ready: {project.url}")
    print("View creation/grouping may still need final adjustment in the GitHub UI.")


if __name__ == "__main__":
    main()
