# Issue Quality Rubric

Use this rubric after adversarial review reports no blockers. The main agent assigns the score.

## Blockers

If any blocker remains, the issue cannot be created and the maximum score is 8/10.

Blockers:

- Duplicate of an existing issue without a clear reason to create a new one.
- Problem or goal is unclear.
- Scope is too large, unbounded, or not implementable as an issue.
- Acceptance checklist is missing or not verifiable.
- Roadmap, PRD, dependency, or parent-epic relationship is wrong or missing when required.
- Hard blocker relationship is missing, reversed, or incorrectly treated as a soft ordering note.
- Open issue has unresolved hard blockers but Project `Workflow Status` is `Ready` instead of `Backlog` or, if actively impeded, `Blocked`.
- Project marks too many deferred or conflict-prone issues as `Ready` instead of limiting `Ready` to the next safe implementation wave.
- Required GitHub Project field plan is missing.
- Body contains secrets, private data, local sessions, credentials, or unsafe public-repo content.
- User confirmation is required for a product decision and has not been obtained.

## Score

Start from 10 and subtract for concrete defects.

- Problem clarity and source of truth: up to -2
  - unclear problem, missing observed/expected behavior, or weak PRD/artifact link.
- Scope and implementation readiness: up to -2
  - too broad, mixed concerns, unclear boundaries, missing dependencies, or not actionable.
- Development order and dependency correctness: up to -1.5
  - hard blockers are missing, soft ordering is over-modeled as a blocker, parallelizable issues are unnecessarily serialized, or blocker direction is wrong.
- Project workflow consistency: up to -1
  - dependency-gated unstarted issues remain `Ready`, deferred parallel candidates remain `Ready`, actively impeded work remains `Ready` or `Backlog` instead of `Blocked`, unblocked next-wave work remains `Backlog`, or issue body/dependency API/Project state disagree.
- Acceptance checklist quality: up to -2
  - not measurable, missing negative cases, missing verification, or not tied to the requested outcome.
- Metadata and Project plan: up to -1.5
  - wrong labels, priority, phase, work type, component, milestone, parent/child relation, or Project field plan.
- Evidence and validation notes: up to -1
  - missing reproduction steps, artifact paths, command outputs, or validation expectations for bugs and E2E findings.
- Public-repo safety and wording quality: up to -1
  - leaks private material, over-quotes copyrighted text, includes noisy logs, or uses ambiguous wording.
- User fit and sequencing: up to -0.5
  - issue is technically valid but not in the right order for the user's current workflow.

## Passing Threshold

Only drafts scoring 9/10 or higher may be created as GitHub issues.

If the draft scores below 9:

1. revise the local draft,
2. re-run adversarial review when the revision is material,
3. rescore,
4. repeat until the score is at least 9 or stop for user input.

## Final Report Format

When reporting completion to the user, include:

- issue number and URL,
- final quality score,
- whether adversarial review found blockers,
- Project item status and key fields,
- hard blocker links created or intentionally not created,
- Project Workflow Status for backlog, ready, in-progress, blocked, and done issues,
- soft ordering and parallelizable work summary,
- any metadata or Project field gaps.
