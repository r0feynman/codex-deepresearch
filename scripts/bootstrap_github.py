#!/usr/bin/env python3
"""Seed GitHub milestones, labels, and starter issues for this repository."""

from __future__ import annotations

import json
import subprocess
import sys


OWNER = "r0feynman"
REPO = "codex-deepresearch"
FULL_NAME = f"{OWNER}/{REPO}"


MILESTONES = [
    ("Phase 0 - Prototype", "Validate the plugin/skill shape and core research loop."),
    ("Phase 1 - MVP", "Ship a usable local DeepResearch workflow with text and visual evidence."),
    ("Phase 2 - Private Alpha", "Harden reliability, ergonomics, and repeated personal use."),
    ("Phase 3 - Public Beta", "Prepare public usage, docs, and compatibility boundaries."),
    ("Phase 4 - Product v1", "Stabilize product-quality workflows and release practices."),
    ("Phase 5 - Team/Cloud", "Explore team workflows, hosted execution, and shared governance."),
]


LABELS = [
    ("type:epic", "5319e7", "Large body of work under a milestone."),
    ("type:task", "0366d6", "Concrete implementation, research, or documentation task."),
    ("type:research", "0e8a16", "Research or investigation task."),
    ("type:docs", "0075ca", "Documentation work."),
    ("component:plugin", "1d76db", "Codex plugin packaging and marketplace integration."),
    ("component:skill", "7057ff", "Codex skill behavior and instructions."),
    ("component:search", "fbca04", "Text search, source collection, and retrieval."),
    ("component:visual", "d876e3", "Image search, screenshots, OCR, or visual collection."),
    ("component:vlm", "bfd4f2", "Vision-language model analysis path."),
    ("component:evidence", "c5def5", "Evidence schema, storage, provenance, and verification."),
    ("priority:P0", "b60205", "Urgent or blocking priority."),
    ("priority:P1", "d93f0b", "High priority."),
    ("priority:P2", "fbca04", "Medium priority."),
    ("status:blocked", "000000", "Blocked by an unresolved dependency or decision."),
    ("status:needs-decision", "d4c5f9", "Needs a product or technical decision."),
    ("status:ready-for-codex", "0e8a16", "Ready for agent implementation."),
]


ISSUES = [
    {
        "title": "[Epic] Phase 0 prototype",
        "milestone": "Phase 0 - Prototype",
        "labels": ["type:epic", "priority:P1"],
        "body": """## Goal
Validate the Codex DeepResearch plugin/skill shape and prove the core research loop can be implemented cleanly.

## Scope
- Keep the public repo safe and installable.
- Define the local execution shape.
- Create the first runnable text-only research loop.

## Acceptance Criteria
- [ ] Plugin scaffold can be validated locally.
- [ ] Skill invocation behavior is documented.
- [ ] First CLI or script entrypoint is defined.
- [ ] Evidence bundle schema has an initial draft.
""",
    },
    {
        "title": "[Task] Define evidence bundle schema",
        "milestone": "Phase 0 - Prototype",
        "labels": ["type:task", "component:evidence", "priority:P1", "status:ready-for-codex"],
        "body": """## Goal
Create the first JSON schema for text and visual evidence.

## Acceptance Criteria
- [ ] Captures source URL, retrieval time, modality, extracted claims, verifier results, and confidence.
- [ ] Supports image URL, page URL, screenshot path, OCR text, and visual notes.
- [ ] Includes at least one example fixture.

## Validation
- [ ] Schema and fixture are checked by a local validation command.
""",
    },
    {
        "title": "[Task] Implement modality router",
        "milestone": "Phase 1 - MVP",
        "labels": ["type:task", "component:skill", "priority:P1", "status:ready-for-codex"],
        "body": """## Goal
Classify research angles as text_only, visual_optional, or visual_required.

## Acceptance Criteria
- [ ] Router returns a structured decision for each angle.
- [ ] Decision includes a short reason.
- [ ] Tests cover text-only, visual-optional, and visual-required examples.
""",
    },
    {
        "title": "[Task] Build text source collection path",
        "milestone": "Phase 1 - MVP",
        "labels": ["type:task", "component:search", "priority:P1"],
        "body": """## Goal
Collect text sources for research angles and preserve citation metadata.

## Acceptance Criteria
- [ ] Stores URL, title, domain, retrieval time, quote snippets, and extracted claims.
- [ ] Does not treat search snippets as evidence by themselves.
- [ ] Produces a source list usable by the synthesis step.
""",
    },
    {
        "title": "[Task] Build visual acquisition path",
        "milestone": "Phase 1 - MVP",
        "labels": ["type:task", "component:visual", "priority:P1"],
        "body": """## Goal
Collect visual evidence without requiring the user to manually provide image files.

## Acceptance Criteria
- [ ] Extracts Open Graph image candidates.
- [ ] Collects body image candidates with surrounding context.
- [ ] Captures first-viewport screenshots for selected pages.
- [ ] Records original page URL and image URL separately.
""",
    },
    {
        "title": "[Task] Add Codex-native VLM analysis path",
        "milestone": "Phase 1 - MVP",
        "labels": ["type:task", "component:vlm", "priority:P1"],
        "body": """## Goal
Analyze collected images and screenshots with Codex-native VLM capability where available.

## Acceptance Criteria
- [ ] Visual analysis output separates observation from inference.
- [ ] Supports screenshot, chart, UI, product image, and document image cases.
- [ ] Visual findings can be attached to the evidence bundle.
""",
    },
]


