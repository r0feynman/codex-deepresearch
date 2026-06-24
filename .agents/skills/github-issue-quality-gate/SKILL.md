---
name: github-issue-quality-gate
description: Draft, adversarially review, score, publish, order, dependency-link, and GitHub Project-sync issues for this repository. Use whenever Codex is asked to create, register, publish, split, rewrite, or bulk-create GitHub issues, backlog items, epics, milestones, implementation tickets, dependencies, blockers, or development order.
---

# GitHub Issue Quality Gate

## Core Rule

Never create the GitHub issue first.

Issue creation is allowed only after a local draft passes:

1. adversarial issue review with no blockers, and
2. main-agent quality scoring of at least 9/10.

After the issue is created, add it to the GitHub Project, set project fields, and verify the Project item exists.

## Workflow

1. Resolve context.
   - Confirm repository, project root, current git status, target GitHub repository, and target GitHub Project.
   - Inspect existing open and recently closed issues to avoid duplicates.
   - Inspect labels, milestones, and project fields before drafting metadata.
   - Resolve development order before drafting: identify strict sequential work, parallelizable work, hard blockers, soft ordering preferences, and epic/sub-issue relationships.

2. Draft locally.
   - Write the issue title, body, labels, milestone, parent/child links, dependency plan, and Project field plan in a local temporary draft.
   - Do not run `gh issue create` yet.
   - For multiple issues, each issue needs its own draft and quality result.

3. Run adversarial issue review.
   - Spawn an independent review subagent with the local draft only.
   - Ask it to find blockers, ambiguity, missing acceptance criteria, duplicate risk, PRD/roadmap mismatch, missing dependencies, unsafe private data, and metadata problems.
   - Do not ask it to create the issue.

4. Revise until no blockers remain.
   - Fix all blocker findings in the local draft.
   - Re-run adversarial review when the changes are material or the previous review reported blockers.

5. Score the final local draft.
   - Use `references/issue-rubric.md`.
   - The main agent, not the review subagent, assigns the final score.
   - If the score is below 9/10, revise the local draft and repeat review or scoring as needed.
   - If the draft cannot reach 9/10 without user input, stop and ask before creating any GitHub issue.

6. Publish only passing drafts.
   - Create the GitHub issue only after the local draft has no blockers and scores at least 9/10.
   - Use body files rather than shell-interpolated heredocs when the issue body contains Markdown backticks, paths, or command examples.

7. Sync to GitHub Project.
   - Add the created issue to the target Project with `gh project item-add`.
   - Set Project fields such as Status, Workflow Status, Phase, Work Type, Priority, and Component when the field supports the intended value.
   - If a Project field lacks the needed option, preserve the information in issue labels/body and report the field gap.
   - Apply GitHub issue dependency links for hard blockers after all referenced issues exist.
   - Use Project `Workflow Status=Ready` only for the next safe implementation wave, not for every issue that is theoretically unblocked.
   - Keep unstarted work in `Backlog` when it is deliberately deferred for sequencing, PR size, file-conflict risk, review bandwidth, or because another Ready issue should land first.
   - If an open issue has unresolved hard blockers and has not started yet, set Project `Workflow Status` to `Backlog`, not `Ready`.
   - Use Project `Workflow Status=Blocked` only when work was selected or started and cannot continue because of a failed run, external dependency, missing decision, missing credential, broken environment, or other active impediment.
   - When all hard blockers close or no longer apply, move the issue to `Ready` only after the issue body and dependency API agree that no hard blockers remain.

8. Verify.
   - Confirm the issue has a non-empty `projectItems` entry.
   - Confirm `gh project item-list` shows the expected fields.
   - Verify hard blocker relationships through GitHub issue dependency output or REST/GraphQL checks.
   - Report issue URL, final quality score, review outcome, Project item status, dependency links, and any field gaps.

## Development Order And Blockers

Every multi-issue draft must include a `Development Order` or `Dependencies / Ordering` section.

Use this operating rule for every new issue set:

- Issue body: write the full development order in `Dependencies / Ordering`.
- GitHub dependency: create dependency links only for true hard blockers.
- Project Workflow:
  - current safe wave: `Ready`
  - later wave or deliberately deferred work: `Backlog`
  - active failure/impediment after selection or start: `Blocked`

Use these categories:

- `Hard blocker`: issue B cannot be accepted, merged, or meaningfully validated until issue A is done. Mirror this as a GitHub issue dependency when possible.
- `Soft ordering`: issue A should land earlier for clarity, but issue B can still be developed in parallel. Keep this in the issue body and Project notes; do not create a GitHub blocking relationship.
- `Parallelizable`: issues can be implemented at the same time, preferably with disjoint files/modules and separate PRs.
- `Epic completion dependency`: an epic cannot close until child issues close. Track with child issue checklist and, when useful, GitHub sub-issues or dependencies.

For Codex DeepResearch:

- Prefer GitHub `blocked by` / `blocking` relationships only for hard blockers.
- Do not mark a docs or validation issue as a blocker merely because it should land early; use soft ordering unless later work truly cannot be accepted without it.
- Preserve parallelizable work explicitly so future implementation agents do not serialize everything unnecessarily.
- The adversarial issue reviewer must check that hard blockers, soft ordering, and parallelizable work are not mixed together.
- Project `Workflow Status` is not the same thing as GitHub issue dependency state:
  - `Backlog`: ordered behind other work, including unresolved hard dependencies that have not been started.
  - `Ready`: selected for the next safe implementation wave and implementation-ready now.
  - `In Progress`: actively being worked.
  - `Blocked`: selected or started work that cannot continue because of an active impediment, failed run, missing external decision, missing credential, or broken environment.
  - `Done`: closed or completed.
- An issue can be technically unblocked but still `Backlog` when starting it in the same wave would create avoidable merge conflicts, overlarge review scope, unclear test ordering, or coordinator overload.
- When multiple issues are parallelizable, mark only the chosen current wave as `Ready`; keep later parallel candidates in `Backlog` until the coordinator intentionally starts that wave.
- Soft ordering alone does not make an issue `Blocked`.

GitHub supports issue dependencies as `blocked by` / `blocking` relationships. The GitHub UI shows blocked issues with a blocked icon in issues and Project boards. Current GitHub docs describe `gh issue create --blocked-by/--blocking` and `gh issue edit --add-blocked-by/--add-blocking`, but the installed `gh` may not expose those flags. If the local `gh issue --help` lacks those flags, use the REST API fallback after issue creation:

```bash
# Get the numeric database id of the blocker issue.
blocking_id=$(gh api repos/OWNER/REPO/issues/BLOCKER_NUMBER --jq .id)

# Mark BLOCKED_NUMBER as blocked by BLOCKER_NUMBER.
gh api \
  --method POST \
  -H "Accept: application/vnd.github+json" \
  repos/OWNER/REPO/issues/BLOCKED_NUMBER/dependencies/blocked_by \
  -f issue_id="$blocking_id"
```

Verify dependencies after applying them:

```bash
gh issue view BLOCKED_NUMBER
gh api repos/OWNER/REPO/issues/BLOCKED_NUMBER/dependencies/blocked_by --jq '.[].number'
```

Then verify Project workflow status. Unstarted dependency-gated issues should usually be `Backlog`; active impediments should be `Blocked`:

```bash
gh project item-list PROJECT_NUMBER --owner OWNER --format json --limit 100 \
  | jq '.items[] | select(.content.number == BLOCKED_NUMBER) | ."workflow Status"'
```

## Codex DeepResearch Defaults

Use these defaults for `/home/user/Projects/codex-deepresearch` unless the user or existing roadmap context says otherwise:

- Repository: `r0feynman/codex-deepresearch`
- Project owner: `r0feynman`
- Project number: `1`
- Default issue Project Status: `Todo`
- Default task Workflow Status: `Backlog`, unless the issue is selected for the next safe implementation wave.
- Default epic Workflow Status: `Backlog`
- Phase: match the PRD roadmap phase, usually `Phase 2 - Private Alpha` for current hardening work.
- Work Type: `Epic`, `Task`, `Research`, or `Docs`.
- Priority: derive from urgency and roadmap role; use `P1` for blocking real-use quality issues and `P2` for follow-up docs or lower-risk cleanup.

Always discover field IDs and option IDs at runtime:

```bash
gh project list --owner r0feynman --format json --limit 20
gh project field-list 1 --owner r0feynman --format json
```

Then add and edit the item:

```bash
gh project item-add 1 --owner r0feynman --url <issue-url> --format json
gh project item-edit --id <item-id> --project-id <project-id> --field-id <field-id> --single-select-option-id <option-id>
```

Verify both sides:

```bash
gh issue view <number> --json number,title,projectItems
gh project item-list 1 --owner r0feynman --format json --limit 100
```

## Draft Requirements

Every publishable issue should include:

- clear title with roadmap prefix when applicable,
- problem or goal,
- source of truth such as PRD section, E2E artifact, user request, or parent epic,
- scope and out-of-scope boundaries when ambiguity is likely,
- implementation-ready acceptance checklist,
- dependencies or ordering constraints,
- hard blocker relationships separate from soft ordering and parallelizable work,
- validation or reproduction notes,
- labels and Project field plan,
- public-repo safety check for pasted logs, paths, screenshots, and generated evidence.

Bug issues should include observed behavior, expected behavior, reproduction evidence, and impacted artifacts. Epic issues should include child issue checklist and completion criteria.

## Stop Conditions

Stop before issue creation when:

- the draft has unresolved blockers,
- the main-agent score is below 9/10,
- the issue appears duplicate and the right action may be to update an existing issue,
- required roadmap or product decisions are missing,
- Project target or repository identity is ambiguous,
- the body would expose secrets, private data, local sessions, or non-public artifacts.