def run(args: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def gh_json(args: list[str]) -> object:
    result = run(["gh", *args])
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    return json.loads(result.stdout or "null")


def ensure_milestones() -> None:
    existing = {
        item["title"]: item
        for item in gh_json(["api", f"repos/{FULL_NAME}/milestones", "--paginate"])
    }
    for title, description in MILESTONES:
        if title in existing:
            print(f"milestone exists: {title}")
            continue
        result = run(
            [
                "gh",
                "api",
                f"repos/{FULL_NAME}/milestones",
                "-f",
                f"title={title}",
                "-f",
                "state=open",
                "-f",
                f"description={description}",
            ]
        )
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            raise SystemExit(result.returncode)
        print(f"created milestone: {title}")


def ensure_labels() -> None:
    existing = {
        item["name"]: item
        for item in gh_json(["api", f"repos/{FULL_NAME}/labels", "--paginate"])
    }
    for name, color, description in LABELS:
        if name in existing:
            result = run(
                [
                    "gh",
                    "label",
                    "edit",
                    name,
                    "--repo",
                    FULL_NAME,
                    "--color",
                    color,
                    "--description",
                    description,
                ]
            )
            action = "updated"
        else:
            result = run(
                [
                    "gh",
                    "label",
                    "create",
                    name,
                    "--repo",
                    FULL_NAME,
                    "--color",
                    color,
                    "--description",
                    description,
                ]
            )
            action = "created"
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            raise SystemExit(result.returncode)
        print(f"{action} label: {name}")


def ensure_issues() -> None:
    existing = {
        item["title"]
        for item in gh_json(
            [
                "issue",
                "list",
                "--repo",
                FULL_NAME,
                "--state",
                "all",
                "--limit",
                "200",
                "--json",
                "title",
            ]
        )
    }
    for issue in ISSUES:
        if issue["title"] in existing:
            print(f"issue exists: {issue['title']}")
            continue
        args = [
            "issue",
            "create",
            "--repo",
            FULL_NAME,
            "--title",
            issue["title"],
            "--body",
            issue["body"],
            "--milestone",
            issue["milestone"],
        ]
        for label in issue["labels"]:
            args.extend(["--label", label])
        result = run(["gh", *args])
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            raise SystemExit(result.returncode)
        print(f"created issue: {issue['title']} -> {result.stdout.strip()}")


def main() -> None:
    ensure_milestones()
    ensure_labels()
    ensure_issues()


if __name__ == "__main__":
    main()
